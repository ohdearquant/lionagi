# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Coherent, bounded active-work snapshot for Studio projections."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

import aiosqlite
from fastapi import Query

from lionagi.state.health import SessionHealth
from lionagi.state.session_naming import resolve_display_name

from ..registry import studio_route
from . import invocations as _invocations_svc
from . import runs as _runs_svc
from ._db import open_db as _open_db
from ._db import require_file_store, store_exists, store_path
from ._io import parse_json_col as _parse_json_col
from .artifact_verification import resolve_artifact_verification
from .sessions import _escape_like

MAX_ACTIVE_SNAPSHOT_ROWS = 500


def _session_scope(
    *,
    alias: str,
    project: str | None,
    project_null: bool,
    search: str | None,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if project_null:
        clauses.append(f"{alias}.project IS NULL")
    elif project:
        clauses.append(f"{alias}.project = ?")
        params.append(project)
    if search:
        escaped = _escape_like(search)
        clauses.append(
            f"(LOWER(COALESCE({alias}.name, '')) LIKE '%' || LOWER(?) || '%' ESCAPE '\\' "
            f"OR LOWER(COALESCE({alias}.agent_name, '')) LIKE '%' || LOWER(?) || '%' ESCAPE '\\')"
        )
        params.extend([escaped, escaped])
    return clauses, params


def _where(clauses: list[str]) -> str:
    return "WHERE " + " AND ".join(clauses) if clauses else ""


_SESSION_COLUMNS = """
    s.id,
    s.name,
    s.created_at,
    s.updated_at,
    s.playbook_name,
    s.agent_name,
    s.invocation_kind,
    s.show_topic,
    s.show_play_name,
    s.artifacts_path,
    s.artifact_contract_json,
    s.artifact_verification_json,
    s.source_kind,
    s.status,
    s.started_at,
    s.ended_at,
    s.last_message_at,
    s.invocation_id,
    s.model,
    s.provider,
    s.effort,
    s.agent_hash,
    s.project,
    s.project_source,
    s.status_reason_code,
    s.status_reason_summary,
    s.node_metadata,
    s.total_cost_usd,
    s.input_tokens,
    s.output_tokens,
    COUNT(DISTINCT b.id) AS branch_count,
    COALESCE(SUM(json_array_length(p.collection)), 0) AS message_count
"""


async def _session_rows(
    db: aiosqlite.Connection,
    *,
    clauses: list[str],
    params: list[Any],
    limit: int,
) -> list[dict[str, Any]]:
    where = _where(clauses)
    cursor = await db.execute(
        f"""
        WITH page AS (
            SELECT s.id AS page_id
            FROM sessions s
            {where}
            ORDER BY s.updated_at DESC, s.id DESC
            LIMIT ?
        )
        SELECT {_SESSION_COLUMNS}
        FROM page
        JOIN sessions s ON s.id = page.page_id
        LEFT JOIN branches b ON b.session_id = s.id
        LEFT JOIN progressions p ON p.id = b.progression_id
        GROUP BY s.id
        ORDER BY s.updated_at DESC, s.id DESC
        """,  # noqa: S608
        [*params, limit],
    )
    return [dict(row) for row in await cursor.fetchall()]


async def _count_sessions(
    db: aiosqlite.Connection, *, clauses: list[str], params: list[Any]
) -> int:
    cursor = await db.execute(
        f"SELECT COUNT(*) AS n FROM sessions s {_where(clauses)}",  # noqa: S608
        params,
    )
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0


def _invocation_scope(
    *,
    project: str | None,
    project_null: bool,
    search: str | None,
    active_children_only: bool,
) -> tuple[str, list[Any]]:
    child_clauses, params = _session_scope(
        alias="child", project=project, project_null=project_null, search=search
    )
    if not child_clauses:
        return "", []
    if active_children_only:
        child_clauses.insert(0, "child.status = 'running'")
    scope_sql = (
        "EXISTS (SELECT 1 FROM sessions child "  # noqa: S608 -- fixed clauses
        "WHERE child.invocation_id = inv.id AND " + " AND ".join(child_clauses) + ")"
    )
    return scope_sql, params


_INVOCATION_COLUMNS = """
    inv.*,
    (SELECT child.project FROM sessions child
     WHERE child.invocation_id = inv.id
     ORDER BY COALESCE(child.updated_at, 0) DESC,
              COALESCE(child.created_at, 0) DESC, child.id DESC LIMIT 1) AS project,
    (SELECT child.project_source FROM sessions child
     WHERE child.invocation_id = inv.id
     ORDER BY COALESCE(child.updated_at, 0) DESC,
              COALESCE(child.created_at, 0) DESC, child.id DESC LIMIT 1) AS project_source
"""


async def _invocation_rows(
    db: aiosqlite.Connection,
    *,
    clauses: list[str],
    params: list[Any],
    limit: int,
) -> list[dict[str, Any]]:
    cursor = await db.execute(
        f"""
        SELECT {_INVOCATION_COLUMNS}
        FROM invocations inv
        {_where(clauses)}
        ORDER BY inv.updated_at DESC, inv.id DESC
        LIMIT ?
        """,  # noqa: S608
        [*params, limit],
    )
    return [dict(row) for row in await cursor.fetchall()]


async def _count_invocations(
    db: aiosqlite.Connection, *, clauses: list[str], params: list[Any]
) -> int:
    cursor = await db.execute(
        f"SELECT COUNT(*) AS n FROM invocations inv {_where(clauses)}",  # noqa: S608
        params,
    )
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0


async def _active_invocation_children(
    db: aiosqlite.Connection, invocation_ids: list[str]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, float | None], set[str]]:
    if not invocation_ids:
        return {}, {}, set()
    placeholders = ",".join("?" for _ in invocation_ids)
    cursor = await db.execute(
        f"""
        SELECT invocation_id,
               COUNT(*) AS child_count,
               MAX(COALESCE(last_message_at, updated_at, started_at)) AS last_activity_at
        FROM sessions
        WHERE invocation_id IN ({placeholders})
        GROUP BY invocation_id
        """,  # noqa: S608
        invocation_ids,
    )
    aggregates = await cursor.fetchall()
    last_activity = {row["invocation_id"]: row["last_activity_at"] for row in aggregates}
    with_children = {row["invocation_id"] for row in aggregates if row["child_count"] > 0}

    cursor = await db.execute(
        f"""
        SELECT * FROM sessions
        WHERE status = 'running' AND invocation_id IN ({placeholders})
        ORDER BY invocation_id, created_at, id
        """,  # noqa: S608
        invocation_ids,
    )
    by_invocation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in await cursor.fetchall():
        by_invocation[row["invocation_id"]].append(dict(row))
    return dict(by_invocation), last_activity, with_children


