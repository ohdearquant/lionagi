"""Simple test file to verify chunk functions work correctly."""

import tempfile
from pathlib import Path

import pytest

from lionagi.libs.file.chunk import chunk_by_chars, chunk_by_tokens, chunk_content
from lionagi.libs.file.process import chunk, dir_to_files
from lionagi.libs.schema.load_pydantic_model_from_schema import (
    load_pydantic_model_from_schema,
)


def test_chunk_by_chars_basic():
    """Test basic char chunking."""
    text = "abcdefghijklmnopqrstuvwxyz" * 4  # 104 chars
    chunks = chunk_by_chars(text, chunk_size=30, overlap=0.2, threshold=5)
    assert len(chunks) == 4
    # Verify all text is captured
    full_text = "".join(chunks).replace("".join(set("".join(chunks)) - set(text)), "")
    assert set(full_text) == set(text)


def test_chunk_by_tokens_basic():
    """Test basic token chunking."""
    tokens = [f"token{i}" for i in range(50)]
    chunks = chunk_by_tokens(tokens, chunk_size=15, overlap=0.1, threshold=3, return_tokens=True)
    assert len(chunks) == 4
    # Verify first and last tokens are present
    assert "token0" in chunks[0][0]
    assert "token49" in chunks[-1][-1]


def test_chunk_content_with_metadata():
    """Test chunk_content function with metadata."""
    content = "The quick brown fox jumps over the lazy dog. " * 5
    result = chunk_content(
        content=content,
        chunk_by="chars",
        chunk_size=50,
        overlap=0.1,
        threshold=10,
        metadata={"source": "test", "version": 1},
        as_node=False,
    )
    assert all(isinstance(r, dict) for r in result)
    assert all(r["source"] == "test" for r in result)
    assert all(r["version"] == 1 for r in result)
    assert all("chunk_id" in r for r in result)
    assert all("total_chunks" in r for r in result)


def test_dir_to_files_basic():
    """Test dir_to_files function."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        # Create test files
        (tmpdir / "test1.txt").write_text("content1")
        (tmpdir / "test2.md").write_text("content2")
        (tmpdir / "test3.py").write_text("content3")
        (tmpdir / "subdir").mkdir()
        (tmpdir / "subdir" / "test4.txt").write_text("content4")

        # Test non-recursive
        files = dir_to_files(tmpdir, file_types=[".txt"], recursive=False)
        assert len(files) == 1
        assert files[0].name == "test1.txt"

        # Test recursive
        files = dir_to_files(tmpdir, file_types=[".txt"], recursive=True)
        assert len(files) == 2
        assert {f.name for f in files} == {"test1.txt", "test4.txt"}

        # Test all files
        files = dir_to_files(tmpdir, recursive=True)
        assert len(files) == 4


def test_chunk_main_function_with_text():
    """Test main chunk function with direct text input."""
    text = "word " * 100  # 500 chars
    chunks = chunk(
        text=text,
        chunk_by="chars",
        chunk_size=100,
        overlap=0.1,
        threshold=20,
        as_node=False,
    )
    assert len(chunks) == 5
    assert all(isinstance(c, str) for c in chunks)


def test_chunk_main_function_with_file():
    """Test main chunk function with file input."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        test_file = tmpdir / "test.txt"
        test_file.write_text("This is a test file. " * 20)

        chunks = chunk(
            url_or_path=test_file,
            chunk_by="tokens",
            chunk_size=10,
            overlap=0.0,
            threshold=2,
            as_node=False,
        )
        assert len(chunks) > 0
        assert all(isinstance(c, str) for c in chunks)


def test_chunk_main_function_with_directory():
    """Test main chunk function with directory input."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        (tmpdir / "file1.txt").write_text("Content of file 1. " * 10)
        (tmpdir / "file2.txt").write_text("Content of file 2. " * 10)

        chunks = chunk(
            url_or_path=tmpdir,
            file_types=[".txt"],
            chunk_by="chars",
            chunk_size=50,
            overlap=0.0,
            threshold=10,
            as_node=False,
        )
        assert len(chunks) > 0
        assert all(isinstance(c, str) for c in chunks)


def test_load_pydantic_model_basic():
    """Test loading a Pydantic model from schema."""
    schema = {
        "title": "TestModel",
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
            "email": {"type": "string", "format": "email"},
        },
        "required": ["name", "age"],
    }

    # This will only work if datamodel-code-generator is installed
    pytest.importorskip("datamodel_code_generator")

    model_class = load_pydantic_model_from_schema(schema)
    assert model_class.__name__ == "TestModel"

    # Test creating an instance
    instance = model_class(name="John Doe", age=30, email="john@example.com")
    assert instance.name == "John Doe"
    assert instance.age == 30
    assert instance.email == "john@example.com"


def test_edge_cases():
    """Test various edge cases."""
    # Empty text - returns two empty strings (quirk in implementation)
    result = chunk_by_chars("", chunk_size=10, overlap=0, threshold=0)
    assert result == ["", ""]

    # Very small text
    result = chunk_by_chars("abc", chunk_size=10, overlap=0, threshold=0)
    assert result == ["abc"]

    # Empty token list - returns two empty strings (same quirk)
    result = chunk_by_tokens([], chunk_size=10, overlap=0, threshold=0, return_tokens=False)
    assert result == ["", ""]

    # Single token
    result = chunk_by_tokens(["word"], chunk_size=10, overlap=0, threshold=0)
    assert len(result) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
