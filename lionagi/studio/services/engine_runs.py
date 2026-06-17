# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Studio service: read path for the engine_runs table."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Query

from lionagi.state.db import DEFAULT_DB_PATH

from ..registry import studio_route
from ._db import open_db as _open_db

_DB = str(DEFAULT_DB_PATH)


def _parse_spec_json(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return raw or {}


async def list_engine_runs(
    *,
    kind: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return engine run rows, newest-first, with optional filters."""
    if not DEFAULT_DB_PATH.exists():
        return []

    async with _open_db(_DB) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='engine_runs'"
        )
        if not await cur.fetchone():
            return []

        conditions: list[str] = []
        params: list[Any] = []
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])

        sql = (
            f"SELECT id, kind, spec_json, status, started_at, ended_at, "  # noqa: S608
            f"session_id, export_dir, error "
            f"FROM engine_runs {where} "
            f"ORDER BY started_at DESC "
            f"LIMIT ? OFFSET ?"
        )
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()

    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "spec_json": _parse_spec_json(r["spec_json"]),
            "status": r["status"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "session_id": r["session_id"],
            "export_dir": r["export_dir"],
            "error": r["error"],
        }
        for r in rows
    ]


async def get_engine_run(run_id: str) -> dict[str, Any] | None:
    """Return a single engine run row as a dict, or None if not found."""
    if not DEFAULT_DB_PATH.exists():
        return None

    async with _open_db(_DB) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='engine_runs'"
        )
        if not await cur.fetchone():
            return None

        cur = await db.execute(
            "SELECT id, kind, spec_json, status, started_at, ended_at, "
            "session_id, export_dir, error "
            "FROM engine_runs WHERE id = ?",
            (run_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None

    return {
        "id": row["id"],
        "kind": row["kind"],
        "spec_json": _parse_spec_json(row["spec_json"]),
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "session_id": row["session_id"],
        "export_dir": row["export_dir"],
        "error": row["error"],
    }


@studio_route("/engine-runs/", method="GET", area="engine-runs", name="list_engine_runs")
async def list_engine_runs_route(
    kind: str | None = Query(default=None, description="Filter by engine kind."),
    status: str | None = Query(default=None, description="Filter by status."),
    session_id: str | None = Query(default=None, description="Filter by associated session id."),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """List engine runs, newest-first.  All query params are optional filters."""
    return await list_engine_runs(
        kind=kind,
        status=status,
        session_id=session_id,
        limit=limit,
        offset=offset,
    )


@studio_route("/engine-runs/{run_id}", method="GET", area="engine-runs", name="get_engine_run")
async def get_engine_run_route(run_id: str) -> dict[str, Any]:
    """Return a single engine run row by id."""
    row = await get_engine_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Engine run '{run_id}' not found")
    return row
