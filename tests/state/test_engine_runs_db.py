# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for StateDB engine_runs CRUD: insert/get/update/list and concurrent write-lock discipline."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.state.db import StateDB  # noqa: E402


def _run_id() -> str:
    return uuid.uuid4().hex


async def test_insert_and_get_engine_run(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    rid = _run_id()

    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id=rid,
            kind="research",
            spec_json={"topic": "GQA attention"},
            started_at=1000.0,
        )
        row = await db.get_engine_run(rid)

    assert row is not None
    assert row["id"] == rid
    assert row["kind"] == "research"
    assert row["spec_json"] == {"topic": "GQA attention"}
    assert row["status"] == "running"
    assert row["started_at"] == 1000.0
    assert row["ended_at"] is None
    assert row["session_id"] is None
    assert row["export_dir"] is None
    assert row["error"] is None


async def test_get_engine_run_returns_none_for_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        result = await db.get_engine_run("nonexistent-run-id")
    assert result is None


async def test_insert_engine_run_session_id_column_present(tmp_path: Path) -> None:
    """session_id column exists and is stored/retrieved; no FK required here."""
    import aiosqlite

    db_path = tmp_path / "state.db"
    rid = _run_id()

    # Seed the schema by opening StateDB once (creates all tables).
    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id=rid,
            kind="planning",
            spec_json={"prompt": "Build a REST API"},
            started_at=2000.0,
            session_id=None,  # NULL is always valid (no FK violation)
        )
        row = await db.get_engine_run(rid)

    assert row is not None
    assert row["session_id"] is None

    # Confirm the column itself is present in the schema.
    async with aiosqlite.connect(str(db_path)) as conn:
        async with conn.execute("PRAGMA table_info(engine_runs)") as cur:
            cols = {r[1] async for r in cur}
    assert "session_id" in cols

    # Verify non-null session_id can be stored by bypassing the FK
    # (the FK is valid in production — sessions exist before engine runs
    # reference them; this test just confirms the column stores the value).
    rid2 = _run_id()
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys = OFF")
        await conn.execute(
            "INSERT INTO engine_runs (id, kind, spec_json, status, started_at, session_id)"
            " VALUES (?, 'research', '{}', 'running', 1000.0, 'test-sess-xyz')",
            (rid2,),
        )
        await conn.commit()

    async with StateDB(db_path) as db:
        row2 = await db.get_engine_run(rid2)
    assert row2 is not None
    assert row2["session_id"] == "test-sess-xyz"


async def test_update_engine_run_completed(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    rid = _run_id()

    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id=rid,
            kind="review",
            spec_json={"artifact": "some code"},
            started_at=500.0,
        )
        await db.update_engine_run(
            rid,
            status="completed",
            ended_at=600.0,
        )
        row = await db.get_engine_run(rid)

    assert row is not None
    assert row["status"] == "completed"
    assert row["ended_at"] == 600.0
    assert row["error"] is None
    assert row["export_dir"] is None


async def test_update_engine_run_failed(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    rid = _run_id()

    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id=rid,
            kind="hypothesis",
            spec_json={"findings": "X causes Y"},
            started_at=100.0,
        )
        await db.update_engine_run(
            rid,
            status="failed",
            ended_at=150.0,
            error="LLM call timed out",
        )
        row = await db.get_engine_run(rid)

    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] == "LLM call timed out"


async def test_update_engine_run_with_export_dir(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    rid = _run_id()

    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id=rid,
            kind="coding",
            spec_json={"spec": "Implement BFS", "test_cmd": "pytest"},
            started_at=300.0,
        )
        await db.update_engine_run(
            rid,
            status="completed",
            ended_at=400.0,
            export_dir="/tmp/coding-output",
        )
        row = await db.get_engine_run(rid)

    assert row is not None
    assert row["export_dir"] == "/tmp/coding-output"
    assert row["status"] == "completed"


async def test_list_engine_runs_ordering_newest_first(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    ids = [_run_id() for _ in range(3)]

    async with StateDB(db_path) as db:
        for i, rid in enumerate(ids):
            await db.insert_engine_run(
                run_id=rid,
                kind="research",
                spec_json={"topic": f"topic-{i}"},
                started_at=float(1000 + i),
            )
        rows = await db.list_engine_runs()

    assert len(rows) >= 3
    returned_ids = [r["id"] for r in rows]
    assert returned_ids.index(ids[2]) < returned_ids.index(ids[0])


async def test_list_engine_runs_filter_by_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    research_id = _run_id()
    review_id = _run_id()

    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id=research_id,
            kind="research",
            spec_json={"topic": "test"},
            started_at=1000.0,
        )
        await db.insert_engine_run(
            run_id=review_id,
            kind="review",
            spec_json={"artifact": "code"},
            started_at=1001.0,
        )
        rows = await db.list_engine_runs(kind="research")

    assert all(r["kind"] == "research" for r in rows)
    ids_in = [r["id"] for r in rows]
    assert research_id in ids_in
    assert review_id not in ids_in


