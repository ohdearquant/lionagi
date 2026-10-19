# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Capacity fairness, episode logging, and live slot-holder visibility."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

import lionagi.studio.config as config
from lionagi.studio.scheduler.engine import _MAX_PREDISPATCH_REFUSALS, SchedulerEngine
from lionagi.studio.scheduler.github import GithubPollResult
from tests.studio.test_scheduler_github_fire_per_event import _item
from tests.studio.test_scheduler_global_slot import _make_svc, _minimal_schedule


def _quiet_tick(engine):
    engine._last_reaper_run = 10_000
    engine._last_checkpoint_run = 10_000
    engine._maybe_start_prune = lambda now: None
    engine._maybe_start_worker_pass = lambda now: None
    engine._deliver_due_dispatches = AsyncMock()


@pytest.mark.asyncio
async def test_oldest_due_row_gets_next_slot_ahead_of_fast_interval(monkeypatch):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    rows = [
        _minimal_schedule(id="fast", trigger_type="interval", next_fire_at=100.0),
        _minimal_schedule(id="early", next_fire_at=90.0),
        _minimal_schedule(id="late", next_fire_at=95.0),
    ]
    svc = _make_svc()
    svc.list_schedules.return_value = rows
    engine = SchedulerEngine(svc=svc)
    _quiet_tick(engine)
    fired, held = [], []

    def launch(schedule, run_id, *, trigger_context, global_slot_claim, **kwargs):
        now = trigger_context["fired_at"]
        fired.append(schedule["id"])
        held.append(global_slot_claim)
        schedule["next_fire_at"] = now + (30 if schedule["id"] == "fast" else 10_000)

    monkeypatch.setattr(engine, "_tracked_fire", launch)
    for now in (100.0, 130.0, 160.0):
        with patch("lionagi.studio.scheduler.engine.time.time", return_value=now):
            await engine._tick()
        assert len(held) == 1
        held.pop().release()
    assert fired == ["early", "late", "fast"]
    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_due_timed_admission_precedes_starting_github_pass(monkeypatch):
    engine = SchedulerEngine(svc=_make_svc())
    engine._svc.list_schedules.return_value = [
        _minimal_schedule(id="poll", trigger_type="github_poll", next_fire_at=None),
        _minimal_schedule(id="timed", next_fire_at=99.0),
    ]
    _quiet_tick(engine)
    calls = []

    async def timed(schedule, now):
        calls.append(schedule["id"])

    monkeypatch.setattr(engine, "_maybe_fire", timed)
    monkeypatch.setattr(
        engine, "_start_github_tick", lambda schedule, now: calls.append(schedule["id"])
    )
    with patch("lionagi.studio.scheduler.engine.time.time", return_value=100.0):
        await engine._tick()
    assert calls == ["timed", "poll"]


@pytest.mark.asyncio
async def test_capacity_episode_logs_once_and_records_first_and_eleventh(monkeypatch, caplog):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(id="waiting", name="waiting schedule", next_fire_at=100.0)
    _, blocker = await engine._reserve_global_slot("blocker", path="manual")
    launches = []
    monkeypatch.setattr(engine, "_tracked_fire", lambda schedule, run_id, **kw: launches.append(kw))
    with caplog.at_level(logging.INFO, logger="lionagi.studio.scheduler.engine"):
        for now in range(100, 110):
            await engine._maybe_fire(schedule, now=float(now))
        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len(messages) == 1
        assert "waiting schedule" in messages[0] and "waiting" in messages[0]
        assert "inflight=1" in messages[0] and "cap=1" in messages[0]
        assert svc.create_schedule_run.await_count == 1
        await engine._maybe_fire(schedule, now=110.0)
        assert svc.create_schedule_run.await_count == 2
        assert len(caplog.records) == 1
        blocker.release()
        await engine._maybe_fire(schedule, now=111.0)
        assert len(launches) == 1
        launches[0]["global_slot_claim"].release()
        _, blocker = await engine._reserve_global_slot("blocker", path="manual")
        try:
            await engine._maybe_fire(schedule, now=112.0)
        finally:
            blocker.release()
    assert len(caplog.records) == 2
    assert svc.create_schedule_run.await_count == 3
    transitions = [
        c
        for c in svc.update_status.await_args_list
        if c.kwargs.get("reason_code") == "schedule.deferred.capacity"
    ]
    assert [c.kwargs["metadata"]["deferral_count"] for c in transitions] == [1, 11, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, 2])
