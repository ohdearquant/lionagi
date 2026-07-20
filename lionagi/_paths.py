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
    "ensure_lionagi_dir",
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
def _find_git_root_cached(cwd: Path) -> Path | None:
    """Memoized `_find_git_root`: the git-root fact is stable for a given
    cwd, and resolving it shells out to `git` under a timeout, which is the
    only part of directory discovery expensive enough to be worth caching.
    """
    return _find_git_root(cwd)


def find_lionagi_dirs() -> list[Path]:
    """Find .lionagi/ directories, project-local first then global ~/.lionagi/.

    Only the git-root lookup is cached (per cwd, for the life of the
    process) -- the `.lionagi/` existence checks themselves are cheap
    `Path.is_dir()` stats and are re-evaluated on every call. A caller
    always sees current `.lionagi/` topology; nothing needs to invalidate
    anything after creating or removing a `.lionagi/` directory.
    """
    cwd = Path.cwd()
    home = Path.home()
    dirs: list[Path] = []

    # 1. Git root (cached lookup, live existence check)
    git_root = _find_git_root_cached(cwd)
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

    return dirs


def clear_lionagi_dirs_cache() -> None:
    """Clear the cached git-root resolution.

    `find_lionagi_dirs()` no longer caches `.lionagi/` existence, so this
    only matters if the git root for a cwd itself changes in-process (e.g.
    a test re-inits a repo at the same path); it is not required after
    creating or removing a `.lionagi/` directory.
    """
    _find_git_root_cached.cache_clear()


def ensure_lionagi_dir(path: Path) -> Path:
    """Create *path* (with parents) if missing.

    Production call sites that may bring a `.lionagi` directory --
    project-local or the global `~/.lionagi` -- into existence for the
    first time should still create it through this helper rather than a
    bare `Path.mkdir`: it is the explicit creation boundary for that
    topology change. `find_lionagi_dirs()` re-checks `.lionagi/` existence
    on every call, so callers that bypass this helper (e.g. a generic path
    utility that happens to create a `.lionagi/*` path) no longer produce
    stale discovery results either.
    """
    path.mkdir(parents=True, exist_ok=True)
    clear_lionagi_dirs_cache()
    return path


# Keep the conventional lru_cache hook available on the public finder, even
# though the finder itself is no longer memoized -- it now only clears the
# git-root cache the finder depends on.
find_lionagi_dirs.cache_clear = clear_lionagi_dirs_cache


# Private alias for callers that imported the old name.
_find_lionagi_dirs = find_lionagi_dirs
