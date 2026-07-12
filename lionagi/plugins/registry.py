# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The plugin registry: combines discovery + trust + settings into one snapshot.

Two-stage laziness, mirroring ``EndpointRegistry._ensure_loaded``:

- **Stage 1 (this module, ``_ensure_loaded``)** — manifests are scanned and
  parsed the first time any consumer asks the registry anything. Cheap,
  data-only, cached for the process.
- **Stage 2 (``activate_target``)** — a declared ``target``/``module`` is
  imported only when that specific capability is actually invoked, never as a
  side effect of discovery, listing, or an unrelated capability of the same
  plugin firing. Import failures are cached per ``(plugin, target)`` so a
  raising module is reported once and never retried.

Nothing in this module runs at ``import lionagi`` time — the registry is
inert until a consumer calls one of its classmethods.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from lionagi.version import __version__ as _lionagi_version

from ._user_settings import read_user_settings
from .discovery import DiscoveredPlugin, discover_plugins
from .manifest import PluginManifest
from .trust import TrustState
from .trust import trust_state as _trust_state

__all__ = (
    "PluginActivationError",
    "PluginRecord",
    "PluginRegistry",
    "PluginState",
)


class PluginState(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    UNTRUSTED = "untrusted"
    CHANGED = "changed"
    INCOMPATIBLE = "incompatible"
    # Extensions beyond the core lifecycle-state vocabulary, needed so a
    # broken or colliding bundle is still visible in `li plugin list` instead
    # of silently vanishing — an unparseable manifest or a same-name
    # collision must surface loudly, not disappear.
    INVALID = "invalid"
    COLLISION = "collision"


@dataclass
class PluginRecord:
    """One plugin's resolved state — the row `li plugin list`/`info` render."""

    name: str
    """Manifest `name:` field (the namespace), or the directory name when the manifest is invalid."""
    dir_name: str
    bundle_dir: Path
    manifest_path: Path
    version: str | None
    state: PluginState
    enabled: bool
    manifest: PluginManifest | None = None
    error: str | None = None


class PluginActivationError(RuntimeError):
    """Stage-2 activation failure: names the plugin and manifest path, not a bare ImportError."""

    def __init__(self, plugin_name: str, target: str, message: str) -> None:
        self.plugin_name = plugin_name
        self.target = target
        super().__init__(message)


def _enabled_flag(name: str, settings: dict[str, Any]) -> bool:
    plugins_block = settings.get("plugins", {})
    if not isinstance(plugins_block, dict):
        return True
    entry = plugins_block.get(name, {})
    if not isinstance(entry, dict):
        return True
    return bool(entry.get("enabled", True))


def _tool_names(manifest: PluginManifest) -> list[str]:
    return [t.name for t in manifest.capabilities.tools]


# Cross-plugin same-name hard-collision applies only to the global,
# non-namespaced surface: tool names are called bare by the model, so there
# is no way to disambiguate two plugins both declaring the same tool name.
# Agent profiles and playbooks are addressable as `<plugin>/<name>` — two
# plugins each shipping (say) a "researcher" profile is not an error, it
# just makes the *bare* name ambiguous (resolved, or not, at the resolver —
# see lionagi.cli._providers); each stays independently reachable via its
# namespaced token. Packs follow the same reasoning.
_NAMED_SURFACES: tuple[tuple[str, Any], ...] = (("tools", _tool_names),)


def _build_snapshot() -> list[PluginRecord]:
    discovered = discover_plugins()
    settings = read_user_settings()
    records: list[PluginRecord] = []

    valid: list[DiscoveredPlugin] = []
    for d in discovered:
        if d.manifest is None:
            records.append(
                PluginRecord(
                    name=d.dir_name,
                    dir_name=d.dir_name,
                    bundle_dir=d.bundle_dir,
                    manifest_path=d.manifest_path,
                    version=None,
                    state=PluginState.INVALID,
                    enabled=False,
                    error=d.error,
                )
            )
        else:
            valid.append(d)

    # Two plugins declaring the same `name:` field is a namespace collision —
    # nothing else about them is well-defined until resolved.
    by_name: dict[str, list[DiscoveredPlugin]] = {}
    for d in valid:
        by_name.setdefault(d.manifest.name, []).append(d)  # type: ignore[union-attr]

    provisional: list[tuple[DiscoveredPlugin, PluginState | None, str | None]] = []
    for name, group in by_name.items():
        if len(group) > 1:
            dirs = ", ".join(str(d.bundle_dir) for d in group)
            msg = f"plugin name {name!r} declared by multiple bundles: {dirs}"
            for d in group:
                provisional.append((d, PluginState.COLLISION, msg))
            continue
        provisional.append((group[0], None, None))

    # Compatibility + enabled + trust, for the uniquely-named survivors.
    candidates: list[DiscoveredPlugin] = []
    for d, forced_state, forced_error in provisional:
        manifest = d.manifest
        assert manifest is not None
        if forced_state is not None:
            records.append(
                PluginRecord(
                    name=manifest.name,
                    dir_name=d.dir_name,
                    bundle_dir=d.bundle_dir,
                    manifest_path=d.manifest_path,
                    version=manifest.version,
                    state=forced_state,
                    enabled=_enabled_flag(manifest.name, settings),
                    manifest=manifest,
                    error=forced_error,
                )
            )
            continue

        if not manifest.is_compatible(_lionagi_version):
            records.append(
                PluginRecord(
                    name=manifest.name,
                    dir_name=d.dir_name,
                    bundle_dir=d.bundle_dir,
                    manifest_path=d.manifest_path,
                    version=manifest.version,
                    state=PluginState.INCOMPATIBLE,
                    enabled=_enabled_flag(manifest.name, settings),
                    manifest=manifest,
                    error=f"requires lionagi {manifest.lionagi!r}, installed {_lionagi_version!r}",
                )
            )
            continue

        enabled = _enabled_flag(manifest.name, settings)
        if not enabled:
            records.append(
                PluginRecord(
                    name=manifest.name,
                    dir_name=d.dir_name,
                    bundle_dir=d.bundle_dir,
                    manifest_path=d.manifest_path,
                    version=manifest.version,
                    state=PluginState.DISABLED,
                    enabled=False,
                    manifest=manifest,
                )
            )
            continue

        ts = _trust_state(d)
        if ts is TrustState.UNTRUSTED:
            records.append(
                PluginRecord(
                    name=manifest.name,
                    dir_name=d.dir_name,
                    bundle_dir=d.bundle_dir,
                    manifest_path=d.manifest_path,
                    version=manifest.version,
                    state=PluginState.UNTRUSTED,
                    enabled=True,
                    manifest=manifest,
                )
            )
            continue
        if ts is TrustState.CHANGED:
            records.append(
                PluginRecord(
                    name=manifest.name,
                    dir_name=d.dir_name,
                    bundle_dir=d.bundle_dir,
                    manifest_path=d.manifest_path,
                    version=manifest.version,
                    state=PluginState.CHANGED,
                    enabled=True,
                    manifest=manifest,
                    error="manifest or a declared capability file changed since trust was recorded",
                )
            )
            continue

        candidates.append(d)

    # Among plugins that would otherwise go active, a same-name capability
    # on the same data-only surface (or the global tool namespace) is a
    # hard error naming both plugins and the surface.
    collided: dict[str, str] = {}
    for surface, extractor in _NAMED_SURFACES:
        owners: dict[str, list[str]] = {}
        for d in candidates:
            for cap_name in extractor(d.manifest):
                owners.setdefault(cap_name, []).append(d.manifest.name)
        for cap_name, owner_names in owners.items():
            if len(owner_names) > 1:
                msg = (
                    f"capability {cap_name!r} on surface {surface!r} declared by "
                    f"multiple enabled plugins: {', '.join(owner_names)}"
                )
                for owner in owner_names:
                    collided[owner] = msg

    for d in candidates:
        manifest = d.manifest
        assert manifest is not None
        if manifest.name in collided:
            records.append(
                PluginRecord(
                    name=manifest.name,
                    dir_name=d.dir_name,
                    bundle_dir=d.bundle_dir,
                    manifest_path=d.manifest_path,
                    version=manifest.version,
                    state=PluginState.COLLISION,
                    enabled=True,
                    manifest=manifest,
                    error=collided[manifest.name],
                )
            )
            continue
        records.append(
            PluginRecord(
                name=manifest.name,
                dir_name=d.dir_name,
                bundle_dir=d.bundle_dir,
                manifest_path=d.manifest_path,
                version=manifest.version,
                state=PluginState.ACTIVE,
                enabled=True,
                manifest=manifest,
            )
        )

    return records


def _import_bundle_module(file_path: Path, *, module_key: str) -> Any:
    if not file_path.is_file():
        raise FileNotFoundError(f"target file not found: {file_path}")
    spec = importlib.util.spec_from_file_location(module_key, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create import spec for {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_key, None)
        raise
    return module


class PluginRegistry:
    """Process-cached plugin inventory. Discovery runs on first access, never at import time."""

    _snapshot: ClassVar[list[PluginRecord] | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()
    _activation_cache: ClassVar[dict[tuple[str, str], Any]] = {}
    _activation_errors: ClassVar[dict[tuple[str, str], str]] = {}

    @classmethod
    def _ensure_loaded(cls) -> list[PluginRecord]:
        if cls._snapshot is not None:
            return cls._snapshot
        with cls._lock:
            if cls._snapshot is None:
                cls._snapshot = _build_snapshot()
            return cls._snapshot

    @classmethod
    def reset(cls) -> None:
        """Drop the cached scan and activation cache. For tests and CLI invocations that want a fresh read."""
        with cls._lock:
            cls._snapshot = None
            cls._activation_cache = {}
            cls._activation_errors = {}

    @classmethod
    def list_plugins(cls) -> list[PluginRecord]:
        return list(cls._ensure_loaded())

    @classmethod
    def get(cls, name: str) -> PluginRecord | None:
        for record in cls._ensure_loaded():
            if record.name == name:
                return record
        return None

    @classmethod
    def active_agent_profile_files(cls) -> dict[str, tuple[str, Path]]:
        """``<plugin>/<name>`` -> (plugin name, absolute profile path), for every ACTIVE plugin.

        Consumed by ``lionagi.cli._providers``: a miss in the project/global
        agent-profile search joins this list.
        """
        out: dict[str, tuple[str, Path]] = {}
        for record in cls._ensure_loaded():
            if record.state is not PluginState.ACTIVE or record.manifest is None:
                continue
            for rel in record.manifest.capabilities.agents:
                stem = Path(rel).stem
                out[f"{record.name}/{stem}"] = (record.name, record.bundle_dir / rel)
        return out

    @classmethod
    def activate_target(cls, plugin_name: str, target: str) -> Any:
        """Stage 2: resolve a bundle-relative ``path.py:callable`` (or bare ``path.py`` module) reference.

        Imported only on first use, cached (success or failure) — a raising
        module is reported once and never retried.
        """
        cache_key = (plugin_name, target)
        if cache_key in cls._activation_errors:
            raise PluginActivationError(plugin_name, target, cls._activation_errors[cache_key])
        if cache_key in cls._activation_cache:
            return cls._activation_cache[cache_key]

        record = cls.get(plugin_name)
        if record is None or record.state is not PluginState.ACTIVE or record.manifest is None:
            msg = f"plugin {plugin_name!r} is not active (no such plugin, or untrusted/disabled/incompatible)"
            cls._activation_errors[cache_key] = msg
            raise PluginActivationError(plugin_name, target, msg)

        module_path, _, attr = target.partition(":")
        file_path = record.bundle_dir / module_path
        try:
            module = _import_bundle_module(
                file_path,
                module_key=f"_lionagi_plugin_{plugin_name}__{module_path.replace('/', '_')}",
            )
            result: Any = getattr(module, attr) if attr else module
        except Exception as exc:  # noqa: BLE001 — re-wrapped with plugin/manifest context, not a bare traceback
            msg = f"failed to activate {target!r} from plugin {plugin_name!r} ({record.manifest_path}): {exc}"
            cls._activation_errors[cache_key] = msg
            raise PluginActivationError(plugin_name, target, msg) from exc

        cls._activation_cache[cache_key] = result
        return result
