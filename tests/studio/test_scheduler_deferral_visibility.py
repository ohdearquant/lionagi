# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Capacity fairness, episode logging, and live slot-holder visibility."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

import lionagi.studio.config as config
from lionagi.studio.scheduler.engine import SchedulerEngine
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
async def test_successful_admission_ends_only_its_schedule_episode(monkeypatch, cap, path):
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", cap)
    engine = SchedulerEngine(svc=_make_svc())
    engine._deferred_log_counts = {"target": 4, "other": 3}
    allowed, claim = await engine._reserve_global_slot("target", path=path)
    assert allowed
    assert engine._deferred_log_counts == {"other": 3}
    if claim is not None:
        claim.release()
    assert engine._global_inflight == 0


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
