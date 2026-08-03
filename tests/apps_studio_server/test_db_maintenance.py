# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for state.db lifecycle (checkpoint, size alert, prune old data)."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB

from ._helpers import run_async

# ── helpers ───────────────────────────────────────────────────────────────────


def _details(event: dict) -> dict:
    """admin_events.details round-trips as a JSON string on sqlite."""
    raw = event["details"]
    return json.loads(raw) if isinstance(raw, str) else raw


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
            "updated_at": started_at,
        }
    )
    return sid


async def _make_schedule_run(db: StateDB, *, status: str, fired_at: float) -> str:
    sched_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    now_ts = time.time()
    await db.execute(
        "INSERT INTO schedules (id, name, trigger_type, action_kind, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (sched_id, f"s-{sched_id[:6]}", "cron", "agent", now_ts, now_ts),
    )
    await db.execute(
        "INSERT INTO schedule_runs"
        " (id, schedule_id, status, trigger_context, action_kind, action_args,"
        "  fired_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, sched_id, status, "{}", "agent", "[]", fired_at, fired_at, fired_at),
    )
    return run_id


def _patch_db(monkeypatch, db_path: Path) -> None:
    from lionagi.studio.services import db_maintenance as maint

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


# ── checkpoint tests ──────────────────────────────────────────────────────────


def test_checkpoint_writes_admin_event(tmp_path, monkeypatch):
    """checkpoint_state_db() inserts an admin_events row and returns PRAGMA counts."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    fixed_now = 1_000_000.0
    monkeypatch.setattr(state_db_mod, "time", SimpleNamespace(time=lambda: fixed_now))

    run_async(_make_session_in(db_path, status="running", started_at=time.time()))

    result = run_async(maint.checkpoint_state_db(actor="test"))

    assert result["mode"] == "TRUNCATE"
    assert result["busy"] is not None
    assert result["checkpointed"] is not None

    last_cp = run_async(maint.get_last_checkpoint_at())
    assert last_cp is not None
    assert last_cp == fixed_now


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

    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    run_async(_make_session_in(db_path, status="running", started_at=time.time()))
    run_async(maint.checkpoint_state_db(actor="test"))

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
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
            rows = await db.fetch_all("SELECT id FROM sessions")
            return {r["id"] for r in rows}

    rem = run_async(remaining_ids())
    assert old_completed not in rem
    assert old_failed not in rem
    assert running_old in rem
    assert recent_completed in rem


def test_prune_preserves_old_session_updated_by_a_recent_resume(tmp_path, monkeypatch):
    """Retention is measured from the latest leg, not the first one."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    old_ts = time.time() - 40 * 86400
    recent_ts = time.time() - 1 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            session_id = await _make_session(db, status="completed", started_at=old_ts)
            await db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (recent_ts, session_id),
            )
            return session_id

    session_id = run_async(seed())

    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))

    assert result["sessions_pruned"] == 0

    async def read():
        async with StateDB(db_path) as db:
            return await db.get_session(session_id)

    assert run_async(read()) is not None


def _reopen_before_call(monkeypatch, maint, session_id: str, *, call: int) -> None:
    """Return *session_id* to running just before the *call*-th chunked write.

    Resuming a branch puts a terminal session back to running, so a session
    selected for pruning can stop being prunable while the prune is running.
    The seam lets that happen at a chosen point instead of waiting for the
    interleaving to occur on its own. Writing on the prune's own connection is
    how a committed write from another transaction looks to the statements that
    follow it, which is what the backends this runs on actually allow.

    Call 1 is the statement that takes the lock, so ``call=1`` is a reopen that
    beats the lock and ``call>=2`` is one that would have to defeat it.
    """
    from sqlalchemy import text

    real = maint._exec_chunked
    seen = {"n": 0}

    async def wrapper(conn, sql_prefix, ids, extra_params=(), suffix="", suffix_params=()):
        seen["n"] += 1
        if seen["n"] == call:
            await conn.execute(
                text("UPDATE sessions SET status = 'running' WHERE id = :sid"),
                {"sid": session_id},
            )
        return await real(conn, sql_prefix, ids, extra_params, suffix, suffix_params)

    monkeypatch.setattr(maint, "_exec_chunked", wrapper)


