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
    svc.create_schedule_run_and_advance = AsyncMock()
    svc.schedule_run_exists_since = AsyncMock(return_value=False)
    svc.update_schedule_run = AsyncMock()
    svc.create_invocation = AsyncMock()
    svc.update_invocation = AsyncMock()
    svc.update_status = AsyncMock()
    svc.list_sessions_for_invocation = AsyncMock(return_value=[])
    svc.count_schedule_runs = AsyncMock(return_value=0)
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 0.0, "tokens": 0})
    svc.metric_value = AsyncMock(return_value=0.0)
    svc.get_invocation = AsyncMock(return_value=None)
    svc.compute_files_overlap = AsyncMock(return_value={"count": 0, "top": []})
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


def test_validate_threshold_config_rejects_unknown_key():
    """A typo'd/extra key (e.g. a made-up 'cooldown_minutes') must be
    rejected rather than silently ignored -- there is no such field; the
    cooldown reuses window_minutes."""
    from lionagi.studio.scheduler.threshold import validate_threshold_config

    with pytest.raises(ValueError, match="unknown key") as exc_info:
        validate_threshold_config(
            {
                "metric": "failed_sessions",
                "op": "gt",
                "value": 5,
                "window_minutes": 60,
                "cooldown_minutes": 120,
            }
        )
    assert "cooldown_minutes" in str(exc_info.value)
    assert "metric" in str(exc_info.value)  # names the allowed keys too
    assert "window_minutes" in str(exc_info.value)


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


def test_svc_validate_threshold_config_rejects_unknown_key():
    from lionagi.studio.services.schedules import _svc_validate_threshold_config

    with pytest.raises(ValueError, match="unknown key"):
        _svc_validate_threshold_config(
            {
                "metric": "failed_sessions",
                "op": "gt",
                "value": 1,
                "window_minutes": 5,
                "cooldown_minutes": 10,
            }
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

    # last_alert_at is NOT stamped by _maybe_fire itself -- _tracked_fire is
    # mocked here (the actual _fire_inner never runs), and the real stamp
    # only lands once a schedule_run row is durably persisted inside
    # _fire_inner (see the "_fire()/_fire_inner() threshold cooldown stamp"
    # tests further below, which exercise the real fire path). Stamping
    # this early would consume the cooldown even if the mocked-out fire
    # never actually happened.
    last_alert_calls = [
        c for c in svc.update_schedule.await_args_list if "last_alert_at" in c.kwargs
    ]
    assert not last_alert_calls


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
# _maybe_fire() — synchronous in-process cooldown reservation. last_alert_at
# alone (a DB read) can go stale between ticks; these tests pin the
# in-memory _threshold_pending gate that closes the resulting duplicate-fire
# race, independent of when (or whether) the durable stamp lands.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_fire_two_ticks_before_first_stamp_fires_only_once():
    """Regression pin for the duplicate-alert race: without a synchronous
    in-process reservation, two ticks separated by _TICK_INTERVAL could both
    read the same stale (not-yet-durably-stamped) last_alert_at and both
    pass the cooldown gate before either fire's background task reaches its
    own stamp. _tracked_fire is mocked here, so the first fire never
    reaches its own release point -- mirroring "tick 2 arrives before tick
    1's fire has done anything durable yet" -- and the second _maybe_fire
    call for the same schedule must still be suppressed by the in-process
    reservation alone."""
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
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_called_once()
    assert schedule["id"] in engine._threshold_pending


@pytest.mark.asyncio
async def test_maybe_fire_reservation_released_after_pre_persistence_failure_allows_refire():
    """create_invocation() raising is a pre-persistence failure -- no
    schedule_run row is ever written. _fire()'s finally must still release
    the in-process threshold reservation (mirroring how it already releases
    max_runs_claim/global_slot_claim on the same exit path), so the next
    tick is free to try again instead of being permanently muted."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.metric_value = AsyncMock(return_value=9.0)
    svc.create_invocation = AsyncMock(side_effect=RuntimeError("db unavailable"))
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

    await engine._maybe_fire(schedule, now=1000.0)
    assert schedule["id"] in engine._threshold_pending
    fire_tasks = list(engine._fire_tasks)
    assert len(fire_tasks) == 1
    with pytest.raises(RuntimeError, match="db unavailable"):
        await fire_tasks[0]

    assert schedule["id"] not in engine._threshold_pending
    svc.create_schedule_run.assert_not_awaited()

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)
    mock_tracked.assert_called_once()


@pytest.mark.asyncio
async def test_maybe_fire_exception_between_reserve_and_fire_releases_threshold_claim():
    """The reservation happens synchronously before the overlap/budget/
    max_runs/global-slot gates run -- a raise from ANY of those gates (not
    just a normal early-return) must still release threshold_claim via
    _maybe_fire's own try/finally, not just _fire()'s. Otherwise a
    transient error mid-gate (e.g. a DB blip in _reserve_max_runs_budget)
    leaks the reservation permanently, muting the alert until an engine
    restart -- worse than the duplicate it exists to prevent."""
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

    with (
        patch.object(
            engine,
            "_reserve_max_runs_budget",
            AsyncMock(side_effect=RuntimeError("db blip")),
        ),
        pytest.raises(RuntimeError, match="db blip"),
    ):
        await engine._maybe_fire(schedule, now=1000.0)

    assert schedule["id"] not in engine._threshold_pending
    assert not engine._fire_tasks

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)
    mock_tracked.assert_called_once()


# ---------------------------------------------------------------------------
# _fire() / _fire_inner() — threshold cooldown stamp lands AFTER the
# schedule_run row is durably persisted, not before the fire starts.
# ---------------------------------------------------------------------------


def _threshold_ctx(**overrides) -> dict:
    ctx = {
        "scheduled": True,
        "metric": "failed_sessions",
        "op": "gt",
        "value": 9.0,
        "threshold": 5,
        "window_minutes": 30,
    }
    ctx.update(overrides)
    return ctx


def _last_alert_calls(svc: AsyncMock) -> list:
    """last_alert_at now rides create_schedule_run_and_advance()'s
    schedule_fields, folded into the same atomic call as the occurrence
    insert -- not a standalone update_schedule() call."""
    return [
        c
        for c in svc.create_schedule_run_and_advance.await_args_list
        if "last_alert_at" in c.kwargs.get("schedule_fields", {})
    ]


@pytest.mark.asyncio
async def test_fire_threshold_schedule_stamps_last_alert_at_after_run_persisted():
    """Happy path: create_schedule_run succeeds, so the cooldown stamp lands
    in the same update_schedule() call that writes last_fired_at."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 30,
        },
    )

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
        await engine._fire(schedule, "run-alert-1", trigger_context=_threshold_ctx())

    svc.create_schedule_run.assert_not_awaited()
    svc.create_schedule_run_and_advance.assert_awaited_once()
    calls = _last_alert_calls(svc)
    assert len(calls) == 1
    assert calls[0].kwargs["schedule_id"] == "sched-001"


