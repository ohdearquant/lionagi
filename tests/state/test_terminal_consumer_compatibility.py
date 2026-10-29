# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Foreign-store writes remain best-effort; durable consumers require native v6."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionagi.state.lifecycle.deliveries import (
    ConsumerCompatibilityError,
    ConsumerSchemaError,
    ack_delivery,
    eligibility_counts,
    is_acknowledged,
    reconcile_unacknowledged,
    register_consumer,
    retire_consumer,
)
from tests.state.test_compatibility_owner_ops import _INSERTS, _managed_store, _profile
from tests.state.test_terminal_eligibility_migration import make_v5_sqlite

pytestmark = pytest.mark.unit


def _manager_state(path):
    with closing(sqlite3.connect(path)) as conn:
        schema = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        meta = conn.execute("SELECT key, value FROM schema_meta ORDER BY key").fetchall()
    return schema, meta


async def _foreign_store(path, *, has_column):
    await _managed_store(path)
    with closing(sqlite3.connect(path)) as conn:
        # The foreign manager has not installed the native consumer indexes.
        conn.execute("DROP INDEX idx_status_transitions_unclassified")
        conn.execute("DROP INDEX idx_status_transitions_eligible_order")
        if not has_column:
            conn.execute("ALTER TABLE status_transitions DROP COLUMN delivery_eligible")
        # The manager never installed this consumer feature. A new foreign shape
        # can have the column without agreeing to lionagi's migration or tables.
        conn.execute("DROP TABLE terminal_consumers")
        conn.execute(
            "DELETE FROM schema_meta WHERE key = 'migration.terminal_delivery_eligibility'"
        )
        conn.commit()
    inserts = {table: set(columns) for table, columns in _INSERTS.items()}
    if has_column:
        inserts["status_transitions"].add("delivery_eligible")
    return _profile(path, inserts=inserts)


def _consumer_calls(db):
    return (
        lambda: register_consumer(db, "sink", start="all"),
        lambda: retire_consumer(db, "sink"),
        lambda: ack_delivery(db, "transition", "sink"),
        lambda: is_acknowledged(db, "transition", "sink"),
        lambda: reconcile_unacknowledged(db, "sink"),
        lambda: eligibility_counts(db),
    )


@pytest.mark.parametrize("has_column", [False, True], ids=["old-foreign", "new-foreign"])
async def test_foreign_transition_pushes_without_migrating_and_consumers_refuse(
    tmp_path, has_column
):
    path = tmp_path / "foreign.db"
    profile = await _foreign_store(path, has_column=has_column)
    schema_before, meta_before = _manager_state(path)
    db = StateDB(path, compatibility=profile, owner="principal:a")
    callbacks = TerminalCallbackRegistry()
    received = []
    callbacks.register("collector", received.append)
    try:
        await db.open()
        db._lifecycle_service()._terminal_callbacks = callbacks
        await db.create_progression("progression")
        await db.create_session(
            {"id": "session", "progression_id": "progression", "status": "running"}
        )
        assert await db.update_status(
            "session", "session", new_status="completed", reason_code="run.completed.ok"
        )
        assert len(received) == 1
        assert received[0].entity.id == "session"
        assert received[0].previous_status == "running"
        assert received[0].terminal_status == "completed"
        for call in _consumer_calls(db):
            with pytest.raises(ConsumerCompatibilityError, match="compatibility mode"):
                await call()
    finally:
        await db.close()
    assert _manager_state(path) == (schema_before, meta_before)
    with closing(sqlite3.connect(path)) as conn:
        if has_column:
            rows = conn.execute(
                "SELECT id, previous_status, delivery_eligible FROM status_transitions "
                "WHERE entity_id='session' ORDER BY created_at"
            ).fetchall()
            assert len(rows) == 2
            initial, terminal = rows
            assert initial[1:] == (None, 0)
            assert terminal[1:] == ("running", 1)
            assert received[0].event_id == terminal[0]
        else:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(status_transitions)")}
            assert "delivery_eligible" not in columns
            terminal = conn.execute(
                "SELECT id FROM status_transitions WHERE entity_id='session' AND status='completed'"
            ).fetchone()
            assert received[0].event_id == terminal[0]


async def test_unmigrated_readonly_store_refuses_all_consumer_apis_with_version(tmp_path):
    path = tmp_path / "v5.db"
    make_v5_sqlite(path)
    before = path.read_bytes()
    db = StateDB(path, readonly=True)
    try:
        await db.open()
        for call in _consumer_calls(db):
            with pytest.raises(ConsumerSchemaError, match="schema version 5"):
                await call()
    finally:
        await db.close()
    assert path.read_bytes() == before
