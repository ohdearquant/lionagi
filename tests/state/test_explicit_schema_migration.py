# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Explicit migration and writable-open admission on both state backends."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import create_async_engine

import lionagi.state as state
from lionagi.state import SchemaTooNewError, StateDB, UnrecognizedStoreError
from lionagi.state import db as state_db
from tests.state.conftest import _manage_runner_database

_VERSION = "SELECT value FROM schema_meta WHERE key = 'version'"
_CLASSIFICATION = "migration.terminal_delivery_eligibility"
_END_TIME_REPAIR = "migration.session_ended_at_backfill"
_TRANSITION = """
INSERT INTO status_transitions
    (id, entity_type, entity_id, previous_status, status, reason_code, source, created_at)
VALUES (:id, 'session', 'history', 'running', 'failed', 'failed', 'synthetic', 1)
"""


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def store_url(request, tmp_path, monkeypatch):
    monkeypatch.delenv("LIONAGI_STATE_AUTO_MIGRATE", raising=False)
    if request.param == "sqlite":
        yield StateDB(tmp_path / "state.db").url
        return
    admin = request.getfixturevalue("pg_url")
    database = make_url(admin).set(database=f"state_migration_{uuid4().hex}")
    url = database.render_as_string(hide_password=False)
    assert await _manage_runner_database(admin, url, create=True)
    try:
        yield url
    finally:
        await _manage_runner_database(admin, url, create=False)


async def _seed(url, version):
    async with StateDB(url=url):
        pass
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            if version == "5":
                # The previous schema had no consumer table, eligibility column,
                # eligibility indexes or classification marker.
                for statement in (
                    "DROP TABLE terminal_consumers",
                    "DROP INDEX idx_status_transitions_unclassified",
                    "DROP INDEX idx_status_transitions_eligible_order",
                    "ALTER TABLE status_transitions DROP COLUMN delivery_eligible",
                ):
                    await conn.exec_driver_sql(statement)
                await conn.execute(
                    text("DELETE FROM schema_meta WHERE key = :key"), {"key": _CLASSIFICATION}
                )
                await conn.execute(text(_TRANSITION), {"id": "historical"})
            if version is None:
                await conn.exec_driver_sql("DELETE FROM schema_meta WHERE key = 'version'")
            else:
                await conn.execute(
                    text("UPDATE schema_meta SET value = :version WHERE key = 'version'"),
                    {"version": version},
                )
        if engine.dialect.name == "sqlite":
            async with engine.connect() as conn:
                await conn.exec_driver_sql("PRAGMA journal_mode = DELETE")
    finally:
        await engine.dispose()


async def _snapshot(url):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            version = await conn.scalar(text(_VERSION))
            if engine.dialect.name == "sqlite":
                catalog = (
                    await conn.exec_driver_sql(
                        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
                    )
                ).all()
                journal = await conn.scalar(text("PRAGMA journal_mode"))
                return version, catalog, journal
            catalog = (
                await conn.exec_driver_sql(
                    """
                    SELECT 'column', table_name, column_name,
                           concat_ws(':', column_default, is_nullable, udt_name)
                    FROM information_schema.columns WHERE table_schema = current_schema()
                    UNION ALL
                    SELECT 'index', tablename, indexname, indexdef
                    FROM pg_indexes WHERE schemaname = current_schema()
                    UNION ALL
                    SELECT 'constraint', rel.relname, con.conname, pg_get_constraintdef(con.oid)
                    FROM pg_constraint con
                    JOIN pg_class rel ON con.conrelid = rel.oid
                    JOIN pg_namespace ns ON rel.relnamespace = ns.oid
                    WHERE ns.nspname = current_schema()
                    ORDER BY 1, 2, 3, 4
                    """
                )
            ).all()
            return version, catalog, None
    finally:
        await engine.dispose()


async def _transition_columns(conn):
    return await conn.run_sync(
        lambda sync: {column["name"] for column in inspect(sync).get_columns("status_transitions")}
    )


async def _migrate(url):
    store = StateDB(url=url)
    try:
        await store.migrate()
    finally:
        await store.close()


async def test_older_native_open_requires_migration_before_touching_the_store(store_url):
    await _seed(store_url, "6")
    before = await _snapshot(store_url)
    store = StateDB(url=store_url)
    try:
        with pytest.raises(RuntimeError, match="li state migrate") as raised:
            await store.open()
        assert isinstance(raised.value, state.SchemaMigrationRequiredError)
        assert store._engine is None
    finally:
        await store.close()
    assert await _snapshot(store_url) == before


