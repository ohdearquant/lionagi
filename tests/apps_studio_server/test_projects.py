# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ADR-0026 Studio /api/projects endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Wire a TestClient with a real temp state.db and patched paths."""
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.projects as projects_mod

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(projects_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(projects_mod, "_DB", str(fake_db))

    # Ensure schema is applied to the temp DB.
    import asyncio

    from lionagi.state.db import StateDB

    async def _init():
        async with StateDB(fake_db) as db:
            pass  # opens + applies schema

    asyncio.run(_init())

    from lionagi.studio.app import app

    return TestClient(app)


# ── GET /api/projects/ ────────────────────────────────────────────────────────


def test_list_projects_empty(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.get("/api/projects/")
    assert r.status_code == 200
    data = r.json()
    assert "projects" in data
    assert "unassigned_count" in data
    assert data["projects"] == []
    assert data["unassigned_count"] == 0


def test_list_projects_after_create(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.post("/api/projects/", json={"name": "my-project"})
    assert r.status_code == 201

    r2 = client.get("/api/projects/")
    assert r2.status_code == 200
    names = [p["name"] for p in r2.json()["projects"]]
    assert "my-project" in names


# ── GET /api/projects/{name} ──────────────────────────────────────────────────


def test_get_project_not_found(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.get("/api/projects/ghost")
    assert r.status_code == 404


def test_get_project_found(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post(
        "/api/projects/", json={"name": "found-project", "github": "https://github.com/org/repo"}
    )
    r = client.get("/api/projects/found-project")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "found-project"
    assert data["github"] == "https://github.com/org/repo"
    assert data["source"] == "studio"
    assert "editable" in data
    assert data["editable"] is True
    assert "agents_used" in data
    assert "playbooks_used" in data


# ── POST /api/projects/ ───────────────────────────────────────────────────────


def test_create_project_minimal(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.post("/api/projects/", json={"name": "minimal"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "minimal"
    assert data["source"] == "studio"


def test_create_project_all_fields(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.post(
        "/api/projects/",
        json={
            "name": "full-project",
            "github": "https://github.com/org/full",
            "description": "Full test project",
            "path": "/tmp/full",
        },
    )
    assert r.status_code == 201


def test_create_project_duplicate_returns_409(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/api/projects/", json={"name": "dup"})
    r2 = client.post("/api/projects/", json={"name": "dup"})
    assert r2.status_code == 409


def test_create_project_empty_name_returns_400(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.post("/api/projects/", json={"name": "   "})
    assert r.status_code == 400


# ── PUT /api/projects/{name} ──────────────────────────────────────────────────


def test_update_project_description(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/api/projects/", json={"name": "updatable"})
    r = client.put("/api/projects/updatable", json={"description": "updated!"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_update_project_not_found_returns_404(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.put("/api/projects/ghost", json={"description": "x"})
    assert r.status_code == 404


# ── DELETE /api/projects/{name} ───────────────────────────────────────────────


def test_delete_studio_project(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/api/projects/", json={"name": "deletable"})
    r = client.delete("/api/projects/deletable")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r2 = client.get("/api/projects/deletable")
    assert r2.status_code == 404


def test_delete_non_studio_project_returns_403(tmp_path, monkeypatch):
    """Auto-detected projects (source != 'studio') cannot be deleted via Studio."""
    import asyncio

    from lionagi.state.db import StateDB

    fake_db = tmp_path / "state.db"

    # Use a name without slashes so it works cleanly as a URL path segment.
    async def _seed():
        async with StateDB(fake_db) as db:
            await db.register_project("auto-detected", "git_remote")

    asyncio.run(_seed())

    client = _make_client(tmp_path, monkeypatch)
    r = client.delete("/api/projects/auto-detected")
    assert r.status_code == 403


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_update_project_with_sql_injection_in_description(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/api/projects/", json={"name": "injection-target"})
    injection = "'); DROP TABLE projects; --"
    r = client.put("/api/projects/injection-target", json={"description": injection})
    assert r.status_code == 200

    r2 = client.get("/api/projects/injection-target")
    assert r2.status_code == 200
    assert r2.json()["description"] == injection


def test_create_project_with_unicode_name(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    unicode_name = "project-alpha-プロジェクト"
    r = client.post("/api/projects/", json={"name": unicode_name})
    assert r.status_code == 201

    r2 = client.get(f"/api/projects/{unicode_name}")
    assert r2.status_code == 200
    assert r2.json()["name"] == unicode_name


def test_delete_project_with_running_sessions_returns_ok(tmp_path, monkeypatch):
    import asyncio
    import time

    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.projects as projects_mod
    from lionagi.state.db import StateDB

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(projects_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(projects_mod, "_DB", str(fake_db))

    async def _seed():
        async with StateDB(fake_db) as _:
            pass
        async with StateDB(fake_db) as db:
            prog_id = "prog-del-test"
            await db.create_progression(prog_id)
            await db.create_session(
                {
                    "id": "sess-del-test",
                    "progression_id": prog_id,
                    "name": "session-for-delete",
                    "status": "running",
                    "started_at": time.time(),
                }
            )

    asyncio.run(_seed())

    client = _make_client(tmp_path, monkeypatch)
    client.post("/api/projects/", json={"name": "delete-with-sessions"})
    r = client.delete("/api/projects/delete-with-sessions")
    assert r.status_code == 200
    assert r.json()["ok"] is True
