# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Prune handles a selected population larger than a connection's SQL bind limit."""

import json
import sqlite3
import time
from contextlib import asynccontextmanager

import pytest

from lionagi.cli.state import _prune
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from tests.cli.test_state_commands import (
    _seed_prune_fixture_with_shared_and_pinned_messages,
    _seed_session,
)

pytestmark = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "setlimit"), reason="SQLite setlimit requires Python 3.11+"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "delete"])
async def test_prune_spans_sqlite_bind_limit_and_preserves_shared_data(
    tmp_path, monkeypatch, dry_run
):
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    old_at = time.time() - 60 * 86400
    async with StateDB(path) as db:
        victim, keeper, _ = await _seed_prune_fixture_with_shared_and_pinned_messages(db)
        victims = [victim]
        for _ in range(11):
            victims.append(await _seed_session(db, status="completed", updated_at=old_at))
        keeper_row = await db.get_session(keeper)
        shared_progression = keeper_row["progression_id"]
        # An additional victim and the surviving session reference the same progression.
        await db.execute(
            "UPDATE sessions SET progression_id = ? WHERE id = ?",
            (shared_progression, victims[1]),
        )
        keeper_branch = await db.fetch_one(
            "SELECT progression_id FROM branches WHERE session_id = ?", (keeper,)
        )
        keeper_branch_progression = keeper_branch["progression_id"]
        shared_row = await db.fetch_one(
            "SELECT collection FROM progressions WHERE id = ?", (keeper_branch_progression,)
        )
        shared_messages = json.loads(shared_row["collection"])
        direct_message = keeper_row["first_msg_id"]
        assert len(shared_messages) == 1 and direct_message is not None
        before = {}
        for table in ("sessions", "branches", "progressions", "messages"):
            before[table] = await db.fetch_all(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608

    original_tx = StateDB._tx
    observed_limits = []

    @asynccontextmanager
    async def limited_tx(self):
        async with original_tx(self) as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            # aiosqlite owns the SQLite connection on its worker thread.
            prior = await raw._execute(raw._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 30)
            observed_limits.append(
                await raw._execute(raw._conn.getlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
            )
            try:
                yield conn
            finally:
                await raw._execute(raw._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, prior)

    monkeypatch.setattr(StateDB, "_tx", limited_tx)
    # Twelve victims produce 36 bindings in the old three-way IN statement;
    # earlier victim recheck needs only 14 and progression collection 24.
    counts = await _prune(keep_days=30, keep_n=0, dry_run=dry_run)
    assert observed_limits == [30]
    assert counts == {"sessions": 12, "branches": 1, "messages": 2}

    async with StateDB(path) as db:
        after = {}
        for table in ("sessions", "branches", "progressions", "messages"):
            after[table] = await db.fetch_all(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608
        if dry_run:
            assert after == before
        else:
            for sid in victims:
                assert await db.get_session(sid) is None
            assert (await db.get_session(keeper))["progression_id"] == shared_progression
            assert await db.fetch_one(
                "SELECT id FROM progressions WHERE id = ?", (shared_progression,)
            )
            for message_id in [*shared_messages, direct_message]:
                assert await db.fetch_one("SELECT id FROM messages WHERE id = ?", (message_id,))


def _limit_transactions(monkeypatch, *, rollback_scratch=None):
    original_tx = StateDB._tx

    @asynccontextmanager
    async def limited_tx(self):
        try:
            async with original_tx(self) as conn:
                raw = (await conn.get_raw_connection()).driver_connection
                previous = await raw._execute(
                    raw._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 30
                )
                try:
                    yield conn
                finally:
                    await raw._execute(
                        raw._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous
                    )
        except Exception:
            if rollback_scratch is not None:
                # Reacquire the single connection after the transaction's rollback.
                async with self._engine.connect() as conn:
                    result = await conn.exec_driver_sql(
                        "SELECT name FROM sqlite_temp_master WHERE type = 'table' "
                        "AND name LIKE 'prune_%' ORDER BY name"
                    )
                    rollback_scratch.extend(row[0] for row in result.fetchall())
            raise

    monkeypatch.setattr(StateDB, "_tx", limited_tx)


def _isolate_store(monkeypatch, path):
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )


@pytest.mark.asyncio
async def test_prune_rechecks_more_candidates_than_connection_can_bind(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _isolate_store(monkeypatch, path)
    old_at = time.time() - 60 * 86400
    async with StateDB(path) as db:
        victims = [
            await _seed_session(db, status="completed", updated_at=old_at) for _ in range(31)
        ]

    # The worker-lock helper is independently bounded here so the next, retention
    # predicate recheck encounters 31 victim ids plus keep_n/cutoff over the limit.
    monkeypatch.setattr(db_mod, "_CLI_WORKER_PRUNE_CHUNK", 16)
    _limit_transactions(monkeypatch)
    assert await _prune(keep_days=30, keep_n=0, dry_run=True) == {
        "sessions": 31,
        "branches": 0,
        "messages": 0,
    }
    async with StateDB(path) as db:
        for sid in victims:
            assert await db.get_session(sid) is not None


@pytest.mark.asyncio
async def test_prune_failure_after_session_deletes_rolls_back_all_batches(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _isolate_store(monkeypatch, path)
    old_at = time.time() - 60 * 86400
    async with StateDB(path) as db:
        victim, keeper, _ = await _seed_prune_fixture_with_shared_and_pinned_messages(db)
        victims = [victim]
        for _ in range(11):
            victims.append(await _seed_session(db, status="completed", updated_at=old_at))
        before = {}
        for table in ("sessions", "branches", "progressions", "messages"):
            before[table] = await db.fetch_all(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608
        # The trigger fires only after the session cascade and message-delete stage.
        await db.execute(
            "CREATE TRIGGER prune_test_failure BEFORE DELETE ON progressions "
            "BEGIN SELECT RAISE(ABORT, 'injected prune failure'); END"
        )

    scratch_after_rollback = []
    _limit_transactions(monkeypatch, rollback_scratch=scratch_after_rollback)
    with pytest.raises(Exception, match="injected prune failure"):
        await _prune(keep_days=30, keep_n=0, dry_run=False)
    assert scratch_after_rollback == []
    async with StateDB(path) as db:
        for table in ("sessions", "branches", "progressions", "messages"):
            after = await db.fetch_all(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608
            assert after == before[table]
        assert await db.get_session(keeper) is not None
        for sid in victims:
            assert await db.get_session(sid) is not None
