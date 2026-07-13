# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0071 D4/D5: the local (host-only) worker/claim loop with capability
matching.

v1 ships ONE worker — the Studio daemon engine itself. A claim is one guarded
CAS through ``lionagi.state.transitions.transition()`` (``queued -> running``)
that also sets ``leased_by``/``lease_expires_at``/``lease_attempts`` in the
same UPDATE — no second write, no parallel CAS path (ADR-0071 scope fence).
Execution resolves through ``lionagi.studio.scheduler.subprocess``; this
module never spawns a process itself.

D4 adds the ``workers`` registry: ``worker_tick`` upserts this worker's
heartbeat before every claim pass, and the claim predicate matches a queued
row's ``required_capabilities``/``execution_target`` against the calling
worker's advertised capabilities/execution targets. A row this worker cannot
serve is left ``queued``, never faked. Remote execution targets and
workflow-registry resolution remain later slices (ADR-0073, remote worker
binding).

ADR-0071 D3 extracts the per-row admission predicate (capability match,
concurrency-key block, the waiter cap, and the duration guard) into
``lionagi.studio.scheduler.admit.admit()``. A terminal ``AdmissionDecision``
transitions the row ``queued -> skipped`` (never faked as "running") and, when
the submission carried a notify request, emits a ``dispatch_outbox``
notification -- a claim-time rejection
must surface observably even though the submitter is no longer on the wire.
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

from lionagi.dispatch.outbox import enqueue_dispatch
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition
from lionagi.studio.scheduler import capabilities
from lionagi.studio.scheduler import subprocess as _subprocess
from lionagi.studio.scheduler.admit import (
    DEFAULT_KEY_CONCURRENCY,
    DEFAULT_WAITER_CAP_MULTIPLIER,
    AdmissionDecision,
    WorkerCaps,
    admit,
    normalize_action_args,
    notify_request,
)

_log = logging.getLogger(__name__)

__all__ = (
    "DEFAULT_HEARTBEAT_TTL_SECONDS",
    "DEFAULT_KEY_CONCURRENCY",
    "DEFAULT_LEASE_TTL_SECONDS",
    "DEFAULT_WAITER_CAP_MULTIPLIER",
    "MAX_LEASE_ATTEMPTS",
    "TASK_WORKER_ENABLED",
    "claim_and_execute",
    "default_execute",
    "reap_expired_leases",
    "register_heartbeat",
    "worker_tick",
)

# Module-level enable flag (ADR-0071 D4 host worker), default ON. The Studio
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
# stays authoritative over the resource itself (ADR-0071 scope fence).
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
    """Normalize a JSON column that is a string on SQLite but a native list
    on Postgres: NULL/empty -> ``[]``, string -> ``json.loads``, list -> passthrough."""
    if not value:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return list(value)


def _matching_candidates(
    page: Any, *, advertised: list[str], targets: set[str]
) -> list[tuple[Any, list[str]]]:
    """Filter one page of queued rows to those this worker can serve (subset-match
    on ``required_capabilities`` + ``execution_target`` membership)."""
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
    """Upsert *worker_id*'s ``workers`` row and bump ``last_heartbeat_at``. A worker
    that stops calling this falls behind ``DEFAULT_HEARTBEAT_TTL_SECONDS`` and
    becomes ineligible for new claims until it heartbeats again."""
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
    """True iff *worker_id*'s heartbeat is older than *heartbeat_ttl*. A worker
    with no row yet (never heartbeated) is treated as not-stale."""
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
    """Resolve *row*'s action_kind through the existing subprocess launcher. The
    task's ``action_args`` payload carries the same ``action_*``-named keys a
    schedule dict would (``action_model``/``action_prompt``/``action_agent``/...)."""
    action_args = row.get("action_args") or {}
    schedule_like = {"action_kind": row["action_kind"], **action_args}
    # kind='command' spawns an allow-listed executable directly, never
    # through `li` -- resolving the `li` executable is unnecessary.
    li_prefix: list[str] | None = None
    if schedule_like["action_kind"] != "command":
        li_prefix, li_resolve_error = _subprocess.resolve_li_executable()
        if li_prefix is None:
            return 1, f"cannot resolve li executable: {li_resolve_error}"
    try:
        argv, tmp_path = _subprocess.build_argv(schedule_like, {}, executable_prefix=li_prefix)
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"
    invocation_id = uuid.uuid4().hex[:12]
    return await _subprocess.spawn_and_wait(
        argv, invocation_id, tmp_path=tmp_path, action_kind=schedule_like.get("action_kind")
    )


