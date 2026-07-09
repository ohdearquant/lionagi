# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0101 D3: the local (host-only) worker/claim loop.

Covers the claim CAS race, cancel-beats-claim, bounded lease-expiry
recovery (R1), terminal re-entry rejection (R2), the D3 claim predicate,
and a full submit -> claim -> execute -> completed round trip.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB
from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition
from lionagi.studio.scheduler.worker import (
    MAX_LEASE_ATTEMPTS,
    claim_and_execute,
    reap_expired_leases,
    worker_tick,
)
from lionagi.studio.services.task_applications import TaskApplication, submit_task


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


async def _submit_host_task(db: StateDB, **overrides) -> str:
    kwargs = {"action_kind": "agent", "args": {}, "execution_target": "host"}
    kwargs.update(overrides)
    app = TaskApplication(**kwargs)
    return await submit_task(db, app)


async def _make_scheduled_queued_row(db: StateDB) -> str:
    """A schedule-fired-shaped row (schedule_id NOT NULL) that otherwise
    looks host-claimable in every other column — proves the worker's claim
    predicate excludes it regardless."""
    sched_id = str(uuid.uuid4())
    await db.create_schedule(
        {
            "id": sched_id,
            "name": f"sched-{sched_id}",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    run_id = str(uuid.uuid4())
    now = time.time()
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": sched_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": {},
            "status": "queued",
            "fired_at": now,
        }
    )
    async with db._tx() as conn:
        await conn.execute(
            text(
                "UPDATE schedule_runs SET execution_target = 'host', "
                "queued_at = :now, required_capabilities = '[]' WHERE id = :id"
            ),
            {"now": now, "id": run_id},
        )
    return run_id


async def _noop_execute(row: dict) -> tuple[int, str]:
    return 0, ""


async def _failing_execute(row: dict) -> tuple[int, str]:
    return 1, "boom"


# ── 1. Claim CAS race ────────────────────────────────────────────────────


async def test_two_concurrent_claims_exactly_one_wins(tmp_path) -> None:
    # A real (file-backed) StateDB, not ":memory:": genuinely concurrent
    # transactions need the connection pool a file DB gets, matching the
    # dispatch outbox's own concurrent-CAS test precedent
    # (tests/dispatch/test_outbox.py's tmp_path-based StateDB).
    async with StateDB(tmp_path / "state.db") as db:
        run_id = await _submit_host_task(db)

        claimed_counts = await asyncio.gather(
            claim_and_execute(db, worker_id="w1", execute=_noop_execute),
            claim_and_execute(db, worker_id="w2", execute=_noop_execute),
        )
        assert sorted(claimed_counts) == [0, 1]

        row = await db.fetch_one(
            "SELECT leased_by, lease_attempts, status FROM schedule_runs WHERE id = ?", (run_id,)
        )
    assert row["leased_by"] in ("w1", "w2")
    assert row["lease_attempts"] == 1
    assert row["status"] == "completed"


# ── 2. Cancel beats claim ────────────────────────────────────────────────


async def test_cancelled_before_claim_is_never_leased(db: StateDB) -> None:
    from lionagi.studio.services.task_applications import cancel_task

    run_id = await _submit_host_task(db)
    assert await cancel_task(db, run_id, actor=Actor(type="operator", id="test")) is True

    claimed = await claim_and_execute(db, worker_id="w1", execute=_noop_execute)
    assert claimed == 0

    row = await db.fetch_one("SELECT status, leased_by FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["status"] == "cancelled"
    assert row["leased_by"] is None


# ── 3. Lease-expiry recovery ─────────────────────────────────────────────


async def test_expired_lease_recovers_to_queued(db: StateDB) -> None:
    run_id = await _submit_host_task(db)

    # Claim it directly via the CAS transition (bypassing execution) so the
    # row is left "running" with a lease, as if a worker crashed mid-task.
    now = time.time()
    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="queued",
            to_state="running",
            reason=StateReason(code="run.started.ok"),
            actor=Actor(type="system", id="w1"),
            idempotency_key=f"claim:{run_id}",
        ),
        patch={"leased_by": "w1", "lease_expires_at": now - 10, "lease_attempts": 1},
    )
    assert result.applied is True

    counts = await reap_expired_leases(db, now=now)
    assert counts == {"requeued": 1, "failed": 0}

    row = await db.fetch_one(
        "SELECT status, leased_by, lease_expires_at, lease_attempts "
        "FROM schedule_runs WHERE id = ?",
        (run_id,),
    )
    assert row["status"] == "queued"
    assert row["leased_by"] is None
    assert row["lease_expires_at"] is None
    assert row["lease_attempts"] == 1  # preserved, not reset


