# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the schedule health verdict (healthy/failing/overdue/never-fired/disabled).

Health is derived from cadence + recorded schedule_runs rows, never from
next_fire_at -- these tests plant fixture rows for each state and assert the
verdict lands where it should, including the two shapes next_fire_at cannot
represent: a schedule that has never once executed, and one that keeps
skipping instead of running.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.state.db import StateDB  # noqa: E402
from lionagi.studio.services.schedules import (  # noqa: E402
    compute_schedule_health,
    create_schedule,
    get_schedule,
    list_schedules,
    update_schedule,
)


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _make_schedule(**overrides) -> str:
    spec = {
        "name": f"health-test-{uuid.uuid4().hex[:8]}",
        "trigger_type": "interval",
        "interval_sec": 300,
        "action_kind": "agent",
        "action_prompt": "ping",
    }
    spec.update(overrides)
    created = await create_schedule(spec)
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


async def _list_row(sid: str) -> dict:
    rows = await list_schedules()
    return next(r for r in rows if r["id"] == sid)


async def test_never_fired_enabled_schedule_with_zero_rows(temp_db_path):
    sid = await _make_schedule()
    row = await _list_row(sid)
    assert row["health_state"] == "never-fired"
    assert row["health_last_outcome"] is None
    assert row["health_last_outcome_at"] is None
    assert row["health_since"] is not None

    detail = await get_schedule(sid)
    assert detail["health_state"] == "never-fired"


async def test_never_fired_when_only_skipped_rows_recorded(temp_db_path):
    """Skipped rows are recorded evidence but not executed evidence."""
    sid = await _make_schedule()
    now = time.time()
    await _seed_run(sid, status="skipped", fired_at=now - 20)
    await _seed_run(sid, status="skipped", fired_at=now - 10)

    row = await _list_row(sid)
    assert row["health_state"] == "never-fired"
    assert row["health_last_outcome"] is None


async def test_healthy_when_recent_execution_completed(temp_db_path):
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 30)

    row = await _list_row(sid)
    assert row["health_state"] == "healthy"
    assert row["health_last_outcome"] == "completed"
    assert row["health_last_outcome_at"] == pytest.approx(now - 30, abs=2)


async def test_failing_when_last_executed_outcome_failed(temp_db_path):
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 600)
    await _seed_run(sid, status="failed", fired_at=now - 30)

    row = await _list_row(sid)
    assert row["health_state"] == "failing"
    assert row["health_last_outcome"] == "failed"


async def test_failing_when_last_executed_outcome_timed_out(temp_db_path):
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="timed_out", fired_at=now - 30)

    row = await _list_row(sid)
    assert row["health_state"] == "failing"


async def test_overdue_when_no_execution_within_expected_cadence(temp_db_path):
    """The #2845 shape: enabled interval schedule, cadence known, but no
    execution evidence in a long time -- overdue regardless of a rosy
    next_fire_at."""
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 5000)

    row = await _list_row(sid)
    assert row["health_state"] == "overdue"


async def test_overdue_outranks_a_pile_of_recent_skips(temp_db_path):
    """A schedule skipping every occurrence must not read as fresh just
    because rows keep being recorded -- only executed rows count as evidence."""
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 5000)
    for i in range(5):
        await _seed_run(sid, status="skipped", fired_at=now - (i * 60))

    row = await _list_row(sid)
    assert row["health_state"] == "overdue"
    assert row["health_last_outcome"] == "completed"


async def test_disabled_takes_precedence_over_any_run_history(temp_db_path):
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="failed", fired_at=now - 5000)
    ok = await update_schedule(sid, {"enabled": 0})
    assert ok

    row = await _list_row(sid)
    assert row["health_state"] == "disabled"

    detail = await get_schedule(sid)
    assert detail["health_state"] == "disabled"


async def test_cron_trigger_has_no_cadence_so_never_reports_overdue(temp_db_path):
    sid = await _make_schedule(trigger_type="cron", cron_expr="0 18 * * *", interval_sec=None)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 500_000)

    row = await _list_row(sid)
    assert row["health_state"] == "healthy"


async def test_chain_children_are_not_evidence(temp_db_path):
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 5000)
    await _seed_run(sid, status="completed", fired_at=now - 10, chain_depth=1)

    row = await _list_row(sid)
    assert row["health_state"] == "overdue"


def test_compute_schedule_health_is_a_pure_function_of_row_and_evidence():
    now = time.time()
    row = {"enabled": 1, "trigger_type": "interval", "interval_sec": 300, "created_at": now - 1000}
    healthy = compute_schedule_health(
        row, {"last_executed_run_at": now - 30, "last_executed_status": "completed"}, now=now
    )
    assert healthy["health_state"] == "healthy"

    overdue = compute_schedule_health(
        row, {"last_executed_run_at": now - 5000, "last_executed_status": "completed"}, now=now
    )
    assert overdue["health_state"] == "overdue"
