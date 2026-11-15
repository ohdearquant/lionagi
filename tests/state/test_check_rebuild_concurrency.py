# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Legacy CHECK rebuilds share the migration transaction and its serialization lock."""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
import sqlite3
import traceback
from pathlib import Path

import pytest
from sqlalchemy import event

import lionagi.state.db as state_db_module
from lionagi.state.db import SCHEMA_VERSION, StateDB

_NUM_CONCURRENT_WORKERS = 4
_BARRIER_TIMEOUT_SECONDS = 15

# Legacy fixture builders


def _create_legacy_session_status_db(db_path: Path) -> None:
    """Full current schema, except sessions.status still carries the legacy
    4-value CHECK constraint that the migration rebuilds away.
    """
    from lionagi.state.db import _SCHEMA_PATH

    schema_sql = _SCHEMA_PATH.read_text()
    # Anchored on the surrounding comment (unique to the sessions table) so
    # this doesn't also match branches.status, which has the same bare
    # "status          TEXT," column definition.
    legacy_status_col = (
        "  status          TEXT,\n"
        "  started_at      REAL,\n"
        "  ended_at        REAL,\n"
        "  -- 1 means ended_at"
    )
    legacy_status_col_with_check = (
        "  status          TEXT CHECK(status IN "
        "('running', 'completed', 'failed', 'aborted')),\n"
        "  started_at      REAL,\n"
        "  ended_at        REAL,\n"
        "  -- 1 means ended_at"
    )
    assert schema_sql.count(legacy_status_col) == 1, (
        "sessions.status column definition not found (or found more than once) in schema.sql "
        "— schema.sql layout changed, update this fixture"
    )
    legacy_sql = schema_sql.replace(legacy_status_col, legacy_status_col_with_check, 1)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(legacy_sql)


def _create_legacy_invocations_status_db(db_path: Path) -> None:
    """Full current schema, except invocations.status still carries the
    legacy 6-value CHECK constraint that omits 'completed_empty'."""
    from lionagi.state.db import _SCHEMA_PATH

    schema_sql = _SCHEMA_PATH.read_text()
    current_check = (
        "  status          TEXT    NOT NULL DEFAULT 'running' CHECK(\n"
        "                    status IN ('running', 'completed', 'completed_empty',\n"
        "                               'failed', 'timed_out', 'aborted', 'cancelled')\n"
        "                  ),\n"
    )
    legacy_check = (
        "  status          TEXT    NOT NULL DEFAULT 'running' CHECK(\n"
        "                    status IN ('running', 'completed',\n"
        "                               'failed', 'timed_out', 'aborted', 'cancelled')\n"
        "                  ),\n"
    )
    assert schema_sql.count(current_check) == 1, (
        "invocations.status CHECK not found (or found more than once) in schema.sql "
        "— schema.sql layout changed, update this fixture"
    )
    legacy_sql = schema_sql.replace(current_check, legacy_check, 1)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(legacy_sql)


def _open_state_db_worker(
    db_path: str,
    start_barrier,
    writer_barrier,
    result_queue,
) -> None:
    """Have every opener reach the migration's writer reservation together."""
    original_make_engine = state_db_module.make_engine
    original_rebuild = StateDB._sqlite_rebuild_table
    rebuilt = []

    def synchronized_make_engine(*args, **kwargs):
        engine = original_make_engine(*args, **kwargs)
        waiting = True

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def synchronize(_conn, _cursor, statement, *_args):
            nonlocal waiting
            if waiting and statement == "BEGIN IMMEDIATE":
                waiting = False
                writer_barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)

        return engine

    async def record_rebuild(conn, table, *args, **kwargs):
        rebuilt.append(table)
        await original_rebuild(conn, table, *args, **kwargs)

    state_db_module.make_engine = synchronized_make_engine
    StateDB._sqlite_rebuild_table = staticmethod(record_rebuild)

    async def open_current_store():
        async with StateDB(db_path):
            pass

    try:
        start_barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
        asyncio.run(open_current_store())
    except Exception:  # noqa: BLE001 -- report child failure to parent
        result_queue.put((traceback.format_exc(), rebuilt))
    else:
        result_queue.put((None, rebuilt))


def test_concurrent_statedb_opens_rebuild_session_status_check(tmp_path: Path) -> None:
    """The waiting writers see the rebuilt CHECK and do not rebuild it again."""
    db_path = tmp_path / "concurrent-legacy-session-status.db"
    _create_legacy_session_status_db(db_path)
    context = multiprocessing.get_context("spawn")
    start_barrier = context.Barrier(_NUM_CONCURRENT_WORKERS + 1)
    writer_barrier = context.Barrier(_NUM_CONCURRENT_WORKERS)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_open_state_db_worker,
            args=(str(db_path), start_barrier, writer_barrier, result_queue),
        )
        for _ in range(_NUM_CONCURRENT_WORKERS)
    ]

    results = []
    try:
        for process in processes:
            process.start()
        start_barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
        for process in processes:
            process.join(timeout=30)
        for _ in processes:
            try:
                results.append(result_queue.get(timeout=2))
            except queue.Empty:
                results.append(("worker exited without reporting a result", []))
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        result_queue.close()
        result_queue.join_thread()

    assert all(process.exitcode == 0 for process in processes)
    assert [error for error, _ in results] == [None] * len(processes)
    assert [table for _, rebuilt in results for table in rebuilt] == ["sessions"]

    with sqlite3.connect(db_path) as conn:
        tables = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name LIKE 'sessions%'"
        ).fetchall()
    assert [name for name, _ in tables] == ["sessions"]
    assert "'running', 'completed', 'failed', 'aborted'" not in tables[0][1]


async def test_invocations_rebuild_crash_mid_sequence_rolls_back_atomically(
    tmp_path: Path, monkeypatch
):
    db_path = tmp_path / "legacy-invocations-crash.db"
    _create_legacy_invocations_status_db(db_path)
    with sqlite3.connect(db_path) as seed:
        seed.execute("UPDATE schema_meta SET value='5' WHERE key='version'")
        seed.execute(
            "INSERT INTO invocations (id, skill, started_at, created_at, updated_at, status)"
            " VALUES ('inv-1', 'demo', 0, 0, 0, 'running')"
        )

    original_make_engine = state_db_module.make_engine

    def crashing_make_engine(*args, **kwargs):
        engine = original_make_engine(*args, **kwargs)

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def fail_before_rename(_conn, _cursor, statement, *_args):
            if statement == "ALTER TABLE invocations_new RENAME TO invocations":
                raise RuntimeError("injected failure during invocations rebuild")

        return engine

    with monkeypatch.context() as patched:
        patched.setattr(state_db_module, "make_engine", crashing_make_engine)
        state = StateDB(db_path)
        try:
            with pytest.raises(RuntimeError, match="injected failure during invocations rebuild"):
                await state.migrate()
        finally:
            await state.close()

    with sqlite3.connect(db_path) as check:
        tables = check.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name LIKE 'invocations%'"
        ).fetchall()
        assert [name for name, _ in tables] == ["invocations"]
        assert "completed_empty" not in tables[0][1]
        assert check.execute("SELECT id FROM invocations").fetchall() == [("inv-1",)]
        assert check.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone() == (
            "5",
        )

    state = StateDB(db_path)
    try:
        await state.migrate()
        async with state._read() as conn:
            assert (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
            assert (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all() == []
            sql = (
                await conn.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='invocations'"
                )
            ).scalar_one()
        assert "'completed_empty'" in sql
        assert await state.schema_version() == SCHEMA_VERSION
    finally:
        await state.close()
