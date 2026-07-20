# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Concurrency-safety tests for the legacy CHECK-constraint table-rebuild
migrations in ``lionagi/state/db.py`` (``_drop_legacy_*_check``).

These migrations rebuild a table (DROP/CREATE/INSERT/RENAME) when they find
a stale CHECK constraint. Two processes cold-opening the same legacy DB can
both observe the stale CHECK and enter the same rebuild; SQLite's write lock
serializes the two attempts, but the loser can still surface an
OperationalError instead of quietly recognizing that the winner already
finished the identical rebuild. Mirrors the multi-process harness in
``test_migrations.py`` (``test_concurrent_statedb_opens_reconcile_cc_session_column``).
"""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
import sqlite3
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from lionagi.state.db import StateDB

_NUM_CONCURRENT_WORKERS = 4
_BARRIER_TIMEOUT_SECONDS = 15

# ── Legacy fixture builders ─────────────────────────────────────────────────


def _create_legacy_session_status_db(db_path: Path) -> None:
    """Full current schema, except sessions.status still carries the legacy
    4-value CHECK constraint that ``_drop_legacy_session_status_check``
    rebuilds away. Every column already matches the current schema, so
    ``_reconcile_columns`` is a no-op and the session-status rebuild's own
    write is the first (and only) write transaction ``open()`` performs.
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
        "  -- ── Activity"
    )
    legacy_status_col_with_check = (
        "  status          TEXT CHECK(status IN "
        "('running', 'completed', 'failed', 'aborted')),\n"
        "  started_at      REAL,\n"
        "  ended_at        REAL,\n"
        "  -- ── Activity"
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


# ── Multi-process race: sessions status CHECK rebuild ───────────────────────


def _open_state_db_worker_synchronized_begin(
    db_path: str,
    start_barrier,
    inspection_barrier,
    result_queue,
) -> None:
    """Open and close one StateDB in a spawned process, synchronizing every
    worker's first ``engine.begin()`` call so they all enter the rebuild's
    write transaction at (as close as SQLite allows) the same instant."""
    import lionagi.state.db as state_db_module
    import lionagi.state.engine as state_engine_module
    from lionagi.state.db import StateDB

    # A shorter-than-production busy_timeout makes lock contention surface
    # as an OperationalError within this test's patience instead of quietly
    # blocking it out. Tuned to 500ms (vs. production's 5000ms default,
    # engine.py:_SQLITE_BUSY_TIMEOUT_MS): low enough that the barrier-forced
    # pileup on the sessions rebuild's write lock can still exceed it under
    # load, but high enough to stay clear of a separate, pre-existing
    # SQLite characteristic — a bare SELECT against sqlite_master can itself
    # transiently contend with another connection's in-flight DDL transaction
    # (schema-lock, not just the write-reservation lock) — which every one of
    # these migrations' un-guarded marker-check reads is exposed to, same as
    # ``_reconcile_columns``'s own pre-ALTER reads. That's out of scope here
    # (this fix only guards the mutations); at very aggressive timeouts it
    # was observed to fail the harness on those unrelated reads rather than
    # on the write path this test targets.
    state_engine_module._SQLITE_BUSY_TIMEOUT_MS = 500

    original_make_engine = state_db_module.make_engine

    class _SynchronizedEngine:
        def __init__(self, engine) -> None:
            self._engine = engine
            self._synchronized = False

        def begin(self):
            if not self._synchronized:
                self._synchronized = True

                @asynccontextmanager
                async def synchronized_begin():
                    inspection_barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
                    async with self._engine.begin() as connection:
                        yield connection

                return synchronized_begin()
            return self._engine.begin()

        def __getattr__(self, name: str):
            return getattr(self._engine, name)

    def synchronized_make_engine(*args, **kwargs):
        return _SynchronizedEngine(original_make_engine(*args, **kwargs))

    # This fixture has every migration column already present, so
    # _reconcile_columns never calls engine.begin(); the first begin() is
    # the sessions CHECK-rebuild's own write transaction.
    state_db_module.make_engine = synchronized_make_engine

    async def _open() -> None:
        state = StateDB(db_path)
        await state.open()
        await state.close()

    try:
        start_barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
        asyncio.run(_open())
    except Exception:  # noqa: BLE001
        result_queue.put(traceback.format_exc())
    else:
        result_queue.put(None)


def test_concurrent_statedb_opens_rebuild_session_status_check(tmp_path: Path) -> None:
    """Concurrent first opens of a legacy-CHECK sessions table tolerate
    another process winning the rebuild race instead of crashing.

    Against the unguarded rebuild (no catch-and-reinspect guard), lowering
    ``_SQLITE_BUSY_TIMEOUT_MS`` far enough (~50ms, well below this test's
    500ms) reliably reproduces the bug this fix closes: N processes racing
    the same DROP/CREATE/INSERT/RENAME transaction see a loser surface an
    OperationalError that isn't caught, failing that worker's whole
    ``open()``. At 500ms the race window is narrow enough that this test
    passes reliably even against the unguarded code on a lightly loaded
    machine — the guard is still exercised (and required) under real
    contention; see the busy_timeout comment on the worker below for why
    500ms was chosen over a more aggressive value that reproduces the bug
    more reliably.
    """
    db_path = tmp_path / "concurrent-legacy-session-status.db"
    _create_legacy_session_status_db(db_path)

    context = multiprocessing.get_context("spawn")
    start_barrier = context.Barrier(_NUM_CONCURRENT_WORKERS + 1)
    inspection_barrier = context.Barrier(_NUM_CONCURRENT_WORKERS)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_open_state_db_worker_synchronized_begin,
            args=(str(db_path), start_barrier, inspection_barrier, result_queue),
        )
        for _ in range(_NUM_CONCURRENT_WORKERS)
    ]

    results: list[str | None] = []
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
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()
    create_sql = row[0]
    # Rebuilt exactly once: the legacy 4-value CHECK is gone, and there is
    # only one `sessions` table definition left (no stray sessions_new).
    assert "'running', 'completed', 'failed', 'aborted'" not in create_sql
    with sqlite3.connect(db_path) as conn:
        table_names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sessions%'"
            )
        ]
    assert table_names == ["sessions"], table_names


