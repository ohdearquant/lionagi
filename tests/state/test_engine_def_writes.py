# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Preset updates validate the row protected by their write transaction."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import JSON, bindparam, event, text
from sqlalchemy.ext.asyncio import AsyncConnection

from lionagi.state.db import StateDB

_BORROWED_DB: contextvars.ContextVar[StateDB | None] = contextvars.ContextVar(
    "engine_write_test_db", default=None
)
_WAIT_SECONDS = 5


@dataclass
class _Stores:
    first: StateDB
    second: StateDB
    def_id: str
    other_id: str


@pytest_asyncio.fixture(
    params=["sqlite", pytest.param("postgresql", marks=pytest.mark.integration)]
)
async def stores(request, tmp_path):
    """Open independent handles before starting the operation under test.

    Opening a handle may itself write schema metadata. Doing that inside either
    competing service call could serialize it before its defective unlocked read.
    """
    if request.param == "postgresql":
        url = request.getfixturevalue("pg_url")
        first = StateDB(url=url)
        second = StateDB(url=url)
    else:
        path = tmp_path / "engine-writes.db"
        first = StateDB(path)
        second = StateDB(path)
    ids = [f"enginewrite{uuid.uuid4().hex}" for _ in range(2)]
    opened = []
    try:
        for db in (first, second):
            await db.open()
            opened.append(db)
        for def_id in ids:
            await first.create_engine_def(
                {
                    "id": def_id,
                    "name": def_id,
                    "kind": "hypothesis",
                    "model": "test-model",
                    "max_depth": 3,
                    "max_agents": 4,
                    "options": {},
                    "description": "before",
                    "created_at": 10.0,
                    "updated_at": 20.0,
                }
            )
        yield _Stores(first, second, *ids)
    finally:
        if opened:
            for def_id in ids:
                await opened[0].delete_engine_def(def_id)
        for db in reversed(opened):
            await db.close()


@pytest.fixture
def service(monkeypatch):
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from lionagi.studio.services import engine_defs

    @asynccontextmanager
    async def borrowed():
        db = _BORROWED_DB.get()
        assert db is not None, "the service call must select its independent handle"
        yield db

    monkeypatch.setattr(engine_defs, "StateDB", borrowed)
    return engine_defs


async def _update(service, db: StateDB, def_id: str, patch: dict[str, Any]):
    token = _BORROWED_DB.set(db)
    try:
        return await service.update_engine_def(def_id, patch)
    finally:
        _BORROWED_DB.reset(token)


async def _wait(event_: asyncio.Event) -> None:
    await asyncio.wait_for(event_.wait(), timeout=_WAIT_SECONDS)


async def _finish(task: asyncio.Task):
    return await asyncio.wait_for(task, timeout=_WAIT_SECONDS)


