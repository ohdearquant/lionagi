# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0071 D3: the admit() seam wired into the worker claim loop.

Covers the convoy-incident regression (N jobs behind one holder, the
(N+1)th over the waiter cap is rejected, not silently queued) and the
requirement that a claim-time terminal rejection surfaces
observably: the row lands on a terminal status carrying the reason, and a
notify-carrying submission produces a dispatch_outbox row. Direct
admit()-in-isolation unit tests live in test_admit.py.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.types import JSON

from lionagi.dispatch.outbox import list_dispatches
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition
from lionagi.studio.scheduler.worker import claim_and_execute
from lionagi.studio.services.task_applications import TaskApplication, submit_task


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


async def _submit(db: StateDB, **overrides) -> str:
    kwargs = {
        "action_kind": "agent",
        "args": {},
        "execution_target": "host",
        "required_capabilities": ["gpu-exclusive"],
    }
    kwargs.update(overrides)
    app = TaskApplication(**kwargs)
    return await submit_task(db, app)


async def _insert_raw_queued_row(
    db: StateDB,
    *,
    action_args: dict | None = None,
    required_capabilities: list[str] | None = None,
) -> str:
    """Insert a ``queued`` row directly, bypassing ``submit_task()``'s
    synchronous admission pre-check (ADR-0071 D3) -- for fixture rows
    that deliberately violate the duration guard, so the CLAIM-TIME path
    (rather than the submit-time short-circuit) is what gets exercised."""
    run_id = str(uuid.uuid4())
    now = time.time()
    async with db._tx() as conn:
        await conn.execute(
            text(
                """INSERT INTO schedule_runs
                   (id, schedule_id, invocation_id, trigger_context,
                    action_kind, action_args, status, chain_depth,
                    fired_at, created_at, queued_at, concurrency_key,
                    required_capabilities, execution_target)
                   VALUES (:id, NULL, NULL, :trigger_context,
                           'agent', :action_args, 'queued', 0,
                           :now, :now, :now, NULL,
                           :required_capabilities, 'host')"""
            ).bindparams(
                bindparam("trigger_context", type_=JSON),
                bindparam("action_args", type_=JSON),
                bindparam("required_capabilities", type_=JSON),
            ),
            {
                "id": run_id,
                "trigger_context": {},
                "action_args": action_args or {},
                "now": now,
                "required_capabilities": required_capabilities or [],
            },
        )
    return run_id


async def _never_completes_execute(row: dict) -> tuple[int, str]:
    # Never called in these tests (the holder is claimed but this fake
    # execute lets the pass move straight to evaluating the remaining
    # candidates without racing a real subprocess).
    return 0, ""


async def _status_of(db: StateDB, run_id: str) -> dict:
    return await db.fetch_one(
        "SELECT status, status_reason_code, status_reason_summary FROM schedule_runs WHERE id = ?",
        (run_id,),
    )


async def _reason_history(db: StateDB, run_id: str) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT status, reason_code, reason_summary FROM status_transitions "
        "WHERE entity_id = ? ORDER BY created_at",
        (run_id,),
    )
    return [dict(r) for r in rows]


# ── 1. Convoy-shape regression ───────────────────────────────────────────


async def test_convoy_third_waiter_over_cap_is_rejected_not_queued(db: StateDB) -> None:
    """Reproduces the convoy incident's shape: one holder claims and runs,
    two waiters stay queued within the (default) cap of 2, and a third
    waiter beyond the cap is terminal-rejected in the very same pass -- not
    silently left queued to busy-wait forever."""
    holder_id = await _submit(db)
    waiter_1 = await _submit(db)
    waiter_2 = await _submit(db)
    waiter_3 = await _submit(db)  # the (N+1)th: over the default cap of 2

    claimed = await claim_and_execute(
        db,
        worker_id="w1",
        execute=_never_completes_execute,
        advertised_capabilities=["gpu-exclusive"],
    )

    assert claimed == 1  # only the holder is actually claimed this pass

    holder_row = await _status_of(db, holder_id)
    assert holder_row["status"] == "completed"

    w1_row = await _status_of(db, waiter_1)
    assert w1_row["status"] == "queued"

    w2_row = await _status_of(db, waiter_2)
    assert w2_row["status"] == "queued"

    w3_row = await _status_of(db, waiter_3)
    assert w3_row["status"] == "skipped"  # rejected, not silently queued


async def test_concurrency_block_holds_across_a_fresh_pass_via_db_state(db: StateDB) -> None:
    """A holder genuinely 'running' in the DB (not merely pass-locally
    claimed) continues to block in-cap waiters on an entirely fresh
    claim_and_execute call -- the DB-backed holder_is_running() check,
    independent of the pass-local claimed_keys seeding."""
    holder_id = await _submit(db)
    waiter_1 = await _submit(db)
    waiter_2 = await _submit(db)

    # Claim the holder directly via a guarded CAS (bypassing execution) so it
    # stays "running" indefinitely, as a real in-flight subprocess would.
    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=holder_id,
            from_state="queued",
            to_state="running",
            reason=StateReason(code=RunReasons.STARTED_OK, summary="held"),
            actor=Actor(type="system", id="test-holder"),
            idempotency_key=f"claim:{holder_id}",
        ),
        patch={"leased_by": "test-holder", "lease_expires_at": None, "lease_attempts": 1},
    )
    assert result.applied is True

    claimed = await claim_and_execute(
        db,
        worker_id="w1",
        execute=_never_completes_execute,
        advertised_capabilities=["gpu-exclusive"],
    )
    assert claimed == 0  # holder already running; waiters stay within cap, deferred

    assert (await _status_of(db, waiter_1))["status"] == "queued"
    assert (await _status_of(db, waiter_2))["status"] == "queued"