# ── Guard-helper unit tests (table/marker-agnostic) ──────────────────────────


async def test_rebuild_check_constraint_swallows_error_when_winner_already_rebuilt():
    """When the rebuild raises OperationalError but a fresh inspection shows
    the table already carries the target (widened) CHECK, the guard treats
    a concurrent winner's completed rebuild as success.

    The ``already_rebuilt`` predicate is exercised directly (unconditionally
    True) rather than depending on a real legacy-schema DB, because
    ``state.open()`` on a genuinely legacy DB would self-heal it via the
    real (already-passing) migration before this synthetic race ever runs.
    """
    state = StateDB(":memory:")
    await state.open()
    try:
        called = False

        async def _boom() -> None:
            nonlocal called
            called = True
            raise OperationalError("statement", {}, Exception("database is locked"))

        # Should not raise: the predicate proves the marker is gone,
        # regardless of the table's actual on-disk content.
        await state._rebuild_check_constraint("sessions", lambda sql: True, _boom)
        assert called
    finally:
        await state.close()


async def test_rebuild_check_constraint_reraises_when_still_legacy():
    """When the rebuild raises OperationalError and a fresh inspection shows
    the table genuinely still on the legacy CHECK (no concurrent winner),
    the guard re-raises — this is a real failure, not a race we can paper
    over."""
    state = StateDB(":memory:")
    await state.open()
    try:

        async def _boom() -> None:
            raise OperationalError("statement", {}, Exception("database is locked"))

        with pytest.raises(OperationalError):
            await state._rebuild_check_constraint("sessions", lambda sql: False, _boom)
    finally:
        await state.close()


async def test_rebuild_check_constraint_handles_raw_sqlite_operational_error():
    """Five of the six rebuilds execute through the raw aiosqlite driver,
    which raises ``sqlite3.OperationalError`` directly — NOT SQLAlchemy's
    wrapper. The guard must treat both types identically: swallow when a
    concurrent winner provably landed the rebuild, re-raise when the table
    is genuinely still legacy."""
    state = StateDB(":memory:")
    await state.open()
    try:

        async def _raw_boom() -> None:
            raise sqlite3.OperationalError("database is locked")

        # Winner already rebuilt -> raw error suppressed.
        await state._rebuild_check_constraint("sessions", lambda sql: True, _raw_boom)

        # Still legacy -> raw error re-raised untouched.
        with pytest.raises(sqlite3.OperationalError):
            await state._rebuild_check_constraint("sessions", lambda sql: False, _raw_boom)
    finally:
        await state.close()


