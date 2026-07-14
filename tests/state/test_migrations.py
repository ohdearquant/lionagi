# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for StateDB schema migration — verifies _reconcile_columns adds all expected columns to old-schema tables."""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
import sqlite3
import traceback
from pathlib import Path

import aiosqlite
import pytest

from lionagi.state.schema_migrations import MIGRATION_COLUMNS

# ── Helpers ───────────────────────────────────────────────────────────────────


async def _column_names(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    return {row["name"] for row in rows}


def _create_pre_cc_session_db(db_path: Path) -> None:
    """Create a current SQLite schema whose sessions table predates cc_session_id."""
    from lionagi.state.db import _SCHEMA_PATH

    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA_PATH.read_text())
        conn.execute("DROP INDEX idx_sessions_cc_session")
        conn.execute("ALTER TABLE sessions RENAME COLUMN cc_session_id TO legacy_cc_session_id")


def _open_state_db_worker(db_path: str, start_gate, result_queue) -> None:
    """Open and close one StateDB in a spawned process."""
    from lionagi.state.db import StateDB

    async def _open() -> None:
        state = StateDB(db_path)
        await state.open()
        await state.close()

    start_gate.wait(timeout=30)
    try:
        asyncio.run(_open())
    except Exception:  # noqa: BLE001
        result_queue.put(traceback.format_exc())
    else:
        result_queue.put(None)


# ── Old-schema fixture ────────────────────────────────────────────────────────


@pytest.fixture
async def old_schema_db():
    """In-memory DB with bare-minimum columns simulating a pre-migration schema."""
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        # sessions: bare-minimum columns from the ADR-0057 era
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
        "dispatch_outbox",  # ADR-0092 — new table registered for future migrations
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


