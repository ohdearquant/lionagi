# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A github_poll schedule's poll-and-fire pass runs as a tracked task, not inside the tick.

The pass awaits each fire it dispatches (the cursor advances per committed event), so it lasts as
long as the slowest action. Awaited inside the tick, one long action made every other schedule
wait for it. The tick now hands the pass off and moves on; a pass still running keeps its
schedule's turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.studio.scheduler.engine import SchedulerEngine


def _github_schedule(sid: str) -> dict:
    return {
        "id": sid,
        "name": f"poll-{sid}",
        "trigger_type": "github_poll",
        "github_repo": "acme/widgets",
        "action_kind": "command",
        "overlap_policy": "skip",
        "missed_fire_policy": "skip",
        "last_fired_at": 0,
    }


def _engine(schedules: list[dict]) -> SchedulerEngine:
    svc = AsyncMock()
    svc.list_schedules = AsyncMock(return_value=schedules)
    svc.update_schedule = AsyncMock()
    engine = SchedulerEngine(svc=svc)
    # The other tick phases open the state database; this test is about the schedule phase only.
    now = time.time()
    engine._last_reaper_run = now
    engine._last_checkpoint_run = now
    engine._maybe_start_prune = lambda now: None  # type: ignore[method-assign]
    engine._maybe_start_worker_pass = lambda now: None  # type: ignore[method-assign]
    engine._deliver_due_dispatches = AsyncMock()  # type: ignore[method-assign]
    return engine


@pytest.mark.asyncio
async def test_tick_returns_while_a_github_poll_pass_is_still_running():
    engine = _engine([_github_schedule("gh-1")])
    release = asyncio.Event()
    passes: list[str] = []

    async def slow_pass(schedule, now):
        passes.append(schedule["id"])
        await release.wait()

    with patch.object(engine, "_tick_github", new=slow_pass):
        started = time.monotonic()
        await asyncio.wait_for(engine._tick(), timeout=2.0)
        assert time.monotonic() - started < 1.0
        await asyncio.sleep(0)  # the handed-off pass gets its first turn
        assert passes == ["gh-1"]
        task = engine._github_tick_tasks["gh-1"]
        assert not task.done()

        # A second tick while the pass runs does not start another pass for that schedule.
        await asyncio.wait_for(engine._tick(), timeout=2.0)
        assert passes == ["gh-1"]
        assert engine._github_tick_tasks["gh-1"] is task

        release.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert "gh-1" not in engine._github_tick_tasks

        # Once the pass has finished, the next tick starts a fresh one.
        release.clear()
        await asyncio.wait_for(engine._tick(), timeout=2.0)
        await asyncio.sleep(0)
        assert passes == ["gh-1", "gh-1"]
        release.set()
        await asyncio.wait_for(engine._github_tick_tasks["gh-1"], timeout=2.0)


@pytest.mark.asyncio
async def test_one_schedules_running_pass_does_not_hold_up_another_schedule():
    engine = _engine([_github_schedule("gh-1"), _github_schedule("gh-2")])
    release = asyncio.Event()
    passes: list[str] = []

    async def pass_(schedule, now):
        passes.append(schedule["id"])
        if schedule["id"] == "gh-1":
            await release.wait()

    with patch.object(engine, "_tick_github", new=pass_):
        await asyncio.wait_for(engine._tick(), timeout=2.0)
        await asyncio.wait_for(engine._github_tick_tasks["gh-2"], timeout=2.0)
        assert passes == ["gh-1", "gh-2"]
        await asyncio.wait_for(engine._tick(), timeout=2.0)
        await asyncio.wait_for(engine._github_tick_tasks["gh-2"], timeout=2.0)
        assert passes == ["gh-1", "gh-2", "gh-2"]
        release.set()
        await asyncio.wait_for(engine._github_tick_tasks["gh-1"], timeout=2.0)
        assert not engine._github_tick_tasks


@pytest.mark.asyncio
async def test_a_failing_pass_is_logged_and_the_tick_carries_on(caplog):
    engine = _engine([_github_schedule("gh-1"), _github_schedule("gh-2")])
    passes: list[str] = []

    async def pass_(schedule, now):
        passes.append(schedule["id"])
        if schedule["id"] == "gh-1":
            raise RuntimeError("poll blew up")

    with patch.object(engine, "_tick_github", new=pass_), caplog.at_level(logging.ERROR):
        await asyncio.wait_for(engine._tick(), timeout=2.0)
        await asyncio.gather(*engine._github_tick_tasks.values())
    assert passes == ["gh-1", "gh-2"]
    assert any("Error evaluating schedule poll-gh-1" in r.getMessage() for r in caplog.records)
    assert not engine._github_tick_tasks


@pytest.mark.asyncio
async def test_stop_cancels_a_running_pass():
    engine = _engine([_github_schedule("gh-1")])
    release = asyncio.Event()

    async def slow_pass(schedule, now):
        await release.wait()

    with patch.object(engine, "_tick_github", new=slow_pass):
        await asyncio.wait_for(engine._tick(), timeout=2.0)
        task = engine._github_tick_tasks["gh-1"]
        await asyncio.wait_for(engine.stop(), timeout=2.0)
    assert task.cancelled()
    assert not engine._github_tick_tasks
