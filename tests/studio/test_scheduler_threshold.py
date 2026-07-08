# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for metric threshold alerts.

Covers: threshold.validate_threshold_config/compare (pure), SchedulerEngine's
_evaluate_threshold_breach + _maybe_fire integration (mocked service, mirrors
test_scheduler_budget.py's pattern), StateDB.metric_value window math (real
in-memory DB, mirrors test_scheduler_budget.py's sum_schedule_spend tests).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


def _minimal_schedule(**overrides) -> dict:
    base = {
        "id": "sched-001",
        "name": "test-sched",
        "trigger_type": "interval",
        "interval_sec": 300,
        "action_kind": "agent",
        "action_model": "gpt-4.1-mini",
        "action_prompt": "{{metric}} breached {{threshold}} (observed {{value}})",
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
    svc.metric_value = AsyncMock(return_value=0.0)
    return svc


# ---------------------------------------------------------------------------
# threshold.py — pure validation + comparison
# ---------------------------------------------------------------------------


def test_validate_threshold_config_accepts_well_formed():
    from lionagi.studio.scheduler.threshold import validate_threshold_config

    validate_threshold_config(
        {"metric": "failed_sessions", "op": "gt", "value": 5, "window_minutes": 60}
    )  # does not raise


@pytest.mark.parametrize(
    "bad_config",
    [
        "not-a-dict",
        {"metric": "bogus_metric", "op": "gt", "value": 5, "window_minutes": 60},
        {"metric": "failed_sessions", "op": "lt", "value": 5, "window_minutes": 60},
        {"metric": "failed_sessions", "op": "gt", "value": "five", "window_minutes": 60},
        {"metric": "failed_sessions", "op": "gt", "value": True, "window_minutes": 60},
        {"metric": "failed_sessions", "op": "gt", "value": 5, "window_minutes": 0},
        {"metric": "failed_sessions", "op": "gt", "value": 5, "window_minutes": "60"},
        {"metric": "failed_sessions", "op": "gt", "value": 5},
    ],
)
def test_validate_threshold_config_rejects_malformed(bad_config):
    from lionagi.studio.scheduler.threshold import validate_threshold_config

    with pytest.raises(ValueError):
        validate_threshold_config(bad_config)


def test_compare_gt_and_gte():
    from lionagi.studio.scheduler.threshold import compare

    assert compare("gt", 6, 5) is True
    assert compare("gt", 5, 5) is False
    assert compare("gte", 5, 5) is True
    assert compare("gte", 4, 5) is False


def test_compare_rejects_unknown_op():
    from lionagi.studio.scheduler.threshold import compare

    with pytest.raises(ValueError, match="Unsupported threshold op"):
        compare("lt", 1, 2)


# ---------------------------------------------------------------------------
# services/schedules.py — service-boundary validation
# ---------------------------------------------------------------------------


def test_svc_validate_threshold_config_accepts_none():
    from lionagi.studio.services.schedules import _svc_validate_threshold_config

    _svc_validate_threshold_config(None)  # does not raise


def test_svc_validate_threshold_config_rejects_bad_metric():
    from lionagi.studio.services.schedules import _svc_validate_threshold_config

    with pytest.raises(ValueError, match="metric"):
        _svc_validate_threshold_config(
            {"metric": "nope", "op": "gt", "value": 1, "window_minutes": 5}
        )


# ---------------------------------------------------------------------------
# SchedulerEngine._evaluate_threshold_breach
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_threshold_breach_returns_none_when_no_config():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(threshold_config=None)

    assert await engine._evaluate_threshold_breach(schedule, now=1000.0) is None
    svc.metric_value.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluate_threshold_breach_none_when_under_threshold():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.metric_value = AsyncMock(return_value=3.0)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 60,
        }
    )

    assert await engine._evaluate_threshold_breach(schedule, now=1000.0) is None
    svc.metric_value.assert_awaited_once_with("failed_sessions", 1000.0 - 60 * 60)


@pytest.mark.asyncio
async def test_evaluate_threshold_breach_returns_breach_dict_when_over():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.metric_value = AsyncMock(return_value=42.0)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        threshold_config={
            "metric": "total_cost_usd",
            "op": "gte",
            "value": 10.0,
            "window_minutes": 15,
        }
    )

    breach = await engine._evaluate_threshold_breach(schedule, now=5000.0)

    assert breach == {
        "metric": "total_cost_usd",
        "op": "gte",
        "value": 42.0,
        "threshold": 10.0,
        "window_minutes": 15,
    }


# ---------------------------------------------------------------------------
# SchedulerEngine._maybe_fire — full threshold-alert integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_fire_no_breach_advances_next_fire_without_firing():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.metric_value = AsyncMock(return_value=1.0)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        next_fire_at=1000.0,
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 30,
        },
    )

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_not_called()
    svc.update_schedule.assert_awaited_once()
    args, kwargs = svc.update_schedule.await_args
    assert args[0] == "sched-001"
    assert "next_fire_at" in kwargs
    assert "last_alert_at" not in kwargs


@pytest.mark.asyncio
async def test_maybe_fire_breach_fires_with_trigger_context():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.metric_value = AsyncMock(return_value=9.0)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        next_fire_at=1000.0,
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 30,
        },
    )

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_called_once()
    _, kwargs = mock_tracked.call_args
    ctx = kwargs["trigger_context"]
    assert ctx["metric"] == "failed_sessions"
    assert ctx["op"] == "gt"
    assert ctx["value"] == 9.0
    assert ctx["threshold"] == 5
    assert ctx["window_minutes"] == 30

    # last_alert_at was stamped as part of the fire (cooldown start).
    last_alert_calls = [
        c for c in svc.update_schedule.await_args_list if "last_alert_at" in c.kwargs
    ]
    assert len(last_alert_calls) == 1
    assert last_alert_calls[0].args[0] == "sched-001"
    assert last_alert_calls[0].kwargs["last_alert_at"] == 1000.0


