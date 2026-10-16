# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared _db helper: WAL mode, busy_timeout, row_factory."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from tests.apps_studio_server._helpers import run_async as _run  # noqa: E402

# open_db() configures busy_timeout, WAL, and row_factory


class TestOpenDb:
    @pytest.mark.integration
    def test_open_db_sets_wal_mode(self, tmp_path):
        """open_db() must switch the connection to WAL journal mode."""
        from lionagi.studio.services._db import open_db

        db_path = str(tmp_path / "test.db")

        async def _check():
            async with open_db(db_path) as db:
                cur = await db.execute("PRAGMA journal_mode")
                row = await cur.fetchone()
            return row[0]

        mode = _run(_check())
        assert mode == "wal", f"Expected WAL journal mode, got {mode!r}"

    @pytest.mark.integration
    def test_open_db_sets_busy_timeout(self, tmp_path):
        """open_db() must set a busy_timeout, and the shared one."""
        from lionagi.state.engine import SQLITE_BUSY_TIMEOUT_MS
        from lionagi.studio.services._db import open_db

        db_path = str(tmp_path / "test.db")

        async def _check():
            async with open_db(db_path) as db:
                cur = await db.execute("PRAGMA busy_timeout")
                row = await cur.fetchone()
            return row[0]

        timeout = _run(_check())
        assert timeout == SQLITE_BUSY_TIMEOUT_MS, (
            f"Expected busy_timeout={SQLITE_BUSY_TIMEOUT_MS}, got {timeout!r}"
        )

    @pytest.mark.integration
    def test_open_db_waits_as_long_as_the_state_engine_does(self, tmp_path):
        """The two connection layers onto one store file must wait the same time
        on a lock.

        Asserted between two live connections rather than against the constant,
        so that re-hardcoding a number in either layer fails here even if it
        happens to be spelled the same as the constant's current value.
        """
        pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed")
        from lionagi.state.engine import make_engine
        from lionagi.studio.services._db import open_db

        db_path = tmp_path / "test.db"

        async def _check():
            async with open_db(str(db_path)) as db:
                cur = await db.execute("PRAGMA busy_timeout")
                helper_value = (await cur.fetchone())[0]
            engine = make_engine(f"sqlite+aiosqlite:///{db_path}")
            try:
                async with engine.connect() as conn:
                    result = await conn.exec_driver_sql("PRAGMA busy_timeout")
                    engine_value = result.scalar()
            finally:
                await engine.dispose()
            return helper_value, engine_value

        helper_value, engine_value = _run(_check())
        assert helper_value == engine_value, (
            f"Studio helper waits {helper_value}ms, StateDB engine waits {engine_value}ms"
        )

    @pytest.mark.integration
    def test_open_db_sets_row_factory(self, tmp_path):
        """open_db() must set row_factory so rows are accessible by column name."""
        from lionagi.studio.services._db import open_db

        db_path = str(tmp_path / "test.db")

        async def _check():
            async with open_db(db_path) as db:
                await db.execute("CREATE TABLE t (a TEXT, b INTEGER)")
                await db.execute("INSERT INTO t VALUES ('hello', 42)")
                await db.commit()
                cur = await db.execute("SELECT a, b FROM t")
                row = await cur.fetchone()
            return row

        row = _run(_check())
        assert row is not None
        assert row["a"] == "hello"
        assert row["b"] == 42

    def test_sessions_service_uses_open_db(self):
        """sessions.py must import and use _open_db (not bare aiosqlite.connect)."""
        import inspect

        import lionagi.studio.services.sessions as sessions_mod

        src = inspect.getsource(sessions_mod)
        # Verify _open_db is imported
        assert "_open_db" in src, "sessions.py must import open_db as _open_db"
        # Verify the module no longer uses bare aiosqlite.connect() calls
        # (the only remaining reference to aiosqlite should be for the Row
        # sentinel or type annotations, not for .connect())
        for line in src.splitlines():
            stripped = line.strip()
            if "aiosqlite.connect(" in stripped and not stripped.startswith("#"):
                raise AssertionError(f"sessions.py still uses bare aiosqlite.connect(): {line!r}")

    def test_shows_service_uses_open_db(self):
        """shows.py must import and use _open_db (not bare aiosqlite.connect)."""
        import inspect

        import lionagi.studio.services.shows as shows_mod

        src = inspect.getsource(shows_mod)
        assert "_open_db" in src, "shows.py must import open_db as _open_db"
        for line in src.splitlines():
            stripped = line.strip()
            if "aiosqlite.connect(" in stripped and not stripped.startswith("#"):
                raise AssertionError(f"shows.py still uses bare aiosqlite.connect(): {line!r}")

    @pytest.mark.asyncio
    async def test_a_write_cancelled_mid_transaction_releases_the_write_lock(self, tmp_path):
        """A write cancelled between its ``BEGIN IMMEDIATE`` and its ``COMMIT`` must not
        leave the store write-locked.

        SQLite keeps a connection's write lock until the transaction ends, and it defers
        the close that would end it while any statement of the connection is still
        unfinalized. A cancelled write leaves both behind: the transaction is never
        committed or rolled back, and the cursor it was reading through is still open, so
        closing the connection alone leaves the lock held -- the next writer waits out its
        whole busy timeout and fails with "database is locked". The helper must end the
        transaction as the block exits, before the connection is closed.
        """
        import asyncio

        from lionagi.studio.services._db import open_db

        db_path = str(tmp_path / "test.db")
        parked = asyncio.Event()

        async def _cancelled_write():
            async with open_db(db_path) as db:
                await db.execute("CREATE TABLE t (a)")
                await db.commit()
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("INSERT INTO t VALUES (1)")
                await db.execute("INSERT INTO t VALUES (2)")
                cursor = await db.execute("SELECT a FROM t")
                # One of two rows read: the statement stays unfinalized, as the frame a
                # cancel unwinds through leaves it.
                await cursor.fetchone()
                parked.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(_cancelled_write())
        await asyncio.wait_for(parked.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert self._write_lock_is_free(db_path), (
            "the store is still write-locked after a cancelled write"
        )

    @pytest.mark.asyncio
    async def test_a_write_cancelled_before_its_begin_has_run_releases_the_write_lock(
        self, tmp_path
    ):
        """A cancel can arrive while the ``BEGIN IMMEDIATE`` is still queued for the
        connection's worker thread. Read on the event loop at that moment the connection is
        in no transaction, and the queued BEGIN opens one once the worker reaches it, so the
        helper has to end the transaction in the worker's order rather than decide from that
        earlier reading."""
        import asyncio
        import threading

        from lionagi.studio.services._db import open_db

        db_path = str(tmp_path / "test.db")
        gate = threading.Event()
        queued = asyncio.Event()

        async def _cancelled_write():
            async with open_db(db_path) as db:
                await db.execute("CREATE TABLE t (a)")
                await db.commit()
                # Keeps the worker busy until the gate opens, so the BEGIN below is still
                # queued when the cancel arrives; its cursor is left unread.
                await db.create_function("hold", 0, lambda: 1 if gate.wait(10) else 0)
                holder = asyncio.ensure_future(db.execute("SELECT hold()"))
                await asyncio.sleep(0)
                begin = db.execute("BEGIN IMMEDIATE")
                queued.set()
                await begin
                await holder
                await asyncio.Event().wait()

        task = asyncio.create_task(_cancelled_write())
        await asyncio.wait_for(queued.wait(), timeout=5)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

        assert self._write_lock_is_free(db_path), (
            "the store is still write-locked after a write cancelled before its BEGIN ran"
        )

    @pytest.mark.asyncio
    async def test_a_write_that_returns_inside_its_transaction_releases_the_write_lock(
        self, tmp_path
    ):
        """A body that returns between its ``BEGIN IMMEDIATE`` and its ``COMMIT``, a read
        still open, leaves no write lock behind, and its writes are rolled back."""
        from lionagi.studio.services._db import open_db

        db_path = str(tmp_path / "test.db")
        async with open_db(db_path) as db:
            await db.execute("CREATE TABLE t (a)")
            await db.commit()
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("INSERT INTO t VALUES (1)")
            await db.execute("INSERT INTO t VALUES (2)")
            cursor = await db.execute("SELECT a FROM t")
            await cursor.fetchone()

        assert self._write_lock_is_free(db_path)
        assert self._rows(db_path) == 0

    @pytest.mark.asyncio
    async def test_a_write_that_raises_inside_its_transaction_releases_the_write_lock(
        self, tmp_path
    ):
        """A body that raises between its ``BEGIN IMMEDIATE`` and its ``COMMIT`` leaves no
        write lock behind, its writes are rolled back, and its own error reaches the caller."""
        from lionagi.studio.services._db import open_db

        db_path = str(tmp_path / "test.db")
        with pytest.raises(RuntimeError, match="the body failed"):
            async with open_db(db_path) as db:
                await db.execute("CREATE TABLE t (a)")
                await db.commit()
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("INSERT INTO t VALUES (1)")
                await db.execute("INSERT INTO t VALUES (2)")
                cursor = await db.execute("SELECT a FROM t")
                await cursor.fetchone()
                raise RuntimeError("the body failed")

        assert self._write_lock_is_free(db_path)
        assert self._rows(db_path) == 0

    @staticmethod
    def _rows(db_path: str) -> int:
        """How many rows table ``t`` holds, read by a fresh connection."""
        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("SELECT count(*) FROM t").fetchone()[0]
        finally:
            conn.close()

    @staticmethod
    def _write_lock_is_free(db_path: str) -> bool:
        """Whether a fresh connection can take the store's write lock right now."""
        import sqlite3

        conn = sqlite3.connect(db_path, isolation_level=None, timeout=0)
        try:
            conn.execute("PRAGMA busy_timeout = 0")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()
