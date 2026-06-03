# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for #1173 state.db lifecycle (checkpoint, size alert, prune old data)."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import StateDB

from ._helpers import run_async

# ── helpers ───────────────────────────────────────────────────────────────────


async def _make_session(db: StateDB, *, status: str, started_at: float) -> str:
    pid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "name": f"s-{status}-{sid[:6]}",
            "status": status,
            "started_at": started_at,
        }
    )
    return sid


async def _make_schedule_run(db: StateDB, *, status: str, fired_at: float) -> str:
    sched_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    now_ts = time.time()
    await db.db.execute(
        "INSERT INTO schedules (id, name, trigger_type, action_kind, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (sched_id, f"s-{sched_id[:6]}", "cron", "agent", now_ts, now_ts),
    )
    await db.db.execute(
        "INSERT INTO schedule_runs"
        " (id, schedule_id, status, trigger_context, action_kind, action_args,"
        "  fired_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, sched_id, status, "{}", "agent", "[]", fired_at, fired_at, fired_at),
    )
    await db.db.commit()
    return run_id


def _patch_db(monkeypatch, db_path: Path) -> None:
    import lionagi.state.db as state_db_mod
    from lionagi.studio.services import db_maintenance as maint

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", db_path)


# ── checkpoint tests ──────────────────────────────────────────────────────────


def test_checkpoint_writes_admin_event(tmp_path, monkeypatch):
    """checkpoint_state_db() inserts an admin_events row and returns PRAGMA counts."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    run_async(_make_session_in(db_path, status="running", started_at=time.time()))

    result = run_async(maint.checkpoint_state_db(actor="test"))

    assert result["mode"] == "TRUNCATE"
    assert result["busy"] is not None
    assert result["checkpointed"] is not None

    last_cp = run_async(maint.get_last_checkpoint_at())
    assert last_cp is not None
    assert last_cp <= time.time()


def test_checkpoint_missing_db_is_noop(tmp_path, monkeypatch):
    """checkpoint_state_db() returns None counts when DB doesn't exist yet."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "nonexistent.db"
    _patch_db(monkeypatch, db_path)

    result = run_async(maint.checkpoint_state_db())
    assert result["busy"] is None
    assert result["checkpointed"] is None

    assert run_async(maint.get_last_checkpoint_at()) is None


# ── size alert tests ──────────────────────────────────────────────────────────


def test_size_alert_below_threshold(monkeypatch):
    import lionagi.studio.config as cfg
    from lionagi.studio.services import db_maintenance as maint

    monkeypatch.setattr(cfg, "DB_SIZE_ALERT_BYTES", 100 * 1024 * 1024)
    alert, threshold = maint.get_db_size_alert(50 * 1024 * 1024)
    assert alert is False
    assert threshold == 100 * 1024 * 1024


def test_size_alert_at_threshold(monkeypatch):
    import lionagi.studio.config as cfg
    from lionagi.studio.services import db_maintenance as maint

    monkeypatch.setattr(cfg, "DB_SIZE_ALERT_BYTES", 100 * 1024 * 1024)
    alert, threshold = maint.get_db_size_alert(100 * 1024 * 1024)
    assert alert is True
    assert threshold == 100 * 1024 * 1024


def test_stats_endpoint_exposes_checkpoint_and_size_fields(tmp_path, monkeypatch):
    """/api/stats includes last_checkpoint_at, size_alert, size_threshold_bytes."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.sessions as sessions_mod
    import lionagi.studio.services.stats as stats_mod
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))
    monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(stats_mod, "_DB", str(db_path))
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", db_path)

    run_async(_make_session_in(db_path, status="running", started_at=time.time()))
    run_async(maint.checkpoint_state_db(actor="test"))

    from lionagi.studio.app import app

    client = TestClient(app)
    r = client.get("/api/stats")
    assert r.status_code == 200
    db = r.json()["db"]
    assert db["last_checkpoint_at"] is not None
    assert isinstance(db["size_alert"], bool)
    assert db["size_threshold_bytes"] > 0


# ── prune tests ───────────────────────────────────────────────────────────────


def test_prune_removes_old_terminal_sessions_only(tmp_path, monkeypatch):
    """Prune deletes old terminal sessions; preserves running + recent terminal."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    old_ts = time.time() - 40 * 86400
    recent_ts = time.time() - 1 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            oc = await _make_session(db, status="completed", started_at=old_ts)
            of = await _make_session(db, status="failed", started_at=old_ts)
            ro = await _make_session(db, status="running", started_at=old_ts)
            rc = await _make_session(db, status="completed", started_at=recent_ts)
        return oc, of, ro, rc

    old_completed, old_failed, running_old, recent_completed = run_async(seed())

    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))
    assert result["sessions_pruned"] == 2

    async def remaining_ids():
        async with StateDB(db_path) as db:
            cur = await db.db.execute("SELECT id FROM sessions")
            rows = await cur.fetchall()
            return {r[0] for r in rows}

    rem = run_async(remaining_ids())
    assert old_completed not in rem
    assert old_failed not in rem
    assert running_old in rem
    assert recent_completed in rem


