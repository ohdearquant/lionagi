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
        assert any("idx_sessions_updated" in part for part in plan), plan
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


async def test_hidden_newer_mirrors_increase_recent_scan_work(
    listing_store, monkeypatch, record_property
):
    """The returned page is bounded, but excluded rows can extend the index scan."""
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
    assert work[1] > 2 * work[0], work
