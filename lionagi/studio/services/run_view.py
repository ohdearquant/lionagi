# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared ``RunView`` joining schedule_runs, invocations,
and sessions behind one outcome-precedence reducer. Every schedule list/detail/
status surface (and eventually `li monitor`) reads through this, not a second
ad hoc reducer.

Precedence: session terminal reason > invocation terminal reason > occurrence
skipped reason or ``error_detail`` > status+exit_code fallback.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, inspect, text

from lionagi.state.db import (
    INVOCATION_TERMINAL_STATUSES,
    SCHEDULE_RUN_TERMINAL_STATUSES,
    SESSION_TERMINAL_STATUSES,
)

__all__ = (
    "build_outcome",
    "build_run_view",
    "build_run_view_for",
    "exit_code_for_view",
    "get_run_view",
    "get_schedule_status_view",
    "list_run_views",
)


def _grading_session(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one session a run's outcome is graded from, or None to fall through.

    The rule, because "which of N sessions speaks for the run" is a decision and
    not a detail: an occurrence has a session-level outcome only when *every*
    linked session is terminal, and that outcome is the **least-trusted** of
    them -- the session whose status maps to the highest exit code in the
    shared table. Recency then id break ties among equally-trusted sessions.

    Both halves are load-bearing. A fan-out links many sessions and a caller
    asking "did this run succeed" is asking about all of them, so grading from
    one representative session is sound only when that session is the one able
    to turn the answer to no; picking any other lets six killed sessions report
    ok because a seventh finished. And a run with a session still in flight has
    not finished, so it has nothing terminal to say yet and the caller falls
    through to the invocation and occurrence, which do know whether it is still
    running.

    Determinism was never the property in short supply here -- picking the
    newest was already perfectly deterministic, and wrong. Representativeness
    is what the callers need, and it is what this picks for.

    A run with exactly one session grades identically to before; the behaviour
    changes only where there is more than one session to disagree about.
    """
    from lionagi.cli._util import EXIT_CODE_BY_STATUS

    if not sessions:
        return None
    if any(s.get("status") not in SESSION_TERMINAL_STATUSES for s in sessions):
        return None
    return max(
        sessions,
        key=lambda s: (
            EXIT_CODE_BY_STATUS.get(s.get("status") or "", 1),
            s.get("created_at") or 0,
            s.get("id") or "",
        ),
    )


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
    reported = bool(summary)
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
    return {"code": code, "summary": summary, "source": "session", "summary_reported": reported}


def _invocation_outcome(invocation: dict[str, Any]) -> dict[str, Any]:
    status = invocation.get("status") or ""
    if status == "completed":
        default_code = "run.completed.ok"
    elif status == "completed_empty":
        default_code = "run.completed_empty.no_evidence"
    else:
        default_code = f"run.failed.{status or 'unknown'}"
    code = invocation.get("status_reason_code") or default_code
    reported = bool(invocation.get("status_reason_summary"))
    summary = invocation.get("status_reason_summary") or (status or "unknown")
    return {
        "code": code,
        "summary": summary,
        "source": "invocation",
        "summary_reported": reported,
    }


def _fallback_outcome(run: dict[str, Any]) -> dict[str, Any]:
    status = run.get("status") or "unknown"
    exit_code = run.get("exit_code")
    if status == "running":
        return {
            "code": "running",
            "summary": "running",
            "source": "occurrence",
            "summary_reported": False,
        }
    if status == "skipped":
        return {
            "code": "skipped",
            "summary": run.get("reason_summary") or run.get("error_detail") or "skipped",
            "source": "occurrence",
            "summary_reported": bool(run.get("reason_summary") or run.get("error_detail")),
        }
    if status == "completed":
        code = "completed"
    elif exit_code is None:
        code = f"{status}_no_exit_code"
    elif exit_code != 0:
        code = f"{status}_exit_nonzero"
    else:
        code = status
    summary = f"{status} (exit {exit_code})" if exit_code is not None else status
    return {"code": code, "summary": summary, "source": "fallback", "summary_reported": False}


def _occurrence_outcome(run: dict[str, Any]) -> dict[str, Any]:
    error_detail = run.get("error_detail")
    status = run.get("status") or "dispatch"
    if not error_detail or status in ("running", "skipped"):
        return _fallback_outcome(run)
    return {
        "code": f"failed_{status}",
        "summary": error_detail,
        "source": "occurrence",
        "summary_reported": True,
    }


def build_outcome(
    run: dict[str, Any],
    invocation: dict[str, Any] | None,
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Outcome precedence: session > invocation > occurrence reason/error > fallback.

    Each branch declares whether its summary is text a caller reported (a free-text
    column) or a string this module generated. Only the branch that produced a
    summary knows which it is, so a list surface cannot re-derive it downstream.
    """
    primary = _grading_session(sessions)
    if primary is not None:
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
        "reason_code": run.get("reason_code"),
        "reason_summary": run.get("reason_summary"),
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
    cli/_inspection.py) rather than inventing a second exit-code table: 0 trusted
    completion, 1 ordinary failure/completed_empty/skip, 2 unknown/no run, 3
    running, and the existing timeout/abort/cancel codes where a session or
    invocation status carries one."""
    from lionagi.cli._inspection import EXIT_RUNNING, EXIT_UNKNOWN
    from lionagi.cli._util import EXIT_CODE_BY_STATUS

    if run is None:
        return EXIT_UNKNOWN
    primary = _grading_session(sessions)
    if primary is not None:
        return EXIT_CODE_BY_STATUS.get(primary["status"], 1)
    if invocation is not None and invocation.get("status") in INVOCATION_TERMINAL_STATUSES:
        return EXIT_CODE_BY_STATUS.get(invocation["status"], 1)
    status = run.get("status")
    if status == "running":
        return EXIT_RUNNING
    if status not in SCHEDULE_RUN_TERMINAL_STATUSES:
        return EXIT_UNKNOWN
    if status in EXIT_CODE_BY_STATUS:
        return EXIT_CODE_BY_STATUS[status]
    return 0 if status == "completed" else 1


# ── DB-backed assembly ──────────────────────────────────────────────────────


async def _with_skip_reasons(db: Any, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read one latest reason per skipped run in bounded sets, without migrations."""
    run_ids = [run["id"] for run in runs if run.get("status") == "skipped"]
    if not run_ids:
        return runs
    reasons: dict[str, dict[str, Any]] = {}
    async with db.read_snapshot() as connection:
        if not await connection.run_sync(
            lambda conn: inspect(conn).has_table("status_transitions")
        ):
            return runs
        owner_where, owner_params = db._owner_predicate("status_transitions")
        # The owner predicate is fixed SQL from StateDB; all values are bound.
        statement = text(
            "SELECT entity_id, reason_code, reason_summary FROM ("  # noqa: S608
            "SELECT entity_id, reason_code, reason_summary, "
            "ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY created_at DESC, id DESC) AS rank "
            "FROM status_transitions WHERE entity_type = 'schedule_run' "
            "AND status = 'skipped' AND entity_id IN :run_ids"
            + owner_where
            + ") AS reasons WHERE rank = 1"
        ).bindparams(bindparam("run_ids", expanding=True))
        # Keep bound parameter counts below SQLite's limit even for in-process
        # readers that request more rows than the HTTP page maximum.
        for offset in range(0, len(run_ids), 500):
            events = await connection.execute(
                statement, {"run_ids": run_ids[offset : offset + 500], **owner_params}
            )
            for event in events.mappings():
                reasons[event["entity_id"]] = {
                    "reason_code": event["reason_code"],
                    "reason_summary": event["reason_summary"],
                }
    return [{**run, **reasons.get(run["id"], {})} for run in runs]


