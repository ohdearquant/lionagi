# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for state.db lifecycle (checkpoint, size alert, prune old data)."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB
from lionagi.studio.services.retention_archive import ArchiveWriteError

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


def test_checkpoint_result_shape_is_the_same_with_and_without_a_store(tmp_path, monkeypatch):
    """Both return paths report the same fields.

    The no-store path builds its dict by hand instead of running the PRAGMA,
    so it is the one that silently falls behind when a field is added. It
    reaches the admin maintenance API unchanged, and a response that drops a
    field depending on whether the store happens to exist is a shape a caller
    cannot rely on.
    """
    from lionagi.studio.services import db_maintenance as maint

    _patch_db(monkeypatch, tmp_path / "absent.db")
    absent = run_async(maint.checkpoint_state_db())

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    run_async(_make_session_in(db_path, status="running", started_at=time.time()))
    present = run_async(maint.checkpoint_state_db(actor="test"))

    assert sorted(absent) == sorted(present)


def test_checkpoint_event_records_the_wal_size_and_the_time_it_took(tmp_path, monkeypatch):
    """The stored row carries what the PRAGMA counters cannot say.

    A TRUNCATE checkpoint that succeeds reports busy, log_pages and
    checkpointed all zero however much it drained, so those three cannot
    separate a long stall from an idle tick. Asserted on the admin_events row
    rather than on the return value because the row is what an operator reads
    afterwards; a field that was returned but never stored would satisfy a
    return-value check and still leave the record useless.
    """
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    run_async(_make_session_in(db_path, status="running", started_at=time.time()))

    async def _grow_a_wal_then_checkpoint_it():
        """Hold a connection open so the WAL is still there when we look.

        SQLite checkpoints and removes the WAL when the last connection
        closes, so a fixture that writes and then lets go leaves nothing
        behind to drain and the size going in is zero however much was
        written. Keeping this connection open is what makes the number under
        test a real one.
        """
        async with StateDB() as db:
            for i in range(200):
                await db.insert_admin_event(
                    action="grow", details={"i": i, "pad": "x" * 400}, actor="fixture"
                )
            standing = db_path.with_name(db_path.name + "-wal").stat().st_size
            await maint.checkpoint_state_db(actor="test")
            events = await db.list_admin_events(action="checkpoint", limit=5)
        return standing, _details(events[0])

    standing_wal, details = run_async(_grow_a_wal_then_checkpoint_it())

    # The fixture is what makes this an assertion rather than a formality:
    # if the WAL were empty the recorded size would be zero either way, and
    # a checkpoint that never read it would pass.
    assert standing_wal > 0
    assert details["wal_bytes_before"] == standing_wal

    # Opening a connection and running a PRAGMA is never free, so a zero here
    # would mean the clock was never read rather than that the work was fast.
    assert details["elapsed_ms"] > 0


def test_wal_size_is_read_from_the_sidecar_that_is_there(tmp_path, monkeypatch):
    """Three answers, and the difference between the two that look alike.

    Zero and None both read as "no bytes to drain" at a glance and mean
    different things: zero is a store whose WAL is genuinely empty, None is a
    store where the question does not apply. An operator seeing None knows to
    stop asking about the WAL; one seeing zero knows the checkpoint had
    nothing to do.
    """
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    # Nothing beside the store yet.
    assert maint._wal_bytes_now() == 0

    # The size comes off the file, so the expected value is the number of
    # bytes actually written rather than anything this test decided.
    payload = b"x" * 4096
    db_path.with_name(db_path.name + "-wal").write_bytes(payload)
    assert maint._wal_bytes_now() == len(payload)

    # A store with no local file at all.
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(
            update={"LIONAGI_STATE_DB_URL": "postgresql+asyncpg://u:p@127.0.0.1:5432/lionagi"}
        ),
    )
    assert maint._wal_bytes_now() is None


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


def test_the_size_threshold_tracks_the_retention_window():
    """Doubling the retention window doubles the size the store may reach, so the two cannot disagree."""
    import lionagi.studio.config as cfg

    thirty = cfg._derive_db_size_alert_bytes(30)
    sixty = cfg._derive_db_size_alert_bytes(60)

    assert sixty == 2 * thirty
    assert thirty > cfg._DB_SIZE_ALERT_FLOOR_BYTES


def test_the_configured_threshold_is_the_one_derived_from_the_configured_window(
    monkeypatch,
):
    """The module constant is the derivation, not a value that merely resembles it."""
    import os

    import lionagi.studio.config as cfg

    if os.environ.get("LIONAGI_STUDIO_DB_SIZE_ALERT_BYTES"):
        pytest.skip("explicit threshold override in env; derivation deliberately bypassed")

    assert cfg.DB_SIZE_ALERT_BYTES == cfg._derive_db_size_alert_bytes(cfg.PRUNE_KEEP_DAYS)


