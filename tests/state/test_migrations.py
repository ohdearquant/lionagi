# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for StateDB schema migration — verifies _reconcile_columns adds all expected columns to old-schema tables."""

from __future__ import annotations

import aiosqlite
import pytest

from lionagi.state.schema_migrations import MIGRATION_COLUMNS

# ── Helpers ───────────────────────────────────────────────────────────────────


async def _column_names(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    return {row["name"] for row in rows}


# ── Old-schema fixture ────────────────────────────────────────────────────────


@pytest.fixture
async def old_schema_db():
    """In-memory DB with bare-minimum columns simulating a pre-migration schema."""
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        # sessions: bare-minimum columns from the ADR-0017 era
        await db.execute("""
            CREATE TABLE sessions (
                id           TEXT PRIMARY KEY,
                created_at   REAL NOT NULL,
                progression_id TEXT NOT NULL
            )
        """)
        # branches: bare-minimum
        await db.execute("""
            CREATE TABLE branches (
                id         TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                session_id TEXT NOT NULL
            )
        """)
        # shows: bare-minimum
        await db.execute("""
            CREATE TABLE shows (
                id         TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            )
        """)
        # plays: bare-minimum
        await db.execute("""
            CREATE TABLE plays (
                id         TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            )
        """)
        # invocations: bare-minimum
        await db.execute("""
            CREATE TABLE invocations (
                id         TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            )
        """)
        # teams: bare-minimum
        await db.execute("""
            CREATE TABLE teams (
                id         TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            )
        """)
        # artifacts: bare-minimum (without updated_at)
        await db.execute("""
            CREATE TABLE artifacts (
                id         TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE schedules (
                id         TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            )
        """)
        # schedule_runs: bare-minimum (without updated_at, reason cols)
        await db.execute("""
            CREATE TABLE schedule_runs (
                id          TEXT PRIMARY KEY,
                created_at  REAL NOT NULL,
                schedule_id TEXT NOT NULL
            )
        """)
        await db.commit()
        yield db


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_migration_columns_constant_is_importable():
    """MIGRATION_COLUMNS can be imported and has the expected tables."""
    expected_tables = {
        "sessions",
        "branches",
        "shows",
        "plays",
        "invocations",
        "teams",
        "artifacts",
        "schedules",
        "schedule_runs",
        "engine_runs",  # Phase C Move 2 — new table registered for future migrations
    }
    assert set(MIGRATION_COLUMNS.keys()) == expected_tables


async def test_migration_columns_no_duplicates():
    """Each table's migration list has no duplicate column names."""
    for table, cols in MIGRATION_COLUMNS.items():
        names = [name for name, _ in cols]
        assert len(names) == len(set(names)), f"Duplicate columns in {table}: {names}"


async def test_reconcile_adds_all_columns(old_schema_db):
    """_reconcile_columns upgrades an old-schema DB to have every migration column."""
    db = old_schema_db

    # Run the same logic as StateDB._reconcile_columns
    for table, columns in MIGRATION_COLUMNS.items():
        cur = await db.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        if not rows:
            continue
        existing = {row["name"] for row in rows}
        for name, defn in columns:
            if name not in existing:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {defn}")
    await db.commit()

    # Verify every migration column now exists (skip tables not in old_schema_db
    # — newly added tables like engine_runs are created by schema.sql, not via
    # ALTER TABLE, so they won't be present in an "old schema" DB fixture).
    for table, columns in MIGRATION_COLUMNS.items():
        if not columns:
            continue
        actual = await _column_names(db, table)
        if not actual:
            continue  # table not present in old_schema_db — skip (new table, not migrated)
        for col_name, _ in columns:
            assert col_name in actual, (
                f"Migration column '{col_name}' missing from table '{table}' after reconcile"
            )


async def test_reconcile_is_idempotent(old_schema_db):
    """Running _reconcile_columns twice does not raise or corrupt the schema."""
    db = old_schema_db

    async def reconcile():
        for table, columns in MIGRATION_COLUMNS.items():
            cur = await db.execute(f"PRAGMA table_info({table})")
            rows = await cur.fetchall()
            if not rows:
                continue
            existing = {row["name"] for row in rows}
            for name, defn in columns:
                if name not in existing:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {defn}")
        await db.commit()

    await reconcile()
    await reconcile()  # second pass — all columns already exist, nothing to do

    for table, columns in MIGRATION_COLUMNS.items():
        if not columns:
            continue
        actual = await _column_names(db, table)
        if not actual:
            continue  # new table not in old_schema_db — skip
        for col_name, _ in columns:
            assert col_name in actual


async def test_sessions_upgrade_path_populates_adr0028_columns(old_schema_db):
    """After upgrade, sessions table has the ADR-0028 status-reason columns."""
    db = old_schema_db

    # Perform migration
    for table, columns in MIGRATION_COLUMNS.items():
        cur = await db.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        if not rows:
            continue
        existing = {row["name"] for row in rows}
        for name, defn in columns:
            if name not in existing:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {defn}")
    await db.commit()

    # Insert a row and write to the ADR-0028 columns
    await db.execute(
        "INSERT INTO sessions (id, created_at, progression_id,"
        " status_reason_code, status_reason_summary, status_evidence_refs)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("sess-1", 1.0, "prog-1", "run.completed.ok", "all good", "[]"),
    )
    await db.commit()

    cur = await db.execute(
        "SELECT status_reason_code, status_reason_summary, status_evidence_refs"
        " FROM sessions WHERE id = ?",
        ("sess-1",),
    )
    row = await cur.fetchone()
    assert row["status_reason_code"] == "run.completed.ok"
    assert row["status_reason_summary"] == "all good"
    assert row["status_evidence_refs"] == "[]"


async def test_schedule_runs_upgrade_path(old_schema_db):
    """After upgrade, schedule_runs has updated_at and ADR-0028 reason columns."""
    db = old_schema_db

    for table, columns in MIGRATION_COLUMNS.items():
        cur = await db.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        if not rows:
            continue
        existing = {row["name"] for row in rows}
        for name, defn in columns:
            if name not in existing:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {defn}")
    await db.commit()

    adr28_cols = {
        "status_reason_code",
        "status_reason_summary",
        "status_evidence_refs",
        "updated_at",
    }
    actual = await _column_names(db, "schedule_runs")
    assert adr28_cols <= actual, f"Missing cols: {adr28_cols - actual}"


async def test_statedb_open_exposes_migration_columns():
    """StateDB.open() on a fresh :memory: DB has all migration columns.

    Confirms the import chain StateDB → MIGRATION_COLUMNS works end-to-end:
    _reconcile_columns is a no-op on a fresh DB (columns already exist), but
    verifying their presence confirms that MIGRATION_COLUMNS and the schema.sql
    agree on what the current schema looks like.
    """
    from sqlalchemy import text

    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    for table, columns in MIGRATION_COLUMNS.items():
        if not columns:
            continue
        async with state._read() as conn:
            rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).mappings().all()
        if not rows:
            continue
        actual = {row["name"] for row in rows}
        for col_name, _ in columns:
            assert col_name in actual, (
                f"Column '{col_name}' missing from '{table}' in a fresh StateDB"
            )

    await state.close()