@pytest.mark.parametrize("path", ["scheduled", "missed_fire", "manual", "github_event"])
async def test_reserving_a_slot_leaves_every_deferral_count_alone(monkeypatch, cap, path):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", cap)
    engine = SchedulerEngine(svc=_make_svc())
    engine._deferred_log_counts = {"target": 4, "other": 3}
    allowed, claim = await engine._reserve_global_slot("target", path=path)
    assert allowed
    assert engine._deferred_log_counts == {"target": 4, "other": 3}
    if claim is not None:
        claim.release()
    assert engine._global_inflight == 0


def _deferral_logs(caplog):
    return [r for r in caplog.records if "deferred: global concurrent-fire cap" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_manual_trigger_does_not_end_a_deferral_episode(monkeypatch, caplog):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(id="waiting", next_fire_at=100.0)
    svc.get_schedule.return_value = schedule
    held = []
    monkeypatch.setattr(
        engine, "_tracked_fire", lambda schedule, run_id, **kw: held.append(kw["global_slot_claim"])
    )
    _, blocker = await engine._reserve_global_slot("blocker", path="manual")
    with caplog.at_level(logging.INFO, logger="lionagi.studio.scheduler.engine"):
        await engine._maybe_fire(schedule, now=100.0)
        blocker.release()
        # The manual run takes the freed slot, so the still-due occurrence is deferred again.
        assert await engine.fire_now("waiting") is not None
        await engine._maybe_fire(schedule, now=101.0)
    held.pop().release()
    assert engine._deferred_log_counts == {"waiting": 2}
    assert len(_deferral_logs(caplog)) == 1
    assert svc.create_schedule_run.await_count == 1
    assert engine._global_inflight == 0


_DUE_OUTCOMES = [
    "fired",
    "overlap_skip",
    "threshold_not_breached",
    "budget_exhausted",
    "rate_limited",
    "max_runs_reached",
    "raised",
    "deferred",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", _DUE_OUTCOMES)
async def test_a_due_evaluation_ends_the_episode_unless_the_cap_defers_it(monkeypatch, outcome):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(id="target", next_fire_at=100.0)
    engine._deferred_log_counts = {"target": 2, "other": 3}
    held = []
    monkeypatch.setattr(
        engine, "_tracked_fire", lambda schedule, run_id, **kw: held.append(kw["global_slot_claim"])
    )
    if outcome == "overlap_skip":
        engine._running["target"] = "earlier-run"
    elif outcome == "threshold_not_breached":
        schedule["threshold_config"] = {"metric": "failure_rate"}
        monkeypatch.setattr(engine, "_evaluate_threshold_breach", AsyncMock(return_value=None))
        monkeypatch.setattr(engine, "_record_evaluation_without_firing", AsyncMock())
    elif outcome == "budget_exhausted":
        monkeypatch.setattr(engine, "_check_budget", AsyncMock(return_value=True))
        monkeypatch.setattr(engine, "_disable_for_budget_exhausted", AsyncMock())
    elif outcome == "rate_limited":
        monkeypatch.setattr(engine, "_reserve_rate_limit", AsyncMock(return_value=(False, None)))
    elif outcome == "max_runs_reached":
        monkeypatch.setattr(
            engine, "_reserve_max_runs_budget", AsyncMock(return_value=(False, None))
        )
    elif outcome == "raised":
        monkeypatch.setattr(
            engine, "_check_budget", AsyncMock(side_effect=RuntimeError("store unavailable"))
        )
    blocker = None
    if outcome == "deferred":
        _, blocker = await engine._reserve_global_slot("blocker", path="manual")
    try:
        if outcome == "raised":
            with pytest.raises(RuntimeError, match="store unavailable"):
                await engine._maybe_fire(schedule, now=100.0)
        else:
            await engine._maybe_fire(schedule, now=100.0)
    finally:
        if blocker is not None:
            blocker.release()
        for claim in held:
            claim.release()
    if outcome == "deferred":
        assert engine._deferred_log_counts == {"target": 3, "other": 3}
    else:
        assert engine._deferred_log_counts == {"other": 3}
    assert len(held) == (1 if outcome == "fired" else 0)
    assert engine._global_inflight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "not_due",
        "deferred",
        "empty_poll",
        "poll_raised",
        "event_deferred",
        "fired_then_deferred",
        "refused_then_deferred",
    ],
)
async def test_a_github_due_pass_ends_the_episode_unless_the_cap_defers_it(monkeypatch, outcome):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(
        id="poller",
        trigger_type="github_poll",
        github_repo="acme/widgets",
        last_fired_at=10_000.0 if outcome == "not_due" else 0,
    )
    if outcome == "refused_then_deferred":
        # The first event's next refusal before dispatch is its last, so the pass moves past it.
        schedule["predispatch_refusal_event"] = _item(1, "2026-01-01T00:00:00Z").cursor
        schedule["predispatch_refusal_count"] = _MAX_PREDISPATCH_REFUSALS - 1
    engine._deferred_log_counts = {"poller": 2, "other": 3}
    held, fired = [], []

    async def poll(*args, **kwargs):
        if outcome == "poll_raised":
            raise RuntimeError("poll failed")
        if outcome == "event_deferred":
            # The cap fills while the poll fetches, after the pre-poll read found room.
            held.append((await engine._reserve_global_slot("blocker", path="manual"))[1])
        if outcome in ("event_deferred", "fired_then_deferred", "refused_then_deferred"):
            return GithubPollResult(
                items=[_item(1, "2026-01-01T00:00:00Z"), _item(2, "2026-01-01T00:01:00Z")],
                scan_complete=True,
            )
        return GithubPollResult(items=[], scan_complete=True)

    async def fire(schedule, run_id, *, global_slot_claim, **kwargs):
        # The first event keeps its slot, so the second finds the cap full.
        fired.append(run_id)
        held.append(global_slot_claim)
        return outcome != "refused_then_deferred"

    monkeypatch.setattr("lionagi.studio.scheduler.github.github_poll", poll)
    monkeypatch.setattr(engine, "_fire", fire)
    if outcome == "deferred":
        held.append((await engine._reserve_global_slot("blocker", path="manual"))[1])
    try:
        if outcome == "poll_raised":
            with pytest.raises(RuntimeError, match="poll failed"):
                await engine._tick_github(schedule, now=10_000.0)
        else:
            await engine._tick_github(schedule, now=10_000.0)
    finally:
        for claim in held:
            claim.release()
    expected = {
        "not_due": {"poller": 2, "other": 3},
        "deferred": {"poller": 3, "other": 3},
        "event_deferred": {"poller": 3, "other": 3},
    }.get(outcome, {"other": 3})
    assert engine._deferred_log_counts == expected
    assert len(fired) == (1 if outcome in ("fired_then_deferred", "refused_then_deferred") else 0)
    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_a_next_fire_at_that_is_not_a_number_fails_only_its_own_row(monkeypatch, caplog):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 2)
    svc = _make_svc()
    svc.list_schedules.return_value = [
        _minimal_schedule(id="bad", name="bad row", next_fire_at="not-a-time"),
        _minimal_schedule(id="unset", name="unset row", next_fire_at=None),
        _minimal_schedule(id="good", name="good row", next_fire_at=90.0),
    ]
    engine = SchedulerEngine(svc=svc)
    _quiet_tick(engine)
    fired = []

    def launch(schedule, run_id, *, global_slot_claim, **kwargs):
        fired.append(schedule["id"])
        global_slot_claim.release()

    monkeypatch.setattr(engine, "_tracked_fire", launch)
    with (
        caplog.at_level(logging.ERROR, logger="lionagi.studio.scheduler.engine"),
        patch("lionagi.studio.scheduler.engine.time.time", return_value=100.0),
    ):
        await engine._tick()
    assert fired == ["good"]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == ["Error evaluating schedule bad row"]


