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
  plugin firing. Trust is revalidated fresh on *every* call, re-reading
  ``plugin.yaml`` from disk rather than trusting the process-cached snapshot
  (see ``_rescan``) — a previously-activated target stops being handed out
  the moment a declared file, or the manifest itself, no longer verifies.
  The target file that actually gets executed is read exactly once and its
  hash checked against the currently-recorded trust entry for that path in
  that same read (see ``_read_and_verify_target_bytes``) — the check and the
  bytes that get compiled/exec'd are never two separate reads of the file,
  which would leave a window for the file to be swapped in between. Import
  results (success or failure) are cached per ``(plugin, target, content
  hash)``, so re-trusting changed content is a guaranteed cache miss rather
  than depending on an earlier call happening to have evicted the old entry.

Nothing in this module runs at ``import lionagi`` time — the registry is
inert until a consumer calls one of its classmethods.
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
    declared_files: tuple[str, ...] = ()
    """Bundle-relative paths the manifest declares — carried through so a capability
    chokepoint can recompute trust fresh without re-running discovery."""


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
    """Re-scan *record*'s bundle directory fresh, right now — including re-reading
    and re-parsing ``plugin.yaml`` itself, not the already-parsed manifest object
    cached on the process-wide snapshot (``record.manifest``).

    The process-wide snapshot (``PluginRegistry._snapshot``) is built once and
    cached — an ``ACTIVE`` record in it can go stale the moment any declared
    file, *or the manifest itself*, is edited after that first scan, and
    nothing re-scans it until ``reset()``. Reusing ``record.manifest`` to
    recompute trust only catches drift in the individually-hashed target
    files; the manifest hash is computed by re-serializing the *parsed
    manifest object*, so a stale cached object always re-derives the same
    "trusted" hash regardless of what's actually on disk in ``plugin.yaml``
    right now. Returns ``None`` if the manifest no longer parses at all
    (edited into something invalid, corrupted, or a declared path no longer
    validates) — definitely not the content that was ever hashed and
    approved.
    """
    fresh = _scan_one(record.bundle_dir)
    return fresh if fresh.manifest is not None else None


def _target_resolution_map(manifest: PluginManifest) -> dict[str, tuple[str, str | None]]:
    """Map each declared, activatable target string to ``(module_path, attr_or_None)``.

    Built directly from the manifest's own typed capability lists — a
    tool's path/callable split comes from ``parse_tool_target`` on
    ``tool.target`` (the exact call ``discovery._collect_declared_paths``
    makes when deciding what to hash), never by re-splitting the caller's
    ``target`` argument independently. That's what makes the file that gets
    imported here provably the same file that got content-hashed at trust
    time — two separately written splitting expressions could disagree on
    where a target's path ends and its callable begins; one shared parser
    over the manifest's own field can't.

    Only tools and providers name Python-importable code; hooks run as
    external commands and agents/playbooks/packs are read as file content,
    so those never flow through this path. A caller-supplied target that
    isn't literally a key in this map (an extra file in the bundle, a
    traversal-shaped string, a typo) is rejected before any import is
    attempted — activation must stay confined to what the trust disclosure
    actually showed the approver.
    """
    resolved: dict[str, tuple[str, str | None]] = {}
    for tool in manifest.capabilities.tools:
        path_part, callable_name = parse_tool_target(tool.target, label="tool target")
        resolved[tool.target] = (path_part, callable_name)
    for provider in manifest.capabilities.providers:
        resolved[provider.module] = (provider.module, None)
    return resolved


