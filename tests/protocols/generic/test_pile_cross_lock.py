# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Cross-family locking contract: sync (threading) and async (task) APIs
must mutually exclude, and subscripting/iteration must be guarded the same
as their named-method siblings."""

import asyncio
import threading
import time

import pytest

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile


class _Item(Element):
    pass


def _hold_lock(pile: Pile, acquired: threading.Event, release: threading.Event) -> None:
    with pile._lock:
        acquired.set()
        release.wait(10)


def test_getitem_blocks_while_sync_lock_held_by_other_thread():
    pile = Pile()
    item = _Item()
    pile.include(item)

    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(pile, acquired, release))
    holder.start()
    assert acquired.wait(5)

    got: list = []
    reader = threading.Thread(target=lambda: got.append(pile[item.id]))
    reader.start()
    time.sleep(0.15)
    assert not got, "__getitem__ must contend for the sync lock"

    release.set()
    reader.join(5)
    holder.join(5)
    assert got and got[0] is item


def test_setitem_blocks_while_sync_lock_held_by_other_thread():
    pile = Pile()
    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(pile, acquired, release))
    holder.start()
    assert acquired.wait(5)

    done: list = []
    new_item = _Item()

    def writer():
        pile[new_item.id] = new_item
        done.append(True)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    time.sleep(0.15)
    assert not done, "__setitem__ must contend for the sync lock"

    release.set()
    writer_thread.join(5)
    holder.join(5)
    assert done and new_item.id in pile


async def test_async_method_excluded_by_sync_lock_held_in_thread():
    pile = Pile()
    pile.include(_Item())

    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(pile, acquired, release))
    holder.start()
    assert acquired.wait(5)

    done = asyncio.Event()

    async def clearer():
        await pile.aclear()
        done.set()

    task = asyncio.ensure_future(clearer())
    await asyncio.sleep(0.2)
    assert not done.is_set(), "aclear must wait for a sync-lock holder in another thread"
    assert len(pile) == 1

    release.set()
    await asyncio.wait_for(done.wait(), 5)
    await task
    holder.join(5)
    assert len(pile) == 0


async def test_sync_caller_blocked_while_async_method_holds_locks():
    from lionagi.ln import async_synchronized

    pile = Pile()
    item = _Item()
    pile.include(item)

    entered = asyncio.Event()

    @async_synchronized
    async def slow_op(self):
        # Holds both locks (async wrapper) while yielding to the loop, so a
        # sync thread can observably contend without freezing the loop.
        entered.set()
        await asyncio.sleep(0.5)

    got: list = []

    def sync_reader():
        got.append(pile.get(item.id, None))

    task = asyncio.ensure_future(slow_op(pile))
    await asyncio.wait_for(entered.wait(), 5)

    reader = threading.Thread(target=sync_reader)
    reader.start()
    await asyncio.sleep(0.15)
    assert not got, "sync get() must wait while an async method holds the locks"

    await task
    reader.join(5)
    assert got == [item]


async def test_cross_family_hammer_keeps_invariants():
    pile = Pile()
    stop = threading.Event()
    errors: list[BaseException] = []

    def sync_worker():
        try:
            while not stop.is_set():
                item = _Item()
                pile.include(item)
                assert item.id in pile
                pile.pop(item.id, None)
                list(pile)
        except BaseException as e:  # noqa: BLE001 - recorded and asserted below
            errors.append(e)

    threads = [threading.Thread(target=sync_worker) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        for _ in range(200):
            item = _Item()
            await pile.ainclude(item)
            await pile.aget(item.id, None)
            await pile.apop(item.id, None)
    finally:
        stop.set()
        for t in threads:
            t.join(10)

    assert not errors, f"cross-family mutation raised: {errors[:3]}"
    assert len(pile.collections) == len(pile.progression)
    assert set(pile.collections.keys()) == set(pile.progression)


async def test_async_body_can_reenter_synchronized_methods():
    # ainclude's body calls the @synchronized include(); the wrapper already
    # holds the RLock, so this must not deadlock.
    pile = Pile()
    item = _Item()
    await asyncio.wait_for(pile.ainclude(item), 5)
    assert item.id in pile
    await asyncio.wait_for(pile.aexclude(item.id), 5)
    assert item.id not in pile


def test_sync_reentrancy_unchanged():
    pile = Pile()
    item = _Item()
    pile.update(item)  # update -> include reentry
    assert item.id in pile
    pile.exclude(item.id)  # exclude -> pop reentry
    assert item.id not in pile


def test_iteration_is_snapshot_during_mutation():
    pile = Pile()
    items = [_Item() for _ in range(50)]
    pile.include(items)

    seen = 0
    for element in pile:
        # Removing already-visited items mid-iteration must not corrupt
        # traversal or raise (unvisited removals fail loud with KeyError).
        pile.pop(element.id, None)
        seen += 1
    assert seen == 50
    assert len(pile) == 0


async def test_async_with_block_excludes_sync_thread():
    # `async with pile:` must hold the full both-lock boundary: a sync-thread
    # reader blocks until the block exits.
    pile = Pile()
    item = _Item()
    pile.include(item)

    got: list = []
    reader = threading.Thread(target=lambda: got.append(pile.get(item.id, None)))

    async with pile:
        reader.start()
        await asyncio.sleep(0.15)
        assert not got, "sync get() must wait while `async with pile:` is held"

    reader.join(5)
    assert got == [item]


async def test_adump_snapshot_excludes_sync_thread_mutation(tmp_path):
    # While adump holds its snapshot region, a sync-thread include() must not
    # interleave; it lands only after the region releases.
    pytest.importorskip("pandas", reason="adump serializes through the optional pandas adapter")
    pile = Pile()
    pile.include([_Item() for _ in range(3)])

    in_snapshot = threading.Event()
    real_to_df = pile.to_df

    def slow_to_df(*a, **kw):
        in_snapshot.set()
        time.sleep(0.3)
        return real_to_df(*a, **kw)

    object.__setattr__(pile, "to_df", slow_to_df)

    included: list = []

    def includer():
        in_snapshot.wait(5)
        pile.include(_Item())
        included.append(True)

    t = threading.Thread(target=includer)
    t.start()
    await pile.adump(tmp_path / "dump.json", obj_key="json")
    t.join(5)

    assert included, "sync include must eventually complete after adump releases"
    assert len(pile) == 4


async def test_same_loop_sync_call_reenters_documented_boundary():
    # Documented boundary: exclusion is cross-thread. A sync call made by a
    # DIFFERENT task on the same event-loop thread re-enters the RLock while
    # an async operation is mid-await, and proceeds. This pins the contract
    # so any future change to it is deliberate.
    from lionagi.ln import async_synchronized

    pile = Pile()
    pile.include(_Item())

    holding = asyncio.Event()
    proceed = asyncio.Event()

    @async_synchronized
    async def slow_op(self):
        holding.set()
        await proceed.wait()

    task = asyncio.ensure_future(slow_op(pile))
    await asyncio.wait_for(holding.wait(), 5)

    # Same loop thread owns the RLock: this re-enters and completes.
    pile.clear()
    assert len(pile) == 0

    proceed.set()
    await task


async def test_async_pile_iterator_does_not_block_event_loop():
    # The legacy AsyncPileIterator must share the spin path: with a sync
    # thread holding _lock, the event loop keeps ticking while it waits.
    pile = Pile()
    items = [_Item() for _ in range(3)]
    pile.include(items)

    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(pile, acquired, release))
    holder.start()
    assert acquired.wait(5)

    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(50):
            ticks += 1
            await asyncio.sleep(0.005)

    async def iterate():
        it = pile.AsyncPileIterator(pile)
        first = await it.__anext__()
        return first

    ticker_task = asyncio.ensure_future(ticker())
    iter_task = asyncio.ensure_future(iterate())
    await asyncio.sleep(0.2)
    assert not iter_task.done(), "iterator must be waiting on the held sync lock"
    assert ticks >= 10, "event loop must keep running while the iterator spins"

    release.set()
    first = await asyncio.wait_for(iter_task, 5)
    assert first is items[0]
    ticker_task.cancel()
    holder.join(5)


async def test_async_pile_iterator_bulk_iteration_yields_between_items():
    # Uncontended bulk iteration through the legacy iterator must checkpoint
    # between elements so a ready sibling task keeps running.
    pile = Pile()
    pile.include([_Item() for _ in range(2000)])

    ticks = 0
    stop = False

    async def ticker():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    ticker_task = asyncio.ensure_future(ticker())
    await asyncio.sleep(0)

    seen = 0
    it = pile.AsyncPileIterator(pile)
    async for _ in it:
        seen += 1

    stop = True
    await ticker_task

    assert seen == 2000
    assert ticks >= 1000, f"ticker starved during bulk iteration (ticks={ticks})"


async def test_cardinality_reads_excluded_while_async_method_holds_locks():
    # __len__ / size / is_empty / __bool__ must sit inside the cross-family
    # boundary like every other public read: a sync thread's cardinality
    # read blocks while an async method holds both locks.
    from lionagi.ln import async_synchronized

    pile = Pile()
    pile.include(_Item())

    entered = asyncio.Event()

    @async_synchronized
    async def slow_op(self):
        entered.set()
        await asyncio.sleep(0.5)

    got: list = []

    def sync_reads():
        got.append((len(pile), pile.size(), pile.is_empty(), bool(pile)))

    task = asyncio.ensure_future(slow_op(pile))
    await asyncio.wait_for(entered.wait(), 5)

    reader = threading.Thread(target=sync_reads)
    reader.start()
    await asyncio.sleep(0.15)
    assert not got, "cardinality reads must wait while an async method holds the locks"

    await task
    reader.join(5)
    assert got == [(1, 1, False, True)]
