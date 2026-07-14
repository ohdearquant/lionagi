# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared ``RunView`` joining schedule_runs, invocations,
and sessions behind one outcome-precedence reducer. Every schedule list/detail/
status surface (and eventually `li monitor`) reads through this, not a second
ad hoc reducer.

Precedence: session terminal reason > invocation terminal reason > occurrence
``error_detail`` > status+exit_code fallback.
"""

from __future__ import annotations

from typing import Any

from lionagi.state.db import (
    INVOCATION_TERMINAL_STATUSES,
    SCHEDULE_RUN_TERMINAL_STATUSES,
    SESSION_TERMINAL_STATUSES,
)


def _primary_session(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Deterministic pick among 0+ sessions linked to one invocation — the
    most recently created wins, id as a stable tiebreak."""
    if not sessions:
        return None
    return max(sessions, key=lambda s: (s.get("created_at") or 0, s.get("id") or ""))


def _artifact_paths(sessions: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for s in sessions:
        path = s.get("artifacts_path")
        if path:
            seen.setdefault(path, None)
    return list(seen)


def _session_outcome(session: dict[str, Any], artifacts: list[str]) -> dict[str, Any]:
    status = session.get("status") or ""
    code = session.get("status_reason_code")
    summary = session.get("status_reason_summary")
    if status == "completed":
        code = code or "run.completed.ok"
        summary = summary or (
            f"completed: {len(artifacts)} artifact(s)" if artifacts else "completed: no artifacts"
        )
    elif status == "completed_empty":
        code = code or "run.completed_empty.no_evidence"
        summary = summary or "completed_empty: no artifacts produced"
    else:
        code = code or f"run.failed.{status or 'unknown'}"
        summary = summary or (status or "failed")
    return {"code": code, "summary": summary, "source": "session"}


def _invocation_outcome(invocation: dict[str, Any]) -> dict[str, Any]:
    status = invocation.get("status") or ""
    if status == "completed":
        default_code = "run.completed.ok"
    elif status == "completed_empty":
        default_code = "run.completed_empty.no_evidence"
    else:
        default_code = f"run.failed.{status or 'unknown'}"
    code = invocation.get("status_reason_code") or default_code
    summary = invocation.get("status_reason_summary") or (status or "unknown")
    return {"code": code, "summary": summary, "source": "invocation"}


def _fallback_outcome(run: dict[str, Any]) -> dict[str, Any]:
    status = run.get("status") or "unknown"
    exit_code = run.get("exit_code")
    if status == "running":
        return {"code": "running", "summary": "running", "source": "occurrence"}
    if status == "skipped":
        return {
            "code": "skipped",
            "summary": run.get("error_detail") or "skipped",
            "source": "occurrence",
        }
    if exit_code == 0:
        code = "completed"
    elif exit_code is not None:
        code = "failed_exit_nonzero"
    else:
        code = f"{status}_no_exit_code"
    summary = f"{status} (exit {exit_code})" if exit_code is not None else status
    return {"code": code, "summary": summary, "source": "fallback"}


def _occurrence_outcome(run: dict[str, Any]) -> dict[str, Any]:
    error_detail = run.get("error_detail")
    status = run.get("status") or "dispatch"
    if not error_detail or status in ("running", "skipped"):
        return _fallback_outcome(run)
    return {"code": f"failed_{status}", "summary": error_detail, "source": "occurrence"}


def build_outcome(
    run: dict[str, Any],
    invocation: dict[str, Any] | None,
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Outcome precedence: session > invocation > occurrence error_detail > fallback."""
    primary = _primary_session(sessions)
    if primary is not None and primary.get("status") in SESSION_TERMINAL_STATUSES:
        return _session_outcome(primary, _artifact_paths(sessions))
    if invocation is not None and invocation.get("status") in INVOCATION_TERMINAL_STATUSES:
        return _invocation_outcome(invocation)
    if run.get("error_detail"):
        return _occurrence_outcome(run)
    return _fallback_outcome(run)


def build_run_view(
    run: dict[str, Any],
    invocation: dict[str, Any] | None,
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    """One joined, reconciled view of a schedule occurrence."""
    fired_at = run.get("fired_at")
    ended_at = run.get("ended_at")
    duration_ms = (
        int((ended_at - fired_at) * 1000) if fired_at is not None and ended_at is not None else None
    )
    return {
        "id": run.get("id"),
        "schedule_id": run.get("schedule_id"),
        "status": run.get("status"),
        "exit_code": run.get("exit_code"),
        "fired_at": fired_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "outcome": build_outcome(run, invocation, sessions),
        "invocation_id": run.get("invocation_id"),
        "session_ids": [s["id"] for s in sessions if s.get("id")],
        "artifacts": _artifact_paths(sessions),
    }


def exit_code_for_view(
    run: dict[str, Any] | None,
    invocation: dict[str, Any] | None,
    sessions: list[dict[str, Any]],
) -> int:
    """Reuses the existing shared status vocabulary (cli/_util.py,
    cli/status.py) rather than inventing a second exit-code table: 0 trusted
    completion, 1 ordinary failure/completed_empty/skip, 2 unknown/no run, 3
    running, and the existing timeout/abort/cancel codes where a session or
    invocation status carries one."""
    from lionagi.cli._util import EXIT_CODE_BY_STATUS
    from lionagi.cli.status import EXIT_RUNNING, EXIT_UNKNOWN

    if run is None:
        return EXIT_UNKNOWN
    primary = _primary_session(sessions)
    if primary is not None and primary.get("status") in SESSION_TERMINAL_STATUSES:
        return EXIT_CODE_BY_STATUS.get(primary["status"], 1)
    if invocation is not None and invocation.get("status") in INVOCATION_TERMINAL_STATUSES:
        return EXIT_CODE_BY_STATUS.get(invocation["status"], 1)
    status = run.get("status")
    if status == "running":
        return EXIT_RUNNING
    if status not in SCHEDULE_RUN_TERMINAL_STATUSES:
        return EXIT_UNKNOWN
    return 0 if status == "completed" else 1


# ── DB-backed assembly ──────────────────────────────────────────────────────


async def _linked(
    db: Any, run: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    invocation_id = run.get("invocation_id")
    if not invocation_id:
        return None, []
    invocation = await db.get_invocation(invocation_id)
    sessions = await db.list_sessions_for_invocation(invocation_id)
    return invocation, sessions


async def _run_view_for_run(db: Any, run: dict[str, Any]) -> dict[str, Any]:
    invocation, sessions = await _linked(db, run)
    return build_run_view(run, invocation, sessions)


async def get_run_view(db: Any, run_id: str) -> dict[str, Any] | None:
    run = await db.get_schedule_run(run_id)
    if run is None:
        return None
    return await _run_view_for_run(db, run)


async def list_run_views(
    db: Any,
    schedule_id: str,
    *,
    status: str | list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    runs = await db.list_schedule_runs(schedule_id, status=status, limit=limit, offset=offset)
    return [await _run_view_for_run(db, run) for run in runs]


async def get_schedule_status_view(db: Any, schedule_id: str) -> dict[str, Any] | None:
    schedule = await db.get_schedule(schedule_id)
    if schedule is None:
        return None
    runs = await db.list_schedule_runs(schedule_id, limit=1)
    latest_run: dict[str, Any] | None = None
    exit_code = 2  # EXIT_UNKNOWN — no run recorded yet
    if runs:
        run = runs[0]
        invocation, sessions = await _linked(db, run)
        latest_run = build_run_view(run, invocation, sessions)
        exit_code = exit_code_for_view(run, invocation, sessions)
    return {
        "schedule": {
            "id": schedule["id"],
            "name": schedule.get("name"),
            "enabled": bool(schedule.get("enabled")),
            "trigger_type": schedule.get("trigger_type"),
            "cron_expr": schedule.get("cron_expr"),
            "interval_sec": schedule.get("interval_sec"),
            "next_fire_at": schedule.get("next_fire_at"),
        },
        "latest_run": latest_run,
        "exit_code": exit_code,
    }