async def test_list_engine_runs_filter_by_status(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    running_id = _run_id()
    done_id = _run_id()

    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id=running_id,
            kind="planning",
            spec_json={"prompt": "plan"},
            started_at=1000.0,
        )
        await db.insert_engine_run(
            run_id=done_id,
            kind="planning",
            spec_json={"prompt": "plan2"},
            started_at=1001.0,
        )
        await db.update_engine_run(done_id, status="completed", ended_at=1100.0)
        rows = await db.list_engine_runs(status="running")

    ids_in = [r["id"] for r in rows]
    assert running_id in ids_in
    assert done_id not in ids_in


async def test_list_engine_runs_limit_and_offset(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    ids = [_run_id() for _ in range(5)]

    async with StateDB(db_path) as db:
        for i, rid in enumerate(ids):
            await db.insert_engine_run(
                run_id=rid,
                kind="research",
                spec_json={"topic": f"t-{i}"},
                started_at=float(1000 + i),
            )
        page1 = await db.list_engine_runs(limit=3, offset=0)
        page2 = await db.list_engine_runs(limit=3, offset=3)

    assert len(page1) == 3
    assert len(page2) == 2
    p1_ids = {r["id"] for r in page1}
    p2_ids = {r["id"] for r in page2}
    assert p1_ids.isdisjoint(p2_ids)


async def test_list_engine_runs_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        rows = await db.list_engine_runs()
    assert rows == []


async def test_spec_json_round_trips_complex(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    rid = _run_id()
    spec = {
        "topic": "attention mechanisms",
        "max_depth": 5,
        "tags": ["GQA", "MHA"],
        "nested": {"key": "val"},
    }

    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id=rid,
            kind="research",
            spec_json=spec,
            started_at=time.time(),
        )
        row = await db.get_engine_run(rid)

    assert row is not None
    assert row["spec_json"] == spec


async def test_concurrent_inserts_no_rows_dropped(tmp_path: Path) -> None:
    """50 concurrent insert_engine_run calls must all succeed; without _write_lock they would race on BEGIN IMMEDIATE and silently drop writes."""
    db_path = tmp_path / "state.db"
    n = 50
    ids = [_run_id() for _ in range(n)]

    async with StateDB(db_path) as db:
        await asyncio.gather(
            *[
                db.insert_engine_run(
                    run_id=ids[i],
                    kind="research",
                    spec_json={"topic": f"topic-{i}"},
                    started_at=float(1000 + i),
                )
                for i in range(n)
            ]
        )
        rows = await db.list_engine_runs(limit=n + 10)

    assert len(rows) == n, (
        f"Expected {n} rows after concurrent inserts, got {len(rows)} — lock discipline broken"
    )


async def test_concurrent_insert_and_update_no_errors(tmp_path: Path) -> None:
    """25 concurrent inserts + 25 concurrent updates on same DB → zero errors."""
    db_path = tmp_path / "state.db"
    n = 25
    ids = [_run_id() for _ in range(n)]

    errors: list[Exception] = []

    async with StateDB(db_path) as db:
        for i in range(n):
            await db.insert_engine_run(
                run_id=ids[i],
                kind="planning",
                spec_json={"prompt": f"plan-{i}"},
                started_at=float(1000 + i),
            )

        async def _update(i: int) -> None:
            try:
                await db.update_engine_run(
                    ids[i],
                    status="completed",
                    ended_at=float(2000 + i),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        new_ids = [_run_id() for _ in range(n)]

        async def _insert(i: int) -> None:
            try:
                await db.insert_engine_run(
                    run_id=new_ids[i],
                    kind="review",
                    spec_json={"artifact": f"code-{i}"},
                    started_at=float(3000 + i),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        await asyncio.gather(*[_insert(i) for i in range(n)], *[_update(i) for i in range(n)])

    assert not errors, f"{len(errors)} write errors on concurrent insert+update: {errors[0]}"
