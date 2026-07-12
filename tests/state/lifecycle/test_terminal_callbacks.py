# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The post-commit TerminalCallbackRegistry hooked onto the guarded
lifecycle transition, its envelope shape, and the terminal_deliveries
reconciliation ledger."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import (
    ActorRecord,
    LifecycleValidationError,
    ReasonRecord,
    TransitionCommand,
)
from lionagi.state.lifecycle.callbacks import (
    EntityRef,
    RunTerminalEnvelope,
    TerminalCallbackRegistry,
)
from lionagi.state.lifecycle.deliveries import (
    ack_delivery,
    is_acknowledged,
    reconcile_unacknowledged,
)
from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService

# ── Fixtures (mirrors tests/state/lifecycle/test_service.py) ─────────────────


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


def _uid() -> str:
    return str(uuid.uuid4())


async def _make_session(db: StateDB, *, status: str = "running") -> str:
    prog_id = _uid()
    await db.create_progression(prog_id)
    sid = _uid()
    await db.create_session({"id": sid, "progression_id": prog_id, "status": status})
    return sid


async def _make_schedule_run(db: StateDB, *, status: str = "queued") -> str:
    sched_id = _uid()
    await db.create_schedule(
        {
            "id": sched_id,
            "name": f"sched-{sched_id}",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    run_id = _uid()
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": sched_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": status,
            "fired_at": time.time(),
        }
    )
    return run_id


def _command(**overrides) -> TransitionCommand:
    base = dict(
        entity_type="session",
        entity_id="",
        to_status="completed",
        reason=ReasonRecord(code="session.stale.no_heartbeat"),
        actor=ActorRecord(type="executor", id="executor"),
    )
    base.update(overrides)
    return TransitionCommand(**base)


# ── Registry mechanics ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_emits_only_to_matching_kind_and_id():
    registry = TerminalCallbackRegistry()
    hits: list[str] = []

    registry.register("a", lambda env: hits.append("a"), kinds=["session"])
    registry.register("b", lambda env: hits.append("b"), kinds=["invocation"])
    registry.register("c", lambda env: hits.append("c"), ids=["only-this-one"])

    envelope = RunTerminalEnvelope(
        event_id="ev1",
        entity=EntityRef(kind="session", id="sess-1"),
        previous_status="running",
        terminal_status="completed",
        reason_code="run.completed.ok",
        occurred_at=time.time(),
    )
    await registry.emit(envelope)

    assert hits == ["a"]


@pytest.mark.asyncio
async def test_registry_is_idempotent_by_name():
    registry = TerminalCallbackRegistry()
    calls: list[int] = []

    registry.register("dup", lambda env: calls.append(1))
    registry.register("dup", lambda env: calls.append(2))  # replaces, not adds

    assert len(registry._registrations) == 1

    envelope = RunTerminalEnvelope(
        event_id="ev2",
        entity=EntityRef(kind="session", id="s"),
        previous_status="running",
        terminal_status="completed",
        reason_code="run.completed.ok",
        occurred_at=time.time(),
    )
    await registry.emit(envelope)
    assert calls == [2]


@pytest.mark.asyncio
async def test_registry_swallows_handler_exception_and_still_runs_others():
    registry = TerminalCallbackRegistry()
    ran: list[str] = []

    def _boom(env):
        raise RuntimeError("handler blew up")

    def _ok(env):
        ran.append("ok")

    registry.register("boom", _boom)
    registry.register("ok", _ok)

    envelope = RunTerminalEnvelope(
        event_id="ev3",
        entity=EntityRef(kind="session", id="s"),
        previous_status="running",
        terminal_status="completed",
        reason_code="run.completed.ok",
        occurred_at=time.time(),
    )
    # Must not raise.
    await registry.emit(envelope)
    assert ran == ["ok"]


