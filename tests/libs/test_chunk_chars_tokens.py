# tests/libs/test_chunk_chars_tokens.py
import importlib

import pytest


def _alpha(n: int) -> str:
    # predictable content with visible indices
    chars = "abcdefghijklmnopqrstuvwxyz"
    out = []
    for i in range(n):
        out.append(chars[i % len(chars)])
    return "".join(out)


def _tokens(n: int) -> list[str]:
    return [f"w{i}" for i in range(n)]


def test_chunk_by_chars_single_chunk_no_overlap(mod_paths, ensure_fake_lionagi):
    mod = importlib.import_module(mod_paths["chunk_mod"])
    text = _alpha(100)
    out = mod.chunk_by_chars(text, chunk_size=200, overlap=0.0, threshold=10)
    assert out == [text]


def test_chunk_by_chars_two_chunks_with_overlap_and_threshold(mod_paths, ensure_fake_lionagi):
    mod = importlib.import_module(mod_paths["chunk_mod"])
    chunk_size = 10
    overlap = 0.5  # overlap_size = int(10 * 0.5 / 2) = 2
    threshold = 3
    text = _alpha(14)  # residue = 4 > threshold -> two chunks
    out = mod.chunk_by_chars(text, chunk_size=chunk_size, overlap=overlap, threshold=threshold)
    assert len(out) == 2
    overlap_size = int(chunk_size * overlap / 2)
    # first chunk is chunk_size + overlap_size
    assert out[0] == text[: chunk_size + overlap_size]
    # second chunk starts at chunk_size - overlap_size
    assert out[1] == text[chunk_size - overlap_size :]


def test_chunk_by_chars_two_chunks_but_small_tail_merges_into_one(mod_paths, ensure_fake_lionagi):
    mod = importlib.import_module(mod_paths["chunk_mod"])
    text = _alpha(13)
    out = mod.chunk_by_chars(text, chunk_size=10, overlap=0.2, threshold=5)
    # residue = 3 <= threshold -> single chunk (original text)
    assert out == [text]


def test_chunk_by_chars_multiple_parts_last_chunk_appended(mod_paths, ensure_fake_lionagi):
    mod = importlib.import_module(mod_paths["chunk_mod"])
    chunk_size = 10
    overlap = 0.2  # overlap_size = 1
    overlap_size = int(chunk_size * overlap / 2)
    threshold = 3
    text_len = 38  # n_chunks = ceil(38/10) = 4
    text = _alpha(text_len)
    out = mod.chunk_by_chars(text, chunk_size=chunk_size, overlap=overlap, threshold=threshold)
    assert len(out) == 4
    last_chunk_start = chunk_size * (4 - 1) - overlap_size  # 29
    assert out[-1] == text[last_chunk_start:]


def test_chunk_by_chars_multiple_parts_small_tail_merged(mod_paths, ensure_fake_lionagi):
    mod = importlib.import_module(mod_paths["chunk_mod"])
    chunk_size = 10
    overlap = 0.2  # overlap_size = 1
    overlap_size = int(chunk_size * overlap / 2)
    threshold = 3
    text_len = 32  # n_chunks = ceil(32/10) = 4; tail from 29 -> length 3 (== threshold), so merge
    text = _alpha(text_len)
    out = mod.chunk_by_chars(text, chunk_size=chunk_size, overlap=overlap, threshold=threshold)
    # last small tail merged into previous chunk -> 3 chunks
    assert len(out) == 3
    # last chunk must end with the tail characters
    merged_tail = text[(chunk_size * (4 - 1) + overlap_size) :]  # from 31:
    assert out[-1].endswith(merged_tail)


@pytest.mark.parametrize("return_tokens", [False, True])
def test_chunk_by_tokens_single_chunk_types(mod_paths, ensure_fake_lionagi, return_tokens):
    mod = importlib.import_module(mod_paths["chunk_mod"])
    toks = _tokens(8)
    out = mod.chunk_by_tokens(
        toks,
        chunk_size=16,
        overlap=0.0,
        threshold=2,
        return_tokens=return_tokens,
    )
    assert len(out) == 1
    if return_tokens:
        assert out[0] == toks
    else:
        assert out[0] == " ".join(toks)


def test_chunk_by_tokens_two_chunks_threshold_path(mod_paths, ensure_fake_lionagi):
    mod = importlib.import_module(mod_paths["chunk_mod"])
    chunk_size = 10
    overlap = 0.5  # overlap_size=2
    threshold = 3
    toks = _tokens(14)  # residue=4>3 -> two chunks
    out = mod.chunk_by_tokens(
        toks,
        chunk_size=chunk_size,
        overlap=overlap,
        threshold=threshold,
        return_tokens=True,
    )
    assert len(out) == 2
    assert out[0] == toks[:12]  # 10 + 2
    assert out[1] == toks[8:]  # 10 - 2


def test_chunk_by_tokens_two_chunks_small_tail_returns_single(mod_paths, ensure_fake_lionagi):
    mod = importlib.import_module(mod_paths["chunk_mod"])
    toks = _tokens(13)  # residue=3
    out = mod.chunk_by_tokens(toks, chunk_size=10, overlap=0.2, threshold=5, return_tokens=False)
    # residue <= threshold -> single chunk string
    assert len(out) == 1
    assert out[0] == " ".join(toks)


def test_chunk_by_tokens_multi_parts_last_chunk_appended(mod_paths, ensure_fake_lionagi):
    mod = importlib.import_module(mod_paths["chunk_mod"])
    chunk_size = 10
    overlap = 0.2  # overlap_size=1
    threshold = 3
    toks = _tokens(38)  # n_chunks=4; last tail size 9 > threshold -> appended
    out = mod.chunk_by_tokens(
        toks,
        chunk_size=chunk_size,
        overlap=overlap,
        threshold=threshold,
        return_tokens=True,
    )
    assert len(out) == 4
    last_chunk_start = chunk_size * (4 - 1) - 1  # 29
    assert out[-1] == toks[last_chunk_start:]
