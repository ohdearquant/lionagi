# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Capacity fairness, episode logging, and live slot-holder visibility."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

import lionagi.studio.config as config
from lionagi.state.db import NO_CURSOR_CLAIM
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


def _poll_row(**overrides):
    return _minimal_schedule(
        id="poll",
        trigger_type="github_poll",
        github_repo="acme/widgets",
        last_fired_at=0,
        **overrides,
    )


def _holding_fires(engine, monkeypatch):
    """Record which schedule each fire is for, and keep every slot it was given."""
    took, held = [], []

    def launch(schedule, run_id, *, trigger_context, global_slot_claim, **kwargs):
        took.append(schedule["id"])
        held.append(global_slot_claim)
        schedule["next_fire_at"] = trigger_context["fired_at"] + 30

    async def fire(schedule, run_id, *, global_slot_claim, **kwargs):
        took.append(schedule["id"])
        held.append(global_slot_claim)
        return True

    monkeypatch.setattr(engine, "_tracked_fire", launch)
    monkeypatch.setattr(engine, "_fire", fire)
    return took, held


async def _hold_a_batch(engine, poll_row, items=None):
    """Poll once on a full cap, so what the poll finds is held for its next turn."""
    _, blocker = await engine._reserve_global_slot("blocker", path="manual")

    async def poll(*args, **kwargs):
        return GithubPollResult(
            items=items or [_item(1, "2026-01-01T00:00:00Z")], scan_complete=True
        )

    with patch("lionagi.studio.scheduler.github.github_poll", new=poll):
        await engine._tick_github(poll_row, now=10_000.0)
    blocker.release()
    return engine._github_batches["poll"]


@pytest.mark.asyncio
async def test_a_poll_on_a_cap_a_timed_row_keeps_full_still_gets_a_slot(monkeypatch):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    _quiet_tick(engine)
    timed = _minimal_schedule(id="timed", trigger_type="interval", next_fire_at=10_000.0)
    svc.list_schedules.return_value = [timed, _poll_row()]
    took, held = _holding_fires(engine, monkeypatch)
    fetch = asyncio.Event()

    async def poll(*args, **kwargs):
        await fetch.wait()
        return GithubPollResult(items=[_item(1, "2026-01-01T00:00:00Z")], scan_complete=True)

    monkeypatch.setattr("lionagi.studio.scheduler.github.github_poll", poll)
    with patch("lionagi.studio.scheduler.engine.time.time", return_value=10_000.0):
        await engine._tick()
    # The timed row took the free slot while the poll was still fetching, holding none.
    assert took == ["timed"]
    fetch.set()
    await asyncio.wait_for(asyncio.gather(*engine._github_tick_tasks.values()), timeout=2)
    assert engine._github_batches["poll"].due_at == 300
    held.pop().release()

    # The timed row is due again, but the event the poll found came due first.
    with patch("lionagi.studio.scheduler.engine.time.time", return_value=10_030.0):
        await engine._tick()
    await asyncio.wait_for(asyncio.gather(*engine._github_tick_tasks.values()), timeout=2)
    assert took == ["timed", "poll"]
    assert "poll" not in engine._github_batches
    assert engine._deferred_log_counts == {"timed": 1}
    held.pop().release()
    assert engine._global_inflight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("timed_due", "first"), [(1_000.0, "poll"), (200.0, "timed")])
