# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0058 Phase 2 gate: D1 result semantics and the D4 guarded transition
algorithm, exercised directly against SQLAlchemyLifecycleService (bypassing
the StateDB.update_status()/transitions.transition() compatibility wrappers
tested elsewhere)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import JSON, bindparam, text

from lionagi.state.db import SCHEMA_VERSION, StateDB, TransitionRejectedError
from lionagi.state.lifecycle import (
    ActorRecord,
    LifecycleNotFoundError,
    LifecycleValidationError,
    OverrideRecord,
    ReasonRecord,
    TransitionCommand,
)
from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService
from lionagi.state.reasons import RunReasons
from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition

# Fixtures


@pytest_asyncio.fixture
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


async def _make_schedule_run(
    db: StateDB, *, status: str = "queued", seed_chain_decision: bool = False
) -> str:
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
    row = {
        "id": run_id,
        "schedule_id": sched_id,
        "trigger_context": {},
        "action_kind": "agent",
        "action_args": [],
        "status": status,
        "fired_at": time.time(),
    }
    if seed_chain_decision:
        await db._create_schedule_run(row, seed_chain_decision=True)
    else:
        await db.create_schedule_run(row)
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


# D1: result semantics


@pytest.mark.asyncio
async def test_transition_applied_shape(db: StateDB) -> None:
    sid = await _make_session(db, status="running")
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(_command(entity_id=sid, to_status="completed"))

    assert outcome.result == "applied"
    assert outcome.previous_status == "running"
    assert outcome.current_status == "completed"
    assert outcome.transition_id is not None


@pytest.mark.asyncio
async def test_guarded_terminal_session_transition_still_stamps_end_and_duration(
    db: StateDB, monkeypatch
) -> None:
    """A guard on an end-time basis column must not disable the invariant."""
    sid = await _make_session(db, status="running")
    await db.update_session(sid, started_at=100.0)
    monkeypatch.setattr("lionagi.state.lifecycle.service.time.time", lambda: 112.5)
    service = SQLAlchemyLifecycleService(db)

    outcome = await service._transition(  # noqa: SLF001 - exercises guarded internal path
        _command(entity_id=sid, to_status="completed"),
        extra_guard={"started_at": 100.0},
    )

    assert outcome.result == "applied"
    row = await db.get_session(sid)
    assert row is not None
    assert row["ended_at"] == 112.5
    assert row["duration_ms"] == pytest.approx(12_500.0)
    assert row["ended_at_is_approximate"] is False


@pytest.mark.asyncio
async def test_transition_conflict_shape_on_expected_statuses_mismatch(db: StateDB) -> None:
    sid = await _make_session(db, status="running")
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(
        _command(
            entity_id=sid,
            to_status="completed",
            expected_statuses=frozenset({"failed"}),
        )
    )

    assert outcome.result == "conflict"
    assert outcome.previous_status == "running"
    assert outcome.current_status == "running"
    assert outcome.transition_id is None


@pytest.mark.asyncio
async def test_transition_rejected_shape_terminal_without_override(db: StateDB) -> None:
    sid = await _make_session(db, status="completed")
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(_command(entity_id=sid, to_status="running"))

    assert outcome.result == "rejected"
    assert outcome.previous_status == "completed"
    assert outcome.current_status == "completed"
    assert outcome.transition_id is None


@pytest.mark.asyncio
async def test_public_transition_rejects_unregistered_reason_code(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="queued")
    service = SQLAlchemyLifecycleService(db)

    with pytest.raises(LifecycleValidationError, match="invalid reason_code"):
        await service.transition(
            _command(
                entity_type="schedule_run",
                entity_id=run_id,
                to_status="running",
                reason=ReasonRecord(code="made.up.code"),
            )
        )


@pytest.mark.asyncio
async def test_public_transition_applies_for_dispatch_without_reason_columns(db: StateDB) -> None:
    """dispatch_outbox has no status_reason_* columns; the public path must
    skip that SET clause instead of failing at the database."""
    from lionhub.orchestration.dispatch import enqueue_dispatch, get_dispatch

    dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(
        _command(
            entity_type="dispatch",
            entity_id=dispatch_id,
            to_status="delivering",
            reason=ReasonRecord(code="dispatch.delivering.attempt"),
        )
    )

    assert outcome.result == "applied"
    row = await get_dispatch(db, dispatch_id)
    assert row["status"] == "delivering"


# delivering -> delivering: the crash-recovery claim must be guarded


