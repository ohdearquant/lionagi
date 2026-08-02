# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Plugin discovery (stage 1 of two-stage load): scan ``<dir>/plugins/*/plugin.yaml``.
Data-only -- parses/validates manifests, never imports bundle code or runs at ``import lionagi`` time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lionagi._paths import find_lionagi_dirs
from lionagi.libs.path_safety import has_traversal, validate_bare_name

from .manifest import ManifestError, PluginManifest, parse_manifest, parse_tool_target

__all__ = (
    "DiscoveredPlugin",
    "discover_plugins",
)


@dataclass
class DiscoveredPlugin:
    """One ``.lionagi/plugins/<dir_name>/`` bundle found during a scan."""

    dir_name: str
    """The plugin's directory name — used for diagnostics before a manifest is known to parse."""
    bundle_dir: Path
    manifest_path: Path
    manifest: PluginManifest | None = None
    error: str | None = None
    """Set (manifest is None) when the manifest failed to parse or a declared path escaped the bundle."""
    declared_files: tuple[str, ...] = field(default_factory=tuple)
    """Bundle-relative paths the manifest declares (manifest itself + every capability file), for trust hashing."""


def _collect_declared_paths(manifest: PluginManifest) -> list[str]:
    """Every bundle-relative file the manifest declares; uses the same ``parse_tool_target``
    parse as ``registry.activate_target`` so the hashed file can never diverge from the imported one."""
    paths: list[str] = []
    for tool in manifest.capabilities.tools:
        path_part, _ = parse_tool_target(tool.target, label="tool target")
        paths.append(path_part)
    for matchers in manifest.capabilities.hooks_external.values():
        for matcher in matchers:
            for hook in matcher.hooks:
                if hook.command:
                    paths.append(hook.command[0])
    paths.extend(manifest.capabilities.agents)
    paths.extend(manifest.capabilities.playbooks)
    for provider in manifest.capabilities.providers:
        paths.append(provider.module)
    paths.extend(manifest.capabilities.packs)
    return paths


def _validate_bundle_relative(bundle_dir: Path, rel: str, *, label: str) -> None:
    """Raise ValueError if *rel* is empty, absolute, traversal-bearing, escapes *bundle_dir*,
    or contains ``:`` (reserved as the tool-target/callable separator)."""
    if not rel or not rel.strip():
        raise ValueError(f"{label} entry is empty")
    if ":" in rel:
        raise ValueError(
            f"{label} entry {rel!r} must not contain ':' "
            "(reserved as the tool-target/callable separator)"
        )
    candidate = Path(rel)
    if candidate.is_absolute():
        raise ValueError(f"{label} entry {rel!r} must be a bundle-relative path, not absolute")
    if has_traversal(candidate):
        raise ValueError(f"{label} entry {rel!r} contains directory traversal ('..')")
    resolved = (bundle_dir / candidate).resolve()
    try:
        resolved.relative_to(bundle_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} entry {rel!r} resolves outside the plugin bundle") from exc


def _validate_agent_profile_names(manifest: PluginManifest) -> None:
    """Reject agent filenames whose ``Path.stem`` (e.g. ``research.v2.md`` -> ``research.v2``)
    would fail ``load_agent_profile()``'s bare-identifier rule at load time instead."""
    for rel in manifest.capabilities.agents:
        validate_bare_name(Path(rel).stem, label=f"plugin {manifest.name!r} agent profile name")


def _scan_one(bundle_dir: Path) -> DiscoveredPlugin:
    manifest_path = bundle_dir / "plugin.yaml"
    dir_name = bundle_dir.name
    try:
        manifest = parse_manifest(manifest_path)
    except ManifestError as exc:
        return DiscoveredPlugin(
            dir_name=dir_name,
            bundle_dir=bundle_dir,
            manifest_path=manifest_path,
            manifest=None,
            error=str(exc),
        )

    try:
        declared = _collect_declared_paths(manifest)
        for rel in declared:
            _validate_bundle_relative(bundle_dir, rel, label=f"plugin {manifest.name!r} capability")
        _validate_agent_profile_names(manifest)
    except ValueError as exc:
        return DiscoveredPlugin(
            dir_name=dir_name,
            bundle_dir=bundle_dir,
            manifest_path=manifest_path,
            manifest=None,
            error=str(exc),
        )

    return DiscoveredPlugin(
        dir_name=dir_name,
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        manifest=manifest,
        declared_files=tuple(declared),
    )


def discover_plugins() -> list[DiscoveredPlugin]:
    """Scan every ``.lionagi/plugins/*/plugin.yaml`` bundle, project dirs first then global.
    A bundle with no ``plugin.yaml`` is skipped; one that fails validation or declares an
    escaping path returns with ``error`` set and ``manifest=None`` -- never aborts the rest of the scan.
    """
    discovered: list[DiscoveredPlugin] = []
    for lionagi_dir in find_lionagi_dirs():
        plugins_root = lionagi_dir / "plugins"
        if not plugins_root.is_dir():
            continue
        for child in sorted(plugins_root.iterdir()):
            if not child.is_dir():
                continue
            if not (child / "plugin.yaml").is_file():
                continue
            discovered.append(_scan_one(child))
    return discovered
