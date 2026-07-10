# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0101 D3/D4: the local (host-only) worker/claim loop with capability
matching.

v1 ships ONE worker — the Studio daemon engine itself — claiming everything
it can serve. A claim is one guarded CAS through
``lionagi.state.transitions.transition()`` (``queued -> running``) that sets
``leased_by``/``lease_expires_at``/``lease_attempts`` in the same guarded
UPDATE the status move performs; there is no second write and no parallel
CAS path (ADR-0101's scope fence). Execution resolves through the existing
subprocess launcher (``lionagi.studio.scheduler.subprocess``) — this module
never spawns a process itself.

D4 adds the ``workers`` registry: ``worker_tick`` upserts this worker's
heartbeat before every claim pass, and the claim predicate matches a queued
row's ``required_capabilities``/``execution_target`` against the calling
worker's advertised capabilities/execution targets (``capabilities.py``'s
token->class map) instead of the D3-era "capability-free only" exclusion. A
row this worker cannot serve is left ``queued``, never faked. Remote
execution targets and workflow-registry resolution remain later slices
(ADR-0102 and a remote worker binding).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.types import JSON

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition
from lionagi.studio.scheduler import capabilities
from lionagi.studio.scheduler import subprocess as _subprocess

_log = logging.getLogger(__name__)

__all__ = (
    "DEFAULT_HEARTBEAT_TTL_SECONDS",
    "DEFAULT_LEASE_TTL_SECONDS",
    "MAX_LEASE_ATTEMPTS",
    "TASK_WORKER_ENABLED",
    "claim_and_execute",
    "default_execute",
    "reap_expired_leases",
    "register_heartbeat",
    "worker_tick",
)

# Module-level enable flag (ADR-0101 D3 host worker), default ON. The Studio
# daemon's scheduler tick checks this before running a worker_tick pass.
TASK_WORKER_ENABLED = True

DEFAULT_LEASE_TTL_SECONDS = 300.0

# D4: a worker whose heartbeat is older than this is ineligible for NEW
# claims (assignment eligibility only). In-flight leases still recover
# solely via schedule_runs.lease_expires_at -- unrelated to this TTL.
DEFAULT_HEARTBEAT_TTL_SECONDS = 90.0

# D3 v1 default: the local worker serves 'host'-targeted rows only. Callers
# that advertise a worker via register_heartbeat/worker_tick without an
# explicit execution_targets list get this default, preserving D3 behavior.
_DEFAULT_EXECUTION_TARGETS: tuple[str, ...] = ("host",)

# D3 R1: a running lease that lapses this many times without completing goes
# terminal instead of re-queuing forever. Deliberately conservative — policy
# tuning (backoff curves, per-capability bounds) belongs to the
# capability-matching slice, not this one.
MAX_LEASE_ATTEMPTS = 3

# CLAIM CANDIDATES (D4): non-workflow, ad-hoc (schedule_id IS NULL) queued
# rows, oldest first, (queued_at, id) tie-break for determinism. SQL only
# narrows to rows any worker could conceivably serve; capability/target
# matching happens in Python (see claim_and_execute docstring for the full
# keyset-paging rationale). Scheduler-fired rows (schedule_id NOT NULL) are
# never touched here -- governed by SchedulerEngine._fire instead.
#
# First page has no cursor params (separate statement): a nullable
# ":cursor IS NULL OR ..." bind can't be typed by asyncpg on Postgres.
_CLAIM_FIRST_PAGE_SQL = """
    SELECT id, action_kind, action_args, lease_attempts, required_capabilities,
           execution_target, concurrency_key, queued_at
    FROM schedule_runs
    WHERE status = 'queued'
      AND schedule_id IS NULL
      AND action_kind != 'workflow'
    ORDER BY queued_at ASC, id ASC
    LIMIT :page_size
"""

