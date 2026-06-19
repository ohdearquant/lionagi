# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Studio service: read path for the engine_runs table."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Query

from lionagi._errors import NotFoundError
from lionagi.state.db import DEFAULT_DB_PATH, StateDB

from ..registry import studio_route

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

    async with StateDB(_DB) as db:
        rows = await db.list_engine_runs(
            kind=kind,
            status=status,
            session_id=session_id,
            limit=limit,
            offset=offset,
        )

    for row in rows:
        row["spec_json"] = _parse_spec_json(row.get("spec_json"))
    return rows


async def get_engine_run(run_id: str) -> dict[str, Any] | None:
    """Return a single engine run row as a dict, or None if not found."""
    if not DEFAULT_DB_PATH.exists():
        return None

    async with StateDB(_DB) as db:
        row = await db.get_engine_run(run_id)

    if row is None:
        return None
    row["spec_json"] = _parse_spec_json(row.get("spec_json"))
    return row


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
        raise NotFoundError(f"Engine run '{run_id}' not found")
    return row
