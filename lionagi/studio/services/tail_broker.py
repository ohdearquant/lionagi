# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""One bounded live-tail reader per session, shared by every local viewer."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

__all__ = (
    "SessionTailBroker",
    "TailEvent",
    "TailRead",
    "TailSubscription",
    "close_all_tail_brokers",
    "subscribe_session_messages",
    "subscribe_session_signals",
)

MessageCursor = tuple[float, str]
Channel = Literal["messages", "signals"]


@dataclass(frozen=True)
class TailRead:
    messages: list[dict[str, Any]]
    signals: list[dict[str, Any]]
    state: dict[str, Any] | None
    message_cursor: MessageCursor | None
    signal_cursor: int
    messages_caught_up: bool
    signals_caught_up: bool


@dataclass(frozen=True)
class TailEvent:
    kind: Literal["data", "heartbeat", "done", "resync"]
    payload: dict[str, Any]
    resume_cursor: MessageCursor | int | None = None


ConnectFactory = Callable[[], AbstractAsyncContextManager[Any]]
ReadTick = Callable[..., Awaitable[TailRead]]


@asynccontextmanager
async def _default_connect() -> AsyncIterator[Any]:
    from ._db import open_db, store_path

    async with open_db(store_path()) as db:
        yield db


async def _default_read_tick(
    db: Any,
    session_id: str,
    message_cursor: MessageCursor | None,
    signal_cursor: int,
    *,
    read_messages: bool,
    read_signals: bool,
) -> TailRead:
    from .sessions import _read_session_tail_tick

    return await _read_session_tail_tick(
        db,
        session_id,
        message_cursor,
        signal_cursor,
        read_messages=read_messages,
        read_signals=read_signals,
    )


