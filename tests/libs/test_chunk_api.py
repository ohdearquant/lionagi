# tests/libs/test_chunk_api.py
import importlib

import pytest


def test_chunk_with_direct_text_returns_strings(mod_paths, ensure_fake_lionagi):
    api = importlib.import_module(mod_paths["api_mod"])
    out = api.chunk(
        text="one two three four five six seven",
        chunk_by="tokens",
        tokenizer=str.split,
        chunk_size=3,
        overlap=0.0,
        threshold=0,
        as_node=False,  # return list[str]
    )
    assert isinstance(out, list) and out
    assert all(isinstance(x, str) for x in out)


def test_chunk_with_file_path_reads_and_chunks(mod_paths, ensure_fake_lionagi, tmp_path):
    api = importlib.import_module(mod_paths["api_mod"])
    f = tmp_path / "doc.txt"
    f.write_text(" ".join(str(i) for i in range(30)), encoding="utf-8")
    out = api.chunk(
        url_or_path=f,
        chunk_by="tokens",
        tokenizer=str.split,
        chunk_size=10,
        overlap=0.0,
        threshold=0,
        as_node=False,
    )
    # Expect 3 chunks of ~10 tokens joined as strings
    assert len(out) == 3
    assert all(isinstance(x, str) for x in out)


def test_chunk_with_directory_and_filter(mod_paths, ensure_fake_lionagi, tmp_path):
    api = importlib.import_module(mod_paths["api_mod"])
    d = tmp_path / "in"
    d.mkdir()
    (d / "a.txt").write_text("a b c d e f g h i j", encoding="utf-8")
    (d / "b.md").write_text("k l m n o p q r s t u v", encoding="utf-8")

    # Only .txt should be read
    out = api.chunk(
        url_or_path=d,
        file_types=[".txt"],
        recursive=False,
        chunk_by="tokens",
        tokenizer=str.split,
        chunk_size=5,
        overlap=0.0,
        threshold=0,
        as_node=False,
    )
    assert out and all(isinstance(x, str) for x in out)
    # the .md file content is excluded
    assert "k l m" not in " ".join(out)


def test_chunk_threshold_filtering(mod_paths, ensure_fake_lionagi, tmp_path):
    api = importlib.import_module(mod_paths["api_mod"])
    out = api.chunk(
        text="short text only",
        chunk_by="chars",
        chunk_size=100,
        overlap=0.0,
        threshold=200,  # filter removes small chunks
        as_node=False,
    )
    assert out == []


def test_chunk_output_file_csv(mod_paths, ensure_fake_lionagi, tmp_path):
    pytest.importorskip("pandas")  # Skip if pandas not installed
    api = importlib.import_module(mod_paths["api_mod"])
    out_path = tmp_path / "out.csv"
    # Bug was fixed - should work now
    res = api.chunk(
        text="word1 word2 word3 word4 word5 word6 word7 word8 word9 word10",
        chunk_by="tokens",
        tokenizer=str.split,
        chunk_size=5,
        overlap=0.0,
        threshold=0,
        output_file=out_path,
        as_node=False,
    )
    assert out_path.exists()
    # Should return 2 chunks of 5 words each
    assert isinstance(res, list)
    assert len(res) == 2


def test_chunk_output_file_json(mod_paths, ensure_fake_lionagi, tmp_path):
    api = importlib.import_module(mod_paths["api_mod"])
    out_path = tmp_path / "out.json"
    res = api.chunk(
        text="word1 word2 word3 word4 word5 word6",
        chunk_by="tokens",
        tokenizer=str.split,
        chunk_size=3,
        overlap=0.0,
        threshold=0,
        output_file=out_path,
        as_node=False,
    )
    assert out_path.exists()
    assert len(res) == 2  # 6 tokens with chunk_size=3


def test_chunk_output_file_parquet_ok(mod_paths, ensure_fake_lionagi, tmp_path):
    pytest.importorskip("pyarrow")  # Skip if pyarrow not installed
    pytest.importorskip("pandas")  # Also need pandas for parquet
    api = importlib.import_module(mod_paths["api_mod"])
    out_path = tmp_path / "out.parquet"
    res = api.chunk(
        text=" ".join(str(i) for i in range(50)),
        chunk_by="tokens",
        tokenizer=str.split,
        chunk_size=10,
        overlap=0.0,
        threshold=0,
        output_file=out_path,
        as_node=False,
    )
    assert out_path.exists()
    assert len(res) == 5  # 50 tokens with chunk_size=10


def test_chunk_docling_reader_missing_dependency(
    mod_paths, ensure_fake_lionagi, tmp_path, monkeypatch
):
    """Missing-dependency path runs regardless of environment via monkeypatching.

    Patch is_import_installed on the already-loaded process module so that
    'docling' appears absent, then verify ImportError is raised.
    """
    api = importlib.import_module(mod_paths["api_mod"])
    # Patch is_import_installed on the module so 'docling' looks missing
    monkeypatch.setattr(api, "is_import_installed", lambda name: False)
    # When reader_tool="docling" and docling is reported absent, expect ImportError
    with pytest.raises(ImportError):
        api.chunk(
            url_or_path="some.pdf",
            reader_tool="docling",
            chunk_by="chars",
            chunk_size=1000,
            overlap=0.0,
            threshold=0,
            as_node=False,
        )
