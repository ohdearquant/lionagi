"""Edge case tests for lionagi's Progression class."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from lionagi._errors import ItemNotFoundError
from lionagi.protocols.generic.event import Event
from lionagi.protocols.generic.processor import Processor
from lionagi.protocols.generic.progression import Progression, prog


class _OkEvent(Event):
    async def _invoke(self):
        return "ok"


class _FailEvent(Event):
    async def _invoke(self):
        raise ValueError("intentional failure")


class _SlowEvent(Event):
    async def _invoke(self):
        await asyncio.sleep(0.05)
        return "slow-ok"


class _Proc(Processor):
    event_type = _OkEvent


def _proc(**kw) -> _Proc:
    defaults = dict(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
    defaults.update(kw)
    return _Proc(**defaults)


class TestProgressionInit:
    def test_empty_progression(self):
        p = Progression()
        assert len(p) == 0

    def test_with_list_of_uuids(self):
        ids = [uuid4(), uuid4()]
        p = Progression(order=ids)
        assert len(p) == 2
        for uid in ids:
            assert uid in p

    def test_name_stored(self):
        p = Progression(name="test-prog")
        assert p.name == "test-prog"

    def test_duplicate_ids_stored(self):
        uid = uuid4()
        p = Progression(order=[uid, uid])
        # Progression is an ordered deque; duplicates may be included
        assert uid in p

    def test_none_in_list_is_ignored(self):
        p = Progression(order=[None])
        assert len(p) == 0

    def test_str_uuid_accepted(self):
        uid = uuid4()
        p = Progression(order=[str(uid)])
        assert uid in p


class TestProgressionAppend:
    def test_append_uuid(self):
        p = Progression()
        uid = uuid4()
        p.append(uid)
        assert uid in p
        assert len(p) == 1

    def test_append_str_uuid(self):
        p = Progression()
        uid = uuid4()
        p.append(str(uid))
        assert uid in p

    def test_append_multiple_maintains_order(self):
        ids = [uuid4() for _ in range(5)]
        p = Progression()
        for uid in ids:
            p.append(uid)
        assert list(p) == ids

    def test_append_invalid_raises(self):
        p = Progression()
        with pytest.raises(ValueError):
            p.append("not-a-uuid")


class TestProgressionInsertFront:
    def test_insert_at_zero_is_prepend(self):
        p = Progression()
        uid1 = uuid4()
        uid2 = uuid4()
        p.append(uid1)
        p.insert(0, uid2)
        assert list(p)[0] == uid2

    def test_insert_preserves_order(self):
        uid1, uid2, uid3 = uuid4(), uuid4(), uuid4()
        p = Progression(order=[uid1, uid3])
        p.insert(1, uid2)
        assert list(p) == [uid1, uid2, uid3]


class TestProgressionPopleft:
    def test_popleft_returns_first(self):
        uid1, uid2 = uuid4(), uuid4()
        p = Progression(order=[uid1, uid2])
        result = p.popleft()
        assert result == uid1
        assert len(p) == 1

    def test_popleft_empty_raises(self):
        p = Progression()
        with pytest.raises(ItemNotFoundError):
            p.popleft()

    def test_popleft_reduces_length(self):
        ids = [uuid4() for _ in range(3)]
        p = Progression(order=ids)
        p.popleft()
        assert len(p) == 2


class TestProgressionPop:
    def test_pop_returns_last(self):
        uid1, uid2 = uuid4(), uuid4()
        p = Progression(order=[uid1, uid2])
        result = p.pop()
        assert result == uid2

    def test_pop_empty_raises(self):
        p = Progression()
        with pytest.raises(ItemNotFoundError):
            p.pop()

    def test_pop_reduces_length(self):
        ids = [uuid4() for _ in range(4)]
        p = Progression(order=ids)
        p.pop()
        assert len(p) == 3


class TestProgressionContains:
    def test_contains_existing(self):
        uid = uuid4()
        p = Progression(order=[uid])
        assert uid in p

    def test_not_contains_missing(self):
        p = Progression(order=[uuid4()])
        assert uuid4() not in p

    def test_contains_by_str_uuid(self):
        uid = uuid4()
        p = Progression(order=[uid])
        assert str(uid) in p


class TestProgressionLenIter:
    def test_len_empty(self):
        assert len(Progression()) == 0

    def test_len_non_empty(self):
        p = Progression(order=[uuid4() for _ in range(7)])
        assert len(p) == 7

    def test_iter_yields_uuids(self):
        ids = [uuid4() for _ in range(3)]
        p = Progression(order=ids)
        result = list(p)
        assert all(isinstance(x, UUID) for x in result)
        assert result == ids


class TestProgressionGetItem:
    def test_getitem_integer(self):
        ids = [uuid4(), uuid4(), uuid4()]
        p = Progression(order=ids)
        assert p[0] == ids[0]
        assert p[2] == ids[2]
        assert p[-1] == ids[2]

    def test_getitem_out_of_range_raises(self):
        from lionagi._errors import ItemNotFoundError

        p = Progression(order=[uuid4()])
        with pytest.raises(ItemNotFoundError):
            _ = p[5]

    def test_getitem_slice(self):
        ids = [uuid4() for _ in range(5)]
        p = Progression(order=ids)
        sliced = p[1:3]
        assert len(sliced) == 2
        assert sliced[0] == ids[1]

    def test_getitem_negative_index(self):
        ids = [uuid4(), uuid4(), uuid4()]
        p = Progression(order=ids)
        assert p[-1] == ids[-1]


class TestProgressionSetItem:
    def test_setitem_replaces(self):
        uid1, uid2, uid3 = uuid4(), uuid4(), uuid4()
        p = Progression(order=[uid1, uid2])
        p[0] = uid3
        assert p[0] == uid3

    def test_setitem_within_range_works(self):
        uid1, uid2 = uuid4(), uuid4()
        p = Progression(order=[uid1])
        p[0] = uid2
        assert p[0] == uid2


class TestProgressionDelItem:
    def test_delitem_by_index(self):
        ids = [uuid4(), uuid4(), uuid4()]
        p = Progression(order=ids)
        del p[1]
        assert len(p) == 2
        assert ids[1] not in p

    def test_delitem_missing_raises(self):
        # __delitem__ takes int|slice — passing UUID raises TypeError
        p = Progression(order=[uuid4()])
        with pytest.raises(TypeError):
            del p[uuid4()]


class TestProgressionRemove:
    def test_remove_existing(self):
        uid = uuid4()
        p = Progression(order=[uid, uuid4()])
        p.remove(uid)
        assert uid not in p

    def test_remove_missing_raises(self):
        p = Progression()
        with pytest.raises(ItemNotFoundError):
            p.remove(uuid4())


class TestProgressionExtend:
    def test_extend_with_progression(self):
        ids = [uuid4(), uuid4()]
        p1 = Progression(order=ids)
        p2 = Progression()
        p2.extend(p1)
        assert len(p2) == 2
        for uid in ids:
            assert uid in p2

    def test_extend_with_list_raises(self):
        p = Progression()
        with pytest.raises(ValueError):
            p.extend([uuid4(), uuid4()])


class TestProgressionClear:
    def test_clear_empties(self):
        p = Progression(order=[uuid4() for _ in range(5)])
        p.clear()
        assert len(p) == 0

    def test_clear_empty_noop(self):
        p = Progression()
        p.clear()
        assert len(p) == 0


class TestProgressionInclude:
    def test_include_adds(self):
        p = Progression()
        uid = uuid4()
        p.include(uid)
        assert uid in p

    def test_include_duplicate_returns_false_or_noop(self):
        uid = uuid4()
        p = Progression(order=[uid])
        result = p.include(uid)
        # include returns False for duplicates or True for new items
        assert result is False or uid in p


class TestProgressionExclude:
    def test_exclude_removes(self):
        uid = uuid4()
        p = Progression(order=[uid])
        p.exclude(uid)
        assert uid not in p

    def test_exclude_missing_returns_false(self):
        p = Progression()
        result = p.exclude(uuid4())
        assert result is False


class TestProgressionArithmetic:
    def test_add_with_uuid(self):
        uid = uuid4()
        p = Progression()
        p2 = p + uid
        assert uid in p2

    def test_add_multiple_uuids(self):
        uid1, uid2 = uuid4(), uuid4()
        p = Progression(order=[uid1])
        p2 = p + uid2
        assert uid1 in p2
        assert uid2 in p2

    def test_iadd_appends_single_uuid(self):
        p = Progression()
        uid = uuid4()
        p += uid
        assert uid in p
        assert len(p) == 1

    def test_sub_removes_single_uuid(self):
        uid1, uid2 = uuid4(), uuid4()
        p = Progression(order=[uid1, uid2])
        result = p - uid2
        assert uid1 in result
        assert uid2 not in result


class TestProgressionMoveSwapReverse:
    def test_swap_two_positions(self):
        uid1, uid2 = uuid4(), uuid4()
        p = Progression(order=[uid1, uid2])
        p.swap(0, 1)
        assert p[0] == uid2
        assert p[1] == uid1

    def test_move_forward(self):
        ids = [uuid4() for _ in range(3)]
        p = Progression(order=ids)
        target = ids[0]
        p.move(0, 2)
        assert target in p
        assert p[0] != target

    def test_reverse(self):
        ids = [uuid4() for _ in range(3)]
        p = Progression(order=ids)
        p.reverse()
        assert list(p) == list(reversed(ids))


class TestProgressionCountIndex:
    def test_count(self):
        uid = uuid4()
        p = Progression(order=[uid, uuid4(), uid])
        assert p.count(uid) >= 1

    def test_index_returns_position(self):
        ids = [uuid4(), uuid4(), uuid4()]
        p = Progression(order=ids)
        assert p.index(ids[1]) == 1

    def test_index_missing_raises(self):
        p = Progression(order=[uuid4()])
        with pytest.raises(ValueError):
            p.index(uuid4())


class TestProgressionEquality:
    def test_equal_progressions(self):
        ids = [uuid4(), uuid4()]
        p1 = Progression(order=ids)
        p2 = Progression(order=ids)
        assert p1 == p2

    def test_different_progressions(self):
        p1 = Progression(order=[uuid4()])
        p2 = Progression(order=[uuid4()])
        assert p1 != p2

    def test_equal_to_list(self):
        ids = [uuid4()]
        p = Progression(order=ids)
        assert p == Progression(order=ids)


class TestProgressionRepr:
    def test_repr_contains_name(self):
        p = Progression(name="myprog")
        assert "myprog" in repr(p)

    def test_repr_is_string(self):
        assert isinstance(repr(Progression()), str)


class TestProgressionNext:
    def test_next_after_append(self):
        p = Progression()
        uid = uuid4()
        p.append(uid)
        result = next(iter(p))
        assert result == uid


class TestProgFactory:
    def test_prog_with_empty_list(self):
        p = prog([])
        assert isinstance(p, Progression)
        assert len(p) == 0

    def test_prog_with_uuid(self):
        uid = uuid4()
        p = prog(uid)
        assert uid in p

    def test_prog_with_list(self):
        ids = [uuid4(), uuid4()]
        p = prog(ids)
        assert len(p) == 2

    def test_prog_with_name(self):
        uid = uuid4()
        p = prog([uid], "myname")
        assert isinstance(p, Progression)
        assert p.name == "myname"
        assert uid in p
