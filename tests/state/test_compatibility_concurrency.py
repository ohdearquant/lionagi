# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility mode beside a managing application that writes and checkpoints (ADR-0128 D8,
item 7).

The framework runs every operation its synthetic profile admits, repeatedly, while the managing
application's own writer inserts rows of its own and checkpoints the write-ahead log on a
connection of its own. The limits below are declared before anything runs, and each measured
quantity is held to its limit.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest_asyncio
from sqlalchemy import event

from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from tests.state.test_compatibility_owner_ops import OWNER, _managed_store, _profile, _rows
from tests.state.test_compatibility_recovery import (
    _assert_the_store_is_sound,
    _framework_view,
    _keys,
    _managing_view,
    _recording_inserts,
    run_every_operation,
)

# The longest one framework write transaction may hold the write lock, in seconds, counted from the
# return of its BEGIN IMMEDIATE to its connection going back to the pool. Eleven runs on a
# development machine measured at most 0.0038 s. The limit is about 500 times that, so a heavily
# loaded machine stays inside it, and it is far below the managing writer's busy timeout below, so
# a hold at the limit would still let a waiting writer succeed.
MAX_WRITE_HOLD_S = 2.0

# The largest fraction of the managing writer's write attempts that may fail with a busy error
# after its busy timeout. Eleven runs measured 0 failures in each 32 attempts, whose slowest took
# at most 0.048 s against the 30 s timeout below. A failed write is an outright loss for the
# managing application, so the limit is small; it stays above zero only so that one attempt
# starved on a machine far slower than the measured one is read from the message, as a rate,
# before it fails.
MAX_MANAGING_BUSY_RATE = 0.05

# The largest size in bytes the write-ahead log file may reach at any sample taken during the run.
# Eleven runs peaked at 0.74 MB. The limit is 16 MiB: over 20 times that, and 4 times the 4 MiB at
# which SQLite's own automatic checkpoint (1000 pages of 4 KiB) starts to fold the log back.
MAX_WAL_BYTES = 16 * 1024 * 1024

# How many times the framework goes through every operation of the profile.
FRAMEWORK_RUNS = 8

# How many write attempts the managing writer makes while the framework goes through them once.
MANAGING_WRITES_PER_RUN = 4

# The managing writer checkpoints the log (truncating it) after every this many write attempts.
CHECKPOINT_EVERY = 2

# The managing writer's busy timeout in seconds: how long one of its writes waits for the lock.
MANAGING_BUSY_TIMEOUT_S = 30.0

# How long, in seconds, the managing writer waits for the framework to hold its first write lock
# before the test is treated as hung. It ends a hang; it never paces or orders anything.
HANG_GUARD_S = 120.0

FRAMEWORK_PRINCIPAL = "principal:a"
MANAGING_PRINCIPAL = "principal:b"

# The columns the managing writer gives each row it inserts into the tables the framework writes.
_MANAGING_COLUMNS = {
    "progressions": ("id", "created_at", "collection", OWNER),
    "messages": ("id", "created_at", "content", "role", "lion_class", OWNER),
}


def _wal_bytes(store: Path) -> int:
    try:
        return os.stat(f"{store}-wal").st_size
    except FileNotFoundError:
        return 0


class _Probe:
    """What the run measures: write-lock holds, and the largest the write-ahead log grew."""

    def __init__(self, store: Path) -> None:
        self.store = store
        self.write_begun = threading.Event()
        self.holds: list[float] = []
        self.wal_peak = 0
        self._began: dict[int, float] = {}
        self._lock = threading.Lock()

    def sample_wal(self) -> None:
        size = _wal_bytes(self.store)
        with self._lock:
            self.wal_peak = max(self.wal_peak, size)

    def attach(self, db: StateDB) -> None:
        sync_engine = db._engine.sync_engine
        # Registered after the engine's own listener, so it runs once BEGIN IMMEDIATE has returned.
        event.listen(sync_engine, "begin", self._begin)
        event.listen(sync_engine.pool, "checkin", self._checkin)

    def _begin(self, conn) -> None:
        if conn.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
            return
        self._began[id(conn.connection.dbapi_connection)] = time.perf_counter()
        self.sample_wal()
        self.write_begun.set()

    def _checkin(self, dbapi_connection, _record) -> None:
        began = self._began.pop(id(dbapi_connection), None)
        if began is not None:
            self.holds.append(time.perf_counter() - began)


