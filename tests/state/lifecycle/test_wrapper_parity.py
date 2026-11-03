# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0058 Phase 2 gate: StateDB.update_status() and
lionagi.state.transitions.transition() both delegate through the same
SQLAlchemyLifecycleService — this pins that their shared invariants (CAS
conflict is a clean skip that writes nothing, a missing row raises
LookupError, a lost race never partially writes) hold identically across
both legacy surfaces, on schedule_run (the one entity type reachable
through both)."""

from __future__ import annotations

import time
import uuid

import pytest
import pytest_asyncio

from lionagi.state.db import StateDB
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


# Missing row: both surfaces raise LookupError


@pytest.mark.asyncio
async def test_update_status_raises_lookup_error_for_missing_row(db: StateDB) -> None:
    with pytest.raises(LookupError):
        await db.update_status(
            "schedule_run",
            _uid(),
            new_status="running",
            reason_code=RunReasons.STARTED_OK,
            source="executor",
        )


@pytest.mark.asyncio
async def test_legacy_transition_raises_lookup_error_for_missing_row(db: StateDB) -> None:
    with pytest.raises(LookupError):
        await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=_uid(),
                from_state="queued",
                to_state="running",
                reason=StateReason(code=RunReasons.STARTED_OK),
                actor=Actor(type="system", id="w1"),
                idempotency_key=_uid(),
            ),
        )


# CAS conflict: both surfaces report a clean skip, no row mutation


@pytest.mark.asyncio
async def test_update_status_cas_conflict_is_a_clean_skip(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="queued")

    applied = await db.update_status(
        "schedule_run",
        run_id,
        new_status="running",
        reason_code=RunReasons.STARTED_OK,
        source="executor",
        expected_statuses={"running"},  # actual status is "queued"
    )

    assert applied is False
    row = await db.get_schedule_run(run_id)
    assert row["status"] == "queued"


@pytest.mark.asyncio
async def test_legacy_transition_cas_conflict_is_a_clean_skip(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="queued")

    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="running",  # actual status is "queued"
            to_state="completed",
            reason=StateReason(code=RunReasons.COMPLETED_OK),
            actor=Actor(type="system", id="w1"),
            idempotency_key=_uid(),
        ),
    )

    assert result.applied is False
    assert result.conflict is True
    # No history row is written on a conflict, so there is no transition id to return.
    assert result.transition_id is None
    row = await db.get_schedule_run(run_id)
    assert row["status"] == "queued"


@pytest.mark.asyncio
async def test_public_transition_rejects_null_guard_values(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="queued")

    with pytest.raises(ValueError, match="None"):
        await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="queued",
                to_state="running",
                reason=StateReason(code=RunReasons.STARTED_OK),
                actor=Actor(type="system", id="w1"),
                idempotency_key=_uid(),
            ),
            guard={"leased_by": None},
        )


# Same-status write: "append" rule applies identically on both surfaces


@pytest.mark.asyncio
async def test_update_status_same_status_write_appends(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="running")

    applied = await db.update_status(
        "schedule_run",
        run_id,
        new_status="running",
        reason_code=RunReasons.STARTED_OK,
        source="executor",
    )

    assert applied is True
    row = await db.get_schedule_run(run_id)
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_legacy_transition_same_status_write_appends(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="running")

    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="running",
            to_state="running",
            reason=StateReason(code=RunReasons.STARTED_OK),
            actor=Actor(type="system", id="w1"),
            idempotency_key=_uid(),
        ),
    )

    assert result.applied is True
    row = await db.get_schedule_run(run_id)
    assert row["status"] == "running"


# ── Undeclared edge: only the legacy transition surface enforces the graph ─


@pytest.mark.asyncio
async def test_update_status_never_enforces_a_declared_edge_graph(db: StateDB) -> None:
    """StateDB.update_status() has no edge concept — any nonterminal ->
    any legal vocabulary status is allowed, unlike transitions.transition()."""
    run_id = await _make_schedule_run(db, status="queued")

    applied = await db.update_status(
        "schedule_run",
        run_id,
        new_status="failed",  # not a declared queued -> failed edge
        reason_code=RunReasons.TIMED_OUT_DEADLINE,
        source="executor",
    )

    assert applied is True


@pytest.mark.asyncio
async def test_legacy_transition_enforces_the_declared_edge_graph(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="queued")

    with pytest.raises(ValueError, match="not in the declared transition vocabulary"):
        await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="queued",
                to_state="failed",  # not a declared queued -> failed edge
                reason=StateReason(code=RunReasons.TIMED_OUT_DEADLINE),
                actor=Actor(type="system", id="w1"),
                idempotency_key=_uid(),
            ),
        )


# ── transition_id names a written row, or nothing ──────────────────────────


@pytest.mark.asyncio
async def test_conflict_returns_no_transition_id(db: StateDB) -> None:
    """A conflicting transition writes no status_transitions row, so its
    result carries no transition id: an id here would reference nothing."""
    run_id = await _make_schedule_run(db, status="queued")

    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="running",  # actual status is "queued"
            to_state="completed",
            reason=StateReason(code=RunReasons.COMPLETED_OK),
            actor=Actor(type="system", id="w1"),
            idempotency_key=_uid(),
        ),
    )

    assert result.applied is False
    assert result.conflict is True
    assert result.transition_id is None


@pytest.mark.asyncio
async def test_applied_transition_id_names_the_written_row(db: StateDB) -> None:
    """An applied transition's id is the id of the history row the service
    wrote, so a caller holding the id can resolve it."""
    run_id = await _make_schedule_run(db, status="queued")

    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="queued",
            to_state="running",
            reason=StateReason(code=RunReasons.STARTED_OK),
            actor=Actor(type="system", id="w1"),
            idempotency_key=_uid(),
        ),
    )

    assert result.applied is True
    assert result.transition_id is not None
    rows = await db.fetch_all("SELECT id FROM status_transitions WHERE entity_id = ?", (run_id,))
    assert result.transition_id in {row["id"] for row in rows}
