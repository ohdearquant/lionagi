# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Service layer for SchedulerEngine StateDB access.

All direct StateDB I/O from the scheduler engine is routed here so _fire()
and friends can be unit-tested by injecting a mock implementation of
SchedulerStateService.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionagi.studio.scheduler import coordination as _coordination

_log = logging.getLogger(__name__)


class SchedulerStateService(Protocol):
    """Protocol satisfied by both the real DB-backed service and test mocks."""

    async def get_schedule(self, schedule_id: str) -> dict[str, Any] | None: ...

    async def list_schedules(self, *, enabled: bool | None = None) -> list[dict[str, Any]]: ...

    async def update_schedule(self, schedule_id: str, **fields: Any) -> None: ...

    async def count_schedule_runs(
        self,
        schedule_id: str,
        *,
        chain_depth: int = 0,
        statuses: tuple[str, ...] = ("completed", "failed", "cancelled"),
        fired_after: float | None = None,
    ) -> int: ...

    async def sum_schedule_spend(self, schedule_id: str) -> dict[str, Any]: ...

    async def metric_value(self, metric: str, window_start: float) -> float: ...

    async def create_schedule_run(self, run: dict[str, Any]) -> None: ...

    async def create_schedule_run_and_advance(
        self,
        run: dict[str, Any],
        *,
        schedule_id: str,
        schedule_fields: dict[str, Any],
    ) -> None: ...

    async def schedule_run_exists_since(self, schedule_id: str, since: float) -> bool: ...

    async def list_undispatched_schedule_runs(self) -> list[dict[str, Any]]: ...

    async def tombstone_and_replace_schedule_run(
        self,
        orphan_id: str,
        replacement_run: dict[str, Any],
        *,
        expected_orphan_status: str = "running",
    ) -> bool: ...

    async def update_schedule_run(self, run_id: str, **fields: Any) -> None: ...

    async def create_invocation(self, invocation: dict[str, Any]) -> None: ...

    async def update_invocation(self, inv_id: str, **fields: Any) -> None: ...

    async def get_invocation(self, invocation_id: str) -> dict[str, Any] | None: ...

    async def compute_files_overlap(
        self, invocation_id: str, *, top_n: int = 5
    ) -> dict[str, Any]: ...

    async def update_status(
        self,
        entity_type: str,
        entity_id: str,
        *,
        new_status: str,
        reason_code: str,
        reason_summary: str,
        evidence_refs: list[dict],
        source: str,
        actor: str,
        metadata: dict | None = None,
        expected_statuses: set[str | None] | frozenset[str | None] | None = None,
    ) -> bool: ...

    async def list_sessions_for_invocation(self, invocation_id: str) -> list[dict[str, Any]]: ...


class _DBSchedulerStateService:
    """Real implementation — each method opens a fresh StateDB context."""

    async def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        async with StateDB() as db:
            return await db.get_schedule(schedule_id)

    async def list_schedules(self, *, enabled: bool | None = None) -> list[dict[str, Any]]:
        async with StateDB() as db:
            return await db.list_schedules(enabled=enabled)

    async def update_schedule(self, schedule_id: str, **fields: Any) -> None:
        async with StateDB() as db:
            await db.update_schedule(schedule_id, **fields)

    async def count_schedule_runs(
        self,
        schedule_id: str,
        *,
        chain_depth: int = 0,
        statuses: tuple[str, ...] = ("completed", "failed", "cancelled"),
        fired_after: float | None = None,
    ) -> int:
        async with StateDB() as db:
            return await db.count_schedule_runs(
                schedule_id,
                chain_depth=chain_depth,
                statuses=statuses,
                fired_after=fired_after,
            )

    async def sum_schedule_spend(self, schedule_id: str) -> dict[str, Any]:
        async with StateDB() as db:
            return await db.sum_schedule_spend(schedule_id)

    async def metric_value(self, metric: str, window_start: float) -> float:
        async with StateDB() as db:
            return await db.metric_value(metric, window_start)

    async def create_schedule_run(self, run: dict[str, Any]) -> None:
        async with StateDB() as db:
            await db.create_schedule_run(run)

    async def create_schedule_run_and_advance(
        self,
        run: dict[str, Any],
        *,
        schedule_id: str,
        schedule_fields: dict[str, Any],
    ) -> None:
        async with StateDB() as db:
            await db.create_schedule_run_and_advance(
                run, schedule_id=schedule_id, schedule_fields=schedule_fields
            )

    async def schedule_run_exists_since(self, schedule_id: str, since: float) -> bool:
        async with StateDB() as db:
            return await db.schedule_run_exists_since(schedule_id, since)

    async def list_undispatched_schedule_runs(self) -> list[dict[str, Any]]:
        async with StateDB() as db:
            return await db.list_undispatched_schedule_runs()

    async def tombstone_and_replace_schedule_run(
        self,
        orphan_id: str,
        replacement_run: dict[str, Any],
        *,
        expected_orphan_status: str = "running",
    ) -> bool:
        async with StateDB() as db:
            return await db.tombstone_and_replace_schedule_run(
                orphan_id,
                replacement_run,
                expected_orphan_status=expected_orphan_status,
            )

    async def update_schedule_run(self, run_id: str, **fields: Any) -> None:
        async with StateDB() as db:
            await db.update_schedule_run(run_id, **fields)

    async def create_invocation(self, invocation: dict[str, Any]) -> None:
        async with StateDB() as db:
            await db.create_invocation(invocation)

    async def update_invocation(self, inv_id: str, **fields: Any) -> None:
        async with StateDB() as db:
            await db.update_invocation(inv_id, **fields)

    async def get_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        async with StateDB() as db:
            return await db.get_invocation(invocation_id)

    async def compute_files_overlap(self, invocation_id: str, *, top_n: int = 5) -> dict[str, Any]:
        async with StateDB() as db:
            return await _coordination.compute_files_overlap(db, invocation_id, top_n=top_n)

    async def update_status(
        self,
        entity_type: str,
        entity_id: str,
        *,
        new_status: str,
        reason_code: str,
        reason_summary: str,
        evidence_refs: list[dict],
        source: str,
        actor: str,
        metadata: dict | None = None,
        expected_statuses: set[str | None] | frozenset[str | None] | None = None,
    ) -> bool:
        async with StateDB() as db:
            return await db.update_status(
                entity_type,
                entity_id,
                new_status=new_status,
                reason_code=reason_code,
                reason_summary=reason_summary,
                evidence_refs=evidence_refs,
                source=source,
                actor=actor,
                metadata=metadata,
                expected_statuses=expected_statuses,
            )

    async def list_sessions_for_invocation(self, invocation_id: str) -> list[dict[str, Any]]:
        async with StateDB() as db:
            return await db.list_sessions_for_invocation(invocation_id)