def _seed_session_with_history(db_path: Path, old_ts: float) -> str:
    """One old terminal session carrying a transition record and an artifact."""

    async def seed():
        async with StateDB(db_path) as db:
            sid = await _make_session(db, status="completed", started_at=old_ts)
            await db.execute(
                "INSERT INTO status_transitions"
                " (id, entity_type, entity_id, previous_status, status, reason_code,"
                "  source, created_at)"
                " VALUES (?, 'session', ?, 'running', 'completed', 'run.completed.ok',"
                "  'executor', ?)",
                (str(uuid.uuid4()), sid, old_ts),
            )
            await db.execute(
                "INSERT INTO artifacts (id, session_id, created_at, updated_at, kind, name,"
                " content) VALUES (?, ?, ?, ?, 'file', 'a.txt', '{}')",
                (str(uuid.uuid4()), sid, old_ts, old_ts),
            )
        return sid

    return run_async(seed())


def _session_and_its_history(db_path: Path, sid: str):
    async def read():
        async with StateDB(db_path) as db:
            return (
                await db.fetch_one("SELECT id, status FROM sessions WHERE id = ?", (sid,)),
                await db.fetch_all("SELECT id FROM status_transitions WHERE entity_id = ?", (sid,)),
                await db.fetch_all("SELECT id FROM artifacts WHERE session_id = ?", (sid,)),
            )

    return run_async(read())


def test_prune_rechecks_updated_at_after_candidate_lock(tmp_path, monkeypatch):
    """A candidate refreshed before its lock keeps its row and history."""
    from sqlalchemy import text

    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    sid = _seed_session_with_history(db_path, time.time() - 40 * 86400)
    real_exec = maint._exec_chunked
    refreshed = False

    async def refresh_before_lock(
        conn, sql_prefix, ids, extra_params=(), suffix="", suffix_params=()
    ):
        nonlocal refreshed
        if not refreshed and sql_prefix.startswith("UPDATE sessions SET updated_at"):
            refreshed = True
            await conn.execute(
                text("UPDATE sessions SET updated_at = :now WHERE id = :sid"),
                {"now": time.time(), "sid": sid},
            )
        return await real_exec(conn, sql_prefix, ids, extra_params, suffix, suffix_params)

    monkeypatch.setattr(maint, "_exec_chunked", refresh_before_lock)

    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))

    row, transitions, artifacts = _session_and_its_history(db_path, sid)
    assert refreshed, "the candidate was not refreshed at the lock seam"
    assert row is not None and row["status"] == "completed"
    assert len(transitions) == 1
    assert len(artifacts) == 1
    assert result["sessions_pruned"] == 0


def test_the_candidate_rows_are_held_before_their_status_is_read(tmp_path, monkeypatch):
    """The prune must write to its candidates before it reads their status.

    The read is what decides the batch, and on both backends it is a write that
    holds a row against other transactions. Reading first and writing later
    leaves the decision resting on a value that anyone may change in between,
    so the order here is the whole guarantee, not a detail of it.
    """
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _seed_session_with_history(db_path, time.time() - 40 * 86400)

    order: list[tuple[str, str]] = []
    real_exec, real_fetch = maint._exec_chunked, maint._fetch_chunked

    async def exec_spy(conn, sql_prefix, ids, extra_params=(), suffix="", suffix_params=()):
        order.append(("write", sql_prefix))
        return await real_exec(conn, sql_prefix, ids, extra_params, suffix, suffix_params)

    async def fetch_spy(conn, sql_prefix, ids, extra_params=()):
        order.append(("read", sql_prefix))
        return await real_fetch(conn, sql_prefix, ids, extra_params)

    monkeypatch.setattr(maint, "_exec_chunked", exec_spy)
    monkeypatch.setattr(maint, "_fetch_chunked", fetch_spy)

    run_async(maint.prune_old_data(keep_days=30, actor="test"))

    against_sessions = [step for step in order if " sessions " in f" {step[1]} "]
    assert against_sessions, "the prune never touched the sessions table"
    kind, sql = against_sessions[0]
    assert kind == "write", f"the first statement against sessions was a read: {sql}"
    assert "UPDATE sessions" in sql


