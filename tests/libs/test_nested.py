# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.libs public exports from nested.py."""

from __future__ import annotations


def test_deep_merge_importable_from_libs():
    """deep_merge must be importable directly from lionagi.libs."""
    from lionagi.libs import deep_merge

    assert callable(deep_merge)


def test_deep_merge_basic():
    """deep_merge returns merged dict with override winning on conflict."""
    from lionagi.libs import deep_merge

    base = {"a": 1, "b": {"x": 10, "y": 20}}
    override = {"b": {"y": 99, "z": 30}, "c": 3}
    result = deep_merge(base, override)
    assert result == {"a": 1, "b": {"x": 10, "y": 99, "z": 30}, "c": 3}
    # default non-mutating: base unchanged
    assert base == {"a": 1, "b": {"x": 10, "y": 20}}
