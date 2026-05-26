# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Enhanced tests for Progression covering PR1 (_members sync) and PR2 (move/swap/reverse)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import Field

from lionagi._errors import ItemNotFoundError
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.progression import Progression
from lionagi.testing import MockElement

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def elems():
    """Five distinct MockElements for general use."""
    return [MockElement(value=i) for i in range(5)]


@pytest.fixture
def prog(elems):
    """Progression seeded with the five element IDs."""
    return Progression(order=[e.id for e in elems])


@pytest.fixture
def empty_prog():
    """An empty Progression."""
    return Progression()


@pytest.fixture
def single_prog():
    """A Progression with exactly one element."""
    e = MockElement(value="only")
    return Progression(order=[e.id]), e


# ===================================================================
# PR1: _members initialization and synchronization
# ===================================================================


class TestMembersInitialization:
    """_members is built from order during model_post_init."""

    def test_members_initialized_from_order(self, prog, elems):
        """_members should contain exactly the IDs present in order."""
        assert prog._members == {e.id for e in elems}

    def test_members_empty_on_empty_progression(self, empty_prog):
        assert empty_prog._members == set()

    def test_members_initialized_from_elements(self):
        """When order is given as Element objects, _members still syncs."""
        elements = [MockElement(value=i) for i in range(3)]
        p = Progression(order=elements)
        assert p._members == {e.id for e in elements}

    def test_members_with_duplicate_ids(self):
        """Duplicates in order are allowed; _members is a set (unique)."""
        e = MockElement(value=0)
        p = Progression(order=[e.id, e.id, e.id])
        assert len(p.order) == 3
        assert len(p._members) == 1
        assert e.id in p._members


class TestMembersSyncAfterMutations:
    """After every mutation, _members must reflect the current order contents."""

    def test_append_single_element(self, prog, elems):
        new = MockElement(value="appended")
        prog.append(new)
        assert new.id in prog._members
        assert prog._members == set(prog.order)

    def test_append_multiple(self, prog):
        new_elems = [MockElement(value=i) for i in range(10, 13)]
        prog.append(new_elems)
        for e in new_elems:
            assert e.id in prog._members
        assert prog._members == set(prog.order)

    def test_include_new_item(self, prog):
        new = MockElement(value="inc")
        prog.include(new)
        assert new.id in prog._members
        assert prog._members == set(prog.order)

    def test_include_existing_item_no_duplicate(self, prog, elems):
        original_len = len(prog)
        prog.include(elems[0])
        assert len(prog) == original_len  # not added again
        assert prog._members == set(prog.order)

    def test_exclude_removes_from_members(self, prog, elems):
        removed = elems[2]
        prog.exclude(removed)
        assert removed.id not in prog._members
        assert prog._members == set(prog.order)

    def test_pop_default_removes_from_members(self, prog):
        popped = prog.pop()
        assert popped not in prog._members
        assert prog._members == set(prog.order)

    def test_pop_index_removes_from_members(self, prog):
        popped = prog.pop(0)
        assert popped not in prog._members
        assert prog._members == set(prog.order)

    def test_pop_duplicate_id_only_removes_if_no_remaining(self):
        """If a UUID appears twice, popping one should keep it in _members."""
        e = MockElement(value="dup")
        p = Progression(order=[e.id, e.id])
        p.pop()
        assert e.id in p._members  # still one remaining
        assert p._members == set(p.order)

    def test_popleft_removes_from_members(self, prog, elems):
        popped = prog.popleft()
        assert popped == elems[0].id
        assert popped not in prog._members
        assert prog._members == set(prog.order)

    def test_remove_updates_members(self, prog, elems):
        target = elems[3]
        prog.remove(target)
        assert target.id not in prog._members
        assert prog._members == set(prog.order)

    def test_clear_empties_members(self, prog):
        prog.clear()
        assert len(prog._members) == 0
        assert len(prog.order) == 0

    def test_insert_adds_to_members(self, prog):
        new = MockElement(value="inserted")
        prog.insert(2, new)
        assert new.id in prog._members
        assert prog._members == set(prog.order)

    def test_setitem_int_updates_members(self, prog, elems):
        old_id = prog[0]
        new = MockElement(value="replaced")
        prog[0] = new
        assert new.id in prog._members
        # old_id should be gone if it appeared only once at index 0
        if old_id not in prog.order:
            assert old_id not in prog._members
        assert prog._members == set(prog.order)

    def test_setitem_slice_updates_members(self, prog, elems):
        replacements = [MockElement(value="r0"), MockElement(value="r1")]
        old_ids = [prog[0], prog[1]]
        prog[0:2] = replacements
        for r in replacements:
            assert r.id in prog._members
        for old_id in old_ids:
            if old_id not in prog.order:
                assert old_id not in prog._members
        assert prog._members == set(prog.order)

    def test_delitem_int_updates_members(self, prog, elems):
        target_id = prog[1]
        del prog[1]
        if target_id not in prog.order:
            assert target_id not in prog._members
        assert prog._members == set(prog.order)

    def test_delitem_slice_updates_members(self, prog, elems):
        del prog[0:3]
        assert prog._members == set(prog.order)
        assert len(prog) == 2

    def test_extend_updates_members(self, prog):
        other = Progression(order=[MockElement(value=i) for i in range(3)])
        prog.extend(other)
        for uid in other.order:
            assert uid in prog._members
        assert prog._members == set(prog.order)


