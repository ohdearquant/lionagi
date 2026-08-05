# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Periodic all-thread stack dumps, armed by an environment variable.

A daemon that stops answering on one route is asking a question only a stack
can answer, and the usual way to get one is to attach a sampling profiler to
the live process. On macOS that needs root, which a rule against improvised
elevation puts out of reach at exactly the moment the process is wedged.

``faulthandler.dump_traceback_later`` answers the same question from inside:
every thread's stack, written to a file on a timer, with no attach and no
privileges. It is stdlib, it is already imported by nothing else here, and it
costs a wedged process nothing it was doing anyway.

It is off unless ``LIONAGI_STUDIO_TRACEBACK_DUMP`` names a path. Absent that
variable this module arms no timer, opens no file, and changes no behaviour.

Capture windows are operator-bounded. ``repeat=True`` appends every interval
for as long as the process runs, so an armed daemon left running writes an
unbounded file. Nothing here rotates or truncates it: the intended use is a
short reproduction window, and a size cap that silently stopped writing would
lose the frames the window exists to catch.
"""

from __future__ import annotations

import faulthandler
import os
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
    """
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
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
