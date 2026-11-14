# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Harvest a leg's artifact scratch directory into the run directory.

The scratch tree's contents are written by the leg, which runs the caller's
own brief under a sandbox. This harvester does not: it runs with the serving
process's privileges, inside the run directory. So the scratch tree is
hostile input, and every traversal decision here is made against an open
descriptor rather than a path:

- The root is opened once with ``O_NOFOLLOW``/``O_DIRECTORY`` and every
  subsequent lookup is relative to a descriptor (``dir_fd=``). Re-resolving a
  path between the check and the open is exactly the window a leg would use
  to swap a checked regular file for a symlink out of the tree.
- Each candidate's opened identity (device + inode, read by ``fstat`` on the
  descriptor actually being copied) is compared against the pre-open
  ``lstat``. A mismatch is recorded and skipped rather than copied.
- Hard links are refused. A hard link is a regular file and passes a naive
  regular-files-only rule, while making the harvester copy an inode the leg
  never produced under the scratch root.
- Caps are counted as bytes are written, never checked up front: a pre-copy
  size read races a file the leg is still growing.

Nothing here interprets file contents; the harvest's product is a copy under
the run directory and a record of what was and was not taken.
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path

__all__ = (
    "MAX_FILES_PER_LEG",
    "MAX_BYTES_PER_LEG",
    "COPY_CHUNK_BYTES",
    "STATE_ABSENT",
    "STATE_EMPTY",
    "STATE_FAILED",
    "SKIP_SYMLINK",
    "SKIP_HARDLINK",
    "SKIP_SWAPPED",
    "SKIP_SPECIAL",
    "SKIP_UNREADABLE",
    "HarvestResult",
    "harvest_leg_artifacts",
)

# An order of magnitude above the observed verdict artifact (single-digit
# markdown files). The cap exists so a runaway leg cannot fill the run
# directory, not to express an expectation about legitimate output.
MAX_FILES_PER_LEG = 1024
MAX_BYTES_PER_LEG = 256 * 1024 * 1024
COPY_CHUNK_BYTES = 256 * 1024

# Harvest states that are not a file count. `dir-absent` and `dir-empty` are
# both legitimate: a leg whose whole answer is its final message writes no
# artifact. `harvest_failed` never is — artifacts were, or may have been,
# written and cannot be served.
STATE_ABSENT = "dir-absent"
STATE_EMPTY = "dir-empty"
STATE_FAILED = "harvest_failed"

# Why a particular entry was not taken. Recorded per entry: a skip that is
# only visible as a smaller file count is indistinguishable from a leg that
# wrote less.
SKIP_SYMLINK = "skipped_symlink"
SKIP_HARDLINK = "skipped_hardlink"
SKIP_SWAPPED = "skipped_swapped"
SKIP_SPECIAL = "skipped_special"
SKIP_UNREADABLE = "skipped_unreadable"


@dataclass(frozen=True)
class HarvestResult:
    """What a harvest established, and what it could not.

    `state` is ``harvested-{n}`` on success and one of the module's state
    constants otherwise. `artifacts` are destination-relative paths, so a
    reader of the record never needs the scratch tree to interpret them.
    """

    state: str
    files: int = 0
    bytes: int = 0
    artifacts: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    detail: str | None = None

    @property
    def failed(self) -> bool:
        return self.state == STATE_FAILED


@dataclass
class _Accumulator:
    files: int = 0
    bytes: int = 0
    artifacts: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


