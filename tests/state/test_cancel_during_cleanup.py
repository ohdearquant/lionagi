# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A second cancel that lands while a StateDB read or write is being cleaned up.

The first cancel makes SQLAlchemy discard the connection and close it gracefully,
which waits for the statement still running on the driver thread. A second cancel
interrupts that wait, and SQLAlchemy falls back to stopping the driver. These tests
pin that the read or write still ends as a cancellation, the pooled connection and
StateDB's write lock are released, and no cleanup task is left behind. A cancelled
transaction must also release SQLite's own write lock while the task and its
traceback still retain the unfinished cursor.

aiosqlite releases before 0.22.1 have no ``stop()``, so SQLAlchemy cannot fall back
to one and its graceful close ends by itself a moment later. The StateDB tests run
there too; the tests of the driver connection itself are skipped.

Without the fix a cleanup task is left waiting forever, so each test gets its own
event loop and a fixture that cancels whatever a test leaves running until it
ends. A failing test runs its teardown after pytest's timeouts are cancelled,
and the loop's own teardown cancels a task only once, so without the fixture a
regression would hang the run instead of failing the test.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.dialects.sqlite.aiosqlite import (
    AsyncAdapt_aiosqlite_connection,
    AsyncAdapt_aiosqlite_dbapi,
)
from sqlalchemy.util.concurrency import greenlet_spawn

import lionagi.state.engine as engine_mod
from lionagi.state.db import StateDB
from lionagi.state.engine import _stop_aiosqlite_once

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.timeout(60)]

_needs_stop = pytest.mark.skipif(
    not hasattr(aiosqlite.Connection, "stop"),
    reason="aiosqlite before 0.22.1 has no stop(), so its connections are left alone",
)


async def _wait_until(predicate, *, timeout: float = 5.0) -> bool:
    """Wait for *predicate* to hold, and return whether it did; the caller asserts, so a
    failure can say what it found."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def _take(lock) -> None:
    async with lock:
        pass


async def _cancel_twice(task: asyncio.Task) -> None:
    """Cancel, let the task reach SQLAlchemy's graceful close, then cancel again."""
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)


def _other_tasks() -> list[str]:
    current = asyncio.current_task()
    return [t.get_coro().__qualname__ for t in asyncio.all_tasks() if t is not current]


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _end_left_over_tasks():
    yield
    # The loop's own teardown cancels each task once. A cleanup task waiting on
    # a stopped driver can need two: one for the wait it is in, and one for the
    # stop that aiosqlite's close() then waits on in its finally.
    current = asyncio.current_task()
    for _ in range(3):
        left = [t for t in asyncio.all_tasks() if t is not current]
        if not left:
            return
        for task in left:
            task.cancel()
        await asyncio.wait(left, timeout=1)


