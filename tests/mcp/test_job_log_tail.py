# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The job log tail reads only the end of the log and returns what a whole-file text read would."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from lionhub.serve.mcp import jobs

# Pieces that make a cut land inside a multi-byte character, between CR and LF,
# on a lone CR, and inside or right after invalid UTF-8.
_PIECES = (
    b"plain ",
    "é".encode(),
    "中".encode(),
    "🎉".encode(),
    b"\r\n",
    b"\r",
    b"\n",
    b"\x80",
    b"\xe2\x82",
    b"\xff",
    b"\xf0\x90",
    b"\xed\xa0\x80",
    b"z",
)


def _whole_file_tail(path: Path, limit: int) -> str:
    data = path.read_text(encoding="utf-8", errors="replace")
    return data[-limit:] if len(data) > limit else data


def _payload(shift: int, repeats: int) -> bytes:
    pieces = _PIECES[shift % len(_PIECES) :] + _PIECES[: shift % len(_PIECES)]
    return b"".join(pieces) * repeats


@pytest.mark.parametrize("shift", range(len(_PIECES)))
def test_the_tail_matches_a_whole_file_read_at_every_cut(tmp_path, shift):
    for repeats in (1, 6):
        for lead in range(4):
            log = tmp_path / f"job-{repeats}-{lead}.log"
            log.write_bytes(b"x" * lead + _payload(shift, repeats))
            for limit in range(1, 40):
                assert jobs._tail(str(log), limit=limit) == _whole_file_tail(log, limit), (
                    repeats,
                    lead,
                    limit,
                )


def test_a_tail_of_four_byte_characters_needs_every_byte_it_reads(tmp_path):
    log = tmp_path / "job.log"
    log.write_bytes("🎉".encode() * 50)
    for limit in range(1, 30):
        assert jobs._tail(str(log), limit=limit) == "🎉" * limit


@pytest.mark.parametrize("limit", [0, -1, -5])
def test_a_limit_below_one_keeps_the_slice_it_always_had(tmp_path, limit):
    log = tmp_path / "job.log"
    log.write_bytes(_payload(3, 4))
    assert jobs._tail(str(log), limit=limit) == _whole_file_tail(log, limit)


def test_an_empty_or_missing_log(tmp_path):
    empty = tmp_path / "empty.log"
    empty.write_bytes(b"")
    assert jobs._tail(str(empty)) == ""
    assert jobs._tail(str(tmp_path / "missing.log")) is None
    assert jobs._tail(str(tmp_path)) is None
    assert jobs._tail(None) is None
    assert jobs._tail("") is None


def test_a_long_log_is_read_from_its_end_only(tmp_path, monkeypatch):
    log = tmp_path / "job.log"
    log.write_bytes(b"x" * (1 << 20) + b"last line\n")
    read_sizes: list[int] = []
    real_open = builtins.open

    class _Counting:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

        def seek(self, *args):
            return self._fh.seek(*args)

        def read(self, *args):
            data = self._fh.read(*args)
            read_sizes.append(len(data))
            return data

    monkeypatch.setattr(jobs, "open", lambda *a, **k: _Counting(real_open(*a, **k)), raising=False)
    assert jobs._tail(str(log), limit=100) == "x" * 90 + "last line\n"
    assert read_sizes and sum(read_sizes) <= 4 * 100
