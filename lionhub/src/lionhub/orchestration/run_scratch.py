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


def _grant_owner_access(root: str) -> None:
    """Give the owner read, write and search on every directory under *root*, links not followed.

    A tool can leave a directory it made read-only (a module cache does), and removing its
    entries needs write on it. Only real directories are changed: a symbolic link is skipped, and
    the change itself does not follow one. Where the platform cannot change a mode without
    following a link, nothing is changed and the first removal's errors stand.
    """
    if os.chmod not in os.supports_follow_symlinks:
        return
    try:
        os.chmod(root, _OWNER_ONLY, follow_symlinks=False)
    except OSError:
        return
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        for name in dirnames:
            path = os.path.join(dirpath, name)
            try:
                if not stat.S_ISDIR(os.lstat(path).st_mode):
                    continue
                os.chmod(path, _OWNER_ONLY, follow_symlinks=False)
            except OSError:
                continue


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
    what it points to is left alone. A directory already gone is not an error.
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
