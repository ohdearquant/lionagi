from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..services import admin as admin_svc

router = APIRouter(prefix="/admin", tags=["admin"])


class PruneBody(BaseModel):
    session_ids: list[str] | None = None
    all_phantom: bool = False


@router.get("/doctor")
async def doctor(
    stale_hours: float = Query(default=1.0, gt=0),
) -> dict[str, Any]:
    return await admin_svc.doctor(stale_hours=stale_hours)


@router.post("/prune")
async def prune(body: PruneBody) -> dict[str, int]:
    has_ids = bool(body.session_ids)
    has_all = body.all_phantom
    if not has_ids and not has_all:
        raise HTTPException(status_code=422, detail="Provide session_ids or all_phantom")
    if has_ids and has_all:
        raise HTTPException(
            status_code=422,
            detail="Provide either session_ids or all_phantom, not both",
        )
    if has_all:
        count = await admin_svc.prune_phantom_sessions()
    else:
        count = await admin_svc.prune_sessions(body.session_ids or [])
    return {"pruned": count}