async def test_live_lease_is_never_reaped(db: StateDB) -> None:
    run_id = await _submit_host_task(db)
    now = time.time()
    await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="queued",
            to_state="running",
            reason=StateReason(code="run.started.ok"),
            actor=Actor(type="system", id="w1"),
            idempotency_key=f"claim:{run_id}",
        ),
        patch={"leased_by": "w1", "lease_expires_at": now + 300, "lease_attempts": 1},
    )

    counts = await reap_expired_leases(db, now=now)
    assert counts == {"requeued": 0, "failed": 0}

    row = await db.fetch_one("SELECT status, leased_by FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["status"] == "running"
    assert row["leased_by"] == "w1"


# ── 4. Bounded re-queue (R1) ─────────────────────────────────────────────


async def test_third_lease_expiry_fails_terminal_not_requeued(db: StateDB) -> None:
    run_id = await _submit_host_task(db)
    now = time.time()

    # Cycle claim -> expire MAX_LEASE_ATTEMPTS times.
    for attempt in range(1, MAX_LEASE_ATTEMPTS + 1):
        result = await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="queued",
                to_state="running",
                reason=StateReason(code="run.started.ok"),
                actor=Actor(type="system", id="w1"),
                idempotency_key=f"claim:{run_id}:{attempt}",
            ),
            patch={
                "leased_by": "w1",
                "lease_expires_at": now - 1,
                "lease_attempts": attempt,
            },
        )
        assert result.applied is True

        counts = await reap_expired_leases(db, now=now)
        if attempt < MAX_LEASE_ATTEMPTS:
            assert counts == {"requeued": 1, "failed": 0}
        else:
            assert counts == {"requeued": 0, "failed": 1}

    row = await db.fetch_one(
        "SELECT status, lease_attempts FROM schedule_runs WHERE id = ?", (run_id,)
    )
    assert row["status"] == "failed"
    assert row["lease_attempts"] == MAX_LEASE_ATTEMPTS

    # A subsequent reap pass leaves the terminal row alone (not "running").
    counts = await reap_expired_leases(db, now=now)
    assert counts == {"requeued": 0, "failed": 0}


# ── 5. Terminal re-entry rejected (R2) ───────────────────────────────────


async def test_completed_to_running_rejected(db: StateDB) -> None:
    run_id = await _submit_host_task(db)
    await claim_and_execute(db, worker_id="w1", execute=_noop_execute)

    row = await db.fetch_one("SELECT status FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["status"] == "completed"

    with pytest.raises(ValueError, match="not in the declared transition vocabulary"):
        await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="completed",
                to_state="running",
                reason=StateReason(code="run.started.ok"),
                actor=Actor(type="system", id="w1"),
                idempotency_key=f"reentry:{run_id}",
            ),
        )


async def test_failed_to_running_rejected(db: StateDB) -> None:
    run_id = await _submit_host_task(db)
    await claim_and_execute(db, worker_id="w1", execute=_failing_execute)

    row = await db.fetch_one("SELECT status FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["status"] == "failed"

    with pytest.raises(ValueError, match="not in the declared transition vocabulary"):
        await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="failed",
                to_state="running",
                reason=StateReason(code="run.started.ok"),
                actor=Actor(type="system", id="w1"),
                idempotency_key=f"reentry:{run_id}",
            ),
        )


# ── 6. Claim predicate ───────────────────────────────────────────────────


async def test_capability_carrying_row_is_never_leased(db: StateDB) -> None:
    run_id = await _submit_host_task(db, required_capabilities=["gpu-exclusive"])
    claimed = await claim_and_execute(db, worker_id="w1", execute=_noop_execute)
    assert claimed == 0
    row = await db.fetch_one("SELECT leased_by, status FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["leased_by"] is None
    assert row["status"] == "queued"


async def test_workflow_kind_row_is_declined_not_leased(db: StateDB) -> None:
    run_id = await _submit_host_task(db, action_kind="workflow")
    claimed = await claim_and_execute(db, worker_id="w1", execute=_noop_execute)
    assert claimed == 0
    row = await db.fetch_one("SELECT leased_by, status FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["leased_by"] is None
    assert row["status"] == "queued"


async def test_scheduler_fired_row_is_never_leased(db: StateDB) -> None:
    run_id = await _make_scheduled_queued_row(db)
    claimed = await claim_and_execute(db, worker_id="w1", execute=_noop_execute)
    assert claimed == 0
    row = await db.fetch_one("SELECT leased_by, status FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["leased_by"] is None
    assert row["status"] == "queued"


# ── 7. Round trip ─────────────────────────────────────────────────────────


async def test_submit_claim_execute_completed_round_trip(db: StateDB) -> None:
    run_id = await _submit_host_task(db, args={"action_prompt": "hello"})

    executed_rows: list[dict] = []

    async def _record_execute(row: dict) -> tuple[int, str]:
        executed_rows.append(row)
        return 0, ""

    counts = await worker_tick(db, worker_id="w1", execute=_record_execute)
    assert counts["claimed"] == 1
    assert counts["requeued"] == 0
    assert counts["failed"] == 0

    assert len(executed_rows) == 1
    assert executed_rows[0]["action_args"] == {"action_prompt": "hello"}

    row = await db.fetch_one("SELECT status, leased_by FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["status"] == "completed"
    assert row["leased_by"] == "w1"

    audit = await db.fetch_all(
        "SELECT previous_status, status, reason_code FROM status_transitions "
        "WHERE entity_id = ? ORDER BY created_at",
        (run_id,),
    )
    assert [(a["previous_status"], a["status"]) for a in audit] == [
        ("queued", "running"),
        ("running", "completed"),
    ]
