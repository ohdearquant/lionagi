# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the engine_defs Studio API.

Coverage:
  - GET /api/engine-defs/          list, filter, empty
  - POST /api/engine-defs/         create, bad kind, bad name, flag injection, name conflict
  - GET /api/engine-defs/{id}      happy path, 404
  - PUT /api/engine-defs/{id}      update semantics, 404
  - DELETE /api/engine-defs/{id}   happy path, 404
  - service: kind frozenset matches _KIND_META
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")


def _rid() -> str:
    return uuid.uuid4().hex[:12]


async def _seed_engine_def(
    db_path: Path,
    *,
    name: str | None = None,
    kind: str = "research",
    model: str | None = None,
    description: str | None = None,
    options: dict | None = None,
) -> str:
    import time

    from lionagi.state.db import StateDB

    def_id = _rid()
    now = time.time()
    async with StateDB(db_path) as db:
        await db.create_engine_def(
            {
                "id": def_id,
                "name": name or f"def-{def_id}",
                "kind": kind,
                "model": model,
                "options": options,
                "description": description,
                "created_at": now,
                "updated_at": now,
            }
        )
    return def_id


# ── Kind frozenset parity test ──────────────────────────────────────────────


def test_valid_engine_kinds_matches_kind_meta():
    """_VALID_ENGINE_KINDS must equal the keys of _KIND_META to prevent drift."""
    from lionagi.cli.engine import _KIND_META
    from lionagi.studio.services.engine_defs import _VALID_ENGINE_KINDS

    assert _VALID_ENGINE_KINDS == set(_KIND_META), (
        "_VALID_ENGINE_KINDS drifted from _KIND_META. "
        f"Extra in svc: {_VALID_ENGINE_KINDS - set(_KIND_META)!r}. "
        f"Missing in svc: {set(_KIND_META) - _VALID_ENGINE_KINDS!r}."
    )


# ── Service layer ────────────────────────────────────────────────────────────


@pytest.fixture
def patched_svc(tmp_path: Path, monkeypatch):
    import lionagi.studio.services.engine_defs as svc
    from lionagi.state.db import DEFAULT_DB_PATH

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(svc, "DEFAULT_DB_PATH", db_path)
    # Also patch db module's DEFAULT_DB_PATH so StateDB() picks up the temp path.
    import lionagi.state.db as db_mod

    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    return svc, db_path


async def test_list_returns_empty_when_db_absent(patched_svc):
    svc, _ = patched_svc
    assert await svc.list_engine_defs() == []


async def test_create_and_list(patched_svc):
    svc, db_path = patched_svc
    result = await svc.create_engine_def({"name": "my-engine", "kind": "research"})
    assert "id" in result
    rows = await svc.list_engine_defs()
    assert any(r["name"] == "my-engine" for r in rows)


async def test_create_bad_kind_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="Invalid engine kind"):
        await svc.create_engine_def({"name": "bad", "kind": "nonexistent"})


async def test_create_bad_name_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError):
        await svc.create_engine_def({"name": "-badname", "kind": "research"})


async def test_create_flag_injection_in_model_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="starts with"):
        await svc.create_engine_def({"name": "ok", "kind": "coding", "model": "--inject"})


async def test_create_flag_injection_in_options_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="starts with"):
        await svc.create_engine_def(
            {
                "name": "ok2",
                "kind": "coding",
                "options": {"test_cmd": "--rm -rf /"},
            }
        )


async def test_create_bad_options_key_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="disallowed keys"):
        await svc.create_engine_def(
            {
                "name": "ok3",
                "kind": "research",
                "options": {"unknown_key": "value"},
            }
        )


async def test_create_name_conflict_raises(patched_svc):
    svc, _ = patched_svc
    await svc.create_engine_def({"name": "dup-name", "kind": "research"})
    with pytest.raises(Exception, match="already exists"):
        await svc.create_engine_def({"name": "dup-name", "kind": "review"})


async def test_get_returns_none_when_absent(patched_svc):
    svc, _ = patched_svc
    assert await svc.get_engine_def("nonexistent") is None


async def test_get_returns_row(patched_svc):
    svc, db_path = patched_svc
    def_id = await _seed_engine_def(db_path, name="my-def", kind="coding", model="gpt-4o")
    row = await svc.get_engine_def(def_id)
    assert row is not None
    assert row["name"] == "my-def"
    assert row["kind"] == "coding"
    assert row["model"] == "gpt-4o"


async def test_update_returns_false_for_missing(patched_svc):
    svc, db_path = patched_svc
    # Ensure DB exists
    await _seed_engine_def(db_path)
    ok = await svc.update_engine_def("nonexistent", {"description": "x"})
    assert ok is False


async def test_update_changes_fields(patched_svc):
    svc, db_path = patched_svc
    def_id = await _seed_engine_def(db_path, name="upd-def", kind="planning")
    ok = await svc.update_engine_def(def_id, {"description": "new desc"})
    assert ok is True
    row = await svc.get_engine_def(def_id)
    assert row["description"] == "new desc"


