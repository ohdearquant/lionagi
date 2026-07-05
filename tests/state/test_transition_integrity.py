# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0094 integrity floor: `update_status()` is the single write path and
refuses to move a terminal entity without an explicit, justified override.
Every rejection and every override is recorded in admin_events; a guarded
CAS write always beats a stale concurrent write."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from lionagi.state.db import (
    _INVOCATION_STATUSES,
    _PLAY_STATUSES,
    _SHOW_STATUSES,
    VALID_SESSION_STATUSES,
    VALID_STATUSES_BY_ENTITY_TYPE,
    StateDB,
    TransitionRejectedError,
)
from lionagi.state.reasons import SessionReasons

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


def _uid() -> str:
    return str(uuid.uuid4())


def _details(event: dict) -> dict:
    """admin_events.details round-trips as a JSON string on sqlite."""
    raw = event["details"]
    return json.loads(raw) if isinstance(raw, str) else raw


async def _make_session(db: StateDB, *, status: str = "running") -> str:
    prog_id = _uid()
    await db.create_progression(prog_id)
    sid = _uid()
    await db.create_session({"id": sid, "progression_id": prog_id, "status": status})
    return sid


async def _make_schedule_run(db: StateDB, *, status: str = "running") -> str:
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


# ── Rejection without override ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_to_running_rejected_without_override(db: StateDB) -> None:
    sid = await _make_session(db, status="completed")

    with pytest.raises(TransitionRejectedError):
        await db.update_status(
            "session",
            sid,
            new_status="running",
            reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
            source="admin",
        )

    row = await db.get_session(sid)
    assert row["status"] == "completed"  # untouched — the write never landed


@pytest.mark.asyncio
async def test_rejected_transition_is_recorded_in_admin_events(db: StateDB) -> None:
    sid = await _make_session(db, status="completed")

    with pytest.raises(TransitionRejectedError):
        await db.update_status(
            "session",
            sid,
            new_status="running",
            reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
            source="admin",
        )

    events = await db.list_admin_events(action="status_transition_rejected", target_id=sid)
    assert len(events) == 1
    details = _details(events[0])
    assert details["entity_type"] == "session"
    assert details["previous_status"] == "completed"
    assert details["attempted_status"] == "running"


# ── Override path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_to_running_succeeds_with_override(db: StateDB) -> None:
    sid = await _make_session(db, status="completed")

    applied = await db.update_status(
        "session",
        sid,
        new_status="running",
        reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
        source="admin",
        override=True,
        override_actor="ocean",
        override_justification="operational repair: mis-marked completed",
    )

    assert applied is True
    row = await db.get_session(sid)
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_override_is_recorded_in_admin_events(db: StateDB) -> None:
    sid = await _make_session(db, status="completed")

    await db.update_status(
        "session",
        sid,
        new_status="running",
        reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
        source="admin",
        override=True,
        override_actor="ocean",
        override_justification="operational repair: mis-marked completed",
    )

    events = await db.list_admin_events(action="status_transition_override", target_id=sid)
    assert len(events) == 1
    details = _details(events[0])
    assert details["previous_status"] == "completed"
    assert details["new_status"] == "running"
    assert details["justification"] == "operational repair: mis-marked completed"
    assert events[0]["actor"] == "ocean"


def test_override_requires_actor_and_justification() -> None:
    async def _run() -> None:
        state = StateDB(":memory:")
        await state.open()
        try:
            sid = await _make_session(state, status="completed")
            with pytest.raises(ValueError):
                await state.update_status(
                    "session",
                    sid,
                    new_status="running",
                    reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
                    source="admin",
                    override=True,
                )
        finally:
            await state.close()

    asyncio.run(_run())


# ── Concurrent stale write loses to the guarded write ───────────────────────


@pytest.mark.asyncio
async def test_concurrent_stale_write_loses_to_guarded_write(db: StateDB) -> None:
    """A writer holding a stale `running` snapshot must not clobber a newer
    terminal write that landed first — CAS on expected_statuses guards it."""
    sid = await _make_session(db, status="running")

    # The "newer" writer marks the session terminal first.
    applied_first = await db.update_status(
        "session",
        sid,
        new_status="completed",
        reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
        source="executor",
        expected_statuses={"running"},
    )
    assert applied_first is True

    # A second writer, still holding its stale "running" snapshot from
    # before the first write landed, tries to CAS from "running" too — it
    # must lose (skip), not silently overwrite the now-terminal status.
    applied_second = await db.update_status(
        "session",
        sid,
        new_status="failed",
        reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
        source="executor",
        expected_statuses={"running"},
    )
    assert applied_second is False

    row = await db.get_session(sid)
    assert row["status"] == "completed"  # the first (newer) write wins


