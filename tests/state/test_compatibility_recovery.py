# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Recovery in compatibility mode (ADR-0128 D8, item 9).

Once the framework stops writing, the application that manages the store must still read every
row the framework wrote, with the column values the framework wrote, and its own integrity and
foreign-key checks must come back clean. The managing application is kept running (it holds a
connection of its own to the store), so the framework's rows are still in the write-ahead log,
and not yet in the store file, when the framework stops; they are then read again after the
managing application's checkpoint and after it reopens the store. A write the framework
abandoned part way, because a later statement of its transaction was denied, leaves nothing of
that transaction for the managing application to see.

The store is the synthetic managed store of ``test_compatibility_owner_ops``, and the managing
side reads it through that module's helpers.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event

from lionagi.state import db as db_mod
from lionagi.state.compat import StateCompatibilityRefused, TableWrites
from lionagi.state.db import StateDB
from tests.state.test_compatibility_owner_ops import (
    OWNER,
    _execute,
    _live_message,
    _managed_store,
    _profile,
    _rows,
    _session,
    _system_message,
)
from tests.state.test_compatibility_teardown_ops import _OPS

PRINCIPAL = "principal:a"

# The column that names a row in each table the framework writes; every other table is named by id.
_KEY_COLUMN = {"message_types": "lion_class"}

# The tables the operations of the synthetic profile write when a run goes through every one of
# them once (``admin_events`` is written only on a rejected status transition).
_WRITTEN_TABLES = {
    "progressions",
    "sessions",
    "messages",
    "message_types",
    "branches",
    "status_transitions",
    "session_signals",
}

_INSERT = re.compile(r'^\s*INSERT\s+INTO\s+"?(\w+)"?\s*\(([^)]*)\)', re.IGNORECASE)


async def run_every_operation(db: StateDB, tag: str = "") -> None:
    """Drive each operation the synthetic profile admits once, on a run whose ids carry *tag*."""
    progression, branch_progression = f"sp{tag}", f"bp{tag}"
    session, system, branch = f"s{tag}", f"m-sys{tag}", f"b{tag}"
    await db.create_progression(progression)
    await db.create_progression(branch_progression)
    await db.create_session(_session(session, progression_id=progression))
    await db.insert_message(_system_message(system))
    await db.create_branch(
        {
            "id": branch,
            "session_id": session,
            "progression_id": branch_progression,
            "system_msg_id": system,
        }
    )
    await db._persist_live_message(
        _live_message(f"m1{tag}"),
        session_id=session,
        branch_progression_id=branch_progression,
        session_progression_id=progression,
    )
    await db._persist_live_message(
        _live_message(f"m-sys2{tag}", role="system"),
        session_id=session,
        branch_progression_id=branch_progression,
        system_branch_id=branch,
    )
    await db.update_session(session, first_msg_id=system, last_msg_id=f"m1{tag}")
    for operation in _OPS.values():
        await operation(db, session)


def _key_column(table: str) -> str:
    return _KEY_COLUMN.get(table, "id")


class _Inserts:
    """The key of every row that the INSERT statements of one handle wrote, by table."""

    def __init__(self) -> None:
        self.keys: dict[str, set[str]] = {}
        self.unread: list[str] = []

    def spy(self, _conn, _cursor, statement, parameters, _context, executemany) -> None:
        match = _INSERT.match(statement)
        if match is None:
            return
        table = match.group(1).lower()
        columns = [name.strip().strip('"').lower() for name in match.group(2).split(",")]
        key = _key_column(table)
        for row in parameters if executemany else [parameters]:
            if isinstance(row, dict) or len(row) != len(columns) or key not in columns:
                self.unread.append(statement)
                continue
            self.keys.setdefault(table, set()).add(row[columns.index(key)])


@contextmanager
def _recording_inserts(db: StateDB):
    inserts = _Inserts()
    event.listen(db._engine.sync_engine, "before_cursor_execute", inserts.spy)
    try:
        yield inserts
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", inserts.spy)
    assert inserts.unread == [], "an INSERT whose key could not be read from its parameters"


