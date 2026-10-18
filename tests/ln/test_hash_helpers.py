"""Digest helpers preserve hashlib values and descriptor ownership."""

import hashlib
import os

import pytest

from lionagi.ln import MAX_HASH_INPUT_BYTES, HashAlgorithm, compute_hash, hash_fd, hash_file


@pytest.mark.parametrize("algorithm", list(HashAlgorithm))
@pytest.mark.parametrize("payload", [b"", b"abc", "café 🦁".encode()])
def test_hash_algorithms_match_hashlib(algorithm, payload, tmp_path):
    expected = getattr(hashlib, algorithm.value)(payload).hexdigest()
    path = tmp_path / "input.bin"
    path.write_bytes(payload)
    assert algorithm.digest_size == len(expected) // 2
    assert compute_hash(payload, algorithm) == expected
    assert hash_file(str(path), algorithm=algorithm, chunk_size=2) == expected
    with path.open("rb") as stream:
        stream.seek(len(payload))
        assert hash_fd(stream.fileno(), algorithm=algorithm, chunk_size=2) == expected
        assert os.lseek(stream.fileno(), 0, os.SEEK_CUR) == 0
        assert stream.read() == payload


def test_uncapped_hashes_keep_large_inputs(tmp_path):
    payload = b"x" * (MAX_HASH_INPUT_BYTES + 1)
    expected = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="limit"):
        compute_hash(payload)
    assert compute_hash(payload, max_bytes=None) == expected
    assert compute_hash(payload.decode(), max_bytes=None) == expected
    path = tmp_path / "large.bin"
    path.write_bytes(payload)
    assert hash_file(path) == expected
    with path.open("rb") as stream:
        assert hash_fd(stream.fileno()) == expected


def test_hash_cap_counts_encoded_bytes_and_includes_boundary():
    assert compute_hash("é", max_bytes=2) == hashlib.sha256("é".encode()).hexdigest()
    with pytest.raises(ValueError, match="2B > 1B limit"):
        compute_hash("é", max_bytes=1)
    assert compute_hash(b"", max_bytes=0) == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_streaming_hash_rejects_nonpositive_chunks(tmp_path, chunk_size):
    path = tmp_path / "input.bin"
    path.write_bytes(b"not empty")
    with pytest.raises(ValueError, match="chunk_size"):
        hash_file(path, chunk_size=chunk_size)
    with path.open("rb") as stream:
        with pytest.raises(ValueError, match="chunk_size"):
            hash_fd(stream.fileno(), chunk_size=chunk_size)


def test_file_hash_propagates_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        hash_file(tmp_path / "missing")


def test_descriptor_error_preserves_open_descriptor_and_read_position(tmp_path, monkeypatch):
    from lionagi.ln import _hash

    path = tmp_path / "input.bin"
    path.write_bytes(b"abcdef")
    original_read = os.read
    error = OSError("read failed")
    with path.open("rb") as stream:
        descriptor = stream.fileno()
        calls = 0

        def failing_read(fd, size):
            nonlocal calls
            if fd == descriptor:
                calls += 1
                if calls == 2:
                    raise error
            return original_read(fd, size)

        monkeypatch.setattr(_hash.os, "read", failing_read)
        with pytest.raises(OSError) as caught:
            hash_fd(descriptor, chunk_size=3)
        assert caught.value is error
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 3
        assert original_read(descriptor, 3) == b"def"