async def test_a_held_github_event_takes_the_next_slot_in_oldest_due_order(
    monkeypatch, timed_due, first
):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    _quiet_tick(engine)
    poll_row = _poll_row()
    svc.list_schedules.return_value = [
        _minimal_schedule(id="timed", next_fire_at=timed_due),
        poll_row,
    ]
    took, held = _holding_fires(engine, monkeypatch)
    batch = await _hold_a_batch(engine, poll_row)
    assert batch.due_at == 300

    with patch("lionagi.studio.scheduler.engine.time.time", return_value=10_000.0):
        await engine._tick()
    await asyncio.wait_for(asyncio.gather(*engine._github_tick_tasks.values()), timeout=2)

    assert took == [first]
    if first == "poll":
        assert "poll" not in engine._github_batches
        assert engine._deferred_log_counts == {"timed": 1}
    else:
        # Still held, at the same due instant, and its deferral episode goes on.
        assert engine._github_batches["poll"] is batch
        assert batch.slot_claim is None
        assert engine._deferred_log_counts == {"poll": 2}
    held.pop().release()
    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_a_held_batch_the_schedule_moved_past_is_dropped_and_polled_afresh_on_a_full_cap(
    monkeypatch,
):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    _quiet_tick(engine)
    poll_row = _poll_row()
    svc.list_schedules.return_value = [poll_row]
    took, held = _holding_fires(engine, monkeypatch)
    await _hold_a_batch(engine, poll_row)
    poll_row["github_cursor"] = "moved-by-an-operator"
    # The cap is full, so a batch kept past the move would wait for a slot instead of polling.
    _, blocker = await engine._reserve_global_slot("blocker", path="manual")
    inflight_during_poll = []

    async def poll(*args, **kwargs):
        inflight_during_poll.append(engine._global_inflight)
        return GithubPollResult(items=[], scan_complete=True)

    monkeypatch.setattr("lionagi.studio.scheduler.github.github_poll", poll)
    with patch("lionagi.studio.scheduler.engine.time.time", return_value=10_000.0):
        await engine._tick()
    await asyncio.wait_for(asyncio.gather(*engine._github_tick_tasks.values()), timeout=2)

    # The poll ran, holding no slot of its own: the one taken is the blocker's.
    assert inflight_during_poll == [1]
    assert took == []
    assert "poll" not in engine._github_batches
    blocker.release()
    assert engine._global_inflight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fired_first", [False, True])
async def test_a_held_batch_claims_its_poll_cycle_only_if_no_event_of_it_has(
    monkeypatch, fired_first
):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    engine = SchedulerEngine(svc=_make_svc())
    poll_row = _poll_row(next_fire_at=10_300.0)
    first, second = _item(1, "2026-01-01T00:00:00Z"), _item(2, "2026-01-01T00:01:00Z")
    claims, held = [], []

    async def fire(schedule, run_id, *, global_slot_claim, **kwargs):
        claims.append((kwargs["expect_next_fire_at"], kwargs["expect_github_cursor"]))
        held.append(global_slot_claim)
        return True

    monkeypatch.setattr(engine, "_fire", fire)
    if fired_first:
        # The first event takes the only slot and keeps it, so the second is held back.
        async def poll(*args, **kwargs):
            return GithubPollResult(items=[first, second], scan_complete=True)

        with patch("lionagi.studio.scheduler.github.github_poll", new=poll):
            await engine._tick_github(poll_row, now=10_000.0)
        held.pop().release()
        # The first event's fire advanced the row's cursor, as the store does.
        poll_row["github_cursor"] = first.cursor
    else:
        await _hold_a_batch(engine, poll_row, items=[first, second])
    claims.clear()
    batch = engine._github_batches["poll"]
    assert batch.cycle_claimed is fired_first

    await engine._tick_github(poll_row, now=10_030.0)

    expected_next = NO_CURSOR_CLAIM if fired_first else 10_300.0
    assert claims[0] == (expected_next, poll_row.get("github_cursor"))
    for claim in held:
        claim.release()
    assert engine._global_inflight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["stop", "schedule_gone"])
async def test_a_slot_reserved_for_a_held_batch_is_released_when_the_batch_is(monkeypatch, how):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    _quiet_tick(engine)
    poll_row = _poll_row()
    await _hold_a_batch(engine, poll_row)

    # The turn is admitted but never runs, so the reserved slot stays on the batch.
    def no_turn(schedule, now):
        return None

    monkeypatch.setattr(engine, "_start_github_tick", no_turn)
    await engine._admit_github_tick(poll_row, 10_000.0)
    assert engine._github_batches["poll"].slot_claim is not None
    assert engine._global_inflight == 1

    if how == "stop":
        await engine.stop()
    else:
        svc.list_schedules.return_value = []
        with patch("lionagi.studio.scheduler.engine.time.time", return_value=10_000.0):
            await engine._tick()

    assert engine._github_batches == {}
    assert engine._global_inflight == 0


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
        if outcome in (
            "deferred",
            "event_deferred",
            "fired_then_deferred",
            "refused_then_deferred",
        ):
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
        answer = next(script)
        if isinstance(answer, Exception):
            raise answer
        return answer, None

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
@pytest.mark.parametrize("gate", ["overlap", "raised"])
async def test_a_due_pass_the_rate_limit_did_not_refuse_ends_the_episode(monkeypatch, caplog, gate):
    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(
        id="limited", name="limited schedule", next_fire_at=100.0, overlap_policy="skip"
    )
    answers = (
        [False, RuntimeError("store unavailable"), False] if gate == "raised" else [False, False]
    )
    monkeypatch.setattr(engine, "_reserve_rate_limit", _scripted_rate_limit(answers))
    with caplog.at_level(logging.INFO, logger="lionagi.studio.scheduler.engine"):
        await engine._maybe_fire(schedule, now=100.0)
        if gate == "overlap":
            # A prior run still active ends the pass at the overlap gate, before the rate limit.
            engine._running["limited"] = "run-prior"
            await engine._maybe_fire(schedule, now=130.0)
            del engine._running["limited"]
        else:
            with pytest.raises(RuntimeError, match="store unavailable"):
                await engine._maybe_fire(schedule, now=130.0)
        await engine._maybe_fire(schedule, now=160.0)
    lines = _rate_limit_lines(caplog)
    assert len(lines) == 2, lines


