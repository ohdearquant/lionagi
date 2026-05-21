# tests/protocols/generic/test_pile_coverage.py
"""Targeted coverage tests for pile.py uncovered lines."""

from __future__ import annotations

import pytest

from lionagi._errors import ItemNotFoundError, ValidationError
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import (
    Pile,
    _validate_item_type,
    _validate_progression,
)
from lionagi.protocols.generic.progression import Progression


class ItemA(Element):
    pass


class ItemB(Element):
    pass


# ---------------------------------------------------------------------------
# _validate_item_type: string resolution and error paths
# ---------------------------------------------------------------------------


class TestValidateItemType:
    def test_string_type_resolves_to_class(self):
        """Lines 73-75: string path successfully resolves."""
        result = _validate_item_type(["lionagi.protocols.generic.element.Element"])
        assert result == {Element}

    def test_string_type_import_fails_raises_validation_error(self):
        """Lines 76-77: string that rspits OK but module doesn't exist raises ValidationError."""
        with pytest.raises(ValidationError):
            _validate_item_type(["nonexistent.module.ClassName"])

    def test_bad_string_resolves_error_is_suppressed(self):
        """String that can't be rsplit('.', 1) returns None (empty value -> no return)."""
        result = _validate_item_type("lionagi.protocols.generic.element.Element")
        assert result is None

    def test_non_type_raises_validation_error(self):
        """Line 98: non-type value raises ValidationError."""
        with pytest.raises(ValidationError):
            _validate_item_type([42])

    def test_duplicate_types_raises_validation_error(self):
        """Line 101: duplicate types in the list raises ValidationError."""
        with pytest.raises(ValidationError, match="duplicated"):
            _validate_item_type([Element, Element])

    def test_non_observable_type_raises_validation_error(self):
        """Line 92: type that is not Observable subclass raises ValidationError."""

        class PlainClass:
            pass

        with pytest.raises(ValidationError):
            _validate_item_type(PlainClass)


# ---------------------------------------------------------------------------
# _validate_progression: dict path, duplicates, ID not found
# ---------------------------------------------------------------------------


class TestValidateProgression:
    def test_dict_with_order_key_creates_progression(self):
        """Lines 116-118 (try path): dict form of progression succeeds."""
        a, b = Element(), Element()
        collections = {a.id: a, b.id: b}
        prog_dict = {"order": [str(a.id), str(b.id)]}
        result = _validate_progression(prog_dict, collections)
        assert isinstance(result, Progression)
        assert len(list(result)) == 2

    def test_dict_fallback_when_from_dict_raises(self):
        """Lines 116-118 (except path): Progression.from_dict raises → fallback to 'order' key."""
        a, b = Element(), Element()
        collections = {a.id: a, b.id: b}
        # 'id' field is bad → Progression.from_dict raises ValidationError
        # fallback uses dict.get('order', []) which has valid IDs
        bad_id_dict = {
            "order": [str(a.id), str(b.id)],
            "id": "not-a-valid-uuid",
        }
        result = _validate_progression(bad_id_dict, collections)
        assert isinstance(result, Progression)
        assert len(list(result)) == 2

    def test_duplicate_ids_in_order_raises_value_error(self):
        """Line 127: duplicate IDs in the order list raises ValueError."""
        a = Element()
        collections = {a.id: a}
        with pytest.raises(ValueError, match="duplicate"):
            _validate_progression([a.id, a.id], collections)

    def test_id_not_in_collections_same_size_raises(self):
        """Line 135: same-size order but with unknown ID raises ValueError."""
        a, b = Element(), Element()
        fake = Element()  # not in collections
        collections = {a.id: a, b.id: b}
        # 2 IDs but one (fake.id) not in collections
        with pytest.raises(ValueError, match="not found"):
            _validate_progression([a.id, fake.id], collections)

    def test_progression_object_input(self):
        """Lines 119-121: Progression object input extracts order."""
        a, b = Element(), Element()
        collections = {a.id: a, b.id: b}
        prog = Progression(order=[a.id, b.id])
        result = _validate_progression(prog, collections)
        assert isinstance(result, Progression)


# ---------------------------------------------------------------------------
# Pile with order kwarg, item_type serialization
# ---------------------------------------------------------------------------


