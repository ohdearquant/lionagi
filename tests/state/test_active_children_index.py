# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The index that lets a poll read one invocation's running children by seek.

The invariant: reading the running children of a named invocation, in creation
order, up to a limit, costs the rows asked for and not the table.
"""

from __future__ import annotations

# The clause the callers carry, spelled out rather than imported. A poll reads
# sessions that are not the mirrored half of a CLI transcript, and the plan has
# to hold up with that clause present, but the index is a schema fact and this
# file should not need a service to state it.
_NOT_ENGINE_MIRROR = "json_extract(sessions.node_metadata, '$.engine_parent_run_id') IS NULL"

_INDEX_NAME = "idx_sessions_invocation_status_created"

_CAPPED_READ = (
    "SELECT * FROM sessions WHERE status = 'running' AND invocation_id = 'inv1' "
    f"AND {_NOT_ENGINE_MIRROR} ORDER BY created_at, id LIMIT 50"
)
_MULTI_INVOCATION_READ = (
    "SELECT * FROM sessions WHERE status = 'running' "
    f"AND invocation_id IN ('a','b','c') AND {_NOT_ENGINE_MIRROR} "
    "ORDER BY invocation_id, created_at, id"
)


def _plan(statement: str) -> list[str]:
    """Plan a statement against the metadata the runtime creates databases from.

    Not the reference schema file: the two have drifted before, and only the
    metadata decides what a running instance actually indexes.
    """
    import sqlalchemy as sa

    from lionagi.state.schema_meta import metadata

    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        metadata.create_all(conn)
        rows = conn.execute(sa.text("EXPLAIN QUERY PLAN " + statement))
        return [str(row[-1]) for row in rows]


def test_reading_an_invocations_children_neither_sorts_nor_scans_the_table():
    """A limit bounds the rows returned, which is not the same as bounding the
    work. Matching on status alone, sqlite ordered the result in a temp b-tree,
    so the read visited and sorted every running session in the database before
    the limit could discard any of them. Polling is what makes that repeat."""
    for name, statement in (("capped", _CAPPED_READ), ("narrow", _MULTI_INVOCATION_READ)):
        plan = _plan(statement)
        assert not any("TEMP B-TREE" in step.upper() for step in plan), f"{name} sorts: {plan}"
        assert any(_INDEX_NAME in step for step in plan), f"{name} does not seek: {plan}"


def test_the_plan_guard_can_see_a_sort():
    """Control. The assertion above passes trivially if EXPLAIN QUERY PLAN ever
    stops reporting sorts, or if the strings it matches on change spelling. This
    orders by a column no index covers, so a working guard must find a sort."""
    plan = _plan("SELECT * FROM sessions WHERE invocation_id = 'inv1' ORDER BY name")
    assert any("TEMP B-TREE" in step.upper() for step in plan), plan


async def test_an_existing_store_gains_the_index_when_it_is_opened(tmp_path):
    """Declaring an index in the table metadata reaches new databases only:
    `metadata.create_all` skips a table that already exists and skips its
    indexes with it. Every store created before the declaration would go on
    running the scan, which is every store that matters."""
    import aiosqlite as aio

    from lionagi.state.db import StateDB

    db_path = tmp_path / "state.db"

    async def index_names() -> set[str]:
        """Read without StateDB. Opening one runs the migration, so a read that
        went through it would create the index it is being asked about."""
        async with aio.connect(str(db_path)) as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='sessions'"
            )
            return {row[0] for row in await cursor.fetchall()}

    async with StateDB(db_path):
        pass
    assert _INDEX_NAME in await index_names(), "a fresh store should already have it"

    # Stand in for a database created before the index was declared.
    async with aio.connect(str(db_path)) as conn:
        await conn.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
        await conn.commit()
    without = await index_names()
    assert _INDEX_NAME not in without
    # Control: dropping one index did not empty the set, so the read works.
    assert len(without) > 3, without

    async with StateDB(db_path):
        pass

    assert _INDEX_NAME in await index_names()


def test_all_three_carriers_declare_the_same_index(tmp_path):
    """The index is written down three times: in the reference schema, in the
    table metadata new stores are built from, and in the migration existing
    stores are opened with. Only the last two decide what a running instance
    indexes, so a reference schema that disagrees is a document that describes
    a database nobody has -- and column ORDER is the part that decides whether
    the read seeks or sorts, so agreeing on the name is not agreeing."""
    import re
    import sqlite3

    import sqlalchemy as sa

    from lionagi.state.db import _SCHEMA_PATH
    from lionagi.state.schema_meta import metadata
    from lionagi.state.schema_migrations import MIGRATION_INDEXES

    expected = ["invocation_id", "status", "created_at", "id"]

    raw_db = tmp_path / "raw.db"
    schema_text = _SCHEMA_PATH.read_text()
    lines = [ln for ln in schema_text.splitlines() if not ln.strip().upper().startswith("PRAGMA")]
    conn = sqlite3.connect(str(raw_db))
    conn.executescript("\n".join(lines))
    conn.commit()
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA index_info({_INDEX_NAME})")
    raw_cols = [row[2] for row in cursor.fetchall()]
    conn.close()
    assert raw_cols == expected, raw_cols

    engine = sa.create_engine("sqlite://")
    with engine.begin() as sa_conn:
        metadata.create_all(sa_conn)
        meta_indexes = {
            index["name"]: index["column_names"]
            for index in sa.inspect(sa_conn).get_indexes("sessions")
        }
    assert meta_indexes.get(_INDEX_NAME) == expected, meta_indexes.get(_INDEX_NAME)

    for dialect in ("sqlite", "postgresql"):
        (statement,) = (sql for sql in MIGRATION_INDEXES[dialect] if _INDEX_NAME in sql)
        cols = [c.strip() for c in re.search(r"sessions\(([^)]+)\)", statement).group(1).split(",")]
        assert cols == expected, (dialect, cols)
