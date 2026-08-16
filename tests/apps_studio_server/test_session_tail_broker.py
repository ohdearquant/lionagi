# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Scale contracts for the daemon-local per-session live tail broker."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest


async def test_twenty_viewers_share_one_tail_read_and_one_connection():
    from lionagi.studio.services.tail_broker import SessionTailBroker, TailRead

    connect_count = 0
    read_count = 0
    emitted = False
    stop_second_read = asyncio.Event()

    @asynccontextmanager
    async def connect():
        nonlocal connect_count
        connect_count += 1
        yield object()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        nonlocal emitted, read_count
        if emitted:
            await stop_second_read.wait()
            return TailRead([], [], None, message_cursor, signal_cursor, True, True)
        emitted = True
        read_count += 1
        return TailRead(
            messages=[
                {
                    "id": "m-1",
                    "timestamp": 101.0,
                    "branch_id": "b-1",
                    "role": "assistant",
                    "content": {},
                }
            ],
            signals=[],
            state={"status": "running", "updated_at": 101.0},
            message_cursor=(101.0, "m-1"),
            signal_cursor=0,
            messages_caught_up=True,
            signals_caught_up=True,
        )

    broker = SessionTailBroker(
        "shared-session",
        connect=connect,
        read_tick=read_tick,
        poll_interval=0.001,
    )
    subscriptions = [await broker.subscribe_messages((100.0, "m-0")) for _ in range(20)]
    try:
        events = await asyncio.wait_for(
            asyncio.gather(*(subscription.next_event() for subscription in subscriptions)),
            timeout=1,
        )
        assert [event.payload["id"] for event in events] == ["m-1"] * 20
        assert read_count == 1
        assert connect_count == 1
    finally:
        await asyncio.gather(*(subscription.close() for subscription in subscriptions))
        stop_second_read.set()
        await broker.close()


async def test_slow_subscriber_gets_explicit_resync_instead_of_unbounded_queue():
    from lionagi.studio.services.tail_broker import SessionTailBroker, TailRead

    emitted = False
    stop_second_read = asyncio.Event()

    @asynccontextmanager
    async def connect():
        yield object()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        nonlocal emitted
        if emitted:
            await stop_second_read.wait()
            return TailRead([], [], None, message_cursor, signal_cursor, True, True)
        emitted = True
        messages = [
            {
                "id": f"m-{i}",
                "timestamp": float(i),
                "branch_id": "b-1",
                "role": "assistant",
                "content": {},
            }
            for i in range(1, 4)
        ]
        return TailRead(
            messages=messages,
            signals=[],
            state={"status": "running", "updated_at": 3.0},
            message_cursor=(3.0, "m-3"),
            signal_cursor=0,
            messages_caught_up=True,
            signals_caught_up=True,
        )

    broker = SessionTailBroker(
        "slow-session",
        connect=connect,
        read_tick=read_tick,
        poll_interval=0.001,
        subscriber_queue_size=1,
    )
    subscription = await broker.subscribe_messages((0.0, ""))
    try:
        event = await asyncio.wait_for(subscription.next_event(), timeout=1)
        assert event.kind == "resync"
        assert event.resume_cursor == (3.0, "m-3")
        assert subscription.queue_size <= 1
    finally:
        await subscription.close()
        stop_second_read.set()
        await broker.close()


@pytest.mark.parametrize("channel", ["messages", "signals"])
async def test_broker_stops_its_reader_when_the_last_viewer_leaves(channel):
    from lionagi.studio.services.tail_broker import SessionTailBroker, TailRead

    reader_cancelled = asyncio.Event()

    @asynccontextmanager
    async def connect():
        try:
            yield object()
        finally:
            reader_cancelled.set()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        await asyncio.sleep(60)
        return TailRead([], [], None, message_cursor, signal_cursor, True, True)

    broker = SessionTailBroker("cleanup", connect=connect, read_tick=read_tick)
    subscription = (
        await broker.subscribe_messages((0.0, ""))
        if channel == "messages"
        else await broker.subscribe_signals(0)
    )
    await subscription.close()

    await asyncio.wait_for(reader_cancelled.wait(), timeout=1)
    assert broker.running is False


async def test_message_stream_starts_at_snapshot_high_water(monkeypatch):
    from lionagi.studio.services import sessions as sessions_svc
    from lionagi.studio.services import tail_broker

    cursor = sessions_svc._encode_session_stream_cursor("snapshot", 42.0, "m-42")
    subscribed_from = None
    closed = False

    class Subscription:
        async def next_event(self):
            return tail_broker.TailEvent("done", {"type": "done"})

        async def close(self):
            nonlocal closed
            closed = True

    async def subscribe(session_id, start):
        nonlocal subscribed_from
        assert session_id == "snapshot"
        subscribed_from = start
        return Subscription()

    async def exists(_session_id):
        return True

    monkeypatch.setattr(sessions_svc, "session_exists", exists)
    monkeypatch.setattr(tail_broker, "subscribe_session_messages", subscribe)

    response = await sessions_svc.stream_session_route("snapshot", cursor=cursor)
    frame = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert subscribed_from == (42.0, "m-42")
    assert 'data: {"type": "done"}' in frame
    assert closed is True


