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
from uuid import UUID

import pytest

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class Item(Element):
    value: int = 0


class OtherItem(Element):
    name: str = ""


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


"""Tests for Pile filter, homogeneity, adapt_to, misc, and filter_method."""


class TestIsHomogenous:
    """is_homogenous previously iterated over dict_values directly, causing
    TypeError in is_same_dtype which requires a list or Mapping, not
    dict_values. Fixed by materialising list(self.collections.values())
    before the call."""

    def test_empty_is_homogenous(self):
        p = Pile()
        assert p.is_homogenous() is True

    def test_single_item_is_homogenous(self):
        p = Pile(collections=[Item(value=0)])
        assert p.is_homogenous() is True

    def test_same_type_two_items_is_homogenous(self):
        """Two items of the same type must return True (exercises the fixed path)."""
        p = Pile(collections=[Item(value=0), Item(value=1)])
        assert p.is_homogenous() is True

    def test_same_type_five_items_is_homogenous(self):
        """Five items of the same type must return True."""
        items = [Item(value=i) for i in range(5)]
        p = Pile(collections=items)
        assert p.is_homogenous() is True

    def test_mixed_types_is_not_homogenous(self):
        """A pile with two different concrete types is not homogenous."""
        items = [Item(value=0), OtherItem(name="x")]
        p = Pile(collections=items)
        assert p.is_homogenous() is False

    def test_mixed_types_three_items_is_not_homogenous(self):
        """Mixed-type pile with 3 items is not homogenous."""
        items = [Item(value=0), Item(value=1), OtherItem(name="x")]
        p = Pile(collections=items)
        assert p.is_homogenous() is False

    def test_empty_pile_homogenous(self):
        assert Pile().is_homogenous() is True


# ---------------------------------------------------------------------------
# 11. adapt_to / adapt_from (json)
# ---------------------------------------------------------------------------


class TestAdaptTo:
    def test_adapt_to_json_returns_string(self, pile_3):
        result = pile_3.adapt_to("json", many=True)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_adapt_to_json_content(self, pile_3):
        result = pile_3.adapt_to("json", many=True)
        for item in pile_3.values():
            assert str(item.id) in result

    def test_adapt_to_csv_returns_string(self, pile_3):
        result = pile_3.adapt_to("csv", many=True)
        assert isinstance(result, str)
        assert "id" in result

    @pytest.mark.asyncio
    async def test_adapt_to_async_json(self, pile_3):
        # Only async adapters registered work; 'json' is sync-only — assert
        # it raises the expected error (AdapterNotFoundError from our local
        # adapter stack, previously sourced from pydapter.exceptions).
        from lionagi.adapters._base import AdapterNotFoundError

        with pytest.raises(AdapterNotFoundError):
            await pile_3.adapt_to_async("json", many=True)


# ---------------------------------------------------------------------------
# 12. Misc: __repr__, __str__, __bool__, keys/values/items, size/is_empty
# ---------------------------------------------------------------------------


class TestMisc:
    def test_repr_empty(self):
        assert repr(Pile()) == "Pile()"

    def test_repr_single(self):
        item = Item(value=1)
        p = Pile(collections=[item])
        r = repr(p)
        assert r.startswith("Pile(")

    def test_repr_multiple(self, pile_3):
        assert repr(pile_3) == "Pile(3)"

    def test_str(self, pile_3):
        assert str(pile_3) == "Pile(3)"

    def test_bool_empty(self):
        assert not Pile()

    def test_bool_non_empty(self, pile_3):
        assert pile_3

    def test_size(self, pile_3):
        assert pile_3.size() == 3

    def test_is_empty_false(self, pile_3):
        assert not pile_3.is_empty()

    def test_is_empty_true(self):
        assert Pile().is_empty()

    def test_keys_returns_ids(self, pile_3, three_items):
        keys = pile_3.keys()
        assert all(isinstance(k, UUID) for k in keys)
        assert set(keys) == {i.id for i in three_items}

    def test_values_in_order(self, five_items):
        p = Pile(collections=five_items)
        vals = p.values()
        assert [v.value for v in vals] == list(range(5))

    def test_items_pairs(self, three_items):
        p = Pile(collections=three_items)
        pairs = p.items()
        for uuid, item in pairs:
            assert isinstance(uuid, UUID)
            assert isinstance(item, Item)

    def test_next_raises_on_empty(self):
        p = Pile()
        with pytest.raises(StopIteration):
            next(p)

    def test_next_returns_first(self, pile_3, three_items):
        first = next(pile_3)
        assert first == three_items[0]

    def test_append_alias_for_update(self, pile_3):
        new = Item(value=99)
        pile_3.append(new)
        assert new in pile_3
        assert len(pile_3) == 4

    def test_remove_int_raises_type_error(self, pile_3):
        with pytest.raises(TypeError):
            pile_3.remove(0)  # type: ignore[arg-type]

    def test_get_by_uuid(self, pile_3, three_items):
        target = three_items[1]
        result = pile_3.get(target.id)
        assert result == target

    def test_get_missing_uuid_default(self, pile_3):
        missing_id = Item().id
        assert pile_3.get(missing_id, None) is None

    def test_update_existing_item_overwrites(self, pile_3, three_items):
        # An item with same id updates in-place without changing length
        updated = Item.model_construct(
            id=three_items[0].id,
            value=999,
            created_at=three_items[0].created_at,
            metadata={},
        )
        pile_3.update(updated)
        assert len(pile_3) == 3
        assert pile_3[three_items[0].id].value == 999