class TestContainsUsesMembers:
    """__contains__ should use O(1) _members lookup."""

    def test_contains_element(self, prog, elems):
        for e in elems:
            assert e in prog

    def test_contains_uuid(self, prog, elems):
        for e in elems:
            assert e.id in prog

    def test_contains_uuid_string(self, prog, elems):
        for e in elems:
            assert str(e.id) in prog

    def test_not_contains_random_uuid(self, prog):
        assert uuid4() not in prog

    def test_not_contains_invalid_string(self, prog):
        assert "not-a-uuid" not in prog

    def test_contains_after_removal(self, prog, elems):
        target = elems[0]
        prog.remove(target)
        assert target not in prog
        assert target.id not in prog

    def test_contains_multiple_items(self, prog, elems):
        # validate_order handles sequences, so [id1, id2] checks all
        assert [elems[0].id, elems[1].id] in prog

    def test_contains_multiple_items_one_missing(self, prog, elems):
        assert [elems[0].id, uuid4()] not in prog


class TestRebuildMembers:
    """_rebuild_members reconstructs _members from order."""

    def test_rebuild_after_manual_order_mutation(self, prog, elems):
        # Directly mutate order (bypassing normal API) to test _rebuild
        new_id = uuid4()
        prog.order.append(new_id)
        # _members is now stale
        assert new_id not in prog._members
        prog._rebuild_members()
        assert new_id in prog._members
        assert prog._members == set(prog.order)

    def test_rebuild_on_empty(self, empty_prog):
        empty_prog._rebuild_members()
        assert empty_prog._members == set()


class TestSerializationRoundTrip:
    """_members is private (not serialized) but must be correct after deserialization."""

    def test_to_dict_excludes_members(self, prog):
        d = prog.to_dict()
        assert "_members" not in d

    def test_model_dump_excludes_members(self, prog):
        d = prog.model_dump()
        assert "_members" not in d

    def test_from_dict_restores_members(self, prog, elems):
        d = prog.to_dict()
        restored = Progression.from_dict(d)
        assert restored._members == {e.id for e in elems}
        for e in elems:
            assert e in restored

    def test_json_round_trip_restores_members(self, prog, elems):
        json_str = prog.model_dump_json()
        restored = Progression.from_dict(json.loads(json_str))
        assert restored._members == {e.id for e in elems}
        assert len(restored) == len(elems)

    def test_contains_works_after_deserialization(self, prog, elems):
        d = prog.to_dict()
        restored = Progression.from_dict(d)
        for e in elems:
            assert e in restored
        assert uuid4() not in restored