@pytest.mark.asyncio
async def test_public_transition_rejects_unguarded_delivering_self_edge(db: StateDB) -> None:
    """The public service entry point has no `extra_guard` kwarg (that is a
    non-public parameter reserved for the legacy adapters), so an unguarded
    call must be refused outright rather than silently letting the
    same-status crash-recovery claim through without any race guard."""
    from lionhub.orchestration.dispatch import enqueue_dispatch

    dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
    service = SQLAlchemyLifecycleService(db)
    await service.transition(
        _command(
            entity_type="dispatch",
            entity_id=dispatch_id,
            to_status="delivering",
            reason=ReasonRecord(code="dispatch.delivering.attempt"),
        )
    )

    with pytest.raises(LifecycleValidationError, match="requires a race guard"):
        await service.transition(
            _command(
                entity_type="dispatch",
                entity_id=dispatch_id,
                to_status="delivering",
                reason=ReasonRecord(code="dispatch.delivering.attempt"),
            )
        )


@pytest.mark.asyncio
async def test_two_claimants_racing_delivering_self_edge_exactly_one_wins(db: StateDB) -> None:
    """Direct-service reproduction of the crash-recovery double-claim: two
    callers holding the same pre-claim snapshot (same expected_version) both
    attempt the delivering -> delivering recovery claim. Exactly one must
    apply (non-null transition id); the other must lose as a conflict, and
    the history table must carry only the one winning row."""
    from lionhub.orchestration.dispatch import enqueue_dispatch, get_dispatch

    dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
    service = SQLAlchemyLifecycleService(db)
    await service.transition(
        _command(
            entity_type="dispatch",
            entity_id=dispatch_id,
            to_status="delivering",
            reason=ReasonRecord(code="dispatch.delivering.attempt"),
        )
    )

    snapshot = await get_dispatch(db, dispatch_id)
    shared_version = snapshot["updated_at"]

    def _recovery_claim() -> TransitionCommand:
        return _command(
            entity_type="dispatch",
            entity_id=dispatch_id,
            to_status="delivering",
            reason=ReasonRecord(code="dispatch.delivering.attempt"),
            expected_version=shared_version,
        )

    outcome_a = await service.transition(_recovery_claim())
    outcome_b = await service.transition(_recovery_claim())

    outcomes = [outcome_a, outcome_b]
    applied = [o for o in outcomes if o.result == "applied"]
    conflicted = [o for o in outcomes if o.result == "conflict"]
    assert len(applied) == 1, f"expected exactly one winner, got {outcomes!r}"
    assert len(conflicted) == 1
    assert applied[0].transition_id is not None
    assert conflicted[0].transition_id is None

    rows = await db.fetch_all(
        "SELECT * FROM status_transitions WHERE entity_id = ? AND status = 'delivering'",
        (dispatch_id,),
    )
    # One row for the original pending -> delivering claim, one for the
    # single winning delivering -> delivering recovery claim.
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_public_transition_rejects_unknown_actor_type(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="queued")
    service = SQLAlchemyLifecycleService(db)

    with pytest.raises(LifecycleValidationError, match="invalid actor type"):
        await service.transition(
            _command(
                entity_type="schedule_run",
                entity_id=run_id,
                to_status="running",
                reason=ReasonRecord(code="run.started.ok"),
                actor=ActorRecord(type="typo", id="u1"),
            )
        )


@pytest.mark.asyncio
async def test_public_transition_archives_team(db: StateDB) -> None:
    """The team policy declares a usable reason domain; active -> archived
    applies through the public API."""
    import time as _time
    import uuid as _uuid

    from sqlalchemy import text

    team_id = _uuid.uuid4().hex
    now = _time.time()
    async with db._tx() as conn:
        await conn.execute(
            text(
                "INSERT INTO teams (id, name, created_at, updated_at, status) "
                "VALUES (:id, :name, :now, :now, 'active')"
            ),
            {"id": team_id, "name": "t", "now": now},
        )
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(
        _command(
            entity_type="team",
            entity_id=team_id,
            to_status="archived",
            reason=ReasonRecord(code="team.archived.operator"),
            actor=ActorRecord(type="operator", id="op1"),
        )
    )

    assert outcome.result == "applied"


@pytest.mark.asyncio
async def test_public_transition_rejects_wrong_domain_reason_code(db: StateDB) -> None:
    """A globally valid code from another entity's reason domain is refused."""
    run_id = await _make_schedule_run(db, status="queued")
    service = SQLAlchemyLifecycleService(db)

    with pytest.raises(LifecycleValidationError, match="does not belong to entity_type"):
        await service.transition(
            _command(
                entity_type="schedule_run",
                entity_id=run_id,
                to_status="running",
                reason=ReasonRecord(code="dispatch.acked.consumer"),
            )
        )


@pytest.mark.asyncio
async def test_public_transition_rejects_empty_actor_id(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="queued")
    service = SQLAlchemyLifecycleService(db)

    with pytest.raises(LifecycleValidationError, match="actor.id must be non-empty"):
        await service.transition(
            _command(
                entity_type="schedule_run",
                entity_id=run_id,
                to_status="running",
                reason=ReasonRecord(code="run.started.ok"),
                actor=ActorRecord(type="system", id=""),
            )
        )


