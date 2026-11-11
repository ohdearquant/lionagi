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
import lionhub.orchestration.scheduler.settings as scheduler_settings
from lionagi.state.db import StateDB
from lionhub.orchestration.scheduler.retention_archive import ArchiveWriteError

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
    from lionhub.orchestration.scheduler import db_maintenance as maint

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


# ── checkpoint tests ──────────────────────────────────────────────────────────


# ── size alert tests ──────────────────────────────────────────────────────────


def test_stats_endpoint_exposes_checkpoint_and_size_fields(tmp_path, monkeypatch):
    """/api/stats includes last_checkpoint_at, size_alert, size_threshold_bytes."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    from lionhub.orchestration.scheduler import db_maintenance as maint

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


async def _make_dispatch(db: StateDB, *, status: str, updated_at: float) -> str:
    from lionhub.orchestration.dispatch import enqueue_dispatch

    dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
    await db.execute(
        "UPDATE dispatch_outbox SET status = ?, updated_at = ? WHERE id = ?",
        (status, updated_at, dispatch_id),
    )
    return dispatch_id


def test_prune_old_data_endpoint(tmp_path, monkeypatch):
    """POST /api/admin/prune-old-data returns pruned counts."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    from lionhub.orchestration.scheduler import db_maintenance as maint

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
    from lionhub.orchestration.scheduler import db_maintenance as maint

    monkeypatch.setattr(maint, "PRUNE_ARCHIVE_DIR", archive_dir, raising=False)
    monkeypatch.setattr(maint, "PRUNE_CHUNK_ROWS", chunk_rows, raising=False)
    # prune_old_data() imports these lazily from config each call; patch the
    # source module too (see the identical pattern in test_retention_archive.py).
    import lionhub.orchestration.scheduler.settings as cfg

    monkeypatch.setattr(scheduler_settings, "PRUNE_ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(scheduler_settings, "PRUNE_CHUNK_ROWS", chunk_rows)


# ── schedule_run / dispatch_outbox chunking + whole-plan safety ────────────


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
    from lionhub.orchestration.scheduler import db_maintenance as maint

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