def _read_and_verify_target_bytes(*, bundle_dir: Path, module_path: str, plugin_name: str) -> bytes:
    """Read the activation target's bytes exactly once, verify them against the
    currently-recorded trust hash for that exact declared path, and hand back
    those same bytes for the caller to compile/exec directly.

    An earlier, broader trust check (the ``_rescan``/``_trust_state`` pair in
    ``activate_target``) also hashes this same file as part of validating the
    whole plugin, but that read is not what gets executed — if the file were
    hashed there and then reopened separately for import, an atomic
    replacement of the file in between
    would execute content that was never verified. This function is the one
    read that matters for that guarantee: the hash it checks and the bytes
    it returns come from the exact same ``read_bytes()`` call, with nothing
    reopening the path in between.
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
    """Compile and exec pre-read *source* bytes into a fresh module.

    Deliberately takes already-read bytes rather than a file path: a version
    of this function that re-read the file itself would reopen it, undoing
    the single-read guarantee ``_read_and_verify_target_bytes`` exists to
    provide. Also deliberately not ``importlib``'s
    ``spec_from_file_location``/``exec_module`` path, which writes and reads
    a ``__pycache__`` ``.pyc`` validated by a *second*-granularity source
    mtime: two writes to the same target within the same wall-clock second
    are indistinguishable to it, so a re-import right after a re-trusted
    edit can silently execute stale bytecode instead of the content that was
    just re-hashed and approved. Compiling the given bytes directly has no
    cache to go stale.
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
    # Keyed by (plugin_name, target, content_hash-of-the-target-file) rather
    # than just (plugin_name, target): a stale entry from before an edit
    # simply lives under a different key than the post-re-trust content, so
    # re-trusting changed bytes is structurally a cache miss, not something
    # that depends on an earlier call having evicted the old entry first.
    _activation_cache: ClassVar[dict[tuple[str, str, str], Any]] = {}
    _activation_errors: ClassVar[dict[tuple[str, str, str], str]] = {}

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
            fresh = _rescan(record)
            if fresh is None or _trust_state(fresh) is not TrustState.TRUSTED:
                continue
            assert fresh.manifest is not None
            for rel in fresh.manifest.capabilities.agents:
                stem = Path(rel).stem
                out[f"{record.name}/{stem}"] = (record.name, fresh.bundle_dir / rel)
        return out

    @classmethod
    def active_provider_targets(cls) -> list[tuple[str, str]]:
        """``(plugin_name, module)`` pairs for every declared provider capability, across every ACTIVE plugin.

        Consumed by ``EndpointRegistry.match`` (``lionagi.service.connections.registry``)
        on a provider-resolution miss: the manifest schema names only the
        bundle-relative module to import (a provider module self-registers
        the provider name it serves via ``@register_endpoint`` as an import
        side effect — there is no separate declared-name field to filter on
        ahead of time), so the caller imports each returned module through
        ``activate_target`` and re-runs its match afterward.
        """
        out: list[tuple[str, str]] = []
        for record in cls._ensure_loaded():
            if record.state is not PluginState.ACTIVE or record.manifest is None:
                continue
            fresh = _rescan(record)
            if fresh is None or _trust_state(fresh) is not TrustState.TRUSTED:
                continue
            assert fresh.manifest is not None
            for cap in fresh.manifest.capabilities.providers:
                out.append((record.name, cap.module))
        return out

    @classmethod
    def activate_target(cls, plugin_name: str, target: str) -> Any:
        """Stage 2: resolve a bundle-relative ``path.py:callable`` (or bare ``path.py`` module) reference.

        Trust is revalidated fresh on *every* call, re-reading ``plugin.yaml``
        and every declared file from disk right now rather than trusting the
        process-cached snapshot: an already-activated target must stop being
        handed out the moment a declared file, or the manifest itself,
        changes — not just refuse brand-new activations. Once that broad
        check passes, the specific target file is read exactly once more —
        that read's hash, checked against the currently-recorded trust entry
        for it, and the bytes that get compiled/exec'd, are the same read
        (see ``_read_and_verify_target_bytes``); nothing here hashes the file
        and then separately reopens it to execute, which would leave a
        window for the file to be swapped in between.

        Imported only on first use, cached (success or failure) by
        ``(plugin, target, content hash)`` — a raising module is reported
        once and never retried for the *same* content, but re-trusted,
        changed content always misses the cache rather than depending on an
        earlier call having evicted the stale entry.
        """
        record = cls.get(plugin_name)
        if record is None or record.state is not PluginState.ACTIVE or record.manifest is None:
            msg = f"plugin {plugin_name!r} is not active (no such plugin, or untrusted/disabled/incompatible)"
            raise PluginActivationError(plugin_name, target, msg)

        fresh = _rescan(record)
        if fresh is None or _trust_state(fresh) is not TrustState.TRUSTED:
            msg = (
                f"plugin {plugin_name!r} is no longer trusted (the manifest or a "
                f"declared file changed since the cached scan) — re-run "
                f"`li plugin trust {plugin_name}` or `li plugin list` to refresh"
            )
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