def test_a_store_within_its_retention_steady_state_does_not_alert(monkeypatch):
    """A store sitting at the steady state its own policy produces must stay quiet."""
    import lionagi.studio.config as cfg
    from lionagi.studio.services import db_maintenance as maint

    keep_days = 30
    steady_state = keep_days * cfg._DB_BYTES_PER_RETAINED_DAY
    monkeypatch.setattr(cfg, "DB_SIZE_ALERT_BYTES", cfg._derive_db_size_alert_bytes(keep_days))

    at_steady_state, _ = maint.get_db_size_alert(steady_state)
    assert at_steady_state is False

    # Just inside the headroom is still explained by the policy.
    inside_headroom, _ = maint.get_db_size_alert(int(steady_state * 1.4))
    assert inside_headroom is False

    # Past it, the store is bigger than the policy accounts for.
    beyond_headroom, _ = maint.get_db_size_alert(int(steady_state * 1.6))
    assert beyond_headroom is True


def test_a_zero_retention_window_cannot_derive_a_threshold_that_alerts_always():
    """Without the floor a keep window of 0 derives a threshold of 0 and alerts unconditionally."""
    import lionagi.studio.config as cfg

    assert cfg._derive_db_size_alert_bytes(0) == cfg._DB_SIZE_ALERT_FLOOR_BYTES
    assert cfg._derive_db_size_alert_bytes(0) > 0


def test_the_per_day_measurement_can_be_recalibrated_without_a_release(monkeypatch):
    """The one empirical input must be settable, or a differing deployment can only override the threshold outright."""
    import importlib

    import lionagi.studio.config as cfg

    baseline = cfg._DB_BYTES_PER_RETAINED_DAY
    assert baseline > 0, "no baseline measurement to recalibrate from"

    monkeypatch.setenv("LIONAGI_STUDIO_DB_BYTES_PER_RETAINED_DAY", str(baseline * 2))
    try:
        importlib.reload(cfg)

        assert cfg._DB_BYTES_PER_RETAINED_DAY == baseline * 2
        # The recalibration has to reach the threshold, not just the constant.
        assert cfg._derive_db_size_alert_bytes(30) == int(
            30 * baseline * 2 * cfg._DB_SIZE_ALERT_HEADROOM
        )
    finally:
        monkeypatch.delenv("LIONAGI_STUDIO_DB_BYTES_PER_RETAINED_DAY", raising=False)
        importlib.reload(cfg)

    # Restoring matters as much as the override: a module left reloaded with a
    # doubled constant would quietly change what every later test measures.
    assert cfg._DB_BYTES_PER_RETAINED_DAY == baseline


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
    deletes the session; a per-statement condition protects each statement
    on its own but still lets the sequence land half-applied -- the earlier
    writes are already done when the reopen arrives, and the delete then
    skips the row, leaving a live session whose history and links are gone.
    The lock is what makes this unreachable; the parametrization drives the
    reopen past it at each step, and every case asserts the session
    survives whole.

    The reopen here rides the prune's own transaction, so it rolls back
    along with everything else and the status reads terminal again
    afterwards (a real resume commits on its own and would keep its
    status). What this checks is the part that's the same either way: none
    of the prune's writes reached the database.
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