class _ManagingWriter:
    """The managing application's writer: its own rows and checkpoints, on its own connection.

    Each batch starts once the framework holds its first write lock of a run, so the batch meets
    the framework's writes in progress. A batch is a fixed number of write attempts, so the run is
    bounded by a count of operations and not by a clock.
    """

    def __init__(self, store: Path, probe: _Probe) -> None:
        self.store = store
        self.probe = probe
        self.attempts = 0
        self.busy = 0
        self.waits: list[float] = []
        self.checkpoints_blocked = 0
        self.rows: dict[tuple[str, str], tuple] = {}

    def write_batch(self) -> None:
        conn = sqlite3.connect(self.store, timeout=MANAGING_BUSY_TIMEOUT_S, isolation_level=None)
        try:
            if not self.probe.write_begun.wait(HANG_GUARD_S):
                raise TimeoutError("the framework began no write transaction")
            for _ in range(MANAGING_WRITES_PER_RUN):
                self._attempt(conn)
        finally:
            conn.close()

    def _insert(self, conn: sqlite3.Connection, table: str, values: tuple) -> None:
        columns = _MANAGING_COLUMNS[table]
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        self.rows[(table, values[0])] = values

    def _attempt(self, conn: sqlite3.Connection) -> None:
        n = self.attempts
        self.attempts += 1
        started = time.perf_counter()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
            self.busy += 1
            return
        try:
            self._insert(
                conn, "progressions", (f"managed-p{n}", float(n), "[]", MANAGING_PRINCIPAL)
            )
            self._insert(
                conn,
                "messages",
                (f"managed-m{n}", float(n), "{}", "user", 2, MANAGING_PRINCIPAL),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        self.waits.append(time.perf_counter() - started)
        self.probe.sample_wal()
        if self.attempts % CHECKPOINT_EVERY == 0:
            busy, _log, _checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            self.checkpoints_blocked += busy
            self.probe.sample_wal()


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> Path:
    return await _managed_store(tmp_path / "managed.db")


async def test_the_framework_and_the_managing_application_write_within_the_declared_limits(store):
    earlier = {table: _keys(store, table) for table in _MANAGING_COLUMNS}
    probe = _Probe(store)
    writer = _ManagingWriter(store, probe)
    loop = asyncio.get_running_loop()

    handle = StateDB(store, compatibility=_profile(store), owner=FRAMEWORK_PRINCIPAL)
    await handle.open()
    await db_mod.register_shared_db(handle)
    probe.attach(handle)
    try:
        with _recording_inserts(handle) as inserts:
            for number in range(FRAMEWORK_RUNS):
                probe.write_begun.clear()
                managing = loop.run_in_executor(None, writer.write_batch)
                try:
                    await run_every_operation(handle, f"-{number}")
                finally:
                    # A run that failed before it held the lock must not leave the batch waiting.
                    probe.write_begun.set()
                    await managing
                probe.sample_wal()
        seen = await _framework_view(handle, inserts.keys)
    finally:
        await handle.close()
        await db_mod.close_shared_db()
    probe.sample_wal()

    assert writer.attempts == FRAMEWORK_RUNS * MANAGING_WRITES_PER_RUN
    assert probe.holds, "no framework write transaction was measured"
    longest_hold = max(probe.holds)
    busy_rate = writer.busy / writer.attempts
    detail = (
        f"{len(probe.holds)} framework write transactions; {writer.attempts} managing write "
        f"attempts, the slowest taking {max(writer.waits, default=0.0):.4f} s; "
        f"{writer.checkpoints_blocked} managing checkpoints blocked"
    )
    assert longest_hold <= MAX_WRITE_HOLD_S, (
        f"a framework write transaction held the write lock for {longest_hold:.4f} s, "
        f"over the limit of {MAX_WRITE_HOLD_S} s ({detail})"
    )
    assert busy_rate <= MAX_MANAGING_BUSY_RATE, (
        f"{writer.busy} of {writer.attempts} managing write attempts failed busy "
        f"(rate {busy_rate:.4f}), over the limit of {MAX_MANAGING_BUSY_RATE} ({detail})"
    )
    assert probe.wal_peak <= MAX_WAL_BYTES, (
        f"the write-ahead log reached {probe.wal_peak} bytes, over the limit of "
        f"{MAX_WAL_BYTES} bytes ({detail})"
    )

    # Every row either side wrote is present, and what the managing application wrote is as it
    # wrote it.
    for (table, key), written in seen.items():
        assert _managing_view(store, table, key) == written, f"{table} {key!r}"
    for (table, key), values in writer.rows.items():
        columns = ", ".join(_MANAGING_COLUMNS[table])
        assert _rows(store, f"SELECT {columns} FROM {table} WHERE id = ?", (key,)) == [values], (
            f"{table} {key!r}"
        )
    for table, keys in earlier.items():
        framework = inserts.keys.get(table, set())
        managing_keys = {key for (name, key) in writer.rows if name == table}
        assert _keys(store, table) == keys | framework | managing_keys, f"the rows of {table}"
    _assert_the_store_is_sound(store, "after the run")