async def test_signal_stream_starts_after_snapshot_sequence(monkeypatch):
    from lionagi.studio.services import sessions as sessions_svc
    from lionagi.studio.services import tail_broker

    subscribed_from = None
    closed = False

    class Subscription:
        async def next_event(self):
            return tail_broker.TailEvent("done", {"type": "done"})

        async def close(self):
            nonlocal closed
            closed = True

    async def subscribe(session_id, start):
        nonlocal subscribed_from
        assert session_id == "snapshot"
        subscribed_from = start
        return Subscription()

    async def exists(_session_id):
        return True

    monkeypatch.setattr(sessions_svc, "session_exists", exists)
    monkeypatch.setattr(tail_broker, "subscribe_session_signals", subscribe)

    response = await sessions_svc.stream_signals("snapshot", after_seq=17)
    frame = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert subscribed_from == 17
    assert 'data: {"type": "done"}' in frame
    assert closed is True


async def test_restarted_message_broker_uses_the_new_snapshot_high_water():
    from lionagi.studio.services.tail_broker import SessionTailBroker, TailRead

    seen_cursors = []
    release = asyncio.Event()

    @asynccontextmanager
    async def connect():
        yield object()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        seen_cursors.append(message_cursor)
        await release.wait()
        return TailRead([], [], None, message_cursor, signal_cursor, True, True)

    broker = SessionTailBroker("restart", connect=connect, read_tick=read_tick)
    first = await broker.subscribe_messages((1.0, "m-1"))
    await asyncio.sleep(0)
    await first.close()

    second = await broker.subscribe_messages((10.0, "m-10"))
    await asyncio.sleep(0)
    try:
        assert second.queue_size == 0, "a new snapshot must not receive stale resync state"
        assert seen_cursors[-1] == (10.0, "m-10")
    finally:
        await second.close()
        release.set()
        await broker.close()


def _message_event(index: int):
    from lionagi.studio.services.tail_broker import TailEvent

    return TailEvent("message", {"i": index}, (float(index), f"m-{index}"))


async def test_a_viewer_joining_past_the_retained_history_is_told_to_resync():
    """Selecting "everything newer than your cursor" says nothing about what sat
    between the two. Once the bounded history has dropped events, the newest
    ones it still holds begin after the joining viewer's position, and sending
    them presents a gap as a continuous replay -- the viewer never learns it
    missed anything."""
    from lionagi.studio.services.tail_broker import SessionTailBroker, TailRead

    @asynccontextmanager
    async def connect():
        yield object()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        await asyncio.sleep(0)
        return TailRead([], [], None, message_cursor, signal_cursor, True, True)

    broker = SessionTailBroker("evicted", connect=connect, read_tick=read_tick, history_size=3)
    resident = await broker.subscribe_messages((0.0, "m-0"))
    try:
        for index in range(1, 6):
            broker._publish("messages", [_message_event(index)], (float(index), f"m-{index}"))
        assert [event.resume_cursor for event in broker._message_history] == [
            (3.0, "m-3"),
            (4.0, "m-4"),
            (5.0, "m-5"),
        ], "the history has to have dropped events for this to be about eviction"

        joining = await broker.subscribe_messages((1.0, "m-1"))
        try:
            event = await asyncio.wait_for(joining.next_event(), timeout=1)
            assert event.kind == "resync", f"a gap was replayed as history: {event.kind}"
            assert joining.queue_size == 0
        finally:
            await joining.close()

        # Control: a cursor the retained history still reaches gets the replay,
        # or the assertion above is satisfied by resyncing everyone.
        covered = await broker.subscribe_messages((3.0, "m-3"))
        try:
            assert covered.queue_size == 2
            first = await asyncio.wait_for(covered.next_event(), timeout=1)
            assert first.kind == "message"
            assert first.resume_cursor == (4.0, "m-4")
        finally:
            await covered.close()
    finally:
        await resident.close()
        await broker.close()