@pytest.mark.parametrize(
    "version", ["0", "06", "abc", None], ids=["zero", "padded", "word", "missing"]
)
@pytest.mark.parametrize("operation", ["open", "migrate"])
async def test_native_store_with_unrecognized_version_is_refused(store_url, version, operation):
    await _seed(store_url, version)
    before = await _snapshot(store_url)
    message = f"version {version!r}" if version is not None else "no .*version"
    store = StateDB(url=store_url)
    try:
        with pytest.raises(UnrecognizedStoreError, match=message) as raised:
            await getattr(store, operation)()
        assert "li state migrate" not in str(raised.value)
        assert store._engine is None
    finally:
        await store.close()
    assert await _snapshot(store_url) == before


async def _end_time_state(engine):
    async with engine.connect() as conn:
        marker = await conn.scalar(
            text("SELECT value FROM schema_meta WHERE key = :key"), {"key": _END_TIME_REPAIR}
        )
        row = (
            await conn.exec_driver_sql(
                "SELECT ended_at, ended_at_is_approximate, duration_ms "
                "FROM sessions WHERE id = 'historical-session'"
            )
        ).one()
        return marker, tuple(row)


@pytest.mark.parametrize("opt_in", [False, True], ids=["explicit", "opt-in"])
async def test_incomplete_current_store_requires_end_time_repair(store_url, monkeypatch, opt_in):
    await _seed(store_url, state_db.SCHEMA_VERSION)
    engine = create_async_engine(store_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM schema_meta WHERE key = :key"), {"key": _END_TIME_REPAIR}
            )
            await conn.exec_driver_sql(
                "INSERT INTO progressions (id, created_at, collection) VALUES ('history', 1, '[]')"
            )
            await conn.exec_driver_sql(
                "INSERT INTO sessions "
                "(id, progression_id, status, created_at, started_at, updated_at, "
                "last_message_at, duration_ms) "
                "VALUES ('historical-session', 'history', 'completed', 1, 10, 50, 60, 999)"
            )

        original = (None, (None, 0, 999.0))
        assert await _end_time_state(engine) == original
        store = StateDB(url=store_url)
        try:
            with pytest.raises(
                state.SchemaMigrationRequiredError, match="li state migrate"
            ) as raised:
                await store.open()
            message = str(raised.value)
            assert f"is at current schema version {state_db.SCHEMA_VERSION}" in message
            assert "has not completed the required historical session end-time repair" in message
            assert store._engine is None
        finally:
            await store.close()
        assert await _end_time_state(engine) == original

        if opt_in:
            with monkeypatch.context() as enabled:
                enabled.setenv("LIONAGI_STATE_AUTO_MIGRATE", "1")
                async with StateDB(url=store_url):
                    pass
        else:
            await _migrate(store_url)
        repaired = ("1", (60.0, 1, None))
        assert await _end_time_state(engine) == repaired

        async def reject_backfill(*_args):
            pytest.fail("ordinary open must not run historical repair")

        monkeypatch.setattr(StateDB, "_backfill_session_ended_at_batch", reject_backfill)
        async with StateDB(url=store_url) as store:
            assert await store.schema_version() == state_db.SCHEMA_VERSION
        assert await _end_time_state(engine) == repaired
    finally:
        await engine.dispose()


async def test_version_six_migration_only_stamps_the_store(store_url, monkeypatch):
    await _seed(store_url, "6")
    _, before, _ = await _snapshot(store_url)
    writes = []

    async def reject(*_args):
        pytest.fail("version 6 must not run the earlier schema migration")

    def record(_conn, _cursor, statement, _params, _context, _many):
        verb = statement.lstrip().split(maxsplit=1)[0].upper()
        if verb in {"ALTER", "CREATE", "DELETE", "DROP", "INSERT", "REPLACE", "TRUNCATE", "UPDATE"}:
            writes.append(statement)

    monkeypatch.setattr(StateDB, "_classify_terminal_delivery_history_once", reject)
    event.listen(Engine, "before_cursor_execute", record)
    try:
        await _migrate(store_url)
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    (write,) = writes
    assert "schema_meta" in write and "version" in write
    version, after, _ = await _snapshot(store_url)
    assert version == "7"
    assert after == before

    with monkeypatch.context() as old:
        old.setattr(state_db, "SCHEMA_VERSION", "6")
        store = StateDB(url=store_url)
        try:
            with pytest.raises(SchemaTooNewError):
                await store.open()
        finally:
            await store.close()
    assert (await _snapshot(store_url))[0] == "7"