def test_prune_admin_event_includes_archive_ids(tmp_path, monkeypatch):
    """The prune admin event must name which archives it wrote, per root kind,
    so an operator can locate the receipt for what got deleted without
    grepping the archive directory."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    _patch_db(monkeypatch, db_path)
    _patch_prune_config(monkeypatch, archive_dir=archive_dir, chunk_rows=100)

    old_ts = time.time() - 40 * 86400
    run_async(_make_session_in(db_path, status="completed", started_at=old_ts))
    run_async(maint.prune_old_data(keep_days=30, actor="test"))

    async def check():
        async with StateDB(db_path) as db:
            return await db.list_admin_events(action="prune", limit=5)

    events = run_async(check())
    details = _details(events[0])
    archive_ids = details["archive_ids"]
    assert len(archive_ids["sessions"]) == 1
    assert archive_ids["runs"] == []
    assert archive_ids["dispatch"] == []
    assert (archive_dir / f"{archive_ids['sessions'][0]}.zip").exists()


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


def _patch_prune_config(monkeypatch, *, archive_dir: Path | None, chunk_rows: int) -> None:
    from lionagi.studio.services import db_maintenance as maint

    monkeypatch.setattr(maint, "PRUNE_ARCHIVE_DIR", archive_dir, raising=False)
    monkeypatch.setattr(maint, "PRUNE_CHUNK_ROWS", chunk_rows, raising=False)
    # prune_old_data() imports these lazily from config each call; patch the
    # source module too (see the identical pattern in test_retention_archive.py).
    import lionagi.studio.config as cfg

    monkeypatch.setattr(cfg, "PRUNE_ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(cfg, "PRUNE_CHUNK_ROWS", chunk_rows)


# ── schedule_run / dispatch_outbox chunking + whole-plan safety ────────────


def test_schedule_run_retention_is_chunked_and_archived(tmp_path, monkeypatch):
    """schedule_run deletion is bounded per PRUNE_CHUNK_ROWS, each chunk archived first."""
    from lionagi.studio.services import db_maintenance as maint
    from lionagi.studio.services.retention_archive import read_archive_chunk

    db_path = tmp_path / "state.db"
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    _patch_db(monkeypatch, db_path)
    _patch_prune_config(monkeypatch, archive_dir=archive_dir, chunk_rows=2)

    old_ts = time.time() - 40 * 86400

    seen_chunks: list[list[str]] = []
    original = maint._prune_run_chunk

    async def spy(conn, run_ids, **kwargs):
        seen_chunks.append(sorted(run_ids))
        return await original(conn, run_ids, **kwargs)

    monkeypatch.setattr(maint, "_prune_run_chunk", spy)

    async def seed():
        async with StateDB(db_path) as db:
            return [
                await _make_schedule_run(db, status="completed", fired_at=old_ts) for _ in range(5)
            ]

    ids = run_async(seed())
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))

    assert result["runs_pruned"] == 5
    assert [len(c) for c in seen_chunks] == [2, 2, 1]
    flat = [i for c in seen_chunks for i in c]
    assert sorted(flat) == sorted(ids)
    assert len(flat) == len(set(flat))

    run_archives = [p for p in archive_dir.glob("run-*.zip")]
    assert len(run_archives) == 3  # one per committed chunk
    archived_ids: set[str] = set()
    for path in run_archives:
        decoded = read_archive_chunk(path)
        archived_ids.update(row["id"] for row in decoded["tables"]["schedule_runs"])
    assert archived_ids == set(ids)


def test_session_prune_archives_preimages_of_nullified_soft_fk_rows(tmp_path, monkeypatch):
    """Rows whose session_id a prune nullifies get their pre-nullify state captured, or a restore orphans them permanently."""
    from lionagi.dispatch import enqueue_dispatch
    from lionagi.studio.services import db_maintenance as maint
    from lionagi.studio.services.retention_archive import read_archive_chunk

    db_path = tmp_path / "state.db"
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    _patch_db(monkeypatch, db_path)
    _patch_prune_config(monkeypatch, archive_dir=archive_dir, chunk_rows=100)

    old_ts = time.time() - 40 * 86400
    recent_ts = time.time() - 1 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            sid = await _make_session(db, status="completed", started_at=old_ts)
            artifact_id = await db.insert_artifact(
                kind="note", name="n1", content={"x": 1}, session_id=sid
            )
            dispatch_id = await enqueue_dispatch(
                db, kind="terminal_notify", deliver_to="seat-1", session_id=sid
            )
            # young dispatch: survives its own retention window, but its
            # session_id soft-FK still gets nullified when the session dies.
            await db.execute(
                "UPDATE dispatch_outbox SET updated_at = ? WHERE id = ?",
                (recent_ts, dispatch_id),
            )
        return sid, artifact_id, dispatch_id

    sid, artifact_id, dispatch_id = run_async(seed())
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))
    assert result["sessions_pruned"] == 1

    session_archives = list(archive_dir.glob("prune-*.zip"))
    assert len(session_archives) == 1
    decoded = read_archive_chunk(session_archives[0])

    archived_artifact_preimages = {r["id"]: r for r in decoded["preimages"]["artifacts"]}
    assert archived_artifact_preimages[artifact_id]["session_id"] == sid

    archived_dispatch_preimages = {r["id"]: r for r in decoded["preimages"]["dispatch_outbox"]}
    assert archived_dispatch_preimages[dispatch_id]["session_id"] == sid

    async def check():
        async with StateDB(db_path) as db:
            dispatch_row = await db.fetch_one(
                "SELECT session_id FROM dispatch_outbox WHERE id = ?", (dispatch_id,)
            )
            artifact_row = await db.fetch_one(
                "SELECT session_id FROM artifacts WHERE id = ?", (artifact_id,)
            )
            return dispatch_row, artifact_row

    dispatch_row, artifact_row = run_async(check())
    # Confirm the actual nullify happened -- the preimage records what these
    # rows looked like *before* this, not instead of it.
    assert dispatch_row["session_id"] is None
    assert artifact_row["session_id"] is None


def test_dispatch_retention_is_chunked_and_archived(tmp_path, monkeypatch):
    """dispatch_outbox deletion is bounded per PRUNE_CHUNK_ROWS, each chunk archived first."""
    from lionagi.studio.services import db_maintenance as maint
    from lionagi.studio.services.retention_archive import read_archive_chunk

    db_path = tmp_path / "state.db"
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    _patch_db(monkeypatch, db_path)
    _patch_prune_config(monkeypatch, archive_dir=archive_dir, chunk_rows=2)

    old_ts = time.time() - 10 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            return [
                await _make_dispatch(db, status="delivered", updated_at=old_ts) for _ in range(5)
            ]

    ids = run_async(seed())
    result = run_async(maint.prune_old_data(dispatch_success_keep_days=7, actor="test"))

    assert result["dispatch_purged"] == 5

    dispatch_archives = list(archive_dir.glob("dispatch-*.zip"))
    assert len(dispatch_archives) == 3  # ceil(5/2)
    archived_ids: set[str] = set()
    for path in dispatch_archives:
        decoded = read_archive_chunk(path)
        archived_ids.update(row["id"] for row in decoded["tables"]["dispatch_outbox"])
    assert archived_ids == set(ids)

    async def remaining():
        async with StateDB(db_path) as db:
            rows = await db.fetch_all("SELECT id FROM dispatch_outbox")
            return {r["id"] for r in rows}

    assert run_async(remaining()) == set()


def test_run_chunk_archive_failure_aborts_dispatch_and_keeps_session_deletes(tmp_path, monkeypatch):
    """Mid-plan archive failure: sessions already chunked+deleted stay deleted,
    the failing run chunk is refused, and dispatch retention (later in the
    plan) is never attempted."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    _patch_db(monkeypatch, db_path)
    _patch_prune_config(monkeypatch, archive_dir=archive_dir, chunk_rows=100)

    old_ts = time.time() - 40 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            sid = await _make_session(db, status="completed", started_at=old_ts)
            run_id = await _make_schedule_run(db, status="completed", fired_at=old_ts)
            dispatch_id = await _make_dispatch(db, status="delivered", updated_at=old_ts)
        return sid, run_id, dispatch_id

    sid, run_id, dispatch_id = run_async(seed())

    original_write = maint.write_archive_chunk

    def flaky_write(destination, archive_id, tables, preimages=None):
        if "schedule_runs" in tables:
            raise ArchiveWriteError("simulated run-chunk archive failure")
        return original_write(destination, archive_id, tables, preimages=preimages)

    monkeypatch.setattr(maint, "write_archive_chunk", flaky_write)

    with pytest.raises(ArchiveWriteError):
        run_async(maint.prune_old_data(keep_days=30, dispatch_success_keep_days=7, actor="test"))

    async def check():
        async with StateDB(db_path) as db:
            session_row = await db.get_session(sid)
            run_row = await db.fetch_one("SELECT id FROM schedule_runs WHERE id = ?", (run_id,))
            dispatch_row = await db.fetch_one(
                "SELECT id FROM dispatch_outbox WHERE id = ?", (dispatch_id,)
            )
        return session_row, run_row, dispatch_row

    session_row, run_row, dispatch_row = run_async(check())
    # Session chunk committed before the run chunk ever started: stays deleted.
    assert session_row is None
    # Run chunk's archive failed: its delete never ran, row survives.
    assert run_row is not None
    # Dispatch retention runs after schedule_run retention in the plan and
    # was never reached.
    assert dispatch_row is not None

    # No run/dispatch archives exist; the one session archive that succeeded does.
    assert list(archive_dir.glob("run-*.zip")) == []
    assert list(archive_dir.glob("dispatch-*.zip")) == []
    assert len(list(archive_dir.glob("prune-*.zip"))) == 1