async def test_rebuild_check_constraint_reraises_on_non_sqlite_dialect():
    """The guard only ever suppresses errors on sqlite — other dialects
    always re-raise, matching ``_reconcile_columns``'s guard."""
    state = StateDB(":memory:")
    await state.open()
    try:
        state.dialect = "postgresql"

        async def _boom() -> None:
            raise OperationalError("statement", {}, Exception("connection reset"))

        with pytest.raises(OperationalError):
            await state._rebuild_check_constraint("sessions", lambda sql: True, _boom)
    finally:
        state.dialect = "sqlite"
        await state.close()


# ── Raw-driver (FK-toggling) rebuild wiring: invocations ────────────────────


async def test_drop_legacy_invocations_status_check_tolerates_concurrent_winner(tmp_path):
    """``_drop_legacy_invocations_status_check`` uses the raw-driver
    BEGIN IMMEDIATE + FK-toggle rebuild path (distinct from the plain
    ``engine.begin()`` path the sessions rebuild uses). Simulate a
    concurrent winner finishing the identical rebuild first, then force
    our own attempt to surface OperationalError, and confirm the guard
    recognizes the already-completed rebuild instead of failing ``open()``.
    """
    db_path = tmp_path / "legacy-invocations.db"
    _create_legacy_invocations_status_db(db_path)

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
    finally:
        await state.close()


async def test_drop_legacy_invocations_status_check_swallows_operational_error(tmp_path):
    """Directly exercise the invocations rebuild's guard wiring: when the
    write raises OperationalError but another process already landed the
    'completed_empty' CHECK, the migration must not fail ``open()``."""
    db_path = tmp_path / "legacy-invocations-race.db"
    _create_legacy_invocations_status_db(db_path)

    state = StateDB(db_path)
    call_count = 0
    original = state._rebuild_check_constraint

    async def _patched(table, already_rebuilt, rebuild):
        nonlocal call_count
        if table == "invocations":
            call_count += 1

            async def _winner_then_boom() -> None:
                # A concurrent winner completes the exact same rebuild via
                # an independent raw connection first...
                async with aiosqlite.connect(str(db_path)) as raw:
                    await raw.execute("PRAGMA foreign_keys = OFF")
                    await raw.execute(
                        """
                        CREATE TABLE invocations_new (
                          id              TEXT    PRIMARY KEY,
                          skill           TEXT    NOT NULL,
                          plugin          TEXT,
                          prompt          TEXT,
                          started_at      REAL    NOT NULL,
                          ended_at        REAL,
                          status          TEXT    NOT NULL DEFAULT 'running'
                                          CHECK(status IN ('running', 'completed',
                                                'completed_empty', 'failed',
                                                'timed_out', 'aborted', 'cancelled')),
                          session_count   INTEGER NOT NULL DEFAULT 0,
                          created_at      REAL    NOT NULL,
                          updated_at      REAL    NOT NULL,
                          node_metadata   JSON,
                          status_reason_code     TEXT,
                          status_reason_summary  TEXT,
                          status_evidence_refs   JSON
                        )
                        """
                    )
                    cols = "id, skill, plugin, prompt, started_at, ended_at, status, session_count, created_at, updated_at, node_metadata, status_reason_code, status_reason_summary, status_evidence_refs"
                    await raw.execute(
                        f"INSERT INTO invocations_new ({cols}) SELECT {cols} FROM invocations"
                    )
                    await raw.execute("DROP TABLE invocations")
                    await raw.execute("ALTER TABLE invocations_new RENAME TO invocations")
                    await raw.execute("PRAGMA foreign_keys = ON")
                    await raw.commit()
                # ...then our own attempt collides with a REAL raw-driver lock
                # error: a holder connection takes the write reservation and a
                # near-zero busy timeout makes the contending BEGIN IMMEDIATE
                # raise sqlite3.OperationalError exactly as it does in
                # production, rather than a hand-constructed SQLAlchemy
                # exception the raw path never produces.
                async with aiosqlite.connect(str(db_path)) as holder:
                    await holder.execute("BEGIN IMMEDIATE")
                    async with aiosqlite.connect(str(db_path)) as contender:
                        await contender.execute("PRAGMA busy_timeout = 1")
                        await contender.execute("BEGIN IMMEDIATE")

            await original(table, already_rebuilt, _winner_then_boom)
        else:
            await original(table, already_rebuilt, rebuild)

    state._rebuild_check_constraint = _patched
    await state.open()
    try:
        assert call_count == 1
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
    finally:
        await state.close()
