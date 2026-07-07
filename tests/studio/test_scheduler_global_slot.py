# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for SchedulerEngine's global concurrent-fire cap.

Covers _reserve_global_slot()/_release_global_slot() in isolation, and the
three fire entry points (_maybe_fire, _tick_github, fire_now) that enforce
the cap around the existing max_runs reservation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _minimal_schedule(**overrides) -> dict:
    base = {
        "id": "sched-001",
        "name": "test-sched",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_model": "gpt-4.1-mini",
        "action_prompt": "ping",
        "action_agent": None,
        "action_playbook": None,
        "action_project": None,
        "action_extra_args": [],
        "action_flow_yaml": None,
        "on_success": None,
        "on_fail": None,
        "overlap_policy": "skip",
        "missed_fire_policy": "skip",
    }
    base.update(overrides)
    return base


def _make_svc() -> AsyncMock:
    svc = AsyncMock()
    svc.get_schedule = AsyncMock(return_value=None)
    svc.list_schedules = AsyncMock(return_value=[])
    svc.update_schedule = AsyncMock()
    svc.create_schedule_run = AsyncMock()
    svc.update_schedule_run = AsyncMock()
    svc.create_invocation = AsyncMock()
    svc.update_invocation = AsyncMock()
    svc.update_status = AsyncMock()
    svc.list_sessions_for_invocation = AsyncMock(return_value=[])
    svc.count_schedule_runs = AsyncMock(return_value=0)
    return svc


# ---------------------------------------------------------------------------
# _reserve_global_slot / _release_global_slot — pure reservation logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserve_global_slot_allows_under_cap(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 2)
    engine = SchedulerEngine(svc=_make_svc())

    allowed, claim = await engine._reserve_global_slot()

    assert allowed is True
    assert claim is not None
    assert engine._global_inflight == 1


@pytest.mark.asyncio
async def test_reserve_global_slot_refuses_at_cap(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 1)
    engine = SchedulerEngine(svc=_make_svc())

    allowed_a, claim_a = await engine._reserve_global_slot()
    assert allowed_a is True
    assert claim_a is not None

    allowed_b, claim_b = await engine._reserve_global_slot()
    assert allowed_b is False
    assert claim_b is None
    assert engine._global_inflight == 1


@pytest.mark.asyncio
async def test_reserve_global_slot_unlimited_when_cap_zero(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 0)
    engine = SchedulerEngine(svc=_make_svc())

    for _ in range(5):
        allowed, claim = await engine._reserve_global_slot()
        assert allowed is True
        assert claim is None

    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_release_global_slot_decrements_and_floors_at_zero(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 3)
    engine = SchedulerEngine(svc=_make_svc())

    _, claim = await engine._reserve_global_slot()
    assert engine._global_inflight == 1
    claim.release()
    assert engine._global_inflight == 0

    # A second release is a no-op (idempotent) and must not go negative.
    claim.release()
    assert engine._global_inflight == 0

    # Direct floor check independent of the claim wrapper.
    engine._release_global_slot()
    assert engine._global_inflight == 0


# ---------------------------------------------------------------------------
# _maybe_fire — defers on no slot, fires normally when available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_fire_defers_when_no_slot_available(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(next_fire_at=1000.0)

    # Saturate the single slot before _maybe_fire runs.
    _, holder_claim = await engine._reserve_global_slot()
    assert holder_claim is not None

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_not_called()
    # next_fire_at must be left untouched (still due) so the next tick retries.
    svc.update_schedule.assert_not_awaited()
    # A deferred-capacity skipped-run record was emitted.
    svc.create_schedule_run.assert_awaited_once()
    (run_payload,), _ = svc.create_schedule_run.await_args
    assert run_payload["trigger_context"]["deferred_capacity"] is True
    deferred_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.kwargs.get("reason_code") == "schedule.deferred.capacity"
    ]
    assert deferred_calls


@pytest.mark.asyncio
async def test_maybe_fire_defer_releases_max_runs_claim(monkeypatch):
    """A schedule with a max_runs budget must get its pre-flight reservation
    back when the fire is deferred for lack of a global slot -- otherwise the
    deferral permanently leaks a max_runs reservation."""
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _make_svc()
    svc.count_schedule_runs = AsyncMock(return_value=0)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=5, next_fire_at=1000.0)

    _, holder_claim = await engine._reserve_global_slot()
    assert holder_claim is not None

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_not_called()
    assert engine._max_runs_inflight.get(schedule["id"], 0) == 0


