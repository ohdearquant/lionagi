# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for POST /api/admin/maintenance."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _make_client(tmp_path, monkeypatch):
    """Return (client, db_path) with all relevant service modules pointed at db_path."""
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.db_maintenance as maint_mod
    import lionagi.studio.services.sessions as sessions_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))
    monkeypatch.setattr(maint_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    return TestClient(app, raise_server_exceptions=False), db_path


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


def test_maintenance_requires_auth_when_token_missing(tmp_path, monkeypatch):
    """401 when LIONAGI_STUDIO_AUTH_TOKEN is set and the request omits Authorization."""
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "s3cret-maint")
    client, _ = _make_client(tmp_path, monkeypatch)

    resp = client.post("/api/admin/maintenance", json={"action": "checkpoint"})
    assert resp.status_code == 401


def test_maintenance_requires_auth_when_token_wrong(tmp_path, monkeypatch):
    """401 when LIONAGI_STUDIO_AUTH_TOKEN is set and the request supplies a wrong token."""
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "s3cret-maint")
    client, _ = _make_client(tmp_path, monkeypatch)

    resp = client.post(
        "/api/admin/maintenance",
        json={"action": "checkpoint"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_maintenance_passes_with_correct_token(tmp_path, monkeypatch):
    """Request succeeds (not 401) when the correct bearer token is supplied."""
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "s3cret-maint")

    import lionagi.studio.services.db_maintenance as maint

    async def _fake_checkpoint(**_):
        return {"mode": "TRUNCATE", "busy": 0, "log_pages": 0, "checkpointed": 0}

    monkeypatch.setattr(maint, "checkpoint_state_db", _fake_checkpoint)
    client, _ = _make_client(tmp_path, monkeypatch)

    resp = client.post(
        "/api/admin/maintenance",
        json={"action": "checkpoint"},
        headers={"Authorization": "Bearer s3cret-maint"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Closed schema: extra fields must be rejected and must not reach service
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "vacuum", "extra": "field"},
        {"action": "checkpoint", "keep_days": 0},
        {"action": "prune", "dry_run": True},
        {"action": "vacuum", "actor": "hacker"},
    ],
)
def test_maintenance_rejects_extra_fields_without_calling_service(tmp_path, monkeypatch, payload):
    """Extra fields must return 422 and must NOT invoke any maintenance function."""
    import lionagi.studio.services.db_maintenance as maint

    calls = []

    async def _spy_vacuum(**_):
        calls.append("vacuum")
        return {"status": "ok"}

    async def _spy_checkpoint(**_):
        calls.append("checkpoint")
        return {"mode": "TRUNCATE", "busy": 0, "log_pages": 0, "checkpointed": 0}

    async def _spy_prune(**_):
        calls.append("prune")
        return {"sessions_pruned": 0, "runs_pruned": 0}

    monkeypatch.setattr(maint, "vacuum_state_db", _spy_vacuum)
    monkeypatch.setattr(maint, "checkpoint_state_db", _spy_checkpoint)
    monkeypatch.setattr(maint, "prune_old_data", _spy_prune)

    client, _ = _make_client(tmp_path, monkeypatch)
    resp = client.post("/api/admin/maintenance", json=payload)

    assert resp.status_code == 422, (
        f"Expected 422 for payload={payload!r}, got {resp.status_code}: {resp.text}"
    )
    assert calls == [], f"Service functions were called despite 422: {calls}"


# ---------------------------------------------------------------------------
# Allowlist / injection rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_action",
    [
        # Shell injection attempt
        "vacuum; rm -rf /",
        # Flag-shaped string
        "--help",
        # Entirely unknown
        "reindex",
        # Empty string
        "",
        # SQL injection
        "vacuum' OR '1'='1",
        # Looks similar but not in the set
        "Vacuum",
        "VACUUM",
    ],
)
def test_maintenance_rejects_disallowed_action(tmp_path, monkeypatch, bad_action):
    """Any action outside the Literal vocabulary must return 422."""
    client, _ = _make_client(tmp_path, monkeypatch)

    resp = client.post("/api/admin/maintenance", json={"action": bad_action})
    assert resp.status_code == 422, (
        f"Expected 422 for action={bad_action!r}, got {resp.status_code}"
    )