_CLAIM_NEXT_PAGE_SQL = """
    SELECT id, action_kind, action_args, lease_attempts, required_capabilities,
           execution_target, concurrency_key, queued_at
    FROM schedule_runs
    WHERE status = 'queued'
      AND schedule_id IS NULL
      AND action_kind != 'workflow'
      AND (
        queued_at > :after_queued_at
        OR (queued_at = :after_queued_at AND id > :after_id)
      )
    ORDER BY queued_at ASC, id ASC
    LIMIT :page_size
"""

# Rows scanned per page while paging for eligible candidates.
_CLAIM_PAGE_SIZE = 50

# Fairness/latency bound, not a correctness cap: rows scanned across pages
# per claim_and_execute pass before giving up for this tick. A deep queue
# of ineligible rows is still scanned to completion over bounded ticks.
_MAX_CLAIM_SCAN_ROWS = 5000

# D4: same-concurrency_key rows currently 'running' block admission of a new
# claim sharing that key -- advisory ordering only; the worker-side host lock
# stays authoritative over the resource itself (ADR-0101 scope fence).
_RUNNING_CONCURRENCY_KEYS_SQL = """
    SELECT DISTINCT concurrency_key FROM schedule_runs
    WHERE status = 'running' AND concurrency_key IS NOT NULL
"""

_REAP_SELECT_SQL = """
    SELECT id, lease_attempts, lease_expires_at
    FROM schedule_runs
    WHERE status = 'running'
      AND lease_expires_at IS NOT NULL
      AND lease_expires_at < :now
"""

_WORKER_HEARTBEAT_SQL = "SELECT last_heartbeat_at FROM workers WHERE worker_id = :worker_id"

_HEARTBEAT_UPSERT_SQL = """
    INSERT INTO workers (worker_id, advertised_capabilities, execution_targets, last_heartbeat_at)
    VALUES (:worker_id, :advertised_capabilities, :execution_targets, :now)
    ON CONFLICT(worker_id) DO UPDATE SET
        advertised_capabilities = excluded.advertised_capabilities,
        execution_targets       = excluded.execution_targets,
        last_heartbeat_at       = excluded.last_heartbeat_at
"""

ExecuteFn = Callable[[dict[str, Any]], Awaitable[tuple[int, str]]]


def _normalize_json_list(value: Any) -> list[Any]:
    """StateDB JSON columns come back as strings on SQLite but as native
    Python values on Postgres (the cross-dialect contract documented on
    ``StateDB``'s query surface). Every JSON-column read in the claim path
    goes through this so it works identically on both backends: NULL/empty
    -> ``[]``, a string -> ``json.loads`` it, a native list -> pass through.
    """
    if not value:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return list(value)


def _matching_candidates(
    page: Any, *, advertised: list[str], targets: set[str]
) -> list[tuple[Any, list[str]]]:
    """Filter one page of queued rows to those this worker can serve.

    Pulled out of ``claim_and_execute`` so the D4 match rule (subset-match
    on ``required_capabilities`` + ``execution_target`` membership) is
    directly testable against a row shaped either the SQLite way (a JSON
    string) or the Postgres way (a native list/None) without a live
    connection of either dialect.
    """
    candidates: list[tuple[Any, list[str]]] = []
    for row in page:
        required = _normalize_json_list(row["required_capabilities"])
        if not capabilities.worker_can_serve(required, advertised):
            continue
        target = row["execution_target"]
        if target and target not in targets:
            continue
        candidates.append((row, required))
    return candidates


