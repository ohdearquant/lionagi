# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""The optional index for recent sessions retains malformed metadata rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from lionagi.state.db import SCHEMA_VERSION, StateDB
from lionagi.state.schema_meta import metadata
from lionagi.state.schema_migrations import MIGRATION_INDEXES

INDEX = "idx_sessions_recent_visible"


def _index_sql(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)
    ).fetchone()
    return row[0] if row else None


def test_reference_schema_and_sqlite_metadata_create_the_optional_index():
    from lionagi.state.db import _SCHEMA_PATH

    with sqlite3.connect(":memory:") as connection:
        connection.executescript(_SCHEMA_PATH.read_text())
        assert _index_sql(connection) is not None
        connection.execute(
            "INSERT INTO progressions (id, created_at, collection) VALUES ('p', 0, '[]')"
        )
        connection.execute(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, node_metadata) "
            "VALUES ('bad', 'p', 0, 0, '{bad')"
        )
        connection.executemany(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, node_metadata) "
            "VALUES (?, 'p', 0, 10, ?)",
            [("v1", "{}"), ("v2", None), ("v3", '{"engine_parent_run_id":null}')],
        )
        old_direction = connection.execute("PRAGMA index_xinfo(idx_sessions_updated)").fetchone()[3]
        new_direction = connection.execute(f"PRAGMA index_xinfo({INDEX})").fetchone()[3]
        assert old_direction == new_direction == 1
        predicate = "json_extract(node_metadata, '$.engine_parent_run_id') IS NULL"
        guard = (
            "node_metadata IS NULL OR NOT json_valid(node_metadata) OR "
            "json_extract(node_metadata, '$.engine_parent_run_id') IS NULL"
        )
        old = connection.execute(
            "SELECT id FROM sessions INDEXED BY idx_sessions_updated "
            f"WHERE {predicate} ORDER BY updated_at DESC LIMIT 3"
        ).fetchall()
        new = connection.execute(
            f"SELECT id FROM sessions INDEXED BY {INDEX} "
            f"WHERE ({guard}) AND {predicate} ORDER BY updated_at DESC LIMIT 3"
        ).fetchall()
        assert new == old

    engine = sa.create_engine("sqlite://")
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            assert INDEX in {
                item["name"] for item in sa.inspect(connection).get_indexes("sessions")
            }
            old_direction = connection.exec_driver_sql(
                "PRAGMA index_xinfo(idx_sessions_updated)"
            ).fetchone()[3]
            new_direction = connection.exec_driver_sql(f"PRAGMA index_xinfo({INDEX})").fetchone()[3]
            assert old_direction == new_direction == 0
            connection.exec_driver_sql(
                "INSERT INTO progressions (id, created_at, collection) VALUES ('p', 0, '[]')"
            )
            connection.exec_driver_sql(
                "INSERT INTO sessions (id, progression_id, created_at, updated_at, node_metadata) "
                "VALUES (?, 'p', 0, 10, ?)",
                [
                    ("v1", "{}"),
                    ("h", '{"engine_parent_run_id":"parent"}'),
                    ("v2", None),
                    ("v3", '{"engine_parent_run_id":null}'),
                ],
            )
            predicate = "json_extract(node_metadata, '$.engine_parent_run_id') IS NULL"
            guard = (
                "node_metadata IS NULL OR NOT json_valid(node_metadata) OR "
                "json_extract(node_metadata, '$.engine_parent_run_id') IS NULL"
            )
            old = (
                connection.exec_driver_sql(
                    "SELECT id FROM sessions INDEXED BY idx_sessions_updated "
                    f"WHERE {predicate} ORDER BY updated_at DESC"
                )
                .scalars()
                .all()
            )
            new = (
                connection.exec_driver_sql(
                    f"SELECT id FROM sessions INDEXED BY {INDEX} "
                    f"WHERE ({guard}) AND {predicate} ORDER BY updated_at DESC"
                )
                .scalars()
                .all()
            )
            assert new == old
            assert set(new) == {"v1", "v2", "v3"}
            connection.exec_driver_sql(
                "INSERT INTO sessions (id, progression_id, created_at, updated_at, node_metadata) "
                "VALUES ('bad-meta', 'p', 0, 0, '{bad')"
            )
            connection.exec_driver_sql(
                "UPDATE sessions SET node_metadata = '{still-bad' WHERE id = 'bad-meta'"
            )
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
async def test_legacy_malformed_metadata_remains_openable_and_writable(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path) as store:
        assert await store.schema_version() == SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        connection.execute(f"DROP INDEX IF EXISTS {INDEX}")
        connection.execute(
            "INSERT INTO progressions (id, created_at, collection) VALUES ('p', 0, '[]')"
        )
        connection.execute(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, node_metadata) "
            "VALUES ('legacy', 'p', 0, 1, '{bad')"
        )
    async with StateDB(path) as store:
        assert await store.schema_version() == SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        assert _index_sql(connection) is not None
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
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / f"state_schema_v{version}.sql"
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

    async with StateDB(path) as store:
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
