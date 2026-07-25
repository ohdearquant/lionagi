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
    """The rebuild turns enforcement off. On the ordinary path it has to come
    back on, or every write for the rest of the process runs unchecked.

    This covers the ordinary path only. What happens when the restore itself
    fails is a different guarantee, pinned below.
    """
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


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class _FakeDriver:
    """Enough of the aiosqlite driver surface for the restore helper."""

    def __init__(self, *, rollback_raises=False, pragma_raises=False, readback=1):
        self.rollback_raises = rollback_raises
        self.pragma_raises = pragma_raises
        self.readback = readback
        self.executed: list[str] = []

    async def rollback(self):
        if self.rollback_raises:
            raise RuntimeError("rollback failed")

    async def commit(self):
        return None

    async def execute(self, sql):
        self.executed.append(sql)
        if self.pragma_raises:
            raise RuntimeError("pragma failed")
        if sql == "PRAGMA foreign_keys":
            return _FakeCursor((self.readback,))
        return _FakeCursor(None)


class _FakeConn:
    def __init__(self):
        self.invalidated = False

    async def invalidate(self):
        self.invalidated = True


@pytest.mark.parametrize(
    ("kwargs", "expect_invalidated", "why"),
    [
        ({}, False, "ordinary path: enforcement confirmed on, connection stays in the pool"),
        (
            {"readback": 0},
            True,
            "the write appeared to succeed but enforcement read back off, which is what "
            "a pragma issued inside a surviving transaction looks like",
        ),
        ({"pragma_raises": True}, True, "the restore itself raised, so nothing was restored"),
        (
            {"rollback_raises": True},
            False,
            "a failed rollback alone is survivable: enforcement still read back on",
        ),
    ],
)
async def test_a_connection_with_unconfirmed_enforcement_is_never_reused(
    kwargs, expect_invalidated, why
):
    """The guarantee is not that the restore always succeeds. It is that a
    connection whose foreign-key enforcement cannot be CONFIRMED is invalidated
    rather than returned to the pool.

    Both failure modes here are the same defect the rebuild itself was fixed for:
    a pragma is a no-op inside an open transaction, so writing it proves nothing
    and only reading it back does.
    """
    from lionagi.state.db import _restore_foreign_keys

    driver = _FakeDriver(**kwargs)
    conn = _FakeConn()

    await _restore_foreign_keys(conn, driver)

    assert conn.invalidated is expect_invalidated, why
    assert "PRAGMA foreign_keys = ON" in driver.executed


@pytest.mark.parametrize("fail_on", ["rollback", "pragma"])
async def test_cancellation_invalidates_before_it_propagates(fail_on):
    """Cancellation is the one path that must not silently skip invalidation.

    It arrives as a BaseException, so an ``except Exception`` guard lets it past
    both the read-back and the invalidation, returning a connection with
    enforcement in an unknown state to the pool. The cancellation itself still
    has to propagate untouched, so this pins both halves: invalidated, and still
    raised.
    """
    from lionagi.ln.concurrency import get_cancelled_exc_class
    from lionagi.state.db import _restore_foreign_keys

    cancelled_exc = get_cancelled_exc_class()

    class _CancellingDriver(_FakeDriver):
        async def rollback(self):
            if fail_on == "rollback":
                raise cancelled_exc()

        async def execute(self, sql):
            self.executed.append(sql)
            if fail_on == "pragma":
                raise cancelled_exc()
            return _FakeCursor((1,))

    driver = _CancellingDriver()
    conn = _FakeConn()

    with pytest.raises(cancelled_exc):
        await _restore_foreign_keys(conn, driver)

    assert conn.invalidated, (
        "cancellation reached the caller without invalidating a connection whose "
        "foreign-key enforcement was never confirmed"
    )


async def test_cancellation_at_the_disable_still_reaches_cleanup(tmp_path: Path, monkeypatch):
    """The disable itself has to be inside the try that installs the cleanup.

    `PRAGMA foreign_keys = OFF` takes effect at its own await, with no flush
    needed. So if it sat above the try, a cancellation arriving right after it
    would leave enforcement off with no finally installed, and the connection
    would go back to the pool disabled for whoever borrows it next. Interrupting
    exactly that await is the only way to pin the ordering.
    """
    import aiosqlite

    import lionagi.state.db as db_mod
    from lionagi.ln.concurrency import get_cancelled_exc_class

    cancelled_exc = get_cancelled_exc_class()

    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(str(path)) as old:
        await old.execute(_LEGACY_SESSIONS_DDL)
        await old.commit()

    reached: list[bool] = []
    real_restore = db_mod._restore_foreign_keys

    async def _spy_restore(conn, driver):
        reached.append(True)
        return await real_restore(conn, driver)

    monkeypatch.setattr(db_mod, "_restore_foreign_keys", _spy_restore)

    real_execute = aiosqlite.Connection.execute

    async def _cancel_on_disable(self, sql, *args, **kwargs):
        if isinstance(sql, str) and sql.strip() == "PRAGMA foreign_keys = OFF":
            raise cancelled_exc()
        return await real_execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "execute", _cancel_on_disable)

    state = StateDB(path)
    with pytest.raises(BaseException) as excinfo:
        await state.open()

    assert isinstance(excinfo.value, cancelled_exc), (
        f"expected the cancellation to propagate, got {type(excinfo.value).__name__}"
    )
    assert reached, (
        "cancellation at the disable never reached the restore, so the pragma is "
        "taking effect outside the try that installs cleanup"
    )
