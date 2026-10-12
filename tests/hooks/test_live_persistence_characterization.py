# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Characterization of live message persistence through ``MESSAGE_ADD``.

Pins what the current ordered writer does, so a later change to its shape can
prove the observable behavior is unchanged: the order rows land in, what the
store does with a write it refuses (both a retryable failure and a message it
rejects outright), and what the teardown flush leaves behind.

Some of these facts have no public surface (the retry-queue registry, the
emitting-bus context, the branch's emit hook and the store's persistence
internals), so those tests read private names, and renaming one fails them on
shape rather than on behavior.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from lionagi import Branch, Session
from lionagi.hooks import (
    HookBus,
    HookPoint,
    build_session_bus,
    route_message_persistence,
    unroute_message_persistence,
)
from lionagi.hooks._message_retry import MessagePersistRetryQueue, PendingMessageEvent
from lionagi.hooks.builtins import persist_message
from lionagi.hooks.bus import _current_emitting_bus

SESSION_ID = "s1"
BRANCH_ID = "b1"
BRANCH_PROG_ID = "bp1"
SESSION_PROG_ID = "sp1"


def _db_message(msg_id: str, role: str = "user") -> dict[str, Any]:
    """A message dict shaped as the state store's write path expects it."""
    return {
        "id": msg_id,
        "created_at": 100.0,
        "node_metadata": {},
        "content": {"text": "x"},
        "embedding": None,
        "sender": "u",
        "recipient": "b",
        "channel": None,
        "role": role,
    }


def _event(msg_id: str) -> PendingMessageEvent:
    return PendingMessageEvent(message=_db_message(msg_id), session_id=SESSION_ID)


async def _stub_shared_db(monkeypatch):
    """Point ``get_shared_db`` at a fresh in-memory store and seed its rows."""
    from lionagi.state.db import StateDB

    db = StateDB(":memory:")
    await db.open()

    async def _get_shared_db(*_args: Any, **_kwargs: Any) -> StateDB:
        return db

    monkeypatch.setattr("lionagi.state.db.get_shared_db", _get_shared_db)
    await db.create_progression(SESSION_PROG_ID)
    await db.create_progression(BRANCH_PROG_ID)
    await db.create_session(
        {"id": SESSION_ID, "progression_id": SESSION_PROG_ID, "status": "running"}
    )
    await db.create_branch(
        {"id": BRANCH_ID, "session_id": SESSION_ID, "progression_id": BRANCH_PROG_ID}
    )
    return db


async def _emit(bus: HookBus, msg_id: str) -> None:
    await bus.emit(
        HookPoint.MESSAGE_ADD,
        message=_db_message(msg_id),
        session_id=SESSION_ID,
        branch_id=BRANCH_ID,
        branch_progression_id=BRANCH_PROG_ID,
        session_progression_id=SESSION_PROG_ID,
    )


class _RefusingDB:
    """Refuses every write, the way a contended sqlite does."""

    def __init__(self) -> None:
        self.attempts = 0
        self.persisted: list[str] = []
        self.refuse = True

    async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
        self.attempts += 1
        if self.refuse:
            raise RuntimeError("database is locked")
        self.persisted.append(message["id"])


# ── writes land in message order ──────────────────────────────────────────────


async def test_message_add_persists_messages_in_emission_order(monkeypatch):
    """Three emissions land in both progressions in the order they were emitted."""
    db = await _stub_shared_db(monkeypatch)
    try:
        bus = build_session_bus({"message.add": ["persist_message"]})
        for msg_id in ("m1", "m2", "m3"):
            await _emit(bus, msg_id)

        assert await db.get_progression(BRANCH_PROG_ID) == ["m1", "m2", "m3"]
        assert await db.get_progression(SESSION_PROG_ID) == ["m1", "m2", "m3"]
        for msg_id in ("m1", "m2", "m3"):
            assert await db.get_message(msg_id) is not None
    finally:
        await db.close()


