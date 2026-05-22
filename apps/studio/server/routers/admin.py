from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..services import admin as admin_svc

router = APIRouter(prefix="/admin", tags=["admin"])


class PruneBody(BaseModel):
    session_ids: list[str] | None = None
    all_phantom: bool = False


class TransitionBody(BaseModel):
    """ADR-0024 §B: admin session transition.

    ``reason`` is required so every transition has an audit-log
    justification. ``target_status`` is constrained to the
    admin-allowed subset (operators can't claim a model
    timed out or completed cleanly).
    """

    session_ids: list[str] = Field(..., min_length=1)
    target_status: Literal["failed", "aborted", "cancelled"]
    reason: str = Field(..., min_length=1, max_length=500)
    actor: str = Field(default="admin", max_length=64)


@router.get("/doctor")
async def doctor(
    stale_hours: float = Query(default=1.0, gt=0),
) -> dict[str, Any]:
    return await admin_svc.doctor(stale_hours=stale_hours)


@router.get("/health")
async def health() -> dict[str, Any]:
    """ADR-0024 §B: composite session health report."""
    return await admin_svc.health_report()


@router.post("/transition")
async def transition(body: TransitionBody) -> dict[str, Any]:
    """ADR-0024 §B: mark running sessions terminal with an audit entry."""
    try:
        return await admin_svc.transition_sessions(
            body.session_ids,
            target_status=body.target_status,
            reason=body.reason,
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/events")
async def admin_events(
    action: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    events = await admin_svc.list_admin_events(
        action=action, target_id=target_id, limit=limit
    )
    return {"events": events}


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
