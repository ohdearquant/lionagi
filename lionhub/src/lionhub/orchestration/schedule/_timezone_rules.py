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

# Each transition is read either side of its instant: rules that change at
# different moments differ in the seconds around them.
_EDGE_SECONDS = (-1, 1)
# Read a year past the last stored transition, where a file with a POSIX footer
# leaves its recurring rules to that footer instead of storing transitions.
_TAIL_SECONDS = 366 * 24 * 3600
# Past the end of its table a file reads its footer, whose transitions are not
# stored and so are not among the instants above. Where one file reads its
# footer and the other still reads a table, a year that table leaves out (a
# summer it stores no change for) falls between two read instants, so that
# stretch is also read once a week between 1900 and 2100. Rules lasting under a
# week there, or outside those years, are not looked for.
_WEEK_SECONDS = 7 * 24 * 3600
_FIRST_WEEKLY_READ = -2208988800  # 1900-01-01 UTC
_LAST_WEEKLY_READ = 4102444800  # 2100-01-01 UTC


def rules_equal(left: tuple | None, right: tuple | None) -> bool:
    """Whether two values from :func:`tzif_rules` carry the same rules.

    Files whose stored tables differ still carry the same rules when loading
    each of them gives the same UTC offset, daylight offset and name at every
    instant either of them stores a change, and weekly while one reads its
    footer and the other a table, and when both carry the same POSIX footer
    text. A build of a zone may store a transition another build leaves to its
    footer, so the stored tables are deliberately not the comparison: only the
    rules they decode to are. The daylight offset is the one the loaded rules
    report for an instant, so two builds of one release that disagree only
    there, a rearguard and a vanguard build of the same tzdata, are not equal.
    The weekly reads that catch a rule one table leaves out cover 1900 to 2100,
    and a rule lasting under a week there is not looked for. An instant whose
    behaviour cannot be read is not evidence of agreement, so the two files are
    not equal through it. A file this reader cannot interpret keeps its exact
    bytes as its only evidence.
    """
    if left is None or right is None or left[0] != right[0]:
        return False
    if left[0] == "exact-bytes":
        return left[1] == right[1]
    if left[2] != right[2]:
        return False
    compared = 0
    points = _change_points(left[3]) | _change_points(right[3])
    for when in points | frozenset(_footer_points(left[3], right[3])):
        try:
            mine = _behaviour(left[1], when)
            theirs = _behaviour(right[1], when)
        except (OverflowError, ValueError):
            # The instant itself is representable, but applying the file's own
            # offset to it is not: a transition near the end of the
            # representable range overflows, and an offset outside a day is
            # refused by ``datetime``. That behaviour cannot be read, so it is
            # not evidence of agreement.
            return False
        if mine is None or theirs is None:
            # An instant this platform cannot represent is not evidence about
            # either file, so it is passed over rather than counted as a match.
            continue
        if mine != theirs:
            return False
        compared += 1
    # Two files with no readable instant in common have no evidence of agreeing.
    return compared > 0


def _change_points(times: tuple) -> frozenset:
    """The instants at which one file's own rules are read.

    Either side of every stored transition — which includes the rule in force
    before the first one — plus a year past the last one, so a footer's
    recurring rules are read too. A file storing no transition is read at a
    single instant, which is all a fixed local time type can vary at.
    """
    if not times:
        return frozenset({0})
    points = {when + delta for when in times for delta in _EDGE_SECONDS}
    return frozenset(points | {max(times) + _TAIL_SECONDS})


def _footer_points(left: tuple, right: tuple) -> range:
    """Weekly instants where one file reads its footer and the other a table."""
    if bool(left) != bool(right):
        # A file storing no transition reads its footer throughout, including
        # the year before the other file's table starts.
        stored = left or right
        start, end = min(stored) - _TAIL_SECONDS, max(stored)
    elif left:
        start, end = sorted((max(left), max(right)))
    else:
        return range(0)
    return range(max(start, _FIRST_WEEKLY_READ), min(end, _LAST_WEEKLY_READ) + 1, _WEEK_SECONDS)


def _behaviour(zone: zoneinfo.ZoneInfo, when: int) -> tuple | None:
    """The UTC offset, daylight offset and name *zone* reads at *when*."""
    from lionagi.ln import coerce_created_at

    try:
        moment = coerce_created_at(when)
    except (OSError, OverflowError, ValueError):
        return None
    local = moment.astimezone(zone)
    return (local.utcoffset(), local.dst(), local.tzname())


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


