# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Mode (lionagi/casts/pattern.py)."""

from pathlib import Path

import pytest

from lionagi.casts.pattern import Mode, PatternKind

MODES_DIR = Path(__file__).parent.parent.parent / "lionagi" / "casts" / "roles" / "modes"


def test_all_fourteen_modes_load():
    modes = [Mode.from_file(p) for p in sorted(MODES_DIR.glob("*.md"))]
    assert len(modes) == 14
    for m in modes:
        assert m.kind == PatternKind.MODE
        assert m.description
        assert m.behaviors


def test_mode_from_md_parses_conflicts():
    fast = Mode.from_file(MODES_DIR / "fast.md")
    assert fast.conflicts_with == frozenset({"slow", "systematic"})


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