# ---------------------------------------------------------------------------
# Batched write helpers — group related DB ops that must be atomic per caller
# ---------------------------------------------------------------------------


async def create_skipped_run(
    svc: SchedulerStateService,
    *,
    run_id: str,
    schedule: dict[str, Any],
    trigger_context: dict[str, Any],
    now: float,
    reason_code: str,
    reason_summary: str,
    metadata: dict[str, Any],
) -> None:
    """Create a skipped schedule_run record and its status event."""
    sid = schedule["id"]
    await svc.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": sid,
            "trigger_context": trigger_context,
            "action_kind": schedule["action_kind"],
            "action_args": [],
            "status": "skipped",
            "fired_at": now,
        }
    )
    await svc.update_status(
        "schedule_run",
        run_id,
        new_status="skipped",
        reason_code=reason_code,
        reason_summary=reason_summary,
        evidence_refs=[{"kind": "schedule", "id": sid}],
        source="system",
        actor=sid,
        metadata=metadata,
    )


async def resolve_invocation_terminal(
    svc: SchedulerStateService,
    invocation_id: str,
    *,
    fallback_status: str,
    exit_code: int | None = None,
    exception: BaseException | None = None,
) -> tuple[str, str, str, list[dict], dict]:
    """Resolve terminal invocation status from child sessions."""
    sessions = await svc.list_sessions_for_invocation(invocation_id)
    child_statuses = [str(s.get("status") or "") for s in sessions]
    evidence_refs = [{"kind": "session", "id": s["id"]} for s in sessions if s.get("id")]
    metadata: dict = {"child_statuses": child_statuses}
    if exit_code is not None:
        metadata["exit_code"] = exit_code
    if exception is not None:
        metadata["exception_class"] = type(exception).__name__

    # Precedence: timed_out > failed > aborted > cancelled > completed_empty
    # > completed. completed_empty outranks completed so one silently empty
    # child still taints the invocation's terminal status instead of being
    # averaged away by its siblings' real completions — this is what feeds
    # the scheduler's exit_code-based on_success/on_fail chain decision.
    if child_statuses:
        if any(s == "timed_out" for s in child_statuses):
            return (
                "timed_out",
                RunReasons.TIMED_OUT_DEADLINE,
                "Invocation timed out because at least one child session timed out.",
                evidence_refs,
                metadata,
            )
        if any(s == "failed" for s in child_statuses):
            return (
                "failed",
                RunReasons.FAILED_EXCEPTION,
                "Invocation failed because at least one child session failed.",
                evidence_refs,
                metadata,
            )
        if any(s == "aborted" for s in child_statuses):
            aborted_reasons = {
                str(sess.get("status_reason_code") or "")
                for sess in sessions
                if sess.get("status") == "aborted"
            }
            if RunReasons.CANCELLED_SIGINT in aborted_reasons:
                reason_code = RunReasons.CANCELLED_SIGINT
                reason_summary = "Invocation was interrupted (SIGINT) because a child session was."
            else:
                reason_code = RunReasons.ABORTED_USER
                reason_summary = (
                    "Invocation was aborted because at least one child session was aborted."
                )
            return ("aborted", reason_code, reason_summary, evidence_refs, metadata)
        if any(s == "cancelled" for s in child_statuses):
            return (
                "cancelled",
                RunReasons.CANCELLED_SYSTEM,
                "Invocation was cancelled because at least one child session was cancelled.",
                evidence_refs,
                metadata,
            )
        if any(s == "completed_empty" for s in child_statuses) and all(
            s in ("completed", "completed_empty") for s in child_statuses
        ):
            return (
                "completed_empty",
                RunReasons.COMPLETED_EMPTY_NO_EVIDENCE,
                "Invocation exited clean but at least one child session produced no "
                "commits ahead of base and no artifacts.",
                evidence_refs,
                metadata,
            )
        if all(s == "completed" for s in child_statuses):
            return (
                "completed",
                RunReasons.COMPLETED_OK,
                "All child sessions completed successfully.",
                evidence_refs,
                metadata,
            )

    if fallback_status == "completed":
        return (
            "completed",
            RunReasons.COMPLETED_OK,
            "Invocation process completed successfully.",
            evidence_refs,
            metadata,
        )
    if fallback_status == "timed_out":
        return (
            "timed_out",
            RunReasons.TIMED_OUT_DEADLINE,
            "Invocation process exceeded its deadline.",
            evidence_refs,
            metadata,
        )
    if fallback_status == "aborted":
        return (
            "aborted",
            RunReasons.ABORTED_USER,
            "Invocation process was aborted.",
            evidence_refs,
            metadata,
        )
    if fallback_status == "cancelled":
        return (
            "cancelled",
            RunReasons.CANCELLED_SYSTEM,
            "Invocation process was cancelled by the runtime.",
            evidence_refs,
            metadata,
        )
    if exception is not None:
        return (
            "failed",
            RunReasons.FAILED_EXCEPTION,
            f"{type(exception).__name__}: {exception}",
            evidence_refs,
            metadata,
        )
    if exit_code is not None and exit_code != 0:
        return (
            "failed",
            RunReasons.FAILED_EXIT_NONZERO,
            f"Invocation process failed with exit code {exit_code}.",
            evidence_refs,
            metadata,
        )
    return (
        "failed",
        RunReasons.FAILED_EXCEPTION,
        "Invocation process failed.",
        evidence_refs,
        metadata,
    )


