# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coverage tests for lionagi/libs/file/process.py — uncovered paths."""

import logging
from pathlib import Path

import pytest

from lionagi.libs.file.process import chunk, dir_to_files

# ---------------------------------------------------------------------------
# dir_to_files: verbose logging (line 76)
# ---------------------------------------------------------------------------


def test_dir_to_files_verbose_logs_count(tmp_path, caplog):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.txt").write_text("world")

    with caplog.at_level(logging.INFO, logger="lionagi.libs.file.process"):
        result = dir_to_files(tmp_path, verbose=True)

    assert len(result) == 2


# ---------------------------------------------------------------------------
# dir_to_files: inner process_file exception with ignore_errors=True (lines 55-58)
# ---------------------------------------------------------------------------


def test_dir_to_files_process_file_exception_ignored_with_verbose(tmp_path, monkeypatch, caplog):
    (tmp_path / "good.py").write_text("x = 1")

    class _BadPath:
        def is_file(self):
            return True

        @property
        def suffix(self):
            raise OSError("simulated suffix error")

        def __str__(self):
            return "/fake/bad_file.py"

    original_glob = Path.glob

    def patched_glob(self, pattern):
        if pattern == "*":
            return iter([_BadPath()])
        return original_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", patched_glob)

    with caplog.at_level(logging.WARNING):
        result = dir_to_files(tmp_path, file_types=[".py"], ignore_errors=True, verbose=True)

    assert result == []


# ---------------------------------------------------------------------------
# dir_to_files: inner exception with ignore_errors=False → bubbles as ValueError (lines 59-60, 79-80)
# ---------------------------------------------------------------------------


def test_dir_to_files_process_file_exception_raised_when_not_ignored(tmp_path, monkeypatch):
    class _BadPath:
        def is_file(self):
            return True

        @property
        def suffix(self):
            raise OSError("simulated suffix error")

        def __str__(self):
            return "/fake/bad_file.py"

    original_glob = Path.glob

    def patched_glob(self, pattern):
        if pattern == "*":
            return iter([_BadPath()])
        return original_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", patched_glob)

    with pytest.raises(ValueError):
        dir_to_files(tmp_path, file_types=[".py"], ignore_errors=False)


# ---------------------------------------------------------------------------
# chunk: as_node=True returns node objects (line 171)
# ---------------------------------------------------------------------------


def test_chunk_as_node_returns_nodes():
    text = "word " * 200
    result = chunk(text=text, as_node=True, chunk_size=100, threshold=10)
    assert isinstance(result, list)
    assert len(result) > 0
    assert all(hasattr(c, "content") for c in result)


# ---------------------------------------------------------------------------
# chunk: unsupported output file format → ValueError (line 168)
# ---------------------------------------------------------------------------


def test_chunk_unsupported_output_format_raises(tmp_path):
    text = "word " * 200
    out = str(tmp_path / "out.xyz")
    with pytest.raises(ValueError, match="Unsupported output file format"):
        chunk(text=text, output_file=out, chunk_size=100, threshold=10)


# ---------------------------------------------------------------------------
# chunk: reader_tool="docling" without docling → ImportError (lines 123-128)
# ---------------------------------------------------------------------------


def test_chunk_docling_not_installed_raises(tmp_path, monkeypatch):
    import lionagi.libs.file.process as proc_mod

    monkeypatch.setattr(proc_mod, "is_import_installed", lambda pkg: False)

    f = tmp_path / "doc.txt"
    f.write_text("some content")

    with pytest.raises(ImportError, match="docling"):
        chunk(url_or_path=f, reader_tool="docling", chunk_size=100, threshold=10)
