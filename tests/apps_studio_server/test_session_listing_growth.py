# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Measure page projection separately from the route's exact total."""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

import pytest

from lionagi.state import db as db_module
from lionagi.studio.services import sessions


@pytest.fixture
def listing_store(tmp_path, monkeypatch, request):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        schema = db_module._SCHEMA_PATH.read_text()
        if getattr(request, "param", None) == "nocase":
            column = (
                "last_msg_id     TEXT    REFERENCES messages(id),\n"
                "  updated_at      REAL    NOT NULL,"
            )
            index = "ON sessions(updated_at DESC);"
            assert schema.count(column) == 1 and schema.count(index) == 1
            schema = schema.replace(
                column, column.replace("REAL    NOT NULL", "REAL COLLATE NOCASE NOT NULL")
            )
            schema = schema.replace(index, "ON sessions(updated_at COLLATE BINARY DESC);")
        conn.executescript(schema)
        conn.executemany(
            "INSERT INTO progressions (id, created_at, collection) VALUES (?, 0, ?)",
            [("pool", "[]"), ("page", '["one", "one", "missing"]')],
        )
        conn.executemany(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, status) "
            "VALUES (?, 'pool', 0, ?, 'completed')",
            [(f"page-{i:02}", 100_000 - i) for i in range(20)],
        )
        conn.executemany(
            "INSERT INTO branches (id, created_at, session_id, progression_id) "
            "VALUES (?, 0, ?, 'page')",
            [(f"page-branch-{i:02}", f"page-{i:02}") for i in range(20)],
        )
        # The newest row is a mirrored child and must not enter either result.
        conn.execute(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, status, "
            "node_metadata) VALUES ('mirror', 'pool', 0, 200000, 'completed', ?)",
            (json.dumps({"engine_parent_run_id": "parent"}),),
        )
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_module, "settings", db_module.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    return path


async def test_recent_route_reports_page_and_count_work_separately(
    listing_store, monkeypatch, record_property
):
    """A fixed page stays bounded; the route still performs an exact count."""
    observations = []
    real_open = sessions._open_db

    @asynccontextmanager
    async def observe_queries(path):
        async with real_open(path) as conn:
            execute = conn.execute
            observed = None
            steps = [0]
            page_start = [0]

            def tick():
                steps[0] += 1
                return 0

            async def capture(sql, parameters=None):
                nonlocal observed
                normalized = " ".join(sql.lower().split())
                is_page = "as branch_count" in normalized and "as message_count" in normalized
                is_count = normalized.startswith("select count(*) as n from sessions s")
                if is_page or is_count:
                    assert observed is None, "expected one measured statement per connection"
                    args = parameters if parameters is not None else ()
                    # Exclude the test observer's own EXPLAIN from the total.
                    await conn.set_progress_handler(None, 0)
                    cursor = await execute("EXPLAIN QUERY PLAN " + sql, args)
                    plan = [row[3] for row in await cursor.fetchall()]
                    await cursor.close()
                    observed = {"kind": "page" if is_page else "count", "plan": plan}
                    page_start[0] = steps[0]
                    await conn.set_progress_handler(tick, 1)
                return await execute(sql, parameters)

            monkeypatch.setattr(conn, "execute", capture)
            await conn.set_progress_handler(tick, 1)
            try:
                yield conn
            finally:
                await conn.set_progress_handler(None, 0)
                if observed is not None:
                    observed["steps"] = steps[0] - page_start[0]
                    observed["connection_steps"] = steps[0]
                    observations.append(observed)

    monkeypatch.setattr(sessions, "_open_db", observe_queries)
    measures = []
    previous = 0
    for population in (100, 10_000):
        with sqlite3.connect(listing_store) as conn:
            conn.executemany(
                "INSERT INTO sessions (id, progression_id, created_at, updated_at, status) "
                "VALUES (?, 'pool', 0, ?, 'completed')",
                [(f"background-{i}", -1000 - i) for i in range(previous, population)],
            )
            conn.executemany(
                "INSERT INTO branches (id, created_at, session_id, progression_id) "
                "VALUES (?, 0, ?, 'pool')",
                [
                    (f"background-branch-{i}", f"background-{i}")
                    for i in range(previous, population)
                ],
            )
        previous = population
        start = len(observations)
        response = await sessions.list_sessions_route(limit=20, offset=0, sort="recent")
        measured = observations[start:]
        assert [entry["kind"] for entry in measured] == ["page", "count"]
        assert all(entry["steps"] > 0 for entry in measured), measured
        assert [row["id"] for row in response["sessions"]] == [f"page-{i:02}" for i in range(20)]
        assert all(
            (row["branch_count"], row["message_count"]) == (1, 3) for row in response["sessions"]
        )
        assert response["total"] == population + 20
        assert response["truncated"] is True
        page, count = measured
        measures.append({"population": population, "page": page, "count": count})

    record_property(
        "listing_growth", json.dumps({"sqlite_version": sqlite3.sqlite_version, "runs": measures})
    )
    for run in measures:
        plan = run["page"]["plan"]
        assert any("idx_sessions_recent_visible" in part for part in plan), plan
        assert not any("USE TEMP B-TREE FOR ORDER BY" in part.upper() for part in plan), plan
        assert not any("USE TEMP B-TREE FOR GROUP BY" in part.upper() for part in plan), plan
    page_steps = [run["page"]["steps"] for run in measures]
    assert max(page_steps) <= 2 * min(page_steps), measures
    # Exact totals are separately measured, not included in the page-work claim.