def test_maintenance_missing_action_field_returns_422(tmp_path, monkeypatch):
    """Omitting the required 'action' field returns 422 (Pydantic validation)."""
    client, _ = _make_client(tmp_path, monkeypatch)
    resp = client.post("/api/admin/maintenance", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Success paths (service layer monkeypatched)
# ---------------------------------------------------------------------------


def test_maintenance_vacuum_success(tmp_path, monkeypatch):
    """action='vacuum' calls vacuum_state_db and returns action + status."""
    import lionagi.studio.services.db_maintenance as maint

    called = []

    async def _fake_vacuum(**_):
        called.append(True)
        return {"status": "ok"}

    monkeypatch.setattr(maint, "vacuum_state_db", _fake_vacuum)
    client, _ = _make_client(tmp_path, monkeypatch)

    resp = client.post("/api/admin/maintenance", json={"action": "vacuum"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "vacuum"
    assert data["status"] == "ok"
    assert called


def test_maintenance_checkpoint_success(tmp_path, monkeypatch):
    """action='checkpoint' calls checkpoint_state_db and returns action + PRAGMA counts."""
    import lionagi.studio.services.db_maintenance as maint

    async def _fake_checkpoint(**_):
        return {"mode": "TRUNCATE", "busy": 0, "log_pages": 5, "checkpointed": 5}

    monkeypatch.setattr(maint, "checkpoint_state_db", _fake_checkpoint)
    client, _ = _make_client(tmp_path, monkeypatch)

    resp = client.post("/api/admin/maintenance", json={"action": "checkpoint"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "checkpoint"
    assert data["mode"] == "TRUNCATE"
    assert "log_pages" in data
    assert "checkpointed" in data


def test_maintenance_prune_success(tmp_path, monkeypatch):
    """action='prune' calls prune_old_data and returns action + pruned counts."""
    import lionagi.studio.services.db_maintenance as maint

    async def _fake_prune(**_):
        return {"sessions_pruned": 3, "runs_pruned": 1}

    monkeypatch.setattr(maint, "prune_old_data", _fake_prune)
    client, _ = _make_client(tmp_path, monkeypatch)

    resp = client.post("/api/admin/maintenance", json={"action": "prune"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "prune"
    assert data["sessions_pruned"] == 3
    assert data["runs_pruned"] == 1


# ---------------------------------------------------------------------------
# Live path — existing initialized DB
# ---------------------------------------------------------------------------


def test_maintenance_vacuum_live_existing_db(tmp_path, monkeypatch):
    """vacuum on an existing initialized DB returns status='ok' and writes an audit event."""
    from lionagi.state.db import StateDB

    # _make_client patches everything to tmp_path/"state.db"
    client, db_path = _make_client(tmp_path, monkeypatch)

    # Initialize the schema so the DB file exists before the request.
    asyncio.run(_async_init_db(db_path))

    resp = client.post("/api/admin/maintenance", json={"action": "vacuum"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "vacuum"
    assert data["status"] == "ok"

    # Verify the audit event was written to the same db_path.
    events = asyncio.run(_async_list_events(db_path, action="vacuum"))
    assert any(e["action"] == "vacuum" for e in events), (
        f"Expected vacuum audit event in {db_path}, got: {events}"
    )


async def _async_init_db(db_path):
    from lionagi.state.db import StateDB

    async with StateDB(db_path):
        pass


async def _async_list_events(db_path, *, action):
    from lionagi.state.db import StateDB

    async with StateDB(db_path) as db:
        return await db.list_admin_events(action=action, limit=5)


# ---------------------------------------------------------------------------
# Live path — DB absent should return graceful responses
# ---------------------------------------------------------------------------


def test_maintenance_vacuum_db_absent(tmp_path, monkeypatch):
    """vacuum with no DB yet returns skipped, not 500."""
    client, _ = _make_client(tmp_path, monkeypatch)
    # db_path is tmp_path/"state.db" but it doesn't exist yet.

    resp = client.post("/api/admin/maintenance", json={"action": "vacuum"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "skipped"


def test_maintenance_checkpoint_db_absent(tmp_path, monkeypatch):
    """checkpoint with no DB yet returns None counts, not 500."""
    client, _ = _make_client(tmp_path, monkeypatch)

    resp = client.post("/api/admin/maintenance", json={"action": "checkpoint"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "checkpoint"
    assert data["busy"] is None


# ---------------------------------------------------------------------------
# Lock-contention → 409 with structured detail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["vacuum", "checkpoint", "prune"])
def test_maintenance_lock_contention_returns_409(tmp_path, monkeypatch, action):
    """All three actions return 409 when another writer holds the DB lock (busy_timeout=100ms)."""
    from lionagi.state.db import StateDB

    # _make_client patches everything to tmp_path/"state.db".
    client, db_path = _make_client(tmp_path, monkeypatch)

    # Initialize the schema so the DB file exists and is in WAL mode.
    asyncio.run(_async_init_db(db_path))

    # Patch _apply_pragmas to use a short busy_timeout so the test is fast.
    original_apply_pragmas = StateDB._apply_pragmas

    async def _fast_pragmas(self):
        await original_apply_pragmas(self)
        await self.db.execute("PRAGMA busy_timeout = 100")

    monkeypatch.setattr(StateDB, "_apply_pragmas", _fast_pragmas)

    # Hold an exclusive write lock with a raw sqlite3 connection.
    lock_conn = sqlite3.connect(str(db_path), timeout=0)
    try:
        lock_conn.execute("BEGIN IMMEDIATE")

        resp = client.post("/api/admin/maintenance", json={"action": action})
    finally:
        lock_conn.close()

    assert resp.status_code == 409, (
        f"Expected 409 for locked DB ({action}), got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "detail" in body, f"Expected 'detail' in 409 body, got: {body}"
    detail_lower = body["detail"].lower()
    assert "busy" in detail_lower or "lock" in detail_lower, (
        f"Expected 'busy' or 'lock' in detail, got: {body['detail']!r}"
    )