async def _with_skip_reason(db: Any, run: dict[str, Any]) -> dict[str, Any]:
    return (await _with_skip_reasons(db, [run]))[0]


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
    """Full schedule_runs row with RunView fields layered on top, additively —
    never a replacement projection."""
    view = await build_run_view_for(db, run)
    return {**run, **view}


async def build_run_view_for(db: Any, run: dict[str, Any]) -> dict[str, Any]:
    """Reconciled RunView fields (outcome/duration_ms/session_ids/artifacts)
    for an already-fetched ``schedule_runs`` row — for callers that already
    hold the row and must not re-read it (a second independent read could
    observe a different row state on a live daemon)."""
    run = await _with_skip_reason(db, run)
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
    runs = await _with_skip_reasons(db, runs)
    views = []
    for run in runs:
        invocation, sessions = await _linked(db, run)
        views.append({**run, **build_run_view(run, invocation, sessions)})
    return views


async def get_schedule_status_view(db: Any, schedule_id: str) -> dict[str, Any] | None:
    schedule = await db.get_schedule(schedule_id)
    if schedule is None:
        return None
    runs = await db.list_schedule_runs(schedule_id, limit=1)
    latest_run: dict[str, Any] | None = None
    exit_code = 2  # EXIT_UNKNOWN — no run recorded yet
    if runs:
        run = await _with_skip_reason(db, runs[0])
        invocation, sessions = await _linked(db, run)
        # Layered on the occurrence row, like the list and detail paths: the
        # reconciled fields alone drop the row's own error text, and a projection
        # that classifies a run's failure has nothing left to read when the winning
        # layer reported no reason of its own.
        latest_run = {**run, **build_run_view(run, invocation, sessions)}
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