async def _cancel_remaining(task: asyncio.Task | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _sql(statement: Any) -> str:
    return " ".join(str(statement).upper().split())


class _AccessTrace:
    """Release a holder only after a discriminating competitor boundary.

    A correct SQLite update waits at BEGIN IMMEDIATE; a correct PostgreSQL
    update waits at its locking SELECT. An unlocked SELECT is allowed to finish
    while the holder is retained, ensuring the defect really captures old data
    before release. Waiting only for an eventual blocked UPDATE would not prove
    which row was used for validation.
    """

    def __init__(self, db: StateDB):
        self.db = db
        self.reached = asyncio.Event()
        self.boundary: str | None = None
        self.statements: list[str] = []

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        sql = _sql(statement)
        self.statements.append(sql)
        if self.boundary is None and (
            sql == "BEGIN IMMEDIATE"
            or (sql.startswith("SELECT") and "FROM ENGINE_DEFS" in sql and "FOR UPDATE" in sql)
        ):
            self.boundary = "protected_read_attempt"
            self.reached.set()

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        sql = _sql(statement)
        if (
            self.boundary is None
            and sql.startswith("SELECT")
            and "FROM ENGINE_DEFS" in sql
            and "FOR UPDATE" not in sql
        ):
            self.boundary = "unprotected_read_completed"
            self.reached.set()

    def __enter__(self):
        event.listen(self.db._engine.sync_engine, "before_cursor_execute", self._before)
        event.listen(self.db._engine.sync_engine, "after_cursor_execute", self._after)
        return self

    def __exit__(self, *exc):
        event.remove(self.db._engine.sync_engine, "before_cursor_execute", self._before)
        event.remove(self.db._engine.sync_engine, "after_cursor_execute", self._after)


async def _held_patch(conn, def_id: str, fields: dict[str, Any]) -> None:
    assert set(fields) <= {"kind", "options", "description"}
    columns = ", ".join(f'"{name}" = :{name}' for name in fields)
    statement = text(f"UPDATE engine_defs SET {columns} WHERE id = :def_id")  # noqa: S608
    if "options" in fields:
        statement = statement.bindparams(bindparam("options", type_=JSON))
    await conn.execute(statement, {"def_id": def_id, **fields})


@pytest.mark.parametrize("first_patch", ["kind", "options"])
async def test_engine_update_validates_the_row_a_competing_writer_committed(
    stores, service, first_patch
):
    """Individually valid patches cannot jointly store a kind's unused option."""
    # The unheld control excludes a general service/setup refusal as the reason
    # the contender later refuses.
    assert await _update(service, stores.second, stores.def_id, {"description": "unheld"})
    holder_patch = (
        {"kind": "research"} if first_patch == "kind" else {"options": {"export_dir": "out"}}
    )
    contender_patch = (
        {"options": {"export_dir": "out"}} if first_patch == "kind" else {"kind": "research"}
    )
    contender = None
    try:
        with _AccessTrace(stores.second) as trace:
            async with stores.first.transaction() as conn:
                await _held_patch(conn, stores.def_id, holder_patch)
                contender = asyncio.create_task(
                    _update(service, stores.second, stores.def_id, contender_patch)
                )
                await _wait(trace.reached)
                assert not contender.done(), "the held writer was bypassed completely"
            with pytest.raises(ValueError, match="export_dir"):
                await _finish(contender)
            assert trace.boundary == "protected_read_attempt", trace.statements
        row = await stores.first.get_engine_def(stores.def_id)
        assert row is not None
        assert row["kind"] == ("research" if first_patch == "kind" else "hypothesis")
        assert row["options"] == ({} if first_patch == "kind" else {"export_dir": "out"})
        assert row["description"] == "unheld"
    finally:
        await _cancel_remaining(contender)


async def test_engine_update_keeps_concurrent_disjoint_patches(stores, service):
    contender = None
    try:
        with _AccessTrace(stores.second) as trace:
            async with stores.first.transaction() as conn:
                await _held_patch(conn, stores.def_id, {"description": "first writer"})
                contender = asyncio.create_task(
                    _update(service, stores.second, stores.def_id, {"model": "changed-model"})
                )
                await _wait(trace.reached)
            assert await _finish(contender)
        row = await stores.first.get_engine_def(stores.def_id)
        assert row is not None
        assert row["description"] == "first writer"
        assert row["model"] == "changed-model"
    finally:
        await _cancel_remaining(contender)


async def test_engine_update_reports_missing_after_a_competing_delete(stores, service):
    contender = None
    try:
        with _AccessTrace(stores.second) as trace:
            async with stores.first.transaction() as conn:
                await conn.execute(
                    text("DELETE FROM engine_defs WHERE id = :def_id"),
                    {"def_id": stores.def_id},
                )
                contender = asyncio.create_task(
                    _update(service, stores.second, stores.def_id, {"description": "too late"})
                )
                await _wait(trace.reached)
            assert await _finish(contender) is False
        assert await stores.first.get_engine_def(stores.def_id) is None
    finally:
        await _cancel_remaining(contender)


def _pause_after_write(monkeypatch, db: StateDB):
    written = asyncio.Event()
    release = asyncio.Event()
    original = AsyncConnection.execute

    async def execute(conn, statement, *args, **kwargs):
        result = await original(conn, statement, *args, **kwargs)
        if (
            _BORROWED_DB.get() is db
            and _sql(statement).startswith("UPDATE ENGINE_DEFS")
            and not written.is_set()
        ):
            # The real UPDATE has executed but its enclosing transaction has
            # not committed. Cancel/release controls this database await's exit.
            written.set()
            await release.wait()
        return result

    monkeypatch.setattr(AsyncConnection, "execute", execute)
    return written, release


async def test_engine_update_commits_before_a_later_delete(stores, service, monkeypatch):
    written, release = _pause_after_write(monkeypatch, stores.second)
    update = asyncio.create_task(
        _update(service, stores.second, stores.def_id, {"description": "committed first"})
    )
    delete = None
    try:
        await _wait(written)
        with _AccessTrace(stores.first) as trace:
            delete = asyncio.create_task(stores.first.delete_engine_def(stores.def_id))
            if stores.first.dialect == "sqlite":
                await _wait(trace.reached)
            else:
                # PostgreSQL DELETE waits on the row written by the updater;
                # observe its actual SQL attempt, not an arbitrary sleep.
                attempted = asyncio.Event()

                def deleting(conn, cursor, statement, parameters, context, executemany):
                    if _sql(statement).startswith("DELETE FROM ENGINE_DEFS"):
                        attempted.set()

                event.listen(stores.first._engine.sync_engine, "before_cursor_execute", deleting)
                try:
                    # The first task has already reached the driver or will
                    # reach this listener when the event loop resumes.
                    await _wait(attempted)
                finally:
                    event.remove(
                        stores.first._engine.sync_engine, "before_cursor_execute", deleting
                    )
            assert not delete.done()
            release.set()
            assert await _finish(update)
            assert await _finish(delete)
        assert await stores.second.get_engine_def(stores.def_id) is None
    finally:
        release.set()
        await _cancel_remaining(update)
        await _cancel_remaining(delete)


@pytest.mark.parametrize("refusal", ["invalid_options", "duplicate_name"])
async def test_engine_update_refusal_rolls_back_and_releases_the_writer(stores, service, refusal):
    before = await stores.first.get_engine_def(stores.def_id)
    assert before is not None
    if refusal == "invalid_options":
        patch = {"kind": "research", "options": {"export_dir": "out"}}
        expected = ValueError
    else:
        patch = {"name": stores.other_id, "description": "must roll back"}
        expected = service.NameConflictError
    with pytest.raises(expected):
        await _update(service, stores.first, stores.def_id, patch)
    assert await stores.second.get_engine_def(stores.def_id) == before
    assert await asyncio.wait_for(
        _update(service, stores.second, stores.def_id, {"description": "after refusal"}),
        timeout=_WAIT_SECONDS,
    )
    after = await stores.first.get_engine_def(stores.def_id)
    assert after is not None and after["description"] == "after refusal"


async def test_engine_update_cancellation_rolls_back_and_releases_the_writer(
    stores, service, monkeypatch
):
    before = await stores.first.get_engine_def(stores.def_id)
    written, release = _pause_after_write(monkeypatch, stores.second)
    update = asyncio.create_task(
        _update(service, stores.second, stores.def_id, {"description": "must roll back"})
    )
    try:
        await _wait(written)
        update.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _finish(update)
        assert await stores.first.get_engine_def(stores.def_id) == before
        release.set()
        assert await asyncio.wait_for(
            _update(service, stores.first, stores.def_id, {"description": "after cancellation"}),
            timeout=_WAIT_SECONDS,
        )
    finally:
        release.set()
        await _cancel_remaining(update)


async def test_engine_in_transaction_writer_binds_json_and_reports_missing(stores):
    # Import inside the test so the behavioral red arms remain runnable before
    # the new private storage helpers have been introduced.
    from lionagi.state.db import _update_engine_def_in_tx

    async with stores.first.transaction() as conn:
        assert (
            await _update_engine_def_in_tx(
                conn, stores.def_id, {"options": {"export_dir": "quote' and 雪"}}
            )
            == 1
        )
        assert await _update_engine_def_in_tx(conn, f"missing{uuid.uuid4().hex}", {}) == 0
    row = await stores.second.get_engine_def(stores.def_id)
    assert row is not None and row["options"] == {"export_dir": "quote' and 雪"}
    before = row.copy()
    with pytest.raises(ValueError):
        async with stores.first.transaction() as conn:
            await _update_engine_def_in_tx(conn, stores.def_id, {"not_a_column": "ignored"})
    assert await stores.second.get_engine_def(stores.def_id) == before


async def test_engine_direct_storage_update_remains_unvalidated(stores):
    assert (
        await stores.first.update_engine_def(
            stores.def_id, kind="research", options={"test_cmd": "legacy"}
        )
        is None
    )
    row = await stores.second.get_engine_def(stores.def_id)
    assert row is not None
    assert row["kind"] == "research"
    assert row["options"] == {"test_cmd": "legacy"}


async def test_engine_locked_read_uses_bound_id_raw_options_and_dialect_lock():
    """Inspect the actual generated statements without requiring a PG server."""
    from lionagi.state.db import _get_engine_def_for_update_in_tx

    class Database:
        def __init__(self, dialect):
            self.dialect = dialect

    class Recorder:
        def __init__(self):
            self.statements = []
            self.row = {"id": "stored", "options": ' {"legacy": true} '}

        async def execute(self, statement, params):
            self.statements.append((str(statement), dict(params)))
            return self

        def mappings(self):
            return self

        def first(self):
            return self.row

    recorder = Recorder()
    requested_id = "preset' OR 1=1 -- 雪"
    for dialect, for_update in [
        ("postgresql", True),
        ("postgresql", False),
        ("sqlite", True),
        ("sqlite", False),
    ]:
        row = await _get_engine_def_for_update_in_tx(
            Database(dialect), recorder, requested_id, for_update=for_update
        )
        assert row == recorder.row
        assert row["options"] == ' {"legacy": true} '
        statement, params = recorder.statements[-1]
        assert params == {"id": requested_id}
        assert requested_id not in statement
        assert "FROM engine_defs WHERE id = :id" in statement
        assert statement.endswith(" FOR UPDATE") == (dialect == "postgresql" and for_update)
        assert ("CAST(options AS TEXT) AS options" in statement) == (dialect == "postgresql")

    recorder.row = None
    assert (
        await _get_engine_def_for_update_in_tx(Database("postgresql"), recorder, requested_id)
        is None
    )
    assert recorder.statements[-1][0].endswith(" FOR UPDATE")