def test_prune_respects_fk_branches_cascade(tmp_path, monkeypatch):
    """Branches attached to pruned sessions are removed via CASCADE."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    old_ts = time.time() - 40 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            sid = await _make_session(db, status="completed", started_at=old_ts)
            pid = str(uuid.uuid4())
            await db.create_progression(pid)
            branch_id = str(uuid.uuid4())
            await db.db.execute(
                "INSERT INTO branches (id, session_id, progression_id, created_at, started_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (branch_id, sid, pid, old_ts, old_ts),
            )
            await db.db.commit()
        return sid, branch_id

    _, branch_id = run_async(seed())
    run_async(maint.prune_old_data(keep_days=30, actor="test"))

    async def check():
        async with StateDB(db_path) as db:
            cur = await db.db.execute("SELECT id FROM branches WHERE id = ?", (branch_id,))
            return await cur.fetchone()

    assert run_async(check()) is None


def test_prune_writes_admin_event(tmp_path, monkeypatch):
    """prune_old_data() writes an admin_events row with action='prune'."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    run_async(_make_session_in(db_path, status="completed", started_at=time.time() - 40 * 86400))
    run_async(maint.prune_old_data(keep_days=30, actor="test"))

    async def check():
        async with StateDB(db_path) as db:
            return await db.list_admin_events(action="prune", limit=5)

    events = run_async(check())
    assert len(events) >= 1
    assert events[0]["action"] == "prune"


def test_prune_old_schedule_runs(tmp_path, monkeypatch):
    """Prune removes old terminal schedule_runs; preserves running ones."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    old_ts = time.time() - 40 * 86400
    recent_ts = time.time() - 1 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            od = await _make_schedule_run(db, status="completed", fired_at=old_ts)
            oro = await _make_schedule_run(db, status="running", fired_at=old_ts)
            rd = await _make_schedule_run(db, status="completed", fired_at=recent_ts)
        return od, oro, rd

    old_done, old_running, recent_done = run_async(seed())
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))
    assert result["runs_pruned"] == 1

    async def check():
        async with StateDB(db_path) as db:
            cur = await db.db.execute("SELECT id FROM schedule_runs")
            rows = await cur.fetchall()
            return {r[0] for r in rows}

    rem = run_async(check())
    assert old_done not in rem
    assert old_running in rem
    assert recent_done in rem


def test_prune_old_data_endpoint(tmp_path, monkeypatch):
    """POST /api/admin/prune-old-data returns pruned counts."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.sessions as sessions_mod
    import lionagi.studio.services.stats as stats_mod
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))
    monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(stats_mod, "_DB", str(db_path))
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", db_path)

    run_async(_make_session_in(db_path, status="completed", started_at=time.time() - 40 * 86400))

    from lionagi.studio.app import app

    client = TestClient(app)
    r = client.post("/api/admin/prune-old-data", json={"keep_days": 30})
    assert r.status_code == 200
    data = r.json()
    assert data["sessions_pruned"] >= 1
    assert "runs_pruned" in data


# ── shared helper ─────────────────────────────────────────────────────────────


async def _make_session_in(db_path: Path, *, status: str, started_at: float) -> str:
    async with StateDB(db_path) as db:
        return await _make_session(db, status=status, started_at=started_at)
