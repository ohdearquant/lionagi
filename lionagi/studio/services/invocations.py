# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Invocations service — backs /api/invocations endpoints."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import Query
from sqlalchemy.ext.asyncio import AsyncConnection

from lionagi._errors import NotFoundError
from lionagi.state.db import StateDB, read_only_open_supported, state_db_known_absent
from lionagi.state.health import SessionHealth, classify_session_health, worst_health

from ..registry import studio_route
from ._io import parse_json_col as _parse_json_col


async def _ps_snapshot_async() -> str:
    """Capture process state without blocking Studio's event loop."""
    from .admin import _ps_snapshot

    return await asyncio.to_thread(_ps_snapshot)


def _invocation_health(
    sessions: list[dict[str, Any]],
    *,
    now: float,
    ps_snapshot: str | None,
) -> tuple[str, float | None]:
    """Worst-of health verdict + latest activity timestamp across an
    invocation's child sessions, reusing the session health classifier
    (ADR-0057) rather than a second vocabulary. "unknown"
    when the invocation has no child sessions yet — liveness genuinely
    cannot be determined, never silently defaulted to "healthy"."""
    if not sessions:
        return "unknown", None

    from .admin import _artifacts_path, process_liveness

    healths: list[SessionHealth] = []
    last_activity: float | None = None
    for s in sessions:
        artifacts = _artifacts_path(s)
        process_alive = process_liveness(s, artifacts, ps_snapshot)
        healths.append(
            classify_session_health(
                s,
                now=now,
                process_alive=process_alive,
                has_artifacts=artifacts is not None,
                has_stale_locks=False,
            )
        )
        activity = s.get("last_message_at") or s.get("updated_at") or s.get("started_at")
        if activity is not None and (last_activity is None or activity > last_activity):
            last_activity = activity

    return worst_health(healths).value, last_activity