class _Blocker:
    """A SQL function that holds the driver thread until the test releases it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> int:
        self.entered.set()
        self.release.wait(10)
        return 1


async def _install_blocker(db: StateDB) -> _Blocker:
    blocker = _Blocker()
    event.listen(
        db._engine.sync_engine,
        "connect",
        lambda dbapi_conn, _rec: dbapi_conn.create_function("block", 0, blocker),
    )
    await db._engine.dispose()  # the next checkout opens a connection that has block()
    return blocker


async def _cancel_twice_while_blocked(blocker: _Blocker, coro) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    try:
        assert await _wait_until(blocker.entered.is_set), "the statement never reached block()"
        await _cancel_twice(task)
    finally:
        blocker.release.set()
    await asyncio.wait({task}, timeout=10)
    return task


@_needs_stop
async def test_close_finishes_when_stop_was_called_while_it_was_queued(tmp_path: Path):
    blocker = _Blocker()
    conn = await aiosqlite.connect(str(tmp_path / "driver.db"))
    running = None
    try:
        _stop_aiosqlite_once(conn)
        await conn.create_function("block", 0, blocker)

        running = asyncio.ensure_future(conn.execute_fetchall("SELECT block()"))
        assert await _wait_until(blocker.entered.is_set), "the statement never reached block()"
        closing = asyncio.ensure_future(conn.close())
        await asyncio.sleep(0)  # close() queues its own close behind the running statement
        conn.stop()  # what SQLAlchemy falls back to when its wait for close() is cancelled
    finally:
        blocker.release.set()
        conn.stop()  # its worker thread ends after the running statement; a repeat stop is a no-op
        if running is not None:
            await asyncio.wait({running}, timeout=5)

    done, _ = await asyncio.wait({closing, running}, timeout=5)

    assert closing in done
    assert running in done

    await _wait_until(lambda: _other_tasks() == [])
    assert _other_tasks() == []


@_needs_stop
async def test_work_queued_before_a_stop_still_runs(tmp_path: Path):
    blocker = _Blocker()
    conn = await aiosqlite.connect(str(tmp_path / "driver.db"))
    running = None
    try:
        _stop_aiosqlite_once(conn)
        await conn.create_function("block", 0, blocker)

        running = asyncio.ensure_future(conn.execute_fetchall("SELECT block()"))
        assert await _wait_until(blocker.entered.is_set), "the statement never reached block()"
        queued = asyncio.ensure_future(conn.execute_fetchall("SELECT 2"))
        await asyncio.sleep(0)  # queued behind the running statement, before the stop
        stopped = conn.stop()
    finally:
        blocker.release.set()
        conn.stop()  # its worker thread ends after the running statement; a repeat stop is a no-op
        if running is not None:
            await asyncio.wait({running}, timeout=5)
    assert stopped is not None

    done, _ = await asyncio.wait({running, queued, stopped}, timeout=5)

    assert done == {running, queued, stopped}
    assert queued.result() == [(2,)]

    await _wait_until(lambda: _other_tasks() == [])
    assert _other_tasks() == []


@_needs_stop
async def test_setting_the_isolation_level_after_a_stop_is_refused_at_once(tmp_path: Path):
    # SQLAlchemy sets the isolation level by queueing work on the driver directly,
    # not through the method that refuses work after a stop.
    blocker = _Blocker()
    conn = await aiosqlite.connect(str(tmp_path / "driver.db"))
    running = None
    try:
        _stop_aiosqlite_once(conn)
        await conn.create_function("block", 0, blocker)
        adapter = AsyncAdapt_aiosqlite_connection(
            AsyncAdapt_aiosqlite_dbapi(aiosqlite, sqlite3), conn
        )

        running = asyncio.ensure_future(conn.execute_fetchall("SELECT block()"))
        assert await _wait_until(blocker.entered.is_set), "the statement never reached block()"
        conn.stop()  # queued behind the running statement
        with pytest.raises(ValueError, match="no active connection"):
            await asyncio.wait_for(greenlet_spawn(setattr, adapter, "isolation_level", None), 2)
    finally:
        blocker.release.set()
        conn.stop()  # its worker thread ends after the running statement; a repeat stop is a no-op
        if running is not None:
            await asyncio.wait({running}, timeout=5)

    await _wait_until(lambda: _other_tasks() == [])
    assert _other_tasks() == []


async def test_two_cancels_inside_a_transaction_end_as_a_cancellation(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    try:
        await db.open()
        await db.execute("CREATE TABLE probe (x INTEGER)")
        blocker = await _install_blocker(db)

        async def write() -> None:
            async with db.transaction() as conn:
                await conn.execute(text("INSERT INTO probe (x) VALUES (1)"))
                await conn.execute(text("SELECT block()"))

        task = await _cancel_twice_while_blocked(blocker, write())

        assert task.done()
        assert task.cancelled()
        assert db._engine.pool.checkedout() == 0
        await asyncio.wait_for(_take(db._write_lock), 1)
        await _wait_until(lambda: _other_tasks() == [])
        assert _other_tasks() == []
    finally:
        await db.close()


@pytest.mark.parametrize("cancels", [1, 2], ids=["single-cancel", "second-cancel"])
async def test_cancelled_transaction_releases_sqlite_lock_with_its_traceback_retained(
    tmp_path: Path, cancels: int
):
    """Completed cancellation must release SQLite's lock before traceback collection."""
    path = tmp_path / "state.db"
    db = StateDB(path)
    blocker = None
    task = None
    retained = []
    try:
        await db.open()
        await db.execute("CREATE TABLE probe (x INTEGER)")
        blocker = await _install_blocker(db)

        async def write() -> None:
            try:
                async with db.transaction() as conn:
                    await conn.execute(text("INSERT INTO probe (x) VALUES (1)"))
                    await conn.execute(text("SELECT block()"))
            except asyncio.CancelledError as exc:
                retained.append(exc)
                raise

        task = asyncio.create_task(write())
        assert await _wait_until(blocker.entered.is_set), "statement never reached block()"
        for _ in range(cancels):
            task.cancel()
            await asyncio.sleep(0.05)
        blocker.release.set()
        done, _ = await asyncio.wait({task}, timeout=10)
        assert task in done and task.cancelled()
        assert retained and retained[0].__traceback__ is not None
        assert db._engine.pool.checkedout() == 0

        # This is SQLite's own writer reservation, independent of the pool and
        # StateDB's Python lock. Keep both the task and its exception alive.
        external = sqlite3.connect(path, timeout=0, isolation_level=None)
        try:
            external.execute("BEGIN IMMEDIATE")
            external.execute("ROLLBACK")
        finally:
            external.close()
        assert await db.fetch_all("SELECT x FROM probe") == []
        await db.execute("INSERT INTO probe (x) VALUES (2)")
        assert await db.fetch_all("SELECT x FROM probe") == [{"x": 2}]
    finally:
        if blocker is not None:
            blocker.release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=10)
        await db.close()