# ── candidate paging keeps a forward seek ─────────────────────────────────────


def _captured_chunk_sql(
    tmp_path, monkeypatch, dialect: str | None = None
) -> tuple[Path, str, tuple]:
    """Drive `_candidate_chunks` and return the SQL it actually built.

    The statement is taken from the running code rather than restated here, so
    the plan assertions below fail if the source stops asking for the index.

    `dialect` overrides what the scan believes it is talking to. The store
    underneath stays SQLite either way, which is what makes the Postgres case
    observable at all: the point is which SQL gets built, and a statement built
    without the hint still runs here.
    """
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    seen: list[tuple[str, tuple]] = []
    real_q = maint._q

    def capture(sql, params):
        seen.append((sql, tuple(params)))
        return real_q(sql, params)

    monkeypatch.setattr(maint, "_q", capture)

    old = time.time() - 90 * 86400

    async def drive() -> None:
        async with StateDB() as db:
            for _ in range(3):
                await _make_session(db, status="completed", started_at=old)
            if dialect is not None:
                assert db.dialect != dialect, (
                    f"the override is a no-op: the store already reports {dialect}"
                )
                db.dialect = dialect
            where_sql, params = maint._session_retention_predicate(time.time() - 30 * 86400)
            async for _chunk in maint._candidate_chunks(
                db, table="sessions", where_sql=where_sql, params=params, size=2
            ):
                pass

    run_async(drive())

    selects = [(s, p) for s, p in seen if s.lstrip().upper().startswith("SELECT ID FROM SESSIONS")]
    assert selects, f"no candidate SELECT captured; saw {[s[:60] for s, _ in seen]}"
    sql, params = selects[0]
    return db_path, sql, params