@pytest.mark.asyncio
async def test_public_transition_rejects_undeclared_nonterminal_edge(db: StateDB) -> None:
    """The public entry point enforces the declared-edge graph: a session in
    "running" may only move to a terminal status, so an in-vocabulary but
    undeclared move is a rejected outcome (with audit), not a silent write."""
    run_id = await _make_schedule_run(db, status="queued")
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(
        _command(
            entity_type="schedule_run",
            entity_id=run_id,
            to_status="completed",
            reason=ReasonRecord(code="run.completed.ok"),
        )
    )

    assert outcome.result == "rejected"
    assert outcome.current_status == "queued"
    events = await db.list_admin_events(action="status_transition_rejected", target_id=run_id)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_public_transition_override_bypasses_undeclared_edge(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="queued")
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(
        _command(
            entity_type="schedule_run",
            entity_id=run_id,
            to_status="completed",
            reason=ReasonRecord(code="run.completed.ok"),
            override=OverrideRecord(actor="operator", justification="manual reconciliation"),
        )
    )

    assert outcome.result == "applied"
    assert outcome.current_status == "completed"
    events = await db.list_admin_events(action="status_transition_override", target_id=run_id)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_guard_conflict_does_not_record_override_event(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="completed")
    service = SQLAlchemyLifecycleService(db)

    outcome = await service._transition(  # noqa: SLF001 - guarded adapter path
        _command(
            entity_type="schedule_run",
            entity_id=run_id,
            to_status="running",
            reason=ReasonRecord(code="run.started.ok"),
            override=OverrideRecord(actor="operator", justification="manual repair"),
        ),
        extra_guard={"dispatched_at": 123.0},
        enforce_edges=True,
    )

    assert outcome.result == "conflict"
    assert outcome.transition_id is None
    assert (await db.get_schedule_run(run_id))["status"] == "completed"
    events = await db.list_admin_events(action="status_transition_override", target_id=run_id)
    assert events == []


@pytest.mark.asyncio
async def test_transition_applies_with_a_valid_override(db: StateDB) -> None:
    sid = await _make_session(db, status="completed")
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(
        _command(
            entity_id=sid,
            to_status="running",
            override=OverrideRecord(actor="operator", justification="repair"),
        )
    )

    assert outcome.result == "applied"
    assert outcome.current_status == "running"


@pytest.mark.asyncio
async def test_transition_raises_not_found_for_missing_entity(db: StateDB) -> None:
    service = SQLAlchemyLifecycleService(db)
    with pytest.raises(LifecycleNotFoundError):
        await service.transition(_command(entity_id=_uid(), to_status="completed"))


@pytest.mark.asyncio
async def test_transition_raises_validation_error_for_unknown_status(db: StateDB) -> None:
    sid = await _make_session(db, status="running")
    service = SQLAlchemyLifecycleService(db)
    with pytest.raises(LifecycleValidationError):
        await service.transition(_command(entity_id=sid, to_status="bogus"))


@pytest.mark.asyncio
async def test_transition_raises_validation_error_for_malformed_override(db: StateDB) -> None:
    sid = await _make_session(db, status="completed")
    service = SQLAlchemyLifecycleService(db)
    with pytest.raises(LifecycleValidationError, match="actor and"):
        await service.transition(
            _command(
                entity_id=sid,
                to_status="running",
                override=OverrideRecord(actor="", justification="repair"),
            )
        )


@pytest.mark.asyncio
async def test_transition_raises_storage_error_style_for_unknown_entity_type(db: StateDB) -> None:
    service = SQLAlchemyLifecycleService(db)
    with pytest.raises(ValueError, match="unknown entity_type"):
        await service.transition(_command(entity_type="bogus_entity", entity_id=_uid()))


# D4: algorithm behaviors


@pytest.mark.asyncio
async def test_conflict_never_writes_status_transitions_row(db: StateDB) -> None:
    sid = await _make_session(db, status="running")
    service = SQLAlchemyLifecycleService(db)

    await service.transition(
        _command(entity_id=sid, to_status="completed", expected_statuses=frozenset({"failed"}))
    )

    rows = await db.fetch_all(
        "SELECT * FROM status_transitions WHERE entity_id = ? AND previous_status IS NOT NULL",
        (sid,),
    )
    assert rows == []
    row = await db.get_session(sid)
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_rejection_commits_only_its_admin_event_not_a_status_transitions_row(
    db: StateDB,
) -> None:
    sid = await _make_session(db, status="completed")
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(_command(entity_id=sid, to_status="running"))
    assert outcome.result == "rejected"

    transitions = await db.fetch_all(
        "SELECT * FROM status_transitions WHERE entity_id = ? AND previous_status IS NOT NULL",
        (sid,),
    )
    assert transitions == []

    events = await db.list_admin_events(action="status_transition_rejected", target_id=sid)
    assert len(events) == 1

    row = await db.get_session(sid)
    assert row["status"] == "completed"  # untouched


