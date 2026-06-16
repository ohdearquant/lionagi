# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for fuzzy_match_keys with non-list Sequence and Mapping key types."""

from __future__ import annotations

import pytest

from lionagi.ln.fuzzy._fuzzy_match import fuzzy_match_keys

# ---------------------------------------------------------------------------
# 1. Non-list Sequence types — should work identically to list
# ---------------------------------------------------------------------------


def test_tuple_keys_exact_match():
    d = {"name": "Alice", "age": 30}
    result = fuzzy_match_keys(d, ("name", "age"))
    assert result["name"] == "Alice"
    assert result["age"] == 30


def test_tuple_keys_fuzzy_match():
    d = {"usr_name": "Bob"}
    # "usr_name" is close enough to "username" with jaro_winkler at 0.7
    result = fuzzy_match_keys(d, ("username",), similarity_threshold=0.7)
    assert "username" in result
    assert result["username"] == "Bob"


def test_frozenset_keys_exact_match():
    d = {"x": 1, "y": 2}
    result = fuzzy_match_keys(d, frozenset({"x", "y"}))
    assert result["x"] == 1
    assert result["y"] == 2


def test_frozenset_keys_unmatched_removed():
    d = {"x": 1, "extra": 99}
    result = fuzzy_match_keys(d, frozenset({"x"}), handle_unmatched="remove")
    assert "x" in result
    assert "extra" not in result


def test_tuple_empty_keys_returns_copy():
    d = {"a": 1}
    result = fuzzy_match_keys(d, ())
    assert result == d
    assert result is not d


# ---------------------------------------------------------------------------
# 2. Mapping (dict) path — must use .keys()
# ---------------------------------------------------------------------------


def test_dict_keys_exact_match():
    d = {"name": "Carol", "age": 25}
    schema = {"name": str, "age": int}
    result = fuzzy_match_keys(d, schema)
    assert result["name"] == "Carol"
    assert result["age"] == 25


# ---------------------------------------------------------------------------
# 3. Bare str — must be rejected, NOT char-split (str is a Sequence[str] so
#    a naive dispatch would silently iterate chars; guard must fire first)
# ---------------------------------------------------------------------------


def test_bare_str_raises_type_error():
    d = {"name": "Dave"}
    with pytest.raises(TypeError, match="bare str"):
        fuzzy_match_keys(d, "name")


def test_bare_str_does_not_char_split():
    d = {"a": 1}
    with pytest.raises(TypeError):
        fuzzy_match_keys(d, "a")


def test_bare_str_longer_key_raises():
    d = {"username": "Eve", "email": "eve@example.com"}
    with pytest.raises(TypeError):
        fuzzy_match_keys(d, "username")