@pytest.mark.asyncio
async def test_slot_holder_snapshot_tracks_exact_claim_and_is_not_mutable(monkeypatch):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 2)
    engine = SchedulerEngine(svc=_make_svc())
    with patch("lionagi.studio.scheduler.engine.time.time", return_value=123.0):
        _, first = await engine._reserve_global_slot("same", path="manual")
        _, second = await engine._reserve_global_slot("same", path="scheduled")
    snapshot = engine.global_slot_holders
    assert snapshot == [
        {"schedule_id": "same", "path": "manual", "acquired_at": 123.0},
        {"schedule_id": "same", "path": "scheduled", "acquired_at": 123.0},
    ]
    snapshot[0]["schedule_id"] = "changed"
    assert engine.global_slot_holders[0]["schedule_id"] == "same"
    first.release()
    first.release()
    assert engine._global_inflight == 1
    assert engine.global_slot_holders == [
        {"schedule_id": "same", "path": "scheduled", "acquired_at": 123.0}
    ]
    second.release()
    assert engine.global_slot_holders == []


@pytest.mark.asyncio
async def test_a_github_poll_holds_no_slot_and_each_event_holds_its_own(monkeypatch):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 2)
    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(
        id="poller", trigger_type="github_poll", github_repo="acme/widgets", last_fired_at=0
    )
    started, resume = asyncio.Event(), asyncio.Event()
    event_holder = []

    async def poll(*args, **kwargs):
        started.set()
        await resume.wait()
        return GithubPollResult(items=[_item(1, "2026-01-01T00:00:00Z")], scan_complete=True)

    async def fire(schedule, run_id, *, global_slot_claim, **kwargs):
        event_holder.extend(engine.global_slot_holders)
        global_slot_claim.release()
        return True

    monkeypatch.setattr("lionagi.studio.scheduler.github.github_poll", poll)
    monkeypatch.setattr(engine, "_fire", fire)
    task = asyncio.create_task(engine._tick_github(schedule, now=10_000.0))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        # The poll is a trigger check: nothing is held while it fetches.
        assert engine.global_slot_holders == []
        assert engine._global_inflight == 0
        resume.set()
        await asyncio.wait_for(task, timeout=2)
        assert [(h["schedule_id"], h["path"]) for h in event_holder] == [("poller", "github_event")]
        assert engine.global_slot_holders == []
        assert engine._global_inflight == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _rate_limit_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "rolling rate limit" in r.getMessage()]


