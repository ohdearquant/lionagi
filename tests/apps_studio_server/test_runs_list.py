"""Tests for #1012 paginated, filtered runs list."""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient

from lionagi.state.db import StateDB


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_sessions(db_path: Path, sessions: list[dict]) -> None:
    async with StateDB(db_path) as db:
        for s in sessions:
            pid = str(uuid.uuid4())
            await db.create_progression(pid)
            await db.create_session({
                "id": s.get("id", str(uuid.uuid4())),
                "progression_id": pid,
                "name": s.get("name"),
                "status": s.get("status", "completed"),
                "playbook_name": s.get("playbook_name"),
                "started_at": s.get("started_at", time.time()),
            })


def _make_client(tmp_path, monkeypatch, db_path: Path) -> TestClient:
    import apps.studio.server.services.sessions as sessions_mod
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))

    from apps.studio.server.app import app
    return TestClient(app)


def test_runs_list_paginates_with_default_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sessions = [{"id": str(uuid.uuid4()), "status": "completed"} for _ in range(25)]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs")
    assert r.status_code == 200
    data = r.json()
    assert len(data["runs"]) == 20
    assert data["page"] == 1
    assert data["per_page"] == 20
    assert data["total"] == 25
    assert data["total_pages"] == 2
    assert data["has_next"] is True
    assert data["has_prev"] is False


def test_runs_list_second_page(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sessions = [{"id": str(uuid.uuid4()), "status": "completed"} for _ in range(25)]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs?page=2&per_page=20")
    assert r.status_code == 200
    data = r.json()
    assert len(data["runs"]) == 5
    assert data["has_next"] is False
    assert data["has_prev"] is True


def test_runs_list_filters_multi_status_and_playbook_contains(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sessions_data = [
        {"id": str(uuid.uuid4()), "status": "running", "playbook_name": "alpha"},
        {"id": str(uuid.uuid4()), "status": "failed", "playbook_name": "beta"},
        {"id": str(uuid.uuid4()), "status": "completed", "playbook_name": "alpha-long"},
    ]
    _run(_seed_sessions(db_path, sessions_data))
    client = _make_client(tmp_path, monkeypatch, db_path)

    # status=running&status=done means running OR done/completed
    r = client.get("/api/runs?status=running&status=done&playbook=alpha")
    assert r.status_code == 200
    data = r.json()
    runs = data["runs"]
    # Should get running/alpha and completed/alpha-long but not failed/beta
    statuses = {run["status"] for run in runs}
    assert "failed" not in statuses
    playbooks = {run["playbook_name"] for run in runs}
    for pb in playbooks:
        assert pb is None or "alpha" in pb.lower()


def test_runs_list_invalid_page_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.get("/api/runs?page=0")
    assert r.status_code == 422
