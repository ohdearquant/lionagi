# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the engine_runs Studio API (Phase C Move 2).

Coverage targets:
  - GET /api/engine-runs/          (list, filters, empty state)
  - GET /api/engine-runs/{id}      (single row, 404 on miss)
  - lionagi.studio.services.engine_runs  (service layer, DB absent)
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.state.db import StateDB  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rid() -> str:
    return uuid.uuid4().hex


async def _seed_engine_run(
    db_path: Path,
    *,
    run_id: str | None = None,
    kind: str = "research",
    spec_json: dict | None = None,
    started_at: float = 1000.0,
    status: str = "running",
    session_id: str | None = None,
) -> str:
    rid = run_id or _rid()
    spec = spec_json or {"topic": "test topic"}
    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id=rid,
            kind=kind,
            spec_json=spec,
            started_at=started_at,
            session_id=session_id,
        )
        if status != "running":
            await db.update_engine_run(rid, status=status, ended_at=started_at + 100.0)
    return rid


# ---------------------------------------------------------------------------
# Studio service layer: engine_runs service
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_engine_runs_svc(tmp_path: Path, monkeypatch):
    import lionagi.studio.services.engine_runs as svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(svc, "_DB", str(db_path))
    monkeypatch.setattr(svc, "DEFAULT_DB_PATH", db_path)
    return svc, db_path


async def test_service_list_returns_empty_when_db_absent(patched_engine_runs_svc):
    svc, db_path = patched_engine_runs_svc
    result = await svc.list_engine_runs()
    assert result == []


async def test_service_get_returns_none_when_db_absent(patched_engine_runs_svc):
    svc, db_path = patched_engine_runs_svc
    result = await svc.get_engine_run("any-id")
    assert result is None


async def test_service_list_returns_seeded_rows(patched_engine_runs_svc):
    svc, db_path = patched_engine_runs_svc
    rid1 = await _seed_engine_run(db_path, kind="research", started_at=1000.0)
    rid2 = await _seed_engine_run(db_path, kind="review", started_at=1001.0)

    rows = await svc.list_engine_runs()
    ids = [r["id"] for r in rows]
    assert rid1 in ids
    assert rid2 in ids


async def test_service_list_newest_first(patched_engine_runs_svc):
    svc, db_path = patched_engine_runs_svc
    rid_old = await _seed_engine_run(db_path, started_at=500.0)
    rid_new = await _seed_engine_run(db_path, started_at=1500.0)

    rows = await svc.list_engine_runs()
    ids = [r["id"] for r in rows]
    assert ids.index(rid_new) < ids.index(rid_old)


async def test_service_list_filter_by_kind(patched_engine_runs_svc):
    svc, db_path = patched_engine_runs_svc
    rid_r = await _seed_engine_run(db_path, kind="research")
    rid_p = await _seed_engine_run(db_path, kind="planning")

    rows = await svc.list_engine_runs(kind="planning")
    ids = [r["id"] for r in rows]
    assert rid_p in ids
    assert rid_r not in ids


async def test_service_list_filter_by_status(patched_engine_runs_svc):
    svc, db_path = patched_engine_runs_svc
    rid_running = await _seed_engine_run(db_path, status="running")
    rid_done = await _seed_engine_run(db_path, status="completed")

    rows = await svc.list_engine_runs(status="completed")
    ids = [r["id"] for r in rows]
    assert rid_done in ids
    assert rid_running not in ids


async def test_service_get_returns_row(patched_engine_runs_svc):
    svc, db_path = patched_engine_runs_svc
    rid = await _seed_engine_run(
        db_path,
        kind="hypothesis",
        spec_json={"findings": "X causes Y"},
        started_at=900.0,
    )

    row = await svc.get_engine_run(rid)
    assert row is not None
    assert row["id"] == rid
    assert row["kind"] == "hypothesis"
    assert row["spec_json"] == {"findings": "X causes Y"}


async def test_service_get_returns_none_for_missing(patched_engine_runs_svc):
    svc, db_path = patched_engine_runs_svc
    # Ensure DB exists (create at least one row so the table exists)
    await _seed_engine_run(db_path)
    result = await svc.get_engine_run("nonexistent-id")
    assert result is None


async def test_service_spec_json_round_trips(patched_engine_runs_svc):
    """spec_json stored as TEXT is deserialized back to a dict by the service."""
    svc, db_path = patched_engine_runs_svc
    spec = {"topic": "GQA", "depth": 3, "tags": ["attn", "fast"]}
    rid = await _seed_engine_run(db_path, spec_json=spec)

    row = await svc.get_engine_run(rid)
    assert row is not None
    assert row["spec_json"] == spec


