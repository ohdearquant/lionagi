# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0058 Phase 2 gate: D1 result semantics and the D4 guarded transition
algorithm, exercised directly against SQLAlchemyLifecycleService (bypassing
the StateDB.update_status()/transitions.transition() compatibility wrappers
tested elsewhere)."""

from __future__ import annotations

import time
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import (
    ActorRecord,
    LifecycleNotFoundError,
    LifecycleValidationError,
    OverrideRecord,
    ReasonRecord,
    TransitionCommand,
)
from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService

# ── Fixtures ──────────────────────────────────────────────────────────────────


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


# ── D1: result semantics ────────────────────────────────────────────────────


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
    from lionagi.dispatch import enqueue_dispatch, get_dispatch

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


# ── delivering -> delivering: the crash-recovery claim must be guarded ─────


@pytest.mark.asyncio
async def test_public_transition_rejects_unguarded_delivering_self_edge(db: StateDB) -> None:
    """The public service entry point has no `extra_guard` kwarg (that is a
    non-public parameter reserved for the legacy adapters), so an unguarded
    call must be refused outright rather than silently letting the
    same-status crash-recovery claim through without any race guard."""
    from lionagi.dispatch import enqueue_dispatch

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
    from lionagi.dispatch import enqueue_dispatch, get_dispatch

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


# ── D4: algorithm behaviors ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conflict_never_writes_status_transitions_row(db: StateDB) -> None:
    sid = await _make_session(db, status="running")
    service = SQLAlchemyLifecycleService(db)

    await service.transition(
        _command(entity_id=sid, to_status="completed", expected_statuses=frozenset({"failed"}))
    )

    rows = await db.fetch_all("SELECT * FROM status_transitions WHERE entity_id = ?", (sid,))
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

    transitions = await db.fetch_all("SELECT * FROM status_transitions WHERE entity_id = ?", (sid,))
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
    assert outcome.result == "conflict"


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
