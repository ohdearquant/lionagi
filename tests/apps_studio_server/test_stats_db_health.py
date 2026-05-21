"""Tests for #1016 expanded stats DB health endpoint."""
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


async def _seed_two_sessions(db_path: Path) -> None:
    async with StateDB(db_path) as db:
        for status in ("running", "completed"):
            pid = str(uuid.uuid4())
            await db.create_progression(pid)
            await db.create_session({
                "id": str(uuid.uuid4()),
                "progression_id": pid,
                "name": f"s-{status}",
                "status": status,
                "started_at": time.time(),
            })


def _make_client(tmp_path, monkeypatch, db_path: Path) -> TestClient:
    import apps.studio.server.services.sessions as sessions_mod
    import apps.studio.server.services.stats as stats_mod
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))
    monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(stats_mod, "_DB", str(db_path))

    from apps.studio.server.app import app
    return TestClient(app)


def test_stats_db_health_with_existing_db(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_seed_two_sessions(db_path))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert "db" in data
    db = data["db"]
    assert db["size_bytes"] > 0
    assert db["wal_bytes"] >= 0
    assert db["tables"]["sessions"] == 2
    assert db["sessions_by_status"].get("running", 0) == 1
    assert db["sessions_by_status"].get("completed", 0) == 1
    assert db["pragmas"]["busy_timeout"] == 5000
    assert isinstance(db["connections_active"], int)
    assert db["last_checkpoint_at"] is None


def test_stats_db_health_missing_db_returns_zeroes(tmp_path, monkeypatch):
    db_path = tmp_path / "missing_state.db"
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert "db" in data
    db = data["db"]
    assert db["size_bytes"] == 0
    assert db["wal_bytes"] == 0
    assert db["tables"]["sessions"] == 0
    assert db["sessions_by_status"] == {}
