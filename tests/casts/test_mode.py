# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Mode (lionagi/casts/pattern.py), inline-Python modes."""

import pytest

from lionagi.casts.pattern import Mode, PatternKind, list_modes


def test_all_fourteen_modes_load():
    names = list_modes()
    assert len(names) == 14
    for n in names:
        m = Mode.load(n)
        assert m.name == n
        assert m.kind == PatternKind.MODE
        assert m.description
        assert m.behaviors


def test_mode_load_parses_conflicts():
    fast = Mode.load("fast")
    assert fast.conflicts_with == frozenset({"slow", "systematic"})


def test_mode_dashed_name_loads():
    # mode names may contain dashes; the module stem uses underscores
    m = Mode.load("constraint-solving")
    assert m.name == "constraint-solving"
    assert m.kind == PatternKind.MODE


def test_mode_is_frozen():
    m = Mode(name="x", description="test", behaviors="do stuff")
    with pytest.raises(AttributeError):
        m.name = "y"


def test_mode_to_dict_excludes_empty():
    m = Mode(name="x", description="test")
    d = m.to_dict()
    assert "behaviors" not in d
    assert "conflicts_with" not in d


def test_mode_kind_is_mode():
    m = Mode(name="x", description="test")
    assert m.kind == PatternKind.MODE