@pytest.mark.parametrize(
    ("listing_store", "replacement"),
    [(None, None), (None, "status"), (None, "partial"), ("nocase", "nocase")],
    indirect=["listing_store"],
)
async def test_recent_listing_without_order_index_does_not_read_off_page_json(
    listing_store, replacement, monkeypatch
):
    with sqlite3.connect(listing_store) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_sessions_recent_visible")
        if replacement != "nocase":
            conn.execute("DROP INDEX idx_sessions_updated")
        if replacement == "status":
            conn.execute("CREATE INDEX idx_sessions_updated ON sessions(status, updated_at)")
        elif replacement == "partial":
            conn.execute(
                "CREATE INDEX idx_sessions_updated ON sessions(updated_at) WHERE status = 'running'"
            )
        conn.execute("INSERT INTO progressions VALUES ('bad', 0, 'not-json')")
        conn.execute("UPDATE sessions SET updated_at = -1 WHERE id = 'page-00'")
        conn.execute("UPDATE branches SET progression_id = 'bad' WHERE id = 'page-branch-00'")
        conn.execute(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, status) "
            "VALUES ('page-20', 'pool', 0, 99980, 'completed')"
        )
    statements = []
    real_open = sessions._open_db

    @asynccontextmanager
    async def capture_listing(path):
        async with real_open(path) as conn:
            execute = conn.execute

            async def capture(sql, parameters=None):
                if "AS branch_count" in sql and "AS message_count" in sql:
                    statements.append(" ".join(sql.lower().split()))
                    assert statements[-1].startswith("with page as"), statements[-1]
                return await execute(sql, parameters)

            monkeypatch.setattr(conn, "execute", capture)
            yield conn

    monkeypatch.setattr(sessions, "_open_db", capture_listing)
    rows = await sessions.list_sessions(limit=20, sort="recent")
    assert len(statements) == 1 and statements[0].startswith("with page as"), statements
    assert [row["id"] for row in rows] == [f"page-{i:02}" for i in range(1, 21)]
    assert all(row["message_count"] == 3 for row in rows[:-1])
    assert rows[-1]["message_count"] == 0


async def test_hidden_newer_mirrors_do_not_increase_recent_scan_work(
    listing_store, monkeypatch, record_property
):
    """Adding hidden mirrors ahead of the page does not grow page-selection work."""
    work = []
    real_open = sessions._open_db

    @asynccontextmanager
    async def observe_page(path):
        async with real_open(path) as conn:
            execute = conn.execute
            steps = [0]
            seen = False

            def tick():
                steps[0] += 1
                return 0

            async def capture(sql, parameters=None):
                nonlocal seen
                if "AS branch_count" in sql and "AS message_count" in sql:
                    assert not seen
                    seen = True
                    await conn.set_progress_handler(tick, 1)
                return await execute(sql, parameters)

            monkeypatch.setattr(conn, "execute", capture)
            try:
                yield conn
            finally:
                await conn.set_progress_handler(None, 0)
                if seen:
                    work.append(steps[0])

    monkeypatch.setattr(sessions, "_open_db", observe_page)
    previous = 0
    for population in (100, 1_000):
        with sqlite3.connect(listing_store) as conn:
            conn.executemany(
                "INSERT INTO sessions (id, progression_id, created_at, updated_at, status, "
                "node_metadata) VALUES (?, 'pool', 0, ?, 'completed', ?)",
                [
                    (f"hidden-{i}", 300_000 + i, '{"engine_parent_run_id":"parent"}')
                    for i in range(previous, population)
                ],
            )
        previous = population
        rows = await sessions.list_sessions(limit=20, sort="recent")
        assert [row["id"] for row in rows] == [f"page-{i:02}" for i in range(20)]
    record_property("hidden_mirror_scan_steps", json.dumps(work))
    assert len(work) == 2 and min(work) > 0, work
    assert max(work) < 1.5 * min(work), work