@pytest.mark.asyncio
async def test_hanging_handler_does_not_starve_a_successful_one_and_is_bounded():
    # Verification item 3: one hanging, one successful handler -- the
    # successful handler is not starved, and total delay is bounded by the
    # shared budget rather than the hang.
    registry = TerminalCallbackRegistry(budget_seconds=0.2)
    ran: list[str] = []

    async def _hang(env):
        await asyncio.sleep(10)  # far longer than the budget
        ran.append("hung-completed")  # should never append

    async def _fast(env):
        ran.append("fast")

    registry.register("hang", _hang)
    registry.register("fast", _fast)

    envelope = RunTerminalEnvelope(
        event_id="ev4",
        entity=EntityRef(kind="session", id="s"),
        previous_status="running",
        terminal_status="completed",
        reason_code="run.completed.ok",
        occurred_at=time.time(),
    )

    start = time.monotonic()
    await registry.emit(envelope)
    elapsed = time.monotonic() - start

    assert "fast" in ran
    assert "hung-completed" not in ran
    # Bounded well under the hang's 10s sleep.
    assert elapsed < 5.0


# ── Lifecycle-service integration (D1 hook point) ────────────────────────────


@pytest.mark.asyncio
async def test_terminal_transition_fires_registered_handler_exactly_once(db: StateDB):
    registry = TerminalCallbackRegistry()
    received: list[RunTerminalEnvelope] = []
    registry.register("collector", lambda env: received.append(env))

    service = SQLAlchemyLifecycleService(db, terminal_callbacks=registry)
    sid = await _make_session(db, status="running")

    outcome = await service.transition(_command(entity_id=sid, to_status="completed"))

    assert outcome.result == "applied"
    assert len(received) == 1
    envelope = received[0]
    assert envelope.event_id == outcome.transition_id
    assert envelope.entity.kind == "session"
    assert envelope.entity.id == sid
    assert envelope.previous_status == "running"
    assert envelope.terminal_status == "completed"
    assert envelope.durable is True
    assert envelope.schema == "lionagi.run-terminal"
    assert envelope.schema_version == 1
    assert envelope.correlation.session_id == sid
    assert envelope.artifacts == ()


@pytest.mark.asyncio
async def test_nonterminal_transition_does_not_fire(db: StateDB):
    # schedule_run's queued -> running edge lands on a declared nonterminal
    # status; the registry must not receive an envelope for it.
    registry = TerminalCallbackRegistry()
    received: list[RunTerminalEnvelope] = []
    registry.register("collector", lambda env: received.append(env))
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=registry)
    run_id = await _make_schedule_run(db, status="queued")

    outcome = await service.transition(
        TransitionCommand(
            entity_type="schedule_run",
            entity_id=run_id,
            to_status="running",
            reason=ReasonRecord(code="run.started.ok"),
            actor=ActorRecord(type="scheduler", id="scheduler"),
        )
    )

    assert outcome.result == "applied"
    assert received == []

    with pytest.raises(LifecycleValidationError):
        await service.transition(_command(entity_id=run_id, to_status="not-a-real-status"))
    assert received == []


@pytest.mark.asyncio
async def test_same_status_append_is_not_a_new_terminal_event(db: StateDB):
    registry = TerminalCallbackRegistry()
    received: list[RunTerminalEnvelope] = []
    registry.register("collector", lambda env: received.append(env))
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=registry)
    sid = await _make_session(db, status="running")

    first = await service.transition(_command(entity_id=sid, to_status="completed"))
    assert first.result == "applied"
    assert len(received) == 1

    # A same-status reason append (session policy's same_status="append")
    # must not emit a second terminal event.
    second = await service.transition(
        _command(
            entity_id=sid,
            to_status="completed",
            reason=ReasonRecord(code="session.stale.no_heartbeat"),
        )
    )
    assert second.result == "applied"
    assert len(received) == 1


@pytest.mark.asyncio
async def test_conflict_outcome_never_fires(db: StateDB):
    registry = TerminalCallbackRegistry()
    received: list[RunTerminalEnvelope] = []
    registry.register("collector", lambda env: received.append(env))
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=registry)
    sid = await _make_session(db, status="running")

    outcome = await service.transition(
        _command(
            entity_id=sid,
            to_status="completed",
            expected_statuses=frozenset({"failed"}),
        )
    )
    assert outcome.result == "conflict"
    assert received == []


