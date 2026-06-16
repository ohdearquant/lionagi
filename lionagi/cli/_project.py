# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0026: project detection cascade (config.toml → global overrides → git remote → None)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

_SOURCE_CONFIG_TOML = "config_toml"
_SOURCE_GLOBAL_OVERRIDE = "global_override"
_SOURCE_GIT_REMOTE = "git_remote"


def detect_project(cwd: Path | None = None) -> tuple[str | None, str | None]:
    """Detect the current project from cwd context.

    Returns (project_name, project_source) where project_source is one of:
    ``config_toml``, ``global_override``, ``git_remote``, or ``None``.
    """
    cwd = cwd or Path.cwd()

    result = _from_config_toml(cwd)
    if result[0]:
        return result

    git_root = _find_git_root(cwd)
    remote_slug = _git_remote_slug(git_root) if git_root else None

    result = _from_global_overrides(cwd, remote_slug)
    if result[0]:
        return result

    if remote_slug:
        return (remote_slug, _SOURCE_GIT_REMOTE)

    return (None, None)


def _from_config_toml(cwd: Path) -> tuple[str | None, str | None]:
    """Walk up from cwd looking for .lionagi/config.toml with [project].name."""
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".lionagi" / "config.toml"
        if candidate.is_file():
            name = _read_project_from_toml(candidate)
            if name:
                return (name, _SOURCE_CONFIG_TOML)
            break
    return (None, None)


def _read_project_from_toml(path: Path) -> str | None:
    """Parse [project].name from a TOML file; uses tomllib (3.11+) or the declared toml dep (3.10)."""
    try:
        try:
            import tomllib

            with open(path, "rb") as f:
                data = tomllib.load(f)
        except ModuleNotFoundError:
            # Python 3.10: use the declared `toml` dependency (pyproject.toml
            # declares toml>=0.10.2).  `toml.load` accepts a text-mode file.
            import toml  # type: ignore[import-untyped]

            with open(path) as f:
                data = toml.load(f)
        project = data.get("project", {})
        if isinstance(project, dict):
            name = project.get("name")
            return str(name) if name else None
    except Exception:
        return None
    return None


def _from_global_overrides(cwd: Path, remote_slug: str | None) -> tuple[str | None, str | None]:
    """Check ~/.lionagi/settings.yaml project_overrides."""
    global_path = Path.home() / ".lionagi" / "settings.yaml"
    if not global_path.is_file():
        return (None, None)

    try:
        import yaml

        with open(global_path) as f:
            settings = yaml.safe_load(f) or {}
    except Exception:
        return (None, None)

    overrides: dict[str, Any] = settings.get("project_overrides", {})
    if not isinstance(overrides, dict):
        return (None, None)

    if remote_slug and remote_slug in overrides:
        return (str(overrides[remote_slug]), _SOURCE_GLOBAL_OVERRIDE)

    cwd_str = str(cwd)
    for key, project_name in overrides.items():
        if key.startswith("/") and cwd_str.startswith(key):
            return (str(project_name), _SOURCE_GLOBAL_OVERRIDE)

    return (None, None)


def _find_git_root(cwd: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:  # noqa: S110
        return None
    return None


def _git_remote_slug(git_root: Path) -> str | None:
    """Extract org/repo from the origin remote URL."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],  # noqa: S607
            capture_output=True,
            text=True,
            cwd=str(git_root),
            timeout=5,
        )
        if result.returncode != 0:
            return None
        return _parse_remote_url(result.stdout.strip())
    except Exception:
        return None


def _parse_remote_url(url: str) -> str | None:
    """Extract org/repo from https://, git@, or ssh:// remote URL formats."""
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]

    if ":" in url and not url.startswith(("https://", "http://", "ssh://")):
        # git@github.com:org/repo
        _, _, path = url.partition(":")
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            return "/".join(parts[-2:])
        return None

    # https:// or ssh:// URL
    parts = url.split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return None