async def test_version_five_migration_rolls_back_shape_and_rows_then_retries(
    store_url, monkeypatch
):
    await _seed(store_url, "5")
    _, before, _ = await _snapshot(store_url)
    store = StateDB(url=store_url)

    async def fail_before_stamp(_self, conn):
        assert "delivery_eligible" in await _transition_columns(conn)
        assert await conn.scalar(text(_VERSION)) == "5"
        raise RuntimeError("injected failure before schema stamp")

    with monkeypatch.context() as fault:
        fault.setattr(StateDB, "_stamp_schema_version", fail_before_stamp, raising=False)
        try:
            with pytest.raises(RuntimeError, match="injected failure before schema stamp"):
                await store.migrate()
        finally:
            await store.close()
    version, catalog, _ = await _snapshot(store_url)
    assert (version, catalog) == ("5", before)

    # Exercise a matching version's transition write without any new column or table.
    engine = create_async_engine(store_url)
    try:
        async with engine.begin() as conn:
            assert "delivery_eligible" not in await _transition_columns(conn)
            await conn.execute(text(_TRANSITION), {"id": "after_failure"})

        await _migrate(store_url)
        async with engine.connect() as conn:
            assert await conn.scalar(text(_VERSION)) == state_db.SCHEMA_VERSION
            assert (
                await conn.execute(
                    text("SELECT id, delivery_eligible FROM status_transitions ORDER BY id")
                )
            ).all() == [("after_failure", True), ("historical", True)]
    finally:
        await engine.dispose()


async def test_newer_version_refuses_open_and_migrate_even_with_opt_in(store_url, monkeypatch):
    await _seed(store_url, str(int(state_db.SCHEMA_VERSION) + 1))
    monkeypatch.setenv("LIONAGI_STATE_AUTO_MIGRATE", "1")
    before = await _snapshot(store_url)
    store = StateDB(url=store_url)
    try:
        for operation in ("open", "migrate"):
            with pytest.raises(SchemaTooNewError):
                await getattr(store, operation)()
            assert store._engine is None
    finally:
        await store.close()
    assert await _snapshot(store_url) == before


async def test_writable_open_migrates_only_when_opted_in(store_url, monkeypatch):
    await _seed(store_url, "5")
    monkeypatch.setenv("LIONAGI_STATE_AUTO_MIGRATE", "1")
    async with StateDB(url=store_url) as store:
        assert await store.schema_version() == state_db.SCHEMA_VERSION
        rows = await store.fetch_all("SELECT delivery_eligible FROM status_transitions")
        assert len(rows) == 1
        assert rows[0]["delivery_eligible"]


async def test_fresh_and_current_stores_open_without_opt_in(store_url):
    async with StateDB(url=store_url) as store:
        assert await store.schema_version() == state_db.SCHEMA_VERSION
    before = await _snapshot(store_url)
    async with StateDB(url=store_url) as store:
        assert await store.schema_version() == state_db.SCHEMA_VERSION
    assert await _snapshot(store_url) == before


@pytest.mark.parametrize("competing_operation", ["open", "migrate"])
async def test_competing_callers_raise_the_version_once(
    store_url, monkeypatch, competing_operation
):
    await _seed(store_url, "6")
    reached_stamp = asyncio.Event()
    may_stamp = asyncio.Event()
    competing_started = asyncio.Event()
    real_stamp = StateDB._stamp_schema_version
    stamps = []

    async def pause_at_stamp(store, conn):
        stamps.append(await conn.scalar(text(_VERSION)))
        reached_stamp.set()
        await may_stamp.wait()
        return await real_stamp(store, conn)

    async def compete():
        store = StateDB(url=store_url)
        competing_started.set()
        try:
            await getattr(store, competing_operation)()
        except state.SchemaMigrationRequiredError:
            assert competing_operation == "open"
            assert store._engine is None
        finally:
            await store.close()

    monkeypatch.setattr(StateDB, "_stamp_schema_version", pause_at_stamp)
    first = asyncio.create_task(_migrate(store_url))
    tasks = [first]
    try:
        await asyncio.wait_for(reached_stamp.wait(), timeout=10)
        tasks.append(asyncio.create_task(compete()))
        await asyncio.wait_for(competing_started.wait(), timeout=10)
        may_stamp.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    finally:
        may_stamp.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert stamps == ["6"]
    assert (await _snapshot(store_url))[0] == state_db.SCHEMA_VERSION
