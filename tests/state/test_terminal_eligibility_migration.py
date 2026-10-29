# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""v5 terminal-delivery history classification on both native backends."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, text

import lionagi.state.db as db_module
from lionagi.state.db import StateDB
from lionagi.state.engine import make_engine
from lionagi.state.lifecycle.callbacks import EXECUTION_ENTITY_KINDS
from lionagi.state.lifecycle.policy import DEFAULT_REGISTRY
from lionagi.state.terminal_eligibility import is_delivery_eligible

pytestmark = pytest.mark.unit
V5_SCHEMA = Path(__file__).resolve().parents[1] / "fixtures" / "state_schema_v5.sql"
MARKER = "migration.terminal_delivery_eligibility"
ROWS = [
    ("true-session", "session", "running", "completed", True),
    ("true-admin", "session", "running", "failed", True),
    ("true-invocation", "invocation", "running", "timed_out", True),
    ("true-play", "play", "running", "merged", True),
    ("true-schedule", "schedule_run", "running", "skipped", True),
    ("false-other", "show", "active", "completed", False),
    ("false-nonterminal", "session", "running", "running_again", False),
    ("false-same", "session", "completed", "completed", False),
    ("unknown-reaper", "session", None, "failed", None),
    ("unknown-init", "schedule_run", None, "skipped", None),
]
INSERT = (
    "INSERT INTO status_transitions "
    "(id, entity_type, entity_id, previous_status, status, reason_code, source, created_at) "
    "VALUES (:id, :kind, :id, :previous, :status, 'fixture', 'system', 1)"
)


def make_v5_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(V5_SCHEMA.read_text())
        conn.executemany(
            "INSERT INTO status_transitions "
            "(id, entity_type, entity_id, previous_status, status, reason_code, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'fixture', 'system', 1)",
            [(i, kind, i, previous, status) for i, kind, previous, status, _ in ROWS],
        )


async def assert_migration(db: StateDB) -> None:
    await db.open()
    assert await db.schema_version() == "6"
    async with db._read() as conn:
        rows = (
            await conn.execute(text("SELECT id, delivery_eligible FROM status_transitions"))
        ).all()
        actual = {
            row.id: None if row.delivery_eligible is None else bool(row.delivery_eligible)
            for row in rows
        }
        value = (
            await conn.execute(
                text("SELECT value FROM schema_meta WHERE key = :key"), {"key": MARKER}
            )
        ).scalar_one()
        marker = json.loads(value)
        assert isinstance(marker["classified_at"], (float, int))
        assert marker["initial_null_count"] == 2
        assert await conn.run_sync(lambda c: inspect(c).has_table("terminal_consumers"))
    assert actual == {i: expected for i, _, _, _, expected in ROWS}

    # Simulate an old process committing after migration with a timestamp from
    # before the marker; timestamp comparisons would miss this row.
    async with db._tx() as conn:
        await conn.execute(
            text(INSERT),
            {"id": "delayed", "kind": "session", "previous": "running", "status": "failed"},
        )
    await db.close()
    await db.open()
    async with db._read() as conn:
        assert (
            await conn.execute(
                text("SELECT value FROM schema_meta WHERE key = :key"), {"key": MARKER}
            )
        ).scalar_one() == value
        nulls = (
            await conn.execute(
                text("SELECT COUNT(*) FROM status_transitions WHERE delivery_eligible IS NULL")
            )
        ).scalar_one()
        assert nulls - marker["initial_null_count"] == 1
        assert (
            await conn.execute(
                text("SELECT delivery_eligible FROM status_transitions WHERE id = 'delayed'")
            )
        ).scalar_one() is None


async def test_v5_sqlite_classification_and_reopen(tmp_path):
    path = tmp_path / "state.db"
    make_v5_sqlite(path)
    db = StateDB(path=path)
    try:
        await assert_migration(db)
    finally:
        await db.close()


