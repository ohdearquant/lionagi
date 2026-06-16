# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""GET /api/engine-runs/ and /api/engine-runs/{id} — engine run read path."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..services import engine_runs as engine_runs_svc

router = APIRouter(prefix="/engine-runs", tags=["engine-runs"])


@router.get("/")
async def list_engine_runs(
    kind: str | None = Query(default=None, description="Filter by engine kind."),
    status: str | None = Query(default=None, description="Filter by status."),
    session_id: str | None = Query(default=None, description="Filter by associated session id."),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """List engine runs, newest-first.  All query params are optional filters."""
    return await engine_runs_svc.list_engine_runs(
        kind=kind,
        status=status,
        session_id=session_id,
        limit=limit,
        offset=offset,
    )


@router.get("/{run_id}")
async def get_engine_run(run_id: str) -> dict[str, Any]:
    """Return a single engine run row by id."""
    row = await engine_runs_svc.get_engine_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Engine run '{run_id}' not found")
    return row
