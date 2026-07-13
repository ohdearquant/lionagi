# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0071 D3: the admit() admission seam, in isolation.

Direct unit tests against StateDB fixtures for each admission condition
(capability mismatch, concurrency block, waiter-cap overflow, duration
overflow), plus the pure ``action_args["admission"]`` payload-convention
helpers. Claim-loop integration (the convoy-shape regression and the
claim-time rejection surfacing test) lives in test_worker_admission.py.
"""

from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.types import JSON

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition
from lionagi.studio.scheduler.admit import (
    AdmissionDecision,
    WorkerCaps,
    admit,
    allows_deferred_over_cap,
    declared_max_duration_seconds,
    holder_is_running,
    normalize_action_args,
    notify_request,
    waiter_ahead_count,
)
from lionagi.studio.services.task_applications import TaskApplication, submit_task


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


async def _submit(db: StateDB, **overrides) -> str:
    kwargs = {"action_kind": "agent", "args": {}, "execution_target": "host"}
    kwargs.update(overrides)
    app = TaskApplication(**kwargs)
    return await submit_task(db, app)


async def _insert_raw_queued_row(
    db: StateDB,
    *,
    action_args: dict | None = None,
    required_capabilities: list[str] | None = None,
    concurrency_key: str | None = None,
) -> str:
    """Insert a ``queued`` row directly, bypassing ``submit_task()``'s
    synchronous admission pre-check (ADR-0071 D3) -- for fixture rows
    that deliberately violate a condition ``admit()`` alone should catch at
    claim time, independent of whether submit_task() would also reject it."""
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
                           :now, :now, :now, :concurrency_key,
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
                "concurrency_key": concurrency_key,
                "required_capabilities": required_capabilities or [],
            },
        )
    return run_id


async def _row(db: StateDB, run_id: str) -> dict:
    return dict(
        await db.fetch_one(
            "SELECT id, action_kind, action_args, lease_attempts, required_capabilities, "
            "execution_target, concurrency_key, queued_at FROM schedule_runs WHERE id = ?",
            (run_id,),
        )
    )


async def _mark_running(db: StateDB, run_id: str, *, now: float) -> None:
    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="queued",
            to_state="running",
            reason=StateReason(code=RunReasons.STARTED_OK, summary="test holder"),
            actor=Actor(type="system", id="test-holder"),
            idempotency_key=f"claim:{run_id}",
        ),
        patch={"leased_by": "test-holder", "lease_expires_at": now + 300, "lease_attempts": 1},
    )
    assert result.applied is True


# ── 1. Capability mismatch -> deferred, never terminal ──────────────────────


async def test_capability_mismatch_defers_not_terminal(db: StateDB) -> None:
    run_id = await _submit(db, required_capabilities=["lean-toolchain"])
    row = await _row(db, run_id)
    worker = WorkerCaps(advertised_capabilities=[])

    decision = await admit(row, worker, db)

    assert decision.admitted is False
    assert decision.terminal is False
    assert decision.reason_code is None


async def test_capability_match_and_no_holder_admits(db: StateDB) -> None:
    run_id = await _submit(db, required_capabilities=["lean-toolchain"])
    row = await _row(db, run_id)
    worker = WorkerCaps(advertised_capabilities=["lean-toolchain"])

    decision = await admit(row, worker, db)

    assert decision == AdmissionDecision(admitted=True)


# ── 2. Concurrency block (within waiter cap) -> deferred, never terminal ────


async def test_concurrency_block_within_cap_defers(db: StateDB) -> None:
    now = time.time()
    holder_id = await _submit(db, required_capabilities=["gpu-exclusive"])
    waiter_id = await _submit(db, required_capabilities=["gpu-exclusive"])
    await _mark_running(db, holder_id, now=now)

    row = await _row(db, waiter_id)
    worker = WorkerCaps(advertised_capabilities=["gpu-exclusive"])

    decision = await admit(row, worker, db, now=now)

    assert decision.admitted is False
    assert decision.terminal is False
    assert decision.reason_code is None
    assert "deferred" in decision.reason_summary


async def test_no_holder_running_admits_even_with_shared_key(db: StateDB) -> None:
    """Two rows share a concurrency_key but neither is running yet -- nothing
    blocks the first candidate."""
    run_id = await _submit(db, required_capabilities=["gpu-exclusive"])
    await _submit(db, required_capabilities=["gpu-exclusive"])
    row = await _row(db, run_id)
    worker = WorkerCaps(advertised_capabilities=["gpu-exclusive"])

    decision = await admit(row, worker, db)

    assert decision == AdmissionDecision(admitted=True)


async def test_claimed_keys_pass_local_holder_blocks_even_when_db_shows_no_running_row(
    db: StateDB,
) -> None:
    """The pass-local ``claimed_keys`` set (seeded from a prior claim in the
    same claim_and_execute pass) is treated as an active holder even though
    the DB no longer shows any row 'running' for the key -- this preserves
    the pre-extraction same-pass blocking behavior."""
    run_id = await _submit(db, required_capabilities=["gpu-exclusive"])
    row = await _row(db, run_id)
    key = row["concurrency_key"]
    assert key is not None
    worker = WorkerCaps(advertised_capabilities=["gpu-exclusive"], claimed_keys={key})

    decision = await admit(row, worker, db)

    assert decision.admitted is False
    assert decision.terminal is False
    assert "deferred" in decision.reason_summary


# ── 3. Waiter-cap overflow -> terminal, unless opted into deferred ─────────


async def test_waiter_cap_exceeded_is_terminal_rejection(db: StateDB) -> None:
    now = time.time()
    holder_id = await _submit(db, required_capabilities=["gpu-exclusive"])
    await _mark_running(db, holder_id, now=now)

    # cap = key_concurrency(1) * waiter_cap_multiplier(1) = 1: the first
    # waiter fits, the second is over cap.
    waiter_1 = await _submit(db, required_capabilities=["gpu-exclusive"])
    waiter_2 = await _submit(db, required_capabilities=["gpu-exclusive"])

    worker = WorkerCaps(advertised_capabilities=["gpu-exclusive"], waiter_cap_multiplier=1)

    row1 = await _row(db, waiter_1)
    decision_1 = await admit(row1, worker, db, now=now)
    assert decision_1.admitted is False
    assert decision_1.terminal is False  # within cap

    row2 = await _row(db, waiter_2)
    decision_2 = await admit(row2, worker, db, now=now)
    assert decision_2.admitted is False
    assert decision_2.terminal is True
    assert decision_2.reason_code == RunReasons.SKIPPED_WAITER_CAP_EXCEEDED
    assert "waiter cap" in decision_2.reason_summary


async def test_waiter_cap_exceeded_but_opted_into_deferred_stays_deferred(db: StateDB) -> None:
    now = time.time()
    holder_id = await _submit(db, required_capabilities=["gpu-exclusive"])
    await _mark_running(db, holder_id, now=now)
    waiter_1 = await _submit(db, required_capabilities=["gpu-exclusive"])
    waiter_2 = await _submit(
        db,
        required_capabilities=["gpu-exclusive"],
        args={"admission": {"allow_deferred_over_cap": True}},
    )
    worker = WorkerCaps(advertised_capabilities=["gpu-exclusive"], waiter_cap_multiplier=1)

    await admit(await _row(db, waiter_1), worker, db, now=now)  # seat the first waiter
    decision = await admit(await _row(db, waiter_2), worker, db, now=now)

    assert decision.admitted is False
    assert decision.terminal is False
    assert "opted into deferred" in decision.reason_summary


# ── 4. Duration guard -> terminal, unconditional ────────────────────────────


async def test_duration_guard_rejects_when_at_or_above_lease_ttl(db: StateDB) -> None:
    # submit_task() itself already synchronously rejects this (see
    # test_task_applications.py); raw-insert so admit()'s own claim-time
    # evaluation of the condition is tested in isolation.
    run_id = await _insert_raw_queued_row(
        db, action_args={"admission": {"max_duration_seconds": 300}}
    )
    row = await _row(db, run_id)
    worker = WorkerCaps(lease_ttl=300.0)

    decision = await admit(row, worker, db)

    assert decision.admitted is False
    assert decision.terminal is True
    assert decision.reason_code == RunReasons.SKIPPED_DURATION_EXCEEDS_LEASE
    assert "300" in decision.reason_summary


async def test_duration_guard_allows_when_below_lease_ttl(db: StateDB) -> None:
    run_id = await _submit(
        db,
        args={"admission": {"max_duration_seconds": 60}},
    )
    row = await _row(db, run_id)
    worker = WorkerCaps(lease_ttl=300.0)

    decision = await admit(row, worker, db)

    assert decision == AdmissionDecision(admitted=True)


async def test_duration_guard_absent_declaration_admits(db: StateDB) -> None:
    run_id = await _submit(db)
    row = await _row(db, run_id)
    worker = WorkerCaps(lease_ttl=300.0)

    decision = await admit(row, worker, db)

    assert decision == AdmissionDecision(admitted=True)


async def test_duration_guard_takes_priority_over_capability_mismatch(db: StateDB) -> None:
    """The duration guard is unconditional:
    it is checked regardless of whether the row is also capability-blocked."""
    run_id = await _insert_raw_queued_row(
        db,
        required_capabilities=["lean-toolchain"],
        action_args={"admission": {"max_duration_seconds": 999}},
    )
    row = await _row(db, run_id)
    worker = WorkerCaps(advertised_capabilities=[], lease_ttl=300.0)

    decision = await admit(row, worker, db)

    assert decision.terminal is True
    assert decision.reason_code == RunReasons.SKIPPED_DURATION_EXCEEDS_LEASE


# ── 5. Pure helper functions ─────────────────────────────────────────────


def test_normalize_action_args_handles_json_string_dict_and_junk():
    assert normalize_action_args(None) == {}
    assert normalize_action_args("") == {}
    assert normalize_action_args('{"a": 1}') == {"a": 1}
    assert normalize_action_args({"a": 1}) == {"a": 1}
    assert normalize_action_args("not json") == {}
    assert normalize_action_args("[1, 2]") == {}  # valid JSON, not a dict


def test_declared_max_duration_seconds_parses_numeric_and_ignores_bad_types():
    assert declared_max_duration_seconds({"admission": {"max_duration_seconds": 120}}) == 120.0
    assert declared_max_duration_seconds({"admission": {"max_duration_seconds": 120.5}}) == 120.5
    assert declared_max_duration_seconds({}) is None
    assert declared_max_duration_seconds({"admission": {}}) is None
    assert declared_max_duration_seconds({"admission": {"max_duration_seconds": "120"}}) is None
    assert declared_max_duration_seconds({"admission": {"max_duration_seconds": True}}) is None


def test_allows_deferred_over_cap_reads_admission_opts():
    assert allows_deferred_over_cap({"admission": {"allow_deferred_over_cap": True}}) is True
    assert allows_deferred_over_cap({"admission": {"allow_deferred_over_cap": False}}) is False
    assert allows_deferred_over_cap({}) is False
    assert allows_deferred_over_cap({"admission": "not-a-dict"}) is False


def test_notify_request_requires_deliver_to():
    assert notify_request({"admission": {"notify": {"deliver_to": "lambda:leo"}}}) == {
        "deliver_to": "lambda:leo"
    }
    assert notify_request({"admission": {"notify": {"kind": "terminal_notify"}}}) is None
    assert notify_request({"admission": {"notify": "not-a-dict"}}) is None
    assert notify_request({}) is None


def test_notify_request_rejects_malformed_field_types():
    # deliver_to of the wrong type (e.g. int) must not be surfaced -- it
    # would otherwise crash DispatchSignal validation at claim time, after
    # the row is already persisted as skipped.
    assert notify_request({"admission": {"notify": {"deliver_to": 1}}}) is None
    assert notify_request({"admission": {"notify": {"deliver_to": ""}}}) is None
    assert notify_request({"admission": {"notify": {"deliver_to": None}}}) is None
    assert (
        notify_request({"admission": {"notify": {"deliver_to": "lambda:leo", "kind": 7}}}) is None
    )
    assert (
        notify_request({"admission": {"notify": {"deliver_to": "lambda:leo", "dedup_key": 123}}})
        is None
    )
    # Valid optional fields still pass through unchanged.
    assert notify_request(
        {
            "admission": {
                "notify": {
                    "deliver_to": "lambda:leo",
                    "kind": "terminal_notify",
                    "dedup_key": "abc",
                }
            }
        }
    ) == {
        "deliver_to": "lambda:leo",
        "kind": "terminal_notify",
        "dedup_key": "abc",
    }


# ── 6. waiter_ahead_count / holder_is_running direct coverage ───────────────


async def test_holder_is_running_true_and_false(db: StateDB) -> None:
    now = time.time()
    run_id = await _submit(db, required_capabilities=["gpu-exclusive"])
    row = await _row(db, run_id)
    key = row["concurrency_key"]

    assert await holder_is_running(db, key) is False

    await _mark_running(db, run_id, now=now)
    assert await holder_is_running(db, key) is True


async def test_waiter_ahead_count_excludes_self_and_the_running_holder(db: StateDB) -> None:
    now = time.time()
    holder_id = await _submit(db, required_capabilities=["gpu-exclusive"])
    await _mark_running(db, holder_id, now=now)
    waiter_1 = await _submit(db, required_capabilities=["gpu-exclusive"])
    waiter_2 = await _submit(db, required_capabilities=["gpu-exclusive"])

    row1 = await _row(db, waiter_1)
    key = row1["concurrency_key"]

    ahead_of_waiter_1 = await waiter_ahead_count(
        db, key, before_queued_at=row1["queued_at"], exclude_id=waiter_1
    )
    assert ahead_of_waiter_1 == 0  # only the running holder precedes it, and holders don't count

    row2 = await _row(db, waiter_2)
    ahead_of_waiter_2 = await waiter_ahead_count(
        db, key, before_queued_at=row2["queued_at"], exclude_id=waiter_2
    )
    assert ahead_of_waiter_2 == 1  # waiter_1 is still queued and strictly earlier


async def test_waiter_ahead_count_no_exclude_counts_every_current_waiter(db: StateDB) -> None:
    """The submit-time shape (no exclude_id): every current queued/retry_wait
    waiter for the key counts, since a not-yet-inserted row lands after all
    of them."""
    now = time.time()
    holder_id = await _submit(db, required_capabilities=["gpu-exclusive"])
    await _mark_running(db, holder_id, now=now)
    await _submit(db, required_capabilities=["gpu-exclusive"])
    row = await _row(db, holder_id)
    key = row["concurrency_key"]

    count = await waiter_ahead_count(db, key, before_queued_at=time.time())
    assert count == 1
