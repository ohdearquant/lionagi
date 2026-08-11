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
