# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi/utils.py: create_path, copy, and related utilities."""

from typing import Annotated, Optional, Union

import pytest

from lionagi.utils import copy, create_path, is_same_dtype, union_members

# ---------------------------------------------------------------------------
# create_path rejects backslash and existing file without overwrite
# ---------------------------------------------------------------------------


def test_create_path_rejects_backslash_and_existing_file_without_overwrite(tmp_path):
    with pytest.raises(ValueError):
        create_path(tmp_path, "bad\\name.txt")

    existing = tmp_path / "report.txt"
    existing.write_text("content")

    with pytest.raises(FileExistsError):
        create_path(tmp_path, "report.txt", file_exist_ok=False)


def test_create_path_returns_correct_path(tmp_path):
    p = create_path(tmp_path, "output.txt")
    assert p.parent == tmp_path
    assert p.name == "output.txt"


def test_create_path_creates_parent_directories(tmp_path):
    p = create_path(tmp_path / "deep" / "subdir", "file.txt")
    assert p.parent.exists()


def test_create_path_file_exist_ok_true_allows_existing(tmp_path):
    existing = tmp_path / "exists.txt"
    existing.write_text("data")
    p = create_path(tmp_path, "exists.txt", file_exist_ok=True)
    assert p == existing


def test_create_path_subdirectory_in_filename(tmp_path):
    p = create_path(tmp_path, "sub/file.txt")
    assert p.parent == tmp_path / "sub"
    assert p.name == "file.txt"


def test_create_path_with_extension_arg(tmp_path):
    p = create_path(tmp_path, "report", extension="md")
    assert p.suffix == ".md"
    assert p.stem == "report"


# ---------------------------------------------------------------------------
# copy utility
# ---------------------------------------------------------------------------


def test_copy_deep_returns_independent_copy():
    original = {"x": [1, 2, 3]}
    clone = copy(original)
    clone["x"].append(99)
    assert original["x"] == [1, 2, 3]


def test_copy_shallow_shares_nested():
    original = {"x": [1, 2]}
    clone = copy(original, deep=False)
    clone["x"].append(99)
    assert original["x"][-1] == 99


def test_copy_num_returns_list():
    original = [1, 2]
    copies = copy(original, num=3)
    assert isinstance(copies, list)
    assert len(copies) == 3
    assert all(c == original for c in copies)
    assert all(c is not original for c in copies)


def test_copy_num_one_returns_single():
    original = {"a": 1}
    result = copy(original, num=1)
    assert isinstance(result, dict)
    assert result == original


def test_copy_rejects_non_positive_copy_count():
    with pytest.raises(ValueError):
        copy({"x": []}, num=0)

    with pytest.raises(ValueError):
        copy({"x": []}, num=-1)


# ---------------------------------------------------------------------------
# is_same_dtype — Mapping branch (lines 92-97)
# ---------------------------------------------------------------------------


def test_is_same_dtype_with_mapping_same_types():
    assert is_same_dtype({"a": 1, "b": 2, "c": 3}) is True


def test_is_same_dtype_with_mapping_mixed_types():
    assert is_same_dtype({"a": 1, "b": "two"}) is False


def test_is_same_dtype_with_mapping_explicit_dtype():
    assert is_same_dtype({"a": 1, "b": 2}, dtype=int) is True
    assert is_same_dtype({"a": 1, "b": "x"}, dtype=int) is False


def test_is_same_dtype_list_infers_dtype():
    assert is_same_dtype([1, 2, 3]) is True
    assert is_same_dtype([1, "two", 3]) is False


# ---------------------------------------------------------------------------
# union_members — lines 131-140 (_unwrap_annotated at 122-124 is exercised too)
# ---------------------------------------------------------------------------


def test_union_members_basic():
    members = union_members(Union[int, str])
    assert int in members
    assert str in members


def test_union_members_drop_none():
    members = union_members(Optional[int], drop_none=True)
    assert len(members) == 1
    assert int in members


def test_union_members_non_union_returns_empty():
    assert union_members(int) == ()
    assert union_members(str) == ()


def test_union_members_with_annotated_wrapping():
    tp = Annotated[Union[int, str], "metadata"]
    members = union_members(tp)
    assert int in members
    assert str in members


def test_union_members_unwrap_annotated_false():
    members = union_members(Union[int, str], unwrap_annotated=False)
    assert int in members
    assert str in members


# ---------------------------------------------------------------------------
# create_path — time_prefix and random_hash_digits (lines 195-202)
# ---------------------------------------------------------------------------


def test_create_path_timestamp_prefix(tmp_path):
    p = create_path(tmp_path, "log.txt", timestamp=True, time_prefix=True)
    # When time_prefix=True the timestamp comes first: "{ts}_{name}.ext"
    assert "_log" in p.stem


def test_create_path_timestamp_suffix(tmp_path):
    p = create_path(tmp_path, "log.txt", timestamp=True, time_prefix=False)
    assert "log_" in p.stem


def test_create_path_random_hash_appended(tmp_path):
    p = create_path(tmp_path, "out.txt", random_hash_digits=8)
    # stem should be "out-{8hexchars}"
    assert "-" in p.stem
    suffix_part = p.stem.split("-")[-1]
    assert len(suffix_part) == 8
    assert all(c in "0123456789abcdef" for c in suffix_part)


def test_create_path_timestamp_format(tmp_path):
    p = create_path(tmp_path, "out.txt", timestamp=True, timestamp_format="%Y")
    import datetime

    year = datetime.datetime.now().strftime("%Y")
    assert year in p.stem
