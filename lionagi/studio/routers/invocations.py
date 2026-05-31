# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0020 invocations API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..services import invocations as inv_svc

router = APIRouter(prefix="/invocations", tags=["invocations"])


@router.get("/")
async def list_invocations(
    skill: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows = await inv_svc.list_invocations(skill=skill, status=status, limit=limit, offset=offset)
    return {
        "invocations": rows,
        "limit": limit,
        "offset": offset,
        # We don't compute total separately — the page is the slice the
        # client asked for. Studio's pagination uses has_next instead of
        # absolute totals.
        "has_next": len(rows) == limit,
    }


@router.get("/{invocation_id}")
async def get_invocation(invocation_id: str) -> dict[str, Any]:
    data = await inv_svc.get_invocation(invocation_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Invocation '{invocation_id}' not found")
    return data
