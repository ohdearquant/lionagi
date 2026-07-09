# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0101 D3: the local (host-only) worker/claim loop.

v1 ships ONE worker — the Studio daemon engine itself — claiming everything
it can serve. A claim is one guarded CAS through
``lionagi.state.transitions.transition()`` (``queued -> running``) that sets
``leased_by``/``lease_expires_at``/``lease_attempts`` in the same guarded
UPDATE the status move performs; there is no second write and no parallel
CAS path (ADR-0101's scope fence). Execution resolves through the existing
subprocess launcher (``lionagi.studio.scheduler.subprocess``) — this module
never spawns a process itself.

Capability matching, remote execution targets, workers-table heartbeats, and
workflow-registry resolution are later slices (D4 / ADR-0102): a row this
worker cannot serve under the D3 claim predicate is left ``queued``, never
faked.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition
from lionagi.studio.scheduler import subprocess as _subprocess

_log = logging.getLogger(__name__)

__all__ = (
    "DEFAULT_LEASE_TTL_SECONDS",
    "MAX_LEASE_ATTEMPTS",
    "TASK_WORKER_ENABLED",
    "claim_and_execute",
    "default_execute",
    "reap_expired_leases",
    "worker_tick",
)

# Module-level enable flag (ADR-0101 D3 host worker), default ON. The Studio
# daemon's scheduler tick checks this before running a worker_tick pass.
TASK_WORKER_ENABLED = True

DEFAULT_LEASE_TTL_SECONDS = 300.0

# D3 R1: a running lease that lapses this many times without completing goes
# terminal instead of re-queuing forever. Deliberately conservative — policy
# tuning (backoff curves, per-capability bounds) belongs to the
# capability-matching slice, not this one.
MAX_LEASE_ATTEMPTS = 3

# CLAIM PREDICATE (D3, conservative v1): only host-targeted, capability-free,
# non-workflow, ad-hoc (schedule_id IS NULL) rows are eligible. Scheduler-fired
# rows (schedule_id NOT NULL) are never touched — that path is governed by
# SchedulerEngine._fire, not this worker.
_CLAIM_SELECT_SQL = """
    SELECT id, action_kind, action_args, lease_attempts
    FROM schedule_runs
    WHERE status = 'queued'
      AND schedule_id IS NULL
      AND execution_target = 'host'
      AND (required_capabilities IS NULL OR required_capabilities = '[]')
      AND action_kind != 'workflow'
    ORDER BY queued_at ASC
    LIMIT :limit
"""

_REAP_SELECT_SQL = """
    SELECT id, lease_attempts, lease_expires_at
    FROM schedule_runs
    WHERE status = 'running'
      AND lease_expires_at IS NOT NULL
      AND lease_expires_at < :now
"""

ExecuteFn = Callable[[dict[str, Any]], Awaitable[tuple[int, str]]]


async def default_execute(row: dict[str, Any]) -> tuple[int, str]:
    """Resolve *row*'s action_kind through the existing subprocess launcher.

    Reuses the scheduler's own action_kind vocabulary and argv builder
    rather than a second launcher: the task application's ``action_args``
    payload carries the same ``action_*``-named keys a schedule dict would
    (``action_model``/``action_prompt``/``action_agent``/...).
    """
    action_args = row.get("action_args") or {}
    schedule_like = {"action_kind": row["action_kind"], **action_args}
    li_prefix, li_resolve_error = _subprocess.resolve_li_executable()
    if li_prefix is None:
        return 1, f"cannot resolve li executable: {li_resolve_error}"
    try:
        argv, tmp_path = _subprocess.build_argv(schedule_like, {}, executable_prefix=li_prefix)
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"
    invocation_id = uuid.uuid4().hex[:12]
    return await _subprocess.spawn_and_wait(argv, invocation_id, tmp_path=tmp_path)


async def reap_expired_leases(db: StateDB, *, now: float | None = None) -> dict[str, int]:
    """Recover or fail rows whose lease has lapsed.

    A row under ``MAX_LEASE_ATTEMPTS`` goes back to ``queued`` (recovery,
    clearing the lease columns); at or beyond the bound it goes to
    ``failed`` (terminal) — unbounded requeue is impossible by construction.
    A live (unexpired) lease is never touched: the guard on
    ``lease_expires_at`` closes the race between this pass's read and its
    own guarded write.
    """
    now = now if now is not None else time.time()
    counts = {"requeued": 0, "failed": 0}

    async with db._read() as conn:
        rows = (await conn.execute(text(_REAP_SELECT_SQL), {"now": now})).mappings().all()

    for row in rows:
        run_id = row["id"]
        if row["lease_attempts"] >= MAX_LEASE_ATTEMPTS:
            result = await transition(
                db,
                TransitionRequest(
                    entity_type="schedule_run",
                    entity_id=run_id,
                    from_state="running",
                    to_state="failed",
                    reason=StateReason(
                        code=RunReasons.FAILED_LEASE_ATTEMPTS_EXHAUSTED,
                        summary=(
                            f"lease expired {row['lease_attempts']} time(s); "
                            f"bound ({MAX_LEASE_ATTEMPTS}) reached"
                        ),
                    ),
                    actor=Actor(type="system", id="task_worker_reaper"),
                    idempotency_key=f"lease_exhausted:{run_id}:{row['lease_attempts']}",
                ),
                guard={"lease_expires_at": row["lease_expires_at"]},
            )
            if result.applied:
                counts["failed"] += 1
        else:
            result = await transition(
                db,
                TransitionRequest(
                    entity_type="schedule_run",
                    entity_id=run_id,
                    from_state="running",
                    to_state="queued",
                    reason=StateReason(
                        code=RunReasons.QUEUED_LEASE_EXPIRED,
                        summary="lease expired before completion; recovered for re-claim",
                    ),
                    actor=Actor(type="system", id="task_worker_reaper"),
                    idempotency_key=f"lease_expired:{run_id}:{row['lease_attempts']}",
                ),
                guard={"lease_expires_at": row["lease_expires_at"]},
                patch={"leased_by": None, "lease_expires_at": None},
            )
            if result.applied:
                counts["requeued"] += 1

    return counts


async def claim_and_execute(
    db: StateDB,
    *,
    worker_id: str,
    execute: ExecuteFn | None = None,
    now: float | None = None,
    lease_ttl: float = DEFAULT_LEASE_TTL_SECONDS,
    limit: int = 20,
) -> int:
    """Claim every eligible queued row this worker can serve, then execute each.

    Returns the number of rows claimed (regardless of execution outcome).
    Each claim is one guarded CAS (``queued -> running``) that atomically
    sets ``leased_by``/``lease_expires_at``/``lease_attempts``; a lost race
    or a row another caller already moved (e.g. cancelled) is skipped, not
    retried within this pass.
    """
    execute = execute if execute is not None else default_execute
    now = now if now is not None else time.time()

    async with db._read() as conn:
        rows = (await conn.execute(text(_CLAIM_SELECT_SQL), {"limit": limit})).mappings().all()

    claimed = 0
    for row in rows:
        run_id = row["id"]
        result = await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="queued",
                to_state="running",
                reason=StateReason(
                    code=RunReasons.STARTED_OK,
                    summary=f"claimed by host worker {worker_id}",
                ),
                actor=Actor(type="system", id=worker_id),
                idempotency_key=f"claim:{run_id}:{worker_id}:{now}",
            ),
            patch={
                "leased_by": worker_id,
                "lease_expires_at": now + lease_ttl,
                "lease_attempts": row["lease_attempts"] + 1,
            },
        )
        if not result.applied:
            continue
        claimed += 1
        await _execute_claimed(db, run_id, row, execute)

    return claimed


