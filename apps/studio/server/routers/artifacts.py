# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0021 artifacts API.

Skill-produced structured outcomes (ReviewVerdict, GateVerdict,
CIResult, ...). Most consumers fetch via /api/invocations/{id} which
returns the artifact list inline; these endpoints exist for the cases
where the caller has only a session id, or wants the artifact alone.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..services import invocations as inv_svc

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str) -> dict[str, Any]:
    data = await inv_svc.get_artifact(artifact_id)
    if data is None:
        raise HTTPException(
            status_code=404, detail=f"Artifact '{artifact_id}' not found"
        )
    return data


# Convenience: sessions don't have their own router-level artifacts endpoint
# (sessions.router predates ADR-0021). This sub-route under /api/artifacts
# keeps the artifact concern in one place.
@router.get("/by-session/{session_id}")
async def list_for_session(session_id: str) -> dict[str, Any]:
    rows = await inv_svc.list_artifacts_for_session(session_id)
    return {"artifacts": rows}