# ── 2. Claim-time rejection surfaces observably ──────────────────────────


async def test_claim_time_rejection_persists_reason_on_the_row(db: StateDB) -> None:
    """The rejection reason must
    land on the schedule_runs row itself (status_reason_code /
    status_reason_summary), not only in the status_transitions audit
    history."""
    holder_id = await _submit(db)
    waiter_1 = await _submit(db)
    waiter_2 = await _submit(db)
    waiter_3 = await _submit(db)  # rejected

    await claim_and_execute(
        db,
        worker_id="w1",
        execute=_never_completes_execute,
        advertised_capabilities=["gpu-exclusive"],
    )

    row = await _status_of(db, waiter_3)
    assert row["status"] == "skipped"
    assert row["status_reason_code"] == RunReasons.SKIPPED_WAITER_CAP_EXCEEDED
    assert "waiter cap" in row["status_reason_summary"]

    history = await _reason_history(db, waiter_3)
    assert history[-1]["status"] == "skipped"
    assert history[-1]["reason_code"] == RunReasons.SKIPPED_WAITER_CAP_EXCEEDED
    assert "waiter cap" in history[-1]["reason_summary"]


async def test_notify_carrying_submission_produces_outbox_row_on_claim_time_rejection(
    db: StateDB,
) -> None:
    holder_id = await _submit(db)
    waiter_1 = await _submit(db)
    waiter_2 = await _submit(db)
    rejected_id = await _submit(
        db,
        args={
            "admission": {
                "notify": {"deliver_to": "lambda:leo", "dedup_key": "convoy-test-dedup"},
            }
        },
    )

    await claim_and_execute(
        db,
        worker_id="w1",
        execute=_never_completes_execute,
        advertised_capabilities=["gpu-exclusive"],
    )

    row = await _status_of(db, rejected_id)
    assert row["status"] == "skipped"

    dispatches = await list_dispatches(db)
    matching = [d for d in dispatches if d["schedule_run_id"] == rejected_id]
    assert len(matching) == 1
    dispatch = matching[0]
    assert dispatch["deliver_to"] == "lambda:leo"
    assert dispatch["kind"] == "terminal_notify"
    assert dispatch["dedup_key"] == "convoy-test-dedup"
    payload = dispatch["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    body = payload["body"] if "body" in payload else payload
    assert body["schedule_run_id"] == rejected_id
    assert body["reason_code"] == RunReasons.SKIPPED_WAITER_CAP_EXCEEDED


async def test_no_notify_request_means_no_outbox_row_on_rejection(db: StateDB) -> None:
    holder_id = await _submit(db)
    waiter_1 = await _submit(db)
    waiter_2 = await _submit(db)
    rejected_id = await _submit(db)  # no admission.notify payload

    await claim_and_execute(
        db,
        worker_id="w1",
        execute=_never_completes_execute,
        advertised_capabilities=["gpu-exclusive"],
    )

    assert (await _status_of(db, rejected_id))["status"] == "skipped"
    dispatches = await list_dispatches(db)
    assert [d for d in dispatches if d["schedule_run_id"] == rejected_id] == []


async def test_duration_guard_rejection_at_claim_time_is_never_leased(db: StateDB) -> None:
    """A duration-guard violation is caught even for a row with no
    concurrency contention at all -- the guard is unconditional. Raw-insert
    (bypassing submit_task()'s own synchronous pre-check, covered separately
    in test_task_applications.py) so this exercises admit()'s claim-time
    evaluation specifically. The rejection reason lands on the row's own
    columns, same as the waiter-cap path."""
    run_id = await _insert_raw_queued_row(
        db, action_args={"admission": {"max_duration_seconds": 99999}}
    )

    claimed = await claim_and_execute(db, worker_id="w1", execute=_never_completes_execute)

    assert claimed == 0
    row = await _status_of(db, run_id)
    assert row["status"] == "skipped"
    assert row["status_reason_code"] == RunReasons.SKIPPED_DURATION_EXCEEDS_LEASE
    assert row["status_reason_summary"]
    history = await _reason_history(db, run_id)
    assert history[-1]["reason_code"] == RunReasons.SKIPPED_DURATION_EXCEEDS_LEASE


async def test_notify_carrying_submission_produces_outbox_row_on_duration_guard_rejection(
    db: StateDB,
) -> None:
    """The duration-guard path fires a dispatch_outbox notify row too, not
    only the waiter-cap path -- both terminal-rejection routes share the
    same _reject_claim() surfacing, but each is exercised independently."""
    run_id = await _insert_raw_queued_row(
        db,
        action_args={
            "admission": {
                "max_duration_seconds": 99999,
                "notify": {"deliver_to": "lambda:leo", "dedup_key": "duration-guard-dedup"},
            }
        },
    )

    await claim_and_execute(db, worker_id="w1", execute=_never_completes_execute)

    row = await _status_of(db, run_id)
    assert row["status"] == "skipped"
    assert row["status_reason_code"] == RunReasons.SKIPPED_DURATION_EXCEEDS_LEASE

    dispatches = await list_dispatches(db)
    matching = [d for d in dispatches if d["schedule_run_id"] == run_id]
    assert len(matching) == 1
    dispatch = matching[0]
    assert dispatch["deliver_to"] == "lambda:leo"
    assert dispatch["kind"] == "terminal_notify"
    assert dispatch["dedup_key"] == "duration-guard-dedup"
    payload = dispatch["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    body = payload["body"] if "body" in payload else payload
    assert body["schedule_run_id"] == run_id
    assert body["reason_code"] == RunReasons.SKIPPED_DURATION_EXCEEDS_LEASE
