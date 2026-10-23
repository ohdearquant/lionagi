# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Page aggregates retain membership semantics without regrouping the page."""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

import pytest

from lionagi.state import db as db_module
from lionagi.studio.services import sessions


@pytest.fixture
def projection_store(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(db_module._SCHEMA_PATH.read_text())
        conn.executemany(
            "INSERT INTO progressions (id, created_at, collection) VALUES (?, 0, ?)",
            [
                ("session-pool", "[]"),
                ("shared", '["retained", "pruned", "pruned"]'),
                ("empty", "[]"),
                ("json-null", "null"),
            ],
        )
        conn.executemany(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, name, "
            "user_label, agent_name, status, total_cost_usd, provider, project, "
            "node_metadata, run_id, ended_at_is_approximate) "
            "VALUES (?, 'session-pool', 0, ?, ?, ?, 'agent', ?, ?, 'openai', ?, ?, ?, ?)",
            [
                ("multi", 30, "raw-name", "50% needle", None, 2.5, "alpha", None, "cli", 1),
                ("empty", 20, "empty", None, "failed", None, "alpha", None, None, 0),
                ("free", 15, "free", None, "completed", 0.0, "beta", None, None, 0),
                ("legacy", 10, "legacy", None, None, 1.0, "alpha", None, None, 0),
                (
                    "mirror",
                    40,
                    "mirror",
                    None,
                    "completed",
                    9.0,
                    "alpha",
                    json.dumps({"engine_parent_run_id": "parent"}),
                    None,
                    0,
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO branches (id, created_at, session_id, progression_id) VALUES (?, 0, ?, ?)",
            [
                ("first", "multi", "shared"),
                ("second", "multi", "shared"),
                ("third", "multi", "empty"),
                ("null", "free", "json-null"),
            ],
        )
        conn.commit()
        # A historical dangling progression still contributes a branch, but
        # the absent joined collection contributes no messages.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO branches (id, created_at, session_id, progression_id) "
            "VALUES ('dangling', 0, 'legacy', 'missing')"
        )
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_module, "settings", db_module.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    return path


async def test_projection_preserves_branch_and_membership_counts(projection_store):
    rows = await sessions.list_sessions()
    assert [row["id"] for row in rows] == ["multi", "empty", "free", "legacy"]
    assert {row["id"]: (row["branch_count"], row["message_count"]) for row in rows} == {
        "multi": (3, 6),
        "empty": (0, 0),
        "free": (1, 0),
        "legacy": (1, 0),
    }
    # Membership length counts duplicate and pruned ids; it is not a count of
    # persisted message rows (this fixture intentionally has none).
    multi = rows[0]
    assert multi["name"] == "50% needle"
    assert multi["status"] == "completed"
    assert multi["run_id"] == "cli"
    assert multi["ended_at_is_approximate"] is True
    assert multi["total_cost_usd"] == 2.5
    assert rows[1]["total_cost_usd"] is None
    assert rows[2]["total_cost_usd"] == 0.0


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"statuses": {"completed"}}, ["multi", "free", "legacy"]),
        ({"statuses": {"failed"}, "project": "alpha"}, ["empty"]),
        ({"search": "50% needle"}, ["multi"]),
        ({"project": "alpha"}, ["multi", "empty", "legacy"]),
    ],
)
async def test_projection_keeps_filters_before_pagination(projection_store, filters, expected):
    where = sessions.SessionFilter(**filters)
    rows = await sessions.list_sessions(where=where, limit=2, offset=1)
    assert [row["id"] for row in rows] == expected[1:3]
    assert await sessions.count_sessions(where) == len(expected)


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("recent", ["multi", "empty", "free", "legacy"]),
        ("cost", ["multi", "legacy", "free", "empty"]),
    ],
)
async def test_projection_keeps_page_order(projection_store, monkeypatch, sort, expected):
    real_open = sessions._open_db

    @asynccontextmanager
    async def reversed_unordered_reads(path):
        async with real_open(path) as conn:
            await conn.execute("PRAGMA reverse_unordered_selects = ON")
            yield conn

    monkeypatch.setattr(sessions, "_open_db", reversed_unordered_reads)
    rows = await sessions.list_sessions(sort=sort, limit=2, offset=1)
    assert [row["id"] for row in rows] == expected[1:3]


@pytest.mark.parametrize("sort", ["recent", "cost"])
async def test_projection_never_reads_off_page_collections(projection_store, sort):
    with sqlite3.connect(projection_store) as conn:
        conn.execute("UPDATE progressions SET collection = 'not-json' WHERE id = 'json-null'")
    rows = await sessions.list_sessions(limit=1, sort=sort)
    assert [(row["id"], row["message_count"]) for row in rows] == [("multi", 6)]


async def test_projection_does_not_regroup_page(projection_store, monkeypatch):
    plans = []
    real_open = sessions._open_db

    @asynccontextmanager
    async def capture_plan(path):
        async with real_open(path) as conn:
            execute = conn.execute

            async def capture(sql, parameters=None):
                if sql.lstrip().startswith("WITH page AS"):
                    cursor = await execute("EXPLAIN QUERY PLAN " + sql, parameters)
                    plans.append([row[3] for row in await cursor.fetchall()])
                return await execute(sql, parameters)

            monkeypatch.setattr(conn, "execute", capture)
            yield conn

    monkeypatch.setattr(sessions, "_open_db", capture_plan)
    assert len(await sessions.list_sessions(limit=2, offset=1)) == 2
    assert len(plans) == 1
    assert not any("GROUP BY" in step or "count(DISTINCT)" in step for step in plans[0]), plans[0]


async def test_projection_work_stays_bounded_as_unrelated_sessions_grow(
    projection_store, monkeypatch
):
    """Recent page work stays bounded as unrelated sessions and branches grow."""
    work = []
    real_open = sessions._open_db

    @asynccontextmanager
    async def count_page_work(path):
        async with real_open(path) as conn:
            execute = conn.execute
            steps = [0]

            def tick():
                steps[0] += 1
                return 0

            async def capture(sql, parameters=None):
                if sql.lstrip().startswith("WITH page AS"):
                    await conn.set_progress_handler(tick, 1)
                return await execute(sql, parameters)

            monkeypatch.setattr(conn, "execute", capture)
            try:
                yield conn
            finally:
                await conn.set_progress_handler(None, 0)
                work.append(steps[0])

    monkeypatch.setattr(sessions, "_open_db", count_page_work)
    previous = 0
    for population in (100, 10_000):
        with sqlite3.connect(projection_store) as conn:
            conn.executemany(
                "INSERT INTO sessions (id, progression_id, created_at, updated_at, status) "
                "VALUES (?, 'session-pool', 0, ?, 'completed')",
                [(f"background-{i}", -1000 - i) for i in range(previous, population)],
            )
            conn.executemany(
                "INSERT INTO branches (id, created_at, session_id, progression_id) "
                "VALUES (?, 0, ?, 'session-pool')",
                [
                    (f"background-branch-{i}", f"background-{i}")
                    for i in range(previous, population)
                ],
            )
        previous = population
        rows = await sessions.list_sessions(limit=2, sort="recent")
        assert [(row["id"], row["message_count"]) for row in rows] == [
            ("multi", 6),
            ("empty", 0),
        ]

    assert len(work) == 2 and min(work) > 0
    assert max(work) <= 2 * min(work), work
