# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Periodic all-thread stack dumps, armed by an environment variable.

Uses ``faulthandler.dump_traceback_later`` to write every thread's stack to
a file on a timer, with no process attach and no elevated privileges needed
(a sampling profiler attach needs root on macOS). Off unless
``LIONAGI_STUDIO_TRACEBACK_DUMP`` names a path -- absent that, this module
arms no timer and changes no behaviour.

``repeat=True`` appends every interval for as long as the process runs, and
nothing here rotates or truncates the file -- an armed daemon left running
writes it unbounded. Intended for a short reproduction window, not
always-on operation.
"""

from __future__ import annotations

import faulthandler
import os
import stat
import sys
from pathlib import Path
from typing import TextIO

_ENV_PATH = "LIONAGI_STUDIO_TRACEBACK_DUMP"
_ENV_INTERVAL = "LIONAGI_STUDIO_TRACEBACK_DUMP_INTERVAL"
_DEFAULT_INTERVAL_SECONDS = 20.0

_handle: TextIO | None = None


def _refuse(message: str) -> None:
    """Say why the dump did not arm.

    On stderr rather than through the logger: this runs before the daemon's
    logging is necessarily useful to whoever set the variable, and an operator
    who armed a capture and got nothing must not have to guess between "the
    hook is not wired" and "the path was wrong".
    """
    print(f"{_ENV_PATH}: not armed: {message}", file=sys.stderr, flush=True)


def _inside_a_git_tree(path: Path) -> Path | None:
    """The nearest ancestor of *path* that is a git working tree, if any.

    Dumps carry stack frames, local file paths and process detail, so a path
    that lands inside a checkout is one ``git add -A`` away from being
    committed. Refusing is cheaper than relying on an ignore rule that the
    operator would have to have written first.

    The entry has to look like a git directory, not merely exist. An aborted
    ``git init`` leaves a ``.git`` containing nothing but empty ``hooks`` and
    ``info``, and shared directories collect them; git itself refuses such a
    directory, so accepting one refuses every path beneath it on behalf of a
    checkout that is not there. The three entries checked here are the ones
    git's own discovery requires.

    A ``.git`` *file* stays a match without reading it. That is how a linked
    worktree and a submodule each record where their real git directory
    lives, and a path under either is inside a working tree. Reading the file
    to confirm would trade a cheap answer for one that can fail, and the
    failure would have to resolve as "not a checkout" -- the wrong direction
    for a guard whose expensive mistake is missing a tree, not inventing one.

    An unreadable candidate is treated as inside a checkout. The function uses
    ``lstat`` to classify ``.git`` and ``HEAD`` without
    following links; checking ``objects`` and ``refs`` can still fail when the
    directory is unreadable, and that error reaches the handler below.
    Unreadable means undecidable, so the function refuses the path rather than
    overlook a tree or raise out of the daemon's startup path.
    """
    for candidate in (path, *path.parents):
        entry = candidate / ".git"
        try:
            # One ``lstat`` per ancestor, read for its type, rather than
            # ``is_file()`` then ``is_dir()``. The common case by far is a path
            # with no checkout above it, and that case walks to the filesystem
            # root: paying two probes per level there doubled the syscalls this
            # function costs against the single ``exists()`` it replaced.
            mode = entry.lstat().st_mode
            if not stat.S_ISDIR(mode):
                return candidate
            # Explicit ``stat`` calls preserve filesystem errors for the
            # handler below. ``Path.is_file`` and ``Path.is_dir`` can turn an
            # unreadable entry into False, which would overlook a tree this
            # guard cannot inspect.
            #
            # Git accepts HEAD as a file or symlink. A directory named HEAD is
            # not a checkout; a missing entry is handled below as no checkout.
            head_mode = (entry / "HEAD").lstat().st_mode
            if (
                (stat.S_ISREG(head_mode) or stat.S_ISLNK(head_mode))
                and stat.S_ISDIR((entry / "objects").stat().st_mode)
                and stat.S_ISDIR((entry / "refs").stat().st_mode)
            ):
                return candidate
        except (FileNotFoundError, NotADirectoryError):
            # The two ENOENT-shaped answers, and the only ones that mean "no
            # checkout here". Kept ahead of the OSError arm below, which they
            # would otherwise match and turn every ordinary ancestor into a
            # refusal.
            continue
        except OSError:
            return candidate
    return None


def arm_traceback_dump() -> bool:
    """Arm the periodic dump if the environment asks for it. Returns whether it armed."""
    global _handle

    raw = os.environ.get(_ENV_PATH)
    if not raw:
        return False

    path = Path(raw).expanduser()
    tree = _inside_a_git_tree(path.parent if path.parent != path else path)
    if tree is not None:
        _refuse(f"{path} is inside the git tree at {tree}; choose a path outside any checkout")
        return False

    interval = _DEFAULT_INTERVAL_SECONDS
    raw_interval = os.environ.get(_ENV_INTERVAL)
    if raw_interval:
        try:
            interval = float(raw_interval)
        except ValueError:
            _refuse(f"{_ENV_INTERVAL}={raw_interval!r} is not a number")
            return False
        if interval <= 0:
            _refuse(f"{_ENV_INTERVAL}={raw_interval!r} must be positive")
            return False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a", encoding="utf-8")
    except OSError as exc:
        # %r, not %s: an argless OSError's str() is empty, and "not armed:"
        # followed by nothing is the same silence this branch exists to break.
        _refuse(f"{path} could not be opened for append: {exc!r}")
        return False

    faulthandler.dump_traceback_later(interval, repeat=True, file=handle)
    _handle = handle
    print(
        f"{_ENV_PATH}: armed, every {interval}s, appending to {path}",
        file=sys.stderr,
        flush=True,
    )
    return True


def disarm_traceback_dump() -> None:
    """Cancel the timer and close the file. Safe to call when never armed."""
    global _handle

    faulthandler.cancel_dump_traceback_later()
    if _handle is not None:
        try:
            _handle.close()
        finally:
            _handle = None
