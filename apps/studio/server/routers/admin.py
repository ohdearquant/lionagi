from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from lionagi.state.reasons import RunReasons, validate_reason_code

from ..services import admin as admin_svc

router = APIRouter(prefix="/admin", tags=["admin"])

_log = logging.getLogger(__name__)

# Fallback mapping for deprecated 'reason' field without reason_code.
_LEGACY_ADMIN_REASON_CODES: dict[str, str] = {
    "failed": RunReasons.FAILED_EXCEPTION,
    "aborted": RunReasons.ABORTED_USER,
    "cancelled": RunReasons.CANCELLED_SYSTEM,
}


class PruneBody(BaseModel):
    session_ids: list[str] | None = None
    all_phantom: bool = False


class TransitionBody(BaseModel):
    """ADR-0024/ADR-0028 admin session transition.

    ``reason_code`` is the preferred field (ADR-0028). The deprecated
    ``reason`` free-text field is kept for backwards compatibility: old
    clients that omit ``reason_code`` and provide ``reason`` get a
    synthesised code from the target_status→code map. New clients should
    supply ``reason_code`` from the controlled vocabulary.
    """

    session_ids: list[str] = Field(..., min_length=1)
    target_status: Literal["failed", "aborted", "cancelled"]
    reason_code: str | None = None
    reason_summary: str = ""
    evidence_refs: list[dict] = Field(default_factory=list)
    # Deprecated; kept for backwards compatibility.
    reason: str | None = Field(default=None, max_length=500)
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
    """ADR-0024/ADR-0028: mark running sessions terminal with a reason code."""
    reason_code = body.reason_code
    reason_summary = body.reason_summary

    if reason_code is not None:
        try:
            reason_code = validate_reason_code(reason_code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif body.reason:
        reason_code = _LEGACY_ADMIN_REASON_CODES[body.target_status]
        reason_summary = body.reason
        _log.warning(
            "Deprecated admin transition field 'reason' used without reason_code; "
            "mapped target_status=%s to reason_code=%s",
            body.target_status,
            reason_code,
        )
    else:
        raise HTTPException(status_code=400, detail="reason_code is required")

    try:
        return await admin_svc.transition_sessions(
            body.session_ids,
            target_status=body.target_status,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=body.evidence_refs,
            actor=body.actor,
            legacy_reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/events")
async def admin_events(
    action: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    events = await admin_svc.list_admin_events(action=action, target_id=target_id, limit=limit)
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
