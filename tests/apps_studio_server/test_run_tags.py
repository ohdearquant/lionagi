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
    _run(_seed_session(db_path, sid))
    _run(run_tags.add_tag(sid, "needs-followup"))

    tagmap = _run(run_tags.tags_for_sessions([sid]))
    assert tagmap == {sid: ["needs-followup"]}


def test_add_tag_twice_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    import lionagi.studio.services.run_tags as run_tags

    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
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
    _run(_seed_session(db_path, sid))
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
    for sid in (sid1, sid2, sid3):
        _run(_seed_session(db_path, sid))
    _run(run_tags.add_tag(sid1, "a"))
    _run(run_tags.add_tag(sid1, "b"))
    _run(run_tags.add_tag(sid2, "a"))
    # sid3 gets no tags

    tagmap = _run(run_tags.tags_for_sessions([sid1, sid2, sid3]))
    assert tagmap == {sid1: ["a", "b"], sid2: ["a"]}
    assert sid3 not in tagmap


def test_tags_for_sessions_chunks_beyond_sql_variable_limit(tmp_path, monkeypatch):
    # A run history larger than SQLite's bound-variable limit must not overflow
    # the IN(...) list — tags_for_sessions chunks the lookup. Request more ids
    # than _MAX_SQL_VARS, with a tagged session in each chunk.
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    import lionagi.studio.services.run_tags as run_tags

    ids = [str(uuid.uuid4()) for _ in range(run_tags._MAX_SQL_VARS + 50)]
    first, last = ids[0], ids[-1]
    _run(_seed_session(db_path, first))
    _run(_seed_session(db_path, last))
    _run(run_tags.add_tag(first, "front"))
    _run(run_tags.add_tag(last, "back"))

    tagmap = _run(run_tags.tags_for_sessions(ids))
    assert tagmap == {first: ["front"], last: ["back"]}


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
    _run(_seed_session(db_path, both))
    _run(_seed_session(db_path, only_a))
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
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    r = client.post(f"/api/sessions/{sid}/tags", json={"tag": "urgent"})
    assert r.status_code == 200
    data = r.json()
    assert data["session_id"] == sid
    assert data["tags"] == ["urgent"]


def test_add_run_tag_route_rejects_empty_tag(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    r = client.post(f"/api/sessions/{sid}/tags", json={"tag": "   "})
    assert r.status_code == 422


def test_add_run_tag_route_404_for_unknown_session(tmp_path, monkeypatch):
    # Tagging must reject a session that does not exist, matching the other
    # session-child routes — otherwise the tag is written but stays invisible
    # in /api/runs (which only surfaces rows joined from `sessions`).
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    client = _make_client(db_path, monkeypatch)

    r = client.post(f"/api/sessions/{uuid.uuid4()}/tags", json={"tag": "urgent"})
    assert r.status_code == 404


def test_remove_run_tag_route(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

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


# ── a tag READ must never create a partial db on a fresh install ────────


def test_list_runs_tag_filter_on_fresh_install_does_not_create_db(tmp_path, monkeypatch):
    """GET /api/runs?tag=x on a brand-new install (no state.db) must not create

    a partial db containing only the run_tags table -- that would leave every
    later `SELECT ... FROM sessions` 500ing.
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
    later sessions query 500s.

    The run_tags -> sessions FK means tagging a nonexistent session fails, but
    the schema-init must still run first so the db is never left partial.
    """
    import sqlite3

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    assert not db_path.exists()

    import lionagi.studio.services.run_tags as run_tags
    import lionagi.studio.services.sessions as sessions_mod

    sid = str(uuid.uuid4())
    # No session row exists yet, so the tag insert trips the FK -- but only
    # after the full schema is built (a run_tags-only partial db would 500 the
    # sessions query below instead).
    with pytest.raises(sqlite3.IntegrityError):
        _run(run_tags.add_tag(sid, "urgent"))

    assert db_path.exists()
    # A `sessions` query must not raise -- it would if only run_tags existed.
    assert _run(sessions_mod.list_sessions()) == []

    # With the session seeded, the same tag now attaches, and dropping the
    # session cascades the tag away (no orphan row survives a prune).
    _run(_seed_session(db_path, sid))
    _run(run_tags.add_tag(sid, "urgent"))
    assert _run(run_tags.tags_for_sessions([sid])) == {sid: ["urgent"]}


def test_pruning_a_session_cascades_its_tags(tmp_path, monkeypatch):
    # add_run_tag rejects orphan tags on write; deleting the parent session
    # must not reintroduce them. The run_tags -> sessions FK ON DELETE CASCADE
    # removes the tag rows when admin.prune_sessions() drops the session.
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    import lionagi.studio.services.admin as admin
    import lionagi.studio.services.run_tags as run_tags

    # admin freezes its own _DB / DEFAULT_DB_PATH at import; _patch_db does not
    # touch them, so point prune at the tmp db too.
    monkeypatch.setattr(admin, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin, "_DB", str(db_path))

    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    _run(run_tags.add_tag(sid, "keep"))
    assert _run(run_tags.tags_for_sessions([sid])) == {sid: ["keep"]}

    pruned = _run(admin.prune_sessions([sid]))
    assert pruned == 1
    assert _run(run_tags.tags_for_sessions([sid])) == {}


def test_remove_tag_on_fresh_install_does_not_create_db(tmp_path, monkeypatch):
    """A delete has nothing to detach on a fresh install and must NOT create
    the db -- a bare _ensure_table would leave a run_tags-only partial db that
    makes every later sessions query 500. Mirrors the read-path guards.
    """
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    assert not db_path.exists()

    import lionagi.studio.services.run_tags as run_tags

    _run(run_tags.remove_tag(str(uuid.uuid4()), "urgent"))
    assert not db_path.exists()


def test_remove_tag_route_on_fresh_install_does_not_create_partial_db(tmp_path, monkeypatch):
    """DELETE as the FIRST tag endpoint hit on a fresh install must return an
    empty tag set and leave NO db behind (not even a run_tags-only one).
    """
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)
    assert not db_path.exists()

    r = client.delete(f"/api/sessions/{uuid.uuid4()}/tags/urgent")
    assert r.status_code == 200
    assert r.json()["tags"] == []
    assert not db_path.exists()


def test_statedb_open_creates_run_tags_table(tmp_path):
    """run_tags lives in the canonical schema_meta, so StateDB.open() alone
    (no service-side _ensure_table) creates it -- keeping the SQLite, Postgres,
    and schema-parity paths consistent.
    """
    import sqlite3

    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))  # StateDB open+close only, no _ensure_table

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='run_tags'"
        ).fetchall()
    finally:
        con.close()
    assert rows == [("run_tags",)]


# ── free-form tags containing "/" must round-trip through the DELETE route ──


def test_remove_run_tag_route_handles_slash_in_tag(tmp_path, monkeypatch):
    """A tag like 'team/backend' must be deletable through the actual route.

    The DELETE path param must capture the rest of the path -- exercised here
    via TestClient, not by calling remove_tag() directly, so the routing
    itself is proven.
    """
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    r = client.post(f"/api/sessions/{sid}/tags", json={"tag": "team/backend"})
    assert r.status_code == 200
    assert r.json()["tags"] == ["team/backend"]

    r2 = client.delete(f"/api/sessions/{sid}/tags/team/backend")
    assert r2.status_code == 200
    assert r2.json()["tags"] == []

    import lionagi.studio.services.run_tags as run_tags

    tagmap = _run(run_tags.tags_for_sessions([sid]))
    assert sid not in tagmap
