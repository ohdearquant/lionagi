# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The legacy sessions rebuild must produce the constraints the declared schema
carries, and must survive a database that has nothing but a sessions table."""

from pathlib import Path

import aiosqlite
import pytest

from lionagi.state.db import StateDB
from lionagi.state.schema_meta import metadata

pytestmark = pytest.mark.asyncio

_LEGACY_SESSIONS_DDL = """
    CREATE TABLE sessions (
      id              TEXT    PRIMARY KEY,
      created_at      REAL    NOT NULL,
      node_metadata   JSON,
      name            TEXT,
      user            TEXT,
      progression_id  TEXT    NOT NULL,
      first_msg_id    TEXT,
      last_msg_id     TEXT,
      updated_at      REAL,
      status          TEXT CHECK(
                        status IS NULL
                        OR status IN ('running', 'completed', 'failed', 'aborted')
                      )
    )
"""


def _declared_foreign_keys() -> set[tuple[str, str, str]]:
    """(child column, parent table, parent column) for every FK the schema declares."""
    return {
        (fk.parent.name, fk.column.table.name, fk.column.name)
        for fk in metadata.tables["sessions"].foreign_keys
    }


async def test_rebuilt_sessions_carries_the_declared_foreign_keys(tmp_path: Path):
    """A rebuilt table must end up with the same foreign keys a freshly created
    one gets. The rebuild writes its own DDL, so nothing but a check like this
    keeps it from drifting away from the declared schema."""
    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(str(path)) as old:
        await old.execute(_LEGACY_SESSIONS_DDL)
        await old.execute(
            "INSERT INTO sessions (id, created_at, progression_id, updated_at, status) "
            "VALUES ('s1', 1.0, 'p1', 1.0, 'running')"
        )
        await old.commit()

    state = StateDB(path)
    await state.open()
    try:
        assert (await state.get_session("s1")) is not None
    finally:
        await state.close()

    async with aiosqlite.connect(str(path)) as db:
        rows = await (await db.execute("PRAGMA foreign_key_list(sessions)")).fetchall()
    # PRAGMA foreign_key_list columns: id, seq, table, from, to, ...
    rebuilt = {(r[3], r[2], r[4]) for r in rows}

    declared = _declared_foreign_keys()
    assert declared, "the schema is expected to declare foreign keys on sessions"
    assert rebuilt == declared, (
        f"rebuilt table's foreign keys {sorted(rebuilt)} do not match the declared "
        f"schema's {sorted(declared)}"
    )


async def test_rebuild_survives_a_database_with_no_parent_tables(tmp_path: Path):
    """The population this rebuild exists to serve: a database old enough that
    the tables sessions references do not exist yet.

    SQLite accepts a forward reference in DDL, so creating the new table is fine.
    Enforcement is what bites: with foreign keys on, every statement against the
    child fails while a parent is missing, including the rebuild's own copy and
    including rows whose foreign key column is NULL. The rebuild therefore has to
    disable enforcement in a way that actually takes effect, which means on the
    raw connection outside a transaction -- open() installs BEGIN IMMEDIATE, so
    the pragma is a no-op if it is issued inside an engine-level begin() block.
    """
    path = tmp_path / "minimal.db"
    async with aiosqlite.connect(str(path)) as old:
        await old.execute(_LEGACY_SESSIONS_DDL)
        await old.execute(
            "INSERT INTO sessions (id, created_at, progression_id, updated_at, status) "
            "VALUES ('only', 1.0, 'p-gone', 1.0, 'running')"
        )
        await old.commit()
        names = {
            r[0]
            for r in await (
                await old.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    assert names == {"sessions"}, f"fixture must start minimal, found {sorted(names)}"

    state = StateDB(path)
    await state.open()  # the rebuild runs here; a live FK check makes it raise
    try:
        row = await state.get_session("only")
        assert row is not None
        assert row["status"] == "running"
        # The row's foreign key values point at rows that do not exist. The
        # rebuild must not have silently dropped them while copying.
        assert row["progression_id"] == "p-gone"
    finally:
        await state.close()


async def test_foreign_key_enforcement_is_restored_after_the_rebuild(tmp_path: Path):
    """The rebuild turns enforcement off. It has to come back on, or every write
    for the rest of the process runs unchecked."""
    path = tmp_path / "restored.db"
    async with aiosqlite.connect(str(path)) as old:
        await old.execute(_LEGACY_SESSIONS_DDL)
        await old.commit()

    state = StateDB(path)
    await state.open()
    try:
        async with state._engine.connect() as conn:
            driver = (await conn.get_raw_connection()).driver_connection
            enforced = await (await driver.execute("PRAGMA foreign_keys")).fetchone()
        assert enforced[0] == 1, "foreign key enforcement was left disabled after the rebuild"
    finally:
        await state.close()
