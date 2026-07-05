# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for consecutive_failures / last_status on schedule list + detail rows."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.state.db import StateDB  # noqa: E402
from lionagi.studio.services.schedules import (  # noqa: E402
    create_schedule,
    get_schedule,
    list_schedules,
)


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr("lionagi.studio.services.schedules.DEFAULT_DB_PATH", db_path)
    return db_path


async def _make_schedule() -> str:
    created = await create_schedule(
        {
            "name": f"streak-test-{uuid.uuid4().hex[:8]}",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    return created["id"]


async def _seed_run(
    schedule_id: str,
    *,
    status: str,
    fired_at: float,
    chain_depth: int = 0,
) -> None:
    async with StateDB() as db:
        await db.create_schedule_run(
            {
                "id": str(uuid.uuid4()),
                "schedule_id": schedule_id,
                "trigger_context": {},
                "action_kind": "agent",
                "action_args": {},
                "status": status,
                "chain_depth": chain_depth,
                "fired_at": fired_at,
            }
        )


async def test_never_fired_schedule_has_zero_streak_and_no_status(temp_db_path):
    sid = await _make_schedule()
    rows = await list_schedules()
    row = next(r for r in rows if r["id"] == sid)
    assert row["consecutive_failures"] == 0
    assert row["last_status"] is None

    detail = await get_schedule(sid)
    assert detail["consecutive_failures"] == 0
    assert detail["last_status"] is None


async def test_failed_failed_completed_newest_first_gives_streak_two(temp_db_path):
    sid = await _make_schedule()
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 30)
    await _seed_run(sid, status="failed", fired_at=now - 20)
    await _seed_run(sid, status="failed", fired_at=now - 10)

    rows = await list_schedules()
    row = next(r for r in rows if r["id"] == sid)
    assert row["consecutive_failures"] == 2
    assert row["last_status"] == "failed"


async def test_running_newest_reports_last_status_but_not_counted_in_streak(temp_db_path):
    sid = await _make_schedule()
    now = time.time()
    await _seed_run(sid, status="failed", fired_at=now - 20)
    await _seed_run(sid, status="failed", fired_at=now - 10)
    await _seed_run(sid, status="running", fired_at=now)

    rows = await list_schedules()
    row = next(r for r in rows if r["id"] == sid)
    assert row["consecutive_failures"] == 2
    assert row["last_status"] == "running"


async def test_skipped_rows_are_ignored_by_streak_but_reported_as_last_status(temp_db_path):
    sid = await _make_schedule()
    now = time.time()
    await _seed_run(sid, status="failed", fired_at=now - 40)
    await _seed_run(sid, status="skipped", fired_at=now - 30)
    await _seed_run(sid, status="failed", fired_at=now - 20)
    await _seed_run(sid, status="skipped", fired_at=now - 10)

    rows = await list_schedules()
    row = next(r for r in rows if r["id"] == sid)
    assert row["consecutive_failures"] == 2
    assert row["last_status"] == "skipped"


async def test_chain_children_do_not_count_toward_streak(temp_db_path):
    sid = await _make_schedule()
    now = time.time()
    await _seed_run(sid, status="failed", fired_at=now - 10)
    await _seed_run(sid, status="failed", fired_at=now - 5, chain_depth=1)

    rows = await list_schedules()
    row = next(r for r in rows if r["id"] == sid)
    assert row["consecutive_failures"] == 1
    assert row["last_status"] == "failed"


async def test_newest_completed_run_resets_streak_to_zero(temp_db_path):
    sid = await _make_schedule()
    now = time.time()
    await _seed_run(sid, status="failed", fired_at=now - 20)
    await _seed_run(sid, status="completed", fired_at=now - 10)

    rows = await list_schedules()
    row = next(r for r in rows if r["id"] == sid)
    assert row["consecutive_failures"] == 0
    assert row["last_status"] == "completed"


async def test_get_schedule_detail_carries_same_fields(temp_db_path):
    sid = await _make_schedule()
    now = time.time()
    await _seed_run(sid, status="failed", fired_at=now - 20)
    await _seed_run(sid, status="failed", fired_at=now - 10)

    detail = await get_schedule(sid)
    assert detail["consecutive_failures"] == 2
    assert detail["last_status"] == "failed"
