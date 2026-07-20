# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Plugin trust: nothing executes before an explicit, content-pinned trust record.

The trust record pins content, not just declaration, for every declared
capability file — executable and consumed-as-instructions alike: the
canonical-JSON manifest, plus every tool/provider target file, hook binary,
agent profile file, playbook file, and pack data file the manifest declares.
Any change to any of these reverts the plugin to ``changed`` and it stops
loading until re-approved. Trust is recorded user-level
(``~/.lionagi/settings.yaml``), never project-level.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any

from ._user_settings import locked_user_settings, read_user_settings
from .discovery import DiscoveredPlugin

__all__ = (
    "TrustState",
    "build_trust_disclosure",
    "compute_trust_hashes",
    "gc_trust_records",
    "read_trusted_plugins",
    "trust_plugin",
    "trust_state",
)


class TrustState(str, Enum):
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"
    CHANGED = "changed"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    """Hash *path*, or ``None`` if it can't be read (deleted/renamed/permission-denied).

    Callers treat ``None`` as "definitely not the previously-recorded hash" —
    a missing pinned file must revert a plugin to ``changed``, not crash
    every caller (`li plugin list`, agent-profile discovery) with an
    unhandled ``OSError``.
    """
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def compute_manifest_hash(discovered: DiscoveredPlugin) -> str:
    """sha256 of the canonical-JSON manifest (stable across YAML formatting/comment changes)."""
    assert discovered.manifest is not None
    canonical = json.dumps(
        discovered.manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_bytes(canonical.encode())


def compute_trust_hashes(discovered: DiscoveredPlugin) -> dict[str, Any]:
    """Return ``{"manifest": <hash>, "targets": {<rel_path>: <hash-or-None>, ...}}``.

    A target hashes to ``None`` when its file can't be read right now — see
    ``_sha256_file``. Never raises on a missing file.
    """
    targets = {rel: _sha256_file(discovered.bundle_dir / rel) for rel in discovered.declared_files}
    return {"manifest": compute_manifest_hash(discovered), "targets": targets}


def read_trusted_plugins() -> dict[str, Any]:
    settings = read_user_settings()
    trusted = settings.get("trusted_plugins", {})
    return trusted if isinstance(trusted, dict) else {}


def _bundle_dir_present(bundle_path: str) -> bool:
    try:
        return Path(bundle_path).is_dir()
    except OSError:
        return False


def gc_trust_records(discovered: list[DiscoveredPlugin]) -> list[str]:
    """Prune ``trusted_plugins`` entries whose bundle directory is confirmed gone (D7).

    Pruning is directory-presence, not manifest-parse-success: ``trust_plugin()``
    pins the bundle's absolute directory alongside the content hashes, and this
    only treats an entry as stale once that exact directory no longer exists on
    disk (D7's stated uninstall step is ``rm -r`` the bundle). A plugin whose
    ``plugin.yaml`` merely fails to parse right now — malformed edit in
    progress, a transient read error, a declared path escaping the bundle — is
    NOT the same thing as an uninstalled plugin; ``discover_plugins()`` still
    reports it (as a manifest-less ``DiscoveredPlugin``) as long as its
    directory is there, and its trust record must survive that untouched, per
    the ADR's stated "bundle directory has been removed" pruning condition.

    Trust records written before this bundle-path pin (legacy shape, no
    ``bundle_path`` key) fall back to the old parsed-manifest-name check —
    conservative in the opposite direction (it can still false-evict a legacy
    record on a transient parse failure), but every record trust_plugin()
    writes from here on carries the precise check, so this fallback is a
    migration window, not the steady-state behavior.

    Not just tidiness: if a genuinely-removed record were left in place and a
    *different* bundle later reappeared under the same name with content that
    happens to hash the same as what was pinned before (a stale checkout, a
    re-cloned repo), that lingering record would trust it silently — the exact
    resurrection D5's content-pinning is meant to prevent.

    Returns the pruned names, sorted, so the caller can report exactly what
    happened and why — this must never prune silently. Idempotent: a second
    call with nothing newly absent returns an empty list and writes nothing.
    """
    live_names = {d.manifest.name for d in discovered if d.manifest is not None}
    with locked_user_settings() as settings:
        trusted = settings.get("trusted_plugins", {})
        if not isinstance(trusted, dict) or not trusted:
            return []
        stale: list[str] = []
        for name, record in trusted.items():
            bundle_path = record.get("bundle_path") if isinstance(record, dict) else None
            if isinstance(bundle_path, str) and bundle_path:
                if not _bundle_dir_present(bundle_path):
                    stale.append(name)
            elif name not in live_names:
                stale.append(name)
        stale.sort()
        if not stale:
            return []
        for name in stale:
            trusted.pop(name, None)
        settings["trusted_plugins"] = trusted
    return stale


def trust_state(discovered: DiscoveredPlugin) -> TrustState:
    """Compute the current trust state of *discovered* against the recorded trust hashes."""
    assert discovered.manifest is not None
    record = read_trusted_plugins().get(discovered.manifest.name)
    if record is None:
        return TrustState.UNTRUSTED
    if not isinstance(record, dict):
        # A hand-edited settings.yaml can put anything under a plugin's key
        # (e.g. `trusted_plugins: {p1: true}`) — the well-formed shape this
        # module ever writes is always a dict (see trust_plugin() below), so
        # anything else can't be the record it looks like and must not be
        # dereferenced with .get() below. Treat it the same as "recorded
        # hashes don't match the current ones" rather than raising and
        # taking down every caller (discovery, `li plugin list`) with it.
        return TrustState.CHANGED
    current = compute_trust_hashes(discovered)
    if record.get("manifest") != current.get("manifest"):
        return TrustState.CHANGED
    if record.get("targets", {}) != current.get("targets", {}):
        return TrustState.CHANGED
    return TrustState.TRUSTED


def build_trust_disclosure(discovered: DiscoveredPlugin) -> dict[str, Any]:
    """Everything a plugin declares, rendered before the trust approval prompt.

    Complete and non-skippable: every hook command's full argv, every
    target/module path, and every profile/playbook/pack file — a bundle
    carrying many hook commands cannot bury one in an elided display.
    """
    assert discovered.manifest is not None
    manifest = discovered.manifest
    hooks: list[dict[str, Any]] = []
    for event, matchers in manifest.capabilities.hooks_external.items():
        for matcher in matchers:
            for hook in matcher.hooks:
                hooks.append(
                    {"event": event, "matcher": matcher.matcher, "argv": list(hook.command)}
                )
    return {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "lionagi": manifest.lionagi,
        "tools": [{"name": t.name, "target": t.target} for t in manifest.capabilities.tools],
        "hooks_external": hooks,
        "agents": list(manifest.capabilities.agents),
        "playbooks": list(manifest.capabilities.playbooks),
        "providers": [p.module for p in manifest.capabilities.providers],
        "packs": list(manifest.capabilities.packs),
    }


def trust_plugin(discovered: DiscoveredPlugin) -> dict[str, Any]:
    """Record trust for *discovered*: pins the manifest + every declared file's content hash,
    plus the bundle's resolved directory path (what ``gc_trust_records()`` checks for presence).

    Returns the disclosure payload that was (or should be) shown to the
    approver — callers render it before calling this, this call just persists
    the resulting hashes.

    Raises ``FileNotFoundError`` if a declared capability file can't be read:
    trusting is pinning content, so a bundle missing a file it declares can't
    be trusted rather than silently pinning a placeholder hash for it.
    """
    assert discovered.manifest is not None
    hashes = compute_trust_hashes(discovered)
    missing = sorted(rel for rel, h in hashes["targets"].items() if h is None)
    if missing:
        raise FileNotFoundError(
            f"cannot trust plugin {discovered.manifest.name!r}: declared file(s) "
            f"missing or unreadable: {', '.join(missing)}"
        )
    record = {**hashes, "bundle_path": str(discovered.bundle_dir.resolve())}
    with locked_user_settings() as settings:
        trusted = settings.setdefault("trusted_plugins", {})
        if not isinstance(trusted, dict):
            trusted = {}
            settings["trusted_plugins"] = trusted
        trusted[discovered.manifest.name] = record
    return build_trust_disclosure(discovered)
