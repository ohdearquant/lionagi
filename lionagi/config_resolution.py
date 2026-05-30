# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import os
import re
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class ResourceKind(Enum):
    AGENT = "AGENT"
    SKILL = "SKILL"
    PLAYBOOK = "PLAYBOOK"
    CHARTER = "CHARTER"
    HOOK = "HOOK"
    RUNNER = "RUNNER"
    GATE = "GATE"
    PRICING = "PRICING"
    SETTINGS = "SETTINGS"


_PROVENANCE_KEY = "_provenance"

_DEFAULT_CONFIGS: dict[ResourceKind, dict[str, Any]] = {kind: {} for kind in ResourceKind}


def _normalize_kind(kind: ResourceKind | str) -> ResourceKind:
    if isinstance(kind, ResourceKind):
        return kind
    if not isinstance(kind, str):
        raise TypeError(f"kind must be ResourceKind or str, got {type(kind).__name__}")

    normalized = kind.upper()
    if normalized in {k.name for k in ResourceKind}:
        return ResourceKind[normalized]

    for candidate in ResourceKind:
        if candidate.value == normalized:
            return candidate

    raise ValueError(f"unsupported resource kind: {kind!r}")


def _safe_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("name must be a non-empty string")
    if not name:
        raise ValueError("name must be a non-empty string")
    if "\x00" in name:
        raise ValueError("name must not contain NUL")
    if name.startswith("."):
        raise ValueError("name must not start with '.': path-traversal forbidden")
    if "/" in name or "\\" in name:
        raise ValueError("name must be a bare identifier, no path separators")
    if "*" in name or "?" in name or "[" in name or "]" in name:
        raise ValueError("name must not contain wildcard characters")
    return name


def _candidate_paths(kind: ResourceKind, root: Path, name: str) -> list[Path]:
    name_safe = _safe_name(name)

    if kind == ResourceKind.AGENT:
        return [
            root / "agents" / name_safe / f"{name_safe}.yaml",
            root / "agents" / name_safe / f"{name_safe}.yml",
            root / "agents" / f"{name_safe}.yaml",
            root / "agents" / f"{name_safe}.yml",
        ]

    if kind == ResourceKind.SKILL:
        return [
            root / "skills" / f"{name_safe}.yaml",
            root / "skills" / f"{name_safe}.yml",
        ]

    if kind == ResourceKind.PLAYBOOK:
        return [
            root / "playbooks" / f"{name_safe}.playbook.yaml",
            root / "playbooks" / f"{name_safe}.playbook.yml",
            root / "playbooks" / f"{name_safe}.yaml",
            root / "playbooks" / f"{name_safe}.yml",
        ]

    if kind == ResourceKind.CHARTER:
        return [
            root / "charters" / f"{name_safe}.yaml",
            root / "charters" / f"{name_safe}.yml",
        ]

    if kind == ResourceKind.HOOK:
        return [
            root / "hooks" / f"{name_safe}.yaml",
            root / "hooks" / f"{name_safe}.yml",
        ]

    if kind == ResourceKind.RUNNER:
        return [
            root / "runners" / f"{name_safe}.yaml",
            root / "runners" / f"{name_safe}.yml",
        ]

    if kind == ResourceKind.GATE:
        return [
            root / "gates" / f"{name_safe}.yaml",
            root / "gates" / f"{name_safe}.yml",
        ]

    if kind == ResourceKind.PRICING:
        return [
            root / "pricing" / f"{name_safe}.yaml",
            root / "pricing" / f"{name_safe}.yml",
        ]

    if kind == ResourceKind.SETTINGS:
        return [
            root / "settings.yaml",
            root / "settings.yml",
        ]

    raise ValueError(f"unsupported resource kind: {kind!r}")


def _candidate_roots(project: str | None = None) -> list[tuple[Path, str]]:
    roots: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def _add_root(path: Path, source: str) -> None:
        if path in seen:
            return
        seen.add(path)
        roots.append((path, source))

    if project is not None:
        project_root = Path(project).expanduser()
        if project_root.name == ".lionagi":
            _add_root(project_root, "project")
        else:
            _add_root(project_root / ".lionagi", "project")
    else:
        try:
            git_root = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=3,
            )
            if git_root.returncode == 0:
                top = Path(git_root.stdout.strip()) / ".lionagi"
                _add_root(top, "project")
        except (OSError, subprocess.TimeoutExpired):
            pass

        cwd = Path.cwd()
        for parent in [cwd, *cwd.parents]:
            _add_root(parent / ".lionagi", "project")

    _add_root(Path.home() / ".lionagi", "user")
    return roots


def _resolve_candidate(root: Path, candidate: Path) -> bool:
    if not candidate.is_file():
        return False
    try:
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(resolved_root)
        return True
    except (OSError, ValueError):
        return False


def _candidate_for_source(
    kind: ResourceKind,
    name: str,
    *,
    project: str | None = None,
    source: str,
) -> Path | None:
    for root, source_name in _candidate_roots(project):
        if source_name != source:
            continue
        if not root.is_dir():
            continue
        for candidate in _candidate_paths(kind, root, name):
            if _resolve_candidate(root, candidate):
                return candidate
    return None


def _load_yaml_file(path: Path) -> dict[str, Any]:
    raw = path.read_text()
    parsed = yaml.safe_load(raw)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"config file must contain a YAML mapping: {path}")
    return parsed