async def _framework_view(db: StateDB, written: dict[str, set[str]]) -> dict[tuple, dict]:
    """Each recorded row as the framework itself reads it just before it stops."""
    seen = {}
    for table, keys in written.items():
        for key in sorted(keys):
            row = await db.fetch_one(
                f'SELECT * FROM "{table}" WHERE "{_key_column(table)}" = ?', [key]
            )
            assert row is not None, f"{table} {key!r} was written and cannot be read back"
            seen[(table, key)] = row
    return seen


def _managing_view(store: Path, table: str, key: str) -> dict | None:
    """One row as the managing application reads it."""
    names = [column[1] for column in _rows(store, f'PRAGMA table_info("{table}")')]
    rows = _rows(store, f'SELECT * FROM "{table}" WHERE "{_key_column(table)}" = ?', (key,))
    return dict(zip(names, rows[0], strict=True)) if rows else None


def _contents(store: Path) -> dict[str, list]:
    """Every row of every table, as the managing application reads them."""
    tables = _rows(
        store,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
    )
    return {name: sorted(_rows(store, f'SELECT * FROM "{name}"'), key=repr) for (name,) in tables}


def _keys(store: Path, table: str) -> set[str]:
    return {key for (key,) in _rows(store, f'SELECT "{_key_column(table)}" FROM "{table}"')}


def _assert_the_store_is_sound(store: Path, stage: str) -> None:
    assert _rows(store, "PRAGMA integrity_check") == [("ok",)], stage
    assert _rows(store, "PRAGMA foreign_key_check") == [], stage


def _assert_every_row_is_read(
    store: Path, seen: dict[tuple, dict], earlier: dict[str, set[str]], stage: str
) -> None:
    for (table, key), written in seen.items():
        assert _managing_view(store, table, key) == written, f"{stage}: {table} {key!r}"
    for table, keys in earlier.items():
        recorded = {key for (name, key) in seen if name == table}
        assert _keys(store, table) == keys | recorded, f"{stage}: the rows of {table}"
    _assert_the_store_is_sound(store, stage)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> Path:
    return await _managed_store(tmp_path / "managed.db")


@pytest.fixture
def managing_application(store: Path):
    """The managing application's own connection, open for as long as the test runs.

    While it is open, the framework's last connection closing does not fold the write-ahead log
    into the store file, as it would not in a deployment where the managing application is up.
    """
    conn = sqlite3.connect(store)
    conn.execute("SELECT COUNT(*) FROM schema_meta").fetchall()
    try:
        yield conn
    finally:
        conn.close()


async def _end_the_run(handle: StateDB) -> None:
    """How a run ends: its handle closes, then the process-wide sweep."""
    await handle.close()
    await db_mod.close_shared_db()


async def test_the_managing_application_reads_every_row_the_framework_wrote(
    store, managing_application
):
    wal = Path(f"{store}-wal")
    earlier = {table: _keys(store, table) for table in _WRITTEN_TABLES}
    contents = _contents(store)

    handle = StateDB(store, compatibility=_profile(store), owner=PRINCIPAL)
    await handle.open()
    await db_mod.register_shared_db(handle)
    try:
        with _recording_inserts(handle) as inserts:
            await run_every_operation(handle)
        seen = await _framework_view(handle, inserts.keys)
    finally:
        await _end_the_run(handle)

    assert set(inserts.keys) == _WRITTEN_TABLES
    assert wal.stat().st_size > 0, "the write-ahead log is empty when the run ends"
    # The store file alone, without its write-ahead log, does not hold the run's session yet, so
    # the first read below is served from the log.
    store_file_only = store.parent / "store-file-only.db"
    shutil.copyfile(store, store_file_only)
    assert _rows(store_file_only, 'SELECT COUNT(*) FROM "sessions" WHERE "id" = ?', ("s",)) == [
        (0,)
    ], "the run's session is already in the store file when the run ends"
    assert seen[("sessions", "s")]["status"] == "completed"
    assert seen[("progressions", "bp")]["collection"] == '["m1","m-sys2"]'
    _assert_every_row_is_read(store, seen, earlier, "after the framework stops")

    checkpoint = _rows(store, "PRAGMA wal_checkpoint(TRUNCATE)")
    assert checkpoint[0][0] == 0, f"the managing application's checkpoint was blocked: {checkpoint}"
    _assert_every_row_is_read(store, seen, earlier, "after the managing application's checkpoint")

    managing_application.close()
    _assert_every_row_is_read(store, seen, earlier, "after the managing application reopens")

    # What the framework did not write is where the managing application left it.
    after = _contents(store)
    for table, rows in contents.items():
        if table not in _WRITTEN_TABLES:
            assert after[table] == rows, table