async def test_a_viewer_joining_before_the_reader_started_is_told_to_resync():
    """A full history is only full from where the reader began. Everything the
    broker published can still start well after a joining viewer's position,
    because the events between the two were never read at all -- and a history
    that has dropped nothing looks complete from the inside."""
    from lionagi.studio.services.tail_broker import SessionTailBroker, TailRead

    @asynccontextmanager
    async def connect():
        yield object()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        await asyncio.sleep(0)
        return TailRead([], [], None, message_cursor, signal_cursor, True, True)

    broker = SessionTailBroker("origin", connect=connect, read_tick=read_tick, history_size=100)
    resident = await broker.subscribe_messages((5.0, "m-5"))
    try:
        for index in (6, 7):
            broker._publish("messages", [_message_event(index)], (float(index), f"m-{index}"))
        assert broker._history_whole["messages"], (
            "the history must have dropped nothing for this to be about the reader's start"
        )

        joining = await broker.subscribe_messages((1.0, "m-1"))
        try:
            event = await asyncio.wait_for(joining.next_event(), timeout=1)
            assert event.kind == "resync", (
                f"messages 2..5 were never read, and the replay presented the gap: {event.kind}"
            )
            assert joining.queue_size == 0
        finally:
            await joining.close()

        # Control: a cursor at or after where the reader began still replays,
        # or the assertion above is satisfied by resyncing everyone.
        covered = await broker.subscribe_messages((6.0, "m-6"))
        try:
            assert covered.queue_size == 1
            first = await asyncio.wait_for(covered.next_event(), timeout=1)
            assert first.kind == "message"
            assert first.resume_cursor == (7.0, "m-7")
        finally:
            await covered.close()
    finally:
        await resident.close()
        await broker.close()


async def test_a_restarted_channel_does_not_replay_the_previous_readers_history():
    """The retained events describe the range the last reader covered. A new
    first viewer moves the cursor somewhere else, and holding those events lets
    the next joiner be handed a replay that spans the seam between the two."""
    from lionagi.studio.services.tail_broker import SessionTailBroker, TailRead

    @asynccontextmanager
    async def connect():
        yield object()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        await asyncio.sleep(0)
        return TailRead([], [], None, message_cursor, signal_cursor, True, True)

    broker = SessionTailBroker("restarted", connect=connect, read_tick=read_tick, history_size=100)
    # A viewer on the other channel, so the broker survives the message
    # channel emptying: this is the same run open in the same pane, whose
    # signal stream stays up while the message stream reconnects.
    watcher = await broker.subscribe_signals(0)
    first = await broker.subscribe_messages((1.0, "m-1"))
    for index in (2, 3):
        broker._publish("messages", [_message_event(index)], (float(index), f"m-{index}"))
    await first.close()

    resident = await broker.subscribe_messages((20.0, "m-20"))
    try:
        assert list(broker._message_history) == [], (
            "the previous reader's events are not part of this reader's range"
        )
        broker._publish("messages", [_message_event(21)], (21.0, "m-21"))

        joining = await broker.subscribe_messages((3.0, "m-3"))
        try:
            event = await asyncio.wait_for(joining.next_event(), timeout=1)
            assert event.kind == "resync", (
                f"a replay spanned the gap between two reader ranges: {event.kind}"
            )
        finally:
            await joining.close()
    finally:
        await resident.close()
        await watcher.close()
        await broker.close()


async def test_a_broker_leaves_the_registry_with_its_last_viewer():
    """The histories are bounded per session; the registry is not. A broker kept
    after its last viewer leaves holds its event deques for the daemon's
    lifetime, so opening and closing streams across many sessions grows memory
    without bound -- one bounded buffer at a time."""
    from lionagi.studio.services import tail_broker as broker_mod

    @asynccontextmanager
    async def connect():
        yield object()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        await asyncio.sleep(0)
        return broker_mod.TailRead([], [], None, message_cursor, signal_cursor, True, True)

    original = dict(broker_mod._BROKERS)
    broker_mod._BROKERS.clear()
    try:
        broker = broker_mod.SessionTailBroker("evicted", connect=connect, read_tick=read_tick)
        broker_mod._BROKERS["evicted"] = broker
        subscription = await broker.subscribe_messages(None)
        await asyncio.sleep(0)
        assert "evicted" in broker_mod._BROKERS

        await subscription.close()

        assert "evicted" not in broker_mod._BROKERS
        assert not broker._message_history and not broker._signal_history
        assert not broker.running
    finally:
        broker_mod._BROKERS.clear()
        broker_mod._BROKERS.update(original)


async def test_a_failed_read_tells_viewers_to_resync_and_reconnects():
    """A reader exception used to end the task outright. The SSE generators
    stayed open and went on sending heartbeats, so a viewer saw a healthy
    connection that would never carry another event and had no reason to
    reconnect."""
    from lionagi.studio.services.tail_broker import SessionTailBroker, TailRead

    connect_count = 0
    reads = 0

    @asynccontextmanager
    async def connect():
        nonlocal connect_count
        connect_count += 1
        yield object()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError("injected read failure")
        await asyncio.sleep(0)
        return TailRead([], [], None, message_cursor, signal_cursor, True, True)

    broker = SessionTailBroker(
        "failing",
        connect=connect,
        read_tick=read_tick,
        poll_interval=0,
        reader_retry_interval=0,
    )
    subscription = await broker.subscribe_messages(None)
    try:
        event = await asyncio.wait_for(subscription.next_event(), timeout=2)
        assert event.kind == "resync"
        await asyncio.wait_for(_wait_for(lambda: connect_count >= 2), timeout=2)
        assert broker.running, "the reader stopped instead of reconnecting"
    finally:
        await subscription.close()
        await broker.close()