# ── max_runs / count_schedule_runs (one-shot semantics) ──────────────────────


async def test_count_schedule_runs_excludes_skipped_and_running():
    """count_schedule_runs only counts terminal, top-level (chain_depth=0) runs."""
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await state.create_schedule(
        {
            "id": "sched-count-1",
            "name": "count-test",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    statuses = ["completed", "failed", "cancelled", "skipped", "running"]
    for i, status in enumerate(statuses):
        await state.create_schedule_run(
            {
                "id": f"run-{i}",
                "schedule_id": "sched-count-1",
                "trigger_context": {},
                "action_kind": "agent",
                "action_args": [],
                "status": status,
                "chain_depth": 0,
                "fired_at": 1.0,
            }
        )

    count = await state.count_schedule_runs("sched-count-1", chain_depth=0)
    assert count == 3  # completed, failed, cancelled — not skipped, not running

    await state.close()


async def test_count_schedule_runs_excludes_chain_children():
    """Chain children (chain_depth>0) never count toward the parent's max_runs."""
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await state.create_schedule(
        {
            "id": "sched-count-2",
            "name": "count-test-chain",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    await state.create_schedule_run(
        {
            "id": "run-parent",
            "schedule_id": "sched-count-2",
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": "completed",
            "chain_depth": 0,
            "fired_at": 1.0,
        }
    )
    await state.create_schedule_run(
        {
            "id": "run-child",
            "schedule_id": "sched-count-2",
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": "completed",
            "chain_depth": 1,
            "chain_parent_id": "run-parent",
            "fired_at": 2.0,
        }
    )

    count = await state.count_schedule_runs("sched-count-2", chain_depth=0)
    assert count == 1

    await state.close()


async def test_max_runs_nullable_defaults_unlimited():
    """A schedule created without max_runs stores it as NULL, not counted against."""
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await state.create_schedule(
        {
            "id": "sched-unlimited",
            "name": "unlimited-test",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    fetched = await state.get_schedule("sched-unlimited")
    assert fetched["max_runs"] is None

    await state.close()
