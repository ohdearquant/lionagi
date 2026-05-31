from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..services import teams as teams_svc

router = APIRouter(prefix="/teams", tags=["teams"])


@router.get("/")
async def list_teams(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    all_teams = teams_svc.list_teams()
    total = len(all_teams)
    page_teams = all_teams[offset : offset + limit]
    return {
        "teams": page_teams,
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_next": offset + limit < total,
    }


@router.get("/{team_id}")
async def get_team(team_id: str) -> dict[str, Any]:
    data = teams_svc.get_team(team_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Team '{team_id}' not found")
    return data