def test_prune_leaves_a_session_that_reopened_before_the_lock(tmp_path, monkeypatch):
    """A session that comes back to life before the lock is simply not pruned.

    This is the reachable case, and it has to stay quiet: resuming a branch
    while a maintenance pass happens to be running is ordinary, so it ends in a
    zero count rather than an error, with the session and everything attached
    to it untouched.
    """
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    sid = _seed_session_with_history(db_path, time.time() - 40 * 86400)
    _reopen_before_call(monkeypatch, maint, sid, call=1)

    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))

    row, transitions, artifacts = _session_and_its_history(db_path, sid)
    assert row is not None and row["status"] == "running"
    assert len(transitions) == 1, "the reopened session lost its transition history"
    assert len(artifacts) == 1, "the reopened session lost its artifact association"
    assert result["sessions_pruned"] == 0


@pytest.mark.parametrize("call", [2, 3, 4, 5, 6, 7])
def test_a_session_that_reopens_past_the_lock_takes_the_whole_pass_down(
    tmp_path, monkeypatch, call
):
    """Nothing may be committed for a session that reopened mid-sequence.

    The prune clears associations and deletes transition history before it
    deletes the session, so a per-statement condition protects each statement
    on its own and still lets the sequence land half-applied: the earlier
    writes are already done when the reopen arrives, and the delete then skips
    the row, leaving a live session whose history and links are gone. The lock
    is what keeps this from being reachable; the parametrization drives the
    reopen past it at each step, and every case asserts the session survives
    whole, so nothing short of abandoning the pass passes here.

    The reopen rides the prune's own transaction here, so it is rolled back
    along with everything else and the status reads terminal again afterwards.
    A real resume commits on its own and would keep its status; what this
    checks is the part that is the same either way, which is that none of the
    prune's writes reached the database.
    """
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    sid = _seed_session_with_history(db_path, time.time() - 40 * 86400)
    _reopen_before_call(monkeypatch, maint, sid, call=call)

    with pytest.raises(maint.PruneRaceError):
        run_async(maint.prune_old_data(keep_days=30, actor="test"))

    row, transitions, artifacts = _session_and_its_history(db_path, sid)
    assert row is not None, "the reopened session lost its row"
    assert len(transitions) == 1, "the reopened session lost its transition history"
    assert len(artifacts) == 1, "the reopened session lost its artifact association"


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
            await db.execute(
                "INSERT INTO branches (id, session_id, progression_id, created_at, started_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (branch_id, sid, pid, old_ts, old_ts),
            )
        return sid, branch_id

    _, branch_id = run_async(seed())
    run_async(maint.prune_old_data(keep_days=30, actor="test"))

    async def check():
        async with StateDB(db_path) as db:
            return await db.fetch_one("SELECT id FROM branches WHERE id = ?", (branch_id,))

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
            rows = await db.fetch_all("SELECT id FROM schedule_runs")
            return {r["id"] for r in rows}

    rem = run_async(check())
    assert old_done not in rem
    assert old_running in rem
    assert recent_done in rem


async def _make_dispatch(db: StateDB, *, status: str, updated_at: float) -> str:
    from lionagi.dispatch import enqueue_dispatch

    dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
    await db.execute(
        "UPDATE dispatch_outbox SET status = ?, updated_at = ? WHERE id = ?",
        (status, updated_at, dispatch_id),
    )
    return dispatch_id


def test_prune_nullifies_dispatch_fks_before_parent_delete(tmp_path, monkeypatch):
    """A young dispatch referencing an old session/schedule_run must not abort
    the prune: its soft FKs are nullified before the parent rows are deleted."""
    from lionagi.dispatch import enqueue_dispatch
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    old_ts = time.time() - 40 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            session_id = await _make_session(db, status="completed", started_at=old_ts)
            run_id = await _make_schedule_run(db, status="completed", fired_at=old_ts)
            dispatch_id = await enqueue_dispatch(
                db,
                kind="terminal_notify",
                deliver_to="seat-1",
                session_id=session_id,
                schedule_run_id=run_id,
            )
        return session_id, run_id, dispatch_id

    session_id, run_id, dispatch_id = run_async(seed())
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))
    assert result["sessions_pruned"] == 1
    assert result["runs_pruned"] == 1

    async def check():
        async with StateDB(db_path) as db:
            return await db.fetch_one(
                "SELECT session_id, schedule_run_id FROM dispatch_outbox WHERE id = ?",
                (dispatch_id,),
            )

    row = run_async(check())
    assert row is not None  # the young dispatch survives its own retention window
    assert row["session_id"] is None
    assert row["schedule_run_id"] is None