@pytest.mark.asyncio
async def test_guarded_update_reasserts_expected_statuses_at_sql_level(db: StateDB) -> None:
    """A race that slips past the Python-level expected_statuses check (the
    row changes between the SELECT and the UPDATE) still loses at the SQL
    WHERE clause — the UPDATE matches zero rows, not the wrong row's worth
    of columns."""
    sid = await _make_session(db, status="running")
    service = SQLAlchemyLifecycleService(db)

    orig_write = SQLAlchemyLifecycleService._write

    async def _write_after_concurrent_write(self, conn, table, command, **kwargs):
        await conn.execute(
            text(f"UPDATE {table} SET status = 'failed' WHERE id = :id"),  # noqa: S608
            {"id": command.entity_id},
        )
        return await orig_write(self, conn, table, command, **kwargs)

    with patch.object(SQLAlchemyLifecycleService, "_write", _write_after_concurrent_write):
        outcome = await service.transition(
            _command(
                entity_id=sid,
                to_status="completed",
                expected_statuses=frozenset({"running"}),
            )
        )

    assert outcome.result == "conflict"
    row = await db.get_session(sid)
    assert row["status"] == "failed"  # the concurrent write landed, ours lost


@pytest.mark.asyncio
async def test_guarded_update_reasserts_expected_version(db: StateDB) -> None:
    sid = await _make_session(db, status="running")
    row = await db.get_session(sid)
    stale_version = row["updated_at"]

    service = SQLAlchemyLifecycleService(db)
    # A concurrent write bumps updated_at first.
    await service.transition(_command(entity_id=sid, to_status="failed"))

    outcome = await service.transition(
        _command(
            entity_id=sid,
            to_status="completed",
            expected_statuses=frozenset({"failed"}),
            expected_version=stale_version,
        )
    )
    assert outcome.result == "rejected"
    events = await db.list_admin_events(action="status_transition_rejected", target_id=sid)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_history_insert_failure_rolls_back_the_entity_update(db: StateDB) -> None:
    sid = await _make_session(db, status="running")
    service = SQLAlchemyLifecycleService(db)

    orig_write = SQLAlchemyLifecycleService._write

    async def _write_then_break_history_insert(self, conn, table, command, **kwargs):
        # Run the guarded UPDATE for real, then force the history append to
        # fail by feeding it a broken table name — the whole transaction
        # must roll back, including the UPDATE that already "succeeded".
        set_clauses = ["status = :status", "updated_at = :now"]
        result = await conn.execute(
            text(
                f"UPDATE {table} SET {', '.join(set_clauses)} "  # noqa: S608
                "WHERE id = :id AND status = :previous_status"
            ),
            {
                "status": command.to_status,
                "now": kwargs["now"],
                "id": command.entity_id,
                "previous_status": kwargs["previous_status"],
            },
        )
        assert result.rowcount == 1
        await conn.execute(
            text("INSERT INTO nonexistent_history_table (id) VALUES (:id)"), {"id": "x"}
        )
        return "unreachable"

    with patch.object(SQLAlchemyLifecycleService, "_write", _write_then_break_history_insert):
        with pytest.raises(Exception):  # noqa: B017, PT011 -- backend-specific DBAPI error
            await service.transition(_command(entity_id=sid, to_status="completed"))

    row = await db.get_session(sid)
    assert row["status"] == "running"  # rolled back


@pytest.mark.asyncio
async def test_measured_completion_clears_the_approximate_end_flag(db: StateDB) -> None:
    """A repaired row that runs again and really finishes stops being approximate.

    The flag and duration_ms are one fact. Left set beside a measured duration,
    the row contradicts itself and every reader believes the flag, so it
    discards the very number this transition measured.
    """
    sid = await _make_session(db, status="running")
    async with db._tx() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET started_at = :started, ended_at = :ended, "
                "ended_at_is_approximate = 1, duration_ms = NULL WHERE id = :id"
            ),
            {"started": 100.0, "ended": 150.0, "id": sid},
        )
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(_command(entity_id=sid, to_status="completed"))
    assert outcome.result == "applied"

    row = await db.get_session(sid)
    assert row is not None
    assert row["ended_at_is_approximate"] is False
    # The stored 150.0 was a guess, so it is not the basis for a measured end.
    assert row["ended_at"] > 150.0
    assert row["duration_ms"] is not None
    assert row["duration_ms"] == pytest.approx((row["ended_at"] - 100.0) * 1000)