@pytest.mark.asyncio
async def test_fire_create_invocation_failure_does_not_stamp_last_alert_at():
    """The blocking gap this regression pins: create_invocation() raises
    BEFORE any schedule_run row exists, so the cooldown must NOT be
    consumed -- the next tick re-evaluates and re-alerts instead of
    silently losing the alert."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.create_invocation = AsyncMock(side_effect=RuntimeError("db unavailable"))
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 30,
        },
    )

    with pytest.raises(RuntimeError, match="db unavailable"):
        await engine._fire(schedule, "run-fail-inv", trigger_context=_threshold_ctx())

    svc.create_schedule_run.assert_not_awaited()
    assert not _last_alert_calls(svc)


@pytest.mark.asyncio
async def test_fire_invalid_action_still_stamps_last_alert_at_after_failed_run_persisted():
    """build_argv() raising (bad action config) still durably persists a
    'failed' schedule_run row before the schedule update -- that IS a
    recorded (if broken) alert attempt, so the cooldown correctly stamps
    to avoid hammering a schedule with a persistently-broken action."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 30,
        },
    )

    with patch(
        "lionagi.studio.scheduler.subprocess.build_argv",
        side_effect=ValueError("bad action_kind"),
    ):
        await engine._fire(schedule, "run-alert-2", trigger_context=_threshold_ctx())

    svc.create_schedule_run.assert_not_awaited()
    svc.create_schedule_run_and_advance.assert_awaited_once()
    calls = _last_alert_calls(svc)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fire_invalid_action_releases_threshold_cooldown_claim():
    """The invalid-action branch (build_argv raising) persists a 'failed'
    schedule_run row and returns normally rather than raising, so _fire()'s
    finally releases the threshold_cooldown_claim the same way the happy
    path does -- the claim must not survive past this branch either."""
    from lionagi.studio.scheduler.engine import SchedulerEngine, _ThresholdCooldownClaim

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 30,
        },
    )
    sid = schedule["id"]
    engine._threshold_pending.add(sid)
    claim = _ThresholdCooldownClaim(engine, sid)

    with patch(
        "lionagi.studio.scheduler.subprocess.build_argv",
        side_effect=ValueError("bad action_kind"),
    ):
        await engine._fire(
            schedule,
            "run-alert-invalid",
            trigger_context=_threshold_ctx(),
            threshold_cooldown_claim=claim,
        )

    svc.create_schedule_run.assert_not_awaited()
    svc.create_schedule_run_and_advance.assert_awaited_once()
    assert sid not in engine._threshold_pending


@pytest.mark.asyncio
async def test_fire_success_releases_threshold_cooldown_claim():
    """Happy path: the threshold_cooldown_claim is released once _fire()
    completes, same as max_runs_claim/global_slot_claim."""
    from lionagi.studio.scheduler.engine import SchedulerEngine, _ThresholdCooldownClaim

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 30,
        },
    )
    sid = schedule["id"]
    engine._threshold_pending.add(sid)
    claim = _ThresholdCooldownClaim(engine, sid)

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
            "run-alert-success",
            trigger_context=_threshold_ctx(),
            threshold_cooldown_claim=claim,
        )

    assert sid not in engine._threshold_pending