def test_prune_purges_terminal_dispatches_by_window(tmp_path, monkeypatch):
    """ADR-0059 delta 3: delivered/acked use the success window, dead_letter/expired the longer one."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    old_success_ts = time.time() - 10 * 86400  # past the 7-day default success window
    recent_success_ts = time.time() - 1 * 86400
    old_dead_letter_ts = time.time() - 40 * 86400  # past the 30-day default dead-letter window
    recent_dead_letter_ts = time.time() - 10 * 86400  # inside the dead-letter window

    async def seed():
        async with StateDB(db_path) as db:
            delivered_old = await _make_dispatch(db, status="delivered", updated_at=old_success_ts)
            acked_recent = await _make_dispatch(db, status="acked", updated_at=recent_success_ts)
            dead_letter_old = await _make_dispatch(
                db, status="dead_letter", updated_at=old_dead_letter_ts
            )
            dead_letter_recent = await _make_dispatch(
                db, status="dead_letter", updated_at=recent_dead_letter_ts
            )
            pending_old = await _make_dispatch(db, status="pending", updated_at=old_dead_letter_ts)
        return delivered_old, acked_recent, dead_letter_old, dead_letter_recent, pending_old

    delivered_old, acked_recent, dead_letter_old, dead_letter_recent, pending_old = run_async(
        seed()
    )

    result = run_async(
        maint.prune_old_data(
            dispatch_success_keep_days=7, dispatch_dead_letter_keep_days=30, actor="test"
        )
    )
    assert result["dispatch_purged"] == 2

    async def remaining_ids():
        async with StateDB(db_path) as db:
            rows = await db.fetch_all("SELECT id FROM dispatch_outbox")
            return {r["id"] for r in rows}

    rem = run_async(remaining_ids())
    assert delivered_old not in rem
    assert dead_letter_old not in rem
    assert acked_recent in rem
    assert dead_letter_recent in rem
    # pending/delivering rows are never retention-eligible, however old.
    assert pending_old in rem


def test_prune_preserves_status_transitions_for_purged_dispatches(tmp_path, monkeypatch):
    """Unlike sessions, purged dispatch history is preserved (no FK; the compact audit trail)."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    old_ts = time.time() - 10 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            return await _make_dispatch(db, status="delivered", updated_at=old_ts)

    dispatch_id = run_async(seed())
    run_async(maint.prune_old_data(dispatch_success_keep_days=7, actor="test"))

    async def check():
        async with StateDB(db_path) as db:
            return await db.fetch_all(
                "SELECT id FROM status_transitions WHERE entity_type = 'dispatch' AND entity_id = ?",
                (dispatch_id,),
            )

    rows = run_async(check())
    assert len(rows) >= 1


def test_prune_admin_event_includes_dispatch_counts(tmp_path, monkeypatch):
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    old_ts = time.time() - 10 * 86400

    async def seed_and_prune():
        async with StateDB(db_path) as db:
            await _make_dispatch(db, status="delivered", updated_at=old_ts)
        await maint.prune_old_data(dispatch_success_keep_days=7, actor="test")
        async with StateDB(db_path) as db:
            return await db.list_admin_events(action="prune", limit=5)

    events = run_async(seed_and_prune())
    assert _details(events[0])["dispatch_purged"] >= 1


def test_prune_old_data_endpoint(tmp_path, monkeypatch):
    """POST /api/admin/prune-old-data returns pruned counts."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    run_async(_make_session_in(db_path, status="completed", started_at=time.time() - 40 * 86400))

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    r = client.post("/api/admin/prune-old-data", json={"keep_days": 30})
    assert r.status_code == 200
    data = r.json()
    assert data["sessions_pruned"] >= 1
    assert "runs_pruned" in data


# ── shared helper ─────────────────────────────────────────────────────────────


async def _make_session_in(db_path: Path, *, status: str, started_at: float) -> str:
    async with StateDB(db_path) as db:
        return await _make_session(db, status=status, started_at=started_at)