# ---------------------------------------------------------------------------
# 13. AsyncPileIterator  (inner class)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_pile_iterator_class():
    items = [Item(value=i) for i in range(3)]
    p = Pile(collections=items)
    it = Pile.AsyncPileIterator(p)
    assert it.__aiter__() is it
    first = await it.__anext__()
    assert first.value == 0
    second = await it.__anext__()
    assert second.value == 1


@pytest.mark.asyncio
async def test_async_pile_iterator_stop():
    p = Pile(collections=[Item(value=0)])
    it = Pile.AsyncPileIterator(p)
    await it.__anext__()
    with pytest.raises(StopAsyncIteration):
        await it.__anext__()


# ---------------------------------------------------------------------------
# 14. filter() with lambda and type predicates
# ---------------------------------------------------------------------------


class TestFilterMethod:
    def test_filter_lambda(self, five_items):
        p = Pile(collections=five_items)
        result = p.filter(lambda x: x.value > 2)
        assert len(result) == 2
        assert all(item.value > 2 for item in result)

    def test_filter_returns_new_pile(self, pile_3):
        result = pile_3.filter(lambda x: True)
        assert isinstance(result, Pile)
        assert result is not pile_3

    def test_filter_type_check_predicate(self):
        items = [Item(value=i) for i in range(3)]
        others = [OtherItem(name="x")]
        p = Pile(collections=items + others)
        result = p.filter(lambda x: isinstance(x, Item))
        assert len(result) == 3

    def test_filter_no_match_empty(self, pile_3):
        result = pile_3.filter(lambda x: False)
        assert isinstance(result, Pile)
        assert len(result) == 0

    def test_filter_preserves_order(self, five_items):
        p = Pile(collections=five_items)
        result = p.filter(lambda x: x.value % 2 == 0)
        values = [item.value for item in result]
        assert values == [0, 2, 4]


# ---------------------------------------------------------------------------
# 15. pile[type] / pile[Filter] — getitem query via the Filter primitive
# ---------------------------------------------------------------------------


class TestGetitemFilter:
    def test_bare_type_filters_by_type(self):
        items = [Item(value=i) for i in range(3)]
        others = [OtherItem(name="a"), OtherItem(name="b")]
        p = Pile(collections=items + others)

        only_items = p[Item]
        assert isinstance(only_items, Pile)
        assert len(only_items) == 3
        assert all(isinstance(x, Item) for x in only_items)

        only_others = p[OtherItem]
        assert len(only_others) == 2
        assert all(isinstance(x, OtherItem) for x in only_others)

    def test_bare_type_no_match_returns_empty_pile(self, pile_3):
        result = pile_3[OtherItem]
        assert isinstance(result, Pile)
        assert len(result) == 0

    def test_typefilter_instance(self):
        from lionagi.ln.types import TypeFilter

        items = [Item(value=i) for i in range(3)]
        p = Pile(collections=[*items, OtherItem(name="x")])
        result = p[TypeFilter(Item)]
        assert len(result) == 3

    def test_specfilter_via_fieldref(self, five_items):
        from lionagi.ln.types import Spec

        p = Pile(collections=five_items)
        result = p[Spec(int, name="value").q >= 3]
        assert isinstance(result, Pile)
        assert sorted(x.value for x in result) == [3, 4]

    def test_composed_filter(self, five_items):
        from lionagi.ln.types import Spec, TypeFilter

        p = Pile(collections=[*five_items, OtherItem(name="z")])
        result = p[TypeFilter(Item) & (Spec(int, name="value").q > 2)]
        assert sorted(x.value for x in result) == [3, 4]

    def test_preserves_order(self, five_items):
        from lionagi.ln.types import Spec

        p = Pile(collections=five_items)
        result = p[Spec(int, name="value").q.is_in([4, 0, 2])]
        # original progression order, not the choices order
        assert [x.value for x in result] == [0, 2, 4]
