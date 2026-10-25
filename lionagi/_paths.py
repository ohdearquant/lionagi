# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Path constants and directory helpers — stdlib only, no lionagi deps."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

__all__ = (
    "AmbiguousProfileNameError",
    "LIONAGI_HOME",
    "RUNS_ROOT",
    "ensure_lionagi_dir",
    "find_lionagi_dirs",
    "is_account_home",
    "is_account_lionagi_dir",
)

LIONAGI_HOME: Path = Path(os.environ.get("LIONAGI_HOME", Path.home() / ".lionagi")).expanduser()
RUNS_ROOT: Path = LIONAGI_HOME / "runs"


def _account_home() -> Path | None:
    """The home directory the password database names for this process's user.

    Read independently of ``$HOME`` on purpose: the user-level `.lionagi` is
    found through ``$HOME``, and the project walks below must recognise the
    account's own directory even when ``$HOME`` points somewhere else. None
    where there is no password database (Windows), no entry for the uid (a
    container, say), or an entry whose home is empty or relative; the walks then
    exclude nothing, as before this rule existed. Never ``$HOME`` in its place:
    that is the value being isolated.
    """
    try:
        import pwd
    except ImportError:
        return None
    try:
        pw_dir = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return None
    # An empty home reads as `Path(".")`, the working directory, which would make the
    # project's own `.lionagi` look like the account's and drop it from the walk.
    if not isinstance(pw_dir, str) or not os.path.isabs(pw_dir):
        return None
    return Path(pw_dir)


def _same_directory(path: Path, other: Path) -> bool:
    # Identity, not spelling: a symlinked parent, a symlinked `.lionagi` or a
    # case variant on a case-insensitive volume all name the same directory.
    try:
        return os.path.samefile(path, other)
    except OSError:
        return False


def is_account_home(directory: Path) -> bool:
    """Whether *directory* is the account's own home directory, by any spelling."""
    home = _account_home()
    return home is not None and _same_directory(directory, home)


def is_account_lionagi_dir(directory: Path) -> bool:
    """Whether *directory* is the account home's own `.lionagi`, by any spelling.

    When the password database identifies the account home, project discovery
    excludes this directory regardless of ``$HOME``. The user-level candidate
    itself follows ``Path.home()``. If the account home cannot be identified,
    no project-directory exclusion applies.
    """
    home = _account_home()
    return home is not None and _same_directory(directory, home / ".lionagi")


def _find_git_root(cwd: Path) -> Path | None:
    """Git repo root for *cwd* via `git rev-parse --show-toplevel`, or None."""
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


def find_lionagi_dirs(cwd: Path | None = None) -> list[Path]:
    """Find `.lionagi/` dirs: git root, then cwd and its parents, then `~/.lionagi/`.

    *cwd* defaults to the process working directory; pass another directory to
    see what a process started there would find.

    When the password database identifies the account home, its `.lionagi` is
    excluded from project candidates at the git root and on the ancestor walk.
    The separate user-level candidate follows ``Path.home() / ".lionagi"``.
    If the account home cannot be identified, no project-directory exclusion
    applies and the walk retains its prior behavior.

    Uncached: every call re-resolves the git root and re-checks the paths, so
    the result always reflects the current cwd and git context.
    """
    cwd = Path.cwd() if cwd is None else cwd
    home = Path.home()
    dirs: list[Path] = []

    git_root = _find_git_root(cwd)
    if git_root is not None:
        candidate = git_root / ".lionagi"
        if candidate.is_dir() and not is_account_lionagi_dir(candidate):
            dirs.append(candidate)

    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".lionagi"
        if candidate.is_dir() and candidate not in dirs and not is_account_lionagi_dir(candidate):
            dirs.append(candidate)

    home_candidate = home / ".lionagi"
    if home_candidate.is_dir() and home_candidate not in dirs:
        dirs.append(home_candidate)

    return dirs


def ensure_lionagi_dir(path: Path) -> Path:
    """Create *path* (with parents) if missing; the creation boundary for `.lionagi/` topology."""
    path.mkdir(parents=True, exist_ok=True)
    return path


# Agent profile files: `<agents_dir>/<name>/<name>.md` or `<agents_dir>/<name>.md`.


def _unreadable_symlink_target(path: Path) -> str | None:
    """Return a broken/non-file symlink's declared target, if applicable."""
    if not path.is_symlink() or path.is_file():
        return None
    try:
        return str(path.readlink())
    except OSError:
        return "<unreadable>"


class AmbiguousProfileNameError(ValueError):
    """One agents dir declares a name under both '-' and '_' spellings."""


def _name_spellings(name: str) -> tuple[str, ...]:
    """NAME plus its separator spellings, requested spelling first.

    '-' and '_' are interchangeable in a profile name, so a role written
    ``postmortem-lead`` everywhere else still finds ``postmortem_lead.md``.
    """
    return tuple(dict.fromkeys((name, name.replace("-", "_"), name.replace("_", "-"))))


def _resolve_profile_path(
    agents_dir: Path,
    name: str,
    *,
    unreadable_symlinks: list[tuple[Path, str]] | None = None,
) -> Path | None:
    """Return profile path for NAME, recording unreadable candidate symlinks.

    The spelling actually asked for wins outright when it exists. Only a
    request that does *not* name an existing file falls back to the other
    separator spelling, so a directory deliberately holding two profiles that
    differ only in separator keeps resolving both, exactly as it did before
    the spellings became interchangeable.

    Failing that, two spellings matching one request in a single directory are
    an error rather than a ranking: whichever one lost would be invisible to
    the caller. Two spellings in *different* roots are ordinary shadowing,
    decided by root order, and are resolved by the caller walking the roots.
    """
    resolved: dict[str, Path] = {}
    for spelling in _name_spellings(name):
        for candidate in (agents_dir / spelling / f"{spelling}.md", agents_dir / f"{spelling}.md"):
            if candidate.is_file():
                resolved.setdefault(spelling, candidate)
                continue
            target = _unreadable_symlink_target(candidate)
            if target is not None and unreadable_symlinks is not None:
                unreadable_symlinks.append((candidate, target))

    if name in resolved:
        return resolved[name]
    if len(resolved) > 1:
        matched = ", ".join(str(p) for p in resolved.values())
        raise AmbiguousProfileNameError(
            f"Agent profile name '{name}' is ambiguous in {agents_dir}: "
            f"'-' and '_' are interchangeable, and both spellings exist ({matched}). "
            "Rename or remove one of them."
        )
    return next(iter(resolved.values()), None)


# Private alias for callers that imported the old name.
_find_lionagi_dirs = find_lionagi_dirs