@pytest.mark.asyncio
async def test_a_caller_supplied_end_is_still_honored_on_an_approximate_row(
    db: StateDB,
) -> None:
    """Control for the test above: only the *stored* guess is refused.

    Without this, replacing the basis with `now` unconditionally would look
    equally correct and would quietly discard an end time the caller measured.
    """
    sid = await _make_session(db, status="running")
    async with db._tx() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET started_at = :started, ended_at = :ended, "
                "ended_at_is_approximate = 1 WHERE id = :id"
            ),
            {"started": 100.0, "ended": 150.0, "id": sid},
        )
    service = SQLAlchemyLifecycleService(db)

    await service.transition(
        _command(entity_id=sid, to_status="completed", patch={"ended_at": 400.0})
    )

    row = await db.get_session(sid)
    assert row is not None
    assert row["ended_at"] == 400.0
    assert row["ended_at_is_approximate"] is False
    assert row["duration_ms"] == pytest.approx(300_000.0)


@pytest.mark.asyncio
async def test_a_caller_sent_flag_cannot_survive_an_end_measured_here(
    db: StateDB,
) -> None:
    """A caller may send the flag without sending an end. It does not describe
    the end this transition then takes.

    The transition has no end from the caller, so it measures one and computes
    a duration from it. Carrying the caller's bit forward past that leaves the
    row asserting that a moment observed here was reconstructed, and readers
    believe the bit over the number, so they discard a duration that was
    actually measured. The flag and the duration are one fact, and this is the
    write that establishes both.
    """
    sid = await _make_session(db, status="running")
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE sessions SET started_at = :started WHERE id = :id"),
            {"started": 100.0, "id": sid},
        )
    service = SQLAlchemyLifecycleService(db)

    await service.transition(
        _command(
            entity_id=sid,
            to_status="completed",
            patch={"ended_at_is_approximate": 1},
        )
    )

    row = await db.get_session(sid)
    assert row is not None
    assert row["ended_at_is_approximate"] is False
    assert row["duration_ms"] == pytest.approx((row["ended_at"] - 100.0) * 1000)


@pytest.mark.asyncio
async def test_a_caller_supplying_both_an_end_and_the_flag_keeps_the_flag(
    db: StateDB,
) -> None:
    """Control for the test above: the clear is not unconditional.

    A caller that supplies an end *and* marks it reconstructed is recording a
    repaired row on purpose. Forcing the flag off there would be the same
    defect in the other direction, turning a declared guess into a measurement
    on the strength of nothing.
    """
    sid = await _make_session(db, status="running")
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE sessions SET started_at = :started WHERE id = :id"),
            {"started": 100.0, "id": sid},
        )
    service = SQLAlchemyLifecycleService(db)

    await service.transition(
        _command(
            entity_id=sid,
            to_status="completed",
            patch={"ended_at": 400.0, "ended_at_is_approximate": 1},
        )
    )

    row = await db.get_session(sid)
    assert row is not None
    assert row["ended_at"] == 400.0
    assert row["ended_at_is_approximate"] is True


@pytest.mark.asyncio
async def test_an_explicitly_null_end_does_not_survive_the_measurement(
    db: StateDB,
) -> None:
    """Sending ``ended_at=None`` is sending the absence of an end, not an end.

    The transition measures one and computes the duration from it. If the
    caller's null were kept, the row would land with no end, marked measured,
    carrying a duration -- the same contradiction the flag handling exists to
    prevent, entered from the other side. Nothing in the tree sends this today;
    it is reachable through the public update path.
    """
    sid = await _make_session(db, status="running")
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE sessions SET started_at = :started WHERE id = :id"),
            {"started": 100.0, "id": sid},
        )
    service = SQLAlchemyLifecycleService(db)

    await service.transition(
        _command(
            entity_id=sid,
            to_status="completed",
            patch={"ended_at": None, "ended_at_is_approximate": 1},
        )
    )

    row = await db.get_session(sid)
    assert row is not None
    assert row["ended_at"] is not None
    assert row["ended_at_is_approximate"] is False
    assert row["duration_ms"] == pytest.approx((row["ended_at"] - 100.0) * 1000)


async def test_a_deliberately_approximate_end_leaves_the_duration_unknown(
    db: StateDB,
) -> None:
    """A caller recording a reconstructed end must not get a duration from it.

    Sending an end together with ``ended_at_is_approximate=1`` says the end was
    derived, not observed. Subtracting the start from it produces a number that
    reads as a measured length, and once persisted no reader can tell it was
    derived -- the flag is on the row, but a consumer that trusts a stored
    duration never consults it. ``docs/internals/state-db.md`` states the
    contract directly: an approximate end leaves duration unknown.
    """
    sid = await _make_session(db, status="running")
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE sessions SET started_at = :started WHERE id = :id"),
            {"started": 100.0, "id": sid},
        )
    service = SQLAlchemyLifecycleService(db)

    await service.transition(
        _command(
            entity_id=sid,
            to_status="completed",
            patch={"ended_at": 400.0, "ended_at_is_approximate": 1},
        )
    )

    row = await db.get_session(sid)
    assert row is not None
    assert row["ended_at"] == 400.0
    assert row["ended_at_is_approximate"] is True
    assert row["duration_ms"] is None, (
        "an approximate end was subtracted from the start and stored as a duration"
    )