async def test_statedb_upgrade_adds_cc_session_lookup_index(tmp_path: Path) -> None:
    """Existing databases gain the partial lookup index after the column migration."""
    from sqlalchemy import text

    from lionagi.state.db import StateDB

    db_path = tmp_path / "pre-cc-session.db"
    _create_pre_cc_session_db(db_path)

    state = StateDB(db_path)
    await state.open()
    try:
        await state.create_progression("progression-1")
        await state.execute(
            "INSERT INTO sessions "
            "(id, cc_session_id, created_at, progression_id, updated_at) "
            "VALUES (:id, :cc_session_id, :created_at, :progression_id, :updated_at)",
            {
                "id": "session-1",
                "cc_session_id": "cc-session-1",
                "created_at": 1.0,
                "progression_id": "progression-1",
                "updated_at": 1.0,
            },
        )
        async with state._read() as conn:
            indexes = (await conn.execute(text("PRAGMA index_list(sessions)"))).mappings().all()
            plan = (
                (
                    await conn.execute(
                        text(
                            "EXPLAIN QUERY PLAN SELECT * FROM sessions "
                            "WHERE cc_session_id = :cc_session_id LIMIT 1"
                        ),
                        {"cc_session_id": "cc-session-1"},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await state.close()

    assert "idx_sessions_cc_session" in {row["name"] for row in indexes}
    details = [row["detail"] for row in plan]
    assert any("SEARCH sessions" in detail for detail in details), details
    assert any("idx_sessions_cc_session" in detail for detail in details), details


def test_concurrent_statedb_opens_reconcile_cc_session_column(tmp_path: Path) -> None:
    """Concurrent first opens tolerate another process winning the ALTER race."""
    db_path = tmp_path / "concurrent-pre-cc-session.db"
    _create_pre_cc_session_db(db_path)

    context = multiprocessing.get_context("spawn")
    start_gate = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_open_state_db_worker,
            args=(str(db_path), start_gate, result_queue),
        )
        for _ in range(4)
    ]

    results: list[str | None] = []
    try:
        for process in processes:
            process.start()
        start_gate.set()
        for process in processes:
            process.join(timeout=30)
        for _ in processes:
            try:
                results.append(result_queue.get(timeout=2))
            except queue.Empty:
                results.append("worker exited without reporting a result")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        result_queue.close()
        result_queue.join_thread()

    exit_codes = [process.exitcode for process in processes]
    assert all(code == 0 for code in exit_codes), exit_codes
    assert results == [None] * len(processes), results

    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
    assert columns.count("cc_session_id") == 1


async def test_sessions_upgrade_path_populates_adr0028_columns(old_schema_db):
    """After upgrade, sessions table has the ADR-0057 status-reason columns."""
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

    # Insert a row and write to the ADR-0057 columns
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
    """After upgrade, schedule_runs has updated_at and ADR-0057 reason columns."""
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


async def test_schedule_runs_upgrade_path_gains_resume_packet(old_schema_db):
    """After upgrade, an existing schedule_runs table gains resume_packet."""
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

    actual = await _column_names(db, "schedule_runs")
    assert "resume_packet" in actual


async def test_schedules_upgrade_path_gains_budget_columns(old_schema_db):
    """After upgrade, an existing schedules table gains budget_usd/budget_tokens."""
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

    actual = await _column_names(db, "schedules")
    assert {"budget_usd", "budget_tokens"} <= actual


async def test_drop_legacy_invocations_status_check_with_fk_referencing_rows(tmp_path):
    """Rebuilding invocations for the completion-trust gate must not choke on
    real FK-referencing child rows (sessions.invocation_id, artifacts.invocation_id).

    `invocations` is an FK target; dropping it while foreign_keys enforcement
    is (still) active raises a FOREIGN KEY constraint failure even though the
    data was already copied into the replacement table first. The migration
    must actually disable enforcement for the drop, not just attempt to.
    """
    from sqlalchemy import text

    from lionagi.state.db import StateDB

    db_path = tmp_path / "legacy.db"

    # Build a legacy-shaped DB by hand: invocations with the pre-gate 6-value
    # CHECK, plus a session row and an artifact row that both FK-reference it.
    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.execute("PRAGMA foreign_keys = ON")
        await raw.execute(
            """
            CREATE TABLE invocations (
              id              TEXT    PRIMARY KEY,
              skill           TEXT    NOT NULL,
              plugin          TEXT,
              prompt          TEXT,
              started_at      REAL    NOT NULL,
              ended_at        REAL,
              status          TEXT    NOT NULL DEFAULT 'running' CHECK(
                                status IN ('running', 'completed', 'failed',
                                           'timed_out', 'aborted', 'cancelled')
                              ),
              session_count   INTEGER NOT NULL DEFAULT 0,
              created_at      REAL    NOT NULL,
              updated_at      REAL    NOT NULL,
              node_metadata   JSON
            )
            """
        )
        await raw.execute(
            """
            CREATE TABLE sessions (
              id              TEXT    PRIMARY KEY,
              created_at      REAL    NOT NULL,
              progression_id  TEXT    NOT NULL,
              updated_at      REAL    NOT NULL,
              invocation_id   TEXT    REFERENCES invocations(id)
            )
            """
        )
        await raw.execute(
            """
            CREATE TABLE artifacts (
              id              TEXT    PRIMARY KEY,
              created_at      REAL    NOT NULL,
              invocation_id   TEXT    REFERENCES invocations(id)
            )
            """
        )
        await raw.execute(
            "INSERT INTO invocations (id, skill, started_at, created_at, updated_at) "
            "VALUES ('inv-1', 'agent', 1.0, 1.0, 1.0)"
        )
        await raw.execute(
            "INSERT INTO sessions (id, created_at, progression_id, updated_at, invocation_id) "
            "VALUES ('sess-1', 1.0, 'prog-1', 1.0, 'inv-1')"
        )
        await raw.execute(
            "INSERT INTO artifacts (id, created_at, invocation_id) VALUES ('art-1', 1.0, 'inv-1')"
        )
        await raw.commit()

    state = StateDB(db_path)
    await state.open()
    try:
        async with state._read() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name='invocations'"
                        )
                    )
                )
                .mappings()
                .first()
            )
            assert "'completed_empty'" in row["sql"]

            inv = (
                (await conn.execute(text("SELECT * FROM invocations WHERE id='inv-1'")))
                .mappings()
                .first()
            )
            assert inv is not None

            # The FK-referencing rows in other tables survived the rebuild
            # untouched — the migration only ever touches `invocations`.
            sess = (
                (await conn.execute(text("SELECT invocation_id FROM sessions WHERE id='sess-1'")))
                .mappings()
                .first()
            )
            assert sess["invocation_id"] == "inv-1"
    finally:
        await state.close()


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


