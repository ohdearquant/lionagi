# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Status filtering preserves legacy rows while seeking the existing index."""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from lionagi.state import db as db_module
from lionagi.state.db import StateDB
from lionagi.state.schema_migrations import MIGRATION_INDEXES
from lionagi.studio.services import sessions


@pytest_asyncio.fixture
async def status_store(tmp_path, monkeypatch, request):
    path = tmp_path / "state.db"
    if getattr(request, "param", "fresh") == "upgraded":
        # Keep historical payload columns needed by the real listing service;
        # the separate minimal-shape test below covers the three-column store.
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at REAL NOT NULL, "
                "progression_id TEXT NOT NULL, node_metadata JSON, name TEXT, user TEXT)"
            )
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_module, "settings", db_module.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    rows = [
        ("running", "running", "alpha", None),
        ("running-other", "running", "beta", None),
        ("completed", "completed", "alpha", None),
        ("legacy", None, "alpha", None),
        ("failed", "failed", "alpha", None),
        ("empty", None, "alpha", None),
        ("legacy-other", None, "beta", None),
        ("mirror", None, "alpha", {"engine_parent_run_id": "parent"}),
    ]
    async with StateDB(path) as db:
        await db.create_progression("progression")
        for updated_at, (session_id, status, project, metadata) in enumerate(rows, start=1):
            await db.create_session(
                {
                    "id": session_id,
                    "progression_id": "progression",
                    "status": status,
                    "project": project,
                    "playbook_name": "alpha",
                    "name": "50% needle",
                    "node_metadata": metadata,
                    "updated_at": float(updated_at),
                }
            )
    # A legacy empty string is not SQL NULL: preserve that distinction in the
    # filter even though the display projection falls back for both values.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET status = '' WHERE id = 'empty'")
    return path


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (
            None,
            ["legacy-other", "empty", "failed", "legacy", "completed", "running-other", "running"],
        ),
        (
            set(),
            ["legacy-other", "empty", "failed", "legacy", "completed", "running-other", "running"],
        ),
        ({"running"}, ["running-other", "running"]),
        ({"failed"}, ["failed"]),
        ({"completed"}, ["legacy-other", "legacy", "completed"]),
        ({"running", "failed"}, ["failed", "running-other", "running"]),
        (
            {"completed", "running"},
            ["legacy-other", "legacy", "completed", "running-other", "running"],
        ),
        ({"missing"}, []),
    ],
)
async def test_status_filter_rows_and_totals(status_store, statuses, expected):
    where = sessions.SessionFilter(statuses=statuses)
    rows = await sessions.list_sessions(where=where)
    assert [row["id"] for row in rows] == expected
    assert await sessions.count_sessions(where) == len(expected)
    for row in rows:
        if row["id"] in {"legacy", "legacy-other", "empty"}:
            assert row["status"] == "completed"


async def test_completed_null_arm_stays_inside_other_filters(status_store):
    async with StateDB(status_store) as db:
        for session_id, playbook, name in [
            ("wrong-playbook", "beta", "50% needle"),
            ("wrong-search", "alpha", "unrelated"),
        ]:
            await db.create_session(
                {
                    "id": session_id,
                    "progression_id": "progression",
                    "status": None,
                    "project": "alpha",
                    "playbook_name": playbook,
                    "name": name,
                }
            )
    where = sessions.SessionFilter(
        statuses={"completed", "running"},
        project="alpha",
        playbook="alpha",
        search="50% needle",
    )
    rows = await sessions.list_sessions(where=where)
    assert [row["id"] for row in rows] == ["legacy", "completed", "running"]
    assert await sessions.count_sessions(where) == 3


async def test_mixed_status_page_keeps_order_offset_and_total(status_store):
    where = sessions.SessionFilter(statuses={"completed", "running"})
    rows = await sessions.list_sessions(where=where, limit=2, offset=1)
    assert [row["id"] for row in rows] == ["legacy", "completed"]
    assert await sessions.count_sessions(where) == 5