async def test_v5_frozen_predicate_matches_registry_terminal_transitions(tmp_path):
    # These are the historical v5 terminal statuses. If the registry changes,
    # update this test's expected sets, never the predicate used for old stores.
    frozen = {
        "session": {"completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"},
        "invocation": {
            "completed",
            "completed_empty",
            "failed",
            "timed_out",
            "aborted",
            "cancelled",
        },
        "play": {"merged", "escalated", "gate_failed", "blocked", "aborted_after_finish"},
        "schedule_run": {"completed", "failed", "timed_out", "skipped", "cancelled"},
    }
    assert set(frozen) == EXECUTION_ENTITY_KINDS
    candidates = []
    for kind in sorted(EXECUTION_ENTITY_KINDS):
        terminals = frozen[kind] | set(DEFAULT_REGISTRY.get(kind).terminal_statuses)
        for status in sorted(terminals):
            candidates.append((kind, "__different__", status))
        terminal = sorted(frozen[kind])[0]
        candidates.extend(
            [
                (kind, terminal, terminal),
                (kind, None, terminal),
                (kind, "__different__", "__nonterminal__"),
            ]
        )
    candidates.append(("show", "active", "completed"))

    path = tmp_path / "state.db"
    make_v5_sqlite(path)
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO status_transitions "
            "(id, entity_type, entity_id, previous_status, status, reason_code, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'fixture', 'system', 1)",
            [
                (f"guard-{i}", kind, f"guard-{i}", previous, status)
                for i, (kind, previous, status) in enumerate(candidates)
            ],
        )

    db = StateDB(path=path)
    try:
        await db.open()
        async with db._read() as conn:
            rows = (
                await conn.execute(text("SELECT id, delivery_eligible FROM status_transitions"))
            ).all()
        actual = {
            row.id: None if row.delivery_eligible is None else bool(row.delivery_eligible)
            for row in rows
        }
        for i, (kind, previous, status) in enumerate(candidates):
            expected = (
                None
                if previous is None and status in frozen.get(kind, set())
                else is_delivery_eligible(kind, previous, status, write_kind="transition")
            )
            assert actual[f"guard-{i}"] == expected, (
                f"Frozen v5 predicate and live registry disagree for {kind}/{previous}/{status}; "
                "when the registry changes, retain the frozen predicate and update the test's expected sets."
            )
    finally:
        await db.close()


async def test_v5_readonly_open_does_not_migrate(tmp_path):
    path = tmp_path / "state.db"
    make_v5_sqlite(path)
    before = path.read_bytes()
    db = StateDB(path=path, readonly=True)
    try:
        await db.open()
        assert await db.schema_version() == "5"
        async with db._read() as conn:
            assert not await conn.run_sync(lambda c: inspect(c).has_table("terminal_consumers"))
            columns = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("status_transitions")}
            )
            assert "delivery_eligible" not in columns
    finally:
        await db.close()
    assert path.read_bytes() == before


async def test_fresh_database_has_zero_baseline(tmp_path):
    db = StateDB(path=tmp_path / "new.db")
    try:
        await db.open()
        async with db._read() as conn:
            value = (
                await conn.execute(
                    text("SELECT value FROM schema_meta WHERE key=:key"), {"key": MARKER}
                )
            ).scalar_one()
            assert json.loads(value)["initial_null_count"] == 0
            assert await conn.run_sync(lambda c: inspect(c).has_table("terminal_consumers"))
    finally:
        await db.close()