class _CapReachedError(Exception):
    """A cap was hit mid-copy; the partial destination is removed by the caller."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def harvest_leg_artifacts(
    scratch_root: Path | str,
    destination: Path | str,
    *,
    max_files: int = MAX_FILES_PER_LEG,
    max_bytes: int = MAX_BYTES_PER_LEG,
) -> HarvestResult:
    """Copy *scratch_root*'s regular files into *destination*, defensively.

    Returns what was established. Raises nothing for hostile input: a tree
    the harvester cannot read produces ``harvest_failed`` with a reason, which
    is a fact the leg's record must carry, never an empty artifact list that
    reads as "the leg wrote nothing".
    """
    root = Path(scratch_root)
    dest = Path(destination)

    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    except FileNotFoundError:
        return HarvestResult(state=STATE_ABSENT)
    except NotADirectoryError:
        # A leg that replaced its own scratch directory with a file: the
        # channel is gone, and saying so is not the same as saying it was
        # empty.
        return HarvestResult(state=STATE_FAILED, detail="scratch path is not a directory")
    except OSError as exc:
        return HarvestResult(state=STATE_FAILED, detail=f"scratch root unreadable: {exc}")

    acc = _Accumulator()
    try:
        try:
            _harvest_dir(root_fd, dest, "", acc, max_files=max_files, max_bytes=max_bytes)
        except _CapReachedError as exc:
            return HarvestResult(
                state=STATE_FAILED,
                files=acc.files,
                bytes=acc.bytes,
                artifacts=tuple(acc.artifacts),
                skipped=tuple(acc.skipped),
                detail=exc.detail,
            )
        except OSError as exc:
            return HarvestResult(
                state=STATE_FAILED,
                files=acc.files,
                bytes=acc.bytes,
                artifacts=tuple(acc.artifacts),
                skipped=tuple(acc.skipped),
                detail=f"traversal failed: {exc}",
            )
    finally:
        os.close(root_fd)

    if acc.files == 0 and not acc.skipped:
        return HarvestResult(state=STATE_EMPTY)
    return HarvestResult(
        state=f"harvested-{acc.files}",
        files=acc.files,
        bytes=acc.bytes,
        artifacts=tuple(acc.artifacts),
        skipped=tuple(acc.skipped),
    )


# O_NOFOLLOW exists on every platform this package spawns CLI legs on; the
# fallback keeps an import from failing where it does not, and the symlink
# check below is what refuses the entry in that case anyway.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _harvest_dir(
    dir_fd: int,
    dest: Path,
    rel: str,
    acc: _Accumulator,
    *,
    max_files: int,
    max_bytes: int,
) -> None:
    """Copy one directory level, recursing through descriptors only."""
    with os.scandir(dir_fd) as entries:
        # Sorted so a record's artifact list is stable across harvests of the
        # same tree; scandir order is the filesystem's, which is not.
        for entry in sorted(entries, key=lambda e: e.name):
            name = entry.name
            rel_path = f"{rel}/{name}" if rel else name

            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                acc.skipped.append((rel_path, SKIP_UNREADABLE))
                continue

            if stat_module.S_ISLNK(st.st_mode):
                acc.skipped.append((rel_path, SKIP_SYMLINK))
                continue

            if stat_module.S_ISDIR(st.st_mode):
                try:
                    child_fd = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=dir_fd
                    )
                except OSError:
                    acc.skipped.append((rel_path, SKIP_UNREADABLE))
                    continue
                try:
                    _harvest_dir(
                        child_fd,
                        dest,
                        rel_path,
                        acc,
                        max_files=max_files,
                        max_bytes=max_bytes,
                    )
                finally:
                    os.close(child_fd)
                continue

            if not stat_module.S_ISREG(st.st_mode):
                acc.skipped.append((rel_path, SKIP_SPECIAL))
                continue

            if acc.files >= max_files:
                raise _CapReachedError(
                    f"file cap reached at {max_files} files ({acc.bytes} bytes copied)"
                )

            _copy_file(
                dir_fd,
                name,
                st,
                dest / rel_path,
                rel_path,
                acc,
                max_bytes=max_bytes,
            )


def _copy_file(
    dir_fd: int,
    name: str,
    pre_open_st: os.stat_result,
    target: Path,
    rel_path: str,
    acc: _Accumulator,
    *,
    max_bytes: int,
) -> None:
    """Copy one regular file, verifying that what was opened is what was checked."""
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        acc.skipped.append((rel_path, SKIP_UNREADABLE))
        return

    try:
        opened = os.fstat(fd)
        # The identity that matters is the one belonging to the descriptor
        # bytes are about to be read from, not the one a second path lookup
        # would report.
        if (opened.st_dev, opened.st_ino) != (pre_open_st.st_dev, pre_open_st.st_ino):
            acc.skipped.append((rel_path, SKIP_SWAPPED))
            return
        if not stat_module.S_ISREG(opened.st_mode):
            acc.skipped.append((rel_path, SKIP_SPECIAL))
            return
        if opened.st_nlink != 1:
            # A hard link is a regular file: it passes a regular-files-only
            # rule while making the harvester serve an inode that was never
            # produced under this root. Checked on the DESCRIPTOR and only
            # here — an earlier check against the path's stat would be blind
            # to a link created between that stat and this open, and having
            # two sites for one refusal leaves neither of them individually
            # testable.
            acc.skipped.append((rel_path, SKIP_HARDLINK))
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with open(fd, "rb", closefd=False) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    if acc.bytes + written + len(chunk) > max_bytes:
                        allowed = max_bytes - acc.bytes - written
                        if allowed > 0:
                            out.write(chunk[:allowed])
                            written += allowed
                        raise _CapReachedError(
                            f"byte cap reached at {max_bytes} bytes "
                            f"({acc.files} files copied, aborted inside {rel_path})"
                        )
                    out.write(chunk)
                    written += len(chunk)
        except _CapReachedError:
            acc.bytes += written
            # The partial copy is removed rather than served: a truncated
            # artifact that reads as complete is the silent truncation this
            # cap exists to avoid.
            target.unlink(missing_ok=True)
            raise
        except OSError as exc:
            acc.skipped.append((rel_path, SKIP_UNREADABLE))
            target.unlink(missing_ok=True)
            del exc
            return

        acc.files += 1
        acc.bytes += written
        acc.artifacts.append(rel_path)
    finally:
        os.close(fd)