async def reap_expired_leases(db: StateDB, *, now: float | None = None) -> dict[str, int]:
    """Recover or fail rows whose lease has lapsed. A row under
    ``MAX_LEASE_ATTEMPTS`` goes back to ``queued`` (lease columns cleared); at
    or beyond the bound it goes to ``failed`` (terminal). A live (unexpired)
    lease is never touched — the guard on ``lease_expires_at`` closes the race
    between this pass's read and its own guarded write."""
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
    waiter_cap_multiplier: int = DEFAULT_WAITER_CAP_MULTIPLIER,
    key_concurrency: int = DEFAULT_KEY_CONCURRENCY,
) -> int:
    """Claim every eligible queued row this worker can serve, then execute each.

    D4 match rule: row R is claimable iff its capability tokens are a subset
    of *advertised_capabilities* AND its execution_target is in
    *execution_targets* (NULL/empty target = claimable by anyone). Candidates
    are then ordered by affinity match, ties broken by ``queued_at``.

    ADR-0071 D3: each candidate is routed through
    ``lionagi.studio.scheduler.admit.admit()``, which folds in the
    capability match above, the concurrency-key block (a matching key
    currently ``running`` -- this pass or a prior one -- defers the row),
    the per-key waiter cap (*waiter_cap_multiplier* x *key_concurrency*,
    D-Cap), and the duration guard (D6). A deferred decision is skipped
    (left ``queued``, retried next tick) -- advisory admission only; the
    worker-side host lock stays authoritative. A terminal decision
    transitions the row to ``skipped`` and surfaces the reason (see
    ``_reject_claim``).

    Candidates are paged oldest-first through a ``(queued_at, id)`` keyset
    cursor until *limit* eligible candidates are found or the queue is
    exhausted, bounded by ``_MAX_CLAIM_SCAN_ROWS`` (a fairness/latency cap,
    not a correctness cap -- a later pass resumes the same order). A long
    prefix of unservable rows never permanently hides an eligible row behind
    it, unlike a fixed-size prefetch window.

    If *worker_id*'s heartbeat is older than *heartbeat_ttl*, this pass
    claims nothing (in-flight leases still recover via
    ``reap_expired_leases``). A worker with no heartbeat history yet is not
    treated as stale.

    Returns the number of rows claimed (regardless of execution outcome).
    Each claim is one guarded CAS (``queued -> running``); a lost race or a
    row another caller already moved is skipped, not retried within this pass.
    A terminal admission rejection never counts toward the returned total.
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

    # `running_keys` seeds admit()'s pass-local `claimed_keys`: a key claimed
    # earlier in this same pass stays treated as an active holder even after
    # its row goes terminal, before later candidates are examined (unchanged
    # from the pre-extraction behavior).
    worker_caps = WorkerCaps(
        advertised_capabilities=advertised,
        lease_ttl=lease_ttl,
        waiter_cap_multiplier=waiter_cap_multiplier,
        key_concurrency=key_concurrency,
        claimed_keys=running_keys,
    )

    claimed = 0
    for row, _required in candidates:
        decision = await admit(row, worker_caps, db, now=now)
        if not decision.admitted:
            if decision.terminal:
                await _reject_claim(db, row, decision)
            continue
        concurrency_key = row["concurrency_key"]
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
            worker_caps.claimed_keys.add(concurrency_key)
        # Lease identity travels to the terminal write: if it lapses mid-run
        # and the reaper reassigns the row, this write's guard mismatches
        # and is dropped instead of clobbering the live lease.
        lease_guard = {"leased_by": worker_id, "lease_expires_at": now + lease_ttl}
        await _execute_claimed(db, run_id, row, execute, lease_guard)

    return claimed


async def _reject_claim(db: StateDB, row: Any, decision: AdmissionDecision) -> None:
    """Surface a terminal admission rejection observably: the row moves ``queued -> skipped`` carrying
    the rejection reason on the schedule_runs row itself (status_reason_code
    / status_reason_summary), and -- whenever the original submission carried
    a notify request (``admit.notify_request``) -- a ``dispatch_outbox``
    notification is emitted, since the submitter already received a success
    at submit time and is no longer on the wire by claim time.

    Goes through ``StateDB.update_status()`` rather than
    ``lionagi.state.transitions.transition()`` (used by every other
    schedule_run transition in this module): the legacy transition surface
    hardcodes ``write_reason_columns=False`` and only appends to the
    status_transitions audit table, which satisfies every other caller here
    but not this one -- the row's own reason columns must carry it. ``update_status()`` writes those columns by default
    for schedule_run (nothing overrides the schedule_run lifecycle policy's
    ``reason_columns=True``), so this is a one-call, no-side-channel fix that
    leaves claim/complete/fail's own transitions unchanged."""
    run_id = row["id"]
    reason_code = decision.reason_code or RunReasons.SKIPPED_WAITER_CAP_EXCEEDED
    applied = await db.update_status(
        "schedule_run",
        run_id,
        new_status="skipped",
        reason_code=reason_code,
        reason_summary=decision.reason_summary or "",
        source="system",
        actor="task_admission",
        expected_statuses={"queued"},
    )
    if not applied:
        # Lost the race (the row was cancelled or otherwise moved
        # concurrently) -- nothing further to surface.
        return

    action_args = normalize_action_args(row.get("action_args"))
    notify = notify_request(action_args)
    if notify is not None:
        await enqueue_dispatch(
            db,
            kind=notify.get("kind", "terminal_notify"),
            deliver_to=notify["deliver_to"],
            body={
                "schedule_run_id": run_id,
                "reason_code": decision.reason_code,
                "reason_summary": decision.reason_summary,
            },
            dedup_key=notify.get("dedup_key"),
            schedule_run_id=run_id,
        )


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
    waiter_cap_multiplier: int = DEFAULT_WAITER_CAP_MULTIPLIER,
    key_concurrency: int = DEFAULT_KEY_CONCURRENCY,
) -> dict[str, int]:
    """One worker tick: heartbeat, then reaper pass, then claim pass. Split from
    any sleep loop so tests (and the Studio daemon's own tick) can drive a
    single pass directly without a timer."""
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
        waiter_cap_multiplier=waiter_cap_multiplier,
        key_concurrency=key_concurrency,
    )
    return {**reaped, "claimed": claimed}