async def test_a_measured_end_still_gets_its_duration(
    db: StateDB,
) -> None:
    """Control: the gate withholds duration from approximate ends, not from all ends.

    Without this, the test above would pass just as well against a transition
    that had stopped computing durations altogether, which is the ordinary way
    a fix of this shape goes wrong.
    """
    sid = await _make_session(db, status="running")
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE sessions SET started_at = :started WHERE id = :id"),
            {"started": 100.0, "id": sid},
        )
    service = SQLAlchemyLifecycleService(db)

    await service.transition(
        _command(entity_id=sid, to_status="completed", patch={"ended_at": 400.0})
    )

    row = await db.get_session(sid)
    assert row is not None
    assert row["ended_at_is_approximate"] is False
    assert row["duration_ms"] == pytest.approx(300.0 * 1000)


async def test_a_supplied_duration_does_not_survive_an_approximate_end(
    db: StateDB,
) -> None:
    """Sending a duration with an approximate end must not persist the duration.

    Not computing one is only half the contract. Both readers discard a stored
    duration when the flag is set, so a persisted one is a value nothing can
    surface, on a row that then asserts a measured length for an end nobody
    measured. The schema-version repair clears it in the same write that sets
    the bit; a transition that sets the bit owes the same.
    """
    sid = await _make_session(db, status="running")
    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE sessions SET started_at = :started WHERE id = :id"),
            {"started": 100.0, "id": sid},
        )
    service = SQLAlchemyLifecycleService(db)

    await service.transition(
        _command(
            entity_id=sid,
            to_status="completed",
            patch={
                "ended_at": 400.0,
                "ended_at_is_approximate": 1,
                "duration_ms": 300000.0,
            },
        )
    )

    row = await db.get_session(sid)
    assert row is not None
    assert row["ended_at_is_approximate"] is True
    assert row["duration_ms"] is None, (
        "a caller-supplied duration was kept beside an approximate end"
    )


async def test_a_stored_duration_is_cleared_when_a_transition_marks_the_end_approximate(
    db: StateDB,
) -> None:
    """The same holds for a duration already on the row rather than in the patch.

    A row can carry a duration written by an earlier terminal transition and
    then be re-marked approximate. Guarding only the patch would leave that one
    in place, which is the same contradiction reached from the store side.
    """
    sid = await _make_session(db, status="running")
    async with db._tx() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET started_at = :started, duration_ms = :duration WHERE id = :id"
            ),
            {"started": 100.0, "duration": 999.0, "id": sid},
        )
    service = SQLAlchemyLifecycleService(db)

    await service.transition(
        _command(
            entity_id=sid,
            to_status="completed",
            patch={"ended_at": 400.0, "ended_at_is_approximate": 1},
        )
    )

    row = await db.get_session(sid)
    assert row is not None
    assert row["duration_ms"] is None, "a pre-existing duration outlived the approximate mark"


_DECISION = {"kind": "chain_decision", "v": 1, "ref": "on_fail"}
_NO_CHAIN = {**_DECISION, "ref": "none"}
_INVOCATION_REF = {"kind": "invocation", "id": "inv"}


@pytest_asyncio.fixture(params=["sqlite", "postgresql"], loop_scope="function")
async def chain_db(request, tmp_path):
    store = (
        StateDB(tmp_path / "chain.db")
        if request.param == "sqlite"
        else StateDB(url=request.getfixturevalue("pg_url"))
    )
    async with store:
        yield store


async def _chain_history(store, run_id):
    return await store.fetch_all(
        "SELECT status, evidence_refs FROM status_transitions "
        "WHERE entity_type='schedule_run' AND entity_id=? ORDER BY created_at, id",
        (run_id,),
    )


async def _chain_rejections(store, run_id):
    return await store.fetch_all(
        "SELECT details FROM admin_events WHERE action='status_transition_rejected' AND target_id=?",
        (run_id,),
    )