@pytest.mark.asyncio
async def test_fire_chain_child_does_not_restamp_last_alert_at():
    """on_success/on_fail chain children (chain_depth > 0) inherit
    threshold_config via the shallow schedule merge but must not restamp
    the cooldown -- they're a follow-on of the same alert cycle."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        threshold_config={
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 30,
        },
    )

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
        await engine._fire(schedule, "run-chain-1", trigger_context=_threshold_ctx(), chain_depth=1)

    assert not _last_alert_calls(svc)


@pytest.mark.asyncio
async def test_fire_non_threshold_schedule_never_stamps_last_alert_at():
    """A schedule with no threshold_config never writes last_alert_at,
    even on a normal successful fire."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()  # no threshold_config

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
        await engine._fire(schedule, "run-no-threshold", trigger_context={"scheduled": True})

    assert not _last_alert_calls(svc)


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
# StateDB.metric_value — github_poll_healthy_age_minutes / _consecutive_401
# (observer self-health)
# ---------------------------------------------------------------------------


async def _make_github_schedule(state, sched_id: str, **overrides):
    schedule = {
        "id": sched_id,
        "name": sched_id,
        "trigger_type": "github_poll",
        "github_repo": "acme/widgets",
        "action_kind": "agent",
    }
    schedule.update(overrides)
    await state.create_schedule(schedule)


@pytest.mark.asyncio
async def test_metric_value_github_poll_healthy_age_minutes_small_age_after_recent_stamp():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    now = 1_000_000.0
    await _make_github_schedule(state, "sched-gh-1")
    await state.update_schedule("sched-gh-1", last_healthy_poll_at=now - 60)  # 1 minute ago

    with patch("lionagi.state.db.time.time", return_value=now):
        age = await state.metric_value("github_poll_healthy_age_minutes", window_start=0.0)
    assert age == 1.0

    await state.close()


@pytest.mark.asyncio
async def test_metric_value_github_poll_healthy_age_minutes_large_after_stale_stamp():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    now = 1_000_000.0
    await _make_github_schedule(state, "sched-gh-1")
    # Stamped healthy 2 hours ago -- e.g. an auth_error poll never moved it since.
    await state.update_schedule("sched-gh-1", last_healthy_poll_at=now - 7200)

    with patch("lionagi.state.db.time.time", return_value=now):
        age = await state.metric_value("github_poll_healthy_age_minutes", window_start=0.0)
    assert age == 120.0

    await state.close()


@pytest.mark.asyncio
async def test_metric_value_github_poll_healthy_age_minutes_sentinel_when_no_github_schedule():
    """No github_poll schedule exists at all -- must not report a large,
    alarm-triggering age; there is nothing to be blind about."""
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    age = await state.metric_value("github_poll_healthy_age_minutes", window_start=0.0)
    assert age == 0.0

    await state.close()


@pytest.mark.asyncio
async def test_metric_value_github_poll_healthy_age_minutes_sentinel_when_never_polled():
    """A github_poll schedule exists but has never recorded a healthy poll
    (last_healthy_poll_at still NULL) -- same no-alarm sentinel as no
    schedule at all, not a crash on the NULL."""
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await _make_github_schedule(state, "sched-gh-1")

    age = await state.metric_value("github_poll_healthy_age_minutes", window_start=0.0)
    assert age == 0.0

    await state.close()


@pytest.mark.asyncio
async def test_metric_value_github_poll_healthy_age_minutes_ignores_disabled_schedules():
    """A disabled github_poll schedule's stamp doesn't count -- only enabled
    schedules are actually being observed."""
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    import time

    now = time.time()
    await _make_github_schedule(state, "sched-gh-1", enabled=0)
    await state.update_schedule("sched-gh-1", last_healthy_poll_at=now - 60)

    age = await state.metric_value("github_poll_healthy_age_minutes", window_start=0.0)
    assert age == 0.0  # disabled schedule's healthy stamp is invisible to the metric

    await state.close()


@pytest.mark.asyncio
async def test_metric_value_github_poll_consecutive_401_counts_and_resets():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()

    await _make_github_schedule(state, "sched-gh-1")

    # No 401s yet -- defaults to 0.
    count = await state.metric_value("github_poll_consecutive_401", window_start=0.0)
    assert count == 0.0

    await state.update_schedule("sched-gh-1", poller_consecutive_401=3)
    count = await state.metric_value("github_poll_consecutive_401", window_start=0.0)
    assert count == 3.0

    # A subsequent healthy poll resets it (mirrors the engine's stamp logic).
    await state.update_schedule("sched-gh-1", poller_consecutive_401=0)
    count = await state.metric_value("github_poll_consecutive_401", window_start=0.0)
    assert count == 0.0

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