async def test_delete_returns_true(patched_svc):
    svc, db_path = patched_svc
    def_id = await _seed_engine_def(db_path)
    ok = await svc.delete_engine_def(def_id)
    assert ok is True
    assert await svc.get_engine_def(def_id) is None


async def test_delete_returns_false_for_missing(patched_svc):
    svc, db_path = patched_svc
    await _seed_engine_def(db_path)
    ok = await svc.delete_engine_def("nonexistent")
    assert ok is False


async def test_max_depth_out_of_range_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="max_depth"):
        await svc.create_engine_def({"name": "x", "kind": "research", "max_depth": 0})


async def test_max_agents_out_of_range_raises(patched_svc):
    svc, _ = patched_svc
    with pytest.raises(ValueError, match="max_agents"):
        await svc.create_engine_def({"name": "x", "kind": "research", "max_agents": 101})


# ── HTTP layer ───────────────────────────────────────────────────────────────


@pytest.fixture
def patched_app(tmp_path: Path, monkeypatch):
    pytest.importorskip("fastapi", reason="studio extra not installed")
    httpx = pytest.importorskip("httpx", reason="httpx not installed")

    import lionagi.state.db as db_mod
    import lionagi.studio.services.engine_defs as ed_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ed_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return app, db_path, client


async def test_list_endpoint_empty(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.get("/api/engine-defs/")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_endpoint_happy_path(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.post(
            "/api/engine-defs/",
            json={"name": "my-engine", "kind": "research"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert "id" in body
    assert body["name"] == "my-engine"


async def test_create_endpoint_bad_kind_422(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.post(
            "/api/engine-defs/",
            json={"name": "bad", "kind": "nonexistent"},
        )
    assert resp.status_code == 422


async def test_create_endpoint_bad_name_422(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.post(
            "/api/engine-defs/",
            json={"name": "-inject", "kind": "research"},
        )
    assert resp.status_code == 422


async def test_create_endpoint_flag_injection_in_model_422(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.post(
            "/api/engine-defs/",
            json={"name": "ok", "kind": "coding", "model": "--inject"},
        )
    assert resp.status_code == 422


async def test_create_endpoint_flag_injection_in_options_422(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        resp = await ac.post(
            "/api/engine-defs/",
            json={"name": "ok2", "kind": "coding", "options": {"test_cmd": "--rm -rf /"}},
        )
    assert resp.status_code == 422


async def test_create_endpoint_name_conflict_409(patched_app):
    _, db_path, client = patched_app
    async with client as ac:
        r1 = await ac.post("/api/engine-defs/", json={"name": "dup", "kind": "research"})
        assert r1.status_code == 201
        r2 = await ac.post("/api/engine-defs/", json={"name": "dup", "kind": "review"})
        assert r2.status_code == 409


async def test_get_endpoint_happy_path(patched_app):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="get-me", kind="planning")
    async with client as ac:
        resp = await ac.get(f"/api/engine-defs/{def_id}")
    assert resp.status_code == 200
    assert resp.json()["kind"] == "planning"


async def test_get_endpoint_404(patched_app):
    _, db_path, client = patched_app
    await _seed_engine_def(db_path)
    async with client as ac:
        resp = await ac.get("/api/engine-defs/nonexistent")
    assert resp.status_code == 404


async def test_update_endpoint_happy_path(patched_app):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path, name="upd-me", kind="hypothesis")
    async with client as ac:
        resp = await ac.put(
            f"/api/engine-defs/{def_id}",
            json={"description": "updated"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_update_endpoint_404(patched_app):
    _, db_path, client = patched_app
    await _seed_engine_def(db_path)
    async with client as ac:
        resp = await ac.put("/api/engine-defs/nonexistent", json={"description": "x"})
    assert resp.status_code == 404


async def test_delete_endpoint_happy_path(patched_app):
    _, db_path, client = patched_app
    def_id = await _seed_engine_def(db_path)
    async with client as ac:
        resp = await ac.delete(f"/api/engine-defs/{def_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_delete_endpoint_404(patched_app):
    _, db_path, client = patched_app
    await _seed_engine_def(db_path)
    async with client as ac:
        resp = await ac.delete("/api/engine-defs/nonexistent")
    assert resp.status_code == 404


async def test_list_endpoint_filter_by_kind(patched_app):
    _, db_path, client = patched_app
    await _seed_engine_def(db_path, name="r1", kind="research")
    await _seed_engine_def(db_path, name="p1", kind="planning")
    async with client as ac:
        resp = await ac.get("/api/engine-defs/?kind=planning")
    assert resp.status_code == 200
    rows = resp.json()
    assert all(r["kind"] == "planning" for r in rows)
    names = [r["name"] for r in rows]
    assert "p1" in names
    assert "r1" not in names
