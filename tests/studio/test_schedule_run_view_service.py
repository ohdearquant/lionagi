# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""DB-backed integration tests for the RunView service surface: the
schedule_runs/invocations/sessions join actually resolves against a real
StateDB, and repeatable ``status`` filtering works end to end."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import StateDB
from lionagi.studio.services.schedules import (
    get_schedule_run,
    get_schedule_status,
    list_schedule_run_views,
)


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr("lionagi.studio.services.schedules.DEFAULT_DB_PATH", db_path)
    return db_path


async def _seed(db: StateDB) -> tuple[str, str, str, str]:
    """One schedule with a terminal run wired to an invocation + session."""
    sched_id = uuid.uuid4().hex[:12]
    await db.create_schedule(
        {
            "id": sched_id,
            "name": "nightly",
            "trigger_type": "cron",
            "cron_expr": "0 2 * * *",
            "action_kind": "agent",
            "next_fire_at": time.time() + 3600,
        }
    )
    inv_id = uuid.uuid4().hex[:12]
    await db.create_invocation(
        {"id": inv_id, "skill": "agent", "started_at": time.time(), "status": "completed"}
    )
    sess_id = str(uuid.uuid4())
    prog_id = f"{sess_id}-prog"
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": sess_id,
            "progression_id": prog_id,
            "invocation_id": inv_id,
            "status": "completed",
            "artifacts_path": f"/runs/{inv_id}/artifacts",
        }
    )
    await db.update_status(
        "session",
        sess_id,
        new_status="completed",
        reason_code="run.completed.ok",
        reason_summary="3 commits landed",
        expected_statuses={"completed"},
    )
    run_id = uuid.uuid4().hex[:12]
    fired_at = time.time()
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": sched_id,
            "invocation_id": inv_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": "completed",
            "exit_code": 0,
            "fired_at": fired_at,
            "ended_at": fired_at + 5,
        }
    )
    return sched_id, run_id, inv_id, sess_id


@pytest.mark.asyncio
async def test_get_schedule_run_carries_reconciled_outcome_and_chain_children(
    temp_db_path: Path,
) -> None:
    async with StateDB() as db:
        _sched_id, run_id, inv_id, sess_id = await _seed(db)

    view = await get_schedule_run(run_id)

    assert view is not None
    assert view["chain_children"] == []  # legacy field preserved (additive layering)
    assert view["outcome"]["source"] == "session"
    assert view["outcome"]["summary"] == "3 commits landed"
    assert view["session_ids"] == [sess_id]
    assert view["artifacts"] == [f"/runs/{inv_id}/artifacts"]
    assert view["duration_ms"] == 5000


@pytest.mark.asyncio
async def test_get_schedule_status_reports_latest_run_and_exit_code(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sched_id, run_id, _inv_id, _sess_id = await _seed(db)

    status = await get_schedule_status(sched_id)

    assert status is not None
    assert status["schedule"]["id"] == sched_id
    assert status["schedule"]["cron_expr"] == "0 2 * * *"
    assert status["latest_run"]["id"] == run_id
    assert status["exit_code"] == 0  # trusted completion


@pytest.mark.asyncio
async def test_get_schedule_status_unknown_schedule_returns_none(temp_db_path: Path) -> None:
    async with StateDB():
        pass
    assert await get_schedule_status("no-such-schedule") is None


@pytest.mark.asyncio
async def test_list_schedule_run_views_repeatable_status_filter(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sched_id, completed_run_id, _inv_id, _sess_id = await _seed(db)
        failed_run_id = uuid.uuid4().hex[:12]
        await db.create_schedule_run(
            {
                "id": failed_run_id,
                "schedule_id": sched_id,
                "invocation_id": None,
                "trigger_context": {},
                "action_kind": "agent",
                "action_args": [],
                "status": "failed",
                "exit_code": None,
                "error_detail": "dispatch failed: missing cwd",
                "fired_at": time.time(),
            }
        )
        skipped_run_id = uuid.uuid4().hex[:12]
        await db.create_schedule_run(
            {
                "id": skipped_run_id,
                "schedule_id": sched_id,
                "invocation_id": None,
                "trigger_context": {},
                "action_kind": "agent",
                "action_args": [],
                "status": "skipped",
                "exit_code": None,
                "error_detail": "overlap policy: prior running",
                "fired_at": time.time(),
            }
        )

    views = await list_schedule_run_views(sched_id, status=["failed", "skipped"], limit=20)

    assert {v["id"] for v in views} == {failed_run_id, skipped_run_id}
    assert {v["outcome"]["source"] for v in views} == {"occurrence"}
    assert completed_run_id not in {v["id"] for v in views}


@pytest.mark.asyncio
async def test_list_schedule_run_views_keeps_full_row_fields(temp_db_path: Path) -> None:
    """RunView fields layer on top additively — pre-existing schedule_runs
    columns (error_detail, trigger_context, action_args, ...) must survive."""
    async with StateDB() as db:
        sched_id, _completed_run_id, _inv_id, _sess_id = await _seed(db)
        failed_run_id = uuid.uuid4().hex[:12]
        await db.create_schedule_run(
            {
                "id": failed_run_id,
                "schedule_id": sched_id,
                "invocation_id": None,
                "trigger_context": {"source": "manual"},
                "action_kind": "agent",
                "action_args": {"prompt": "ping"},
                "status": "failed",
                "exit_code": None,
                "error_detail": "dispatch failed: missing cwd",
                "fired_at": time.time(),
            }
        )

    views = await list_schedule_run_views(sched_id, status=["failed"], limit=20)

    view = next(v for v in views if v["id"] == failed_run_id)
    assert view["error_detail"] == "dispatch failed: missing cwd"
    assert view["trigger_context"] == {"source": "manual"}
    assert view["action_args"] == {"prompt": "ping"}
    # RunView-additive fields are still present alongside the raw row.
    assert view["outcome"]["source"] == "occurrence"