class TestMembersThreadSafety:
    """Verify _members stays consistent under concurrent async operations."""

    def test_concurrent_append_and_contains(self):
        p = Progression()
        added_ids: list[UUID] = []

        async def appender():
            for _ in range(200):
                e = MockElement(value="a")
                p.append(e)
                added_ids.append(e.id)
                await asyncio.sleep(0)

        async def checker():
            for _ in range(200):
                # Members should always be a superset of what's in order
                for uid in list(p.order):
                    assert uid in p._members
                await asyncio.sleep(0)

        async def run():
            await asyncio.gather(appender(), checker())

        asyncio.run(run())
        assert p._members == set(p.order)

    def test_concurrent_append_and_pop(self):
        p = Progression()

        async def appender():
            for _ in range(100):
                p.append(MockElement(value="x"))
                await asyncio.sleep(0)

        async def popper():
            for _ in range(50):
                if p:
                    p.pop()
                await asyncio.sleep(0)

        async def run():
            await asyncio.gather(appender(), popper())

        asyncio.run(run())
        assert p._members == set(p.order)
        assert 50 <= len(p) <= 100


# ===================================================================
# PR2: _validate_index, move, swap, reverse
# ===================================================================


class TestValidateIndex:
    """_validate_index normalizes negative indices and checks bounds."""

    def test_positive_index(self, prog):
        assert prog._validate_index(0) == 0
        assert prog._validate_index(4) == 4

    def test_negative_index(self, prog):
        assert prog._validate_index(-1) == 4
        assert prog._validate_index(-5) == 0

    def test_out_of_bounds_positive(self, prog):
        with pytest.raises(ItemNotFoundError):
            prog._validate_index(5)

    def test_out_of_bounds_negative(self, prog):
        with pytest.raises(ItemNotFoundError):
            prog._validate_index(-6)

    def test_empty_progression(self, empty_prog):
        with pytest.raises(ItemNotFoundError, match="empty"):
            empty_prog._validate_index(0)

    def test_allow_end_true(self, prog):
        # With allow_end, index == len(order) is valid
        assert prog._validate_index(5, allow_end=True) == 5

    def test_allow_end_still_rejects_beyond(self, prog):
        with pytest.raises(ItemNotFoundError):
            prog._validate_index(6, allow_end=True)

    def test_allow_end_on_empty(self, empty_prog):
        # allow_end on empty means index 0 is valid (for insertion)
        assert empty_prog._validate_index(0, allow_end=True) == 0

    def test_allow_end_rejects_positive_on_empty(self, empty_prog):
        with pytest.raises(ItemNotFoundError):
            empty_prog._validate_index(1, allow_end=True)