@pytest.mark.parametrize("index_kind", ["missing", "wrong_predicate"])
async def test_visible_index_fallback_preserves_complete_page(listing_store, index_kind):
    """An old or unrelated index with the same name cannot exclude visible rows."""
    with sqlite3.connect(listing_store) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_sessions_recent_visible")
        if index_kind == "wrong_predicate":
            conn.execute(
                "CREATE INDEX idx_sessions_recent_visible ON sessions(updated_at DESC) "
                "WHERE status = 'running'"
            )
    rows = await sessions.list_sessions(limit=5, offset=3, sort="recent")
    assert [row["id"] for row in rows] == [f"page-{i:02}" for i in range(3, 8)]
    assert [(row["branch_count"], row["message_count"]) for row in rows] == [(1, 3)] * 5


@pytest.mark.parametrize("visible_index", [True, False])
async def test_recent_listing_reads_read_only_store(listing_store, visible_index, monkeypatch):
    import aiosqlite

    if not visible_index:
        with sqlite3.connect(listing_store) as conn:
            conn.execute("DROP INDEX IF EXISTS idx_sessions_recent_visible")
    with sqlite3.connect(listing_store) as conn:
        before = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()

    @asynccontextmanager
    async def readonly(path):
        async with aiosqlite.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    monkeypatch.setattr(sessions, "_open_db", readonly)
    rows = await sessions.list_sessions(limit=5, sort="recent")
    assert [row["id"] for row in rows] == [f"page-{i:02}" for i in range(5)]
    with sqlite3.connect(listing_store) as conn:
        after = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    assert after == before


@pytest.mark.parametrize("visible_index", [True, False])
async def test_recent_listing_malformed_metadata_preserves_error_and_order(
    listing_store, visible_index
):
    with sqlite3.connect(listing_store) as conn:
        if not visible_index:
            conn.execute("DROP INDEX IF EXISTS idx_sessions_recent_visible")
        conn.execute(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, status, "
            "node_metadata) VALUES ('bad-metadata', 'pool', 0, -100, 'completed', '{bad')"
        )
    rows = await sessions.list_sessions(limit=5, sort="recent")
    assert [row["id"] for row in rows] == [f"page-{i:02}" for i in range(5)]
    with sqlite3.connect(listing_store) as conn:
        conn.execute("UPDATE sessions SET updated_at = 400000 WHERE id = 'bad-metadata'")
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        await sessions.list_sessions(limit=5, sort="recent")


async def test_recent_listing_visibility_follows_parent_value_not_truthiness(listing_store):
    entries = [
        ("missing-parent", 400005, "{}"),
        ("null-parent", 400004, '{"engine_parent_run_id":null}'),
        ("zero-parent", 400003, '{"engine_parent_run_id":0}'),
        ("false-parent", 400002, '{"engine_parent_run_id":false}'),
        ("empty-parent", 400001, '{"engine_parent_run_id":""}'),
    ]
    with sqlite3.connect(listing_store) as conn:
        conn.executemany(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, status, "
            "node_metadata) VALUES (?, 'pool', 0, ?, 'completed', ?)",
            entries,
        )
    rows = await sessions.list_sessions(limit=4, sort="recent")
    assert [row["id"] for row in rows] == [
        "missing-parent",
        "null-parent",
        "page-00",
        "page-01",
    ]


