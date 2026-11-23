"""A run's writable scratch directory: made in the run's state directory, removed at run end
unless the profile keeps it.

A profile that declares ``scratch: true`` or ``scratch: keep`` gets one directory per ``li agent``
run, ``<run state dir>/scratch``, made with mode 0700. It is the one write root the run adds to
the child's sandbox, and the child's ``TMPDIR``, ``TMP`` and ``TEMP``. See ADR-0041 Amendment 6.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ("SCRATCH_DIRNAME", "SCRATCH_MODES", "RunScratch", "remove_scratch")

#: The name of the directory inside the run's state directory.
SCRATCH_DIRNAME = "scratch"

#: What a profile's ``scratch`` value resolves to: removed at run end, or kept.
SCRATCH_MODES: tuple[str, ...] = ("remove", "keep")

_OWNER_ONLY = stat.S_IRWXU

# A directory opened with these flags is never reached through a symbolic link at the last
# component, so a mode changed through the descriptor is that directory's, not a link target's.
# Linux has no lchmod, so a mode change by path cannot refuse a link there; this works on Linux and
# macOS alike.
_CAN_OPEN_UP = all(hasattr(os, name) for name in ("fchmod", "O_DIRECTORY", "O_NOFOLLOW"))
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_up(path: str) -> None:
    """Give the owner read, write and search on the directory at *path*, if it can be opened."""
    try:
        fd = os.open(path, _DIRECTORY_FLAGS)
    except OSError:
        return
    try:
        os.fchmod(fd, _OWNER_ONLY)
    except OSError:
        pass
    finally:
        os.close(fd)


def _grant_owner_access(root: str) -> None:
    """Give the owner read, write and search on every directory under *root*, links not followed.

    A tool can leave a directory it made read-only (a module cache does), and removing its
    entries needs write on it. Each directory is opened with ``O_DIRECTORY`` and ``O_NOFOLLOW``
    and its mode changed through that descriptor, so a symbolic link in its place is not opened
    and nothing it names is changed. A directory its owner cannot open for reading is left as it
    is, with everything below it, and the second removal's errors name it. Where the platform
    lacks ``fchmod``, ``O_DIRECTORY`` or ``O_NOFOLLOW``, nothing is changed.
    """
    if not _CAN_OPEN_UP:
        return
    _open_up(root)
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        for name in dirnames:
            _open_up(os.path.join(dirpath, name))


def _rmtree(path: str) -> list[str]:
    errors: list[str] = []

    def note(func: Any, failed: str, exc: BaseException) -> None:
        errors.append(f"{getattr(func, '__name__', func)} {failed}: {exc}")

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=note)
    else:
        shutil.rmtree(path, onerror=lambda func, failed, info: note(func, failed, info[1]))
    return errors


def remove_scratch(path: str | os.PathLike[str], run_dir: str | os.PathLike[str]) -> str | None:
    """Remove the scratch directory at *path*; return why it was refused or not removed, else None.

    Only that directory goes, by the path the run recorded. It is refused, and left exactly as it
    is, when it is a symbolic link, is not a directory, is not named ``scratch``, or does not sit
    directly in *run_dir*. Nothing inside it is followed: a link inside is removed as a link and
    what it points to is left alone. A directory already gone is not an error. When the first
    removal leaves something, every directory under *path* is opened up to its owner and removal
    runs once more; what is inside a directory its owner cannot open is left and named.
    """
    path = os.fspath(path)
    run_dir = os.fspath(run_dir)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"scratch not removed: cannot read {path}: {exc}"
    if stat.S_ISLNK(mode):
        return (
            f"scratch not removed: {path} is a symbolic link; it was left in place and nothing "
            "it points to was removed"
        )
    if not stat.S_ISDIR(mode):
        return f"scratch not removed: {path} is not a directory; it was left in place"
    if os.path.basename(path) != SCRATCH_DIRNAME:
        return f"scratch not removed: {path} is not named {SCRATCH_DIRNAME!r}"
    if os.path.realpath(os.path.dirname(path)) != os.path.realpath(run_dir):
        return f"scratch not removed: {path} is not directly in the run directory {run_dir}"
    errors = _rmtree(path)
    if errors and os.path.lexists(path):
        _grant_owner_access(path)
        errors = _rmtree(path)
    if errors and os.path.lexists(path):
        shown = "; ".join(errors[:5])
        more = f" (and {len(errors) - 5} more)" if len(errors) > 5 else ""
        return f"scratch partly removed: {shown}{more}"
    return None


@dataclass(frozen=True)
class RunScratch:
    """One run's scratch directory and whether the run keeps it."""

    path: Path
    run_dir: Path
    keep: bool

    @classmethod
    def create(cls, run_dir: str | os.PathLike[str], *, keep: bool) -> RunScratch:
        """Make ``<run_dir>/scratch`` with mode 0700. A name already there is refused."""
        run_dir = Path(run_dir)
        path = run_dir / SCRATCH_DIRNAME
        os.mkdir(path, _OWNER_ONLY)
        # mkdir's mode passes through the umask; set it outright.
        os.chmod(path, _OWNER_ONLY)
        return cls(path=path, run_dir=run_dir, keep=keep)

    def record(self) -> dict[str, Any]:
        """The run.json ``scratch`` entry while the run is live."""
        return {"path": str(self.path), "keep": self.keep}

    def finish(self) -> dict[str, Any]:
        """Remove the directory unless kept; return the run.json ``scratch`` entry at run end.

        ``removed`` says whether the directory is gone, and ``removal_error`` why it is not, or
        is only partly, removed. A kept directory reads ``removed: false`` with no error.
        """
        entry = self.record()
        if self.keep:
            entry.update(removed=False, removal_error=None)
            return entry
        error = remove_scratch(self.path, self.run_dir)
        entry.update(removed=not os.path.lexists(self.path), removal_error=error)
        return entry