class TailSubscription:
    def __init__(
        self,
        broker: SessionTailBroker,
        subscription_id: int,
        channel: Channel,
        queue_size: int,
    ) -> None:
        self._broker = broker
        self._id = subscription_id
        self.channel = channel
        self._queue: asyncio.Queue[TailEvent] = asyncio.Queue(maxsize=queue_size)
        self._closed = False

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def next_event(self) -> TailEvent:
        return await self._queue.get()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._broker._unsubscribe(self._id)

    def _clear(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    def _put_batch(
        self,
        events: list[TailEvent],
        *,
        resume_cursor: MessageCursor | int | None,
    ) -> None:
        if not events:
            return
        available = self._queue.maxsize - self._queue.qsize()
        if len(events) > available:
            self._clear()
            self._queue.put_nowait(
                TailEvent(
                    kind="resync",
                    payload={"type": "resync"},
                    resume_cursor=resume_cursor,
                )
            )
            return
        for event in events:
            self._queue.put_nowait(event)


class SessionTailBroker:
    """Fan one persistent-connection tailer out to bounded viewer queues."""

    def __init__(
        self,
        session_id: str,
        *,
        connect: ConnectFactory = _default_connect,
        read_tick: ReadTick = _default_read_tick,
        poll_interval: float = 0.5,
        reader_retry_interval: float = 1.0,
        # One normal server batch must fit before the consumer task gets its
        # first scheduling turn; overflow remains explicit beyond that bound.
        subscriber_queue_size: int = 1_024,
        history_size: int = 1_000,
    ) -> None:
        self.session_id = session_id
        self._connect = connect
        self._read_tick = read_tick
        self._poll_interval = max(0.0, poll_interval)
        self._reader_retry_interval = max(0.0, reader_retry_interval)
        self._subscriber_queue_size = max(1, subscriber_queue_size)
        self._subscribers: dict[int, TailSubscription] = {}
        self._next_subscription_id = 0
        self._message_cursor: MessageCursor | None = None
        self._signal_cursor = 0
        self._message_history: deque[TailEvent] = deque(maxlen=max(1, history_size))
        self._signal_history: deque[TailEvent] = deque(maxlen=max(1, history_size))
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def subscribe_messages(self, cursor: MessageCursor | None) -> TailSubscription:
        return await self._subscribe("messages", cursor)

    async def subscribe_signals(self, cursor: int) -> TailSubscription:
        return await self._subscribe("signals", max(0, cursor))

    async def _subscribe(
        self,
        channel: Channel,
        cursor: MessageCursor | int | None,
    ) -> TailSubscription:
        started_reader = False
        async with self._lock:
            if self._closed:
                raise RuntimeError("tail broker is closed")
            subscription_id = self._next_subscription_id
            self._next_subscription_id += 1
            subscription = TailSubscription(
                self,
                subscription_id,
                channel,
                self._subscriber_queue_size,
            )
            self._subscribers[subscription_id] = subscription
            if channel == "messages":
                start = cursor if isinstance(cursor, tuple) else None
                if not any(
                    item.channel == "messages" and item is not subscription
                    for item in self._subscribers.values()
                ):
                    self._message_cursor = start
                else:
                    self._replay_or_resync(subscription, start, self._message_history)
            else:
                start_seq = int(cursor or 0)
                if not any(
                    item.channel == "signals" and item is not subscription
                    for item in self._subscribers.values()
                ):
                    self._signal_cursor = start_seq
                else:
                    self._replay_or_resync(subscription, start_seq, self._signal_history)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(
                    self._run(),
                    name=f"studio-tail-{self.session_id}",
                )
                started_reader = True
        if started_reader:
            # Let the task enter its connection context before exposing the
            # subscription.  Otherwise an immediately-disconnected viewer can
            # cancel a not-yet-started task and skip resource cleanup entirely.
            #
            # Cleanup for this await belongs here and cannot belong to the
            # caller. The caller installs its `finally` around the subscription
            # it received, and a cancellation landing on this line means it
            # never receives one -- while the registration and the reader task
            # above have already happened. The subscriber queue would stay in
            # `_subscribers` and the reader would keep polling, for a viewer
            # that is gone, once per aborted request.
            try:
                await asyncio.sleep(0)
            except BaseException:
                await subscription.close()
                raise
        return subscription

    def _replay_or_resync(
        self,
        subscription: TailSubscription,
        cursor: MessageCursor | int | None,
        history: deque[TailEvent],
    ) -> None:
        current = (
            self._message_cursor if subscription.channel == "messages" else self._signal_cursor
        )
        if cursor == current:
            return
        replay = [
            event
            for event in history
            if event.resume_cursor is not None and (cursor is None or event.resume_cursor > cursor)
        ]
        if replay:
            subscription._put_batch(replay, resume_cursor=current)
            return
        subscription._put_batch(
            [TailEvent("resync", {"type": "resync"}, current)],
            resume_cursor=current,
        )

    async def _unsubscribe(self, subscription_id: int) -> None:
        task: asyncio.Task[None] | None = None
        # Registry lock first, then the broker's own, everywhere both are held.
        async with _BROKERS_LOCK, self._lock:
            self._subscribers.pop(subscription_id, None)
            if not self._subscribers:
                # The histories are bounded per session; the registry is not.
                # Keeping a broker after its last viewer leaves turns "at most
                # two thousand events" into "at most two thousand events per
                # session anyone has ever opened, for the life of the daemon",
                # which is the same as unbounded. It goes when they go.
                if _BROKERS.get(self.session_id) is self:
                    del _BROKERS[self.session_id]
                self._message_history.clear()
                self._signal_history.clear()
                if self._task is not None:
                    task = self._task
                    self._task = None
                    task.cancel()
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        task: asyncio.Task[None] | None
        async with self._lock:
            self._closed = True
            self._subscribers.clear()
            task = self._task
            self._task = None
            if task is not None:
                task.cancel()
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    def _channel_subscribers(self, channel: Channel) -> list[TailSubscription]:
        return [
            subscription
            for subscription in self._subscribers.values()
            if subscription.channel == channel
        ]

    def _publish(
        self,
        channel: Channel,
        events: list[TailEvent],
        cursor: MessageCursor | int | None,
    ) -> None:
        history = self._message_history if channel == "messages" else self._signal_history
        history.extend(events)
        for subscription in self._channel_subscribers(channel):
            subscription._put_batch(events, resume_cursor=cursor)

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self._read_until_done()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A read or connection failure is not the end of the
                    # stream, and letting the task die makes it look like one
                    # from nowhere: the SSE generators stay open and keep
                    # sending heartbeats, so a viewer sees a healthy connection
                    # that will never carry another event. Subscribers are told
                    # to resync, since their cursors may now have a gap, and
                    # the reader reconnects.
                    async with self._lock:
                        if not self._subscribers:
                            return
                        self._notify_resync_locked()
                    await asyncio.sleep(self._reader_retry_interval)
        except asyncio.CancelledError:
            raise
        finally:
            async with self._lock:
                if self._task is asyncio.current_task():
                    self._task = None

    def _notify_resync_locked(self) -> None:
        for channel in ("messages", "signals"):
            cursor = self._message_cursor if channel == "messages" else self._signal_cursor
            for subscription in self._channel_subscribers(channel):
                subscription._put_batch(
                    [TailEvent("resync", {"type": "resync"}, cursor)],
                    resume_cursor=cursor,
                )

    async def _read_until_done(self) -> None:
        async with self._connect() as db:
            while True:
                async with self._lock:
                    read_messages = bool(self._channel_subscribers("messages"))
                    read_signals = bool(self._channel_subscribers("signals"))
                    if not read_messages and not read_signals:
                        return
                    message_cursor = self._message_cursor
                    signal_cursor = self._signal_cursor
                batch = await self._read_tick(
                    db,
                    self.session_id,
                    message_cursor,
                    signal_cursor,
                    read_messages=read_messages,
                    read_signals=read_signals,
                )
                async with self._lock:
                    if batch.messages:
                        message_events = [
                            TailEvent(
                                "data",
                                message,
                                (float(message.get("timestamp") or 0.0), str(message["id"])),
                            )
                            for message in batch.messages
                        ]
                        self._message_cursor = batch.message_cursor
                        self._publish(
                            "messages",
                            message_events,
                            self._message_cursor,
                        )
                    if batch.signals:
                        signal_events = [
                            TailEvent("data", signal, int(signal["seq"]))
                            for signal in batch.signals
                        ]
                        self._signal_cursor = batch.signal_cursor
                        self._publish("signals", signal_events, self._signal_cursor)
                    done = False
                    if batch.state is not None:
                        from .sessions import is_session_stream_done

                        done = is_session_stream_done(batch.state, now=time.time())
                    active_caught_up = (not read_messages or batch.messages_caught_up) and (
                        not read_signals or batch.signals_caught_up
                    )
                    if done and active_caught_up:
                        done_event = TailEvent("done", {"type": "done"})
                        self._publish("messages", [done_event], self._message_cursor)
                        self._publish("signals", [done_event], self._signal_cursor)
                        return
                if (read_messages and not batch.messages_caught_up) or (
                    read_signals and not batch.signals_caught_up
                ):
                    await asyncio.sleep(0)
                else:
                    await asyncio.sleep(self._poll_interval)


_BROKERS: dict[str, SessionTailBroker] = {}
_BROKERS_LOCK = asyncio.Lock()


def _broker_for(session_id: str) -> SessionTailBroker:
    """Caller holds `_BROKERS_LOCK`.

    Lookup and the subscription that follows it have to happen under the same
    hold. Between them a broker can lose its last viewer and be evicted, and a
    caller holding the reference would then attach to something no longer
    reachable -- a private reader for one viewer, which is the sharing this
    module exists to do.
    """
    broker = _BROKERS.get(session_id)
    if broker is None or broker._closed:
        broker = SessionTailBroker(session_id)
        _BROKERS[session_id] = broker
    return broker


async def subscribe_session_messages(
    session_id: str,
    cursor: MessageCursor | None,
) -> TailSubscription:
    async with _BROKERS_LOCK:
        return await _broker_for(session_id).subscribe_messages(cursor)


async def subscribe_session_signals(session_id: str, cursor: int) -> TailSubscription:
    async with _BROKERS_LOCK:
        return await _broker_for(session_id).subscribe_signals(cursor)


async def close_all_tail_brokers() -> None:
    async with _BROKERS_LOCK:
        brokers = list(_BROKERS.values())
        _BROKERS.clear()
    await asyncio.gather(*(broker.close() for broker in brokers), return_exceptions=True)