async def test_recent_listing_preserves_equal_timestamp_order(listing_store, monkeypatch):
    """A partial-index scan must preserve the old scan's tie order and page boundary."""
    with sqlite3.connect(listing_store) as conn:
        conn.execute("UPDATE sessions SET updated_at = 100000 WHERE id LIKE 'page-%'")
        definition = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_sessions_recent_visible'"
        ).fetchone()[0]
        conn.execute("DROP INDEX idx_sessions_recent_visible")
    statements = []
    real_open = sessions._open_db

    @asynccontextmanager
    async def capture_listing(path):
        async with real_open(path) as conn:
            execute = conn.execute

            async def capture(sql, parameters=None):
                if "AS branch_count" in sql and "AS message_count" in sql:
                    statements.append(" ".join(sql.lower().split()))
                return await execute(sql, parameters)

            monkeypatch.setattr(conn, "execute", capture)
            yield conn

    monkeypatch.setattr(sessions, "_open_db", capture_listing)
    baseline = await sessions.list_sessions(limit=7, offset=3, sort="recent")
    expected = [row["id"] for row in baseline]
    assert len(expected) == 7 and all(name.startswith("page-") for name in expected)
    with sqlite3.connect(listing_store) as conn:
        conn.execute(definition)
    compatible = await sessions.list_sessions(limit=7, offset=3, sort="recent")
    assert [row["id"] for row in compatible] == expected
    assert "indexed by idx_sessions_recent_visible" in statements[-1]
    with sqlite3.connect(listing_store) as conn:
        conn.execute("DROP INDEX idx_sessions_recent_visible")
        if "updated_at ASC" in definition:
            incompatible = definition.replace("updated_at ASC", "updated_at DESC")
        else:
            assert "updated_at DESC" in definition
            incompatible = definition.replace("updated_at DESC", "updated_at ASC")
        conn.execute(incompatible)
    fallback = await sessions.list_sessions(limit=7, offset=3, sort="recent")
    assert [row["id"] for row in fallback] == expected
    assert "indexed by idx_sessions_recent_visible" not in statements[-1]


@pytest.mark.parametrize("message", ["no query solution", "disk I/O error"])
async def test_visible_index_probe_falls_back_only_for_no_query_solution(
    listing_store, monkeypatch, message
):
    real_open = sessions._open_db
    injected = []
    statements = []

    @asynccontextmanager
    async def inject_probe_failure(path):
        async with real_open(path) as conn:
            original_cursor = conn.cursor
            original_execute = conn.execute

            async def cursor_with_failure():
                cursor = await original_cursor()
                execute = cursor.execute

                async def capture(sql, parameters=None):
                    if (
                        "EXPLAIN QUERY PLAN" in sql
                        and "INDEXED BY idx_sessions_recent_visible" in sql
                    ):
                        injected.append(sql)
                        raise sqlite3.OperationalError(message)
                    return await execute(sql, parameters)

                monkeypatch.setattr(cursor, "execute", capture)
                return cursor

            async def capture_listing(sql, parameters=None):
                if "AS branch_count" in sql and "AS message_count" in sql:
                    statements.append(sql)
                return await original_execute(sql, parameters)

            monkeypatch.setattr(conn, "cursor", cursor_with_failure)
            monkeypatch.setattr(conn, "execute", capture_listing)
            yield conn

    monkeypatch.setattr(sessions, "_open_db", inject_probe_failure)
    if message == "no query solution":
        rows = await sessions.list_sessions(limit=5, sort="recent")
        assert [row["id"] for row in rows] == [f"page-{i:02}" for i in range(5)]
        assert len(statements) == 1 and "INDEXED BY idx_sessions_updated" in statements[0]
    else:
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            await sessions.list_sessions(limit=5, sort="recent")
        assert not statements
    assert len(injected) == 1