async def test_failed_classification_rolls_back_column_marker_and_version(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    make_v5_sqlite(path)
    original = StateDB._classify_terminal_delivery_history_once

    async def fail_after_classification(self, conn):
        await original(self, conn)
        raise RuntimeError("injected migration interruption")

    db = StateDB(path=path)
    monkeypatch.setattr(
        StateDB, "_classify_terminal_delivery_history_once", fail_after_classification
    )
    try:
        with pytest.raises(RuntimeError, match="injected migration"):
            await db.open()
    finally:
        await db.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone() == (
            "5",
        )
        assert (
            conn.execute("SELECT value FROM schema_meta WHERE key=?", (MARKER,)).fetchone() is None
        )
        assert "delivery_eligible" not in {
            row[1] for row in conn.execute("PRAGMA table_info(status_transitions)")
        }
    monkeypatch.setattr(StateDB, "_classify_terminal_delivery_history_once", original)
    try:
        await assert_migration(db)
    finally:
        await db.close()


async def test_concurrent_sqlite_opens_classify_once(tmp_path):
    path = tmp_path / "state.db"
    make_v5_sqlite(path)
    first, second = StateDB(path=path), StateDB(path=path)
    try:
        await asyncio.gather(first.open(), second.open())
        async with first._read() as conn:
            marker = json.loads(
                (
                    await conn.execute(
                        text("SELECT value FROM schema_meta WHERE key=:key"), {"key": MARKER}
                    )
                ).scalar_one()
            )
            assert marker["initial_null_count"] == 2
            assert (
                await conn.execute(
                    text("SELECT COUNT(*) FROM status_transitions WHERE delivery_eligible IS NULL")
                )
            ).scalar_one() == 2
    finally:
        await first.close()
        await second.close()


@pytest_asyncio.fixture
async def isolated_migration_postgres(pg_url, monkeypatch):
    # A private schema keeps the session-scoped PG fixture's other tests intact.
    schema = "migration_3203_" + uuid.uuid4().hex
    admin = make_engine(pg_url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    def isolated_engine(url, **kwargs):
        # One connection makes the pool-reuse regression deterministic.
        kwargs.setdefault("pool_size", 1)
        kwargs.setdefault("max_overflow", 0)
        engine = make_engine(url, **kwargs)

        def set_schema(connection, _record):
            cursor = connection.cursor()
            try:
                cursor.execute(f'SET search_path TO "{schema}"')
            finally:
                cursor.close()
            # SET is transactional in PostgreSQL. Commit it before the first
            # inspection connection is returned to the pool and rolled back.
            connection.commit()

        event.listen(engine.sync_engine, "connect", set_schema)
        return engine

    monkeypatch.setattr(db_module, "make_engine", isolated_engine)
    db = StateDB(url=pg_url)
    try:
        yield db, schema
    finally:
        await db.close()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_postgres_v5_classification(isolated_migration_postgres):
    db, schema = isolated_migration_postgres
    await db.open()
    async with db._tx() as conn:
        assert (await conn.execute(text("SELECT current_schema()"))).scalar_one() == schema
        await conn.execute(text("DROP TABLE terminal_consumers"))
        await conn.execute(text("ALTER TABLE status_transitions DROP COLUMN delivery_eligible"))
        await conn.execute(text("DELETE FROM schema_meta WHERE key=:key"), {"key": MARKER})
        await conn.execute(text("UPDATE schema_meta SET value='5' WHERE key='version'"))
        for i, kind, previous, status, _ in ROWS:
            await conn.execute(
                text(INSERT), {"id": i, "kind": kind, "previous": previous, "status": status}
            )
        assert (await conn.execute(text("SELECT current_schema()"))).scalar_one() == schema
    await db.close()
    await assert_migration(db)
    async with db._read() as conn:
        assert (await conn.execute(text("SELECT current_schema()"))).scalar_one() == schema


async def test_postgres_search_path_survives_rollback_and_pool_reuse(isolated_migration_postgres):
    db, schema = isolated_migration_postgres
    await db.open()
    async with db._engine.connect() as first:
        transaction = await first.begin()
        initial = (await first.execute(text("SELECT current_schema(), pg_backend_pid()"))).one()
        assert initial[0] == schema
        await transaction.rollback()
    # The pool has one connection. Verify backend identity as well so a newly
    # connected session cannot make the isolation assertion pass by accident.
    async with db._engine.connect() as reused:
        after = (await reused.execute(text("SELECT current_schema(), pg_backend_pid()"))).one()
        assert after[1] == initial[1]
        assert after[0] == schema
