"""Tests for the expanded stats DB health endpoint."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

import lionagi.state.db as state_db_mod
import lionagi.state.engine as state_engine_mod
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

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


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
    # The pragma must reflect this process's configured busy_timeout, not a
    # hardcoded default -- LIONAGI_SQLITE_BUSY_TIMEOUT_MS lets a deployment
    # raise it, and this process (and the test suite) may already be running
    # under such a deployment's environment.
    assert db["pragmas"]["busy_timeout"] == state_engine_mod.SQLITE_BUSY_TIMEOUT_MS
    assert isinstance(db["connections_active"], int)
    assert db["last_checkpoint_at"] is None


def test_stats_db_health_busy_timeout_reflects_deployment_knob_not_default(tmp_path, monkeypatch):
    """A deployment that raises the busy_timeout above the 5000ms default
    must see its own value reported back, not the suite's default."""
    monkeypatch.setattr(state_engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 30000)
    db_path = tmp_path / "state.db"
    _run(_seed_two_sessions(db_path))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["db"]["pragmas"]["busy_timeout"] == 30000
    assert data["db"]["pragmas"]["busy_timeout"] != 5000


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
# stats reports the store the daemon serves, not a path of its own
# ---------------------------------------------------------------------------


def test_stats_size_comes_from_the_configured_store(tmp_path, monkeypatch):
    """The reported size belongs to the store in play, whichever file that is.

    Each service used to hold its own copy of the default path, so this asked
    whether stats read stats' copy rather than admin's. There is one
    resolution now, and the question that replaces it is whether that
    resolution follows the configuration: the default path exists here and is
    the wrong answer, so a service still reading it reports a size of zero.
    """
    configured = tmp_path / "configured_state.db"
    _run(_seed_two_sessions(configured))

    default_path = tmp_path / "default_state.db"
    default_path.write_bytes(b"")

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", default_path)
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": str(configured)}),
    )

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    r = client.get("/api/stats")
    assert r.status_code == 200
    db = r.json()["db"]
    assert db["size_bytes"] == configured.stat().st_size
    assert db["tables"]["sessions"] == 2


# ---------------------------------------------------------------------------
# invocation node_metadata parse failure must return None, not the raw string
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

    db_path = tmp_path / "state.db"
    inv_id = str(uuid.uuid4())
    _run(_seed_invocation_with_bad_metadata(db_path, inv_id))

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    r = client.get(f"/api/invocations/{inv_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["node_metadata"] is None, (
        "parse failure on node_metadata must yield None, not the raw string"
    )


def test_invocation_list_bad_metadata_becomes_none(tmp_path, monkeypatch):
    """Corrupted node_metadata in list endpoint must also be None."""

    db_path = tmp_path / "state.db"
    inv_id = str(uuid.uuid4())
    _run(_seed_invocation_with_bad_metadata(db_path, inv_id))

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    r = client.get("/api/invocations")
    assert r.status_code == 200
    invocations = r.json()["invocations"]
    matching = [i for i in invocations if i["id"] == inv_id]
    assert len(matching) == 1
    assert matching[0]["node_metadata"] is None, (
        "parse failure on node_metadata must yield None in list endpoint"
    )


# ---------------------------------------------------------------------------
# the size alert is one decision, and both health surfaces report it
# ---------------------------------------------------------------------------


def test_doctor_and_stats_agree_on_the_size_alert(tmp_path, monkeypatch):
    """Both health surfaces must read the same threshold decision.

    /api/stats already decided this. /api/admin/doctor reported the raw size
    and nothing else, so the only way for a reader of the doctor payload to
    know whether the store was over its limit was to re-derive the limit --
    which is how a health view ends up unable to say "unhealthy" at all.
    Asserting agreement rather than a fixed number is what keeps a second
    copy of the threshold from being introduced later.
    """
    import lionagi.studio.config as studio_config

    db_path = tmp_path / "state.db"
    _run(_seed_two_sessions(db_path))
    monkeypatch.setattr(studio_config, "DB_SIZE_ALERT_BYTES", 1)
    client = _make_client(tmp_path, monkeypatch, db_path)

    stats_db = client.get("/api/stats").json()["db"]
    doctor_db = client.get("/api/admin/doctor").json()["db_health"]

    assert stats_db["size_alert"] is True
    assert doctor_db["size_alert"] is True, (
        "doctor must report the alert /api/stats already raised for the same store"
    )
    assert doctor_db["size_threshold_bytes"] == stats_db["size_threshold_bytes"]
    assert doctor_db["size_bytes"] == stats_db["size_bytes"]


def test_doctor_size_alert_is_false_below_the_threshold(tmp_path, monkeypatch):
    """The other arm: a store under its limit must not be flagged.

    Without this, an implementation that hardcodes the alert to True passes
    the agreement test above -- and a hardcoded health signal is the exact
    defect this pair exists to prevent.
    """
    import lionagi.studio.config as studio_config

    db_path = tmp_path / "state.db"
    _run(_seed_two_sessions(db_path))
    monkeypatch.setattr(studio_config, "DB_SIZE_ALERT_BYTES", 10**12)
    client = _make_client(tmp_path, monkeypatch, db_path)

    stats_db = client.get("/api/stats").json()["db"]
    doctor_db = client.get("/api/admin/doctor").json()["db_health"]

    assert doctor_db["size_bytes"] > 0, "the store must be non-empty for this to mean anything"
    assert stats_db["size_alert"] is False
    assert doctor_db["size_alert"] is False
    assert doctor_db["size_threshold_bytes"] == 10**12
