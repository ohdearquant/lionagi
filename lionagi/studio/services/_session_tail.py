# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Application-local, snapshot-bound session tails with bounded retained delivery."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from lionagi.ln.concurrency import CancelScope, Event, ExceptionGroup, Lock, move_on_after, sleep

from ._db import open_db, store_path

__all__ = ("SessionTailRegistry", "StreamResyncError", "capture_frontiers")

MAX_BROKERS = 64
MAX_SUBSCRIBERS = 128
MAX_QUEUE_ROWS = 128
MAX_QUEUE_BYTES = 8 * 1_048_576
MAX_RETAINED_BYTES = 16 * 1_048_576
MAX_TOTAL_RETAINED_BYTES = 64 * 1_048_576
IDLE_SECONDS = 300.0
POLL_SECONDS = 0.5
HEARTBEAT_SECONDS = 5.0
TAIL_BATCH_ROWS = 64

Channel = Literal["messages", "signals"]


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


class StreamResyncError(ValueError):
    """The live position is unavailable; obtain a new paired snapshot."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    @property
    def detail(self) -> dict[str, str]:
        return {"code": "stream_resync_required", "reason": self.reason}


@dataclass(frozen=True)
class _Frontier:
    progression_id: str | None
    length: int
    anchor: str | None


def capture_frontiers(
    branches: list[Any], collections: dict[str, list[str]]
) -> dict[str, _Frontier]:
    """Capture typed append positions from the detail transaction's own rows."""
    result = {}
    for branch in branches:
        branch_id, progression_id = branch["id"], branch["progression_id"]
        ids = collections[branch_id]
        if (
            not isinstance(branch_id, str)
            or not branch_id
            or (progression_id is not None and not isinstance(progression_id, str))
            or not isinstance(ids, list)
            or any(not isinstance(item, str) or not item for item in ids)
        ):
            raise StreamResyncError("frontier_unavailable")
        result[branch_id] = _Frontier(progression_id, len(ids), ids[-1] if ids else None)
    return result


def _frontier_bytes(frontiers: dict[str, _Frontier]) -> int:
    return len(
        _json(
            {key: [v.progression_id, v.length, v.anchor] for key, v in frontiers.items()}
        ).encode()
    )


def _validate(
    frontiers: dict[str, _Frontier], current: dict[str, tuple[str | None, list[str]]]
) -> None:
    for branch_id, old in frontiers.items():
        found = current.get(branch_id)
        if found is None or found[0] != old.progression_id:
            raise StreamResyncError("anchor_mismatch")
        ids = found[1]
        if len(ids) < old.length:
            raise StreamResyncError("frontier_shrunk")
        if old.length and ids[old.length - 1] != old.anchor:
            raise StreamResyncError("anchor_mismatch")


@dataclass
class _Bootstrap:
    token: str
    session_id: str
    frontiers: dict[str, _Frontier]
    signal_seq: int
    touched: float
    size: int


@dataclass
class _Delivery:
    channel: Channel
    index: int
    payload: str
    size: int
    branch_id: str | None = None
    position: int = 0
    message_id: str | None = None
    progression_id: str | None = None
    references: int = 1


