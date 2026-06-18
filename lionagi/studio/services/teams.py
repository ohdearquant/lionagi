from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query

from lionagi.libs.path_safety import safe_join
from lionagi.utils import LIONAGI_HOME

from ..registry import studio_route
from ._io import read_json_file as _read_json

_TEAMS_ROOT = LIONAGI_HOME / "teams"


def list_teams() -> list[dict[str, Any]]:
    if not _TEAMS_ROOT.exists():
        return []
    result = []
    try:
        paths = sorted(_TEAMS_ROOT.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for path in paths:
        data = _read_json(path)
        if data is None:
            continue
        members = data.get("members")
        member_count = len(members) if isinstance(members, list) else 0
        try:
            last_modified = path.stat().st_mtime
        except OSError:
            last_modified = 0.0
        result.append(
            {
                "id": str(data.get("id") or path.stem),
                "name": str(data.get("name") or path.stem),
                "member_count": member_count,
                "last_modified": last_modified,
            }
        )
    return result


def get_team(team_id: str) -> dict[str, Any] | None:
    if ".json" in team_id:
        team_id = team_id.replace(".json", "")
    try:
        safe_join(_TEAMS_ROOT, f"{team_id}.json")
    except ValueError:
        return None
    path = _TEAMS_ROOT / f"{team_id}.json"
    return _read_json(path)


@studio_route("/teams/", method="GET", area="teams", name="list_teams")
async def list_teams_route(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    all_teams = list_teams()
    total = len(all_teams)
    page_teams = all_teams[offset : offset + limit]
    return {
        "teams": page_teams,
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_next": offset + limit < total,
    }


@studio_route("/teams/{team_id}", method="GET", area="teams", name="get_team")
async def get_team_route(team_id: str) -> dict[str, Any]:
    data = get_team(team_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Team '{team_id}' not found")
    return data