async def test_the_bus_keeps_one_retry_queue_per_stream(monkeypatch):
    """Every queued emission for one (db, session, branch, progressions) stream
    shares a single ordered queue, so ordering is a property of the stream.

    The queue registry is private and no public surface exposes it; the key list
    and the object identity below are the only observation of it.
    """
    db = await _stub_shared_db(monkeypatch)
    try:
        bus = build_session_bus({"message.add": ["persist_message"]})
        await _emit(bus, "m1")
        first_queue = next(iter(bus._message_retry_queues.values()))
        await _emit(bus, "m2")
        second_queue = next(iter(bus._message_retry_queues.values()))

        assert list(bus._message_retry_queues) == [
            (id(db), SESSION_ID, BRANCH_ID, BRANCH_PROG_ID, SESSION_PROG_ID)
        ]
        assert second_queue is first_queue, (
            "the second emission must reuse the queue the first one created; a fresh "
            "queue cannot see the backlog it has to order against"
        )
    finally:
        await db.close()


async def test_persist_message_outside_a_bus_writes_directly(monkeypatch):
    """A direct call with no bus emitting writes the row before it returns."""
    db = await _stub_shared_db(monkeypatch)
    try:
        await persist_message(
            message=_db_message("m1"),
            session_id=SESSION_ID,
            branch_progression_id=BRANCH_PROG_ID,
        )

        assert await db.get_progression(BRANCH_PROG_ID) == ["m1"]
        assert await db.get_message("m1") is not None
    finally:
        await db.close()


async def test_branch_message_add_emits_in_order_through_the_routed_writer():
    """A branch's own ``a_add_message`` drives the routed writer once per message,
    in order, and each emit carries that branch's id."""
    branch = Branch(name="b1")
    session = Session(default_branch=branch)
    bus = session.hooks
    seen: list[str] = []
    emitted_branch_ids: list[str | None] = []

    async def _on_message(message: Any = None, **_kwargs: Any) -> None:
        seen.append(str(message.id))

    async def _spy(*, branch_id: str | None = None, **_kwargs: Any) -> None:
        emitted_branch_ids.append(branch_id)

    handler = route_message_persistence(session, branch, _on_message)
    bus.on(HookPoint.MESSAGE_ADD, _spy)
    first = await branch.msgs.a_add_message(instruction="a")
    second = await branch.msgs.a_add_message(instruction="b")

    assert seen == [str(first.id), str(second.id)]
    assert emitted_branch_ids == [str(branch.id), str(branch.id)]

    unroute_message_persistence(branch, handler)
    await branch.msgs.a_add_message(instruction="c")
    assert seen == [str(first.id), str(second.id)]

    await branch.drain_signals()


async def test_one_routing_call_leaves_one_delivery_path():
    """One routing call replaces the built-in handler on the session bus, and a
    message is emitted once and reaches the callback once."""
    branch = Branch(name="b1")
    session = Session(default_branch=branch)
    bus = session.hooks
    bus.on(HookPoint.MESSAGE_ADD, persist_message)
    deliveries: list[str] = []
    emits: list[str | None] = []

    async def _on_message(message: Any = None, **_kwargs: Any) -> None:
        deliveries.append(str(message.id))

    async def _spy(*, branch_id: str | None = None, **_kwargs: Any) -> None:
        emits.append(branch_id)

    handler = route_message_persistence(session, branch, _on_message)
    bus.on(HookPoint.MESSAGE_ADD, _spy)
    assert bus.handlers_for(HookPoint.MESSAGE_ADD) == [handler, _spy]
    assert persist_message not in bus.handlers_for(HookPoint.MESSAGE_ADD)

    message = await branch.msgs.a_add_message(instruction="a")

    assert emits == [str(branch.id)], "one emit per message"
    assert deliveries == [str(message.id)], "one delivery per message"
    await branch.drain_signals()