def _seed_provenance(
    data: dict[str, Any],
    provenance: dict[str, str],
    path: tuple[str, ...] = (),
) -> None:
    for key, value in data.items():
        key_path = path + (str(key),)
        if isinstance(value, dict):
            _seed_provenance(value, provenance, key_path)
            continue
        provenance[".".join(key_path)] = "default"


def _deep_merge(
    base: dict[str, Any],
    override: dict[str, Any],
    provenance: dict[str, str],
    source: str,
    path: tuple[str, ...] = (),
) -> None:
    for key, override_value in override.items():
        current_path = path + (str(key),)
        joined = ".".join(current_path)
        if isinstance(override_value, dict):
            if not isinstance(base.get(key), dict):
                base[key] = {}
            _deep_merge(base[key], override_value, provenance, source, current_path)
            continue

        base[key] = copy.deepcopy(override_value)
        provenance[joined] = source


def _env_key_variants(prefix: str, kind: ResourceKind, name: str) -> list[str]:
    safe = re.sub(r"[^A-Z0-9_]+", "_", name.upper())
    return [
        f"{prefix}_{kind.value}_{safe}",
        f"{prefix}_{kind.value}_{safe}_YAML",
    ]


def _load_override_from_env(
    prefix: str,
    kind: ResourceKind,
    name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    for key in _env_key_variants(prefix, kind, name):
        raw = os.environ.get(key)
        if raw is None:
            continue
        parsed = yaml.safe_load(raw)
        if parsed is None:
            return {}, key
        if not isinstance(parsed, dict):
            raise ValueError(f"environment override {key!r} must be a YAML mapping")
        return parsed, key
    return None, None


def resolve_resource_path(
    kind: ResourceKind | str,
    name: str,
    project: str | None = None,
) -> tuple[Path | None, str | None]:
    """Resolve the highest-priority config file for KIND and NAME.

    Returns:
        (path, source) when a candidate is found.
        (None, None) when no config file exists.
    """
    resolved = _normalize_kind(kind)
    normalized_name = _safe_name(name)

    for source in ("project", "user"):
        path = _candidate_for_source(
            resolved,
            normalized_name,
            project=project,
            source=source,
        )
        if path is not None:
            return path, source

    return None, None


def resolve_config(
    kind: ResourceKind | str,
    name: str,
    project: str | None = None,
) -> dict[str, Any]:
    """Resolve config with cascade and deep merge semantics.

    Precedence order:
        CLI-like override -> env override -> project file -> user file -> built-in defaults.

    YAML dictionaries are deep-merged recursively; lists are replaced.
    """
    resolved = _normalize_kind(kind)
    _safe_name(name)

    result: dict[str, Any] = copy.deepcopy(_DEFAULT_CONFIGS[resolved])
    provenance: dict[str, Any] = {
        "sources": {
            "default": "builtin",
            "user": None,
            "project": None,
            "env": None,
            "cli": None,
        },
        "keys": {},
    }

    _seed_provenance(result, provenance["keys"])

    for source in ("user", "project"):
        path = _candidate_for_source(resolved, name, project=project, source=source)
        if path is None:
            continue
        data = _load_yaml_file(path)
        if not data:
            continue
        provenance["sources"][source] = str(path)
        _deep_merge(result, data, provenance["keys"], source)

    env_data, env_key = _load_override_from_env("LIONAGI", resolved, name)
    if env_data is not None:
        provenance["sources"]["env"] = env_key
        _deep_merge(result, env_data, provenance["keys"], "env")

    cli_data, cli_key = _load_override_from_env("LIONAGI_CLI", resolved, name)
    if cli_data is not None:
        provenance["sources"]["cli"] = cli_key
        _deep_merge(result, cli_data, provenance["keys"], "cli")

    resolved_cfg: dict[str, Any] = copy.deepcopy(result)
    resolved_cfg[_PROVENANCE_KEY] = provenance
    return resolved_cfg


def list_resource_names(kind: ResourceKind | str, project: str | None = None) -> list[str]:
    """List available resource names for KIND across project and user roots."""
    resolved = _normalize_kind(kind)
    names: set[str] = set()

    for root, _source in _candidate_roots(project):
        if not root.is_dir():
            continue
        if resolved == ResourceKind.SETTINGS:
            continue

        if resolved == ResourceKind.PLAYBOOK:
            directory = root / "playbooks"
            if not directory.is_dir():
                continue
            for candidate in directory.iterdir():
                if not candidate.is_file() or candidate.suffix.lower() not in {".yaml", ".yml"}:
                    continue
                if candidate.name.endswith(".playbook.yaml"):
                    names.add(candidate.name[: -len(".playbook.yaml")])
                elif candidate.name.endswith(".playbook.yml"):
                    names.add(candidate.name[: -len(".playbook.yml")])
                else:
                    names.add(candidate.stem)
            continue

        directory = {
            ResourceKind.AGENT: "agents",
            ResourceKind.SKILL: "skills",
            ResourceKind.CHARTER: "charters",
            ResourceKind.HOOK: "hooks",
            ResourceKind.RUNNER: "runners",
            ResourceKind.GATE: "gates",
            ResourceKind.PRICING: "pricing",
        }[resolved]
        if not (root / directory).is_dir():
            continue
        for candidate in (root / directory).iterdir():
            if candidate.is_dir():
                direct_yaml = candidate / f"{candidate.name}.yaml"
                direct_yml = candidate / f"{candidate.name}.yml"
                if direct_yaml.is_file() or direct_yml.is_file():
                    names.add(candidate.name)
                continue

            if candidate.suffix.lower() in {".yaml", ".yml"}:
                names.add(candidate.stem)

    return sorted(names)
