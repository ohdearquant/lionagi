"""Tests for #1014 admin doctor and prune endpoints."""
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


async def _seed_running_session(db_path: Path, session_id: str, artifacts_path: str | None = None) -> None:
    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        await db.create_session({
            "id": session_id,
            "progression_id": pid,
            "name": "test-session",
            "status": "running",
            "started_at": time.time(),
        })
        if artifacts_path is not None:
            await db.db.execute(
                "UPDATE sessions SET artifacts_path = ? WHERE id = ?",
                (artifacts_path, session_id),
            )
            await db.db.commit()


def _make_client(tmp_path, monkeypatch, db_path: Path) -> TestClient:
    import apps.studio.server.services.admin as admin_mod
    import apps.studio.server.services.sessions as sessions_mod
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))

    from apps.studio.server.app import app
    return TestClient(app)


def test_admin_doctor_reports_missing_artifacts_phantom(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    missing_dir = str(tmp_path / "nonexistent_artifacts")
    _run(_seed_running_session(db_path, sid, artifacts_path=missing_dir))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/admin/doctor")
    assert r.status_code == 200
    data = r.json()
    assert "phantom_sessions" in data
    assert "db_health" in data
    assert "diagnostic_run_at" in data
    assert data["db_health"]["size_bytes"] > 0

    phantoms = data["phantom_sessions"]
    assert len(phantoms) >= 1
    reasons = {p["reason"] for p in phantoms}
    assert "missing_artifacts" in reasons


def test_admin_doctor_no_db_returns_empty_health(tmp_path, monkeypatch):
    db_path = tmp_path / "missing.db"
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/admin/doctor")
    assert r.status_code == 200
    data = r.json()
    assert data["phantom_sessions"] == []
    assert data["db_health"]["size_bytes"] == 0
    assert data["db_health"]["wal_bytes"] == 0
    assert data["db_health"]["wal_pending"] == 0


def test_admin_prune_selected_sessions(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    s1 = str(uuid.uuid4())
    s2 = str(uuid.uuid4())
    _run(_seed_running_session(db_path, s1))
    _run(_seed_running_session(db_path, s2))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.post("/api/admin/prune", json={"session_ids": [s1]})
    assert r.status_code == 200
    assert r.json()["pruned"] == 1

    # Verify s1 gone, s2 remains via doctor
    r2 = client.get("/api/admin/doctor")
    remaining_ids = {p["session_id"] for p in r2.json()["phantom_sessions"]}
    assert s1 not in remaining_ids


def test_admin_prune_rejects_empty_body(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.post("/api/admin/prune", json={})
    assert r.status_code == 422


# ─── ADR-0024: /api/admin/health + /api/admin/transition ─────────────────────


def test_admin_health_reports_status_and_health_buckets(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_seed_running_session(db_path, str(uuid.uuid4())))
    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.get("/api/admin/health")
    assert r.status_code == 200
    body = r.json()
    assert "sessions" in body
    sess = body["sessions"]
    assert "by_status" in sess
    assert "by_health" in sess
    # Seeded one running session.
    assert sess["by_status"].get("running") == 1
    # All ADR-0024 health buckets sum to total.
    assert sum(sess["by_health"].values()) == sess["total"]


def test_admin_transition_marks_running_session_failed(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": [sid],
            "target_status": "failed",
            "reason": "manual cleanup after restart",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transitioned"] == [sid]
    assert body["skipped"] == []
    assert body["event_id"]  # admin_events row written

    # Verify DB state changed.
    async def _check():
        async with StateDB(db_path) as db:
            row = await db.get_session(sid)
            assert row["status"] == "failed"
            assert row["ended_at"] is not None
            events = await db.list_admin_events(action="transition")
            assert len(events) == 1
            assert events[0]["actor"] == "admin"

    _run(_check())


def test_admin_transition_rejects_invalid_target(tmp_path, monkeypatch):
    """ADR-0025: admin operators cannot mark sessions completed or timed_out."""
    db_path = tmp_path / "state.db"
    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": ["any"],
            "target_status": "completed",  # not in admin-allowed set
            "reason": "test",
        },
    )
    assert r.status_code == 422


def test_admin_transition_skips_non_running(tmp_path, monkeypatch):
    """Already-terminal sessions are reported as skipped, not silently no-op."""
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_running_session(db_path, sid))

    async def _terminal():
        async with StateDB(db_path) as db:
            await db.update_session(sid, status="completed")

    _run(_terminal())

    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": [sid],
            "target_status": "failed",
            "reason": "test",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transitioned"] == []
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["session_id"] == sid


def test_admin_transition_requires_reason(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.post(
        "/api/admin/transition",
        json={
            "session_ids": ["x"],
            "target_status": "failed",
            "reason": "",
        },
    )
    assert r.status_code == 422
