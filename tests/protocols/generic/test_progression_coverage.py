# tests/protocols/generic/test_progression_coverage.py
"""Targeted coverage tests for progression.py uncovered lines."""

from __future__ import annotations

import pytest

from lionagi._errors import ItemNotFoundError
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.progression import Progression


def _make_prog(*elems) -> tuple[Progression, list[Element]]:
    items = [Element() for _ in range(elems[0] if elems else 3)]
    prog = Progression(order=[e.id for e in items])
    return prog, items


# ---------------------------------------------------------------------------
# __getitem__: TypeError and empty-slice branches
# ---------------------------------------------------------------------------


class TestProgressionGetItem:
    def test_getitem_non_int_raises_type_error(self):
        prog, _ = _make_prog(3)
        with pytest.raises(TypeError, match="integers or slices"):
            _ = prog["string_key"]

    def test_getitem_out_of_range_slice_raises(self):
        prog, _ = _make_prog(3)
        with pytest.raises(ItemNotFoundError):
            _ = prog[10:20]


# ---------------------------------------------------------------------------
# __setitem__: slice path and out-of-range int insert
# ---------------------------------------------------------------------------


class TestProgressionSetItem:
    def test_setitem_slice_replaces_range(self):
        prog, items = _make_prog(4)
        new_id = Element().id
        prog[1:3] = [new_id]
        assert new_id in prog
        assert len(prog) == 3  # 4 - 2 + 1

    def test_setitem_out_of_range_inserts(self):
        prog, items = _make_prog(2)
        new_id = Element().id
        prog[100] = new_id
        assert new_id in prog
        assert len(prog) == 3


# ---------------------------------------------------------------------------
# include: ValueError and empty-refs paths
# ---------------------------------------------------------------------------


class TestProgressionInclude:
    def test_include_invalid_value_returns_false(self):
        prog, _ = _make_prog(2)
        result = prog.include("definitely-not-a-valid-id-or-element")
        assert result is False

    def test_include_none_returns_true(self):
        prog, _ = _make_prog(2)
        original_len = len(prog)
        result = prog.include(None)
        assert result is True
        assert len(prog) == original_len


# ---------------------------------------------------------------------------
# exclude: ValueError and empty-refs paths
# ---------------------------------------------------------------------------


class TestProgressionExclude:
    def test_exclude_invalid_value_returns_false(self):
        prog, _ = _make_prog(2)
        result = prog.exclude("not-a-uuid-at-all")
        assert result is False

    def test_exclude_none_returns_true(self):
        prog, items = _make_prog(2)
        original_len = len(prog)
        result = prog.exclude(None)
        assert result is True
        assert len(prog) == original_len


# ---------------------------------------------------------------------------
# pop: middle-index path
# ---------------------------------------------------------------------------


class TestProgressionPop:
    def test_pop_middle_index(self):
        prog, items = _make_prog(4)
        mid_id = items[1].id
        popped = prog.pop(1)
        assert popped == mid_id
        assert mid_id not in prog
        assert len(prog) == 3


# ---------------------------------------------------------------------------
# remove: invalid UUID string
# ---------------------------------------------------------------------------


class TestProgressionRemove:
    def test_remove_invalid_uuid_raises(self):
        prog, _ = _make_prog(2)
        with pytest.raises(ItemNotFoundError):
            prog.remove("not-a-uuid-string")

    def test_remove_empty_list_returns_early(self):
        prog, items = _make_prog(2)
        orig_len = len(prog)
        prog.remove([])  # empty list → refs = [] → line 358: return
        assert len(prog) == orig_len


# ---------------------------------------------------------------------------
# index: with end parameter
# ---------------------------------------------------------------------------


class TestProgressionIndex:
    def test_index_with_end_param(self):
        prog, items = _make_prog(4)
        idx = prog.index(items[2].id, 0, 4)
        assert idx == 2

    def test_index_with_end_excludes_out_of_range(self):
        prog, items = _make_prog(4)
        with pytest.raises(ValueError):
            prog.index(items[3].id, 0, 2)


# ---------------------------------------------------------------------------
# __add__ and __isub__
# ---------------------------------------------------------------------------


class TestProgressionArithmetic:
    def test_add_creates_new_progression(self):
        prog, items = _make_prog(2)
        extra = Element()
        new_prog = prog + extra.id
        assert extra.id in new_prog
        assert len(new_prog) == 3
        assert extra.id not in prog  # original unchanged

    def test_radd_creates_new_progression(self):
        prog, items = _make_prog(2)
        extra = Element()
        # UUID.__add__(Progression) fails, so Python calls prog.__radd__(extra.id)
        new_prog = extra.id + prog
        assert extra.id in new_prog
        assert len(list(new_prog)) == 3

    def test_isub_removes_id_in_place(self):
        prog, items = _make_prog(3)
        target_id = items[0].id
        prog -= items[0]
        assert target_id not in prog
        assert len(prog) == 2


# ---------------------------------------------------------------------------
# Comparison operators
# ---------------------------------------------------------------------------


class TestProgressionComparisons:
    def _two_progs(self):
        a, b = Element(), Element()
        p1 = Progression(order=[a.id])
        p2 = Progression(order=[b.id])
        return p1, p2

    def test_gt(self):
        p1, p2 = self._two_progs()
        # exactly one is greater
        assert (p1 > p2) != (p2 > p1)

    def test_lt(self):
        p1, p2 = self._two_progs()
        assert (p1 < p2) != (p2 < p1)

    def test_ge_equal(self):
        prog, items = _make_prog(2)
        prog2 = Progression(order=list(prog.order))
        assert prog >= prog2

    def test_le_equal(self):
        prog, items = _make_prog(2)
        prog2 = Progression(order=list(prog.order))
        assert prog <= prog2

    def test_eq_non_progression_returns_not_implemented(self):
        prog, _ = _make_prog(2)
        result = prog.__eq__("not-a-progression")
        assert result is NotImplemented
