# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Path constants and directory helpers — stdlib only, no lionagi deps."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

__all__ = (
    "LIONAGI_HOME",
    "RUNS_ROOT",
    "find_lionagi_dirs",
)

LIONAGI_HOME: Path = Path(os.environ.get("LIONAGI_HOME", Path.home() / ".lionagi")).expanduser()
RUNS_ROOT: Path = LIONAGI_HOME / "runs"


def _find_git_root(cwd: Path) -> Path | None:
    """Return the git repository root for *cwd*, or None if not in a repo."""
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
    except Exception:  # noqa: BLE001
        return None
    return None


def find_lionagi_dirs() -> list[Path]:
    """Find .lionagi/ directories, project-local first then global ~/.lionagi/."""
    dirs: list[Path] = []

    # 1. Git root
    git_root = _find_git_root(Path.cwd())
    if git_root is not None:
        candidate = git_root / ".lionagi"
        if candidate.is_dir():
            dirs.append(candidate)

    # 2. Walk up from cwd
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".lionagi"
        if candidate.is_dir() and candidate not in dirs:
            dirs.append(candidate)

    # 3. Global ~/.lionagi/ (always check)
    home_candidate = Path.home() / ".lionagi"
    if home_candidate.is_dir() and home_candidate not in dirs:
        dirs.append(home_candidate)

    return dirs


# Private alias for callers that imported the old name.
_find_lionagi_dirs = find_lionagi_dirs
