# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Compatibility push uses the shape observed when its transition was written."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
from tests.state.test_terminal_consumer_compatibility import _foreign_store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initially_present", [False, True], ids=["add-after-write", "drop-after-write"]
)
async def test_foreign_schema_change_between_commit_and_push(
    tmp_path, monkeypatch, initially_present
):
    path = tmp_path / "foreign.db"
    profile = await _foreign_store(path, has_column=initially_present)
    callbacks = TerminalCallbackRegistry()
    received = []
    callbacks.register("collector", received.append)
    async with StateDB(path, compatibility=profile, owner="principal:a") as db:
        service = db._lifecycle_service()
        service._terminal_callbacks = callbacks
        await db.create_progression("progression")
        await db.create_session(
            {"id": "session", "progression_id": "progression", "status": "running"}
        )
        original = service._emit_terminal_event
        captured = []

        async def change_schema_after_commit(command, **kwargs):
            captured.append(kwargs)
            # The external manager changes its schema only after the writer's
            # transaction is committed and before its post-commit push.
            with closing(sqlite3.connect(path)) as conn:
                assert conn.execute(
                    "SELECT status FROM sessions WHERE id = 'session'"
                ).fetchone() == ("completed",)
                if initially_present:
                    conn.execute("ALTER TABLE status_transitions DROP COLUMN delivery_eligible")
                else:
                    conn.execute(
                        "ALTER TABLE status_transitions ADD COLUMN delivery_eligible BOOLEAN"
                    )
                conn.commit()
            await original(command, **kwargs)

        monkeypatch.setattr(service, "_emit_terminal_event", change_schema_after_commit)
        assert await db.update_status(
            "session", "session", new_status="completed", reason_code="run.completed.ok"
        )
        assert len(captured) == 1
        assert captured[0]["eligibility_stored"] is initially_present
        assert captured[0]["eligibility_at_write"] is True
        if initially_present:
            # A stored decision cannot be replaced by an in-memory fallback if
            # the foreign manager removes its column before the read.
            assert received == []
        else:
            # This row was written without the column; the newly added column
            # reads NULL and must not override its write-time fallback.
            assert len(received) == 1
            assert received[0].event_id == captured[0]["transition_id"]
            with closing(sqlite3.connect(path)) as conn:
                row = conn.execute(
                    "SELECT delivery_eligible FROM status_transitions WHERE id = ?",
                    (received[0].event_id,),
                ).fetchone()
            assert row == (None,)
