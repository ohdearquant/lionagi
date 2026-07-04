"""Tests for teams read-only viewer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402


def _make_client(monkeypatch, teams_root: Path) -> TestClient:
    import lionagi.studio.services.teams as teams_mod

    monkeypatch.setattr(teams_mod, "_TEAMS_ROOT", teams_root)

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def _write_team(teams_root: Path, filename: str, data: dict) -> None:
    teams_root.mkdir(parents=True, exist_ok=True)
    (teams_root / filename).write_text(json.dumps(data))


def test_teams_list_paginates_json_files(tmp_path, monkeypatch):
    teams_root = tmp_path / "teams"
    _write_team(teams_root, "team1.json", {"id": "t1", "name": "Team One", "members": ["a", "b"]})
    _write_team(teams_root, "team2.json", {"id": "t2", "name": "Team Two", "members": []})
    client = _make_client(monkeypatch, teams_root)

    r = client.get("/api/teams?limit=1&offset=0")
    assert r.status_code == 200
    data = r.json()
    assert len(data["teams"]) == 1
    assert data["total"] == 2
    assert data["has_next"] is True
    team = data["teams"][0]
    assert "id" in team
    assert "name" in team
    assert "member_count" in team
    assert "last_modified" in team


def test_teams_list_missing_directory_returns_empty(tmp_path, monkeypatch):
    teams_root = tmp_path / "nonexistent_teams"
    client = _make_client(monkeypatch, teams_root)

    r = client.get("/api/teams")
    assert r.status_code == 200
    data = r.json()
    assert data["teams"] == []
    assert data["total"] == 0
    assert data["has_next"] is False


def test_team_detail_returns_full_json(tmp_path, monkeypatch):
    teams_root = tmp_path / "teams"
    team_data = {
        "id": "abc123",
        "name": "Test Team",
        "members": ["x"],
        "messages": [{"from": "a", "to": "b"}],
    }
    _write_team(teams_root, "abc123.json", team_data)
    client = _make_client(monkeypatch, teams_root)

    r = client.get("/api/teams/abc123")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "abc123"
    assert data["name"] == "Test Team"
    assert len(data["messages"]) == 1


def test_team_detail_missing_returns_404(tmp_path, monkeypatch):
    teams_root = tmp_path / "teams"
    teams_root.mkdir()
    client = _make_client(monkeypatch, teams_root)

    r = client.get("/api/teams/missing")
    assert r.status_code == 404


def test_team_detail_traversal_returns_404(tmp_path, monkeypatch):
    """A team_id with a path separator is rejected as 404, never 500."""
    teams_root = tmp_path / "teams"
    teams_root.mkdir()
    client = _make_client(monkeypatch, teams_root)

    r = client.get("/api/teams/aaa%2Fbbb")
    assert r.status_code == 404


def test_get_team_invalid_component_returns_none(tmp_path, monkeypatch):
    """get_team() catches the path-safety ValueError and returns None."""
    import lionagi.studio.services.teams as teams_mod

    teams_root = tmp_path / "teams"
    teams_root.mkdir()
    monkeypatch.setattr(teams_mod, "_TEAMS_ROOT", teams_root)

    assert teams_mod.get_team("../secrets") is None