# ── resume_packet round-trip ─────────────────────────────────────────────────


async def test_resume_packet_roundtrips_as_dict():
    """A dict-shaped resume_packet written via update_schedule_run reads back
    identical via get_schedule_run, mirroring an Element.to_dict(mode="db")
    sidecar payload."""
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await state.create_schedule(
        {
            "id": "sched-resume-1",
            "name": "resume-test-1",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    await state.create_schedule_run(
        {
            "id": "run-resume-1",
            "schedule_id": "sched-resume-1",
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": {},
            "status": "running",
            "fired_at": 1.0,
        }
    )

    packet = {
        "lion_class": "lionagi.protocols.generic.element.Element",
        "id": "elem-1",
        "created_at": 1700000000.0,
        "metadata": {"turn": 3, "tags": ["a", "b"]},
    }
    await state.update_schedule_run(
        "run-resume-1",
        resume_packet=packet,
    )

    fetched = await state.get_schedule_run("run-resume-1")
    assert fetched is not None
    assert fetched["resume_packet"] == packet

    await state.close()


async def test_resume_packet_null_roundtrips_as_none():
    """A schedule_run created without resume_packet stores NULL, which reads
    back as None, not a JSON-decode error or empty dict."""
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await state.create_schedule(
        {
            "id": "sched-resume-2",
            "name": "resume-test-2",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    await state.create_schedule_run(
        {
            "id": "run-resume-2",
            "schedule_id": "sched-resume-2",
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": {},
            "status": "running",
            "fired_at": 1.0,
        }
    )

    fetched = await state.get_schedule_run("run-resume-2")
    assert fetched is not None
    assert fetched["resume_packet"] is None

    await state.close()


async def test_update_schedule_run_rejects_unknown_field():
    """update_schedule_run's field allowlist still rejects unrecognized
    fields — resume_packet joining the allowed set must not widen it."""
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await state.create_schedule(
        {
            "id": "sched-resume-3",
            "name": "resume-test-3",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    await state.create_schedule_run(
        {
            "id": "run-resume-3",
            "schedule_id": "sched-resume-3",
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": {},
            "status": "running",
            "fired_at": 1.0,
        }
    )

    with pytest.raises(ValueError, match="Invalid schedule_run field"):
        await state.update_schedule_run("run-resume-3", not_a_real_field="x")

    await state.close()


async def test_dispatched_at_migration_backfills_preexisting_running_rows(tmp_path):
    """A ``schedule_runs`` row already at ``status='running'`` when the
    ``dispatched_at`` column is first added must have it backfilled to its
    ``fired_at`` — not left ``NULL``.

    Without the backfill, ``dispatched_at IS NULL`` is indistinguishable
    between "genuinely never dispatched" and "predates the column
    entirely", so ``list_undispatched_schedule_runs()`` (consumed by
    ``SchedulerEngine._recover_undispatched_fires()`` on the next daemon
    startup) would treat every pre-existing running row as crashed and
    duplicate-fire a replacement for it — including one that is still
    genuinely executing across the upgrade restart.
    """
    from sqlalchemy import text

    from lionagi.state.db import StateDB

    db_path = tmp_path / "legacy_no_dispatched_at.db"

    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.execute(
            """
            CREATE TABLE schedules (
              id           TEXT    PRIMARY KEY,
              name         TEXT    NOT NULL UNIQUE,
              enabled      INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
              trigger_type TEXT    NOT NULL CHECK(trigger_type IN ('cron', 'interval', 'github_poll')),
              action_kind  TEXT    NOT NULL CHECK(action_kind IN ('agent', 'flow', 'fanout', 'play', 'flow_yaml')),
              created_at   REAL    NOT NULL,
              updated_at   REAL    NOT NULL
            )
            """
        )
        # schedule_runs at ADR-0071 D4 shape (lease_attempts present) but
        # pre-dating the dispatched_at / resume_packet columns entirely.
        await raw.execute(
            """
            CREATE TABLE schedule_runs (
              id                  TEXT    PRIMARY KEY,
              schedule_id         TEXT    REFERENCES schedules(id) ON DELETE CASCADE,
              invocation_id       TEXT,
              trigger_context     JSON    NOT NULL,
              action_kind         TEXT    NOT NULL,
              action_args         JSON    NOT NULL,
              status              TEXT    NOT NULL DEFAULT 'running'
                                  CHECK(status IN ('queued', 'waiting_dependency', 'running',
                                                   'retry_wait', 'completed', 'failed',
                                                   'timed_out', 'skipped', 'cancelled')),
              exit_code           INTEGER,
              chain_parent_id     TEXT    REFERENCES schedule_runs(id),
              chain_depth         INTEGER NOT NULL DEFAULT 0,
              fired_at            REAL    NOT NULL,
              ended_at            REAL,
              error_detail        TEXT,
              created_at          REAL    NOT NULL,
              updated_at          REAL,
              status_reason_code     TEXT,
              status_reason_summary  TEXT,
              status_evidence_refs   JSON,
              queued_at           REAL,
              leased_by           TEXT,
              lease_expires_at    REAL,
              concurrency_key     TEXT,
              lease_attempts      INTEGER NOT NULL DEFAULT 0,
              required_capabilities  JSON,
              execution_target       TEXT,
              library_ref             TEXT,
              library_content_hash    TEXT
            )
            """
        )
        await raw.execute(
            "INSERT INTO schedules (id, name, trigger_type, action_kind, created_at, updated_at) "
            "VALUES ('sched-da', 'sched-da', 'interval', 'agent', 1.0, 1.0)"
        )
        # A row still genuinely running (or orphaned by an unrelated earlier
        # crash) at the moment of the upgrade -- fired_at is deliberately
        # far in the past so a naive "treat as undispatched" scan would be
        # the only thing standing between it and a duplicate re-fire.
        await raw.execute(
            "INSERT INTO schedule_runs "
            "(id, schedule_id, trigger_context, action_kind, action_args, status, "
            " chain_depth, fired_at, created_at) "
            "VALUES ('run-da-running', 'sched-da', '{}', 'agent', '{}', 'running', 0, 100.0, 100.0)"
        )
        # A row already terminal before the upgrade -- must NOT be touched
        # by the backfill (it was never a candidate for the recovery scan).
        await raw.execute(
            "INSERT INTO schedule_runs "
            "(id, schedule_id, trigger_context, action_kind, action_args, status, "
            " chain_depth, fired_at, created_at) "
            "VALUES ('run-da-completed', 'sched-da', '{}', 'agent', '{}', 'completed', 0, 50.0, 50.0)"
        )
        await raw.commit()

    state = StateDB(db_path)
    await state.open()
    try:
        async with state._read() as conn:
            running_row = (
                (
                    await conn.execute(
                        text("SELECT * FROM schedule_runs WHERE id = :id"),
                        {"id": "run-da-running"},
                    )
                )
                .mappings()
                .first()
            )
            completed_row = (
                (
                    await conn.execute(
                        text("SELECT * FROM schedule_runs WHERE id = :id"),
                        {"id": "run-da-completed"},
                    )
                )
                .mappings()
                .first()
            )
        assert running_row["dispatched_at"] == running_row["fired_at"] == 100.0
        assert completed_row["dispatched_at"] is None

        # The whole point of the backfill: the recovery scan must no longer
        # pick up the pre-existing running row as an undispatched orphan.
        orphans = await state.list_undispatched_schedule_runs()
        assert orphans == []
    finally:
        await state.close()