async def _execute_claimed(db: StateDB, run_id: str, row: Any, execute: ExecuteFn) -> None:
    task_row = dict(row)
    action_args = task_row.get("action_args")
    if isinstance(action_args, str):
        task_row["action_args"] = json.loads(action_args) if action_args else {}

    try:
        exit_code, error_detail = await execute(task_row)
    except Exception as exc:  # noqa: BLE001
        exit_code, error_detail = 1, f"{type(exc).__name__}: {exc}"

    completion_time = time.time()
    if exit_code == 0:
        await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="running",
                to_state="completed",
                reason=StateReason(
                    code=RunReasons.COMPLETED_OK, summary="task execution completed"
                ),
                actor=Actor(type="system", id="task_worker"),
                idempotency_key=f"complete:{run_id}:{completion_time}",
            ),
        )
    else:
        await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="running",
                to_state="failed",
                reason=StateReason(
                    code=RunReasons.FAILED_EXIT_NONZERO,
                    summary=(error_detail or f"exit code {exit_code}")[:500],
                ),
                actor=Actor(type="system", id="task_worker"),
                idempotency_key=f"fail:{run_id}:{completion_time}",
            ),
        )


async def worker_tick(
    db: StateDB,
    *,
    worker_id: str,
    execute: ExecuteFn | None = None,
    now: float | None = None,
    lease_ttl: float = DEFAULT_LEASE_TTL_SECONDS,
) -> dict[str, int]:
    """One worker tick: reaper pass then claim pass.

    Split from any sleep loop so tests (and the Studio daemon's own tick)
    can drive a single pass directly without a timer.
    """
    now = now if now is not None else time.time()
    reaped = await reap_expired_leases(db, now=now)
    claimed = await claim_and_execute(
        db, worker_id=worker_id, execute=execute, now=now, lease_ttl=lease_ttl
    )
    return {**reaped, "claimed": claimed}
