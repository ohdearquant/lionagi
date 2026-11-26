# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""The optional index for recent sessions retains malformed metadata rows."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa

from lionhub.state.db import SCHEMA_VERSION, StateDB
from lionhub.state.schema_meta import metadata
from lionhub.state.schema_migrations import MIGRATION_INDEXES

INDEX = "idx_sessions_recent_visible"
ORDER_INDEX = "idx_sessions_updated"
VISIBLE_PREDICATE = "json_extract(node_metadata, '$.engine_parent_run_id') IS NULL"
INDEX_PREDICATE = f"node_metadata IS NULL OR NOT json_valid(node_metadata) OR {VISIBLE_PREDICATE}"


def _index_sql(connection: sqlite3.Connection, index: str = INDEX) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index,)
    ).fetchone()
    return row[0] if row else None


def _index_shape(connection: sqlite3.Connection, index: str):
    sql = _index_sql(connection, index)
    assert sql is not None
    keys = connection.execute(
        'SELECT name, "desc", coll FROM pragma_index_xinfo(?) WHERE "key" ORDER BY seqno',
        (index,),
    ).fetchall()
    predicate = " ".join(sql.split()).partition(" WHERE ")[2]
    return keys, predicate or None


def _insert_sessions_with_equal_timestamps(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO progressions (id, created_at, collection) VALUES ('p', 0, '[]')"
    )
    connection.executemany(
        "INSERT INTO sessions (id, progression_id, created_at, updated_at, node_metadata) "
        "VALUES (?, 'p', 0, 10, ?)",
        [
            ("v1", "{}"),
            ("h", '{"engine_parent_run_id":"parent"}'),
            ("v2", None),
            ("v3", '{"engine_parent_run_id":null}'),
        ],
    )


def _recent_session_ids(connection: sqlite3.Connection, index: str) -> list[str]:
    predicate = VISIBLE_PREDICATE
    if index == INDEX:
        predicate = f"({INDEX_PREDICATE}) AND {predicate}"
    rows = connection.execute(
        f"SELECT id FROM sessions INDEXED BY {index} "
        f"WHERE {predicate} ORDER BY updated_at DESC LIMIT 3"
    )
    return [row[0] for row in rows]


def _exercise_recent_index(connection: sqlite3.Connection) -> list[str]:
    _insert_sessions_with_equal_timestamps(connection)
    connection.execute(
        "INSERT INTO sessions (id, progression_id, created_at, updated_at, node_metadata) "
        "VALUES ('bad', 'p', 0, 0, '{bad')"
    )
    old = _recent_session_ids(connection, ORDER_INDEX)
    new = _recent_session_ids(connection, INDEX)
    assert new == old
    assert set(new) == {"v1", "v2", "v3"}
    connection.execute("UPDATE sessions SET node_metadata = '{still-bad' WHERE id='bad'")
    return new


def test_reference_schema_and_sqlite_metadata_create_the_optional_index():
    from lionhub.state.db import _SCHEMA_PATH

    engine = sa.create_engine("sqlite://")
    try:
        with closing(sqlite3.connect(":memory:")) as raw, engine.begin() as conn:
            raw.executescript(_SCHEMA_PATH.read_text())
            metadata.create_all(conn)
            sqlalchemy_db = conn.connection.driver_connection

            expected = {
                ORDER_INDEX: ([("updated_at", 0, "BINARY")], None),
                INDEX: ([("updated_at", 0, "BINARY")], INDEX_PREDICATE),
            }
            raw_indexes = {name: _index_shape(raw, name) for name in expected}
            metadata_indexes = {name: _index_shape(sqlalchemy_db, name) for name in expected}
            assert raw_indexes == metadata_indexes == expected
            assert _exercise_recent_index(raw) == _exercise_recent_index(sqlalchemy_db)
    finally:
        engine.dispose()


