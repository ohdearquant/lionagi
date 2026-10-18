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
from lionagi.studio.services import sessions


@pytest_asyncio.fixture
async def status_store(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
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
    # Keep all production indexes: the optimizer must choose a seek without
    # INDEXED BY or a hand-built schema that makes the choice for it.
    with sqlite3.connect(status_store) as conn:
        conn.executemany(
            "INSERT INTO sessions (id, progression_id, created_at, updated_at, status) "
            "VALUES (?, 'progression', 0, ?, 'cancelled')",
            [(f"background-{index}", float(index + 100)) for index in range(5000)],
        )
        conn.execute("ANALYZE")

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
    # The legacy NULL OR arm may still make SQLite choose an ordered scan.
    # For sparse non-NULL filters, both queries must actually constrain the
    # status index; an unrelated seek by session id in the outer join is not
    # evidence that selecting the page avoided scanning sessions.
    if "completed" not in statuses:
        for statement in statements:
            plan = statement["plan"]
            assert any(
                step.startswith("SEARCH s USING ") and "(status=?" in step for step in plan
            ), plan
            assert not any(step.startswith("SCAN s") for step in plan), plan