@pytest.mark.asyncio
async def test_maybe_fire_breach_within_cooldown_suppresses_refire():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.metric_value = AsyncMock(return_value=9.0)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        next_fire_at=1000.0,
        last_alert_at=990.0,  # 10s ago, well within the 30-minute cooldown window
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 30,
        },
    )

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_not_called()
    # Only the next_fire_at advance -- no second last_alert_at stamp.
    last_alert_calls = [
        c for c in svc.update_schedule.await_args_list if "last_alert_at" in c.kwargs
    ]
    assert not last_alert_calls


@pytest.mark.asyncio
async def test_maybe_fire_breach_after_cooldown_elapsed_refires():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.metric_value = AsyncMock(return_value=9.0)
    engine = SchedulerEngine(svc=svc)
    window_minutes = 30
    schedule = _minimal_schedule(
        next_fire_at=10_000.0,
        last_alert_at=10_000.0 - (window_minutes * 60) - 1,  # just past cooldown
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": window_minutes,
        },
    )

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=10_000.0)

    mock_tracked.assert_called_once()


@pytest.mark.asyncio
async def test_maybe_fire_threshold_still_honors_overlap_policy():
    """A breach that would fire is still gated by the pre-existing overlap check."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.metric_value = AsyncMock(return_value=9.0)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        next_fire_at=1000.0,
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 30,
        },
    )
    engine._running[schedule["id"]] = "some-other-run"

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_not_called()
    # last_alert_at must NOT be consumed by a fire that never actually spawned.
    last_alert_calls = [
        c for c in svc.update_schedule.await_args_list if "last_alert_at" in c.kwargs
    ]
    assert not last_alert_calls


# ---------------------------------------------------------------------------
# StateDB.metric_value — real in-memory DB, window math
# ---------------------------------------------------------------------------


async def _make_session(
    state, sess_id: str, *, status: str, ended_at: float, total_cost_usd: float = 0.0
):
    prog_id = f"prog-{sess_id}"
    await state.create_progression(prog_id)
    await state.create_session(
        {
            "id": sess_id,
            "progression_id": prog_id,
            "status": status,
            "started_at": ended_at - 1,
            "ended_at": ended_at,
        }
    )
    await state.update_session(sess_id, total_cost_usd=total_cost_usd)


@pytest.mark.asyncio
async def test_metric_value_failed_sessions_counts_only_in_window():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await _make_session(state, "in-window-failed", status="failed", ended_at=100.0)
    await _make_session(state, "in-window-timed-out", status="timed_out", ended_at=110.0)
    await _make_session(state, "in-window-completed", status="completed", ended_at=120.0)
    await _make_session(state, "before-window-failed", status="failed", ended_at=10.0)

    count = await state.metric_value("failed_sessions", window_start=50.0)
    assert count == 2.0

    await state.close()


@pytest.mark.asyncio
async def test_metric_value_total_cost_usd_sums_only_in_window():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await _make_session(state, "s1", status="completed", ended_at=100.0, total_cost_usd=1.5)
    await _make_session(state, "s2", status="completed", ended_at=110.0, total_cost_usd=2.5)
    await _make_session(state, "s3-before", status="completed", ended_at=10.0, total_cost_usd=100.0)

    total = await state.metric_value("total_cost_usd", window_start=50.0)
    assert total == pytest.approx(4.0)

    await state.close()


@pytest.mark.asyncio
async def test_metric_value_p95_latency_ms_window_and_percentile():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    # 20 invocations inside the window with latencies 1..20 seconds;
    # nearest-rank p95 of 20 samples is the 19th smallest (index 18) = 19s.
    for i in range(1, 21):
        await state.create_invocation(
            {
                "id": f"inv-{i}",
                "skill": "agent",
                "started_at": 100.0,
                "ended_at": 100.0 + i,
            }
        )
    # One invocation before the window with an extreme latency that must not
    # skew the percentile.
    await state.create_invocation(
        {"id": "inv-before", "skill": "agent", "started_at": 10.0, "ended_at": 10.0 + 999}
    )

    p95 = await state.metric_value("p95_latency_ms", window_start=50.0)
    assert p95 == pytest.approx(19_000.0)

    await state.close()


@pytest.mark.asyncio
async def test_metric_value_p95_latency_ms_empty_window_returns_zero():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    p95 = await state.metric_value("p95_latency_ms", window_start=50.0)
    assert p95 == 0.0

    await state.close()


@pytest.mark.asyncio
async def test_metric_value_unknown_metric_raises():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    with pytest.raises(ValueError, match="Unknown threshold metric"):
        await state.metric_value("bogus", window_start=0.0)

    await state.close()


# ---------------------------------------------------------------------------
# create_schedule / update_schedule round-trip threshold_config + last_alert_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_round_trips_threshold_config_and_last_alert_at():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await state.create_schedule(
        {
            "id": "sched-threshold-1",
            "name": "threshold-test",
            "trigger_type": "interval",
            "interval_sec": 300,
            "action_kind": "agent",
            "threshold_config": {
                "metric": "failed_sessions",
                "op": "gt",
                "value": 5,
                "window_minutes": 60,
            },
        }
    )

    row = await state.get_schedule("sched-threshold-1")
    assert row["threshold_config"] == {
        "metric": "failed_sessions",
        "op": "gt",
        "value": 5,
        "window_minutes": 60,
    }
    assert row["last_alert_at"] is None

    await state.update_schedule("sched-threshold-1", last_alert_at=123.0)
    row = await state.get_schedule("sched-threshold-1")
    assert row["last_alert_at"] == 123.0

    await state.close()