def _history_refs(history_row):
    refs = history_row["evidence_refs"]
    return json.loads(refs) if isinstance(refs, str) else refs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "target", "expected", "marked", "decision"),
    [
        ("running", "completed", {"running"}, False, _DECISION),
        ("running", "completed", {"running"}, True, {**_DECISION, "v": True}),
        ("running", "completed", None, True, _DECISION),
        ("queued", "running", {"queued"}, True, _DECISION),
    ],
    ids=["unmarked", "boolean-version", "no-running-guard", "nonterminal-target"],
)
async def test_schedule_decision_requires_an_authorized_guarded_terminal_write(
    chain_db, status, target, expected, marked, decision
):
    store = chain_db
    run_id = await _make_schedule_run(store, status=status)
    before = await store.get_schedule_run(run_id)
    history = await _chain_history(store, run_id)
    service = store._lifecycle_service()
    outcome = await service.transition(
        _command(
            entity_type="schedule_run",
            entity_id=run_id,
            to_status=target,
            reason=ReasonRecord(code=RunReasons.COMPLETED_OK, evidence_refs=(decision,)),
            expected_statuses=frozenset(expected) if expected is not None else None,
            record_chain_decision=marked,
        )
    )
    assert (
        outcome.result,
        outcome.current_status,
        outcome.transition_id,
        outcome.rejection_reason,
    ) == ("rejected", status, None, "decision_conflict")
    events = await _chain_rejections(store, run_id)
    assert len(events) == 1
    with pytest.raises(TransitionRejectedError, match="decision_conflict") as rejection:
        await store.update_status(
            "schedule_run",
            run_id,
            new_status=target,
            reason_code=RunReasons.COMPLETED_OK,
            expected_statuses=expected,
            evidence_refs=[decision],
            record_chain_decision=marked,
        )
    assert rejection.value.reason == "decision_conflict"
    assert rejection.value.previous_status == status
    events = await _chain_rejections(store, run_id)
    assert len(events) == 2
    assert await store.get_schedule_run(run_id) == before
    assert await _chain_history(store, run_id) == history


@pytest.mark.asyncio
@pytest.mark.parametrize(("stamp", "decision_expected"), [("6", False), ("7", True)])
async def test_schedule_decisions_read_the_same_store_stamp_as_the_writer(
    chain_db, stamp, decision_expected
):
    store = chain_db
    async with store._tx() as conn:
        await conn.execute(
            text("UPDATE schema_meta SET value=:stamp WHERE key='version'"),
            {"stamp": stamp},
        )
    try:
        run_id = await _make_schedule_run(store, status="running")
        assert await store.update_status(
            "schedule_run",
            run_id,
            new_status="failed",
            reason_code=RunReasons.FAILED_EXCEPTION,
            expected_statuses={"running"},
            evidence_refs=[_INVOCATION_REF, _DECISION],
            record_chain_decision=True,
        )
        run = await store.get_schedule_run(run_id)
        terminal_refs = [_INVOCATION_REF, _DECISION] if decision_expected else [_INVOCATION_REF]
        assert run["status_evidence_refs"] == terminal_refs
        assert _history_refs((await _chain_history(store, run_id))[-1]) == terminal_refs

        for status in ("failed", "skipped", "running", "queued"):
            run_id = await _make_schedule_run(store, status=status, seed_chain_decision=True)
            row = await store.get_schedule_run(run_id)
            seed = [_NO_CHAIN] if decision_expected and status in ("failed", "skipped") else []
            assert row["status_evidence_refs"] == seed
            assert _history_refs((await _chain_history(store, run_id))[0]) == seed
        for status in ("failed", "skipped"):
            run_id = await _make_schedule_run(store, status=status)
            row = await store.get_schedule_run(run_id)
            assert row["status_evidence_refs"] == []
            assert _history_refs((await _chain_history(store, run_id))[0]) == []
    finally:
        async with store._tx() as conn:
            await conn.execute(
                text("UPDATE schema_meta SET value=:stamp WHERE key='version'"),
                {"stamp": SCHEMA_VERSION},
            )


@pytest.mark.asyncio
async def test_schedule_decision_survives_an_append_based_on_a_running_snapshot(chain_db):
    store = chain_db
    run_id = await _make_schedule_run(store, status="running")
    read_complete, append_allowed = asyncio.Event(), asyncio.Event()

    async def later_writer():
        snapshot = await store.get_schedule_run(run_id)
        assert snapshot["status"] == "running"
        read_complete.set()
        await append_allowed.wait()
        return await store.update_status(
            "schedule_run",
            run_id,
            new_status="failed",
            reason_code=RunReasons.FAILED_EXCEPTION,
            evidence_refs=snapshot["status_evidence_refs"],
        )

    pending_append = asyncio.create_task(later_writer())
    try:
        await asyncio.wait_for(read_complete.wait(), timeout=5)
        assert await store.update_status(
            "schedule_run",
            run_id,
            new_status="failed",
            reason_code=RunReasons.FAILED_EXCEPTION,
            expected_statuses={"running"},
            evidence_refs=[_INVOCATION_REF, _DECISION],
            record_chain_decision=True,
        )
    finally:
        append_allowed.set()
        assert await pending_append
    row = await store.get_schedule_run(run_id)
    assert row["status_evidence_refs"] == [_DECISION]
    history = await _chain_history(store, run_id)
    assert [_history_refs(item) for item in history] == [
        [],
        [_INVOCATION_REF, _DECISION],
        [_DECISION],
    ]


def _legacy_chain_command(run_id, *, origin="failed", target="failed", evidence_refs=()):
    return TransitionRequest(
        entity_type="schedule_run",
        entity_id=run_id,
        from_state=origin,
        to_state=target,
        reason=StateReason(code=RunReasons.FAILED_EXCEPTION, evidence_refs=list(evidence_refs)),
        actor=Actor(type="scheduler", id="scheduler"),
        idempotency_key=_uid(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2], ids=["known-version", "future-version"])
