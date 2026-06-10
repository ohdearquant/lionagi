# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for #1056 — admin transition atomicity guard.

The transition_sessions() UPDATE WHERE must include timestamp snapshot conditions
so a concurrent heartbeat between classify and UPDATE causes rowcount==0 (lost
race → session goes to skipped, not transitioned).
"""

from __future__ import annotations

import time
import uuid

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from tests.apps_studio_server._helpers import run_async as _run  # noqa: E402


async def _seed_stale_session(
    db_path,
    session_id: str,
    last_message_at: float | None = None,
    updated_at: float | None = None,
) -> None:
    """Seed a running session that classify_session_health will mark STALE/ORPHANED."""
    from lionagi.state.db import StateDB

    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        old_time = last_message_at or (time.time() - 7 * 3600)  # 7h old → stale
        await db.create_session(
            {
                "id": session_id,
                "progression_id": pid,
                "name": "stale-session",
                "status": "running",
                "started_at": old_time,
            }
        )
        # Set last_message_at and updated_at explicitly
        _up = updated_at or old_time
        await db.db.execute(
            "UPDATE sessions SET last_message_at=?, updated_at=? WHERE id=?",
            (old_time, _up, session_id),
        )
        await db.db.commit()


def _make_admin_client(tmp_path, monkeypatch, db_path):
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.sessions as sessions_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))

    from fastapi.testclient import TestClient

    from lionagi.studio.app import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Test: WHERE clause snapshot guard — simulated heartbeat between classify/UPDATE
# ---------------------------------------------------------------------------


def test_transition_refused_when_heartbeat_changes_health(tmp_path, monkeypatch):
    """If last_message_at changes between classify and UPDATE, rowcount==0 → skipped.

    We simulate this by:
    1. Seeding a stale session with old timestamps.
    2. Monkeypatching classify_session_health with a SYNCHRONOUS fake that bumps
       last_message_at in the DB via a separate connection BEFORE returning
       SessionHealth.STALE.  The sync constraint is load-bearing: transition_sessions()
       calls classify_session_health() synchronously (line 338 admin.py); an async
       fake would return an un-awaited coroutine — the bump would never fire and the
       health guard would be skipped entirely.
    3. Asserting the session ends up in skipped with reason='changed_since_snapshot'
       (not transitioned), because last_message_at changed between snapshot and UPDATE.
    """
    from lionagi.state.health import SessionHealth

    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    old_ts = time.time() - 7 * 3600  # 7 hours ago

    _run(_seed_stale_session(db_path, sid, last_message_at=old_ts, updated_at=old_ts))

    # MAJOR 2 fix: patch BOTH admin.DEFAULT_DB_PATH and lionagi.state.db.DEFAULT_DB_PATH.
    # transition_sessions() opens StateDB() which resolves lionagi.state.db.DEFAULT_DB_PATH
    # at call time — patching only admin_mod.DEFAULT_DB_PATH leaves StateDB() pointing at
    # the real default DB (a readonly path in CI).
    import lionagi.state.db as state_db_mod
    import lionagi.state.health as health_mod
    import lionagi.studio.services.admin as adm

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(adm, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(adm, "_DB", str(db_path))

    # MAJOR 1 fix: synchronous fake classifier.  The fake bumps last_message_at
    # via a direct sqlite3 (synchronous) connection — no second event loop needed.
    # aiosqlite is just a thread-executor wrapper around sqlite3; we can open
    # sqlite3 directly here because the write happens before the outer coroutine's
    # UPDATE, and SQLite serialises concurrent writers automatically.
    import sqlite3

    def _sync_bump_and_classify(session, **kwargs):
        """Bump last_message_at synchronously, then return STALE (simulates the race)."""
        new_ts = time.time()  # new timestamp that doesn't match snapshot
        con = sqlite3.connect(str(db_path))
        try:
            con.execute(
                "UPDATE sessions SET last_message_at=? WHERE id=?",
                (new_ts, session["id"]),
            )
            con.commit()
        finally:
            con.close()
        return SessionHealth.STALE

    monkeypatch.setattr(health_mod, "classify_session_health", _sync_bump_and_classify)

    result = _run(
        adm.transition_sessions(
            session_ids=[sid],
            target_status="failed",
            reason_code="run.failed.exception",
            reason_summary="test atomicity guard",
            actor="test",
        )
    )

    # The session should be in skipped, NOT transitioned, because last_message_at
    # changed between snapshot and UPDATE (WHERE clause fails → rowcount==0).
    assert sid not in result["transitioned"], (
        "Session should not be transitioned when heartbeat changed last_message_at"
    )
    skipped_ids = [s["session_id"] for s in result["skipped"]]
    assert sid in skipped_ids, (
        f"Session {sid} should be in skipped after heartbeat race; got {result}"
    )

    # MEDIUM 1 fix: verify the reason is 'changed_since_snapshot' (not the
    # misleading 'not_running:running' that the old code emitted when status
    # was still 'running' but timestamps had changed).
    skipped_entry = next(s for s in result["skipped"] if s["session_id"] == sid)
    assert skipped_entry["reason"] == "changed_since_snapshot", (
        f"Expected reason='changed_since_snapshot', got {skipped_entry['reason']!r}. "
        "The atomicity guard should report the heartbeat-race reason, not 'not_running:running'."
    )