async def flush_run_telemetry(
    svc: SchedulerStateService,
    bus: Any,
    *,
    run_id: str,
    invocation_id: str,
    top_n: int = 5,
) -> dict[str, Any] | None:
    """Compute and persist one run's coordination telemetry exactly once,
    riding the invocation's own terminal write (engine.py calls this only
    after its own ``_guarded_terminal_status("invocation", ...)`` returns
    True -- the same guard that makes that write itself land exactly once,
    see the four `_fire_inner` terminal sites).

    Pulls the bus's accumulated signal counters for *run_id* (popping them
    -- see ``SchedulerSignalBus.pop_run_counters``) and the invocation's
    files-read overlap across its child sessions, then merges both under a
    ``"coordination"`` key in ``invocations.node_metadata`` (a
    read-modify-write, since ``update_invocation`` replaces node_metadata
    wholesale rather than merging it). *bus* is typed ``Any`` to avoid a
    scheduler.signals import here; it only needs ``pop_run_counters``.

    Returns the telemetry dict that was persisted, or ``None`` when there
    is nothing to report at all (no signal ever emitted for this run_id AND
    no file overlap) -- a schedule action that never touches the signal bus
    or the files-read pattern leaves node_metadata untouched, matching the
    "measure-only" surfacing rule (the CLI/monitor summary lines only print
    when non-zero).

    Best-effort: this rides an already-committed terminal write, so a
    failure computing overlap or persisting node_metadata must never
    propagate back into the caller and be mistaken for (or alter) that
    run's actual outcome. Any such failure is logged and swallowed, and the
    counters already popped above are lost with it rather than retried --
    telemetry is measure-only, not authoritative state. Cancellation
    (``asyncio.CancelledError`` and any other backend's cancellation
    exception) is a ``BaseException``, not an ``Exception``, so it is never
    caught here and always propagates.
    """
    signals = bus.pop_run_counters(run_id)
    try:
        overlap = await svc.compute_files_overlap(invocation_id, top_n=top_n)
        if signals is None and not overlap.get("count"):
            return None

        telemetry = {
            "signals": signals or {"emitted": {}, "received": 0, "acted_on": 0},
            "files_overlap": overlap,
        }

        invocation = await svc.get_invocation(invocation_id)
        node_metadata = (invocation or {}).get("node_metadata")
        if isinstance(node_metadata, str):
            try:
                node_metadata = json.loads(node_metadata)
            except (ValueError, TypeError):
                node_metadata = {}
        if not isinstance(node_metadata, dict):
            node_metadata = {}
        node_metadata["coordination"] = telemetry
        await svc.update_invocation(invocation_id, node_metadata=node_metadata)
        return telemetry
    except Exception:
        _log.warning(
            "Failed to flush coordination telemetry for run %s (invocation %s)",
            run_id,
            invocation_id,
            exc_info=True,
        )
        return None


# Singleton for production use
default_scheduler_state: SchedulerStateService = _DBSchedulerStateService()
