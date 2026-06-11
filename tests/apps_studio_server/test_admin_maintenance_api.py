# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for POST /api/admin/maintenance (Phase C Move 3).

Coverage:
  - auth required (401 when LIONAGI_STUDIO_AUTH_TOKEN is set and absent)
  - allowlist enforcement: unknown / shell-injection / flag-shaped actions → 422
  - success path for each of vacuum, checkpoint, prune (service layer monkeypatched)
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixture: a TestClient wired to a tmp DB path
# ---------------------------------------------------------------------------


def _make_client(tmp_path, monkeypatch):
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.sessions as sessions_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))

    from lionagi.studio.app import app

    return TestClient(app, raise_server_exceptions=False), db_path


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


def test_maintenance_requires_auth_when_token_set(tmp_path, monkeypatch):
    """401 when LIONAGI_STUDIO_AUTH_TOKEN is set and the request omits it."""
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "s3cret-maint")
    client, _ = _make_client(tmp_path, monkeypatch)

    resp = client.post("/api/admin/maintenance", json={"action": "checkpoint"})
    assert resp.status_code == 401


def test_maintenance_passes_with_correct_token(tmp_path, monkeypatch):
    """Request succeeds (not 401) when the correct bearer token is supplied."""
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "s3cret-maint")

    import lionagi.studio.services.db_maintenance as maint

    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", tmp_path / "state.db")

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
    """Any action outside the closed allowlist must return 422."""
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

    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", tmp_path / "state.db")

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

    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", tmp_path / "state.db")

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

    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", tmp_path / "state.db")

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
# Live path (no mock) — DB absent should still return without crashing
# ---------------------------------------------------------------------------


def test_maintenance_vacuum_db_absent(tmp_path, monkeypatch):
    """vacuum with no DB yet returns skipped, not 500."""
    import lionagi.studio.services.db_maintenance as maint

    nonexistent = tmp_path / "noexist.db"
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", nonexistent)

    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", nonexistent)
    client, _ = _make_client(tmp_path, monkeypatch)

    resp = client.post("/api/admin/maintenance", json={"action": "vacuum"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "skipped"


def test_maintenance_checkpoint_db_absent(tmp_path, monkeypatch):
    """checkpoint with no DB yet returns None counts, not 500."""
    import lionagi.studio.services.db_maintenance as maint

    nonexistent = tmp_path / "noexist.db"
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", nonexistent)

    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", nonexistent)
    client, _ = _make_client(tmp_path, monkeypatch)

    resp = client.post("/api/admin/maintenance", json={"action": "checkpoint"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "checkpoint"
    assert data["busy"] is None
