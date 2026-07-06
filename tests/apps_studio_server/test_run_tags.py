# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the run_tags m2m store, service, and the /runs tag filter (row C, slice 1)."""

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


def _patch_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.run_tags as run_tags_mod
    import lionagi.studio.services.sessions as sessions_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(run_tags_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(run_tags_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))


def _make_client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _patch_db(monkeypatch, db_path)
    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


async def _init_db(db_path: Path) -> None:
    async with StateDB(db_path):
        pass  # opens + applies schema (creates run_tags table too)


async def _seed_session(db_path: Path, session_id: str, **fields) -> None:
    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        payload = {
            "id": session_id,
            "progression_id": pid,
            "status": fields.pop("status", "completed"),
            "started_at": fields.pop("started_at", time.time()),
            **fields,
        }
        await db.create_session(payload)


# ── add_tag / tags_for_sessions / remove_tag (direct, no HTTP) ───────────────


def test_add_tag_then_tags_for_sessions_returns_it(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    import lionagi.studio.services.run_tags as run_tags

    sid = str(uuid.uuid4())
    _run(run_tags.add_tag(sid, "needs-followup"))

    tagmap = _run(run_tags.tags_for_sessions([sid]))
    assert tagmap == {sid: ["needs-followup"]}


def test_add_tag_twice_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    import lionagi.studio.services.run_tags as run_tags

    sid = str(uuid.uuid4())
    _run(run_tags.add_tag(sid, "x"))
    _run(run_tags.add_tag(sid, "x"))

    tagmap = _run(run_tags.tags_for_sessions([sid]))
    assert tagmap == {sid: ["x"]}


def test_remove_tag_removes_it(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    import lionagi.studio.services.run_tags as run_tags

    sid = str(uuid.uuid4())
    _run(run_tags.add_tag(sid, "x"))
    _run(run_tags.remove_tag(sid, "x"))

    tagmap = _run(run_tags.tags_for_sessions([sid]))
    assert tagmap == {}


def test_add_tag_empty_string_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    import lionagi.studio.services.run_tags as run_tags

    sid = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc_info:
        _run(run_tags.add_tag(sid, "   "))
    assert exc_info.value.status_code == 422


def test_tags_for_sessions_empty_list_returns_empty_dict(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    import lionagi.studio.services.run_tags as run_tags

    assert _run(run_tags.tags_for_sessions([])) == {}


def test_tags_for_sessions_batches_multiple_sessions_in_one_call(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    import lionagi.studio.services.run_tags as run_tags

    sid1, sid2, sid3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    _run(run_tags.add_tag(sid1, "a"))
    _run(run_tags.add_tag(sid1, "b"))
    _run(run_tags.add_tag(sid2, "a"))
    # sid3 gets no tags

    tagmap = _run(run_tags.tags_for_sessions([sid1, sid2, sid3]))
    assert tagmap == {sid1: ["a", "b"], sid2: ["a"]}
    assert sid3 not in tagmap


# ── session_ids_with_tags — the F8 SQL pre-filter (AND-composed) ────────────


def test_session_ids_with_tags_none_when_no_tags_requested(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    import lionagi.studio.services.run_tags as run_tags

    assert _run(run_tags.session_ids_with_tags([])) is None
    assert _run(run_tags.session_ids_with_tags(None)) is None


def test_session_ids_with_tags_and_composition(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    import lionagi.studio.services.run_tags as run_tags

    both = str(uuid.uuid4())
    only_a = str(uuid.uuid4())
    _run(run_tags.add_tag(both, "a"))
    _run(run_tags.add_tag(both, "b"))
    _run(run_tags.add_tag(only_a, "a"))

    result = _run(run_tags.session_ids_with_tags(["a", "b"]))
    assert result == {both}
    assert only_a not in result


def test_session_ids_with_tags_no_matches_returns_empty_set(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    import lionagi.studio.services.run_tags as run_tags

    result = _run(run_tags.session_ids_with_tags(["none"]))
    assert result == set()


# ── Route-level: POST/DELETE /api/sessions/{id}/tags ─────────────────────────


def test_add_run_tag_route_returns_current_tags(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    client = _make_client(db_path, monkeypatch)

    sid = str(uuid.uuid4())
    r = client.post(f"/api/sessions/{sid}/tags", json={"tag": "urgent"})
    assert r.status_code == 200
    data = r.json()
    assert data["session_id"] == sid
    assert data["tags"] == ["urgent"]


def test_add_run_tag_route_rejects_empty_tag(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    client = _make_client(db_path, monkeypatch)

    sid = str(uuid.uuid4())
    r = client.post(f"/api/sessions/{sid}/tags", json={"tag": "   "})
    assert r.status_code == 422


def test_remove_run_tag_route(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    client = _make_client(db_path, monkeypatch)

    sid = str(uuid.uuid4())
    client.post(f"/api/sessions/{sid}/tags", json={"tag": "urgent"})
    r = client.delete(f"/api/sessions/{sid}/tags/urgent")
    assert r.status_code == 200
    assert r.json()["tags"] == []


# ── Round-trip via the run row (R1: run_id ≡ session_id) ─────────────────────


def test_list_runs_tag_filter_round_trips_through_run_row(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    r = client.post(f"/api/sessions/{sid}/tags", json={"tag": "x"})
    assert r.status_code == 200

    r2 = client.get("/api/runs?tag=x")
    assert r2.status_code == 200
    runs = r2.json()["runs"]
    target = next((run for run in runs if run["run_id"] == sid), None)
    assert target is not None, "tagged session did not round-trip through the run row"
    assert target["id"] == sid
    assert "x" in target["tags"]


def test_list_runs_tag_filter_no_match_is_empty(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    r = client.get("/api/runs?tag=none")
    assert r.status_code == 200
    assert r.json()["runs"] == []


def test_list_runs_without_tag_filter_still_attaches_tags_field(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    client.post(f"/api/sessions/{sid}/tags", json={"tag": "x"})

    r = client.get("/api/runs")
    assert r.status_code == 200
    target = next((run for run in r.json()["runs"] if run["id"] == sid), None)
    assert target is not None
    assert target["tags"] == ["x"]


# ── P2a: a tag READ must never create a partial db on a fresh install ────────


def test_list_runs_tag_filter_on_fresh_install_does_not_create_db(tmp_path, monkeypatch):
    """GET /api/runs?tag=x on a brand-new install (no state.db) must not create

    a partial db containing only the run_tags table -- that would leave every
    later `SELECT ... FROM sessions` 500ing (codex P2a, PR #1834).
    """
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    assert not db_path.exists()

    import lionagi.studio.services.runs as runs_mod

    result = _run(runs_mod.list_runs(tag=["x"]))

    assert result == []
    assert not db_path.exists(), "a tag read created a (partial) db file"


def test_tags_for_sessions_on_fresh_install_does_not_create_db(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    assert not db_path.exists()

    import lionagi.studio.services.run_tags as run_tags

    assert _run(run_tags.tags_for_sessions([str(uuid.uuid4())])) == {}
    assert not db_path.exists()


def test_session_ids_with_tags_on_fresh_install_does_not_create_db(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    assert not db_path.exists()

    import lionagi.studio.services.run_tags as run_tags

    assert _run(run_tags.session_ids_with_tags(["x"])) is None
    assert not db_path.exists()


def test_add_tag_on_fresh_install_initializes_full_schema_not_just_run_tags(tmp_path, monkeypatch):
    """add_tag on a fresh install must apply the FULL schema (sessions, etc.),

    not just the run_tags table -- otherwise the db is left partial and a
    later sessions query 500s (codex P2a, PR #1834).
    """
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    assert not db_path.exists()

    import lionagi.studio.services.run_tags as run_tags
    import lionagi.studio.services.sessions as sessions_mod

    sid = str(uuid.uuid4())
    _run(run_tags.add_tag(sid, "urgent"))

    assert db_path.exists()
    # A `sessions` query must not raise -- it would if only run_tags existed.
    assert _run(sessions_mod.list_sessions()) == []
    tagmap = _run(run_tags.tags_for_sessions([sid]))
    assert tagmap == {sid: ["urgent"]}


# ── P2b: free-form tags containing "/" must round-trip through the DELETE route ──


def test_remove_run_tag_route_handles_slash_in_tag(tmp_path, monkeypatch):
    """A tag like 'team/backend' must be deletable through the actual route.

    The DELETE path param must capture the rest of the path (codex P2b,
    PR #1834) -- exercised here via TestClient, not by calling remove_tag()
    directly, so the routing itself is proven.
    """
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    client = _make_client(db_path, monkeypatch)

    sid = str(uuid.uuid4())
    r = client.post(f"/api/sessions/{sid}/tags", json={"tag": "team/backend"})
    assert r.status_code == 200
    assert r.json()["tags"] == ["team/backend"]

    r2 = client.delete(f"/api/sessions/{sid}/tags/team/backend")
    assert r2.status_code == 200
    assert r2.json()["tags"] == []

    import lionagi.studio.services.run_tags as run_tags

    tagmap = _run(run_tags.tags_for_sessions([sid]))
    assert sid not in tagmap