class TestMove:
    """move() relocates an item from one position to another."""

    def test_move_basic(self, prog, elems):
        ids = [e.id for e in elems]
        # Move index 0 to index 2
        prog.move(0, 2)
        assert prog[0] == ids[1]
        assert prog[1] == ids[0]
        assert prog[2] == ids[2]

    def test_move_first_to_last(self, prog, elems):
        first_id = prog[0]
        # to_index uses allow_end=True, so len(prog) means "after last"
        prog.move(0, len(prog))
        assert prog[-1] == first_id

    def test_move_last_to_first(self, prog, elems):
        last_id = prog[-1]
        prog.move(-1, 0)
        assert prog[0] == last_id

    def test_move_negative_indices(self, prog, elems):
        ids_before = list(prog.order)
        # Move last item to second position
        prog.move(-1, 1)
        assert prog[1] == ids_before[-1]

    def test_move_same_position_is_noop(self, prog, elems):
        ids_before = list(prog.order)
        prog.move(2, 2)
        assert list(prog.order) == ids_before

    def test_move_out_of_bounds_from(self, prog):
        with pytest.raises(ItemNotFoundError):
            prog.move(10, 0)

    def test_move_out_of_bounds_to(self, prog):
        with pytest.raises(ItemNotFoundError):
            prog.move(0, 10)

    def test_move_preserves_length(self, prog):
        original_len = len(prog)
        prog.move(0, 3)
        assert len(prog) == original_len

    def test_move_does_not_change_members(self, prog, elems):
        members_before = set(prog._members)
        prog.move(0, 3)
        assert prog._members == members_before

    def test_move_on_two_element_progression(self):
        e1, e2 = MockElement(value=1), MockElement(value=2)
        p = Progression(order=[e1.id, e2.id])
        # move(0, 1) with the adjustment logic is a no-op for adjacent forward
        # move(0, 2) uses allow_end and places e1 at the end
        p.move(0, 2)
        assert p[0] == e2.id
        assert p[1] == e1.id

    def test_move_adjacent_forward(self, prog, elems):
        ids = [e.id for e in elems]
        prog.move(1, 2)
        assert prog[1] == ids[1]  # moved to same spot when adjacent

    def test_move_adjacent_backward(self, prog, elems):
        ids = [e.id for e in elems]
        prog.move(2, 1)
        assert prog[1] == ids[2]
        assert prog[2] == ids[1]


class TestSwap:
    """swap() exchanges two items at given positions."""

    def test_swap_basic(self, prog, elems):
        id_at_0 = prog[0]
        id_at_4 = prog[4]
        prog.swap(0, 4)
        assert prog[0] == id_at_4
        assert prog[4] == id_at_0

    def test_swap_same_index_noop(self, prog, elems):
        ids_before = list(prog.order)
        prog.swap(2, 2)
        assert list(prog.order) == ids_before

    def test_swap_negative_indices(self, prog, elems):
        id_first = prog[0]
        id_last = prog[-1]
        prog.swap(0, -1)
        assert prog[0] == id_last
        assert prog[-1] == id_first

    def test_swap_out_of_bounds(self, prog):
        with pytest.raises(ItemNotFoundError):
            prog.swap(0, 10)

    def test_swap_preserves_length(self, prog):
        original_len = len(prog)
        prog.swap(1, 3)
        assert len(prog) == original_len

    def test_swap_does_not_change_members(self, prog, elems):
        members_before = set(prog._members)
        prog.swap(0, 4)
        assert prog._members == members_before

    def test_swap_both_negative(self, prog, elems):
        id_m1 = prog[-1]
        id_m2 = prog[-2]
        prog.swap(-1, -2)
        assert prog[-1] == id_m2
        assert prog[-2] == id_m1

    def test_swap_adjacent(self, prog, elems):
        id_at_1 = prog[1]
        id_at_2 = prog[2]
        prog.swap(1, 2)
        assert prog[1] == id_at_2
        assert prog[2] == id_at_1

    def test_swap_empty_raises(self, empty_prog):
        with pytest.raises(ItemNotFoundError):
            empty_prog.swap(0, 1)


class TestReverse:
    """reverse() reverses the order in-place."""

    def test_reverse_basic(self, prog, elems):
        ids_before = list(prog.order)
        prog.reverse()
        assert list(prog.order) == list(reversed(ids_before))

    def test_reverse_empty(self, empty_prog):
        empty_prog.reverse()
        assert len(empty_prog) == 0

    def test_reverse_single_item(self, single_prog):
        p, e = single_prog
        p.reverse()
        assert p[0] == e.id
        assert len(p) == 1

    def test_reverse_preserves_members(self, prog, elems):
        members_before = set(prog._members)
        prog.reverse()
        assert prog._members == members_before

    def test_reverse_twice_restores_order(self, prog, elems):
        ids_before = list(prog.order)
        prog.reverse()
        prog.reverse()
        assert list(prog.order) == ids_before

    def test_reverse_does_not_change_length(self, prog):
        original_len = len(prog)
        prog.reverse()
        assert len(prog) == original_len

    def test_reverse_contains_still_works(self, prog, elems):
        prog.reverse()
        for e in elems:
            assert e in prog