class TestPileInit:
    def test_init_with_order_kwarg(self):
        """Line 240: Pile with explicit 'order' kwarg uses _validate_progression."""
        a, b = Element(), Element()
        p = Pile(collections=[a, b], order=[a.id, b.id])
        assert len(p) == 2
        assert list(p.progression)[0] == a.id

    def test_validate_before_with_order_key(self):
        """Line 240: _validate_before called directly with 'order' key triggers that branch."""
        a, b = Element(), Element()
        result = Pile._validate_before(
            {
                "collections": [a, b],
                "order": [a.id, b.id],
            }
        )
        assert "progression" in result
        assert isinstance(result["progression"], Progression)

    def test_serialize_item_type_non_none(self):
        """Lines 291-293: _serialize_item_type returns list when item_type is set."""
        p = Pile(collections=[Element()], item_type={Element})
        result = p._serialize_item_type({Element})
        assert isinstance(result, list)
        assert any("Element" in s for s in result)

    def test_serialize_item_type_none(self):
        """Lines 291: _serialize_item_type returns None for None value."""
        p = Pile(collections=[])
        result = p._serialize_item_type(None)
        assert result is None

    def test_dunder_list(self):
        """Line 543: __list__ returns list of items."""
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        lst = p.__list__()
        assert isinstance(lst, list)
        assert len(lst) == 2


# ---------------------------------------------------------------------------
# _getitem: callable key, UUID key, list key edge cases
# ---------------------------------------------------------------------------


class TestPileGetItem:
    def test_callable_key_filters_items(self):
        """Lines 804-805: callable key uses _filter_by_function."""
        items = [Element() for _ in range(4)]
        p = Pile(collections=items)
        ids_to_keep = {items[0].id, items[2].id}
        result = p[lambda x: x.id in ids_to_keep]
        assert isinstance(result, Pile)
        assert len(result) == 2

    def test_none_key_raises_value_error(self):
        """Line 802: None key raises ValueError immediately."""
        a = Element()
        p = Pile(collections=[a])
        with pytest.raises(ValueError, match="getitem key not provided"):
            p._getitem(None)

    def test_uuid_key_retrieves_item(self):
        """Line 821: UUID key directly retrieves from collections."""
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        assert p[a.id] is a
        assert p[b.id] is b

    def test_slice_key_converts_progression_to_list(self):
        """Line 811: slice key → result_ids is Progression → converted to list."""
        a, b, c = Element(), Element(), Element()
        p = Pile(collections=[a, b, c])
        result = p[0:2]
        assert isinstance(result, list)
        assert len(result) == 2

    def test_list_key_multiple_items(self):
        """Lines 828-840: list key returns list of items."""
        a, b, c = Element(), Element(), Element()
        p = Pile(collections=[a, b, c])
        result = p[[a, b]]
        assert isinstance(result, list)
        assert len(result) == 2

    def test_list_key_single_item(self):
        """Lines 828-840: single-element list key returns item directly."""
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        result = p[[a]]
        assert result is a


# ---------------------------------------------------------------------------
# _setitem: non-int key paths and mismatch errors
# ---------------------------------------------------------------------------


class TestPileSetItem:
    def test_setitem_uuid_key_adds_new_item(self):
        """Lines 868-883: non-int key path appends to progression."""
        a = Element()
        p = Pile(collections=[a])
        new_item = Element()
        p[new_item.id] = new_item
        assert new_item in p
        assert len(p) == 2

    def test_setitem_list_key_len_mismatch_raises(self):
        """Line 873: key length != item count raises KeyError."""
        a = Element()
        p = Pile(collections=[a])
        new1, new2 = Element(), Element()
        with pytest.raises((KeyError, Exception)):
            p[[new1.id]] = [new1, new2]

    def test_setitem_list_key_id_mismatch_raises(self):
        """Line 879: key ID not matching item ID raises KeyError."""
        a = Element()
        p = Pile(collections=[a])
        new1, new2 = Element(), Element()
        with pytest.raises((KeyError, Exception)):
            p[[new1.id]] = [new2]  # new2.id != new1.id

    def test_setitem_nested_list_key_flattens(self):
        """Line 871: nested list key is flattened via to_list."""
        a = Element()
        p = Pile(collections=[a])
        new = Element()
        # Nested list key should be flattened: [[new.id]] → [new.id]
        p[[[new.id]]] = [new]
        assert new in p


# ---------------------------------------------------------------------------
# _get: list of non-int keys
# ---------------------------------------------------------------------------


