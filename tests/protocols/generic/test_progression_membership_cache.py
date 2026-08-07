# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Focused regression coverage for Progression's O(1) `_members`-cache-backed
`__contains__` (perf target 1): mutation synchronization, duplicate policy,
slicing, serialization, exceptions, and a timeit ratio proving membership
cost no longer scales with n.
"""

from __future__ import annotations

import timeit
from collections import deque
from uuid import uuid4

import pytest

from lionagi._errors import ItemNotFoundError
from lionagi.protocols.generic.progression import Progression, prog
from lionagi.testing import MockElement


@pytest.fixture
def elements():
    return [MockElement(value=i) for i in range(5)]


@pytest.fixture
def progression(elements):
    return Progression(order=[e.id for e in elements])


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_contains_happy_path_element_and_id(progression, elements):
    for e in elements:
        assert e in progression
        assert e.id in progression
        assert str(e.id) in progression


def test_contains_missing_id_is_false(progression):
    assert uuid4() not in progression


def test_contains_sequence_of_ids(progression, elements):
    assert [elements[0].id, elements[1].id] in progression
    assert [elements[0].id, uuid4()] not in progression


def test_contains_empty_progression():
    assert uuid4() not in Progression()


def test_contains_invalid_candidate_returns_false(progression):
    assert "not-a-uuid" not in progression
    assert object() not in progression


# ---------------------------------------------------------------------------
# Mutation invariants: `_members` (and thus `__contains__`) must reflect
# every public mutation path.
# ---------------------------------------------------------------------------


def test_append_element_updates_membership(progression):
    new = MockElement(value="new")
    progression.append(new)
    assert new in progression


def test_append_ref_sequence_updates_membership(progression):
    new_ids = [uuid4(), uuid4()]
    progression.append(new_ids)
    assert new_ids in progression


def test_include_updates_membership_and_is_idempotent(progression):
    new = MockElement(value="new")
    assert progression.include(new) is True
    assert new in progression
    before = list(progression.order)
    assert progression.include(new) is False
    assert list(progression.order) == before


def test_exclude_updates_membership(progression, elements):
    target = elements[0]
    assert progression.exclude(target) is True
    assert target not in progression


def test_remove_updates_membership(progression, elements):
    target = elements[0]
    progression.remove(target)
    assert target not in progression
    with pytest.raises(ItemNotFoundError):
        progression.remove(target)


def test_pop_removes_membership_when_no_duplicate_remains(progression, elements):
    target = elements[-1]
    uid = progression.pop()
    assert uid == target.id
    assert target not in progression


def test_pop_keeps_membership_when_duplicate_remains(progression, elements):
    dup = elements[0]
    progression.append(dup)  # duplicate id now present twice
    progression.pop()  # removes the trailing duplicate occurrence
    assert dup in progression


def test_popleft_removes_membership(progression, elements):
    target = elements[0]
    uid = progression.popleft()
    assert uid == target.id
    assert target not in progression


def test_clear_empties_membership(progression):
    progression.clear()
    assert len(progression) == 0
    for _ in range(3):
        assert uuid4() not in progression


def test_insert_updates_membership(progression):
    new = uuid4()
    progression.insert(1, new)
    assert new in progression
    assert progression.order[1] == new


def test_setitem_int_updates_membership(progression, elements):
    old = elements[0]
    new = uuid4()
    progression[0] = new
    assert new in progression
    assert old not in progression


def test_setitem_slice_updates_membership(progression, elements):
    new_ids = [uuid4(), uuid4()]
    progression[1:3] = new_ids
    assert new_ids in progression
    assert elements[1] not in progression
    assert elements[2] not in progression


def test_delitem_int_updates_membership(progression, elements):
    target = elements[0]
    del progression[0]
    assert target not in progression


def test_delitem_slice_updates_membership(progression, elements):
    del progression[0:2]
    assert elements[0] not in progression
    assert elements[1] not in progression


def test_extend_updates_membership(progression):
    other = Progression(order=[uuid4(), uuid4()])
    progression.extend(other)
    for uid in other.order:
        assert uid in progression


def test_iadd_updates_membership(progression):
    new = MockElement(value="iadd")
    progression += new
    assert new in progression


def test_isub_updates_membership(progression, elements):
    target = elements[0]
    progression -= target
    assert target not in progression


def test_move_swap_reverse_preserve_membership(progression, elements):
    before = {e.id for e in elements}
    progression.move(0, 2)
    progression.swap(0, 1)
    progression.reverse()
    assert {i for i in progression.order} == before
    for e in elements:
        assert e in progression


# ---------------------------------------------------------------------------
# Duplicate policy
# ---------------------------------------------------------------------------


def test_duplicate_ids_collapse_in_membership_but_not_in_order():
    uid = uuid4()
    p = Progression(order=[uid, uid, uid])
    assert len(p) == 3
    assert p.count(uid) == 3
    assert uid in p
    p._rebuild_members()
    assert p._members == {uid}


def test_construction_with_duplicates_then_single_removal_keeps_membership():
    uid = uuid4()
    other = uuid4()
    p = Progression(order=[uid, other, uid])
    p.remove(uid)  # removes ALL occurrences per contract
    assert uid not in p
    assert other in p


# ---------------------------------------------------------------------------
# Slicing
# ---------------------------------------------------------------------------


def test_getitem_slice_returns_new_synced_progression(progression, elements):
    sliced = progression[1:3]
    assert isinstance(sliced, Progression)
    assert list(sliced.order) == [elements[1].id, elements[2].id]
    assert elements[1] in sliced
    assert elements[0] not in sliced
    # Mutating the slice must not affect the original.
    original_len = len(progression)
    sliced.append(uuid4())
    assert len(progression) == original_len
    assert elements[0] in progression


def test_getitem_empty_slice_raises(progression):
    with pytest.raises(ItemNotFoundError):
        progression[100:200]


def test_getitem_invalid_key_type_raises(progression):
    with pytest.raises(TypeError):
        progression["bad-key"]


# ---------------------------------------------------------------------------
# External direct-`order`-mutation contract (the reason a naive cache-only
# `__contains__` cannot be used, per ranked_targets.md Target 1 risk note).
# ---------------------------------------------------------------------------


def test_direct_order_mutation_is_observed_by_contains():
    p = Progression()
    new_id = uuid4()
    p.order.append(new_id)  # bypasses every Progression method
    assert len(p) == 1
    assert new_id in p  # __contains__ must self-heal via the length check

    p.order.clear()
    assert len(p) == 0
    assert new_id not in p


def test_include_after_direct_order_mutation_does_not_duplicate():
    # Regression: a naive "only __contains__ self-heals" cache design lets
    # `include()` read a stale `_members` set and re-append an id that direct
    # mutation already put in `order`, producing a duplicate.
    p = Progression()
    dup_id = uuid4()
    p.order.append(dup_id)  # bypasses every Progression method
    assert p.include(dup_id) is False
    assert list(p.order).count(dup_id) == 1


def test_include_after_direct_order_clear_reinserts():
    # Regression: after a direct `order.clear()`, a stale `_members` cache
    # would make `include()` think the id is still present and silently
    # refuse to reinsert it.
    p = Progression()
    uid = uuid4()
    p.append(uid)
    p.order.clear()  # bypasses every Progression method
    assert p.include(uid) is True
    assert list(p.order) == [uid]


def test_direct_order_mutation_updates_members_eagerly():
    # `order` is wrapped in an owning deque that keeps `_members` correct on
    # every direct mutation (not just length-changing ones) — see the
    # `_MembersDeque` design rationale in `progression.py`.
    p = Progression()
    new_id = uuid4()
    p.order.append(new_id)
    assert new_id in p._members
    assert p._members == set(p.order)


def test_direct_order_reassignment_leaves_members_stale_until_synced():
    # The one remaining staleness case: `order` replaced wholesale with a
    # plain (unwrapped) deque bypasses the owning-deque's eager updates until
    # the next `_ensure_synced()`-gated call (or an explicit rebuild).
    p = Progression()
    p.order.append(uuid4())
    new_ids = [uuid4(), uuid4()]
    p.order = deque(new_ids)
    assert p._members != set(new_ids)
    p._rebuild_members()
    assert new_ids[0] in p._members
    assert p._members == set(p.order)


# ---------------------------------------------------------------------------
# Length-preserving external mutation (the `_ensure_synced` length-only guard
# cannot see these — the owning `_MembersDeque` must handle them directly).
# ---------------------------------------------------------------------------


def test_direct_order_setitem_length_preserving_membership_both_signs():
    p = Progression()
    a, b = uuid4(), uuid4()
    p.order.extend([a, b])
    c = uuid4()

    p.order[0] = c  # length-preserving external replacement

    assert a not in p  # negative sign: replaced id no longer a member
    assert c in p  # positive sign: replacement id is now a member


def test_include_after_length_preserving_setitem_does_not_duplicate():
    p = Progression()
    a, b = uuid4(), uuid4()
    p.order.extend([a, b])
    c = uuid4()
    p.order[0] = c

    assert p.include(c) is False  # already present; must not re-append
    assert list(p.order).count(c) == 1


def test_remove_succeeds_after_length_preserving_setitem():
    p = Progression()
    a, b = uuid4(), uuid4()
    p.order.extend([a, b])
    c = uuid4()
    p.order[0] = c

    p.remove(c)  # must not raise ItemNotFoundError for a present id
    assert c not in p
    assert list(p.order) == [b]


def test_direct_order_popleft_append_length_preserving_membership():
    p = Progression()
    ids = [uuid4() for _ in range(3)]
    p.order.extend(ids)
    new_id = uuid4()

    popped = p.order.popleft()
    p.order.append(new_id)

    assert popped not in p
    assert new_id in p
    assert p.include(new_id) is False
    p.remove(new_id)
    assert new_id not in p


def test_direct_order_mutator_coverage_keeps_members_consistent():
    # Exercises every mutating deque operation the owning wrapper must own,
    # asserting `_members == set(order)` after each step.
    p = Progression()
    ids = [uuid4() for _ in range(5)]
    p.order.extend(ids)
    assert p._members == set(p.order)

    p.order.appendleft(uuid4())
    assert p._members == set(p.order)

    p.order.rotate(2)
    assert p._members == set(p.order)

    p.order.reverse()
    assert p._members == set(p.order)

    p.order.insert(1, uuid4())
    assert p._members == set(p.order)

    p.order.extendleft([uuid4(), uuid4()])
    assert p._members == set(p.order)

    removed = p.order[0]
    del p.order[0]
    assert removed not in p._members
    assert p._members == set(p.order)

    p.order.remove(p.order[0])
    assert p._members == set(p.order)

    p.order += [uuid4()]
    assert p._members == set(p.order)

    popped = p.order.pop()
    assert popped not in p._members
    assert p._members == set(p.order)

    p.order.clear()
    assert p._members == set() == set(p.order)


def test_progression_method_coverage_keeps_members_consistent_with_order():
    # Companion to test_direct_order_mutator_coverage_keeps_members_consistent
    # above, which drives `p.order` directly. This drives the same ground
    # through Progression's OWN mutating methods instead — append/pop/
    # popleft/insert/__setitem__/extend all delegate to a `self.order` that
    # is, by then, a bound `_MembersDeque`, so the membership set is already
    # correct once that delegation returns. Asserting `_members == set(order)`
    # after each is what would catch the two layers drifting apart: a change
    # to one side's discard rule that isn't mirrored on the other still
    # passes a test that only checks `x in p`, since a set and a deque agree
    # on simple presence long after they've diverged on exactly *which*
    # duplicate survived.
    p = Progression()
    ids = [uuid4() for _ in range(8)]

    p.append(ids[0])
    assert p._members == set(p.order)

    p.append(ids[1:3])
    assert p._members == set(p.order)

    p.insert(1, ids[3])
    assert p._members == set(p.order)

    p.extend(Progression(order=[ids[4], ids[5]]))
    assert p._members == set(p.order)

    replaced = p.order[0]
    p[0] = ids[6]  # length-preserving __setitem__, fresh id, no duplicate
    assert replaced not in p._members
    assert p._members == set(p.order)

    p[0] = ids[7]  # another length-preserving __setitem__, fresh id
    assert p._members == set(p.order)

    popped = p.pop()
    assert popped not in p._members
    assert p._members == set(p.order)

    left = p.popleft()
    assert left not in p._members
    assert p._members == set(p.order)

    # A duplicate id: popping one occurrence must not evict it from the set
    # while the other survives.
    dup = uuid4()
    p.append(dup)
    p.append(dup)
    p.pop()
    assert dup in p._members
    assert p._members == set(p.order)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_to_dict_excludes_private_cache(progression):
    d = progression.to_dict()
    assert "_members" not in d
    assert "_order_len" not in d


def test_model_dump_excludes_private_cache(progression):
    d = progression.model_dump()
    assert "_members" not in d
    assert "_order_len" not in d


def test_from_dict_round_trip_restores_membership(progression, elements):
    d = progression.to_dict()
    restored = Progression.from_dict(d)
    for e in elements:
        assert e in restored
    assert restored._members == {e.id for e in elements}


def test_json_round_trip_restores_membership(progression, elements):
    json_str = progression.model_dump_json()
    restored = Progression.model_validate_json(json_str)
    for e in elements:
        assert e in restored


def test_equality_ignores_private_cache(elements):
    a = Progression(order=[e.id for e in elements], name="x")
    b = Progression(order=[e.id for e in elements], name="x")
    assert a == b
    a.append(MockElement(value="extra"))
    assert a != b


# ---------------------------------------------------------------------------
# Exceptions preserved
# ---------------------------------------------------------------------------


def test_pop_empty_raises_item_not_found():
    with pytest.raises(ItemNotFoundError):
        Progression().pop()


def test_popleft_empty_raises_item_not_found():
    with pytest.raises(ItemNotFoundError):
        Progression().popleft()


def test_remove_missing_raises_item_not_found(progression):
    with pytest.raises(ItemNotFoundError):
        progression.remove(uuid4())


def test_move_out_of_range_raises_item_not_found(progression):
    with pytest.raises(ItemNotFoundError):
        progression.move(0, 100)


def test_factory_prog_preserves_membership():
    ids = [uuid4(), uuid4()]
    p = prog(ids, "named")
    assert p.name == "named"
    for i in ids:
        assert i in p


# ---------------------------------------------------------------------------
# Performance regression: membership cost must not scale with n.
#
# Pre-fix, `__contains__` rebuilt `set(self.order)` on every call, making the
# cost linear in n. Post-fix it is O(1) amortized. A 20x growth in n should
# not produce anywhere near a 20x growth in check time; we assert a generous
# ratio ceiling well below linear scaling to stay robust to CI noise while
# still failing hard if the O(n) behavior regresses.
# ---------------------------------------------------------------------------


def test_contains_cost_does_not_scale_with_size():
    small_n = 2_000
    large_n = 40_000  # 20x larger

    small = Progression(order=[uuid4() for _ in range(small_n)])
    large = Progression(order=[uuid4() for _ in range(large_n)])

    # Miss lookups exercise the full `all(...)` scan path without short-circuiting.
    small_probe = uuid4()
    large_probe = uuid4()

    small_time = timeit.timeit(lambda: small_probe in small, number=5_000)
    large_time = timeit.timeit(lambda: large_probe in large, number=5_000)

    ratio = large_time / small_time if small_time > 0 else float("inf")

    # A purely O(n) implementation would show ~20x; a generous ceiling of 5x
    # comfortably distinguishes O(1)-amortized behavior from O(n) regression
    # while tolerating CI noise.
    assert ratio < 5, (
        f"contains() cost scaled with size (ratio={ratio:.2f}, "
        f"small={small_time:.4f}s, large={large_time:.4f}s) — "
        "expected O(1) amortized membership via the `_members` cache"
    )


def test_contains_cost_flat_across_n_1k_and_n_100k():
    # Mirrors the contract's own timeit probe (baseline_repro.md) at the
    # exact n=1k / n=100k scale, on top of the owning-deque fix — the O(1)
    # win must survive length-preserving-mutation-safety changes.
    small_n = 1_000
    large_n = 100_000  # 100x larger

    small = Progression(order=[uuid4() for _ in range(small_n)])
    large = Progression(order=[uuid4() for _ in range(large_n)])

    small_probe = uuid4()
    large_probe = uuid4()

    small_time = timeit.timeit(lambda: small_probe in small, number=5_000)
    large_time = timeit.timeit(lambda: large_probe in large, number=5_000)

    ratio = large_time / small_time if small_time > 0 else float("inf")

    # A purely O(n) implementation would show ~100x; a generous ceiling of 5x
    # comfortably distinguishes O(1)-amortized behavior from an O(n)
    # regression while tolerating CI noise.
    assert ratio < 5, (
        f"contains() cost scaled with size (ratio={ratio:.2f}, "
        f"n=1k={small_time:.4f}s, n=100k={large_time:.4f}s) — "
        "expected flat per-check cost across n=1k vs n=100k"
    )