async def _load_invocations_from_db(
    db: StateDB,
    *,
    skill: str | None = None,
    plugin: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    connection: AsyncConnection,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows = await db.list_invocations(
        skill=skill,
        plugin=plugin,
        status=status,
        limit=limit,
        offset=offset,
        connection=connection,
    )
    children_by_invocation = await db.list_sessions_for_invocations(
        [row["id"] for row in rows], connection=connection
    )
    return rows, children_by_invocation


async def _serialize_invocations(
    rows: list[dict[str, Any]],
    children_by_invocation: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    now = time.time()
    ps_snapshot: str | None = None
    out: list[dict[str, Any]] = []
    for r in rows:
        child_sessions = children_by_invocation[r["id"]]
        if child_sessions and ps_snapshot is None:
            ps_snapshot = await _ps_snapshot_async()
        health, last_activity_at = _invocation_health(
            child_sessions, now=now, ps_snapshot=ps_snapshot
        )
        node_meta = r.get("node_metadata")
        if isinstance(node_meta, str):
            try:
                node_meta = json.loads(node_meta)
            except json.JSONDecodeError:
                node_meta = None
        out.append(
            {
                "id": r["id"],
                "skill": r["skill"],
                "plugin": r.get("plugin"),
                "prompt": r.get("prompt"),
                "started_at": r["started_at"],
                "ended_at": r.get("ended_at"),
                "status": r["status"],
                "status_reason_code": r.get("status_reason_code"),
                "status_reason_summary": r.get("status_reason_summary"),
                "status_evidence_refs": _parse_json_col(r.get("status_evidence_refs")),
                "session_count": r.get("session_count", 0),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "node_metadata": node_meta,
                # ADR-0063: project provenance from the most-recently updated
                # child session.  NULL when the invocation has no sessions yet.
                "project": r.get("project"),
                "project_source": r.get("project_source"),
                # From the schedule_run that fired this invocation (ADR-0070),
                # when it was a scheduled run. NULL for interactive invocations.
                "schedule_run_exit_code": r.get("schedule_run_exit_code"),
                "schedule_run_error_detail": r.get("schedule_run_error_detail"),
                # ADR-0057 health verdict + last-activity, derived from child
                # sessions (issue #2851) — same vocabulary runs already use.
                "health": health,
                "last_activity_at": last_activity_at,
            }
        )
    return out


async def _list_invocations_from_db(
    db: StateDB,
    *,
    skill: str | None = None,
    plugin: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    async with db.read_snapshot() as connection:
        rows, children_by_invocation = await _load_invocations_from_db(
            db,
            skill=skill,
            plugin=plugin,
            status=status,
            limit=limit,
            offset=offset,
            connection=connection,
        )
    return await _serialize_invocations(rows, children_by_invocation)


async def list_invocations(
    *,
    skill: str | None = None,
    plugin: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if state_db_known_absent():
        return []
    async with StateDB(readonly=read_only_open_supported()) as db:
        return await _list_invocations_from_db(
            db,
            skill=skill,
            plugin=plugin,
            status=status,
            limit=limit,
            offset=offset,
        )


async def _count_invocations_from_db(
    db: StateDB,
    *,
    skill: str | None = None,
    plugin: str | None = None,
    status: str | None = None,
    connection: AsyncConnection | None = None,
) -> int:
    return await db.count_invocations(
        skill=skill,
        plugin=plugin,
        status=status,
        connection=connection,
    )


async def count_invocations(
    *,
    skill: str | None = None,
    plugin: str | None = None,
    status: str | None = None,
) -> int:
    if state_db_known_absent():
        return 0
    async with StateDB(readonly=read_only_open_supported()) as db:
        return await _count_invocations_from_db(db, skill=skill, plugin=plugin, status=status)


async def get_invocation(
    invocation_id: str, *, readonly: bool | None = None
) -> dict[str, Any] | None:
    """One invocation with its child sessions, artifacts and derived health.

    ``readonly`` opens the store read-only for callers whose contract says they
    only read. The ordinary open runs schema application, which takes a write
    lock and can issue one-time migration statements, so a caller that promises
    not to write should not be reaching for it. ``None`` (the default) selects
    read-only mode when the configured store supports it and otherwise uses the
    ordinary server-backed connection. Callers may pass an explicit bool when
    their own contract requires it.
    """
    if state_db_known_absent():
        return None
    if readonly is None:
        readonly = read_only_open_supported()
    async with StateDB(readonly=readonly) as db:
        row = await db.get_invocation(invocation_id)
        if row is None:
            return None
        node_meta = row.get("node_metadata")
        if isinstance(node_meta, str):
            try:
                node_meta = json.loads(node_meta)
            except json.JSONDecodeError:
                node_meta = None
        sessions = await db.list_sessions_for_invocation(invocation_id)
        # Structured outcomes alongside child sessions for the invocation detail page.
        artifacts = await db.list_artifacts_for_invocation(invocation_id)
        # The schedule_run that fired this invocation, when scheduled, so the
        # detail page can show exit_code/error_detail without correlating IDs.
        schedule_run = await db.get_schedule_run_by_invocation(invocation_id)
        ps_snapshot: str | None = None
        if sessions:
            ps_snapshot = await _ps_snapshot_async()
        health, last_activity_at = _invocation_health(
            sessions, now=time.time(), ps_snapshot=ps_snapshot
        )
    return {
        "id": row["id"],
        "skill": row["skill"],
        "plugin": row.get("plugin"),
        "prompt": row.get("prompt"),
        "started_at": row["started_at"],
        "ended_at": row.get("ended_at"),
        "status": row["status"],
        "status_reason_code": row.get("status_reason_code"),
        "status_reason_summary": row.get("status_reason_summary"),
        "status_evidence_refs": _parse_json_col(row.get("status_evidence_refs")),
        "session_count": row.get("session_count", 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "node_metadata": node_meta,
        "schedule_run_exit_code": schedule_run.get("exit_code") if schedule_run else None,
        "schedule_run_error_detail": schedule_run.get("error_detail") if schedule_run else None,
        "health": health,
        "last_activity_at": last_activity_at,
        "sessions": [
            {
                "id": s["id"],
                "name": s.get("name"),
                "agent_name": s.get("agent_name"),
                "playbook_name": s.get("playbook_name"),
                "invocation_kind": s.get("invocation_kind"),
                "status": s.get("status"),
                "last_message_at": s.get("last_message_at"),
                "started_at": s.get("started_at"),
                "ended_at": s.get("ended_at"),
                # Model disclosure on the child sessions list.
                "model": s.get("model"),
                "effort": s.get("effort"),
            }
            for s in sessions
        ],
        "artifacts": [_serialize_artifact(a) for a in artifacts],
    }


def _serialize_artifact(row: dict[str, Any]) -> dict[str, Any]:
    """Common artifact projection — decodes JSON content columns so the frontend gets real objects."""
    raw_content = row.get("content")
    if isinstance(raw_content, str):
        parsed = _parse_json_col(raw_content)
        content = parsed if not isinstance(parsed, str) else None
    else:
        content = raw_content
    return {
        "id": row["id"],
        "invocation_id": row.get("invocation_id"),
        "session_id": row.get("session_id"),
        "kind": row["kind"],
        "name": row["name"],
        "created_at": row["created_at"],
        "content": content,
        "file_path": row.get("file_path"),
    }


# ── Artifacts (ADR-0077) ──────────────────────────────────────────────────────


async def list_artifacts_for_session(session_id: str) -> list[dict[str, Any]]:
    if state_db_known_absent():
        return []
    async with StateDB() as db:
        rows = await db.list_artifacts_for_session(session_id)
    return [_serialize_artifact(r) for r in rows]


async def get_artifact(artifact_id: str, *, readonly: bool = False) -> dict[str, Any] | None:
    """One artifact row. ``readonly`` carries the same meaning as in
    :func:`get_invocation`, and defaults to False for the same reason."""
    if state_db_known_absent():
        return None
    async with StateDB(readonly=readonly) as db:
        row = await db.get_artifact(artifact_id)
    return _serialize_artifact(row) if row else None


@studio_route("/invocations/", method="GET", area="invocations", name="list_invocations")
async def list_invocations_route(
    skill: str | None = Query(default=None),
    plugin: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if state_db_known_absent():
        rows: list[dict[str, Any]] = []
        total = 0
        completed_total = 0
    else:
        async with StateDB(readonly=read_only_open_supported()) as db:
            async with db.read_snapshot() as connection:
                raw_rows, children_by_invocation = await _load_invocations_from_db(
                    db,
                    skill=skill,
                    plugin=plugin,
                    status=status,
                    limit=limit,
                    offset=offset,
                    connection=connection,
                )
                total = await _count_invocations_from_db(
                    db,
                    skill=skill,
                    plugin=plugin,
                    status=status,
                    connection=connection,
                )
                completed_total = await _count_invocations_from_db(
                    db,
                    skill=skill,
                    plugin=plugin,
                    status="completed",
                    connection=connection,
                )
            rows = await _serialize_invocations(raw_rows, children_by_invocation)
    # Real totals, not a count of this page: `limit` caps at 200, so counting
    # `rows` instead silently plateaus there with nothing distinguishing an
    # exact count from a capped one. completed_total always means status ==
    # "completed" specifically -- it ignores the caller's own `status` filter
    # (which scopes `total`/`rows`) so success-rate math stays meaningful
    # even when a caller has filtered the list by some other status.
    return {
        "invocations": rows,
        "limit": limit,
        "offset": offset,
        "has_next": len(rows) == limit,
        "total": total,
        "completed_total": completed_total,
    }


@studio_route(
    "/invocations/{invocation_id}", method="GET", area="invocations", name="get_invocation"
)
async def get_invocation_route(invocation_id: str) -> dict[str, Any]:
    data = await get_invocation(invocation_id)
    if data is None:
        raise NotFoundError(f"Invocation '{invocation_id}' not found")
    return data


@studio_route(
    "/artifacts/{artifact_id}",
    method="GET",
    area="invocations",
    tags=["artifacts"],
    name="get_artifact",
)
async def get_artifact_route(artifact_id: str) -> dict[str, Any]:
    data = await get_artifact(artifact_id)
    if data is None:
        raise NotFoundError(f"Artifact '{artifact_id}' not found")
    return data


# Sessions have no router-level artifacts endpoint; this sub-route keeps
# the artifact concern in one place.
@studio_route(
    "/artifacts/by-session/{session_id}", method="GET", area="invocations", tags=["artifacts"]
)
async def list_for_session(session_id: str) -> dict[str, Any]:
    rows = await list_artifacts_for_session(session_id)
    return {"artifacts": rows}