@pytest.mark.parametrize("replacement", ["extra_key", "collation", "broader_predicate"])
async def test_visible_index_wrong_shape_falls_back(listing_store, monkeypatch, replacement):
    with sqlite3.connect(listing_store) as conn:
        definition = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_sessions_recent_visible'"
        ).fetchone()[0]
        conn.execute("DROP INDEX idx_sessions_recent_visible")
        assert "updated_at DESC" in definition
        if replacement == "broader_predicate":
            conn.execute(definition + " OR status = 'completed'")
        else:
            index_key = (
                "updated_at DESC, id"
                if replacement == "extra_key"
                else "updated_at COLLATE NOCASE DESC"
            )
            conn.execute(definition.replace("updated_at DESC", index_key))
    real_open = sessions._open_db
    statements = []

    @asynccontextmanager
    async def capture_listing(path):
        async with real_open(path) as conn:
            execute = conn.execute

            async def capture(sql, parameters=None):
                if "AS branch_count" in sql and "AS message_count" in sql:
                    statements.append(sql)
                return await execute(sql, parameters)

            monkeypatch.setattr(conn, "execute", capture)
            yield conn

    monkeypatch.setattr(sessions, "_open_db", capture_listing)
    rows = await sessions.list_sessions(limit=5, offset=3, sort="recent")
    assert [row["id"] for row in rows] == [f"page-{i:02}" for i in range(3, 8)]
    assert len(statements) == 1 and "INDEXED BY idx_sessions_recent_visible" not in statements[0]


async def test_native_store_asc_index_preserves_tie_page(tmp_path, monkeypatch):
    path = tmp_path / "native.db"
    async with db_module.StateDB(path):
        pass
    with sqlite3.connect(path) as conn:
        for name in ("idx_sessions_updated", "idx_sessions_recent_visible"):
            assert conn.execute(f"PRAGMA index_xinfo('{name}')").fetchone()[3] == 0
        conn.execute("INSERT INTO progressions (id, created_at, collection) VALUES ('p', 0, '[]')")
        conn.executemany(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, status) "
            "VALUES (?, 'p', 0, 100, 'completed')",
            [(f"tie-{i}",) for i in range(10)],
        )
        definition = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_sessions_recent_visible'"
        ).fetchone()[0]
        conn.execute("DROP INDEX idx_sessions_recent_visible")
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_module, "settings", db_module.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    baseline = await sessions.list_sessions(limit=4, offset=3, sort="recent")
    with sqlite3.connect(path) as conn:
        conn.execute(definition)
    real_open = sessions._open_db
    statements = []

    @asynccontextmanager
    async def capture_listing(path):
        async with real_open(path) as conn:
            execute = conn.execute

            async def capture(sql, parameters=None):
                if "AS branch_count" in sql and "AS message_count" in sql:
                    statements.append(sql)
                return await execute(sql, parameters)

            monkeypatch.setattr(conn, "execute", capture)
            yield conn

    monkeypatch.setattr(sessions, "_open_db", capture_listing)
    indexed = await sessions.list_sessions(limit=4, offset=3, sort="recent")
    assert [row["id"] for row in indexed] == [row["id"] for row in baseline]
    assert len(statements) == 1 and "INDEXED BY idx_sessions_recent_visible" in statements[0]


async def test_recent_listing_keeps_schema_snapshot_during_index_drop(listing_store, monkeypatch):
    with sqlite3.connect(listing_store) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    real_open = sessions._open_db
    statements = []
    dropped = False

    @asynccontextmanager
    async def drop_after_index_list(path):
        async with real_open(path) as conn:
            execute = conn.execute

            async def capture(sql, parameters=None):
                nonlocal dropped
                cursor = await execute(sql, parameters)
                if sql == "PRAGMA index_list(sessions)" and not dropped:
                    with sqlite3.connect(listing_store) as writer:
                        writer.execute("DROP INDEX idx_sessions_recent_visible")
                    dropped = True
                if "AS branch_count" in sql and "AS message_count" in sql:
                    statements.append(sql)
                return cursor

            monkeypatch.setattr(conn, "execute", capture)
            yield conn

    monkeypatch.setattr(sessions, "_open_db", drop_after_index_list)
    first = await sessions.list_sessions(limit=5, offset=3, sort="recent")
    assert dropped
    assert [row["id"] for row in first] == [f"page-{i:02}" for i in range(3, 8)]
    assert len(statements) == 1 and "INDEXED BY idx_sessions_recent_visible" in statements[0]
    with sqlite3.connect(listing_store) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'idx_sessions_recent_visible'"
            ).fetchone()
            is None
        )
    second = await sessions.list_sessions(limit=5, offset=3, sort="recent")
    assert [row["id"] for row in second] == [f"page-{i:02}" for i in range(3, 8)]
    assert len(statements) == 2 and "INDEXED BY idx_sessions_recent_visible" not in statements[1]