async def test_schedule_decision_survives_terminal_override_and_legacy_history(chain_db, version):
    store = chain_db
    run_id = await _make_schedule_run(store, status="failed")
    decision = {**_NO_CHAIN, "v": version}
    async with store._tx() as conn:
        await conn.execute(
            text("UPDATE schedule_runs SET status_evidence_refs=:refs WHERE id=:run_id").bindparams(
                bindparam("refs", type_=JSON)
            ),
            {"refs": [decision], "run_id": run_id},
        )

    assert await store.update_status(
        "schedule_run",
        run_id,
        new_status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        evidence_refs=[_INVOCATION_REF],
        override=True,
        override_actor="operator",
        override_justification="observed completion",
    )
    assert await store.update_status(
        "schedule_run",
        run_id,
        new_status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        evidence_refs=[_INVOCATION_REF, decision],
    )
    before_legacy = await store.get_schedule_run(run_id)
    assert before_legacy["status_evidence_refs"] == [_INVOCATION_REF, decision]

    ordinary = {"kind": "schedule", "id": "definition"}
    outcome = await transition(
        store,
        _legacy_chain_command(
            run_id, origin="completed", target="completed", evidence_refs=(ordinary,)
        ),
    )
    assert outcome.applied
    after_legacy = await store.get_schedule_run(run_id)
    assert after_legacy["status_evidence_refs"] == before_legacy["status_evidence_refs"]
    assert after_legacy["status_reason_code"] == before_legacy["status_reason_code"]
    assert _history_refs((await _chain_history(store, run_id))[-1]) == [ordinary, decision]


@pytest.mark.asyncio
async def test_schedule_decision_policy_loses_stale_guards_before_an_audit(chain_db):
    store = chain_db
    run_id = await _make_schedule_run(store, status="failed", seed_chain_decision=True)
    before = await store.get_schedule_run(run_id)
    history = await _chain_history(store, run_id)

    assert not await store.update_status(
        "schedule_run",
        run_id,
        new_status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        evidence_refs=[_DECISION],
        expected_updated_at=-1.0,
    )
    outcome = await transition(
        store,
        _legacy_chain_command(run_id, evidence_refs=(_DECISION,)),
        guard={"lease_attempts": 1},
    )
    assert (outcome.applied, outcome.conflict, outcome.transition_id) == (False, True, None)
    with pytest.raises(LifecycleValidationError, match="not in the declared transition"):
        await transition(
            store, _legacy_chain_command(run_id, target="completed", evidence_refs=(_DECISION,))
        )
    assert not await _chain_rejections(store, run_id)
    assert await store.get_schedule_run(run_id) == before
    assert await _chain_history(store, run_id) == history


@pytest.mark.asyncio
async def test_schedule_terminal_floor_and_decision_refusals_keep_current_and_history(chain_db):
    store = chain_db
    run_id = await _make_schedule_run(store, status="failed", seed_chain_decision=True)
    before = await store.get_schedule_run(run_id)
    history = await _chain_history(store, run_id)
    with pytest.raises(TransitionRejectedError, match="without override=True") as rejection:
        await store.update_status(
            "schedule_run",
            run_id,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            evidence_refs=[_DECISION],
        )
    assert rejection.value.reason == "no_override"
    with pytest.raises(TransitionRejectedError, match="terminal_floor"):
        await store.update_status(
            "schedule_run",
            run_id,
            new_status="running",
            reason_code=RunReasons.STARTED_OK,
            override=True,
            override_actor="operator",
            override_justification="retry old occurrence",
        )
    with pytest.raises(TransitionRejectedError, match="terminal_floor"):
        await transition(store, _legacy_chain_command(run_id, target="running"))
    with pytest.raises(TransitionRejectedError, match="decision_conflict"):
        await transition(store, _legacy_chain_command(run_id, evidence_refs=(_DECISION,)))
    with pytest.raises(TransitionRejectedError, match="decision_conflict"):
        await store.update_status(
            "schedule_run",
            run_id,
            new_status="failed",
            reason_code=RunReasons.FAILED_EXCEPTION,
            evidence_refs=[{**_NO_CHAIN, "v": True}],
        )
    events = await _chain_rejections(store, run_id)
    assert len(events) == 5
    assert await store.get_schedule_run(run_id) == before
    assert await _chain_history(store, run_id) == history

    undecided = await _make_schedule_run(store, status="failed")
    before = await store.get_schedule_run(undecided)
    history = await _chain_history(store, undecided)
    with pytest.raises(TransitionRejectedError, match="decision_conflict"):
        await transition(store, _legacy_chain_command(undecided, evidence_refs=(_DECISION,)))
    events = await _chain_rejections(store, undecided)
    assert len(events) == 1
    assert await store.get_schedule_run(undecided) == before
    assert await _chain_history(store, undecided) == history
