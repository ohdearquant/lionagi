# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The plugin registry: combines discovery + trust + settings into one
snapshot. Two-stage laziness (scan-only, then per-call revalidated
activation); nothing runs at ``import lionagi`` time. See docs/internals/runtime.md.
"""

from __future__ import annotations

import hashlib
import sys
import threading
import types
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from lionagi.version import __version__ as _lionagi_version

from ._user_settings import read_user_settings
from .discovery import DiscoveredPlugin, _scan_one, discover_plugins
from .manifest import PluginManifest, parse_tool_target
from .trust import TrustState
from .trust import read_trusted_plugins as _read_trusted_plugins
from .trust import trust_state as _trust_state

__all__ = (
    "PluginActivationError",
    "PluginRecord",
    "PluginRegistry",
    "PluginState",
    "PluginToolCollisionError",
    "ToolTarget",
)


class PluginState(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    UNTRUSTED = "untrusted"
    CHANGED = "changed"
    INCOMPATIBLE = "incompatible"
    # Extensions beyond the core lifecycle vocabulary, so a broken/colliding
    # bundle stays visible in `li plugin list` instead of vanishing silently.
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
    declared_files: tuple[str, ...] = ()
    """Bundle-relative paths the manifest declares — carried through so a capability
    chokepoint can recompute trust fresh without re-running discovery."""


class PluginActivationError(RuntimeError):
    """Stage-2 activation failure: names the plugin and manifest path, not a bare ImportError."""

    def __init__(self, plugin_name: str, target: str, message: str) -> None:
        self.plugin_name = plugin_name
        self.target = target
        super().__init__(message)


@dataclass
class ToolTarget:
    """A plugin-declared tool resolved for a consumer (e.g. ``ActionManager``):
    which plugin owns it, and its ``target`` string for ``activate_target``."""

    plugin_name: str
    target: str


class PluginToolCollisionError(RuntimeError):
    """ADR-0088 D6: two enabled plugins declare the same non-namespaced tool
    name (called bare, no namespace to disambiguate) — a hard error, not a shadow."""

    def __init__(self, tool_name: str, message: str) -> None:
        self.tool_name = tool_name
        super().__init__(message)


def _enabled_flag(name: str, settings: dict[str, Any]) -> bool:
    plugins_block = settings.get("plugins", {})
    if not isinstance(plugins_block, dict):
        return True
    entry = plugins_block.get(name, {})
    if not isinstance(entry, dict):
        return True
    return bool(entry.get("enabled", True))


def _is_live_active(manifest: PluginManifest) -> bool:
    """Compatible + enabled, read fresh from settings — never from a cached ``PluginRecord``."""
    return manifest.is_compatible(_lionagi_version) and _enabled_flag(
        manifest.name, read_user_settings()
    )


def _tool_names(manifest: PluginManifest) -> list[str]:
    return [t.name for t in manifest.capabilities.tools]


# Hard-collision applies only to the global, non-namespaced tool surface;
# profiles/playbooks/packs are addressable as `<plugin>/<name>` instead.
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
                    declared_files=d.declared_files,
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
                    declared_files=d.declared_files,
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
                    declared_files=d.declared_files,
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
                    declared_files=d.declared_files,
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
                    declared_files=d.declared_files,
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
                    declared_files=d.declared_files,
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
                    declared_files=d.declared_files,
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
                declared_files=d.declared_files,
            )
        )

    return records


def _rescan(record: PluginRecord) -> DiscoveredPlugin | None:
    """Re-scan *record*'s bundle directory fresh, re-reading and re-parsing
    ``plugin.yaml`` itself rather than the cached ``record.manifest``.
    Returns ``None`` if the manifest no longer parses. See docs/internals/runtime.md.
    """
    fresh = _scan_one(record.bundle_dir)
    return fresh if fresh.manifest is not None else None


def _tool_target_for(manifest: PluginManifest, tool_name: str) -> str | None:
    """The declared ``target`` string for *tool_name* in *manifest*, or ``None``."""
    for tool in manifest.capabilities.tools:
        if tool.name == tool_name:
            return tool.target
    return None


def _fresh_active_plugins(
    records: list[PluginRecord],
) -> tuple[
    dict[str, DiscoveredPlugin],
    dict[str, list[str]],
    dict[str, list[DiscoveredPlugin]],
]:
    """Rebuild live eligibility and tool ownership from freshly scanned
    manifests; process-cached records are only bundle-directory candidates.
    """
    by_name: dict[str, list[DiscoveredPlugin]] = {}
    for record in records:
        fresh = _rescan(record)
        if fresh is None:
            continue
        assert fresh.manifest is not None
        by_name.setdefault(fresh.manifest.name, []).append(fresh)

    collided_names = {name for name, group in by_name.items() if len(group) > 1}
    candidates: list[DiscoveredPlugin] = []
    for name, group in by_name.items():
        if name in collided_names:
            continue
        fresh = group[0]
        assert fresh.manifest is not None
        if not _is_live_active(fresh.manifest):
            continue
        if _trust_state(fresh) is not TrustState.TRUSTED:
            continue
        candidates.append(fresh)

    tool_owners: dict[str, list[str]] = {}
    for fresh in candidates:
        assert fresh.manifest is not None
        for tool_name in _tool_names(fresh.manifest):
            tool_owners.setdefault(tool_name, []).append(fresh.manifest.name)

    for owner_names in tool_owners.values():
        if len(owner_names) > 1:
            collided_names.update(owner_names)

    active = {
        fresh.manifest.name: fresh
        for fresh in candidates
        if fresh.manifest is not None and fresh.manifest.name not in collided_names
    }
    return active, tool_owners, by_name


def _target_resolution_map(manifest: PluginManifest) -> dict[str, tuple[str, str | None]]:
    """Map each declared, activatable target string to ``(module_path,
    attr_or_None)``, using the same ``parse_tool_target`` split the hashing
    path uses. Only tools/providers are Python-importable; see docs/internals/runtime.md.
    """
    resolved: dict[str, tuple[str, str | None]] = {}
    for tool in manifest.capabilities.tools:
        path_part, callable_name = parse_tool_target(tool.target, label="tool target")
        resolved[tool.target] = (path_part, callable_name)
    for provider in manifest.capabilities.providers:
        resolved[provider.module] = (provider.module, None)
    return resolved


def _read_and_verify_target_bytes(*, bundle_dir: Path, module_path: str, plugin_name: str) -> bytes:
    """Read the activation target's bytes exactly once, verify against the
    trust hash, and return those same bytes to compile/exec — the single
    read closes a TOCTOU window a hash-then-reopen sequence would leave open.
    """
    file_path = bundle_dir / module_path
    try:
        source_bytes = file_path.read_bytes()
    except OSError as exc:
        raise FileNotFoundError(f"target file not found: {file_path}") from exc

    trusted_entry = _read_trusted_plugins().get(plugin_name)
    targets = trusted_entry.get("targets", {}) if isinstance(trusted_entry, dict) else {}
    recorded_hash = targets.get(module_path)
    current_hash = hashlib.sha256(source_bytes).hexdigest()
    if recorded_hash is None or recorded_hash != current_hash:
        raise PermissionError(
            f"{module_path!r} content does not match the trusted hash recorded for "
            f"plugin {plugin_name!r} — re-run `li plugin trust {plugin_name}`"
        )
    return source_bytes


def _exec_bundle_module(source: bytes, *, file_path: Path, module_key: str) -> Any:
    """Compile and exec pre-read *source* bytes into a fresh module. Takes
    bytes, not a path (preserves the single-read guarantee), and compiles
    directly rather than through importlib's mtime-cached ``.pyc`` path.
    See docs/internals/runtime.md.
    """
    module = types.ModuleType(module_key)
    module.__file__ = str(file_path)
    sys.modules[module_key] = module
    try:
        code = compile(source, str(file_path), "exec")
        exec(code, module.__dict__)  # noqa: S102 — the bundle content this trust model is built to gate
    except BaseException:
        sys.modules.pop(module_key, None)
        raise
    return module


class PluginRegistry:
    """Process-cached plugin inventory. Discovery runs on first access, never at import time."""

    _snapshot: ClassVar[list[PluginRecord] | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()
    # Monotonic counter bumped every time _snapshot is (re)built. id()-based
    # tokens are reusable once the prior snapshot list is garbage-collected,
    # so a rebuilt snapshot could get the SAME id() as an earlier one a
    # caller had cached -- making a stale entry look current and skipping
    # the post-reset trust/enablement/peer-collision recheck. A counter that
    # only ever increases cannot repeat.
    _generation: ClassVar[int] = 0
    # Keyed by (plugin_name, target, content_hash) so re-trusted content is
    # structurally a cache miss, not dependent on prior eviction.
    _activation_cache: ClassVar[dict[tuple[str, str, str], Any]] = {}
    _activation_errors: ClassVar[dict[tuple[str, str, str], str]] = {}

    @classmethod
    def _ensure_loaded(cls) -> list[PluginRecord]:
        if cls._snapshot is not None:
            return cls._snapshot
        with cls._lock:
            if cls._snapshot is None:
                cls._snapshot = _build_snapshot()
                cls._generation += 1
            return cls._snapshot

    @classmethod
    def reset(cls) -> None:
        """Drop the cached scan and activation cache. For tests and CLI invocations that want a fresh read."""
        with cls._lock:
            cls._snapshot = None
            cls._activation_cache = {}
            cls._activation_errors = {}

    @classmethod
    def snapshot_generation(cls) -> int:
        """Cheap process-lifetime token for the cached plugin scan: identical
        across calls until ``reset()`` forces a rebuild, at which point it
        strictly increases. Lets a caller that already fully validated a
        plugin against the live snapshot cheaply detect ``nothing has been
        reset since`` without repeating the scan itself; never a substitute
        for the full, per-target trust/eligibility check ``activate_target()``
        performs whenever this token *does* change. A monotonic counter
        (not ``id()`` of the cached snapshot list) so the token can never
        repeat across the process lifetime, even after many reset() cycles
        recycle the same memory address for a new snapshot object.
        """
        cls._ensure_loaded()
        return cls._generation

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
        """``<plugin>/<name>`` -> (plugin name, absolute profile path), for
        every ACTIVE plugin. Consumed by ``lionagi.cli._providers``.
        """
        out: dict[str, tuple[str, Path]] = {}
        active, _, _ = _fresh_active_plugins(cls._ensure_loaded())
        for plugin_name, fresh in active.items():
            assert fresh.manifest is not None
            for rel in fresh.manifest.capabilities.agents:
                stem = Path(rel).stem
                out[f"{plugin_name}/{stem}"] = (plugin_name, fresh.bundle_dir / rel)
        return out

    @classmethod
    def active_playbook_files(cls) -> dict[str, tuple[str, Path]]:
        """``<plugin>/<name>`` -> (plugin name, absolute playbook path), for
        every ACTIVE plugin. Consumed by ``lionagi.cli.orchestrate``.
        """
        out: dict[str, tuple[str, Path]] = {}
        active, _, _ = _fresh_active_plugins(cls._ensure_loaded())
        for plugin_name, fresh in active.items():
            assert fresh.manifest is not None
            for rel in fresh.manifest.capabilities.playbooks:
                stem = Path(rel).stem.removesuffix(".playbook")
                out[f"{plugin_name}/{stem}"] = (plugin_name, fresh.bundle_dir / rel)
        return out

    @classmethod
    def active_provider_targets(cls) -> list[tuple[str, str]]:
        """``(plugin_name, module)`` pairs for every declared provider
        capability, across every live-eligible plugin. Consumed by
        ``EndpointRegistry.match`` on a resolution miss; see docs/internals/runtime.md.
        """
        active, _, _ = _fresh_active_plugins(cls._ensure_loaded())
        out: list[tuple[str, str]] = []
        for plugin_name, fresh in active.items():
            assert fresh.manifest is not None
            for cap in fresh.manifest.capabilities.providers:
                out.append((plugin_name, cap.module))
        return out

    @classmethod
    def resolve_tool_target(cls, tool_name: str) -> ToolTarget | None:
        """ADR-0088 D3: ``ActionManager`` tool-name-resolution miss. Returns
        the target when exactly one live-eligible plugin declares *tool_name*,
        ``None`` on a true miss, raises ``PluginToolCollisionError`` on >1 (D6).
        """
        active, tool_owners, _ = _fresh_active_plugins(cls._ensure_loaded())
        owner_names = tool_owners.get(tool_name, [])
        distinct_owners = list(dict.fromkeys(owner_names))
        if len(distinct_owners) > 1:
            names = ", ".join(distinct_owners)
            msg = (
                f"tool {tool_name!r} is declared by multiple enabled plugins "
                f"({names}) — disable one with `li plugin disable <name>`"
            )
            raise PluginToolCollisionError(tool_name, msg)
        if len(owner_names) != 1:
            return None
        plugin_name = owner_names[0]
        fresh = active.get(plugin_name)
        if fresh is None:
            return None
        assert fresh.manifest is not None
        target = _tool_target_for(fresh.manifest, tool_name)
        assert target is not None
        return ToolTarget(plugin_name=plugin_name, target=target)

    @classmethod
    def activate_target(cls, plugin_name: str, target: str) -> Any:
        """Stage 2: resolve a bundle-relative ``path.py:callable`` (or bare
        ``path.py`` module) reference. Eligibility and trust are revalidated
        fresh on every call, never from the cached snapshot. See
        docs/internals/runtime.md for the single-read and cache-key contract.
        """
        active, _, by_name = _fresh_active_plugins(cls._ensure_loaded())
        fresh = active.get(plugin_name)
        if fresh is None:
            matches = by_name.get(plugin_name, [])
            if len(matches) == 1:
                candidate = matches[0]
                assert candidate.manifest is not None
                if (
                    _is_live_active(candidate.manifest)
                    and _trust_state(candidate) is not TrustState.TRUSTED
                ):
                    msg = (
                        f"plugin {plugin_name!r} is no longer trusted (the manifest or a "
                        f"declared file changed since the cached scan) — re-run "
                        f"`li plugin trust {plugin_name}` or `li plugin list` to refresh"
                    )
                    raise PluginActivationError(plugin_name, target, msg)
            msg = f"plugin {plugin_name!r} is not active (no such plugin, or untrusted/disabled/incompatible)"
            raise PluginActivationError(plugin_name, target, msg)
        assert fresh.manifest is not None

        resolution = _target_resolution_map(fresh.manifest)
        if target not in resolution:
            msg = (
                f"target {target!r} is not declared by plugin {plugin_name!r}'s manifest "
                "(only tool/provider targets can be activated)"
            )
            raise PluginActivationError(plugin_name, target, msg)

        module_path, attr = resolution[target]
        try:
            source_bytes = _read_and_verify_target_bytes(
                bundle_dir=fresh.bundle_dir, module_path=module_path, plugin_name=plugin_name
            )
        except (FileNotFoundError, PermissionError) as exc:
            msg = f"failed to activate {target!r} from plugin {plugin_name!r} ({fresh.manifest_path}): {exc}"
            raise PluginActivationError(plugin_name, target, msg) from exc

        cache_key = (plugin_name, target, hashlib.sha256(source_bytes).hexdigest())
        if cache_key in cls._activation_errors:
            raise PluginActivationError(plugin_name, target, cls._activation_errors[cache_key])
        if cache_key in cls._activation_cache:
            return cls._activation_cache[cache_key]

        try:
            module = _exec_bundle_module(
                source_bytes,
                file_path=fresh.bundle_dir / module_path,
                module_key=f"_lionagi_plugin_{plugin_name}__{module_path.replace('/', '_')}",
            )
            result: Any = getattr(module, attr) if attr else module
        except Exception as exc:  # noqa: BLE001 — re-wrapped with plugin/manifest context, not a bare traceback
            msg = f"failed to activate {target!r} from plugin {plugin_name!r} ({fresh.manifest_path}): {exc}"
            cls._activation_errors[cache_key] = msg
            raise PluginActivationError(plugin_name, target, msg) from exc

        cls._activation_cache[cache_key] = result
        return result