# ── Status vocabulary — update_status() rejects unknown statuses ───────────


@pytest.mark.asyncio
async def test_update_status_rejects_unknown_status_for_session(db: StateDB) -> None:
    """A status outside VALID_STATUSES_BY_ENTITY_TYPE must never persist,
    regardless of whether the entity is currently terminal or not."""
    sid = await _make_session(db, status="running")

    with pytest.raises(ValueError, match="bogus_status"):
        await db.update_status(
            "session",
            sid,
            new_status="bogus_status",
            reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
            source="admin",
        )

    row = await db.get_session(sid)
    assert row["status"] == "running"  # unchanged — the write never landed


@pytest.mark.asyncio
async def test_update_status_rejects_unknown_status_for_schedule_run(db: StateDB) -> None:
    run_id = await _make_schedule_run(db, status="running")

    with pytest.raises(ValueError, match="bogus_status"):
        await db.update_status(
            "schedule_run",
            run_id,
            new_status="bogus_status",
            reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
            source="admin",
        )

    row = await db.get_schedule_run(run_id)
    assert row["status"] == "running"  # unchanged — the write never landed


@pytest.mark.parametrize(
    ("entity_type", "authoritative"),
    [
        ("session", VALID_SESSION_STATUSES),
        ("invocation", _INVOCATION_STATUSES),
        ("show", _SHOW_STATUSES),
        ("play", _PLAY_STATUSES),
    ],
)
def test_valid_vocabulary_admits_every_authoritative_status(
    entity_type: str, authoritative: frozenset[str]
) -> None:
    """update_status()'s per-entity vocabulary must be a superset of the status
    set that the entity's own writer (update_show/update_play/…) already blesses.
    A legal write that the writer accepts but the floor rejects is a regression;
    this pins the whole under-inclusion class, not one entity."""
    vocab = VALID_STATUSES_BY_ENTITY_TYPE[entity_type]
    missing = authoritative - vocab
    assert not missing, f"{entity_type} vocabulary omits legal statuses: {sorted(missing)}"


# ── Storage-level CAS — the UPDATE itself guards on previous_status ─────────


@pytest.mark.asyncio
async def test_storage_level_cas_rejects_row_changed_under_update_status(db: StateDB) -> None:
    """_apply_status_write()'s UPDATE re-asserts previous_status at the SQL
    level (not only via the Python expected_statuses check above it): if the
    row changes between update_status()'s SELECT and its UPDATE, the write
    affects zero rows and raises loudly instead of silently overwriting.

    Real thread concurrency is flaky, so the race is simulated
    deterministically: a second writer lands, on the SAME connection/
    transaction, in between the read that update_status() already performed
    and the guarded UPDATE — mirroring exactly the gap the SQL guard closes.
    """
    sid = await _make_session(db, status="running")

    orig_apply = StateDB._apply_status_write

    async def _apply_after_concurrent_write(self, conn, table, canonical_type, entity_id, **kwargs):
        # Simulate a second writer landing between update_status()'s SELECT
        # (already done — previous_status="running" is captured in kwargs)
        # and this UPDATE.
        await conn.execute(
            text(f"UPDATE {table} SET status = 'completed' WHERE id = :id"),  # noqa: S608
            {"id": entity_id},
        )
        return await orig_apply(self, conn, table, canonical_type, entity_id, **kwargs)

    with patch.object(StateDB, "_apply_status_write", _apply_after_concurrent_write):
        with pytest.raises(RuntimeError, match="status CAS lost"):
            await db.update_status(
                "session",
                sid,
                new_status="failed",
                reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
                source="executor",
            )

    # The whole transaction (the simulated concurrent write and the guarded
    # write) rolled back on the raised error — the row is exactly as it was
    # before update_status() was ever called, not silently left as "failed".
    row = await db.get_session(sid)
    assert row["status"] == "running"