class TestMembersUnaffectedByReorderOps:
    """move, swap, and reverse do not change set membership."""

    def test_move_members_unchanged(self, prog, elems):
        expected = {e.id for e in elems}
        prog.move(0, 4)
        assert prog._members == expected

    def test_swap_members_unchanged(self, prog, elems):
        expected = {e.id for e in elems}
        prog.swap(1, 3)
        assert prog._members == expected

    def test_reverse_members_unchanged(self, prog, elems):
        expected = {e.id for e in elems}
        prog.reverse()
        assert prog._members == expected

    def test_chained_operations_members_consistent(self, prog, elems):
        """Sequence of move, swap, reverse should keep _members consistent."""
        expected = {e.id for e in elems}
        prog.move(0, 3)
        prog.swap(1, 4)
        prog.reverse()
        prog.move(2, 0)
        prog.swap(0, -1)
        assert prog._members == expected
        assert len(prog) == 5


# ===================================================================
# Integration: combined PR1 + PR2 scenarios
# ===================================================================


class TestIntegrationScenarios:
    """End-to-end scenarios mixing membership mutations and reorder operations."""

    def test_append_then_move_then_contains(self, prog, elems):
        new = MockElement(value="new")
        prog.append(new)
        prog.move(-1, 0)
        assert prog[0] == new.id
        assert new in prog
        assert prog._members == set(prog.order)

    def test_remove_then_swap(self, prog, elems):
        prog.remove(elems[2])
        assert len(prog) == 4
        prog.swap(0, -1)
        assert prog._members == set(prog.order)
        assert elems[2] not in prog

    def test_reverse_then_pop(self, prog, elems):
        prog.reverse()
        popped = prog.pop()
        # After reverse, last item is elems[0].id
        assert popped == elems[0].id
        assert popped not in prog._members
        assert prog._members == set(prog.order)

    def test_clear_then_append_then_move(self, empty_prog):
        elements = [MockElement(value=i) for i in range(4)]
        for e in elements:
            empty_prog.append(e)
        empty_prog.move(0, 3)
        assert empty_prog._members == {e.id for e in elements}
        assert len(empty_prog) == 4

    def test_serialize_after_reorder_operations(self, prog, elems):
        prog.swap(0, 4)
        prog.reverse()
        prog.move(1, 3)
        d = prog.to_dict()
        restored = Progression.from_dict(d)
        assert list(restored.order) == list(prog.order)
        assert restored._members == set(prog.order)
        for e in elems:
            assert e in restored

    def test_include_idempotent_after_swap(self, prog, elems):
        prog.swap(0, 2)
        # Include an already-present element
        result = prog.include(elems[0])
        assert result is False  # already present, nothing appended
        assert len(prog) == 5

    def test_exclude_after_move(self, prog, elems):
        prog.move(0, 4)
        prog.exclude(elems[0])
        assert elems[0] not in prog
        assert len(prog) == 4
        assert prog._members == set(prog.order)

    def test_setitem_after_reverse(self, prog, elems):
        prog.reverse()
        new = MockElement(value="replaced")
        prog[0] = new
        assert prog[0] == new.id
        assert new.id in prog._members
        assert prog._members == set(prog.order)

    def test_delitem_after_swap(self, prog, elems):
        prog.swap(1, 3)
        del prog[1]
        assert len(prog) == 4
        assert prog._members == set(prog.order)

    def test_extend_then_reverse_then_pop(self, prog):
        other = Progression(order=[MockElement(value=i) for i in range(3)])
        prog.extend(other)
        assert len(prog) == 8
        prog.reverse()
        prog.pop()
        assert len(prog) == 7
        assert prog._members == set(prog.order)