async def test_a_second_routing_call_adds_a_second_delivery_path():
    """Routing twice de-duplicates the branch emit hook but registers the per-branch
    demux handler unconditionally, so the bus holds both demux handlers and one
    message reaches the callback once per routing call.

    Pinned as current behavior, not asserted as correct, as is what follows from
    it: tearing down the first routing removes its handler and the emit hook, so
    the second routing's handler stays registered with nothing emitting to it.
    """
    branch = Branch(name="b1")
    session = Session(default_branch=branch)
    bus = session.hooks
    deliveries: list[str] = []
    emits: list[str | None] = []

    async def _on_message(message: Any = None, **_kwargs: Any) -> None:
        deliveries.append(str(message.id))

    async def _spy(*, branch_id: str | None = None, **_kwargs: Any) -> None:
        emits.append(branch_id)

    first = route_message_persistence(session, branch, _on_message)
    second = route_message_persistence(session, branch, _on_message)
    bus.on(HookPoint.MESSAGE_ADD, _spy)
    assert len(bus.handlers_for(HookPoint.MESSAGE_ADD)) == 3, "two demux handlers + spy"

    message = await branch.msgs.a_add_message(instruction="a")

    assert emits == [str(branch.id)], "the emit hook itself is registered only once"
    assert deliveries == [str(message.id)] * 2, "one delivery per registered demux handler"
    await branch.drain_signals()

    unroute_message_persistence(branch, first)
    await branch.msgs.a_add_message(instruction="b")
    await branch.drain_signals()

    assert second in bus.handlers_for(HookPoint.MESSAGE_ADD)
    assert emits == [str(branch.id)], "nothing emits MESSAGE_ADD for the branch any more"
    assert deliveries == [str(message.id)] * 2


async def test_message_add_demuxes_by_branch_and_a_bare_emit_reaches_every_route():
    """An emission carrying a branch id reaches only that branch's writer; an
    emission without one is not filtered and reaches all of them."""
    first = Branch(name="b1")
    second = Branch(name="b2")
    session = Session(default_branch=first)
    session.include_branches(second)
    seen: list[tuple[str, str]] = []

    async def _on_first(message: Any = None, **_kwargs: Any) -> None:
        seen.append(("b1", str(message["id"])))

    async def _on_second(message: Any = None, **_kwargs: Any) -> None:
        seen.append(("b2", str(message["id"])))

    route_message_persistence(session, first, _on_first)
    route_message_persistence(session, second, _on_second)
    bus = session.hooks

    await bus.emit(HookPoint.MESSAGE_ADD, branch_id=str(first.id), message={"id": "x"})
    assert seen == [("b1", "x")]

    await bus.emit(HookPoint.MESSAGE_ADD, branch_id=str(second.id), message={"id": "y"})
    assert seen == [("b1", "x"), ("b2", "y")]

    await bus.emit(HookPoint.MESSAGE_ADD, message={"id": "z"})
    assert seen[-2:] == [("b1", "z"), ("b2", "z")]


async def test_a_failing_routed_writer_does_not_abort_the_branch(caplog):
    """``MESSAGE_ADD`` is observational: a writer that raises is logged, and the
    message still returns to its caller."""
    branch = Branch(name="b1")
    session = Session(default_branch=branch)

    async def _boom(message: Any = None, **_kwargs: Any) -> None:
        raise RuntimeError("writer blew up")

    route_message_persistence(session, branch, _boom)
    with caplog.at_level(logging.ERROR, logger="lionagi.hooks"):
        message = await branch.msgs.a_add_message(instruction="a")

    assert message is not None
    assert any(
        record.levelno >= logging.ERROR and "message.add" in record.message
        for record in caplog.records
    ), "the swallowed writer failure is logged, not dropped silently"
    await branch.drain_signals()


# ── a write the store refuses: retry, deferral, surviving rows ────────────────


async def test_a_refused_write_leaves_no_rows_and_stays_pending(monkeypatch):
    """A failure on the second progression update rolls the whole bundle back:
    no message row, neither progression advanced, one event still queued."""
    from sqlalchemy import event

    db = await _stub_shared_db(monkeypatch)
    try:
        bus = build_session_bus({"message.add": ["persist_message"]})
        progression_updates = 0

        def _fail_second_progression(conn, cursor, statement, parameters, context, executemany):
            nonlocal progression_updates
            if statement.lstrip().startswith("UPDATE progressions"):
                progression_updates += 1
                if progression_updates == 2:
                    raise RuntimeError("injected middle progression failure")

        event.listen(db._engine.sync_engine, "before_cursor_execute", _fail_second_progression)
        try:
            await _emit(bus, "lost")
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", _fail_second_progression)

        assert await db.get_message("lost") is None
        assert await db.get_progression(BRANCH_PROG_ID) == []
        assert await db.get_progression(SESSION_PROG_ID) == []
        # The queue registry is private; the pending backlog is the only observation.
        queue = next(iter(bus._message_retry_queues.values()))
        assert queue.pending_count == 1
    finally:
        await db.close()