@dataclass(eq=False)
class _Subscriber:
    owner: _Tail
    bootstrap: _Bootstrap
    channel: Channel
    starting_cursor: str
    starting_seq: int
    after_index: int
    delivered_index: int | None = None
    queue: deque[tuple[_Delivery, int]] = field(default_factory=deque)
    queued_bytes: int = 0
    changed: Event = field(default_factory=Event)
    control: str | None = None
    finished: bool = False
    released: bool = False
    ready: bool = False

    def frame(self, delivery: _Delivery) -> str:
        if delivery.channel == "messages":
            cursor = f"{self.bootstrap.token}.{self.owner.generation}.{delivery.index}"
            return f"id: {cursor}\ndata: {delivery.payload}\n\n"
        return f"data: {delivery.payload}\n\n"

    def eligible(self, delivery: _Delivery) -> bool:
        if delivery.channel != self.channel:
            return False
        if self.channel == "signals":
            return delivery.index > self.starting_seq
        old = self.bootstrap.frontiers.get(delivery.branch_id)
        return delivery.index > self.after_index and delivery.position >= (old.length if old else 0)

    def offer(self, delivery: _Delivery) -> None:
        if (
            not self.ready
            or self.control is not None
            or self.finished
            or not self.eligible(delivery)
        ):
            return
        size = len(self.frame(delivery).encode())
        if size > MAX_QUEUE_BYTES:
            self.resync("resource_limit")
        elif len(self.queue) >= MAX_QUEUE_ROWS or self.queued_bytes + size > MAX_QUEUE_BYTES:
            self.resync("overflow")
        else:
            delivery.references += 1
            self.queue.append((delivery, size))
            self.queued_bytes += size
            self.changed.set()

    def clear(self) -> None:
        while self.queue:
            delivery, _ = self.queue.popleft()
            self.owner.release(delivery)
        self.queued_bytes = 0

    def resync(self, reason: str) -> None:
        if self.control is not None:
            return
        self.clear()
        value: dict[str, Any] = {
            "type": "resync_required",
            "reason": reason,
            "stream": self.channel,
        }
        if self.channel == "messages":
            # Before any payload, keep the subscription start in the client's own form.
            value["resume_cursor"] = (
                self.starting_cursor
                if self.delivered_index is None
                else f"{self.bootstrap.token}.{self.owner.generation}.{self.delivered_index}"
            )
        else:
            value["resume_after_seq"] = (
                self.starting_seq if self.delivered_index is None else self.delivered_index
            )
        self.control = f"data: {_json(value)}\n\n"
        self.changed.set()

    async def frames(self):
        try:
            while True:
                if self.control is not None:
                    yield self.control
                    return
                if self.queue:
                    delivery, size = self.queue.popleft()
                    self.queued_bytes -= size
                    frame = self.frame(delivery)
                    self.owner.release(delivery)
                    self.delivered_index = delivery.index
                    yield frame
                    continue
                if self.finished:
                    yield 'data: {"type":"done"}\n\n'
                    return
                if self.changed.is_set():
                    self.changed = Event()
                with move_on_after(HEARTBEAT_SECONDS) as timeout:
                    await self.changed.wait()
                if timeout.cancelled_caught:
                    yield 'data: {"type":"heartbeat"}\n\n'
        finally:
            with CancelScope(shield=True):
                await self.owner.registry.unsubscribe(self)