def test_the_candidate_paging_query_seeks_rather_than_sorting(tmp_path, monkeypatch):
    """Each page must be a forward seek on the primary key, not a re-sort.

    Left to the planner with no collected statistics, SQLite prefers the
    status/time index and adds a temporary sort for ORDER BY id, which makes
    every page re-read and re-sort the whole remaining backlog. That is
    invisible to a correctness test -- the ids come out identical either way --
    so the plan itself is what has to be asserted.
    """
    db_path, sql, params = _captured_chunk_sql(tmp_path, monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        plan = [row[-1] for row in con.execute("EXPLAIN QUERY PLAN " + sql, params)]
    finally:
        con.close()

    rendered = " | ".join(plan)
    assert "TEMP B-TREE" not in rendered.upper(), (
        f"the paging query sorts instead of seeking, which is the quadratic plan: {rendered}"
    )
    assert "sqlite_autoindex_sessions_1" in rendered, (
        f"the paging query is not walking the primary key: {rendered}"
    )


def test_the_paging_query_does_not_carry_sqlite_syntax_to_postgres(tmp_path, monkeypatch):
    """`INDEXED BY` is SQLite-only, and the prune also runs on Postgres.

    Nothing gates `prune_old_data` by dialect: the scheduler tick and the admin
    route both call it against whatever `StateDB` resolves to. PostgreSQL has
    no `INDEXED BY` clause, so emitting one unconditionally would fail every
    pass at prepare time rather than merely planning it badly. It also does not
    need the hint, keeping its own statistics.

    The failure this guards is invisible to the SQLite suite by construction,
    which is why it is asserted on the built statement rather than on a result.
    """
    _, sql, _ = _captured_chunk_sql(tmp_path, monkeypatch, dialect="postgresql")

    assert "INDEXED BY" not in sql.upper(), (
        f"the paging query carries SQLite-only syntax to Postgres: {sql}"
    )
    assert "sqlite_autoindex" not in sql, f"SQLite index name leaked into a Postgres query: {sql}"
    # Still the same scan, not a differently-shaped one.
    assert "ORDER BY id" in sql and "id > ?" in sql, (
        f"the Postgres form is not a forward seek: {sql}"
    )


def test_every_paged_table_has_the_primary_key_index_the_query_names(tmp_path, monkeypatch):
    """The paging query names `sqlite_autoindex_<table>_1` for three tables.

    That name exists because each table declares `id TEXT PRIMARY KEY`. If a
    schema change removes or renames it the prune fails at prepare time, so
    this pins the assumption where it is cheap to read rather than leaving it
    to be discovered during a retention pass.
    """
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    async def touch() -> None:
        async with StateDB() as db:
            await _make_session(db, status="completed", started_at=time.time())

    run_async(touch())

    con = sqlite3.connect(db_path)
    try:
        for table in ("sessions", "schedule_runs", "dispatch_outbox"):
            name = f"sqlite_autoindex_{table}_1"
            indexes = {row[1] for row in con.execute(f"PRAGMA index_list({table})")}
            assert name in indexes, f"{table} has no {name}; found {sorted(indexes)}"
            columns = [row[2] for row in con.execute(f"PRAGMA index_info({name})")]
            assert columns == ["id"], f"{name} covers {columns}, not the id the query seeks on"
    finally:
        con.close()
