"""Tests for #1016 expanded stats DB health endpoint."""

from __future__ import annotations

import asyncio
import json
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


async def _seed_two_sessions(db_path: Path) -> None:
    async with StateDB(db_path) as db:
        for status in ("running", "completed"):
            pid = str(uuid.uuid4())
            await db.create_progression(pid)
            await db.create_session(
                {
                    "id": str(uuid.uuid4()),
                    "progression_id": pid,
                    "name": f"s-{status}",
                    "status": status,
                    "started_at": time.time(),
                }
            )


def _make_client(tmp_path, monkeypatch, db_path: Path) -> TestClient:
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.sessions as sessions_mod
    import lionagi.studio.services.stats as stats_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))
    monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(stats_mod, "_DB", str(db_path))

    from lionagi.studio.app import app

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


# ---------------------------------------------------------------------------
# Regression: Finding 4 — stats DB path delegation
# When stats.DEFAULT_DB_PATH is patched to a small temp DB, size_bytes must
# reflect THAT DB, not admin.DEFAULT_DB_PATH (which was the pre-fix bug).
# ---------------------------------------------------------------------------


def test_stats_size_comes_from_stats_db_path(tmp_path, monkeypatch):
    """size_bytes must come from stats.DEFAULT_DB_PATH, not admin.DEFAULT_DB_PATH."""
    small_db = tmp_path / "small_state.db"
    _run(_seed_two_sessions(small_db))

    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.sessions as sessions_mod
    import lionagi.studio.services.stats as stats_mod

    # Patch stats to the small DB; leave admin pointing at a nonexistent path
    admin_db = tmp_path / "admin_state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", small_db)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", small_db)
    monkeypatch.setattr(sessions_mod, "_DB", str(small_db))
    monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", small_db)
    monkeypatch.setattr(stats_mod, "_DB", str(small_db))
    monkeypatch.setattr(admin_mod, "DEFAULT_DB_PATH", admin_db)

    from lionagi.studio.app import app

    client = TestClient(app)
    r = client.get("/api/stats")
    assert r.status_code == 200
    db = r.json()["db"]
    # The stats endpoint must report the size of small_db, which exists and has content
    assert db["size_bytes"] > 0, "size_bytes must reflect the patched stats DB, not admin's DB"
    assert db["tables"]["sessions"] == 2


# ---------------------------------------------------------------------------
# Regression: Finding 3 — invocation node_metadata parse failure -> None
# _parse_json_col returns the raw string on JSONDecodeError; invocations must
# convert that to None (matching origin/main behavior).
# ---------------------------------------------------------------------------


async def _seed_invocation_with_bad_metadata(db_path: Path, inv_id: str) -> None:
    async with StateDB(db_path) as db:
        await db.create_invocation(
            {
                "id": inv_id,
                "skill": "test-skill",
                "status": "running",
                "started_at": time.time(),
                "node_metadata": "{bad-json-that-cannot-be-parsed",
            }
        )


def test_invocation_bad_metadata_becomes_none(tmp_path, monkeypatch):
    """Corrupted node_metadata must be None, not the raw invalid string."""
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.invocations as inv_mod

    db_path = tmp_path / "state.db"
    inv_id = str(uuid.uuid4())
    _run(_seed_invocation_with_bad_metadata(db_path, inv_id))

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(inv_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    client = TestClient(app)
    r = client.get(f"/api/invocations/{inv_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["node_metadata"] is None, (
        "parse failure on node_metadata must yield None, not the raw string"
    )


def test_invocation_list_bad_metadata_becomes_none(tmp_path, monkeypatch):
    """Corrupted node_metadata in list endpoint must also be None."""
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.invocations as inv_mod

    db_path = tmp_path / "state.db"
    inv_id = str(uuid.uuid4())
    _run(_seed_invocation_with_bad_metadata(db_path, inv_id))

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(inv_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    client = TestClient(app)
    r = client.get("/api/invocations")
    assert r.status_code == 200
    invocations = r.json()
    matching = [i for i in invocations if i["id"] == inv_id]
    assert len(matching) == 1
    assert matching[0]["node_metadata"] is None, (
        "parse failure on node_metadata must yield None in list endpoint"
    )
