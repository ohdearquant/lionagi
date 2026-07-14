# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Path constants and directory helpers — stdlib only, no lionagi deps."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

__all__ = (
    "LIONAGI_HOME",
    "RUNS_ROOT",
    "clear_lionagi_dirs_cache",
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


@lru_cache(maxsize=32)
def _find_lionagi_dirs_cached(cwd: Path, home: Path) -> tuple[Path, ...]:
    """Find .lionagi/ directories for a stable cwd/home pair."""
    dirs: list[Path] = []

    # 1. Git root
    git_root = _find_git_root(cwd)
    if git_root is not None:
        candidate = git_root / ".lionagi"
        if candidate.is_dir():
            dirs.append(candidate)

    # 2. Walk up from cwd
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".lionagi"
        if candidate.is_dir() and candidate not in dirs:
            dirs.append(candidate)

    # 3. Global ~/.lionagi/ (always check)
    home_candidate = home / ".lionagi"
    if home_candidate.is_dir() and home_candidate not in dirs:
        dirs.append(home_candidate)

    return tuple(dirs)


def find_lionagi_dirs() -> list[Path]:
    """Find .lionagi/ directories, project-local first then global ~/.lionagi/."""
    return list(_find_lionagi_dirs_cached(Path.cwd(), Path.home()))


def clear_lionagi_dirs_cache() -> None:
    """Clear cached directory discovery after filesystem topology changes."""
    _find_lionagi_dirs_cached.cache_clear()


# Keep the conventional lru_cache hook available on the public finder while
# caching by cwd/home internally so in-process directory changes cannot reuse
# stale discovery results.
find_lionagi_dirs.cache_clear = clear_lionagi_dirs_cache


# Private alias for callers that imported the old name.
_find_lionagi_dirs = find_lionagi_dirs
