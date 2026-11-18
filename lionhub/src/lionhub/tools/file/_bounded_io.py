# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_NON_REGULAR_KINDS = {
    "b": "block device",
    "c": "character device",
    "d": "directory",
    "l": "symbolic link",
    "p": "FIFO",
    "s": "socket",
}


class _NotRegularFileError(OSError):
    def __init__(self, path: Path, mode: int):
        kind = _NON_REGULAR_KINDS.get(stat.filemode(mode)[0], "special file")
        super().__init__(f"Path is not a file: {path} ({kind}). Expected a regular file.")


class _FileSizeLimitError(OSError):
    def __init__(self, path: Path, limit: int):
        super().__init__(f"File {path} exceeds {limit} bytes.")


@contextmanager
def _open_regular_file(path: Path, flags: int) -> Iterator[tuple[int, os.stat_result]]:
    # A replacement FIFO must not block the open before the descriptor can be checked.
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o666)
    except OSError as exc:
        # Some special files, such as sockets, fail before there is a descriptor to inspect.
        try:
            mode = path.lstat().st_mode
        except OSError:
            raise exc from None
        if not stat.S_ISREG(mode):
            raise _NotRegularFileError(path, mode) from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _NotRegularFileError(path, info.st_mode)
        yield fd, info
    finally:
        os.close(fd)


def _read_regular_bytes(path: Path, limit: int) -> bytes:
    with _open_regular_file(path, os.O_RDONLY) as (fd, info):
        if info.st_size > limit:
            raise _FileSizeLimitError(path, limit)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            contents = stream.read(limit + 1)
    if len(contents) > limit:
        raise _FileSizeLimitError(path, limit)
    return contents