def test_postgresql_schema_creation_does_not_create_the_sqlite_index():
    emitted: list[str] = []
    engine = sa.create_mock_engine(
        "postgresql://",
        lambda sql, *args, **kwargs: emitted.append(str(sql.compile(dialect=engine.dialect))),
    )
    metadata.create_all(engine)
    assert not any(INDEX in statement for statement in emitted)
    assert not any(INDEX in statement for statement in MIGRATION_INDEXES["postgresql"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("direction", "desc"), [("ASC", 0), ("DESC", 1)])
@pytest.mark.parametrize("index_exists", [False, True], ids=["missing-index", "existing-index"])
async def test_legacy_malformed_metadata_remains_openable_and_writable(
    tmp_path, direction, desc, index_exists
):
    path = tmp_path / "state.db"
    async with StateDB(path) as store:
        assert await store.schema_version() == SCHEMA_VERSION
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"DROP INDEX IF EXISTS {INDEX}")
        connection.execute(f"DROP INDEX {ORDER_INDEX}")
        connection.execute(f"CREATE INDEX {ORDER_INDEX} ON sessions(updated_at {direction})")
        if index_exists:
            connection.execute(
                f"CREATE INDEX {INDEX} ON sessions(updated_at {direction}) WHERE {INDEX_PREDICATE}"
            )
        original_order_index = _index_sql(connection, ORDER_INDEX)
        original_candidate_index = _index_sql(connection)
        _insert_sessions_with_equal_timestamps(connection)
        connection.execute(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, node_metadata) "
            "VALUES ('legacy', 'p', 0, 1, '{bad')"
        )
        old_order = _recent_session_ids(connection, ORDER_INDEX)
        assert set(old_order) == {"v1", "v2", "v3"}

    async with StateDB(path) as store:
        assert await store.schema_version() == SCHEMA_VERSION
    with closing(sqlite3.connect(path)) as connection, connection:
        assert _index_sql(connection, ORDER_INDEX) == original_order_index
        assert _index_shape(connection, INDEX) == (
            [("updated_at", desc, "BINARY")],
            INDEX_PREDICATE,
        )
        if index_exists:
            assert _index_sql(connection) == original_candidate_index
        assert _recent_session_ids(connection, ORDER_INDEX) == old_order
        assert _recent_session_ids(connection, INDEX) == old_order
        assert (
            connection.execute("SELECT node_metadata FROM sessions WHERE id='legacy'").fetchone()[0]
            == "{bad"
        )
        connection.execute(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, node_metadata) "
            "VALUES ('later', 'p', 0, 2, '{bad-again')"
        )
        connection.execute("UPDATE sessions SET node_metadata = '{new-bad' WHERE id='legacy'")
        assert (
            connection.execute("SELECT node_metadata FROM sessions WHERE id='legacy'").fetchone()[0]
            == "{new-bad"
        )


@pytest.mark.asyncio
async def test_existing_same_name_index_is_not_replaced_at_open(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute(f"DROP INDEX IF EXISTS {INDEX}")
        connection.execute(f"CREATE INDEX {INDEX} ON sessions(status)")
        original = _index_sql(connection)
    async with StateDB(path):
        pass
    with sqlite3.connect(path) as connection:
        assert _index_sql(connection) == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_index",
    [
        None,
        "ON sessions(status)",
        "ON sessions(updated_at, status)",
        "ON sessions(updated_at COLLATE NOCASE)",
        "ON sessions(updated_at) WHERE status = 'running'",
    ],
    ids=["missing", "wrong-column", "multiple-keys", "wrong-collation", "partial"],
)
async def test_unusable_order_index_does_not_install_candidate(tmp_path, old_index):
    path = tmp_path / "state.db"
    async with StateDB(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute(f"DROP INDEX IF EXISTS {INDEX}")
        connection.execute("DROP INDEX idx_sessions_updated")
        if old_index is not None:
            connection.execute(f"CREATE INDEX idx_sessions_updated {old_index}")
    async with StateDB(path):
        pass
    with sqlite3.connect(path) as connection:
        assert _index_sql(connection) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["4", "5"])
async def test_legacy_schema_upgrade_preserves_order_and_malformed_metadata(tmp_path, version):
    path = tmp_path / f"v{version}.db"
    fixture = Path(__file__).resolve().parent / "fixtures" / f"state_schema_v{version}.sql"
    with sqlite3.connect(path) as connection:
        connection.executescript(fixture.read_text())
        assert connection.execute("PRAGMA index_xinfo(idx_sessions_updated)").fetchone()[3] == 1
        connection.execute(
            "INSERT INTO progressions (id, created_at, collection) VALUES ('p', 0, '[]')"
        )
        connection.execute(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, node_metadata) "
            "VALUES ('bad', 'p', 0, 0, '{bad')"
        )

    store = StateDB(path)
    await store.migrate()
    async with store:
        assert await store.schema_version() == SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        assert _index_sql(connection) is not None
        old_direction = connection.execute("PRAGMA index_xinfo(idx_sessions_updated)").fetchone()[3]
        new_direction = connection.execute(f"PRAGMA index_xinfo({INDEX})").fetchone()[3]
        assert old_direction == new_direction == 1
        connection.execute("UPDATE sessions SET node_metadata = '{still-bad' WHERE id='bad'")
        assert (
            connection.execute("SELECT node_metadata FROM sessions WHERE id='bad'").fetchone()[0]
            == "{still-bad"
        )
