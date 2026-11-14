"""Pruning respects active terminal delivery debt and removes ACK children."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

import lionagi.state.db as state_db
from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionagi.state.lifecycle.deliveries import (
    ack_delivery,
    get_owed_transition,
    reconcile_unacknowledged,
    register_consumer,
    retire_consumer,
)
from lionhub.orchestration.scheduler import db_maintenance, settings

CONSUMER = "retention-test"


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr(state_db, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(settings, "PRUNE_ARCHIVE_DIR", None)
    monkeypatch.setattr(settings, "PRUNE_CHUNK_ROWS", 5)
    return path


async def _terminal(db, name):
    db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
    await db.create_progression(name)
    await db.create_session({"id": name, "progression_id": name, "status": "running"})
    assert await db.update_status(
        "session",
        name,
        new_status="failed",
        reason_code="session.stale.no_heartbeat",
    )
    await db.execute("UPDATE sessions SET updated_at = 1 WHERE id = ?", (name,))
    return (
        await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_id = ? AND delivery_eligible IS TRUE",
            (name,),
        )
    )["id"]


async def _mixed_session(db):
    first = await _terminal(db, "mixed")
    await ack_delivery(db, first, CONSUMER)
    # Reopen the fixture while retaining its first eligible event and ACK.
    await db.execute("UPDATE sessions SET status = 'running' WHERE id = 'mixed'")
    assert await db.update_status(
        "session",
        "mixed",
        new_status="failed",
        reason_code="session.stale.no_heartbeat",
    )
    await db.execute("UPDATE sessions SET updated_at = 1 WHERE id = 'mixed'")
    second = (
        await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_id = 'mixed' "
            "AND delivery_eligible IS TRUE AND id != ?",
            (first,),
        )
    )["id"]
    return first, second


def _disable_foreign_keys_for_prune(monkeypatch):
    observed = []

    class UnenforcedStateDB(state_db.StateDB):
        async def __aenter__(self):
            await super().__aenter__()

            @event.listens_for(self._engine.sync_engine, "checkout")
            def disable_foreign_keys(connection, _record, _proxy):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys = OFF")
                cursor.close()

            return self

        @asynccontextmanager
        async def transaction(self):
            async with super().transaction() as conn:
                observed.append((await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one())
                yield conn

    monkeypatch.setattr(db_maintenance, "StateDB", UnenforcedStateDB)
    return observed


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("entry", ["explicit", "age", "explicit-fk-off"])
async def test_prune_retains_owed_history_and_removes_ack_children_in_a_mixed_chunk(
    store, entry, monkeypatch, caplog
):
    observed = _disable_foreign_keys_for_prune(monkeypatch) if entry == "explicit-fk-off" else None
    async with state_db.StateDB(store) as db:
        await register_consumer(db, CONSUMER, start="all")
        owed_only = await _terminal(db, "owed")
        acknowledged = await _terminal(db, "acked")
        await ack_delivery(db, acknowledged, CONSUMER)
        mixed_ack, mixed_owed = await _mixed_session(db)
        # An ACK from another consumer cannot satisfy the original consumer's debt.
        await register_consumer(db, "other", start="now")

    with caplog.at_level(logging.INFO, logger=db_maintenance._log.name):
        if entry == "age":
            assert await db_maintenance.prune_old_data(keep_days=30, actor="test") == {
                "sessions_pruned": 1,
                "runs_pruned": 0,
                "dispatch_purged": 0,
            }
            assert "retained_for_delivery=2" in caplog.text
        else:
            assert (
                await db_maintenance.prune_terminal_sessions_by_id(["owed", "acked", "mixed"]) == 1
            )
            assert "Retained 2 terminal sessions with owed deliveries" in caplog.text

    if observed is not None:
        assert observed == [0]

    async with state_db.StateDB(store) as db:
        assert await db.get_session("owed") is not None
        assert await db.get_session("mixed") is not None
        assert await db.get_session("acked") is None
        assert {r["transition_id"] for r in await reconcile_unacknowledged(db, CONSUMER)} == {
            owed_only,
            mixed_owed,
        }
        assert await get_owed_transition(db, CONSUMER, owed_only) is not None
        assert await get_owed_transition(db, CONSUMER, mixed_owed) is not None
        assert {
            r["transition_id"]
            for r in await db.fetch_all("SELECT transition_id FROM terminal_deliveries")
        } == {
            owed_only,
            mixed_ack,
            mixed_owed,
        }
        assert not await db.fetch_one(
            "SELECT id FROM status_transitions WHERE id = ?", (acknowledged,)
        )
        assert await db.fetch_all("PRAGMA foreign_key_check") == []
        if entry == "age":
            entries = await db.list_admin_events(action="prune")
            assert len(entries) == 1
            assert json.loads(entries[0]["details"])["sessions_retained_for_delivery"] == 2


@pytest.mark.asyncio(loop_scope="function")
async def test_retired_consumer_does_not_block_prune(store):
    async with state_db.StateDB(store) as db:
        await register_consumer(db, CONSUMER, start="all")
        acked, owed = await _mixed_session(db)
        assert await get_owed_transition(db, CONSUMER, owed) is not None
        assert await retire_consumer(db, CONSUMER)
        assert await db.fetch_one(
            "SELECT 1 FROM terminal_deliveries WHERE transition_id = ?", (acked,)
        )
    assert await db_maintenance.prune_terminal_sessions_by_id(["mixed"]) == 1
    async with state_db.StateDB(store) as db:
        assert await db.get_session("mixed") is None
        assert await db.fetch_all("SELECT * FROM terminal_deliveries") == []
        assert await db.fetch_all("SELECT * FROM status_transitions") == []
        assert await db.fetch_all("PRAGMA foreign_key_check") == []


@pytest.mark.asyncio(loop_scope="function")
async def test_postgresql_prune_binds_debt_and_ack_statements(monkeypatch):
    """Exercise the actual statement builders with the asyncpg dialect."""
    executed = []

    async def execute(statement, values=None):
        compiled = statement.compile(dialect=PGDialect_asyncpg())
        bound = tuple(values[name] for name in compiled.positiontup) if values else ()
        executed.append((str(compiled), bound))
        if str(statement).startswith("SELECT id FROM sessions WHERE status IN"):
            return SimpleNamespace(fetchall=lambda: [("session-1",)])
        if str(statement).startswith("SELECT progression_id FROM sessions"):
            return SimpleNamespace(fetchall=lambda: [])
        return SimpleNamespace(rowcount=1, fetchall=lambda: [])

    async def lock_candidates(_conn, ids, *, dialect):
        assert dialect == "postgresql"
        return ids

    monkeypatch.setattr(db_maintenance, "_locked_cli_worker_prune_candidates", lock_candidates)
    conn = SimpleNamespace(dialect=PGDialect_asyncpg(), execute=execute)
    assert await db_maintenance._prune_session_chunk(
        conn,
        ["session-1"],
        sess_ph="?, ?, ?, ?, ?, ?",
        retention_sql="status IN (?, ?, ?, ?, ?, ?)",
        retention_params=db_maintenance._TERMINAL_SESSION_STATUSES,
        archive_destination=None,
        archive_id="unused",
    ) == (1, 0)

    sql = [statement for statement, _ in executed]
    consumers = sql.index("LOCK TABLE terminal_consumers IN SHARE MODE")
    deliveries = sql.index("LOCK TABLE terminal_deliveries IN SHARE ROW EXCLUSIVE MODE")
    debt = next(i for i, statement in enumerate(sql) if "st.delivery_eligible IS TRUE" in statement)
    child_delete = next(
        i
        for i, statement in enumerate(sql)
        if statement.startswith("DELETE FROM terminal_deliveries")
    )
    parent_delete = next(
        i
        for i, statement in enumerate(sql)
        if statement.startswith("DELETE FROM status_transitions")
    )
    assert consumers < deliveries < debt < child_delete < parent_delete
    assert "tc.retired_at IS NULL" in sql[debt]
    assert "$1" in sql[debt] and executed[debt][1] == ("session-1",)
    assert executed[child_delete][1] == ("session-1", *db_maintenance._TERMINAL_SESSION_STATUSES)
