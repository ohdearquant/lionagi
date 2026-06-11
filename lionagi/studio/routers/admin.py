from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from lionagi.state.reasons import RunReasons, validate_reason_code

from ..services import admin as admin_svc

router = APIRouter(prefix="/admin", tags=["admin"])

# Closed set of DB maintenance actions exposed through the Studio UI.
_MAINTENANCE_ACTIONS = frozenset({"vacuum", "checkpoint", "prune"})

_log = logging.getLogger(__name__)

# Fallback mapping for deprecated 'reason' field without reason_code.
_LEGACY_ADMIN_REASON_CODES: dict[str, str] = {
    "failed": RunReasons.FAILED_EXCEPTION,
    "aborted": RunReasons.ABORTED_USER,
    "cancelled": RunReasons.CANCELLED_SYSTEM,
}


class MaintenanceBody(BaseModel):
    """Request body for POST /api/admin/maintenance.

    ``action`` must be one of the allowlisted values; any other token is
    rejected with 422 before any operation runs.
    """

    action: str = Field(
        ...,
        description="DB maintenance action: 'vacuum', 'checkpoint', or 'prune'.",
    )


class PruneBody(BaseModel):
    session_ids: list[str] | None = None
    all_phantom: bool = False


class PruneOldDataBody(BaseModel):
    keep_days: int | None = Field(
        default=None, ge=1, description="Retain sessions newer than this many days"
    )


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


@router.post("/prune-old-data")
async def prune_old_data(body: PruneOldDataBody) -> dict[str, int]:
    """Remove terminal sessions/runs older than keep_days (default from config)."""
    from ..services.db_maintenance import prune_old_data as _prune

    return await _prune(keep_days=body.keep_days, actor="admin")


@router.post("/maintenance")
async def run_maintenance(body: MaintenanceBody) -> dict[str, Any]:
    """Run a DB maintenance action (vacuum | checkpoint | prune).

    The ``action`` field is validated against a strict allowlist before
    any operation runs; unknown values return 422 immediately.
    """
    if body.action not in _MAINTENANCE_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown action {body.action!r}. Allowed values: {sorted(_MAINTENANCE_ACTIONS)}"
            ),
        )

    from ..services.db_maintenance import (
        checkpoint_state_db,
        prune_old_data,
        vacuum_state_db,
    )

    if body.action == "vacuum":
        result = await vacuum_state_db(actor="admin")
        return {"action": "vacuum", **result}

    if body.action == "checkpoint":
        result = await checkpoint_state_db(actor="admin")
        return {"action": "checkpoint", **result}

    # action == "prune"
    result = await prune_old_data(actor="admin")
    return {"action": "prune", **result}


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
