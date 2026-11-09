# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the shared state store helpers and configuration."""

from __future__ import annotations

import logging

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.state import engine as eng
from lionagi.state.db import StateDB
from lionhub.orchestration.state_store import (
    StoreNotAddressableError,
    require_file_store,
    store_exists,
    store_path,
)
from lionhub_tests.orchestration._test_support_state_store import (
    ENV,
    _configure,
    _run,
    _seed,
)


class TestOpenDb:
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

    @pytest.mark.integration
    def test_open_db_sets_wal_mode(self, tmp_path):
        """open_db() must switch the connection to WAL journal mode."""
        from lionhub.orchestration.state_store import open_db

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
        from lionhub.orchestration.state_store import open_db

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
        from lionhub.orchestration.state_store import open_db

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
        from lionhub.orchestration.state_store import open_db

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

        from lionhub.orchestration.state_store import open_db

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

        from lionhub.orchestration.state_store import open_db

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
        from lionhub.orchestration.state_store import open_db

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
        from lionhub.orchestration.state_store import open_db

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


def test_existing_store_path_does_not_raise(tmp_path, monkeypatch):
    """The accept arm: an existing file-backed store is left alone."""
    default = tmp_path / "state.db"
    _run(StateDB(default).open())
    _configure(monkeypatch, default=default, url=None)

    require_file_store()  # must not raise


def test_missing_store_file_does_not_raise(tmp_path, monkeypatch):
    """A store that simply has not been created yet is not the same as one
    this layer cannot reach -- the existing empty-result behavior stays."""
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url=None)
    assert not default.exists()

    require_file_store()  # must not raise


def test_server_backed_store_raises(tmp_path, monkeypatch):
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url="postgresql://user:secret@host/db")

    with pytest.raises(StoreNotAddressableError) as exc_info:
        require_file_store()
    assert exc_info.value.backend == "postgresql"


def test_in_memory_store_raises(tmp_path, monkeypatch):
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url=":memory:")

    with pytest.raises(StoreNotAddressableError) as exc_info:
        require_file_store()
    assert "memory" in exc_info.value.backend


def test_unconfigured_deployment_resolves_the_default_path(tmp_path, monkeypatch):
    """With nothing configured, this is the path the services always used."""
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url=None)

    assert store_path() == str(default)
    assert store_exists() is False

    _run(_seed(default, "s"))
    assert store_path() == str(default)
    assert store_exists() is True


def test_configured_store_moves_the_path_the_services_open(tmp_path, monkeypatch):
    default = tmp_path / "default_state.db"
    configured = tmp_path / "configured_state.db"
    _run(_seed(default, "in-the-default-store"))
    _run(_seed(configured, "in-the-configured-store"))

    _configure(monkeypatch, default=default, url=str(configured))

    assert store_path() == str(configured)


def test_a_configured_store_is_never_created_by_reading_it(tmp_path, monkeypatch):
    """Resolution alone must not bring a store into existence."""
    default = tmp_path / "default_state.db"
    configured = tmp_path / "not_there_yet.db"
    _run(_seed(default, "in-the-default-store"))

    _configure(monkeypatch, default=default, url=str(configured))

    assert store_exists() is False
    assert not configured.exists()


class TestBothStoreEntryPointsAnnounce:
    """The engine and the Studio connection helper are two independent ways
    into one store file. A process that only used the second would never say
    which timeout it had."""

    @pytest.mark.asyncio
    async def test_the_studio_connection_helper_announces(self, monkeypatch, tmp_path, caplog):
        import lionhub.orchestration.state_store as state_store

        monkeypatch.setattr(eng, "_busy_timeout_announced", False)
        monkeypatch.delenv(ENV, raising=False)

        caplog.clear()
        with caplog.at_level(logging.INFO, logger=eng._log.name):
            async with state_store.open_db(str(tmp_path / "studio.db")):
                pass

        assert any("busy_timeout" in r.getMessage() for r in caplog.records)
