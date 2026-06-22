"""Tests for paginated, filtered runs list."""

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
            payload = {
                "id": s.get("id", str(uuid.uuid4())),
                "progression_id": pid,
                "name": s.get("name"),
                "status": s.get("status", "completed"),
                "playbook_name": s.get("playbook_name"),
                "started_at": s.get("started_at", time.time()),
                "project": s.get("project"),
            }
            # Only forward updated_at when set — create_session treats a present
            # key as authoritative and would otherwise write a NULL timestamp.
            if "updated_at" in s:
                payload["updated_at"] = s["updated_at"]
            await db.create_session(payload)


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


# ─── GET /api/runs/projects — per-project counts for the lazy runs explorer ───


def test_runs_projects_groups_counts_and_sorted(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    base = time.time()
    sessions = (
        [
            {"id": str(uuid.uuid4()), "project": "org/alpha", "updated_at": base - 100}
            for _ in range(3)
        ]
        + [
            {"id": str(uuid.uuid4()), "project": "org/beta", "updated_at": base - 10}
            for _ in range(2)
        ]
        + [{"id": str(uuid.uuid4()), "project": None, "updated_at": base - 50}]
    )
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs/projects")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 6
    counts = {g["project"]: g["count"] for g in data["projects"]}
    assert counts == {"org/alpha": 3, "org/beta": 2, None: 1}
    # Sorted by last_activity desc → beta (newest) first; never shadowed by /runs/{id}.
    order = [g["project"] for g in data["projects"]]
    assert order[0] == "org/beta"
    activities = [g["last_activity"] for g in data["projects"]]
    assert activities == sorted(activities, reverse=True)


def test_runs_list_project_null_filter(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sessions = [{"id": str(uuid.uuid4()), "project": "org/alpha"} for _ in range(2)] + [
        {"id": str(uuid.uuid4()), "project": None} for _ in range(3)
    ]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs?project_null=true")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    assert all(run["project"] is None for run in data["runs"])

    # A positive project filter returns only that project's runs.
    r2 = client.get("/api/runs?project=org/alpha")
    assert r2.json()["total"] == 2


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
