# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for #1056 — admin transition atomicity guard.

The transition_sessions() UPDATE WHERE must include timestamp snapshot conditions
so a concurrent heartbeat between classify and UPDATE causes rowcount==0 (lost
race → session goes to skipped, not transitioned).
"""
from __future__ import annotations

import asyncio
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
        await db.create_session({
            "id": session_id,
            "progression_id": pid,
            "name": "stale-session",
            "status": "running",
            "started_at": old_time,
        })
        # Set last_message_at and updated_at explicitly
        _up = updated_at or old_time
        await db.db.execute(
            "UPDATE sessions SET last_message_at=?, updated_at=? WHERE id=?",
            (old_time, _up, session_id),
        )
        await db.db.commit()


def _make_admin_client(tmp_path, monkeypatch, db_path):
    import apps.studio.server.services.admin as admin_mod
    import apps.studio.server.services.sessions as sessions_mod
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))

    from apps.studio.server.app import app
    from fastapi.testclient import TestClient
    return TestClient(app)


# ---------------------------------------------------------------------------
# Test: WHERE clause snapshot guard — simulated heartbeat between classify/UPDATE
# ---------------------------------------------------------------------------


def test_transition_refused_when_heartbeat_changes_health(tmp_path, monkeypatch):
    """If last_message_at changes between classify and UPDATE, rowcount==0 → skipped.

    We simulate this by:
    1. Seeding a stale session with old timestamps.
    2. Monkeypatching classify_session_health to pretend the session is STALE
       but also bumping last_message_at in the DB BEFORE the UPDATE fires.
    3. Asserting the session ends up in skipped (not transitioned).
    """
    from lionagi.state.health import SessionHealth

    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    old_ts = time.time() - 7 * 3600  # 7 hours ago

    _run(_seed_stale_session(db_path, sid, last_message_at=old_ts, updated_at=old_ts))

    # Monkeypatch classify_session_health to always return STALE for running sessions.
    import apps.studio.server.services.admin as admin_mod

    original_classify = None

    async def _bump_and_classify(session, **kwargs):
        """Bump last_message_at first, then return STALE (simulates the race)."""
        from lionagi.state.db import StateDB
        new_ts = time.time()  # new timestamp that doesn't match snapshot
        async with StateDB(db_path) as db:
            await db.db.execute(
                "UPDATE sessions SET last_message_at=? WHERE id=?",
                (new_ts, session["id"]),
            )
            await db.db.commit()
        return SessionHealth.STALE

    # Wrap classify to intercept it inside transition_sessions.
    import lionagi.state.health as health_mod
    monkeypatch.setattr(health_mod, "classify_session_health", _bump_and_classify)

    import apps.studio.server.services.admin as adm

    monkeypatch.setattr(adm, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(adm, "_DB", str(db_path))

    # transition_sessions is a sync function that drives an async coro internally.
    result = _run(
        adm.transition_sessions(
            session_ids=[sid],
            target_status="failed",
            reason="test atomicity guard",
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