def _without_the_grant(store: Path, table: str, column: str):
    """The synthetic profile with the grant to update *column* of *table* taken out."""
    writes = dict(_profile(store).writes)
    granted = writes[table]
    writes[table] = TableWrites(
        insert=granted.insert, update=granted.update - {column}, unwritten=granted.unwritten
    )
    return _profile(store, writes=writes)


async def _prepare_a_live_message(handle: StateDB, store: Path) -> None:
    await handle.create_progression("sp")
    await handle.create_progression("bp")
    await handle.create_session(_session("s1"))


async def _abandon_a_live_message(handle: StateDB) -> None:
    # The message is written and both progressions are appended to before the session's
    # activity is touched, and the profile does not grant that update.
    await handle._persist_live_message(
        _live_message("m-late"),
        session_id="s1",
        branch_progression_id="bp",
        session_progression_id="sp",
    )


async def _prepare_a_session_with_an_invocation(handle: StateDB, store: Path) -> None:
    _execute(
        store,
        f"INSERT INTO invocations (id, skill, started_at, created_at, updated_at, {OWNER}) "
        "VALUES ('inv-a', 'review', 0, 0, 0, ?)",
        (PRINCIPAL,),
    )
    await handle.create_progression("sp")


async def _abandon_a_session_with_an_invocation(handle: StateDB) -> None:
    # The session and its first transition are written before the invocation's counter is
    # raised, and the profile does not grant that update.
    await handle.create_session(_session("s-late", invocation_id="inv-a"))


@pytest.mark.parametrize(
    ("denied", "prepare", "abandon"),
    [
        (
            ("sessions", "last_message_at"),
            _prepare_a_live_message,
            _abandon_a_live_message,
        ),
        (
            ("invocations", "session_count"),
            _prepare_a_session_with_an_invocation,
            _abandon_a_session_with_an_invocation,
        ),
    ],
    ids=["live-message", "session-with-invocation"],
)
async def test_a_write_the_framework_abandoned_leaves_nothing_for_the_managing_application(
    store,
    managing_application,
    denied: tuple[str, str],
    prepare: Callable[[StateDB, Path], Awaitable[None]],
    abandon: Callable[[StateDB], Awaitable[None]],
):
    handle = StateDB(store, compatibility=_without_the_grant(store, *denied), owner=PRINCIPAL)
    await handle.open()
    try:
        await prepare(handle, store)
        before = _contents(store)

        with _recording_inserts(handle) as inserts:
            with pytest.raises(StateCompatibilityRefused):
                await abandon(handle)

        assert inserts.keys, "the transaction wrote a row before its later statement was denied"
        assert _contents(store) == before
        _assert_the_store_is_sound(store, "after the abandoned write")
        # The write lock is not left held: the managing application writes at once.
        with closing(sqlite3.connect(store, timeout=0, isolation_level=None)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
    finally:
        await _end_the_run(handle)

    assert _contents(store) == before
    _assert_the_store_is_sound(store, "after the framework stops")
    _rows(store, "PRAGMA wal_checkpoint(TRUNCATE)")
    assert _contents(store) == before
    managing_application.close()
    assert _contents(store) == before
    _assert_the_store_is_sound(store, "after the managing application reopens")