async def register_heartbeat(
    db: StateDB,
    *,
    worker_id: str,
    advertised_capabilities: list[str] | None = None,
    execution_targets: list[str] | None = None,
    now: float | None = None,
) -> None:
    """Upsert *worker_id*'s ``workers`` row and bump ``last_heartbeat_at``.

    Called once per ``worker_tick`` before the claim pass, so a worker
    ticking regularly never reads its own heartbeat as stale. A worker that
    stops ticking falls behind ``DEFAULT_HEARTBEAT_TTL_SECONDS`` and becomes
    ineligible for new claims until it heartbeats again.
    """
    now = now if now is not None else time.time()
    async with db._tx() as conn:
        await conn.execute(
            text(_HEARTBEAT_UPSERT_SQL).bindparams(
                bindparam("advertised_capabilities", type_=JSON),
                bindparam("execution_targets", type_=JSON),
            ),
            {
                "worker_id": worker_id,
                "advertised_capabilities": list(advertised_capabilities or ()),
                "execution_targets": list(execution_targets or _DEFAULT_EXECUTION_TARGETS),
                "now": now,
            },
        )


async def _worker_is_stale(
    db: StateDB, *, worker_id: str, now: float, heartbeat_ttl: float
) -> bool:
    """True iff *worker_id* has a ``workers`` row whose heartbeat is older
    than *heartbeat_ttl*. A worker with no row yet (never heartbeated) is
    treated as not-stale -- there is no signal to distrust, and every
    existing D3 caller that never wrote to ``workers`` keeps claiming."""
    async with db._read() as conn:
        row = (
            (await conn.execute(text(_WORKER_HEARTBEAT_SQL), {"worker_id": worker_id}))
            .mappings()
            .first()
        )
    if row is None or row["last_heartbeat_at"] is None:
        return False
    return (now - row["last_heartbeat_at"]) > heartbeat_ttl


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
    advertised_capabilities: list[str] | None = None,
    execution_targets: list[str] | None = None,
    heartbeat_ttl: float = DEFAULT_HEARTBEAT_TTL_SECONDS,
) -> int:
    """Claim every eligible queued row this worker can serve, then execute each.

    D4 match rule: a queued row R is claimable by this worker iff R's
    eligibility∪serialization capability tokens are a subset of
    *advertised_capabilities* AND R's execution_target is in
    *execution_targets* (a NULL/empty execution_target is claimable by any
    worker). Candidates are then ordered by affinity match (a row whose
    affinity tokens this worker also advertises is preferred), ties broken
    by ``queued_at`` (the SQL order is preserved by a stable sort). A row
    sharing a ``concurrency_key`` with another row already ``running`` (or
    already claimed earlier in this same pass) is skipped -- advisory
    admission only; the worker-side host lock stays authoritative.

    Candidates are collected by paging the queued rows oldest-first through
    a ``(queued_at, id)`` keyset cursor until *limit* eligible candidates
    are found or the queue is exhausted (bounded defensively by
    ``_MAX_CLAIM_SCAN_ROWS`` -- a fairness/latency cap on how much one pass
    scans, not a correctness cap: nothing scanned past it is hidden, a later
    pass resumes the same oldest-first order). This means a long prefix of
    rows this worker cannot serve never permanently hides an eligible row
    behind it, unlike a single fixed-size prefetch window.

    If *worker_id* has a ``workers`` row whose heartbeat is older than
    *heartbeat_ttl*, this pass claims nothing (assignment eligibility gate;
    in-flight leases are unaffected and still recover via
    ``reap_expired_leases``). A worker with no heartbeat history yet (never
    called ``register_heartbeat``/``worker_tick``) is not treated as stale.

    Returns the number of rows claimed (regardless of execution outcome).
    Each claim is one guarded CAS (``queued -> running``) that atomically
    sets ``leased_by``/``lease_expires_at``/``lease_attempts``; a lost race
    or a row another caller already moved (e.g. cancelled) is skipped, not
    retried within this pass.
    """
    execute = execute if execute is not None else default_execute
    now = now if now is not None else time.time()
    advertised = list(advertised_capabilities or ())
    targets = set(execution_targets or _DEFAULT_EXECUTION_TARGETS)

    if await _worker_is_stale(db, worker_id=worker_id, now=now, heartbeat_ttl=heartbeat_ttl):
        return 0

    candidates: list[tuple[Any, list[str]]] = []
    async with db._read() as conn:
        running_keys = {
            r["concurrency_key"]
            for r in (await conn.execute(text(_RUNNING_CONCURRENCY_KEYS_SQL))).mappings().all()
        }

        after_queued_at: float | None = None
        after_id: str = ""
        scanned = 0
        while len(candidates) < limit and scanned < _MAX_CLAIM_SCAN_ROWS:
            if after_queued_at is None:
                stmt = text(_CLAIM_FIRST_PAGE_SQL)
                params: dict[str, Any] = {"page_size": _CLAIM_PAGE_SIZE}
            else:
                stmt = text(_CLAIM_NEXT_PAGE_SQL)
                params = {
                    "after_queued_at": after_queued_at,
                    "after_id": after_id,
                    "page_size": _CLAIM_PAGE_SIZE,
                }
            page = (await conn.execute(stmt, params)).mappings().all()
            if not page:
                break
            scanned += len(page)
            candidates.extend(_matching_candidates(page, advertised=advertised, targets=targets))
            last = page[-1]
            after_queued_at = last["queued_at"]
            after_id = last["id"]
            if len(page) < _CLAIM_PAGE_SIZE:
                break  # queue exhausted

    # Stable sort: preserves the SQL's queued_at ASC order among equal
    # affinity scores, only reordering across different scores.
    candidates.sort(key=lambda item: -capabilities.affinity_score(item[1], advertised))
    # `limit` caps claim attempts, applied after affinity reordering so it
    # never truncates before affinity gets a chance to reorder.
    candidates = candidates[:limit]

    claimed = 0
    for row, _required in candidates:
        concurrency_key = row["concurrency_key"]
        if concurrency_key is not None and concurrency_key in running_keys:
            continue
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
        if concurrency_key is not None:
            # Advisory: nothing else in this same pass may claim a row
            # sharing this key, even after this row finishes executing.
            running_keys.add(concurrency_key)
        # Lease identity travels to the terminal write: if it lapses mid-run
        # and the reaper reassigns the row, this write's guard mismatches
        # and is dropped instead of clobbering the live lease.
        lease_guard = {"leased_by": worker_id, "lease_expires_at": now + lease_ttl}
        await _execute_claimed(db, run_id, row, execute, lease_guard)

    return claimed


