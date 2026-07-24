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
        result = _validate_item_type(["lionagi.protocols.generic.element.Element"])
        assert result == {Element}

    def test_string_type_import_fails_raises_validation_error(self):
        with pytest.raises(ValidationError):
            _validate_item_type(["nonexistent.module.ClassName"])

    def test_bad_string_resolves_error_is_suppressed(self):
        result = _validate_item_type("lionagi.protocols.generic.element.Element")
        assert result is None

    def test_non_type_raises_validation_error(self):
        with pytest.raises(ValidationError):
            _validate_item_type([42])

    def test_duplicate_types_raises_validation_error(self):
        with pytest.raises(ValidationError, match="duplicated"):
            _validate_item_type([Element, Element])

    def test_any_class_is_accepted_conformance_is_checked_at_admission(self):
        """item_type normalizes classes; it does not judge conformance.

        Observable is structural, so conformance belongs to instances, not
        classes: a class assigning ``self.id`` in ``__init__`` declares nothing
        at class level yet its instances conform. Rejecting such classes here
        would contradict the pile's own admission rule, so the check lives at
        admission, where a real object can be inspected.
        """

        class PlainClass:
            pass

        assert _validate_item_type(PlainClass) == {PlainClass}


# ---------------------------------------------------------------------------
# _validate_progression: dict path, duplicates, ID not found
# ---------------------------------------------------------------------------


class TestValidateProgression:
    def test_dict_with_order_key_creates_progression(self):
        a, b = Element(), Element()
        collections = {a.id: a, b.id: b}
        prog_dict = {"order": [str(a.id), str(b.id)]}
        result = _validate_progression(prog_dict, collections)
        assert isinstance(result, Progression)
        assert len(list(result)) == 2

    def test_dict_fallback_when_from_dict_raises(self):
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
        a = Element()
        collections = {a.id: a}
        with pytest.raises(ValueError, match="duplicate"):
            _validate_progression([a.id, a.id], collections)

    def test_id_not_in_collections_same_size_raises(self):
        a, b = Element(), Element()
        fake = Element()  # not in collections
        collections = {a.id: a, b.id: b}
        # 2 IDs but one (fake.id) not in collections
        with pytest.raises(ValueError, match="not found"):
            _validate_progression([a.id, fake.id], collections)

    def test_progression_object_input(self):
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
        a, b = Element(), Element()
        p = Pile(collections=[a, b], order=[a.id, b.id])
        assert len(p) == 2
        assert list(p.progression)[0] == a.id

    def test_validate_before_with_order_key(self):
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
        p = Pile(collections=[Element()], item_type={Element})
        result = p._serialize_item_type({Element})
        assert isinstance(result, list)
        assert any("Element" in s for s in result)

    def test_serialize_item_type_none(self):
        p = Pile(collections=[])
        result = p._serialize_item_type(None)
        assert result is None

    def test_dunder_list(self):
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
        items = [Element() for _ in range(4)]
        p = Pile(collections=items)
        ids_to_keep = {items[0].id, items[2].id}
        result = p[lambda x: x.id in ids_to_keep]
        assert isinstance(result, Pile)
        assert len(result) == 2

    def test_none_key_raises_value_error(self):
        a = Element()
        p = Pile(collections=[a])
        with pytest.raises(ValueError, match="getitem key not provided"):
            p._getitem(None)

    def test_uuid_key_retrieves_item(self):
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        assert p[a.id] is a
        assert p[b.id] is b

    def test_slice_key_converts_progression_to_list(self):
        a, b, c = Element(), Element(), Element()
        p = Pile(collections=[a, b, c])
        result = p[0:2]
        assert isinstance(result, list)
        assert len(result) == 2

    def test_list_key_multiple_items(self):
        a, b, c = Element(), Element(), Element()
        p = Pile(collections=[a, b, c])
        result = p[[a, b]]
        assert isinstance(result, list)
        assert len(result) == 2

    def test_list_key_single_item(self):
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        result = p[[a]]
        assert result is a


# ---------------------------------------------------------------------------
# _setitem: non-int key paths and mismatch errors
# ---------------------------------------------------------------------------


class TestPileSetItem:
    def test_setitem_uuid_key_adds_new_item(self):
        a = Element()
        p = Pile(collections=[a])
        new_item = Element()
        p[new_item.id] = new_item
        assert new_item in p
        assert len(p) == 2

    def test_setitem_list_key_len_mismatch_raises(self):
        a = Element()
        p = Pile(collections=[a])
        new1, new2 = Element(), Element()
        with pytest.raises((KeyError, Exception)):
            p[[new1.id]] = [new1, new2]

    def test_setitem_list_key_id_mismatch_raises(self):
        a = Element()
        p = Pile(collections=[a])
        new1, new2 = Element(), Element()
        with pytest.raises((KeyError, Exception)):
            p[[new1.id]] = [new2]  # new2.id != new1.id

    def test_setitem_nested_list_key_flattens(self):
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
        a, b, c = Element(), Element(), Element()
        p = Pile(collections=[a, b, c])
        result = p._get([a, b])
        assert len(result) == 2

    def test_get_single_in_list(self):
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        result = p.get([a])
        assert result is a

    def test_get_empty_list_raises(self):
        a = Element()
        p = Pile(collections=[a])
        with pytest.raises(ItemNotFoundError):
            p._get([])

    def test_get_missing_with_default(self):
        a = Element()
        p = Pile(collections=[a])
        result = p.get(Element(), "fallback")
        assert result == "fallback"

    def test_get_missing_no_default_raises(self):
        a = Element()
        p = Pile(collections=[a])
        with pytest.raises(ItemNotFoundError):
            p.get(Element())


# ---------------------------------------------------------------------------
# _pop: non-int key paths
# ---------------------------------------------------------------------------


class TestPilePop:
    def test_pop_element_key(self):
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        popped = p.pop(a)
        assert popped is a
        assert len(p) == 1

    def test_pop_multiple_elements(self):
        a, b, c = Element(), Element(), Element()
        p = Pile(collections=[a, b, c])
        result = p.pop([a, b])
        assert len(result) == 2
        assert len(p) == 1

    def test_pop_missing_with_default(self):
        a = Element()
        p = Pile(collections=[a])
        result = p.pop(Element(), "default_val")
        assert result == "default_val"

    def test_pop_missing_no_default_raises(self):
        a = Element()
        p = Pile(collections=[a])
        with pytest.raises(ItemNotFoundError):
            p.pop(Element())

    def test_pop_int_out_of_range_with_default(self):
        a = Element()
        p = Pile(collections=[a])
        result = p._pop(100, "fallback")
        assert result == "fallback"

    def test_pop_empty_list_key_raises(self):
        a = Element()
        p = Pile(collections=[a])
        with pytest.raises(ItemNotFoundError):
            p._pop([])


# ---------------------------------------------------------------------------
# adapt_to / adapt_from
# ---------------------------------------------------------------------------


class TestPileAdapters:
    def test_adapt_to_json(self):
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
        items = [Element() for _ in range(4)]
        p = Pile(collections=items)
        collected = []
        async for item in p:
            collected.append(item)
        assert len(collected) == 4

    @pytest.mark.asyncio
    async def test_anext_called_directly(self):
        a, b = Element(), Element()
        p = Pile(collections=[a, b])
        item = await p.__anext__()
        assert item is a

    @pytest.mark.asyncio
    async def test_anext_stop_async_iteration(self):
        p = Pile(collections=[])
        with pytest.raises(StopAsyncIteration):
            await p.__anext__()

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
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
        from lionagi.protocols.generic.pile import to_list_type

        assert to_list_type(None) == []
