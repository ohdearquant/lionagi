from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lionagi.utils import LIONAGI_HOME

from ._path_safety import safe_path_join

_TEAMS_ROOT = LIONAGI_HOME / "teams"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None


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
        result.append({
            "id": str(data.get("id") or path.stem),
            "name": str(data.get("name") or path.stem),
            "member_count": member_count,
            "last_modified": last_modified,
        })
    return result


def get_team(team_id: str) -> dict[str, Any] | None:
    if ".json" in team_id:
        team_id = team_id.replace(".json", "")
    try:
        safe_path_join(_TEAMS_ROOT, f"{team_id}.json")
    except Exception:
        return None
    path = _TEAMS_ROOT / f"{team_id}.json"
    return _read_json(path)
