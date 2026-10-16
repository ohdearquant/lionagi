# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Bounded TZif evidence for identifying the scheduler's host timezone."""

from __future__ import annotations

import io
import os
import stat
import struct
import zoneinfo
from pathlib import Path

__all__ = ()

MAX_TZIF_BYTES = 1 << 20


def read_tzif(path: Path, *, limit: int = MAX_TZIF_BYTES) -> bytes | None:
    """Read a bounded regular file without waiting for a FIFO writer."""
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                return None
            data = stream.read(limit + 1)
        return data if len(data) <= limit else None
    except OSError:
        return None


def tzif_rules(data: bytes | None) -> tuple | None:
    """Compare complete TZif rule tables, without sampling calendar years.

    Modern readers use the 64-bit block. Abbreviation padding, the compatibility
    block and trailing bytes do not identify its rules. Transition/type arrays
    must match; differently structured but equivalent tables can be refused.
    POSIX footers must match exactly; this does not prove equivalence between
    different expressions for the same recurring rule. Loadable files outside
    this reader's boundary use exact bytes as their only equivalence evidence.
    """
    if data is None or len(data) > MAX_TZIF_BYTES:
        return None
    try:
        version, _counts, _cursor, end = _bounds(data, 0, 4)
        if version != b"\0":
            _bounds(data, end, 8)
        zoneinfo.ZoneInfo.from_file(io.BytesIO(data))
    except (ValueError, OverflowError, IndexError, struct.error, UnicodeError, AssertionError):
        return None
    try:
        modern = data[4:5] != b"\0"
        version, end, times, indices, types = _block(data, 0, 4, skip=modern)
        footer = b""
        if version != b"\0":
            second_version, end, times, indices, types = _block(data, end, 8)
            if second_version != version or data[end : end + 1] != b"\n":
                raise ValueError("Unsupported TZif block or footer")
            footer_end = data.index(b"\n", end + 1)
            footer = data[end + 1 : footer_end]
            if b"\0" in footer:
                raise ValueError("Unsupported TZif footer")
            footer.decode("ascii")

        # Retain type indices and even unused records: public loaders can infer
        # DST deltas from their relationships. This comparison deliberately
        # does not try to minimize or rewrite a transition table.
        return "tzif", times, indices, tuple(types), footer
    except (ValueError, OverflowError, IndexError, struct.error, UnicodeError, AssertionError):
        return ("exact-bytes", data)


def _bounds(data: bytes, start: int, width: int) -> tuple:
    if data[start : start + 4] != b"TZif" or len(data) - start < 44:
        raise ValueError("Incomplete TZif header")
    version = data[start + 4 : start + 5]
    counts = struct.unpack_from(">6I", data, start + 20)
    utc_count, std_count, leap_count, count, type_count, char_count = counts
    cursor = start + 44
    end = (
        cursor
        + count * (width + 1)
        + type_count * 6
        + char_count
        + leap_count * (width + 4)
        + std_count
        + utc_count
    )
    if end > len(data):
        raise ValueError("Incomplete TZif data block")
    return version, counts, cursor, end


def _block(data: bytes, start: int, width: int, *, skip: bool = False) -> tuple:
    version, counts, cursor, end = _bounds(data, start, width)
    if version not in (b"\0", b"2", b"3", b"4"):
        raise ValueError("Unsupported TZif version")
    utc_count, std_count, leap_count, count, type_count, char_count = counts
    # Modern loaders only skip over the obsolete compatibility block. Its
    # local-time records and padding must not constrain modern equivalence.
    if skip:
        return version, end, (), (), ()
    if (
        not 1 <= type_count <= 256
        or not char_count
        or utc_count not in (0, type_count)
        or std_count not in (0, type_count)
    ):
        raise ValueError("Invalid TZif counts")
    times = struct.unpack_from(f">{count}{'i' if width == 4 else 'q'}", data, cursor)
    if any(left >= right for left, right in zip(times, times[1:], strict=False)):
        raise ValueError("Unordered TZif transitions")
    cursor += count * width
    indices = data[cursor : cursor + count]
    if any(index >= type_count for index in indices):
        raise ValueError("Invalid TZif transition type")
    cursor += count
    records = list(struct.iter_unpack(">iBB", data[cursor : cursor + type_count * 6]))
    cursor += type_count * 6
    names = data[cursor : cursor + char_count]
    types = []
    for offset, is_dst, name_index in records:
        if is_dst not in (0, 1) or name_index >= char_count or offset == -(1 << 31):
            raise ValueError("Invalid TZif local time type")
        name_end = names.index(b"\0", name_index)
        types.append((offset, is_dst, names[name_index:name_end].decode("ascii")))
    cursor += char_count + leap_count * (width + 4)
    standard = data[cursor : cursor + std_count]
    universal = data[cursor + std_count : end]
    if any(value not in (0, 1) for value in standard + universal):
        raise ValueError("Invalid TZif time indicators")
    if any(value and (not standard or not standard[i]) for i, value in enumerate(universal)):
        raise ValueError("Inconsistent TZif time indicators")
    return version, end, times, indices, types
