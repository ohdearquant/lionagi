# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for fuzzy_match_keys with non-list Sequence and Mapping key types.

A bare str is a Sequence[str] but iterating it yields single characters, not
key names.  Callers who accidentally pass a raw string must get a clear
TypeError rather than a silent wrong result (char-split) or an opaque
AttributeError.  This module verifies that:

  - tuple and frozenset keys work correctly (the primary regression).
  - dict keys work correctly (Mapping path).
  - A bare str is rejected with TypeError before any matching occurs.
"""

from __future__ import annotations

import pytest

from lionagi.ln.fuzzy._fuzzy_match import fuzzy_match_keys

# ---------------------------------------------------------------------------
# 1. Non-list Sequence types — should work identically to list
# ---------------------------------------------------------------------------


def test_tuple_keys_exact_match():
    """Tuple of key names must work the same as a list."""
    d = {"name": "Alice", "age": 30}
    result = fuzzy_match_keys(d, ("name", "age"))
    assert result["name"] == "Alice"
    assert result["age"] == 30


def test_tuple_keys_fuzzy_match():
    """Fuzzy matching must work when keys are supplied as a tuple."""
    d = {"usr_name": "Bob"}
    # "usr_name" is close enough to "username" with jaro_winkler at 0.7
    result = fuzzy_match_keys(d, ("username",), similarity_threshold=0.7)
    assert "username" in result
    assert result["username"] == "Bob"


def test_frozenset_keys_exact_match():
    """frozenset of key names must work the same as a list."""
    d = {"x": 1, "y": 2}
    result = fuzzy_match_keys(d, frozenset({"x", "y"}))
    assert result["x"] == 1
    assert result["y"] == 2


def test_frozenset_keys_unmatched_removed():
    """Unmatched keys with frozenset input and handle_unmatched='remove'."""
    d = {"x": 1, "extra": 99}
    result = fuzzy_match_keys(d, frozenset({"x"}), handle_unmatched="remove")
    assert "x" in result
    assert "extra" not in result


def test_tuple_empty_keys_returns_copy():
    """An empty tuple for keys must return a copy of the original dict."""
    d = {"a": 1}
    result = fuzzy_match_keys(d, ())
    assert result == d
    assert result is not d


# ---------------------------------------------------------------------------
# 2. Mapping (dict) path — must use .keys()
# ---------------------------------------------------------------------------


def test_dict_keys_exact_match():
    """A dict passed as keys should use its .keys() for matching."""
    d = {"name": "Carol", "age": 25}
    schema = {"name": str, "age": int}
    result = fuzzy_match_keys(d, schema)
    assert result["name"] == "Carol"
    assert result["age"] == 25


# ---------------------------------------------------------------------------
# 3. Bare str — attack-driven: must be rejected, NOT char-split
#
# A str is a Sequence[str] in the type system.  A naive Mapping/Sequence
# dispatch would silently iterate it into individual characters and attempt
# to fuzzy-match each char against the dict's keys — a wrong and silent
# result.  The guard must fire BEFORE any matching logic runs.
# ---------------------------------------------------------------------------


def test_bare_str_raises_type_error():
    """A bare str must raise TypeError, not produce char-split results.

    The silent-wrong-result path would be: set('name') == {'n','a','m','e'} and
    the function would try to match those single-char pseudo-keys against the
    dict.  The correct behaviour is an explicit TypeError before any matching.
    """
    d = {"name": "Dave"}
    with pytest.raises(TypeError, match="bare str"):
        fuzzy_match_keys(d, "name")


def test_bare_str_does_not_char_split():
    """Verify the str guard fires even for a single-char string."""
    d = {"a": 1}
    with pytest.raises(TypeError):
        fuzzy_match_keys(d, "a")


def test_bare_str_longer_key_raises():
    """A multi-char string that looks like a valid key must still raise."""
    d = {"username": "Eve", "email": "eve@example.com"}
    with pytest.raises(TypeError):
        fuzzy_match_keys(d, "username")
