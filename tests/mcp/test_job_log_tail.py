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


@pytest.fixture
def utf8_locale(monkeypatch):
    monkeypatch.setattr(jobs.locale, "getpreferredencoding", lambda do_setlocale=True: "UTF-8")


def _payload(shift: int, repeats: int) -> bytes:
    pieces = _PIECES[shift % len(_PIECES) :] + _PIECES[: shift % len(_PIECES)]
    return b"".join(pieces) * repeats


@pytest.mark.parametrize("shift", range(len(_PIECES)))
def test_the_tail_matches_a_whole_file_read_at_every_cut(tmp_path, shift, utf8_locale):
    # The read starts four bytes per character before the end, so trailing bytes
    # move the cut through every offset inside the pieces.
    for repeats in (1, 6):
        for trail in range(4):
            log = tmp_path / f"job-{repeats}-{trail}.log"
            log.write_bytes(_payload(shift, repeats) + b"z" * trail)
            for limit in range(1, 40):
                assert jobs._tail(str(log), limit=limit) == _whole_file_tail(log, limit), (
                    repeats,
                    trail,
                    limit,
                )


def test_a_tail_of_four_byte_characters_comes_back_whole(tmp_path, utf8_locale):
    log = tmp_path / "job.log"
    log.write_bytes("🎉".encode() * 50)
    for limit in range(1, 30):
        assert jobs._tail(str(log), limit=limit) == "🎉" * limit


@pytest.mark.parametrize("limit", [0, -1, -5])
def test_a_limit_below_one_keeps_the_slice_it_always_had(tmp_path, limit, utf8_locale):
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


class _CountingFile:
    def __init__(self, fh, sizes):
        self._fh = fh
        self._sizes = sizes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def seek(self, *args):
        return self._fh.seek(*args)

    def read(self, *args):
        data = self._fh.read(*args)
        self._sizes.append(len(data))
        return data


@pytest.fixture
def read_sizes(monkeypatch):
    """Byte counts of every read the tail makes through the module's ``open``."""
    sizes: list[int] = []
    real_open = builtins.open
    monkeypatch.setattr(
        jobs, "open", lambda *a, **k: _CountingFile(real_open(*a, **k), sizes), raising=False
    )
    return sizes


def test_a_long_log_is_read_from_its_end_only(tmp_path, read_sizes, utf8_locale):
    log = tmp_path / "job.log"
    log.write_bytes(b"x" * (1 << 20) + b"last line\n")
    assert jobs._tail(str(log), limit=100) == "x" * 90 + "last line\n"
    assert read_sizes and sum(read_sizes) <= 4 * 100


def test_a_log_in_another_locale_encoding_is_decoded_whole_in_that_encoding(
    tmp_path, monkeypatch, read_sizes
):
    monkeypatch.setattr(jobs.locale, "getpreferredencoding", lambda do_setlocale=True: "latin-1")
    log = tmp_path / "job.log"
    log.write_bytes(b"x" * 50 + "caf\u00e9\r\nna\u00efve".encode("latin-1"))
    assert jobs._tail(str(log), limit=10) == "caf\u00e9\nna\u00efve"
    assert jobs._tail(str(log), limit=11) == "xcaf\u00e9\nna\u00efve"
    assert jobs._tail(str(log), limit=0) == "x" * 50 + "caf\u00e9\nna\u00efve"
    # Only UTF-8 is read from an offset; another encoding is read whole.
    assert read_sizes == [log.stat().st_size] * 3