async def test_the_store_rejects_a_malformed_message_and_the_queue_drops_it(monkeypatch, caplog):
    """A message the store rejects outright (it validates content and role) raises
    ``ValueError`` and writes no rows; the queue drops that event with a warning
    rather than leaving it at the head, where every message behind it would be
    blocked.

    The retryable refusal in the test above stays queued instead: only a
    deterministic rejection, which would fail identically on every retry, is dropped.
    """
    db = await _stub_shared_db(monkeypatch)
    try:
        bus = build_session_bus({"message.add": ["persist_message"]})
        malformed = _db_message("bad-1")
        malformed["content"] = None

        with caplog.at_level(logging.WARNING, logger="lionagi.hooks.builtins"):
            await bus.emit(
                HookPoint.MESSAGE_ADD,
                message=malformed,
                session_id=SESSION_ID,
                branch_id=BRANCH_ID,
                branch_progression_id=BRANCH_PROG_ID,
                session_progression_id=SESSION_PROG_ID,
            )
            await _emit(bus, "good-1")

        assert await db.get_message("bad-1") is None
        assert await db.get_message("good-1") is not None
        assert await db.get_progression(BRANCH_PROG_ID) == ["good-1"]
        assert await db.get_progression(SESSION_PROG_ID) == ["good-1"]
        queue = next(iter(bus._message_retry_queues.values()))
        assert queue.pending_count == 0
        assert any("dropping malformed message" in record.message for record in caplog.records), (
            "the dropped event is reported, not silently discarded"
        )
    finally:
        await db.close()


