# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Service layer for SchedulerEngine StateDB access.

All direct StateDB I/O from the scheduler engine is routed here so _fire()
and friends can be unit-tested by injecting a mock implementation of
SchedulerStateService.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

_log = logging.getLogger(__name__)


class SchedulerStateService(Protocol):
    """Protocol satisfied by both the real DB-backed service and test mocks."""

    async def get_schedule(self, schedule_id: str) -> dict[str, Any] | None: ...

    async def list_schedules(self, *, enabled: bool | None = None) -> list[dict[str, Any]]: ...

    async def update_schedule(self, schedule_id: str, **fields: Any) -> None: ...

    async def count_schedule_runs(self, schedule_id: str, *, chain_depth: int = 0) -> int: ...

    async def create_schedule_run(self, run: dict[str, Any]) -> None: ...

    async def update_schedule_run(self, run_id: str, **fields: Any) -> None: ...

    async def create_invocation(self, invocation: dict[str, Any]) -> None: ...

    async def update_invocation(self, inv_id: str, **fields: Any) -> None: ...

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
    ) -> None: ...

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

    async def count_schedule_runs(self, schedule_id: str, *, chain_depth: int = 0) -> int:
        async with StateDB() as db:
            return await db.count_schedule_runs(schedule_id, chain_depth=chain_depth)

    async def create_schedule_run(self, run: dict[str, Any]) -> None:
        async with StateDB() as db:
            await db.create_schedule_run(run)

    async def update_schedule_run(self, run_id: str, **fields: Any) -> None:
        async with StateDB() as db:
            await db.update_schedule_run(run_id, **fields)

    async def create_invocation(self, invocation: dict[str, Any]) -> None:
        async with StateDB() as db:
            await db.create_invocation(invocation)

    async def update_invocation(self, inv_id: str, **fields: Any) -> None:
        async with StateDB() as db:
            await db.update_invocation(inv_id, **fields)

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
    ) -> None:
        async with StateDB() as db:
            await db.update_status(
                entity_type,
                entity_id,
                new_status=new_status,
                reason_code=reason_code,
                reason_summary=reason_summary,
                evidence_refs=evidence_refs,
                source=source,
                actor=actor,
                metadata=metadata,
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


# Singleton for production use
default_scheduler_state: SchedulerStateService = _DBSchedulerStateService()