class TestPileGet:
    def test_get_list_of_elements_multi(self):
        """Line 911: _get with list of 2+ elements returns list."""
        a, b, c = Element(), Element(), Element()
        p = Pile(collections=[a, b, c])
        result = p._get([a, b])
        assert len(result) == 2

    def test_get_single_in_list(self):
        """Line 910: single-element result returns item directly."""
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        result = p.get([a])
        assert result is a

    def test_get_empty_list_raises(self):
        """Line 908: empty key list → empty result → raises ItemNotFoundError."""
        a = Element()
        p = Pile(collections=[a])
        with pytest.raises(ItemNotFoundError):
            p._get([])

    def test_get_missing_with_default(self):
        """Line 916: missing key with default returns default."""
        a = Element()
        p = Pile(collections=[a])
        result = p.get(Element(), "fallback")
        assert result == "fallback"

    def test_get_missing_no_default_raises(self):
        """Line 915: missing key without default raises ItemNotFoundError."""
        a = Element()
        p = Pile(collections=[a])
        with pytest.raises(ItemNotFoundError):
            p.get(Element())


# ---------------------------------------------------------------------------
# _pop: non-int key paths
# ---------------------------------------------------------------------------


class TestPilePop:
    def test_pop_element_key(self):
        """Lines 938-952: pop with element key."""
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        popped = p.pop(a)
        assert popped is a
        assert len(p) == 1

    def test_pop_multiple_elements(self):
        """Lines 938-948: pop with list of elements returns list."""
        a, b, c = Element(), Element(), Element()
        p = Pile(collections=[a, b, c])
        result = p.pop([a, b])
        assert len(result) == 2
        assert len(p) == 1

    def test_pop_missing_with_default(self):
        """Line 952: missing element with default returns default."""
        a = Element()
        p = Pile(collections=[a])
        result = p.pop(Element(), "default_val")
        assert result == "default_val"

    def test_pop_missing_no_default_raises(self):
        """Line 951: missing element without default raises ItemNotFoundError."""
        a = Element()
        p = Pile(collections=[a])
        with pytest.raises(ItemNotFoundError):
            p.pop(Element())

    def test_pop_int_out_of_range_with_default(self):
        """Line 938: int out of range with default returns default."""
        a = Element()
        p = Pile(collections=[a])
        result = p._pop(100, "fallback")
        assert result == "fallback"

    def test_pop_empty_list_key_raises(self):
        """Line 945: empty list key → empty result → raises ItemNotFoundError."""
        a = Element()
        p = Pile(collections=[a])
        with pytest.raises(ItemNotFoundError):
            p._pop([])


# ---------------------------------------------------------------------------
# adapt_to / adapt_from
# ---------------------------------------------------------------------------


class TestPileAdapters:
    def test_adapt_to_json(self):
        """Lines 1028-1029: adapt_to passes adapt_meth kwarg."""
        a = Element()
        p = Pile(collections=[a])
        result = p.adapt_to("json")
        assert isinstance(result, str)
        assert "collections" in result


# ---------------------------------------------------------------------------
# Async iteration
# ---------------------------------------------------------------------------


class TestPileAsyncIteration:
    @pytest.mark.asyncio
    async def test_aiter_yields_all_items(self):
        """Lines 753-759: __aiter__ yields all items via async for."""
        items = [Element() for _ in range(4)]
        p = Pile(collections=items)
        collected = []
        async for item in p:
            collected.append(item)
        assert len(collected) == 4

    @pytest.mark.asyncio
    async def test_anext_called_directly(self):
        """Lines 763-764: __anext__ called directly returns first item."""
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        item = await p.__anext__()
        assert item is a

    @pytest.mark.asyncio
    async def test_anext_stop_async_iteration(self):
        """Lines 765-766: __anext__ on empty pile raises StopAsyncIteration."""
        p = Pile(collections=[])
        with pytest.raises(StopAsyncIteration):
            await p.__anext__()

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        """Lines 985-997: async context manager acquires/releases lock."""
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        async with p as pp:
            assert pp is p
            assert len(pp) == 2


# ---------------------------------------------------------------------------
# to_list_type: None input (module-level function in pile.py)
# ---------------------------------------------------------------------------


class TestToListType:
    def test_to_list_type_none_returns_empty_list(self):
        """Line 1175: to_list_type(None) returns []."""
        from lionagi.protocols.generic.pile import to_list_type

        assert to_list_type(None) == []