async def test_two_cancels_inside_a_read_snapshot_end_as_a_cancellation(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    try:
        await db.open()
        blocker = await _install_blocker(db)

        async def read() -> None:
            async with db.read_snapshot() as conn:
                await conn.execute(text("SELECT block()"))

        task = await _cancel_twice_while_blocked(blocker, read())

        assert task.done()
        assert task.cancelled()
        assert db._engine.pool.checkedout() == 0
        await _wait_until(lambda: _other_tasks() == [])
        assert _other_tasks() == []
    finally:
        await db.close()


async def test_two_cancels_during_a_read_on_a_read_only_store_end_as_a_cancellation(
    tmp_path: Path,
):
    path = tmp_path / "state.db"
    async with StateDB(path):  # creates the file a read-only store needs
        pass
    db = StateDB(path, readonly=True)
    try:
        await db.open()
        blocker = await _install_blocker(db)

        task = await _cancel_twice_while_blocked(blocker, db.fetch_all("SELECT block()"))

        assert task.done()
        assert task.cancelled()
        assert db._engine.pool.checkedout() == 0
        await _wait_until(lambda: _other_tasks() == [])
        assert _other_tasks() == []
    finally:
        await db.close()


@pytest.mark.slow_timing
async def test_two_cancels_while_begin_waits_for_another_writer_leave_the_store_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The other writer is released about 0.3s in, against a 5s busy timeout, so
    # the wait for the lock never times out on its own.
    monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 5000)
    path = tmp_path / "state.db"
    db = StateDB(path)
    holder = None
    try:
        await db.open()
        holder = sqlite3.connect(path, timeout=0, isolation_level=None)
        await db.execute("CREATE TABLE probe (x INTEGER)")
        holder.execute("BEGIN IMMEDIATE")

        task = asyncio.ensure_future(db.execute("INSERT INTO probe (x) VALUES (1)"))
        assert await _wait_until(lambda: db._engine.pool.checkedout() == 1), (
            "the insert never checked out a connection"
        )
        await asyncio.sleep(0.2)  # into BEGIN IMMEDIATE, waiting on the holder
        await _cancel_twice(task)
        holder.execute("COMMIT")
        await asyncio.wait({task}, timeout=10)

        assert task.done()
        assert task.cancelled()
        await _wait_until(lambda: _other_tasks() == [])
        assert _other_tasks() == []
        await asyncio.wait_for(db.execute("INSERT INTO probe (x) VALUES (2)"), 10)
        assert await db.fetch_all("SELECT x FROM probe") == [{"x": 2}]
    finally:
        if holder is not None:
            holder.close()
        await db.close()