@pytest.mark.parametrize("status_store", ["fresh", "upgraded"], indirect=True)
@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({"running"}, ["running-other", "running"]),
        ({"running", "failed"}, ["failed", "running-other", "running"]),
        ({"completed"}, ["legacy-other", "legacy", "completed"]),
        (
            {"completed", "running"},
            ["legacy-other", "legacy", "completed", "running-other", "running"],
        ),
    ],
)
async def test_sparse_status_queries_on_the_real_store(
    status_store, monkeypatch, record_property, statuses, expected
):
    # Fresh stores retain all production indexes; upgraded stores use indexes
    # installed by real StateDB migration. Neither arm forces INDEXED BY.
    with sqlite3.connect(status_store) as conn:
        assert "idx_sessions_status_updated" in {
            row[1] for row in conn.execute("PRAGMA index_list(sessions)")
        }, "StateDB must add the status index when upgrading an existing sessions table"
        conn.executemany(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, status) "
            "VALUES (?, 'progression', 0, ?, 'cancelled')",
            [(f"background-{index}", float(index + 100)) for index in range(5000)],
        )
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'"
        ).fetchall(), "exercise production's unanalysed store, not manually gathered statistics"

    statements = []
    real_open = sessions._open_db

    @asynccontextmanager
    async def capture_queries(path):
        async with real_open(path) as conn:
            execute = conn.execute

            async def capture(sql, parameters=None):
                if "FROM sessions s" in sql:
                    cursor = await execute("EXPLAIN QUERY PLAN " + sql, parameters)
                    statements.append(
                        {
                            "sql": sql,
                            "parameters": parameters,
                            "plan": [row[3] for row in await cursor.fetchall()],
                        }
                    )
                return await execute(sql, parameters)

            monkeypatch.setattr(conn, "execute", capture)
            yield conn

    monkeypatch.setattr(sessions, "_open_db", capture_queries)
    where = sessions.SessionFilter(statuses=statuses)
    rows = await sessions.list_sessions(where=where)
    total = await sessions.count_sessions(where)
    record_property(
        "status_filter_evidence",
        json.dumps(
            {
                "sqlite_version": sqlite3.sqlite_version,
                "analyzed": False,
                "statuses": sorted(statuses),
                "rows": [{"id": row["id"], "status": row["status"]} for row in rows],
                "total": total,
                "statements": statements,
            }
        ),
    )
    assert [row["id"] for row in rows] == expected
    assert total == len(expected)
    assert len(statements) == 2, "capture both production page and count queries"
    # All four filters constrain the status index without ANALYZE, including
    # both arms of completed's legacy NULL OR. An outer join's lookup by id
    # alone would not prove that selecting the page avoided scanning sessions.
    for statement in statements:
        plan = statement["plan"]
        status_seeks = [
            step for step in plan if step.startswith("SEARCH s USING ") and "(status=?" in step
        ]
        assert status_seeks, plan
        assert all("idx_sessions_status_updated" in step for step in status_seeks), plan
        assert not any(step.startswith("SCAN s") for step in plan), plan
        if "completed" in statuses:
            assert "MULTI-INDEX OR" in plan, plan
            assert len(status_seeks) == 2, plan


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_status_index_upgrade_ddl_preserves_rows_and_is_repeatable(dialect):
    # Exercise the portable status-index migration from each backend's list.
    # This checks its upgrade contract on SQLite, not live PostgreSQL behavior.
    statements = [ddl for ddl in MIGRATION_INDEXES[dialect] if "idx_sessions_status_updated" in ddl]
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE sessions (id TEXT, status TEXT, updated_at REAL)")
        conn.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?)",
            [("legacy", None, 1), ("newer", "running", 3), ("older", "running", 2)],
        )
        for _ in range(2):
            for ddl in statements:
                conn.execute(ddl)
        index_keys = [
            (row[2], row[3])
            for row in conn.execute("PRAGMA index_xinfo(idx_sessions_status_updated)")
            if row[5]
        ]
        assert index_keys == [("status", 0), ("updated_at", 1)]
        assert conn.execute("SELECT id, status FROM sessions ORDER BY updated_at").fetchall() == [
            ("legacy", None),
            ("older", "running"),
            ("newer", "running"),
        ]


async def test_minimal_old_store_gains_status_index_on_repeated_open(tmp_path):
    path = tmp_path / "minimal-old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at REAL NOT NULL, "
            "progression_id TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO sessions VALUES ('legacy', 1, 'progression')")
    for _ in range(2):
        async with StateDB(path):
            pass
        with sqlite3.connect(path) as conn:
            assert "idx_sessions_status_updated" in {
                row[1] for row in conn.execute("PRAGMA index_list(sessions)")
            }
            assert conn.execute("SELECT id, created_at FROM sessions").fetchall() == [("legacy", 1)]
            conn.execute("UPDATE sessions SET status = 'running', updated_at = 1")
            sql = "SELECT id FROM sessions WHERE status IN (?) ORDER BY updated_at DESC"
            assert conn.execute(sql, ("running",)).fetchall() == [("legacy",)]
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, ("running",))]
            assert any(
                "SEARCH sessions USING INDEX idx_sessions_status_updated (status=?)" in step
                for step in plan
            ), plan