async def _execute_claimed(
    db: StateDB, run_id: str, row: Any, execute: ExecuteFn, lease_guard: dict[str, Any]
) -> None:
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
            guard=lease_guard,
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
            guard=lease_guard,
        )


async def worker_tick(
    db: StateDB,
    *,
    worker_id: str,
    execute: ExecuteFn | None = None,
    now: float | None = None,
    lease_ttl: float = DEFAULT_LEASE_TTL_SECONDS,
    advertised_capabilities: list[str] | None = None,
    execution_targets: list[str] | None = None,
    heartbeat_ttl: float = DEFAULT_HEARTBEAT_TTL_SECONDS,
) -> dict[str, int]:
    """One worker tick: heartbeat, then reaper pass, then claim pass.

    Split from any sleep loop so tests (and the Studio daemon's own tick)
    can drive a single pass directly without a timer. The heartbeat upsert
    runs first every tick, so a worker ticking regularly is never stale for
    its own claim pass -- ``heartbeat_ttl`` only bites a worker that stops
    ticking.
    """
    now = now if now is not None else time.time()
    await register_heartbeat(
        db,
        worker_id=worker_id,
        advertised_capabilities=advertised_capabilities,
        execution_targets=execution_targets,
        now=now,
    )
    reaped = await reap_expired_leases(db, now=now)
    claimed = await claim_and_execute(
        db,
        worker_id=worker_id,
        execute=execute,
        now=now,
        lease_ttl=lease_ttl,
        advertised_capabilities=advertised_capabilities,
        execution_targets=execution_targets,
        heartbeat_ttl=heartbeat_ttl,
    )
    return {**reaped, "claimed": claimed}
