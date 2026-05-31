"""Tests for #1012 paginated, filtered runs list."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402


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
            await db.create_session(
                {
                    "id": s.get("id", str(uuid.uuid4())),
                    "progression_id": pid,
                    "name": s.get("name"),
                    "status": s.get("status", "completed"),
                    "playbook_name": s.get("playbook_name"),
                    "started_at": s.get("started_at", time.time()),
                }
            )


def _make_client(tmp_path, monkeypatch, db_path: Path) -> TestClient:
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.sessions as sessions_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))

    from lionagi.studio.app import app

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


# ─── ADR-0024/FIX-1: UNRESPONSIVE maps to 'stale' in runs list ───────────────


async def _seed_running_session_with_activity(
    db_path: Path, session_id: str, last_message_at: float, invocation_kind: str = "agent"
) -> None:
    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": pid,
                "name": "test-stale",
                "status": "running",
                "invocation_kind": invocation_kind,
                "started_at": last_message_at,
                "last_message_at": last_message_at,
            }
        )


def test_runs_list_threshold_crossing_session_reports_stale_not_unresponsive(tmp_path, monkeypatch):
    """Running session past its kind-aware threshold → effective_health='stale'.

    The full ADR-0024 classifier returns UNRESPONSIVE (process alive + past
    threshold), but the dashboard frontend counts effective_health==='stale'.
    The runs list MUST map UNRESPONSIVE → 'stale' so the dashboard counter
    stays correct.
    """
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    # last_message_at = 7h ago; agent threshold = 6h → UNRESPONSIVE without fix
    old_activity = time.time() - 7 * 3600
    _run(_seed_running_session_with_activity(db_path, sid, last_message_at=old_activity))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs")
    assert r.status_code == 200
    runs = r.json()["runs"]
    target = next((run for run in runs if run["id"] == sid), None)
    assert target is not None, "seeded session not found in runs list"
    assert target["effective_health"] == "stale", (
        f"expected 'stale', got {target['effective_health']!r}; "
        "UNRESPONSIVE must be mapped to 'stale' for dashboard compatibility"
    )