class SessionTailRegistry:
    """One configured store and boot epoch for an application's lifetime."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path if path is not None else store_path()
        self.epoch = secrets.token_urlsafe(18)
        self.bootstraps: dict[str, _Bootstrap] = {}
        self.tails: dict[str, _Tail] = {}
        self.metadata_bytes = 0
        self.payload_bytes = 0
        self.lock = Lock()
        self.closed = False

    @property
    def retained_bytes(self) -> int:
        return self.metadata_bytes + self.payload_bytes

    def session_metadata_bytes(self, session_id: str) -> int:
        bootstraps = sum(
            boot.size for boot in self.bootstraps.values() if boot.session_id == session_id
        )
        tail = self.tails.get(session_id)
        return bootstraps + (tail.metadata_size if tail is not None else 0)

    def session_retained_bytes(self, session_id: str) -> int:
        tail = self.tails.get(session_id)
        return self.session_metadata_bytes(session_id) + (
            tail.retained_bytes if tail is not None else 0
        )

    def can_fit(self, session_id: str, extra: int) -> bool:
        return (
            self.retained_bytes + extra <= MAX_TOTAL_RETAINED_BYTES
            and self.session_retained_bytes(session_id) + extra <= MAX_RETAINED_BYTES
        )

    def make_room(self, session_id: str, extra: int, *, own_session_only: bool = False) -> bool:
        """Reclaim eligible payload; eviction can raise StreamResyncError."""
        # Dropping payload cannot fix an oversized metadata admission.
        if (
            self.metadata_bytes + extra > MAX_TOTAL_RETAINED_BYTES
            or self.session_metadata_bytes(session_id) + extra > MAX_RETAINED_BYTES
        ):
            return False
        # Subscribers still queueing an evicted delivery are resynced; those
        # that no longer hold it are unaffected. Admission reclaims only its
        # own session; publishers may reclaim across sessions.
        while not self.can_fit(session_id, extra):
            local_pressure = self.session_retained_bytes(session_id) + extra > MAX_RETAINED_BYTES
            candidates = [
                tail
                for key, tail in self.tails.items()
                if tail.log and (not (local_pressure or own_session_only) or key == session_id)
            ]
            if not candidates:
                return False
            victim = min(candidates, key=lambda tail: (tail.touched, tail.session_id))
            item = victim.log[0]
            for sub in tuple(victim.subscribers):
                if any(queued is item for queued, _size in sub.queue):
                    sub.resync("overflow")
            victim._evict()
        return True

    def _expire(self) -> None:
        now = time.monotonic()
        active = {sub.bootstrap.token for tail in self.tails.values() for sub in tail.subscribers}
        for token, boot in list(self.bootstraps.items()):
            if token in active:
                boot.touched = now
            elif now - boot.touched >= IDLE_SECONDS:
                self.metadata_bytes -= boot.size
                del self.bootstraps[token]
        for session_id, tail in list(self.tails.items()):
            if not tail.subscribers and tail.task is None and now - tail.touched >= IDLE_SECONDS:
                tail.discard()
                del self.tails[session_id]

    def snapshot(
        self, session_id: str, frontiers: dict[str, _Frontier], signal_seq: int
    ) -> dict[str, Any]:
        self._expire()
        if self.closed or type(signal_seq) is not int or signal_seq < 0:
            raise StreamResyncError("frontier_unavailable")
        token = f"sl1.{self.epoch}.{secrets.token_urlsafe(18)}"
        size = _frontier_bytes(frontiers) + len(token.encode()) + len(session_id.encode()) + 128
        if not self.make_room(session_id, size, own_session_only=True):
            raise StreamResyncError("resource_limit")
        self.bootstraps[token] = _Bootstrap(
            token, session_id, frontiers, signal_seq, time.monotonic(), size
        )
        self.metadata_bytes += size
        return {"version": 1, "message_cursor": token, "signal_seq": signal_seq}

    def _decode(self, token: str, session_id: str) -> tuple[_Bootstrap, str | None, int | None]:
        self._expire()
        if self.closed or not isinstance(token, str) or len(token) > 512:
            raise StreamResyncError("frontier_unavailable")
        parts = token.split(".")
        if len(parts) not in {3, 5} or parts[0] != "sl1":
            raise StreamResyncError("frontier_unavailable")
        if parts[1] != self.epoch:
            raise StreamResyncError("foreign_epoch")
        boot = self.bootstraps.get(".".join(parts[:3]))
        if boot is None:
            raise StreamResyncError("expired")
        if boot.session_id != session_id:
            raise StreamResyncError("frontier_unavailable")
        generation, index = None, None
        if len(parts) == 5:
            generation = parts[3]
            if (
                not generation
                or len(generation) > 64
                or not parts[4].isascii()
                or not parts[4].isdigit()
                or len(parts[4]) > 20
            ):
                raise StreamResyncError("frontier_unavailable")
            index = int(parts[4])
        boot.touched = time.monotonic()
        return boot, generation, index

    async def subscribe(
        self, session_id: str, channel: Channel, token: str, *, after_seq: int = 0
    ) -> _Subscriber:
        async with self.lock:
            boot, generation, index = self._decode(token, session_id)
            if channel == "signals" and (
                index is not None or type(after_seq) is not int or after_seq < boot.signal_seq
            ):
                raise StreamResyncError("frontier_unavailable")
            tail = self.tails.get(session_id)
            if tail is not None and tail.closing and tail.task is not None:
                try:
                    await asyncio.shield(tail.task)
                except StreamResyncError:
                    raise
                except Exception as error:
                    raise StreamResyncError(tail.error or "frontier_unavailable") from error
            if tail is not None and tail.error is not None:
                if tail.subscribers:
                    raise StreamResyncError(tail.error)
                await tail.stop()
                tail.discard()
                del self.tails[session_id]
                tail = None
            created = tail is None
            if tail is None:
                if index is not None:
                    raise StreamResyncError("frontier_unavailable")
                if len(self.tails) >= MAX_BROKERS:
                    raise StreamResyncError("resource_limit")
                tail = _Tail(self, boot)
                tail.check_retention(boot, channel, index, after_seq, generation)
                if not self.make_room(session_id, tail.metadata_size, own_session_only=True):
                    raise StreamResyncError("resource_limit")
                self.metadata_bytes += tail.metadata_size
                self.tails[session_id] = tail
            if len(tail.subscribers) >= MAX_SUBSCRIBERS:
                raise StreamResyncError("resource_limit")
            sub = None
            try:
                tail.check_retention(boot, channel, index, after_seq, generation)
                sub = _Subscriber(tail, boot, channel, token, after_seq, index or 0)
                tail.subscribers.add(sub)
                if tail.task is None:
                    await tail.start()
                await tail.admit(sub, generation)
                if sub.control is not None:
                    raise StreamResyncError(tail.error or "frontier_unavailable")
            except BaseException:
                if sub is not None:
                    tail.subscribers.discard(sub)
                    sub.clear()
                if not tail.subscribers:
                    with CancelScope(shield=True):
                        try:
                            await tail.stop()
                        finally:
                            if created and self.tails.get(session_id) is tail:
                                tail.discard()
                                del self.tails[session_id]
                raise
            return sub

    async def unsubscribe(self, sub: _Subscriber) -> None:
        with CancelScope(shield=True):
            async with self.lock:
                if sub.released:
                    return
                sub.released = True
                sub.owner.subscribers.discard(sub)
                sub.bootstrap.touched = time.monotonic()
                sub.clear()
                if not sub.owner.subscribers:
                    await sub.owner.stop()
                sub.owner.touched = time.monotonic()
                self._expire()

    async def aclose(self) -> None:
        with CancelScope(shield=True):
            async with self.lock:
                self.closed = True
                errors = []
                for tail in self.tails.values():
                    for sub in tail.subscribers:
                        sub.resync("frontier_unavailable")
                    try:
                        await tail.stop()
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        tail.discard()
                self.tails.clear()
                self.bootstraps.clear()
                self.metadata_bytes = 0
                if errors:
                    raise ExceptionGroup("Session tail shutdown failed", errors)


class _Tail:
    def __init__(self, registry: SessionTailRegistry, boot: _Bootstrap) -> None:
        self.registry = registry
        self.session_id = boot.session_id
        self.generation = secrets.token_urlsafe(12)
        self.io_lock = Lock()
        self.positions = dict(boot.frontiers)
        self.floor_positions = dict(boot.frontiers)
        self.signal_seq = boot.signal_seq
        self.signal_floor = boot.signal_seq
        self.message_index = 0
        self.message_floor = 0
        self.log: deque[_Delivery] = deque()
        self.subscribers: set[_Subscriber] = set()
        self.retained_bytes = 0
        self.metadata_size = 2 * _frontier_bytes(self.positions) + 256
        self.task: asyncio.Task | None = None
        self.resource: Any = None
        self.db: Any = None
        self.schema_version: int | None = None
        self.completed_data_version: int | None = None
        self.completed_state: dict[str, Any] | None = None
        self.signal_definition: str | None = None
        self.signal_definition_bytes = 0
        self.has_signals = False
        self.error: str | None = None
        self.closing = False
        self.touched = time.monotonic()

    def check_retention(
        self,
        boot: _Bootstrap,
        channel: Channel,
        index: int | None,
        after_seq: int,
        generation: str | None = None,
    ) -> None:
        if channel == "signals":
            if after_seq < self.signal_floor or after_seq > max(self.signal_seq, boot.signal_seq):
                raise StreamResyncError("frontier_unavailable")
        elif index is not None:
            if generation != self.generation:
                raise StreamResyncError("frontier_unavailable")
            if index < self.message_floor or index > self.message_index:
                raise StreamResyncError("frontier_unavailable")
        else:
            for branch_id, floor in self.floor_positions.items():
                cut = boot.frontiers.get(branch_id)
                if (cut.length if cut else 0) < floor.length:
                    raise StreamResyncError("frontier_unavailable")
                if (
                    cut
                    and cut.length == floor.length
                    and (cut.anchor, cut.progression_id) != (floor.anchor, floor.progression_id)
                ):
                    raise StreamResyncError("anchor_mismatch")

    def release(self, item: _Delivery) -> None:
        item.references -= 1
        if item.references == 0:
            self.retained_bytes -= item.size
            self.registry.payload_bytes -= item.size

    def _evict(self) -> None:
        item = self.log[0]
        floors = dict(self.floor_positions)
        if item.channel == "messages":
            floors[item.branch_id] = _Frontier(
                item.progression_id, item.position + 1, item.message_id
            )
        size = (
            _frontier_bytes(self.positions)
            + _frontier_bytes(floors)
            + self.signal_definition_bytes
            + 256
        )
        # Queue holders must be released before eviction; their shared payload
        # stays charged until its final reference disappears.
        if item.references != 1 or size - self.metadata_size > item.size:
            raise StreamResyncError("resource_limit")
        self.log.popleft()
        if item.channel == "signals":
            self.signal_floor = max(self.signal_floor, item.index)
        else:
            self.message_floor = max(self.message_floor, item.index)
        self.release(item)
        self.floor_positions = floors
        self.registry.metadata_bytes += size - self.metadata_size
        self.metadata_size = size

    def resize_metadata(self, positions: dict[str, _Frontier] | None = None) -> None:
        positions = self.positions if positions is None else positions
        while True:
            size = (
                _frontier_bytes(positions)
                + _frontier_bytes(self.floor_positions)
                + self.signal_definition_bytes
                + 256
            )
            extra = size - self.metadata_size
            if self.registry.can_fit(self.session_id, extra):
                self.registry.metadata_bytes += extra
                self.metadata_size = size
                return
            if not self.registry.make_room(self.session_id, extra):
                raise StreamResyncError("resource_limit")
            # Eviction can change floor metadata. Recompute before charging.

    def discard(self) -> None:
        while self.log:
            self.release(self.log.popleft())
        self.registry.metadata_bytes -= self.metadata_size
        self.metadata_size = 0
        self.signal_definition = None
        self.signal_definition_bytes = 0

    def publish(self, channel: Channel, value: dict[str, Any], **position: Any) -> None:
        payload = _json(value)
        if channel == "messages":
            self.message_index += 1
            index = self.message_index
        else:
            index = value["seq"]
        size = len(payload.encode()) + len(_json(position).encode()) + 128
        if not self.registry.make_room(self.session_id, size):
            raise StreamResyncError("resource_limit")
        item = _Delivery(channel, index, payload, size, **position)
        self.retained_bytes += size
        self.registry.payload_bytes += size
        self.log.append(item)
        for sub in self.subscribers:
            sub.offer(item)

    async def _scalar(self, query: str) -> Any:
        async with self.db.execute(query) as cursor:
            row = await cursor.fetchone()
        return row[0]

    def _cache_signal_definition(self, definition: str | None) -> None:
        # Logical serialized UTF-8 bytes, consistent with frontier accounting;
        # this does not represent a bound on Python object/RSS overhead.
        size = len(_json(definition).encode("utf-8")) if definition is not None else 0
        extra = size - self.signal_definition_bytes
        if not self.registry.make_room(self.session_id, extra):
            raise StreamResyncError("resource_limit")
        # Charge before retaining the value. A rejected admission cannot leave
        # an uncharged schema string on this retained owner.
        self.registry.metadata_bytes += extra
        self.metadata_size += extra
        self.signal_definition_bytes = size
        self.signal_definition = definition

    async def _signal_table_definition(self) -> str | None:
        async with self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_signals'"
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row is not None else None

    async def _revalidate_signal_capability(self) -> None:
        cookie = await self._scalar("PRAGMA schema_version")
        if cookie != self.schema_version:
            if await self._signal_table_definition() != self.signal_definition:
                raise StreamResyncError("frontier_unavailable")
            # Other tables may change without invalidating this capability.
            self.schema_version = cookie

    async def start(self) -> None:
        async with self.io_lock:
            self.error = None
            # data_version values are comparable only on the same connection.
            self.completed_data_version = None
            self.completed_state = None
            try:
                self.resource = open_db(self.registry.path)
                self.db = await self.resource.__aenter__()
                async with self.db.execute("PRAGMA query_only = ON"):
                    pass
                async with self.db.execute("BEGIN"):
                    pass
                try:
                    if self.schema_version is None:
                        cookie = await self._scalar("PRAGMA schema_version")
                        definition = await self._signal_table_definition()
                        self._cache_signal_definition(definition)
                        self.schema_version = cookie
                        self.has_signals = definition is not None
                    else:
                        await self._revalidate_signal_capability()
                finally:
                    with CancelScope(shield=True):
                        await self.db.rollback()
                await self._drain()
            except BaseException as exc:
                self.error = (
                    exc.reason if isinstance(exc, StreamResyncError) else "frontier_unavailable"
                )
                with CancelScope(shield=True):
                    await self._close_unlocked()
                if isinstance(exc, Exception) and not isinstance(exc, StreamResyncError):
                    raise StreamResyncError(self.error) from exc
                raise
            self.task = asyncio.create_task(self.run(), name="session-tail")

    async def _close_unlocked(self) -> None:
        if self.resource is not None:
            resource = self.resource
            try:
                await resource.__aexit__(None, None, None)
            finally:
                self.resource = None
                self.db = None

    async def _close(self) -> None:
        async with self.io_lock:
            await self._close_unlocked()

    async def stop(self) -> None:
        task = self.task
        try:
            if task is not None:
                # Do not cancel a task already closing its resource. Keep task
                # ownership visible until close really completes.
                if not self.closing and not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            await self._close()
            if self.task is task:
                self.task = None

    async def admit(self, sub: _Subscriber, generation: str | None) -> None:
        async with self.io_lock:
            if self.db is None or self.error is not None:
                raise StreamResyncError(self.error or "frontier_unavailable")
            async with self.db.execute("BEGIN"):
                pass
            try:
                await self._revalidate_signal_capability()
                current = await self._collections()
                _validate(self.positions, current)
                _validate(sub.bootstrap.frontiers, current)
                if sub.channel == "signals":
                    maximum = 0
                    if self.has_signals:
                        async with self.db.execute(
                            "SELECT COALESCE(MAX(seq),0) FROM session_signals WHERE session_id=?",
                            (self.session_id,),
                        ) as cursor:
                            maximum = (await cursor.fetchone())[0]
                    if sub.starting_seq > maximum:
                        raise StreamResyncError("frontier_unavailable")
            finally:
                with CancelScope(shield=True):
                    await self.db.rollback()
            self.check_retention(
                sub.bootstrap,
                sub.channel,
                sub.after_index if generation is not None else None,
                sub.starting_seq,
                generation,
            )
            if sub.control is not None:
                raise StreamResyncError(self.error or "frontier_unavailable")
            sub.ready = True
            for item in self.log:
                sub.offer(item)
            if sub.control is not None:
                raise StreamResyncError("overflow")

    async def run(self) -> None:
        try:
            while self.subscribers:
                progressed, done = await self.drain()
                if done:
                    for sub in self.subscribers:
                        if sub.ready:
                            sub.finished = True
                            sub.changed.set()
                    if all(sub.ready for sub in self.subscribers):
                        return
                    # An admission waiting on the query lock must finish before
                    # an empty terminal tail can relinquish the resource.
                    await sleep(0)
                elif not progressed:
                    await sleep(POLL_SECONDS)
        except StreamResyncError as exc:
            self.error = exc.reason
            for sub in self.subscribers:
                sub.resync(exc.reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error = "frontier_unavailable"
            for sub in self.subscribers:
                sub.resync(self.error)
        finally:
            self.closing = True
            try:
                with CancelScope(shield=True):
                    await self._close()
            except BaseException:
                self.error = self.error or "frontier_unavailable"
                raise
            finally:
                if self.task is asyncio.current_task():
                    self.task = None
                self.closing = False
                self.touched = time.monotonic()

    async def _collections(self) -> dict[str, tuple[str | None, list[str]]]:
        async with self.db.execute(
            "SELECT b.id,b.progression_id,p.collection FROM branches b "
            "LEFT JOIN progressions p ON p.id=b.progression_id "
            "WHERE b.session_id=? ORDER BY b.id",
            (self.session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        result = {}
        for row in rows:
            try:
                ids = json.loads(row["collection"]) if row["collection"] else []
            except (TypeError, ValueError) as exc:
                raise StreamResyncError("frontier_unavailable") from exc
            if not isinstance(ids, list) or any(not isinstance(x, str) or not x for x in ids):
                raise StreamResyncError("frontier_unavailable")
            result[row["id"]] = (row["progression_id"], ids)
        return result

    async def _messages(self, current):
        from . import sessions

        candidates = []
        for branch_id, (progression_id, ids) in current.items():
            old = self.positions.get(branch_id, _Frontier(progression_id, 0, None))
            for position in range(old.length, len(ids)):
                candidates.append([branch_id, position, ids[position]])
                if len(candidates) >= min(TAIL_BATCH_ROWS, sessions.MAX_HYDRATED_ROWS):
                    break
            if len(candidates) >= min(TAIL_BATCH_ROWS, sessions.MAX_HYDRATED_ROWS):
                break
        if not candidates:
            return [], False, dict(self.positions)
        budget = sessions._HydrationBudget()
        values = []
        sizes = {}
        async with self.db.execute(
            f"SELECT m.id,m.created_at,m.sender,m.role,{sessions._BOUNDED_CONTENT_COLUMNS}, "  # noqa: S608
            "mt.lion_class AS lion_class_str, CAST(c.key AS INTEGER) AS candidate_index "
            "FROM json_each(?) c JOIN messages m ON m.id=json_extract(c.value,'$[2]') "
            "LEFT JOIN message_types mt ON m.lion_class=mt.type_id ORDER BY CAST(c.key AS INTEGER)",
            (
                sessions.MAX_ACTION_CONTENT_CHARS,
                sessions.MAX_ACTION_CONTENT_CHARS,
                sessions.MAX_ACTION_CONTENT_CHARS,
                _json(candidates),
            ),
        ) as cursor:
            while True:
                row = await cursor.fetchone()
                if row is None:
                    end = len(candidates)
                    break
                index = row["candidate_index"]
                admitted = budget.admits(int(row["content_length"] or 0))
                if not admitted and values:
                    end = index
                    break
                message = sessions._format_message(row)
                branch_id, position, _ = candidates[index]
                message["branch_id"] = branch_id
                values.append((message, branch_id, position))
                sizes[message["id"]] = int(row["content_bytes"] or 0)
                if not admitted:
                    end = index + 1
                    break
        # Missing message rows are pruned membership, not a permanently stuck tail.
        pending_positions = dict(self.positions)
        for branch_id, position, message_id in candidates[:end]:
            pending_positions[branch_id] = _Frontier(
                current[branch_id][0], position + 1, message_id
            )
        messages = [value[0] for value in values]
        links = await sessions._fetch_action_link_ids(
            self.db, sessions._withheld_sizes(messages, sizes), budget
        )
        for message in messages:
            message.update(links.get(message["id"], {}))
        return values, bool(end), pending_positions

    async def drain(self) -> tuple[bool, bool]:
        async with self.io_lock:
            return await self._drain()

    async def _drain(self) -> tuple[bool, bool]:
        from . import sessions, signals

        before = await self._scalar("PRAGMA data_version")
        if before == self.completed_data_version and self.completed_state is not None:
            # Another connection's schema commit changes data_version. A stable
            # cache-hit return can reuse the completed capability validation.
            state = self.completed_state
            candidate = sessions.is_session_stream_done(state, now=time.time())
            after = await self._scalar("PRAGMA data_version")
            if before != after or candidate:
                # A version change needs fresh capability validation. Time can
                # also make cached state a done candidate; only that candidate
                # needs the own-session frontier and terminal predicates.
                self.completed_data_version = None
                self.completed_state = None
                terminal = await self._confirm_terminal(
                    state, self.positions, self.signal_seq, candidate=candidate
                )
                return before != after or not terminal, terminal
            return False, False
        self.completed_data_version = None
        self.completed_state = None
        async with self.db.execute("BEGIN"):
            pass
        try:
            await self._revalidate_signal_capability()
            current = await self._collections()
            _validate(self.positions, current)
            for sub in self.subscribers:
                _validate(sub.bootstrap.frontiers, current)
            message_rows, progressed, pending_positions = await self._messages(current)
            signal_rows = (
                await signals._read_signals(
                    self.db, self.session_id, self.signal_seq, limit=min(TAIL_BATCH_ROWS, 500)
                )
                if self.has_signals
                else []
            )
            pending_signal_seq = signal_rows[-1]["seq"] if signal_rows else self.signal_seq
            async with self.db.execute(
                "SELECT updated_at,status FROM sessions WHERE id=?", (self.session_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise StreamResyncError("frontier_unavailable")
            state = {"updated_at": row["updated_at"] or 0.0, "status": row["status"] or "completed"}
            for branch_id, (progression_id, _ids) in current.items():
                pending_positions.setdefault(branch_id, _Frontier(progression_id, 0, None))
            await self.db.commit()
        except BaseException:
            with CancelScope(shield=True):
                await self.db.rollback()
            raise
        after = await self._scalar("PRAGMA data_version")
        progressed = progressed or bool(signal_rows)
        terminal = not progressed and sessions.is_session_stream_done(state, now=time.time())
        if before != after:
            # The initial snapshot's capability check cannot cover a commit
            # between the version fences. Revalidate for before != after; only
            # terminal candidates need the own-session confirmation predicates.
            terminal = await self._confirm_terminal(
                state, pending_positions, pending_signal_seq, candidate=terminal
            )
        # No retention/accounting mutation spans an await: other sessions may
        # evict retained payloads while this connection is reading its snapshot.
        self.resize_metadata(pending_positions)
        self.positions = pending_positions
        self.signal_seq = pending_signal_seq
        for value, branch_id, position in message_rows:
            self.publish(
                "messages",
                value,
                branch_id=branch_id,
                position=position,
                message_id=value["id"],
                progression_id=current[branch_id][0],
            )
        for value in signal_rows:
            self.publish("signals", value)
        if not progressed and before == after:
            # A progressed batch may leave a suffix behind. Only a completed,
            # empty stable drain permits subsequent idle reads to be skipped.
            self.completed_data_version = after
            self.completed_state = state
        return progressed or before != after, terminal

    async def _confirm_terminal(
        self,
        state: dict[str, Any],
        positions: dict[str, _Frontier],
        signal_seq: int,
        *,
        candidate: bool,
    ) -> bool:
        from . import sessions

        async with self.db.execute("BEGIN"):
            pass
        try:
            # This first contributing table read pins the confirmation snapshot.
            # Commits after that point are outside this terminal decision.
            async with self.db.execute(
                "SELECT updated_at,status FROM sessions WHERE id=?", (self.session_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise StreamResyncError("frontier_unavailable")
            await self._revalidate_signal_capability()
            if not candidate:
                return False
            confirmed_state = {
                "updated_at": row["updated_at"] or 0.0,
                "status": row["status"] or "completed",
            }
            current = await self._collections()
            _validate(positions, current)
            frontiers = {
                branch_id: _Frontier(progression_id, len(ids), ids[-1] if ids else None)
                for branch_id, (progression_id, ids) in current.items()
            }
            if frontiers != positions or confirmed_state != state:
                return False
            if self.has_signals:
                async with self.db.execute(
                    "SELECT 1 FROM session_signals WHERE session_id=? AND seq>? LIMIT 1",
                    (self.session_id, signal_seq),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        return False
            return sessions.is_session_stream_done(confirmed_state, now=time.time())
        finally:
            with CancelScope(shield=True):
                await self.db.rollback()
