# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The prune remains correct across batches and reuse of one SQLite connection."""

from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import event

from lionagi.cli import state as state_mod
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB


@pytest.mark.asyncio
@pytest.mark.parametrize("first_is_preview", [True, False], ids=["preview-then-real", "real-twice"])
async def test_connection_reuse_keeps_shared_progression_across_victim_batches(
    tmp_path, monkeypatch, first_is_preview
):
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(state_mod, "_PRUNE_VICTIM_INSERT_BATCH_SIZE", 2)

    async with StateDB(path) as db:
        progression, message = str(uuid.uuid4()), str(uuid.uuid4())
        await db.create_progression(progression)
        await db.insert_message(
            {
                "id": message,
                "created_at": time.time(),
                "node_metadata": {},
                "content": {"text": "kept by the survivor"},
                "role": "user",
                "sender": "u",
                "recipient": "x",
                "channel": "test",
            }
        )
        await db.append_to_progression(progression, message)
        victims = [str(uuid.UUID(int=i)) for i in range(1, 5)]
        survivor = str(uuid.UUID(int=5))
        for sid in [*victims, survivor]:
            await db.create_session(
                {"id": sid, "progression_id": progression, "status": "completed"}
            )
            age = 60 if sid in victims else 0
            await db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (time.time() - age * 86400, sid),
            )

        # _prune constructs a StateDB itself. Borrow this already-open handle
        # without closing it so each call returns to the same SQLite pool.
        class BorrowedDB:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *_exc):
                return None

        monkeypatch.setattr(db_mod, "StateDB", BorrowedDB)
        executions = []

        def record(_connection, _cursor, statement, parameters, _context, executemany):
            if "prune_victims" not in statement and "SELECT id FROM sessions" not in statement:
                return
            driver = _connection.connection.driver_connection
            executions.append((id(driver), statement, parameters, executemany))

        event.listen(db._engine.sync_engine, "before_cursor_execute", record)
        try:
            calls = []
            for dry_run in (first_is_preview, False):
                executions.clear()
                counts = await state_mod._prune(keep_days=30, keep_n=0, dry_run=dry_run)
                calls.append((counts, list(executions)))
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", record)

        first, second = calls
        assert first[0] == {"sessions": 4, "branches": 0, "messages": 0}
        assert second[0] == {
            "sessions": 4 if first_is_preview else 0,
            "branches": 0,
            "messages": 0,
        }
        # Both calls reached the same underlying aiosqlite connection, not just
        # two connections to the same file. The second real call still selects.
        for _counts, seen in calls:
            assert seen
        assert len({connection for _counts, seen in calls for connection, *_ in seen}) == 1

        for _counts, seen in calls[: 2 if first_is_preview else 1]:
            batches = [
                parameters
                for _connection, statement, parameters, executemany in seen
                if "INSERT INTO prune_victims" in statement and executemany
            ]
            assert len(batches) == 2
            assert all(len(batch) == 2 for batch in batches)
            batch_ids = [{str(row[0]) for row in batch} for batch in batches]
            assert set.union(*batch_ids) == set(victims)
            # Every victim points to the same progression, so the two observed
            # disjoint batches each contain a sharer; the survivor shares it too.
            assert batch_ids[0].isdisjoint(batch_ids[1])

        assert await db.get_session(survivor) is not None
        for sid in victims:
            assert await db.get_session(sid) is None
        row = await db.fetch_one("SELECT collection FROM progressions WHERE id = ?", (progression,))
        assert row is not None and message in json.loads(row["collection"])
        assert await db.fetch_one("SELECT id FROM messages WHERE id = ?", (message,))