@pytest.mark.asyncio
async def test_a_restarted_engine_logs_an_open_rate_limit_episode_again(monkeypatch, caplog):
    from tests.studio.test_scheduler_engine import _supervised_engine

    engine = await _supervised_engine(monkeypatch, [])
    schedule = _minimal_schedule(id="limited", name="limited schedule", next_fire_at=100.0)
    monkeypatch.setattr(engine, "_reserve_rate_limit", _scripted_rate_limit([False, False]))
    with caplog.at_level(logging.INFO, logger="lionagi.studio.scheduler.engine"):
        await engine._maybe_fire(schedule, now=100.0)
        await engine.start()
        await engine.stop()
        await engine._maybe_fire(schedule, now=130.0)
    lines = _rate_limit_lines(caplog)
    assert len(lines) == 2, lines


@pytest.mark.asyncio
async def test_a_github_rate_limit_episode_logs_once_until_a_pass_is_not_refused(
    monkeypatch, caplog
):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(
        id="poller", name="poller schedule", trigger_type="github_poll", last_fired_at=0
    )
    monkeypatch.setattr(
        engine, "_reserve_rate_limit", _scripted_rate_limit([False, False, True, False])
    )
    _, blocker = await engine._reserve_global_slot("blocker", path="manual")
    try:
        with caplog.at_level(logging.INFO, logger="lionagi.studio.scheduler.engine"):
            await engine._tick_github(schedule, now=10_000.0)
            await engine._tick_github(schedule, now=20_000.0)
            assert len(_rate_limit_lines(caplog)) == 1
            # The reservation passes and the full cap defers the pass before any poll.
            await engine._tick_github(schedule, now=30_000.0)
            await engine._tick_github(schedule, now=40_000.0)
    finally:
        blocker.release()
    lines = _rate_limit_lines(caplog)
    assert len(lines) == 2, lines
    assert all("poller schedule" in line and "github events deferred" in line for line in lines)


@pytest.mark.asyncio
async def test_a_rate_limit_that_stops_a_github_batch_opens_the_episode(monkeypatch, caplog):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 4)
    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(
        id="poller", name="poller schedule", trigger_type="github_poll", last_fired_at=0
    )
    # Poll 1: room before polling and for PR 1, none for PR 2. Poll 2: refused before polling.
    # Poll 3: room, and the poll finds nothing. Poll 4: refused before polling.
    monkeypatch.setattr(
        engine, "_reserve_rate_limit", _scripted_rate_limit([True, True, False, False, True, False])
    )
    batches = [[_item(1, "2026-01-01T00:00:00Z"), _item(2, "2026-01-01T00:01:00Z")], []]
    held = []

    async def poll(*args, **kwargs):
        return GithubPollResult(items=batches.pop(0), scan_complete=True)

    async def fire(schedule, run_id, *, global_slot_claim, **kwargs):
        held.append(global_slot_claim)
        return True

    monkeypatch.setattr("lionagi.studio.scheduler.github.github_poll", poll)
    monkeypatch.setattr(engine, "_fire", fire)
    try:
        with caplog.at_level(logging.INFO, logger="lionagi.studio.scheduler.engine"):
            for now in (10_000.0, 20_000.0, 30_000.0, 40_000.0):
                await engine._tick_github(schedule, now=now)
    finally:
        for claim in held:
            claim.release()
    assert len(held) == 1
    lines = _rate_limit_lines(caplog)
    assert len(lines) == 2, lines
    assert "not dispatched this poll" in lines[0] and "[2]" in lines[0], lines[0]
    assert "github events deferred without polling" in lines[1], lines[1]
