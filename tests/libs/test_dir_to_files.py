# tests/libs/test_dir_to_files.py
import importlib

import pytest


def test_dir_to_files_invalid_directory(mod_paths, ensure_fake_lionagi, tmp_path):
    mod = importlib.import_module(mod_paths["api_mod"])
    with pytest.raises(ValueError):
        mod.dir_to_files(tmp_path / "does_not_exist")


def test_dir_to_files_non_recursive_and_filter(mod_paths, ensure_fake_lionagi, tmp_path):
    mod = importlib.import_module(mod_paths["api_mod"])
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.md").write_text("b")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.txt").write_text("c")

    out = mod.dir_to_files(tmp_path, file_types=[".txt", ".md"], recursive=False)
    paths = {p.name for p in out}
    assert paths == {"a.txt", "b.md"}


def test_dir_to_files_recursive_and_filter(mod_paths, ensure_fake_lionagi, tmp_path):
    mod = importlib.import_module(mod_paths["api_mod"])
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.txt").write_text("c")
    (tmp_path / "sub" / "d.md").write_text("d")

    out = mod.dir_to_files(tmp_path, file_types=[".txt"], recursive=True)
    names = sorted(p.name for p in out)
    assert names == ["a.txt", "c.txt"]
