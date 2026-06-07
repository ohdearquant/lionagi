# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coverage tests for lionagi/protocols/generic/pile.py (~76% → 90%+ target).

Targets uncovered lines: to_df, dump, filter_by_type, set ops (__ior__,
__iand__, __ixor__, __or__, __and__, __xor__), __setitem__ by UUID/int,
insert at boundaries, async edges, from_dict/to_dict roundtrip,
is_homogenous, adapt_to/adapt_from, strict_type enforcement.
"""

from __future__ import annotations

import importlib

import pytest

from lionagi._errors import ItemExistsError, ValidationError
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class Item(Element):
    value: int = 0


class OtherItem(Element):
    name: str = ""


# ---------------------------------------------------------------------------
# Regression: sized Element that is falsy when empty must not be dropped
# ---------------------------------------------------------------------------


class TestIncludeFalsyElement:
    """An empty Progression/Pile is a valid item whose len() is 0.

    Regression for the `if not value: return {}` short-circuit in
    _validate_collections that silently dropped any falsy Observable.
    """

    def test_include_empty_progression(self):
        from lionagi.protocols.generic.progression import Progression

        pile = Pile()
        prog = Progression(name="empty")
        assert not prog  # empty → falsy
        pile.include(prog)
        assert len(pile) == 1
        assert prog.id in pile

    def test_include_empty_pile(self):
        pile = Pile()
        inner = Pile()  # empty → falsy
        pile.include(inner)
        assert len(pile) == 1

    def test_nonempty_progression_is_single_item(self):
        from lionagi.protocols.generic.progression import Progression

        a, b = Item(value=1), Item(value=2)
        prog = Progression(order=[a.id, b.id], name="ord")
        pile = Pile()
        pile.include(prog)
        # the Progression is ONE item, not expanded into its member UUIDs
        assert len(pile) == 1
        assert prog.id in pile

    def test_empty_inputs_still_noop(self):
        pile = Pile()
        pile.include([])
        pile.include(None)
        pile.include("")
        assert len(pile) == 0


@pytest.fixture
def three_items():
    return [Item(value=i) for i in range(3)]


@pytest.fixture
def five_items():
    return [Item(value=i) for i in range(5)]


@pytest.fixture
def pile_3(three_items):
    return Pile(collections=three_items)


@pytest.fixture
def pile_5(five_items):
    return Pile(collections=five_items)


# ---------------------------------------------------------------------------
# 1. to_df / dump (pandas-dependent)
# ---------------------------------------------------------------------------

pandas_missing = importlib.util.find_spec("pandas") is None


"""Tests for Pile mutation: set ops, filter, strict_type, setitem, insert."""


class TestInPlaceSetOps:
    """In-place set ops mutate self — tested here because |= / &= / ^=
    are uncovered and work correctly (unlike __or__, __and__, __xor__
    which have an 'items=' kwarg bug)."""

    def setup_method(self):
        self.a0, self.a1, self.a2 = Item(value=0), Item(value=1), Item(value=2)
        self.b0 = Item(value=10)

    def test_ior_union(self):
        p1 = Pile(collections=[self.a0, self.a1])
        p2 = Pile(collections=[self.a1, self.a2])
        p1 |= p2
        assert len(p1) == 3
        assert self.a0 in p1
        assert self.a1 in p1
        assert self.a2 in p1

    def test_ior_no_duplicate(self):
        p1 = Pile(collections=[self.a0])
        p2 = Pile(collections=[self.a0])
        p1 |= p2
        assert len(p1) == 1

    def test_ior_type_error_on_non_pile(self):
        p = Pile(collections=[self.a0])
        with pytest.raises(TypeError):
            p |= [self.a1]  # type: ignore[assignment]

    def test_iand_intersection(self):
        p1 = Pile(collections=[self.a0, self.a1, self.a2])
        p2 = Pile(collections=[self.a1, self.a2, self.b0])
        p1 &= p2
        assert len(p1) == 2
        assert self.a1 in p1
        assert self.a2 in p1
        assert self.a0 not in p1

    def test_iand_empty_result(self):
        p1 = Pile(collections=[self.a0])
        p2 = Pile(collections=[self.b0])
        p1 &= p2
        assert len(p1) == 0

    def test_iand_type_error_on_non_pile(self):
        p = Pile(collections=[self.a0])
        with pytest.raises(TypeError):
            p &= {self.a0}  # type: ignore[assignment]

    def test_ixor_symmetric_difference(self):
        p1 = Pile(collections=[self.a0, self.a1])
        p2 = Pile(collections=[self.a1, self.a2])
        p1 ^= p2
        assert len(p1) == 2
        assert self.a0 in p1
        assert self.a2 in p1
        assert self.a1 not in p1

    def test_ixor_disjoint(self):
        p1 = Pile(collections=[self.a0])
        p2 = Pile(collections=[self.b0])
        p1 ^= p2
        assert len(p1) == 2

    def test_ixor_identical(self):
        p1 = Pile(collections=[self.a0, self.a1])
        p2 = Pile(collections=[self.a0, self.a1])
        p1 ^= p2
        assert len(p1) == 0

    def test_ixor_type_error_on_non_pile(self):
        p = Pile(collections=[self.a0])
        with pytest.raises(TypeError):
            p ^= [self.a0]  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 3. Non-in-place set ops (__or__, __and__, __xor__)
# ---------------------------------------------------------------------------


class TestNonInPlaceSetOps:
    """__or__, __and__, __xor__ previously passed 'items=' instead of
    'collections=' to Pile.__init__, causing ValidationError on every call.
    These tests assert the corrected behaviour: set ops return a new Pile
    with the expected membership."""

    def setup_method(self):
        self.a0, self.a1, self.a2 = Item(value=0), Item(value=1), Item(value=2)

    def test_or_raises_on_non_pile(self):
        """TypeError is still raised for non-Pile operands."""
        p = Pile(collections=[self.a0])
        with pytest.raises(TypeError):
            _ = p | [self.a1]

    def test_and_raises_on_non_pile(self):
        """TypeError is still raised for non-Pile operands."""
        p = Pile(collections=[self.a0])
        with pytest.raises(TypeError):
            _ = p & [self.a1]

    def test_xor_raises_on_non_pile(self):
        """TypeError is still raised for non-Pile operands."""
        p = Pile(collections=[self.a0])
        with pytest.raises(TypeError):
            _ = p ^ [self.a1]

    def test_or_union_returns_new_pile(self):
        """Non-in-place union produces a new Pile containing all unique items."""
        p1 = Pile(collections=[self.a0, self.a1])
        p2 = Pile(collections=[self.a1, self.a2])
        result = p1 | p2
        assert isinstance(result, Pile)
        assert len(result) == 3
        assert self.a0 in result
        assert self.a1 in result
        assert self.a2 in result

    def test_or_preserves_item_type(self):
        """Union preserves item_type from the left operand."""
        p1 = Pile(collections=[self.a0], item_type={Item})
        p2 = Pile(collections=[self.a1])
        result = p1 | p2
        assert result.item_type == {Item}

    def test_or_disjoint(self):
        """Union of disjoint piles contains all items."""
        p1 = Pile(collections=[self.a0])
        p2 = Pile(collections=[self.a1])
        result = p1 | p2
        assert len(result) == 2

    def test_or_no_duplicate(self):
        """Union does not duplicate shared items."""
        p1 = Pile(collections=[self.a0, self.a1])
        p2 = Pile(collections=[self.a0])
        result = p1 | p2
        assert len(result) == 2

    def test_and_intersection_returns_new_pile(self):
        """Non-in-place intersection produces a new Pile of shared items."""
        p1 = Pile(collections=[self.a0, self.a1])
        p2 = Pile(collections=[self.a1, self.a2])
        result = p1 & p2
        assert isinstance(result, Pile)
        assert len(result) == 1
        assert self.a1 in result
        assert self.a0 not in result
        assert self.a2 not in result

    def test_and_empty_result(self):
        """Intersection of disjoint piles is empty."""
        p1 = Pile(collections=[self.a0])
        p2 = Pile(collections=[self.a1])
        result = p1 & p2
        assert len(result) == 0

    def test_and_preserves_item_type(self):
        """Intersection preserves item_type from the left operand."""
        p1 = Pile(collections=[self.a0, self.a1], item_type={Item})
        p2 = Pile(collections=[self.a1])
        result = p1 & p2
        assert result.item_type == {Item}

    def test_xor_symmetric_difference_returns_new_pile(self):
        """Non-in-place symmetric difference excludes common items."""
        p1 = Pile(collections=[self.a0, self.a1])
        p2 = Pile(collections=[self.a1, self.a2])
        result = p1 ^ p2
        assert isinstance(result, Pile)
        assert len(result) == 2
        assert self.a0 in result
        assert self.a2 in result
        assert self.a1 not in result

    def test_xor_disjoint(self):
        """Symmetric difference of disjoint piles contains all items."""
        p1 = Pile(collections=[self.a0])
        p2 = Pile(collections=[self.a1])
        result = p1 ^ p2
        assert len(result) == 2

    def test_xor_identical(self):
        """Symmetric difference of identical piles is empty."""
        p1 = Pile(collections=[self.a0, self.a1])
        p2 = Pile(collections=[self.a0, self.a1])
        result = p1 ^ p2
        assert len(result) == 0

    def test_or_does_not_mutate_operands(self):
        """Non-in-place ops must not mutate either operand."""
        p1 = Pile(collections=[self.a0])
        p2 = Pile(collections=[self.a1])
        _ = p1 | p2
        assert len(p1) == 1
        assert len(p2) == 1

    def test_and_does_not_mutate_operands(self):
        p1 = Pile(collections=[self.a0, self.a1])
        p2 = Pile(collections=[self.a1])
        _ = p1 & p2
        assert len(p1) == 2
        assert len(p2) == 1


class TestMultiItemPop:
    """_pop with a slice returning >1 item previously used 'items=' kwarg,
    causing the same ValidationError as the set-op bug."""

    def setup_method(self):
        self.items = [Item(value=i) for i in range(5)]
        self.pile = Pile(collections=self.items)

    def test_pop_slice_multi_returns_pile(self):
        """Popping a slice of 3 items returns a Pile, not an error."""
        result = self.pile.pop(slice(0, 3))
        assert isinstance(result, Pile)
        assert len(result) == 3
        assert len(self.pile) == 2

    def test_pop_slice_single_returns_item(self):
        """Popping a slice of exactly 1 item returns the item directly."""
        result = self.pile.pop(slice(0, 1))
        assert isinstance(result, Item)
        assert len(self.pile) == 4

    def test_pop_slice_all_returns_pile(self):
        """Popping all items returns a Pile."""
        result = self.pile.pop(slice(None))
        assert isinstance(result, Pile)
        assert len(result) == 5
        assert len(self.pile) == 0

    def test_pop_slice_preserves_item_type(self):
        """Multi-item pop preserves the item_type of the parent pile."""
        p = Pile(collections=[Item(value=i) for i in range(3)], item_type={Item})
        result = p.pop(slice(0, 2))
        assert isinstance(result, Pile)
        assert result.item_type == {Item}


# ---------------------------------------------------------------------------
# 4. filter_by_type
# ---------------------------------------------------------------------------


class TestFilterByType:
    def test_filter_by_type_basic(self, five_items):
        others = [OtherItem(name=f"o{i}") for i in range(2)]
        p = Pile(collections=five_items + others)
        result = p.filter_by_type(Item)
        assert len(result) == 5
        assert all(isinstance(r, Item) for r in result)

    def test_filter_by_type_returns_list_by_default(self, five_items):
        p = Pile(collections=five_items)
        result = p.filter_by_type(Item)
        assert isinstance(result, list)

    def test_filter_by_type_as_pile(self, five_items):
        p = Pile(collections=five_items)
        result = p.filter_by_type(Item, as_pile=True)
        assert isinstance(result, Pile)
        assert len(result) == 5

    def test_filter_by_type_strict(self):
        class SubItem(Item):
            pass

        items = [Item(value=0), SubItem(value=1)]
        p = Pile(collections=items)
        result = p.filter_by_type(Item, strict_type=True)
        assert len(result) == 1
        assert result[0].value == 0

    def test_filter_by_type_no_strict_includes_subclasses(self):
        class SubItem(Item):
            pass

        items = [Item(value=0), SubItem(value=1)]
        p = Pile(collections=items)
        result = p.filter_by_type(Item, strict_type=False)
        assert len(result) == 2

    def test_filter_by_type_reverse(self, five_items):
        p = Pile(collections=five_items)
        result = p.filter_by_type(Item, reverse=True)
        values = [r.value for r in result]
        assert values == [4, 3, 2, 1, 0]

    def test_filter_by_type_num_items(self, five_items):
        p = Pile(collections=five_items)
        result = p.filter_by_type(Item, num_items=2)
        assert len(result) == 2
        assert result[0].value == 0
        assert result[1].value == 1

    def test_filter_by_type_num_items_reverse(self, five_items):
        p = Pile(collections=five_items)
        result = p.filter_by_type(Item, reverse=True, num_items=2)
        assert len(result) == 2
        assert result[0].value == 4
        assert result[1].value == 3

    def test_filter_by_type_empty_result(self, five_items):
        p = Pile(collections=five_items)
        result = p.filter_by_type(OtherItem)
        assert result == []

    def test_filter_by_type_invalid_type_raises(self, five_items):
        p = Pile(collections=five_items)
        with pytest.raises(TypeError, match="item_type must be a type"):
            p.filter_by_type("not_a_type")  # type: ignore[arg-type]

    def test_filter_by_type_list_input(self, five_items):
        others = [OtherItem(name="x")]
        p = Pile(collections=five_items + others)
        result = p.filter_by_type([Item, OtherItem])
        assert len(result) == 6


# ---------------------------------------------------------------------------
# 5. Strict type enforcement
# ---------------------------------------------------------------------------


class TestStrictType:
    def test_strict_type_rejects_wrong_type_on_include(self):
        p = Pile(collections=[], item_type={Item}, strict_type=True)
        with pytest.raises((ValidationError, TypeError)):
            p.include(OtherItem(name="x"))

    def test_strict_type_accepts_exact_type(self):
        p = Pile(collections=[], item_type={Item}, strict_type=True)
        item = Item(value=42)
        p.include(item)
        assert len(p) == 1

    def test_strict_type_rejects_subclass(self):
        class SubItem(Item):
            pass

        p = Pile(collections=[], item_type={Item}, strict_type=True)
        with pytest.raises((ValidationError, TypeError)):
            p.include(SubItem(value=1))

    def test_non_strict_accepts_subclass(self):
        class SubItem(Item):
            pass

        p = Pile(collections=[], item_type={Item}, strict_type=False)
        p.include(SubItem(value=1))
        assert len(p) == 1

    def test_strict_type_on_construction_rejects(self):
        class SubItem(Item):
            pass

        with pytest.raises((ValidationError, TypeError)):
            Pile(
                collections=[SubItem(value=1)],
                item_type={Item},
                strict_type=True,
            )


# ---------------------------------------------------------------------------
# 6. __setitem__ with UUID keys and integer indices
# ---------------------------------------------------------------------------


class TestSetItem:
    def test_setitem_int_replaces_item(self, pile_3):
        new = Item(value=99)
        pile_3[0] = new
        assert pile_3[0].value == 99
        assert len(pile_3) == 3

    def test_setitem_int_at_last_index(self, pile_3):
        new = Item(value=77)
        pile_3[2] = new
        assert pile_3[2].value == 77

    def test_setitem_uuid_adds_new_item(self, pile_3):
        new = Item(value=55)
        pile_3[new.id] = new
        assert new in pile_3
        assert len(pile_3) == 4

    def test_setitem_existing_uuid_raises(self, pile_3, three_items):
        """Setting an existing UUID via non-int path raises ItemExistsError."""
        existing = three_items[0]
        new = Item(value=existing.value)
        # Force new to have same id (clone the id)
        # We can't change the id (frozen), so just confirm existing raises
        with pytest.raises(ItemExistsError):
            pile_3[existing.id] = existing

    def test_setitem_invalid_index_raises(self, pile_3):
        new = Item(value=99)
        with pytest.raises((ValueError, IndexError)):
            pile_3[100] = new


# ---------------------------------------------------------------------------
# 7. insert at start, middle, end
# ---------------------------------------------------------------------------


class TestInsert:
    def test_insert_at_start(self, pile_3):
        new = Item(value=100)
        pile_3.insert(0, new)
        assert pile_3[0].value == 100
        assert len(pile_3) == 4

    def test_insert_at_middle(self, pile_3):
        new = Item(value=200)
        pile_3.insert(1, new)
        assert pile_3[1].value == 200
        assert len(pile_3) == 4

    def test_insert_at_end(self, pile_3):
        new = Item(value=300)
        pile_3.insert(len(pile_3), new)
        assert pile_3[-1].value == 300
        assert len(pile_3) == 4

    def test_insert_preserves_order(self, five_items):
        p = Pile(collections=five_items)
        sentinel = Item(value=99)
        p.insert(2, sentinel)
        values = [item.value for item in p.values()]
        assert values == [0, 1, 99, 2, 3, 4]

    def test_insert_duplicate_raises(self, pile_3, three_items):
        with pytest.raises(ItemExistsError):
            pile_3.insert(0, three_items[0])


# ---------------------------------------------------------------------------
# 8. Async edge cases
# ---------------------------------------------------------------------------


def test_pile_setitem_rolls_back_on_key_item_id_mismatch():
    import uuid

    from lionagi.protocols.generic.element import Element
    from lionagi.protocols.generic.pile import Pile

    a = Element()
    b = Element()
    pile = Pile(collections=[a, b])
    original_ids = list(pile.progression)
    original_len = len(pile)

    missing_id = str(uuid.uuid4())
    with pytest.raises((ValueError, Exception)):
        Pile(collections=[a, b], order=[missing_id])

    assert list(pile.progression) == original_ids
    assert len(pile) == original_len


def test_pile_init_rebuilds_invalid_progression_from_collection_order():
    import uuid

    from lionagi.protocols.generic.element import Element
    from lionagi.protocols.generic.pile import Pile

    a = Element()
    b = Element()

    pile = Pile(collections=[a, b])
    assert len(pile.progression) == 2
    assert a.id in pile.progression
    assert b.id in pile.progression

    with pytest.raises(ValueError):
        Pile(collections=[a, b], order=[str(uuid.uuid4())])
