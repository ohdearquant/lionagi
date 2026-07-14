# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coverage tests for lionagi/protocols/generic/pile.py."""

from __future__ import annotations

import importlib
from uuid import UUID

import pytest

from lionagi._errors import ItemNotFoundError
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile, to_list_type

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class Item(Element):
    value: int = 0


class OtherItem(Element):
    name: str = ""


@pytest.mark.parametrize("reference_type", ["uuid", "uuid_string", "element"])
def test_to_list_type_returns_list_for_single_reference(reference_type):
    item = Item(value=1)
    reference = {
        "uuid": item.id,
        "uuid_string": str(item.id),
        "element": item,
    }[reference_type]

    result = to_list_type(reference)

    assert isinstance(result, list)
    assert len(result) == 1
    if reference_type == "element":
        assert result == [item]
    else:
        assert result == [item.id]
        assert isinstance(result[0], UUID)


def test_uuid_string_reference_access_paths():
    item = Item(value=1)
    item_id = str(item.id)

    pile = Pile(collections=[item], order=item_id)
    assert pile[item_id] is item

    inserted = Pile()
    inserted[item_id] = item
    assert inserted[item_id] is item
    inserted.exclude(item_id)
    assert len(inserted) == 0


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


@pytest.mark.asyncio
class TestAsyncEdgeCases:
    async def test_ainclude_adds_item(self):
        p = Pile()
        item = Item(value=1)
        await p.ainclude(item)
        assert item in p

    async def test_ainclude_rejects_wrong_type(self):
        p = Pile(collections=[], item_type={Item})
        with pytest.raises(TypeError):
            await p.ainclude("not_an_item")  # type: ignore[arg-type]

    async def test_aexclude_removes_item(self):
        item = Item(value=5)
        p = Pile(collections=[item])
        await p.aexclude(item)
        assert item not in p

    async def test_aexclude_nonexistent_no_raise(self):
        p = Pile()
        other = Item(value=99)
        await p.aexclude(other)  # should not raise
        # Pile must remain empty and unchanged
        assert len(p) == 0

    async def test_aget_returns_item(self):
        item = Item(value=7)
        p = Pile(collections=[item])
        result = await p.aget(0)
        assert result.value == 7

    async def test_aget_missing_returns_default(self):
        p = Pile()
        sentinel = object()
        result = await p.aget(99, sentinel)
        assert result is sentinel

    async def test_aget_missing_no_default_raises(self):
        p = Pile()
        with pytest.raises(ItemNotFoundError):
            await p.aget(99)

    async def test_async_iteration_order(self, five_items):
        p = Pile(collections=five_items)
        collected = []
        async for item in p:
            collected.append(item.value)
        assert collected == list(range(5))

    async def test_async_iteration_empty(self):
        p = Pile()
        collected = [item async for item in p]
        assert collected == []

    async def test_async_context_manager(self):
        items = [Item(value=i) for i in range(3)]
        p = Pile(collections=items)
        async with p:
            assert len(p) == 3
        assert len(p) == 3  # ctx manager doesn't clear

    async def test_aclear(self):
        p = Pile(collections=[Item(value=i) for i in range(3)])
        await p.aclear()
        assert len(p) == 0

    async def test_aupdate(self):
        p = Pile(collections=[Item(value=0)])
        await p.aupdate([Item(value=1), Item(value=2)])
        assert len(p) == 3

    async def test_apop_by_index(self):
        items = [Item(value=i) for i in range(3)]
        p = Pile(collections=items)
        popped = await p.apop(0)
        assert popped.value == 0
        assert len(p) == 2

    async def test_asetitem(self):
        items = [Item(value=i) for i in range(3)]
        p = Pile(collections=items)
        new = Item(value=99)
        await p.asetitem(0, new)
        assert p[0].value == 99

    async def test_aremove(self):
        item = Item(value=42)
        p = Pile(collections=[item])
        await p.aremove(item)
        assert item not in p


# ---------------------------------------------------------------------------
# 9. from_dict / to_dict serialization roundtrip
# ---------------------------------------------------------------------------


class TestSerializationRoundtrip:
    def test_to_dict_has_required_keys(self, pile_3):
        d = pile_3.to_dict()
        for key in ("id", "created_at", "collections", "progression", "strict_type"):
            assert key in d

    def test_to_dict_collections_is_list(self, pile_3):
        d = pile_3.to_dict()
        assert isinstance(d["collections"], list)
        assert len(d["collections"]) == 3

    def test_from_dict_roundtrip_length(self, pile_3):
        d = pile_3.to_dict()
        p2 = Pile.from_dict(d)
        assert len(p2) == len(pile_3)

    def test_from_dict_roundtrip_order(self, pile_3):
        d = pile_3.to_dict()
        p2 = Pile.from_dict(d)
        original_ids = [str(k) for k in pile_3.keys()]
        restored_ids = [str(k) for k in p2.keys()]
        assert original_ids == restored_ids

    def test_from_dict_roundtrip_strict_type(self):
        items = [Item(value=i) for i in range(3)]
        p = Pile(collections=items, item_type={Item}, strict_type=True)
        d = p.to_dict()
        p2 = Pile.from_dict(d)
        assert p2.strict_type is True

    def test_from_dict_empty_pile(self):
        p = Pile()
        d = p.to_dict()
        p2 = Pile.from_dict(d)
        assert len(p2) == 0

    def test_to_dict_progression_is_dict(self, pile_3):
        d = pile_3.to_dict()
        assert isinstance(d["progression"], dict)


# ---------------------------------------------------------------------------
# 10. is_homogenous
# ---------------------------------------------------------------------------