def _prepare_session(row: dict[str, Any]) -> dict[str, Any]:
    row["name"] = resolve_display_name(row)
    row["source_kind"] = row.get("source_kind") or "live"
    contract = _parse_json_col(row.get("artifact_contract_json"))
    row["artifact_contract_json"] = contract
    row["artifact_verification_json"] = resolve_artifact_verification(
        _parse_json_col(row.get("artifact_verification_json")),
        status=row.get("status") or "completed",
        contract=contract,
        artifacts_path=None,
    )
    return row


def _serialize_invocation(
    row: dict[str, Any], *, health: str | None, last_activity_at: float | None
) -> dict[str, Any]:
    node_metadata = _parse_json_col(row.get("node_metadata"))
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
        "node_metadata": node_metadata if isinstance(node_metadata, dict) else None,
        "project": row.get("project"),
        "project_source": row.get("project_source"),
        "health": health,
        "last_activity_at": last_activity_at,
    }


async def read_active_snapshot(
    *,
    run_limit: int,
    invocation_limit: int,
    recent_limit: int,
    project: str | None = None,
    project_null: bool = False,
    search: str | None = None,
) -> dict[str, Any]:
    require_file_store()
    observed_at = time.time()
    empty = {
        "snapshot_version": f"{int(observed_at * 1_000_000)}:0:0",
        "snapshot_at": observed_at,
        "active_runs": [],
        "active_run_total": 0,
        "active_run_omitted": 0,
        "active_invocations": [],
        "active_invocation_total": 0,
        "active_invocation_omitted": 0,
        "recent_runs": [],
        "recent_run_has_more": False,
        "recent_invocations": [],
        "recent_invocation_has_more": False,
        "complete": True,
    }
    if not store_exists():
        return empty

    scope_clauses, scope_params = _session_scope(
        alias="s", project=project, project_null=project_null, search=search
    )
    active_run_clauses = ["s.status = 'running'", *scope_clauses]
    recent_run_clauses = ["(s.status IS NULL OR s.status <> 'running')", *scope_clauses]
    active_inv_scope, active_inv_scope_params = _invocation_scope(
        project=project,
        project_null=project_null,
        search=search,
        active_children_only=True,
    )
    recent_inv_scope, recent_inv_scope_params = _invocation_scope(
        project=project,
        project_null=project_null,
        search=search,
        active_children_only=False,
    )
    active_inv_clauses = ["inv.status = 'running'"]
    recent_inv_clauses = ["inv.status <> 'running'"]
    if active_inv_scope:
        active_inv_clauses.append(active_inv_scope)
    if recent_inv_scope:
        recent_inv_clauses.append(recent_inv_scope)

    async with _open_db(store_path()) as db:
        await db.execute("BEGIN")
        try:
            active_run_total = await _count_sessions(
                db, clauses=active_run_clauses, params=scope_params
            )
            active_runs_raw = await _session_rows(
                db,
                clauses=active_run_clauses,
                params=scope_params,
                limit=run_limit,
            )
            recent_runs_raw = await _session_rows(
                db,
                clauses=recent_run_clauses,
                params=scope_params,
                limit=recent_limit + 1,
            )
            active_invocation_total = await _count_invocations(
                db, clauses=active_inv_clauses, params=active_inv_scope_params
            )
            active_invocations_raw = await _invocation_rows(
                db,
                clauses=active_inv_clauses,
                params=active_inv_scope_params,
                limit=invocation_limit,
            )
            recent_invocations_raw = await _invocation_rows(
                db,
                clauses=recent_inv_clauses,
                params=recent_inv_scope_params,
                limit=recent_limit + 1,
            )
            (
                child_rows,
                child_activity,
                invocations_with_children,
            ) = await _active_invocation_children(db, [row["id"] for row in active_invocations_raw])
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    recent_run_has_more = len(recent_runs_raw) > recent_limit
    recent_invocation_has_more = len(recent_invocations_raw) > recent_limit
    active_session_rows = [_prepare_session(row) for row in active_runs_raw]
    recent_session_rows = [_prepare_session(row) for row in recent_runs_raw[:recent_limit]]
    all_running_children = [child for children in child_rows.values() for child in children]
    process_snapshot: str | None = None
    if active_session_rows or all_running_children:
        from .admin import _ps_snapshot

        process_snapshot = _ps_snapshot()

    active_runs = [
        _runs_svc._run_row(
            row,
            observed_at,
            process_alive=_runs_svc._session_liveness(row, process_snapshot),
        )
        for row in active_session_rows
    ]
    recent_runs = [
        _runs_svc._run_row(row, observed_at, process_alive=None) for row in recent_session_rows
    ]

    active_invocations: list[dict[str, Any]] = []
    for row in active_invocations_raw:
        invocation_id = row["id"]
        running_children = child_rows.get(invocation_id, [])
        if running_children:
            health, _ = _invocations_svc._invocation_health(
                running_children, now=observed_at, ps_snapshot=process_snapshot
            )
        elif invocation_id in invocations_with_children:
            health = SessionHealth.HEALTHY.value
        else:
            health = "unknown"
        active_invocations.append(
            _serialize_invocation(
                row,
                health=health,
                last_activity_at=child_activity.get(invocation_id),
            )
        )
    recent_invocations = [
        _serialize_invocation(row, health=None, last_activity_at=None)
        for row in recent_invocations_raw[:recent_limit]
    ]

    active_run_omitted = max(0, active_run_total - len(active_runs))
    active_invocation_omitted = max(0, active_invocation_total - len(active_invocations))
    return {
        "snapshot_version": (
            f"{int(observed_at * 1_000_000)}:{active_run_total}:{active_invocation_total}"
        ),
        "snapshot_at": observed_at,
        "active_runs": active_runs,
        "active_run_total": active_run_total,
        "active_run_omitted": active_run_omitted,
        "active_invocations": active_invocations,
        "active_invocation_total": active_invocation_total,
        "active_invocation_omitted": active_invocation_omitted,
        "recent_runs": recent_runs,
        "recent_run_has_more": recent_run_has_more,
        "recent_invocations": recent_invocations,
        "recent_invocation_has_more": recent_invocation_has_more,
        "complete": (
            active_run_omitted == 0
            and active_invocation_omitted == 0
            and not recent_run_has_more
            and not recent_invocation_has_more
        ),
    }


@studio_route("/active-snapshot", method="GET", area="runs", name="get_active_snapshot")
async def get_active_snapshot_route(
    run_limit: int = Query(default=200, ge=1, le=MAX_ACTIVE_SNAPSHOT_ROWS),
    invocation_limit: int = Query(default=100, ge=1, le=MAX_ACTIVE_SNAPSHOT_ROWS),
    recent_limit: int = Query(default=200, ge=1, le=MAX_ACTIVE_SNAPSHOT_ROWS),
    project: str | None = Query(default=None),
    project_null: bool = Query(default=False),
    search: str | None = Query(default=None, min_length=1, max_length=200),
) -> dict[str, Any]:
    return await read_active_snapshot(
        run_limit=run_limit,
        invocation_limit=invocation_limit,
        recent_limit=recent_limit,
        project=project,
        project_null=project_null,
        search=search,
    )
