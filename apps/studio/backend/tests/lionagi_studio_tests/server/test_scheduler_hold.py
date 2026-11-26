# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""LIONAGI_SCHEDULER_HOLD: the daemon serves its API with the scheduler engine not started."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from lionhub.serve.daemon.daemon_log import SCHEDULER_LOGGER

from .test_leg_host_lifespan import _quiet_lifespan, _serve_once

_HOLD = "LIONAGI_SCHEDULER_HOLD"
_HELD_LINE = (
    "LIONAGI_SCHEDULER_HOLD is set: the scheduler engine is not started, and no schedule "
    "fires until the daemon starts without it"
)


def _scheduler_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == SCHEDULER_LOGGER]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["1", "true", "ON"])
async def test_a_held_start_serves_without_the_scheduler(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    app, started = _quiet_lifespan(monkeypatch)
    monkeypatch.delenv("LIONAGI_STUDIO_LEG_HOST", raising=False)
    monkeypatch.setenv(_HOLD, value)
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)

    await _serve_once(app)

    # Only the engine is held: the rest of the daemon, the mirror included, starts.
    assert started == ["mirror"]
    assert _HELD_LINE in _scheduler_lines(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "0", "off"])
async def test_without_a_hold_the_scheduler_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str | None
) -> None:
    """The control: unset, empty or false, the same lifespan starts the engine."""
    app, started = _quiet_lifespan(monkeypatch)
    monkeypatch.delenv("LIONAGI_STUDIO_LEG_HOST", raising=False)
    if value is None:
        monkeypatch.delenv(_HOLD, raising=False)
    else:
        monkeypatch.setenv(_HOLD, value)
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)

    await _serve_once(app)

    assert started == ["scheduler", "mirror"]
    assert _HELD_LINE not in _scheduler_lines(caplog)


@pytest.mark.asyncio
async def test_a_hold_value_that_is_neither_on_nor_off_stops_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misspelt hold must not start the engine, and must not start the daemon unheld."""
    app, started = _quiet_lifespan(monkeypatch)
    monkeypatch.delenv("LIONAGI_STUDIO_LEG_HOST", raising=False)
    monkeypatch.setenv(_HOLD, "held")

    with pytest.raises(ValueError, match=_HOLD):
        await _serve_once(app)

    assert started == []


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionhub.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _row(schedule_id: str) -> dict:
    from lionhub.state.db import StateDB

    async with StateDB() as db:
        return await db.get_schedule(schedule_id)


@pytest.mark.asyncio
async def test_changes_made_under_a_hold_survive_into_a_normal_start(
    monkeypatch: pytest.MonkeyPatch, temp_db_path: Path
) -> None:
    """A disable and an enable made while held are what the next normal start reads.

    Three cron schedules whose next fire passed three days ago, as on a store whose daemon
    was down: one on before the hold and turned off during it, one off before and turned on
    during it, and one on and left alone with missed_fire_policy=run_once. The last is the
    control: the start that follows must fire it exactly once, so a start that fires nothing
    for the other two is a start that could have.
    """
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionhub.orchestration.schedule.timing as timing
    from lionhub.orchestration.scheduler.engine import SchedulerEngine
    from lionhub.orchestration.scheduler.scheduler_state import _DBSchedulerStateService
    from lionhub.serve.daemon.services.schedules import (
        create_schedule,
        disable_schedule,
        enable_schedule,
    )
    from lionhub.state.db import StateDB

    monkeypatch.setattr(timing, "SCHEDULER_TZ", "America/New_York")

    async def cron(name: str, **extra) -> str:
        row = await create_schedule(
            {
                "name": name,
                "trigger_type": "cron",
                "cron_expr": "0 18 * * *",
                "action_kind": "agent",
                "action_prompt": "ping",
                **extra,
            }
        )
        return row["id"]

    switched_off = await cron("held-switched-off")
    switched_on = await cron("held-switched-on")
    left_on = await cron("held-left-on", missed_fire_policy="run_once")
    assert await disable_schedule(switched_on) is True
    stale = time.time() - 3 * 86400
    async with StateDB() as db:
        for schedule_id in (switched_off, switched_on, left_on):
            await db.update_schedule(schedule_id, next_fire_at=stale)

    app, started = _quiet_lifespan(monkeypatch)
    monkeypatch.delenv("LIONAGI_STUDIO_LEG_HOST", raising=False)
    monkeypatch.setenv(_HOLD, "1")
    instance = SimpleNamespace(state=SimpleNamespace(session_tail_registry=None))
    async with app.lifespan(instance):
        # The service calls behind `li schedule disable` and `li schedule enable`.
        assert await disable_schedule(switched_off) is True
        assert await enable_schedule(switched_on) is True
    assert started == ["mirror"]

    held_at = time.time()
    off, on, left = await _row(switched_off), await _row(switched_on), await _row(left_on)
    assert off["enabled"] == 0
    assert on["enabled"] == 1
    assert on["next_fire_at"] > held_at
    # Nothing ran while held: the schedule left alone still carries the missed occurrence.
    assert left["next_fire_at"] == pytest.approx(stale)

    monkeypatch.delenv(_HOLD)
    fired: list[tuple[str, dict]] = []

    def record_fire(schedule, *_args, **kwargs):
        fired.append((schedule["id"], kwargs.get("trigger_context") or {}))

    engine = SchedulerEngine(svc=_DBSchedulerStateService())
    with (
        patch.object(engine, "_tracked_fire", side_effect=record_fire),
        patch(
            "lionhub.orchestration.scheduler.lifecycle.run_periodic_reapers",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "lionhub.orchestration.scheduler.db_maintenance.checkpoint_state_db",
            new=AsyncMock(return_value=None),
        ),
    ):
        try:
            # A normal start's order: the armed cron recompute, the startup recovery passes
            # (missed fires among them), then the first tick.
            await engine._recompute_armed_cron_schedules()
            await engine._startup_recovery_passes()
            await asyncio.wait_for(engine._tick(), timeout=5)
        finally:
            await engine.stop()

    assert [schedule_id for schedule_id, _ in fired] == [left_on], fired
    assert fired[0][1].get("missed_recovery") is True
    started_at = time.time()
    off, on = await _row(switched_off), await _row(switched_on)
    assert off["enabled"] == 0
    assert on["enabled"] == 1
    assert on["next_fire_at"] > started_at
    async with StateDB() as db:
        for schedule_id in (switched_off, switched_on):
            assert await db.count_schedule_runs(schedule_id, chain_depth=0) == 0