# ---------------------------------------------------------------------------
# HTTP endpoint layer
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_app(tmp_path: Path, monkeypatch):
    """Return (app, db_path, httpx.AsyncClient) with engine_runs DB patched.

    Skips when fastapi/httpx extras are not installed.
    """
    pytest.importorskip("fastapi", reason="studio extra not installed")
    httpx = pytest.importorskip("httpx", reason="httpx not installed")

    import lionagi.studio.services.engine_runs as er_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(er_svc, "_DB", str(db_path))
    monkeypatch.setattr(er_svc, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return app, db_path, client


async def test_list_endpoint_returns_empty(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.get("/api/engine-runs/")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_endpoint_returns_seeded_rows(patched_app):
    _, db_path, client = patched_app
    rid1 = await _seed_engine_run(db_path, kind="research", started_at=1000.0)
    rid2 = await _seed_engine_run(db_path, kind="review", started_at=1001.0)

    async with client as ac:
        resp = await ac.get("/api/engine-runs/")
    assert resp.status_code == 200
    data = resp.json()
    ids = [r["id"] for r in data]
    assert rid1 in ids
    assert rid2 in ids


async def test_list_endpoint_filter_kind(patched_app):
    _, db_path, client = patched_app
    rid_r = await _seed_engine_run(db_path, kind="research")
    rid_p = await _seed_engine_run(db_path, kind="planning")

    async with client as ac:
        resp = await ac.get("/api/engine-runs/?kind=planning")
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()]
    assert rid_p in ids
    assert rid_r not in ids


async def test_list_endpoint_filter_status(patched_app):
    _, db_path, client = patched_app
    rid_running = await _seed_engine_run(db_path, status="running")
    rid_done = await _seed_engine_run(db_path, status="completed")

    async with client as ac:
        resp = await ac.get("/api/engine-runs/?status=completed")
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()]
    assert rid_done in ids
    assert rid_running not in ids


async def test_list_endpoint_newest_first(patched_app):
    _, db_path, client = patched_app
    rid_old = await _seed_engine_run(db_path, started_at=500.0)
    rid_new = await _seed_engine_run(db_path, started_at=1500.0)

    async with client as ac:
        resp = await ac.get("/api/engine-runs/")
    assert resp.status_code == 200
    rows = resp.json()
    ids = [r["id"] for r in rows]
    assert ids.index(rid_new) < ids.index(rid_old)


async def test_get_endpoint_returns_row(patched_app):
    _, db_path, client = patched_app
    rid = await _seed_engine_run(
        db_path,
        kind="coding",
        spec_json={"spec": "BFS impl", "test_cmd": "pytest"},
        started_at=200.0,
    )

    async with client as ac:
        resp = await ac.get(f"/api/engine-runs/{rid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == rid
    assert data["kind"] == "coding"
    assert data["spec_json"] == {"spec": "BFS impl", "test_cmd": "pytest"}


async def test_get_endpoint_404_on_missing(patched_app):
    _, db_path, client = patched_app
    # Seed one row so the table exists.
    await _seed_engine_run(db_path)

    async with client as ac:
        resp = await ac.get("/api/engine-runs/nonexistent-run-id")
    assert resp.status_code == 404


async def test_get_endpoint_session_link_field(patched_app):
    """session_id on a run is returned in the response payload."""
    _, db_path, client = patched_app

    # Ensure schema is initialised by seeding a row without FK violation.
    await _seed_engine_run(db_path)

    # Insert a row with a non-null session_id bypassing the FK (the
    # production code only sets session_id when the session already exists;
    # this test is verifying the API field shape, not the FK constraint).
    rid = uuid.uuid4().hex
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys = OFF")
        await conn.execute(
            "INSERT INTO engine_runs (id, kind, spec_json, status, started_at, session_id)"
            " VALUES (?, 'research', '{}', 'running', 1000.0, 'test-session-001')",
            (rid,),
        )
        await conn.commit()

    async with client as ac:
        resp = await ac.get(f"/api/engine-runs/{rid}")
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "test-session-001"


def test_list_endpoint_bearer_auth(tmp_path: Path, monkeypatch):
    """GET /api/engine-runs/ returns 401 when auth token is set and missing."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    import lionagi.studio.services.engine_runs as er_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(er_svc, "_DB", str(db_path))
    monkeypatch.setattr(er_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "secret-engine-token")

    from lionagi.studio.app import app

    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/engine-runs/")
    assert resp.status_code == 401

    resp_ok = client.get(
        "/api/engine-runs/",
        headers={"Authorization": "Bearer secret-engine-token"},
    )
    assert resp_ok.status_code != 401
