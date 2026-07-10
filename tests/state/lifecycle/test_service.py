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
            reason=ReasonRecord(code="run.completed.exit_zero"),
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
            reason=ReasonRecord(code="run.completed.exit_zero"),
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
