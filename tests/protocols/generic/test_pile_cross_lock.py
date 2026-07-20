# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Cross-family locking contract: sync (threading) and async (task) APIs
must mutually exclude, and subscripting/iteration must be guarded the same
as their named-method siblings."""

import asyncio
import threading
import time

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
