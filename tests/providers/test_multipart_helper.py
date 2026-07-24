from __future__ import annotations

import io

import pytest

from lionagi.providers._multipart import _replayable_file_factory


class NonSeekable:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data

    def seekable(self) -> bool:
        return False


def test_none_returns_none() -> None:
    factory = _replayable_file_factory(None, "file")
    assert factory() is None


def test_bytes_are_snapshotted_into_fresh_streams() -> None:
    source = bytearray(b"abc")
    factory = _replayable_file_factory(source, "file")
    source[:] = b"zzz"

    first = factory()
    second = factory()
    assert first is not second
    assert first.read() == b"abc"
    assert second.read() == b"abc"


def test_seekable_stream_preserves_cursor_and_snapshots_suffix() -> None:
    source = io.BytesIO(b"prefix-payload")
    source.seek(len(b"prefix-"))
    original_position = source.tell()

    factory = _replayable_file_factory(source, "image")

    assert source.tell() == original_position
    first = factory()
    second = factory()
    assert first is not second
    assert first.read() == b"payload"
    assert second.read() == b"payload"


def test_non_seekable_stream_is_rejected_when_retryable() -> None:
    with pytest.raises(TypeError, match="audio"):
        _replayable_file_factory(NonSeekable(b"x"), "audio")


def test_non_seekable_stream_is_reused_when_not_retryable() -> None:
    source = NonSeekable(b"x")
    factory = _replayable_file_factory(
        source,
        "audio",
        require_replayable=False,
    )
    assert factory() is source