def _scripted_rate_limit(answers):
    script = iter(answers)

    async def reserve(schedule, *, now):
        return next(script), None

    return reserve


@pytest.mark.asyncio
async def test_a_rate_limit_episode_logs_once_and_a_passing_reservation_ends_it(
    monkeypatch, caplog
):
    engine = SchedulerEngine(svc=_make_svc())
    limited = _minimal_schedule(id="limited", name="limited schedule", next_fire_at=100.0)
    other = _minimal_schedule(id="other", name="other schedule", next_fire_at=100.0)
    monkeypatch.setattr(
        engine, "_reserve_rate_limit", _scripted_rate_limit([False, False, False, True, False])
    )
    launches = []
    monkeypatch.setattr(engine, "_tracked_fire", lambda schedule, run_id, **kw: launches.append(kw))
    with caplog.at_level(logging.INFO, logger="lionagi.studio.scheduler.engine"):
        await engine._maybe_fire(limited, now=100.0)
        await engine._maybe_fire(other, now=100.0)
        await engine._maybe_fire(limited, now=130.0)
        # Three refusals across two schedules: one line each, however often the tick re-checks.
        assert len(_rate_limit_lines(caplog)) == 2
        await engine._maybe_fire(limited, now=160.0)
        assert len(launches) == 1
        launches[0]["global_slot_claim"].release()
        await engine._maybe_fire(limited, now=190.0)
    lines = _rate_limit_lines(caplog)
    assert len(lines) == 3, lines
    assert ["limited schedule" in line for line in lines] == [True, False, True]
    assert all("deferring without disabling or advancing next_fire_at" in line for line in lines)


@pytest.mark.asyncio
async def test_a_github_rate_limit_episode_logs_once_until_a_reservation_passes(
    monkeypatch, caplog
):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(id="poller", name="poller schedule", trigger_type="github_poll")
    monkeypatch.setattr(
        engine, "_reserve_rate_limit", _scripted_rate_limit([False, False, True, False])
    )
    _, blocker = await engine._reserve_global_slot("blocker", path="manual")
    try:
        with caplog.at_level(logging.INFO, logger="lionagi.studio.scheduler.engine"):
            assert await engine._github_due_pass(schedule, 100.0) is False
            assert await engine._github_due_pass(schedule, 130.0) is False
            assert len(_rate_limit_lines(caplog)) == 1
            # The reservation passes and the full cap defers the pass before any poll.
            assert await engine._github_due_pass(schedule, 160.0) is True
            assert await engine._github_due_pass(schedule, 190.0) is False
    finally:
        blocker.release()
    lines = _rate_limit_lines(caplog)
    assert len(lines) == 2, lines
    assert all("poller schedule" in line and "github events deferred" in line for line in lines)
