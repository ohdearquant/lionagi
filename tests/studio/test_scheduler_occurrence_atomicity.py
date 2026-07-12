# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Crash-interleaving regression tests for the scheduler's occurrence-insert
+ cursor-advance atomicity.

These exercise a real temp-dir sqlite ``StateDB`` (never the process-wide
``~/.lionagi/state.db``) rather than mocks, because the property under test
is a genuine transaction boundary: a simulated mid-write crash must leave
*zero* durable trace, and only a real database rollback proves that.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from lionagi.state.db import StateDB
from lionagi.studio.scheduler.engine import SchedulerEngine
from lionagi.studio.services.scheduler_state import _DBSchedulerStateService


def _schedule_row(schedule_id: str, **overrides) -> dict:
    base = {
        "id": schedule_id,
        "name": f"sched-{schedule_id}",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_model": "gpt-4.1-mini",
        "action_prompt": "ping",
        "enabled": 1,
        "next_fire_at": 1_000.0,
        "missed_fire_policy": "skip",
    }
    base.update(overrides)
    return base


def _run_row(run_id: str, schedule_id: str, *, fired_at: float, **overrides) -> dict:
    base = {
        "id": run_id,
        "schedule_id": schedule_id,
        "trigger_context": {"source": "cron"},
        "action_kind": "agent",
        "action_args": {"prompt": "ping"},
        "status": "running",
        "fired_at": fired_at,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_crash_between_insert_and_advance_does_not_double_fire(tmp_path):
    """(a) A process death between the occurrence insert and the cursor
    advance must roll back BOTH halves -- not just leave the schedule's
    cursor behind while the occurrence row survives. Otherwise a restart
    that recomputes "still due" from the stale cursor would fire again for
    an occurrence that the crashed attempt already (partially) recorded.
    """
    db_path = tmp_path / "state.db"
    sid = "sched-a"

    async with StateDB(db_path) as db:
        await db.create_schedule(_schedule_row(sid, next_fire_at=1000.0))

    original_execute = AsyncConnection.execute

    async def _crash(self, statement, *args, **kwargs):
        if "UPDATE schedules" in str(statement):
            raise RuntimeError("simulated crash before cursor advance")
        return await original_execute(self, statement, *args, **kwargs)

    async with StateDB(db_path) as db:
        with patch.object(AsyncConnection, "execute", _crash):
            with pytest.raises(RuntimeError, match="simulated crash"):
                await db.create_schedule_run_and_advance(
                    _run_row("run-1", sid, fired_at=1000.0),
                    schedule_id=sid,
                    schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
                )

    # Post-crash: neither half landed -- the occurrence row is absent and
    # the schedule's cursor is untouched.
    async with StateDB(db_path) as db:
        runs = await db.list_schedule_runs(sid)
        schedule = await db.get_schedule(sid)
    assert runs == []
    assert schedule["next_fire_at"] == 1000.0

    # "Restart": the same occurrence is retried for real (no crash this
    # time) and must produce exactly one durable row -- proving the
    # crashed attempt left nothing behind to double up against.
    async with StateDB(db_path) as db:
        await db.create_schedule_run_and_advance(
            _run_row("run-1", sid, fired_at=1000.0),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
        )
        runs = await db.list_schedule_runs(sid)
        schedule = await db.get_schedule(sid)
    assert len(runs) == 1
    assert schedule["next_fire_at"] == 2000.0


@pytest.mark.asyncio
async def test_github_path_crash_before_second_event_refires_only_second(tmp_path):
    """(b) Two github_poll events are dispatched in the same poll batch.
    The first event's occurrence-insert + github_cursor advance commits
    cleanly; the process then dies mid-transaction for the second event.
    A restart must see the first event as already recorded (so it is never
    re-fired) while the second is still open (schedule_run_exists_since is
    False for it, and the cursor has not moved past it) -- so the next poll
    re-fires ONLY the second event, never both.
    """
    db_path = tmp_path / "state.db"
    sid = "sched-b"
    event1_time = "2026-07-07T10:00:00Z"
    event2_time = "2026-07-07T11:00:00Z"

    async with StateDB(db_path) as db:
        await db.create_schedule(
            _schedule_row(
                sid,
                trigger_type="github_poll",
                github_repo="acme/widgets",
                github_cursor=None,
            )
        )

        # Event 1: normal atomic commit.
        await db.create_schedule_run_and_advance(
            _run_row("run-event1", sid, fired_at=1000.0),
            schedule_id=sid,
            schedule_fields={"github_cursor": event1_time, "last_fired_at": 1000.0},
        )

    original_execute = AsyncConnection.execute

    async def _crash(self, statement, *args, **kwargs):
        if "UPDATE schedules" in str(statement):
            raise RuntimeError("simulated crash before cursor advance")
        return await original_execute(self, statement, *args, **kwargs)

    async with StateDB(db_path) as db:
        with patch.object(AsyncConnection, "execute", _crash):
            with pytest.raises(RuntimeError, match="simulated crash"):
                await db.create_schedule_run_and_advance(
                    _run_row("run-event2", sid, fired_at=1100.0),
                    schedule_id=sid,
                    schedule_fields={"github_cursor": event2_time, "last_fired_at": 1100.0},
                )

    async with StateDB(db_path) as db:
        runs = await db.list_schedule_runs(sid)
        schedule = await db.get_schedule(sid)
        exists_since_event2 = await db.schedule_run_exists_since(sid, since=1100.0)

    # Only event 1 is durable; the cursor sits at event 1, never advanced
    # to (or past) event 2.
    assert len(runs) == 1
    assert runs[0]["id"] == "run-event1"
    assert schedule["github_cursor"] == event1_time
    assert exists_since_event2 is False

    # "Restart": re-fire for event 2 only (event 1 is never re-dispatched
    # because the poll's own cursor filter -- github_cursor -- excludes it;
    # this call models the recorded outcome of that re-poll).
    async with StateDB(db_path) as db:
        await db.create_schedule_run_and_advance(
            _run_row("run-event2", sid, fired_at=1100.0),
            schedule_id=sid,
            schedule_fields={"github_cursor": event2_time, "last_fired_at": 1100.0},
        )
        runs = await db.list_schedule_runs(sid)
        schedule = await db.get_schedule(sid)

    assert len(runs) == 2
    assert {r["id"] for r in runs} == {"run-event1", "run-event2"}
    assert schedule["github_cursor"] == event2_time


@pytest.mark.asyncio
async def test_missed_fire_recovery_skips_occurrence_already_in_schedule_runs(
    tmp_path, monkeypatch
):
    """(c) Startup/missed-fire recovery must consult schedule_runs before
    queuing a recovery fire. A schedule whose next_fire_at is past-due but
    which already has a schedule_run row recorded at-or-after that time
    (the atomic transaction committed, then the process died before the
    run's terminal write) must NOT be re-fired -- only have its cursor
    advanced past the already-handled occurrence.
    """
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    sid = "sched-c"
    due_at = 1000.0

    async with StateDB(db_path) as db:
        await db.create_schedule(
            _schedule_row(sid, next_fire_at=due_at, missed_fire_policy="run_once")
        )
        # Simulate: the atomic transaction already committed this
        # occurrence + advanced the cursor once (to a value that is itself
        # still in the past relative to "now" in this test, so the
        # schedule is still seen as due -- exercising the recovery path
        # rather than the ordinary tick).
        await db.create_schedule_run_and_advance(
            _run_row("run-crashed", sid, fired_at=due_at),
            schedule_id=sid,
            schedule_fields={"next_fire_at": due_at, "last_fired_at": due_at},
        )

    svc = _DBSchedulerStateService()
    engine = SchedulerEngine(svc=svc)

    with (
        patch.object(engine, "_tracked_fire") as mock_tracked_fire,
        patch.object(engine, "_recover_missed_fire_run_once") as mock_recover,
        patch.object(engine, "_record_missed_fire_skip") as mock_skip,
    ):
        await engine._check_missed_fires()

    mock_tracked_fire.assert_not_called()
    mock_recover.assert_not_called()
    mock_skip.assert_not_called()

    async with StateDB(db_path) as db:
        schedule = await db.get_schedule(sid)
        runs = await db.list_schedule_runs(sid)

    # No second occurrence was recorded, and the cursor moved past the
    # already-handled fire time instead of staying stuck due-in-the-past.
    assert len(runs) == 1
    assert schedule["next_fire_at"] is not None
    assert schedule["next_fire_at"] > due_at