@pytest.mark.asyncio
async def test_handler_failure_never_changes_persisted_status(db: StateDB):
    registry = TerminalCallbackRegistry()

    def _boom(env):
        raise RuntimeError("simulated handler crash")

    registry.register("boom", _boom)
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=registry)
    sid = await _make_session(db, status="running")

    outcome = await service.transition(_command(entity_id=sid, to_status="completed"))

    assert outcome.result == "applied"
    row = await db.fetch_one("SELECT status FROM sessions WHERE id = :id", {"id": sid})
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_no_registered_handler_is_a_noop(db: StateDB):
    # Default registry with nothing registered for this test's session id
    # must not raise or otherwise affect the transition.
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=TerminalCallbackRegistry())
    sid = await _make_session(db, status="running")
    outcome = await service.transition(_command(entity_id=sid, to_status="completed"))
    assert outcome.result == "applied"


# ── terminal_deliveries reconciliation (1b, 1b-i, 1b-ii) ─────────────────────


@pytest.mark.asyncio
async def test_reconcile_unacknowledged_returns_unacked_terminal_events(db: StateDB):
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=TerminalCallbackRegistry())
    sid = await _make_session(db, status="running")
    outcome = await service.transition(_command(entity_id=sid, to_status="completed"))

    pending = await reconcile_unacknowledged(db, "consumer-x")
    ids = {row["transition_id"] for row in pending}
    assert outcome.transition_id in ids

    await ack_delivery(db, outcome.transition_id, "consumer-x")

    pending_after = await reconcile_unacknowledged(db, "consumer-x")
    ids_after = {row["transition_id"] for row in pending_after}
    assert outcome.transition_id not in ids_after
    assert await is_acknowledged(db, outcome.transition_id, "consumer-x")


@pytest.mark.asyncio
async def test_reconcile_is_per_consumer(db: StateDB):
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=TerminalCallbackRegistry())
    sid = await _make_session(db, status="running")
    outcome = await service.transition(_command(entity_id=sid, to_status="completed"))

    await ack_delivery(db, outcome.transition_id, "consumer-a")

    # A different registered consumer's set is independent -- acking as
    # consumer-a must not affect consumer-b's unacknowledged set.
    pending_b = await reconcile_unacknowledged(db, "consumer-b")
    assert outcome.transition_id in {row["transition_id"] for row in pending_b}


@pytest.mark.asyncio
async def test_offline_longer_than_any_horizon_still_reconciles(db: StateDB):
    # Verification item 1b-i: a registered consumer that simply never
    # queries carries no retention cutoff on its unacknowledged set -- an
    # old terminal event, however old, is still returned. Simulated here by
    # backdating created_at on the status_transitions row and asserting the
    # reconciliation query applies no age filter.
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=TerminalCallbackRegistry())
    sid = await _make_session(db, status="running")
    outcome = await service.transition(_command(entity_id=sid, to_status="completed"))

    ancient = time.time() - (400 * 24 * 3600)  # ~400 days old
    await db.execute(
        "UPDATE status_transitions SET created_at = :ts WHERE id = :id",
        {"ts": ancient, "id": outcome.transition_id},
    )

    pending = await reconcile_unacknowledged(db, "long-offline-consumer")
    ids = {row["transition_id"] for row in pending}
    assert outcome.transition_id in ids


@pytest.mark.asyncio
async def test_parallel_ack_by_same_consumer_is_single_row_and_errorless(db: StateDB):
    # Verification item 1b-ii.
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=TerminalCallbackRegistry())
    sid = await _make_session(db, status="running")
    outcome = await service.transition(_command(entity_id=sid, to_status="completed"))

    await asyncio.gather(
        ack_delivery(db, outcome.transition_id, "consumer-race"),
        ack_delivery(db, outcome.transition_id, "consumer-race"),
    )

    rows = await db.fetch_all(
        "SELECT * FROM terminal_deliveries WHERE transition_id = :id AND consumer = :c",
        {"id": outcome.transition_id, "c": "consumer-race"},
    )
    assert len(rows) == 1
