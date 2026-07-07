# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for SchedulerEngine's per-schedule token/spend budget.

Covers _check_budget() in isolation, and the three fire entry points
(_maybe_fire, _tick_github, fire_now) that enforce the budget as a pre-fire
cumulative gate -- a pure read, unlike max_runs / the global slot which are
claim/release reservations.
"""

from __future__ import annotations

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
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 0.0, "tokens": 0})
    return svc


# ---------------------------------------------------------------------------
# service-boundary validation — non-finite budgets must be rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_svc_validate_budget_usd_rejects_non_finite(bad_value):
    """A non-finite budget_usd is rejected at the service boundary.

    A plain ``<= 0`` predicate lets nan/inf through; nan then round-trips to NULL
    in SQLite and _check_budget treats the schedule as unbounded forever.
    """
    from lionagi.studio.services.schedules import _svc_validate_budget_usd

    with pytest.raises(ValueError, match="finite positive number"):
        _svc_validate_budget_usd(bad_value)


@pytest.mark.parametrize("good_value", [0.01, 1, 12.5, 1000.0])
def test_svc_validate_budget_usd_accepts_finite_positive(good_value):
    from lionagi.studio.services.schedules import _svc_validate_budget_usd

    _svc_validate_budget_usd(good_value)  # does not raise


# ---------------------------------------------------------------------------
# _check_budget — pure read, no reservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_budget_unbounded_when_both_columns_null():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(budget_usd=None, budget_tokens=None)

    assert await engine._check_budget(schedule) is False
    svc.sum_schedule_spend.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_budget_over_on_cost_usd():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 10.0, "tokens": 0})
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(budget_usd=10.0, budget_tokens=None)

    assert await engine._check_budget(schedule) is True


@pytest.mark.asyncio
async def test_check_budget_over_on_tokens():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 0.0, "tokens": 5000})
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(budget_usd=None, budget_tokens=5000)

    assert await engine._check_budget(schedule) is True


@pytest.mark.asyncio
async def test_check_budget_under_both_bounds_fires():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 1.0, "tokens": 100})
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(budget_usd=10.0, budget_tokens=5000)

    assert await engine._check_budget(schedule) is False


@pytest.mark.asyncio
async def test_check_budget_cost_only_bound_trips():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 20.0, "tokens": 100})
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(budget_usd=10.0, budget_tokens=None)

    assert await engine._check_budget(schedule) is True


@pytest.mark.asyncio
async def test_check_budget_tokens_only_bound_trips():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 1.0, "tokens": 9000})
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(budget_usd=None, budget_tokens=5000)

    assert await engine._check_budget(schedule) is True


@pytest.mark.asyncio
async def test_check_budget_either_bound_trips_when_both_set():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    # Cost is under, but tokens are over -- either bound tripping is sufficient.
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 1.0, "tokens": 9000})
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(budget_usd=10.0, budget_tokens=5000)

    assert await engine._check_budget(schedule) is True


# ---------------------------------------------------------------------------
# _maybe_fire — auto-disables + records, does not fire when over budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_fire_disables_and_records_when_over_budget():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 10.0, "tokens": 0})
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(budget_usd=10.0, next_fire_at=1000.0)

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_not_called()
    svc.update_schedule.assert_awaited_once_with("sched-001", enabled=0)
    svc.create_schedule_run.assert_awaited_once()
    (run_payload,), _ = svc.create_schedule_run.await_args
    assert run_payload["trigger_context"]["budget_exhausted"] is True
    budget_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.kwargs.get("reason_code") == "schedule.budget.exhausted"
    ]
    assert budget_calls


@pytest.mark.asyncio
async def test_maybe_fire_fires_normally_when_under_budget():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 1.0, "tokens": 0})
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(budget_usd=10.0, next_fire_at=1000.0)

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_called_once()
    svc.update_schedule.assert_not_awaited()


# ---------------------------------------------------------------------------
# _tick_github — disables over-budget schedules WITHOUT polling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_github_disables_without_polling_when_over_budget():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 0.0, "tokens": 5000})
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        trigger_type="github_poll",
        github_repo="acme/widgets",
        last_fired_at=0,
        budget_tokens=5000,
    )

    with patch("lionagi.studio.scheduler.github.github_poll", new=AsyncMock()) as mock_poll:
        await engine._tick_github(schedule, now=10_000.0)

    mock_poll.assert_not_awaited()
    svc.update_schedule.assert_awaited_once_with("sched-001", enabled=0)
    svc.create_schedule_run.assert_awaited_once()
    (run_payload,), _ = svc.create_schedule_run.await_args
    assert run_payload["trigger_context"]["budget_exhausted"] is True
    # The budget check runs before slot reservation, so a bailed fire must leave
    # the global concurrency counter untouched -- a regression that moved the
    # check after _reserve_global_slot and returned without release would leak here.
    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_tick_github_polls_normally_when_under_budget():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 0.0, "tokens": 0})
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        trigger_type="github_poll",
        github_repo="acme/widgets",
        last_fired_at=0,
        budget_tokens=5000,
    )

    with patch(
        "lionagi.studio.scheduler.github.github_poll",
        new=AsyncMock(return_value=[]),
    ) as mock_poll:
        await engine._tick_github(schedule, now=10_000.0)

    mock_poll.assert_awaited_once()
    svc.update_schedule.assert_not_awaited()


# ---------------------------------------------------------------------------
# fire_now — refuses (does not disable) at exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_now_raises_when_over_budget():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.get_schedule = AsyncMock(return_value=_minimal_schedule(budget_usd=10.0))
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 10.0, "tokens": 0})
    engine = SchedulerEngine(svc=svc)

    with pytest.raises(ValueError, match="exhausted its budget"):
        await engine.fire_now("sched-001")

    # fire_now refuses, it does not auto-disable (mirrors max_runs).
    svc.update_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_fire_now_succeeds_when_under_budget():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.get_schedule = AsyncMock(return_value=_minimal_schedule(budget_usd=10.0))
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 1.0, "tokens": 0})
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire", return_value=MagicMock()) as mock_tracked:
        run_id = await engine.fire_now("sched-001")

    assert run_id is not None
    mock_tracked.assert_called_once()


# ---------------------------------------------------------------------------
# sum_schedule_spend — real StateDB aggregate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sum_schedule_spend_aggregates_across_sessions():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await state.create_schedule(
        {
            "id": "sched-spend-1",
            "name": "spend-test",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )

    for i in range(2):
        inv_id = f"inv-{i}"
        await state.create_invocation({"id": inv_id, "skill": "agent", "started_at": 1.0})
        await state.create_schedule_run(
            {
                "id": f"run-{i}",
                "schedule_id": "sched-spend-1",
                "invocation_id": inv_id,
                "trigger_context": {},
                "action_kind": "agent",
                "action_args": [],
                "status": "completed",
                "chain_depth": 0,
                "fired_at": 1.0,
            }
        )
        prog_id = f"prog-{i}"
        await state.create_progression(prog_id)
        sess_id = f"sess-{i}"
        await state.create_session(
            {
                "id": sess_id,
                "progression_id": prog_id,
                "status": "completed",
                "invocation_id": inv_id,
            }
        )
        await state.update_session(
            sess_id,
            input_tokens=100 * (i + 1),
            output_tokens=50 * (i + 1),
            total_cost_usd=1.5 * (i + 1),
        )

    spend = await state.sum_schedule_spend("sched-spend-1")

    assert spend["cost_usd"] == pytest.approx(1.5 + 3.0)
    assert spend["tokens"] == (100 + 50) + (200 + 100)

    await state.close()


@pytest.mark.asyncio
async def test_sum_schedule_spend_zero_for_schedule_with_no_runs():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await state.create_schedule(
        {
            "id": "sched-spend-empty",
            "name": "spend-empty",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )

    spend = await state.sum_schedule_spend("sched-spend-empty")
    assert spend == {"cost_usd": 0.0, "tokens": 0}

    await state.close()
