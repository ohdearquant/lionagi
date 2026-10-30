# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Admin transitions publish only committed eligible lifecycle facts."""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("fastapi")

from lionagi.state.db import StateDB
from lionagi.state.health import SessionHealth
from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS
from lionagi.state.lifecycle.deliveries import reconcile_unacknowledged, register_consumer
from tests.apps_studio_server._helpers import run_async
from tests.apps_studio_server.test_admin_transition import _seed_stale_session


def test_admin_committed_transition_is_pushed_and_replayable(tmp_path, monkeypatch):
    import lionagi.state.db as state_db_mod
    import lionagi.state.health as health_mod
    import lionagi.studio.services.admin as admin

    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    run_async(_seed_stale_session(db_path, sid))
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        health_mod, "classify_session_health", lambda *_a, **_k: SessionHealth.STALE
    )
    monkeypatch.setattr(admin, "_classify_phantom", lambda *_a, **_k: None)

    async def register():
        async with StateDB(db_path) as db:
            await register_consumer(db, "admin-test", start="all")

    run_async(register())
    events = []
    name = "admin-" + uuid.uuid4().hex
    DEFAULT_TERMINAL_CALLBACKS.register(name, lambda event: events.append(event))
    try:
        result = run_async(
            admin.transition_sessions(
                session_ids=[sid],
                target_status="failed",
                reason_code="run.failed.exception",
                actor="test",
            )
        )
    finally:
        DEFAULT_TERMINAL_CALLBACKS.unregister(name)
    assert result["transitioned"] == [sid]
    assert len(events) == 1

    async def snapshot():
        async with StateDB(db_path) as db:
            rows = await db.fetch_all(
                "SELECT id, previous_status, status, delivery_eligible FROM status_transitions "
                "WHERE entity_id = ? AND source = ?",
                (sid, "admin"),
            )
            return rows, await reconcile_unacknowledged(db, "admin-test")

    rows, pending = run_async(snapshot())
    assert len(rows) == 1 and rows[0]["delivery_eligible"]
    assert rows[0]["previous_status"] == "running" and rows[0]["status"] == "failed"
    assert events[0].event_id == rows[0]["id"]
    assert events[0].entity.id == sid
    assert rows[0]["id"] in {row["transition_id"] for row in pending}