def _daylight_offset_reads_past_the_table(
    data: bytes, cursor: int, width: int, counts: tuple
) -> bool:
    """Whether the public loader would read past this table's transitions.

    The loader assigns each daylight type the difference to the neighbouring
    standard offset, and where the stored transitions leave one unplaced it
    looks at the transition after the current one. On the last transition that
    is one past the table, which the process reads as memory rather than data.
    This walks the same assignment over the block the loader reads and reports
    when it would step past the end.
    """
    count, type_count = counts[3], counts[4]
    indices = data[cursor + count * width : cursor + count * (width + 1)]
    records = data[cursor + count * (width + 1) : cursor + count * (width + 1) + type_count * 6]
    offsets = []
    daylight = []
    for offset, is_dst, _name in struct.iter_unpack(">lbb", records):
        offsets.append(offset)
        # A type is daylight to the loader whenever its flag is not zero, so
        # this mirrors the flag rather than assuming the two declared values.
        daylight.append(bool(is_dst))
    placed = [False] * len(daylight)
    remaining = sum(daylight)
    found = 0
    for position in range(1, len(indices)):
        if found == remaining:
            return False
        index = indices[position]
        if not daylight[index] or placed[index]:
            continue
        difference = 0
        before = indices[position - 1]
        if not daylight[before]:
            difference = offsets[index] - offsets[before]
        if not difference and index < type_count - 1:
            if position + 1 >= len(indices):
                return True
            after = indices[position + 1]
            if daylight[after]:
                continue
            difference = offsets[index] - offsets[after]
        if difference:
            found += 1
            placed[index] = True
    return False


def tzif_rules(data: bytes | None) -> tuple | None:
    """One file's rules as evidence, without the layout that encodes them.

    Modern readers use the 64-bit block. Abbreviation padding, the compatibility
    block and trailing bytes do not identify its rules. Compare two of these
    with :func:`rules_equal`: files whose stored tables differ but whose UTC
    offsets, daylight offsets, names and POSIX footer agree describe the same
    rules.
    This does not prove equivalence between different footer expressions for the
    same recurrence. After a supported file passes the loader checks, details
    outside this reader's comparison boundary keep exact bytes as evidence.
    """
    if data is None or len(data) > MAX_TZIF_BYTES:
        return None
    try:
        version, counts, cursor, end = _bounds(data, 0, 4)
        if version not in (b"\0", b"2", b"3", b"4"):
            raise ValueError("Unsupported TZif version")
        width = 4
        if version != b"\0":
            _version, counts, cursor, end = _bounds(data, end, 8)
            width = 8
            # The public loader reads the POSIX footer one byte at a time until
            # it finds a newline and never stops at end of file, so a footer it
            # could not finish reading is refused before the loader sees it.
            if data[end : end + 1] != b"\n" or b"\n" not in data[end + 1 :]:
                raise ValueError("Unterminated TZif footer")
        # Before Python 3.13 the public loader accepts a transition type equal
        # to the number of local time types, one past its table, and the
        # process then crashes reading it. The block it loads is checked first.
        count, type_count = counts[3], counts[4]
        types = data[cursor + count * width : cursor + count * (width + 1)]
        if any(index >= type_count for index in types):
            raise ValueError("TZif transition type out of range")
        # The loader takes any flag but zero as daylight, so a flag outside the
        # two declared values drives its table walk into places this reader
        # cannot mirror. Such a value is not a time type this reader interprets,
        # so the file is refused rather than handed over.
        flags = data[cursor + count * (width + 1) : cursor + count * (width + 1) + type_count * 6]
        if any(
            is_dst not in (0, 1) for _offset, is_dst, _name in struct.iter_unpack(">lbb", flags)
        ):
            raise ValueError("Invalid TZif daylight flag")
        # The loader infers each daylight type's offset from the transitions
        # around it, and can turn to the transition after the last one when the
        # last one leaves that offset unplaced. A table with no such transition
        # makes the pure-Python loader raise and the C accelerator read out of
        # bounds, so the table is refused before either of them sees it.
        if _daylight_offset_reads_past_the_table(data, cursor, width, counts):
            raise ValueError("TZif transition table too short for its daylight types")
        zone = zoneinfo.ZoneInfo.from_file(io.BytesIO(data))
    except (ValueError, OverflowError, IndexError, struct.error, UnicodeError, AssertionError):
        return None
    try:
        modern = data[4:5] != b"\0"
        version, end, times, _indices, _types = _block(data, 0, 4, skip=modern)
        footer = b""
        if version != b"\0":
            second_version, end, times, _indices, _types = _block(data, end, 8)
            if second_version != version or data[end : end + 1] != b"\n":
                raise ValueError("Unsupported TZif block or footer")
            footer_end = data.index(b"\n", end + 1)
            footer = data[end + 1 : footer_end]
            if b"\0" in footer:
                raise ValueError("Unsupported TZif footer")
            footer.decode("ascii")
    except (ValueError, OverflowError, IndexError, struct.error, UnicodeError, AssertionError):
        return ("exact-bytes", data)
    return "tzif", zone, footer, times


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