async def test_the_queued_event_commits_ahead_of_the_next_message(monkeypatch):
    """After the store recovers, the queued event commits before the later one,
    so both are in emission order."""
    db = await _stub_shared_db(monkeypatch)
    try:
        bus = build_session_bus({"message.add": ["persist_message"]})
        queue = None
        # The store's write path is private; it is patched to fail once so the first
        # event is still queued when the second arrives.
        original = type(db)._persist_live_message
        calls = {"n": 0}

        async def _fail_first(self, message, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated busy timeout")
            await original(self, message, **kwargs)

        monkeypatch.setattr(type(db), "_persist_live_message", _fail_first)
        await _emit(bus, "first")
        queue = next(iter(bus._message_retry_queues.values()))
        assert queue.pending_count == 1

        await _emit(bus, "second")

        assert await db.get_progression(BRANCH_PROG_ID) == ["first", "second"]
        assert await db.get_progression(SESSION_PROG_ID) == ["first", "second"]
        assert queue.pending_count == 0
    finally:
        await db.close()


async def test_retry_defers_after_three_consecutive_failures_and_stops_attempting():
    """Three consecutive refusals defer the queue; later submissions queue without
    being attempted at all."""
    db = _RefusingDB()
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")

    results = [await queue.submit(_event(f"m{i}")) for i in range(4)]

    assert results == [False, False, False, False]
    assert db.attempts == 3, "the fourth event is queued, never attempted"
    assert queue.pending_count == 4


async def test_a_deferred_queue_gets_one_more_attempt_at_teardown_then_drains_in_order():
    """Teardown is one further attempt, not a retry loop: while the store keeps
    refusing, the flush tries the head event once and leaves the backlog; the
    backlog drains in emission order only after the store recovers."""
    db = _RefusingDB()
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")
    for i in range(3):
        await queue.submit(_event(f"m{i}"))
    assert queue.pending_count == 3
    assert db.attempts == 3

    assert await queue.flush_final() is False
    assert db.attempts == 4, "one further attempt, on the head event only"
    assert queue.pending_count == 3

    db.refuse = False
    assert await queue.flush_final() is True
    assert db.persisted == ["m0", "m1", "m2"]
    assert queue.pending_count == 0


# ── the teardown flush ────────────────────────────────────────────────────────


async def test_session_end_flushes_pending_events_before_its_handlers_run():
    """``SESSION_END`` drains the bus's queues first: a handler registered for it
    observes the backlog already written."""
    db = _RefusingDB()
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")
    for i in range(3):
        await queue.submit(_event(f"m{i}"))
    db.refuse = False

    bus = HookBus()
    # The registry is private; handing the queue to the bus is the only way to make
    # the bus's own teardown see a pending backlog.
    bus._message_retry_queues = {"k": queue}
    pending_at_handler: list[int] = []

    async def _handler(**_kwargs: Any) -> None:
        pending_at_handler.append(queue.pending_count)

    bus.on(HookPoint.SESSION_END, _handler)
    await bus.emit(HookPoint.SESSION_END, session_id=SESSION_ID, status="completed")

    assert pending_at_handler == [0]
    assert db.persisted == ["m0", "m1", "m2"]


async def test_session_end_reports_the_loss_and_keeps_the_events(caplog):
    """A still-refusing store at teardown logs one error and leaves the events
    pending; the bus does not drop them to report success."""
    db = _RefusingDB()
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")
    for i in range(3):
        await queue.submit(_event(f"m{i}"))

    bus = HookBus()
    bus._message_retry_queues = {"k": queue}

    with caplog.at_level(logging.ERROR, logger="test"):
        await bus.emit(HookPoint.SESSION_END, session_id=SESSION_ID, status="completed")

    losses = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(losses) == 1
    assert queue.pending_count == 3


async def test_a_cancelled_teardown_flush_keeps_every_event_pending():
    """A teardown cancelled mid-flush has written nothing further and still holds
    the whole backlog; the cancellation propagates and the SESSION_END handlers
    behind the flush never run."""

    class _BlockingDB:
        def __init__(self) -> None:
            self.refusals = 3
            self.persisted: list[str] = []
            self.started = asyncio.Event()

        async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
            if self.refusals > 0:
                self.refusals -= 1
                raise RuntimeError("database is locked")
            self.started.set()
            await asyncio.sleep(3600)

    db = _BlockingDB()
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")

    async def _seed() -> None:
        for i in range(4):
            await queue.submit(_event(f"m{i}"))

    # The fourth submit must return without a write: three consecutive refusals
    # defer the queue. A queue that keeps attempting would block on the write the
    # fake is holding, which is the state this test is about.
    await asyncio.wait_for(_seed(), timeout=5)
    assert queue.pending_count == 4

    bus = HookBus()
    bus._message_retry_queues = {"k": queue}
    handlers_ran: list[str] = []

    async def _handler(**_kwargs: Any) -> None:
        handlers_ran.append("ran")

    bus.on(HookPoint.SESSION_END, _handler)
    teardown = asyncio.create_task(
        bus.emit(HookPoint.SESSION_END, session_id=SESSION_ID, status="completed")
    )
    await asyncio.wait_for(db.started.wait(), timeout=5)
    teardown.cancel()
    try:
        await teardown
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - the cancellation must reach the caller
        raise AssertionError("a cancelled teardown must propagate CancelledError")

    assert queue.pending_count == 4
    assert db.persisted == []
    assert handlers_ran == []


async def test_the_emitting_bus_context_is_set_and_reset_inside_the_emitting_task():
    """The bus a handler is emitted on is task-local state that ``persist_message``
    reads to choose its queued path: it is set for the handler and reset when emit
    returns, both inside the task that called emit. No public surface exposes
    this."""
    bus = HookBus()
    observed: dict[str, Any] = {}

    async def _handler(**_kwargs: Any) -> None:
        observed["in_handler"] = _current_emitting_bus()

    async def _emit_from_this_task() -> None:
        observed["before"] = _current_emitting_bus()
        await bus.emit(HookPoint.SESSION_END, session_id=SESSION_ID)
        observed["after"] = _current_emitting_bus()

    bus.on(HookPoint.SESSION_END, _handler)
    await asyncio.create_task(_emit_from_this_task())

    assert observed["before"] is None
    assert observed["in_handler"] is bus
    assert observed["after"] is None


async def test_a_cancelled_direct_write_keeps_the_event_queued():
    """Cancelling the coroutine that submits an event leaves that event queued:
    the queue pops only after a write succeeds."""

    class _BlockingDB:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
            self.started.set()
            await asyncio.sleep(3600)

    db = _BlockingDB()
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")
    submit = asyncio.create_task(queue.submit(_event("m0")))
    await asyncio.wait_for(db.started.wait(), timeout=5)
    submit.cancel()
    try:
        await submit
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - the cancellation must reach the caller
        raise AssertionError("a cancelled submit must propagate CancelledError")

    assert queue.pending_count == 1
