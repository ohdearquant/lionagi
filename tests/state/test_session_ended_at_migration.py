# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Historical session end-time migration and insertion guarantees."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB

pytestmark = pytest.mark.asyncio

_BACKFILL_KEY = "migration.session_ended_at_backfill"


def _seed_legacy_terminal_rows(db_path: Path) -> None:
    rows = [
        # id, status, created_at, updated_at, started_at, last_message_at, ended_at
        ("01-completed", "completed", 1.0, 50.0, 10.0, 60.0, None),
        ("02-failed", "failed", 2.0, 80.0, 20.0, 70.0, None),
        ("03-cancelled", "cancelled", 3.0, 20.0, 15.0, 30.0, None),
        ("04-empty", "completed_empty", 4.0, 40.0, 25.0, None, None),
        # NULL means an interrupted pre-terminal write and is owned by the reaper.
        ("05-null-status", None, 5.0, 45.0, 35.0, None, None),
        ("06-running", "running", 6.0, 90.0, 50.0, 95.0, None),
        ("07-measured", "completed", 7.0, 100.0, 75.0, 99.0, 110.0),
    ]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM schema_meta WHERE key = ?", (_BACKFILL_KEY,))
        for row in rows:
            progression_id = f"{row[0]}-progression"
            conn.execute(
                "INSERT INTO progressions (id, created_at, collection) VALUES (?, ?, '[]')",
                (progression_id, row[2]),
            )
            conn.execute(
                "INSERT INTO sessions "
                "(id, created_at, progression_id, updated_at, status, started_at, "
                "last_message_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row[0], row[2], progression_id, row[3], row[1], row[4], row[5], row[6]),
            )
        conn.commit()
    finally:
        conn.close()


async def test_historical_terminal_end_backfill_is_bounded_and_idempotent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path):
        pass
    _seed_legacy_terminal_rows(db_path)

    # A tiny batch makes transaction boundaries observable without a large fixture.
    monkeypatch.setattr(state_db_mod, "_SESSION_ENDED_AT_BACKFILL_BATCH_SIZE", 2)
    original = StateDB._backfill_session_ended_at_batch
    batch_sizes: list[int] = []

    async def observed_batch(self: StateDB, conn: Any) -> int:
        count = await original(self, conn)
        batch_sizes.append(count)
        return count

    monkeypatch.setattr(StateDB, "_backfill_session_ended_at_batch", observed_batch)

    async with StateDB(db_path) as db:
        rows = {
            row["id"]: row
            for row in await db.fetch_all(
                "SELECT id, ended_at, ended_at_is_approximate, duration_ms "
                "FROM sessions ORDER BY id"
            )
        }

    # Four eligible rows require two bounded writes, followed by one empty probe.
    assert batch_sizes == [2, 2, 0]
    assert rows["01-completed"]["ended_at"] == 60.0
    assert rows["02-failed"]["ended_at"] == 80.0
    assert rows["03-cancelled"]["ended_at"] == 30.0
    assert rows["04-empty"]["ended_at"] == 40.0
    for row_id in (
        "01-completed",
        "02-failed",
        "03-cancelled",
        "04-empty",
    ):
        assert rows[row_id]["ended_at_is_approximate"] == 1
        assert rows[row_id]["duration_ms"] is None

    assert rows["06-running"]["ended_at"] is None
    assert rows["06-running"]["ended_at_is_approximate"] == 0
    assert rows["07-measured"]["ended_at"] == 110.0
    assert rows["07-measured"]["ended_at_is_approximate"] == 0
    assert rows["05-null-status"]["ended_at"] is None
    assert rows["05-null-status"]["ended_at_is_approximate"] == 0

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (_BACKFILL_KEY,)
        ).fetchone() == ("1",)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT id FROM sessions INDEXED BY idx_sessions_terminal_missing_end "
            "WHERE ended_at IS NULL AND status IN "
            "('completed','completed_empty','failed','timed_out','aborted','cancelled') "
            "ORDER BY id LIMIT 500"
        ).fetchall()
        assert any("idx_sessions_terminal_missing_end" in str(step) for step in plan)
    finally:
        conn.close()

    # The completion marker prevents both rescans and rewrites on later opens.
    batch_sizes.clear()
    async with StateDB(db_path):
        pass
    assert batch_sizes == []


async def test_new_terminal_insert_without_measured_end_is_marked_approximate(
    tmp_path: Path,
) -> None:
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression("terminal-progression")
        await db.create_session(
            {
                "id": "terminal-import",
                "progression_id": "terminal-progression",
                "status": "completed",
                "created_at": 10.0,
                "started_at": 20.0,
                "updated_at": 30.0,
                "last_message_at": 40.0,
            }
        )
        row = await db.get_session("terminal-import")

    assert row is not None
    assert row["ended_at"] == 40.0
    assert row["ended_at_is_approximate"] is True
    # Approximate evidence must never be promoted to a measured duration.
    assert row["duration_ms"] is None