async def _wait_for(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0)


async def test_a_subscribe_cancelled_after_registration_leaves_nothing_behind():
    """The caller cannot clean up a subscription it never received.

    Registering the subscriber and starting the reader both happen before
    subscribe() yields, and it yields on purpose -- the reader has to enter its
    connection context before the subscription is exposed. A viewer that
    disconnects on that turn cancels the awaiting request, so the route
    generator's try/finally is never installed, while the queue is already in
    the subscriber map and the reader is already polling. One aborted request
    per orphan, and nothing ever collects them.
    """
    import lionagi.studio.services.tail_broker as broker_mod

    @asynccontextmanager
    async def connect():
        yield object()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        await asyncio.sleep(0)
        return broker_mod.TailRead([], [], None, message_cursor, signal_cursor, True, True)

    broker = broker_mod.SessionTailBroker("cancelled", connect=connect, read_tick=read_tick)

    task = asyncio.create_task(broker.subscribe_messages(None))
    # One turn is enough to reach the yield inside subscribe: the registration
    # and the reader task are behind it, the return is in front of it.
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Let the unwind and the reader's own cancellation settle.
    for _ in range(5):
        await asyncio.sleep(0)

    assert broker._subscribers == {}, broker._subscribers
    assert not broker.running


async def test_an_aborted_subscribe_through_the_registry_leaves_the_registry_usable():
    """The same abort, taken through the entry point the daemon actually calls.

    That path holds the process-wide registry lock across registration, and the
    cleanup an aborted subscribe performs takes that same lock. Doing the
    cleanup under the hold leaves the lock held for good, which stops every
    later message and signal subscription for every session, not just this one.
    """
    import lionagi.studio.services.tail_broker as broker_mod

    @asynccontextmanager
    async def connect():
        yield object()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        await asyncio.sleep(0)
        return broker_mod.TailRead([], [], None, message_cursor, signal_cursor, True, True)

    def register(session_id: str) -> None:
        broker_mod._BROKERS[session_id] = broker_mod.SessionTailBroker(
            session_id, connect=connect, read_tick=read_tick
        )

    register("registry-abort")
    try:
        task = asyncio.create_task(broker_mod.subscribe_session_messages("registry-abort", None))
        # One turn reaches the yield the registry lock is already released for.
        await asyncio.sleep(0)
        task.cancel()

        # A hang here is the failure this test is about, so it is bounded.
        _done, pending = await asyncio.wait([task], timeout=2)
        assert not pending, "the aborted subscribe never finished unwinding"
        assert task.cancelled()
        assert not broker_mod._BROKERS_LOCK.locked()

        # A free lock is the mechanism. A subscribe that still returns is the
        # consequence, and it is the half a later reader would notice.
        register("registry-after")
        subscription = await asyncio.wait_for(
            broker_mod.subscribe_session_messages("registry-after", None), timeout=2
        )
        await subscription.close()
    finally:
        # Deliberately not close_all_tail_brokers(): that takes the registry
        # lock, which is exactly what a failure here leaves held, so the
        # teardown would turn a failing assertion into a hung suite.
        task.cancel()
        await asyncio.wait([task], timeout=2)
        brokers = list(broker_mod._BROKERS.values())
        broker_mod._BROKERS.clear()
        await asyncio.gather(*(b.close() for b in brokers), return_exceptions=True)


async def test_a_subscribe_that_completes_leaves_a_live_subscriber():
    """Control: subscribe has to be able to leave a subscriber and a running
    reader behind, or the assertions above are satisfied by a broker that never
    registers anything."""
    import lionagi.studio.services.tail_broker as broker_mod

    @asynccontextmanager
    async def connect():
        yield object()

    async def read_tick(
        _db,
        _session_id,
        message_cursor,
        signal_cursor,
        *,
        read_messages,
        read_signals,
    ):
        await asyncio.sleep(0)
        return broker_mod.TailRead([], [], None, message_cursor, signal_cursor, True, True)

    broker = broker_mod.SessionTailBroker("not-cancelled", connect=connect, read_tick=read_tick)
    subscription = await broker.subscribe_messages(None)
    try:
        assert len(broker._subscribers) == 1
        assert broker.running
    finally:
        await subscription.close()