@pytest.mark.asyncio
async def test_maybe_fire_fires_when_slot_available(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 4)
    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule()

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_called_once()
    _, kwargs = mock_tracked.call_args
    assert kwargs["global_slot_claim"] is not None


# ---------------------------------------------------------------------------
# _tick_github — defers before fetch when no slot; releases on no-events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_github_defers_before_fetch_when_no_slot(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        trigger_type="github_poll", github_repo="acme/widgets", last_fired_at=0
    )

    _, holder_claim = await engine._reserve_global_slot()
    assert holder_claim is not None

    with patch("lionagi.studio.scheduler.github.github_poll", new=AsyncMock()) as mock_poll:
        await engine._tick_github(schedule, now=10_000.0)

    mock_poll.assert_not_awaited()
    svc.update_schedule.assert_not_awaited()
    svc.create_schedule_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_tick_github_releases_slot_on_no_events(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        trigger_type="github_poll", github_repo="acme/widgets", last_fired_at=0
    )

    with patch(
        "lionagi.studio.scheduler.github.github_poll",
        new=AsyncMock(return_value=[]),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_tick_github_fires_and_releases_slot_on_completion(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 4)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        trigger_type="github_poll", github_repo="acme/widgets", last_fired_at=0
    )

    with (
        patch(
            "lionagi.studio.scheduler.github.github_poll",
            new=AsyncMock(return_value=[{"number": 1}]),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    assert engine._global_inflight == 0


# ---------------------------------------------------------------------------
# fire_now — refuses (does not defer) at capacity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_now_raises_at_capacity_and_releases_max_runs_claim(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _make_svc()
    svc.get_schedule = AsyncMock(return_value=_minimal_schedule(max_runs=5))
    svc.count_schedule_runs = AsyncMock(return_value=0)
    engine = SchedulerEngine(svc=svc)

    _, holder_claim = await engine._reserve_global_slot()
    assert holder_claim is not None

    with pytest.raises(ValueError, match="capacity"):
        await engine.fire_now("sched-001")

    assert engine._max_runs_inflight.get("sched-001", 0) == 0
    assert len(engine._fire_tasks) == 0


@pytest.mark.asyncio
async def test_fire_now_succeeds_when_slot_available(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 4)
    svc = _make_svc()
    svc.get_schedule = AsyncMock(return_value=_minimal_schedule())
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire", return_value=MagicMock()) as mock_tracked:
        run_id = await engine.fire_now("sched-001")

    assert run_id is not None
    _, kwargs = mock_tracked.call_args
    assert kwargs["global_slot_claim"] is not None


# ---------------------------------------------------------------------------
# Slot released after a real _fire() completes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_releases_global_slot_on_completion(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 4)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    _, slot_claim = await engine._reserve_global_slot()
    assert engine._global_inflight == 1

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await engine._fire(
            schedule,
            "run-001",
            trigger_context={"scheduled": True},
            global_slot_claim=slot_claim,
        )

    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_fire_releases_global_slot_on_exception(monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 4)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    _, slot_claim = await engine._reserve_global_slot()
    assert engine._global_inflight == 1

    with patch(
        "lionagi.studio.scheduler.subprocess.build_argv",
        side_effect=ValueError("bad action_kind"),
    ):
        await engine._fire(
            schedule,
            "run-002",
            trigger_context={"scheduled": True},
            global_slot_claim=slot_claim,
        )

    assert engine._global_inflight == 0


# ---------------------------------------------------------------------------
# Deferred-record throttling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_record_deferred_throttles_after_first(monkeypatch):
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    for _ in range(9):
        await engine._maybe_record_deferred(schedule, now=1000.0)

    # First deferral emits; deferrals 2-9 (count % 10 != 1) do not.
    assert svc.create_schedule_run.await_count == 1

    # The 10th and 11th deferrals: count=10 (no emit), count=11 (emit, 11%10==1).
    await engine._maybe_record_deferred(schedule, now=1000.0)
    assert svc.create_schedule_run.await_count == 1
    await engine._maybe_record_deferred(schedule, now=1000.0)
    assert svc.create_schedule_run.await_count == 2
