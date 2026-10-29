# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A GitHub poll cannot act on a target that changed while it was in flight."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from lionagi.studio.scheduler import github as github_module
from lionagi.studio.scheduler import subprocess as subprocess_module
from lionagi.studio.scheduler.engine import SchedulerEngine
from lionagi.studio.services import schedules
from tests.studio.test_github_poll_stagnation import (
    _NOW,
    _pr,
    _reap,
    _Response,
    _row,
    _schedule,
    _Service,
)

pytest_plugins = ("tests.studio.test_github_poll_stagnation",)


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered", [False, True], ids=["dispatchable", "filtered"])
async def test_old_poll_cannot_dispatch_or_advance_a_retargeted_schedule(
    stores, client, process, monkeypatch, filtered
):
    db, editor, reader = stores
    initial = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    schedule = await _schedule(
        db, streak=0, github_cursor=initial, github_filter={"event": "pr_merged", "draft": False}
    )
    client.response = _Response([_pr(1, draft=filtered)])
    entered, release = asyncio.Event(), asyncio.Event()
    started = []
    spawn = subprocess_module.asyncio.create_subprocess_exec

    async def capture_spawn(*args, **kwargs):
        started.append(args)
        return await spawn(*args, **kwargs)

    async def pause_poll():
        client.before_get = None
        entered.set()
        await release.wait()

    @asynccontextmanager
    async def edit_context():
        yield editor

    monkeypatch.setattr(subprocess_module.asyncio, "create_subprocess_exec", capture_spawn)
    monkeypatch.setattr(schedules, "StateDB", edit_context)
    client.before_get = pause_poll
    engine = SchedulerEngine(svc=_Service(db))
    task = asyncio.create_task(engine._tick_github(schedule, _NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await schedules.update_schedule("poll", {"github_repo": "other/repo"})
        retargeted = await _row(reader)
        assert retargeted["github_cursor"] == initial
        assert retargeted["poller_target_generation"] == 1
        release.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        await _reap([task])
    row = await _row(reader)
    assert row["github_repo"] == "other/repo"
    assert row["github_cursor"] == initial
    # HTTP liveness retains its existing meaning even when the target moved.
    assert row["last_healthy_poll_at"] == _NOW
    assert row["poller_consecutive_401"] == 0
    assert started == []
    assert await reader.list_schedule_runs("poll") == []
    assert "poll" not in engine._github_batches
    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_held_old_batch_is_discarded_after_target_changes_away_and_back(
    stores, client, process, monkeypatch
):
    import lionagi.studio.config as config

    db, editor, reader = stores
    initial = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    await _schedule(db, streak=0, github_cursor=initial)
    client.response = _Response([_pr(1)])
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    started = []
    spawn = subprocess_module.asyncio.create_subprocess_exec

    async def capture_spawn(*args, **kwargs):
        started.append(args)
        return await spawn(*args, **kwargs)

    @asynccontextmanager
    async def edit_context():
        yield editor

    monkeypatch.setattr(subprocess_module.asyncio, "create_subprocess_exec", capture_spawn)
    monkeypatch.setattr(schedules, "StateDB", edit_context)
    engine = SchedulerEngine(svc=_Service(db))
    allowed, holder = await engine._reserve_global_slot("other", path="scheduled")
    assert allowed
    try:
        await engine._tick_github(await _row(db), _NOW)
        assert "poll" in engine._github_batches
        assert client.calls == 1
        assert await schedules.update_schedule("poll", {"github_repo": "other/repo"})
        assert await schedules.update_schedule("poll", {"github_repo": "owner/repo"})
        row = await _row(reader)
        assert row["poller_target_generation"] == 2
        assert row["github_cursor"] == initial
    finally:
        holder.release()

    # A fresh complete empty read distinguishes a discarded batch from replaying its old PR.
    client.response = _Response([])
    await engine._tick_github(await _row(db), _NOW + 2)
    row = await _row(reader)
    assert client.calls == 2
    assert row["github_cursor"] == initial
    assert started == []
    runs = await reader.list_schedule_runs("poll")
    assert len(runs) == 1
    assert runs[0]["status"] == "skipped"
    assert runs[0]["invocation_id"] is None
    assert runs[0]["action_args"] == []
    assert runs[0]["trigger_context"] == {"deferred_capacity": True, "fired_at": _NOW}
    assert "poll" not in engine._github_batches
    assert engine._global_inflight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered", [False, True], ids=["dispatchable", "filtered"])
async def test_unchanged_target_still_processes_poll(stores, client, process, filtered):
    db, _, reader = stores
    initial = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    schedule = await _schedule(
        db, streak=0, github_cursor=initial, github_filter={"event": "pr_merged", "draft": False}
    )
    client.response = _Response([_pr(1, draft=filtered)])
    engine = SchedulerEngine(svc=_Service(db))
    await engine._tick_github(schedule, _NOW)
    row = await _row(reader)
    assert row["poller_target_generation"] == 0
    assert row["github_cursor"] == github_module._cursor_for("2026-10-24T10:05:00Z", 1)
    runs = await reader.list_schedule_runs("poll")
    assert len(runs) == (0 if filtered else 1)
    assert engine._global_inflight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered", [False, True], ids=["occurrence", "cursor"])
async def test_retarget_after_poll_before_atomic_write_is_rejected(
    stores, client, process, monkeypatch, filtered
):
    db, editor, reader = stores
    initial = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    schedule = await _schedule(
        db, streak=0, github_cursor=initial, github_filter={"event": "pr_merged", "draft": False}
    )
    client.response = _Response([_pr(1, draft=filtered)])
    service = _Service(db)
    entered, release = asyncio.Event(), asyncio.Event()
    original_occurrence = service.create_schedule_run_and_advance
    original_update = service.update_schedule

    async def pause_occurrence(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_occurrence(*args, **kwargs)

    async def pause_cursor(*args, **kwargs):
        if kwargs.get("guard_cursor_forward") and "github_cursor" in kwargs:
            entered.set()
            await release.wait()
        return await original_update(*args, **kwargs)

    @asynccontextmanager
    async def edit_context():
        yield editor

    monkeypatch.setattr(schedules, "StateDB", edit_context)
    if filtered:
        monkeypatch.setattr(service, "update_schedule", pause_cursor)
    else:
        monkeypatch.setattr(service, "create_schedule_run_and_advance", pause_occurrence)
    engine = SchedulerEngine(svc=service)
    task = asyncio.create_task(engine._tick_github(schedule, _NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await schedules.update_schedule("poll", {"github_repo": "other/repo"})
        release.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        await _reap([task])
    row = await _row(reader)
    assert row["github_cursor"] == initial
    assert row["predispatch_refusal_count"] == 0
    assert row["predispatch_refusal_event"] is None
    assert row["poller_consecutive_cap_no_dispatch"] == 0
    assert await reader.list_schedule_runs("poll") == []
    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_retarget_does_not_change_existing_401_health_accounting(stores, client, monkeypatch):
    db, editor, reader = stores
    await _schedule(db, streak=0)
    await db.update_schedule("poll", poller_consecutive_401=2)
    schedule = await _row(db)
    client.response = _Response(status=401)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_poll():
        client.before_get = None
        entered.set()
        await release.wait()

    @asynccontextmanager
    async def edit_context():
        yield editor

    monkeypatch.setattr(schedules, "StateDB", edit_context)
    client.before_get = pause_poll
    task = asyncio.create_task(SchedulerEngine(svc=_Service(db))._tick_github(schedule, _NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await schedules.update_schedule("poll", {"github_repo": "other/repo"})
        release.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        await _reap([task])
    row = await _row(reader)
    assert row["poller_consecutive_401"] == 3
    assert row["last_healthy_poll_at"] is None
    assert row["poller_consecutive_cap_no_dispatch"] == 0


@pytest.mark.asyncio
async def test_same_target_claim_loser_is_not_a_predispatch_refusal(
    stores, client, process, monkeypatch
):
    db, other, reader = stores
    initial = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    schedule = await _schedule(db, streak=0, github_cursor=initial)
    client.response = _Response([_pr(1)])
    first_service, second_service = _Service(db), _Service(other)
    first_engine, second_engine = (
        SchedulerEngine(svc=first_service),
        SchedulerEngine(svc=second_service),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = first_service.create_schedule_run_and_advance
    started = []
    spawn = subprocess_module.asyncio.create_subprocess_exec

    async def capture_spawn(*args, **kwargs):
        started.append(args)
        return await spawn(*args, **kwargs)

    async def pause_claim(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(subprocess_module.asyncio, "create_subprocess_exec", capture_spawn)
    monkeypatch.setattr(first_service, "create_schedule_run_and_advance", pause_claim)
    first = asyncio.create_task(first_engine._tick_github(schedule, _NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        # Both engines use the same target and original cursor; the second commits first.
        await asyncio.wait_for(second_engine._tick_github(dict(schedule), _NOW), timeout=5)
        winner_row = await _row(reader)
        assert winner_row["poller_target_generation"] == schedule["poller_target_generation"]
        assert winner_row["github_cursor"] == github_module._cursor_for("2026-10-24T10:05:00Z", 1)
        release.set()
        await asyncio.wait_for(first, timeout=5)
    finally:
        release.set()
        await _reap([first])

    row = await _row(reader)
    assert row["github_cursor"] == winner_row["github_cursor"]
    assert row["predispatch_refusal_count"] == 0
    assert row["predispatch_refusal_event"] is None
    runs = await reader.list_schedule_runs("poll")
    assert len(runs) == 1
    assert runs[0]["trigger_context"]["github_events"][0]["pr_number"] == 1
    assert len(started) == 1
    invocations = await reader.list_invocations(skill="scheduled:poll")
    assert len(invocations) == 2
    loser = next(inv for inv in invocations if inv["id"] != runs[0]["invocation_id"])
    assert loser["status"] == "cancelled"
    assert first_engine._global_inflight == second_engine._global_inflight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("retarget", [False, True], ids=["setup-refusal", "lost-claim"])
async def test_setup_refusal_is_not_recorded_after_target_claim_is_lost(
    stores, client, process, monkeypatch, retarget
):
    import lionagi.studio.scheduler.engine as engine_module

    db, editor, reader = stores
    initial = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    schedule = await _schedule(db, streak=0, github_cursor=initial)
    client.response = _Response([_pr(1)])
    service = _Service(db)
    engine = SchedulerEngine(svc=service)
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.create_schedule_run_and_advance
    started = []
    spawn = subprocess_module.asyncio.create_subprocess_exec

    async def fail_cwd(_schedule):
        raise RuntimeError("synthetic action setup failure")

    async def capture_spawn(*args, **kwargs):
        started.append(args)
        return await spawn(*args, **kwargs)

    async def pause_claim(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(engine_module, "_resolve_action_cwd", fail_cwd)
    monkeypatch.setattr(subprocess_module.asyncio, "create_subprocess_exec", capture_spawn)
    if retarget:
        monkeypatch.setattr(service, "create_schedule_run_and_advance", pause_claim)
    task = asyncio.create_task(engine._tick_github(schedule, _NOW))
    try:
        if retarget:
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert await editor.update_schedule("poll", github_repo="other/repo")
            release.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        await _reap([task])

    row = await _row(reader)
    runs = await reader.list_schedule_runs("poll")
    invocations = await reader.list_invocations(skill="scheduled:poll")
    assert len(invocations) == 1 and started == []
    assert row["github_cursor"] == initial
    assert engine._global_inflight == 0
    if retarget:
        assert row["poller_target_generation"] == 1
        assert row["predispatch_refusal_count"] == 0
        assert row["predispatch_refusal_event"] is None
        assert runs == []
        assert invocations[0]["status"] == "cancelled"
    else:
        assert row["poller_target_generation"] == 0
        assert row["predispatch_refusal_count"] == 1
        assert row["predispatch_refusal_event"] == github_module._cursor_for(
            "2026-10-24T10:05:00Z", 1
        )
        assert len(runs) == 1 and runs[0]["status"] == "failed"
        assert runs[0]["invocation_id"] == invocations[0]["id"]
        assert invocations[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_target_change_after_occurrence_commit_does_not_undo_the_claim(
    stores, client, process, monkeypatch
):
    db, editor, reader = stores
    initial = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    schedule = await _schedule(db, streak=0, github_cursor=initial)
    client.response = _Response([_pr(1)])
    service = _Service(db)
    engine = SchedulerEngine(svc=service)
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.create_schedule_run_and_advance
    started = []
    spawn = subprocess_module.asyncio.create_subprocess_exec

    async def capture_spawn(*args, **kwargs):
        started.append(args)
        return await spawn(*args, **kwargs)

    async def pause_after_commit(*args, **kwargs):
        won = await original(*args, **kwargs)
        assert won
        entered.set()
        await release.wait()
        return won

    monkeypatch.setattr(subprocess_module.asyncio, "create_subprocess_exec", capture_spawn)
    monkeypatch.setattr(service, "create_schedule_run_and_advance", pause_after_commit)
    task = asyncio.create_task(engine._tick_github(schedule, _NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        before = await _row(reader)
        assert before["github_cursor"] == github_module._cursor_for("2026-10-24T10:05:00Z", 1)
        runs_before = await reader.list_schedule_runs("poll")
        assert len(runs_before) == 1 and started == []
        assert await editor.update_schedule("poll", github_repo="other/repo")
        release.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        await _reap([task])

    row = await _row(reader)
    assert row["github_repo"] == "other/repo" and row["poller_target_generation"] == 1
    assert row["github_cursor"] == before["github_cursor"]
    assert row["predispatch_refusal_count"] == 0
    runs = await reader.list_schedule_runs("poll")
    assert len(runs) == 1 and runs[0]["id"] == runs_before[0]["id"]
    assert runs[0]["trigger_context"]["github_events"][0]["pr_number"] == 1
    assert len(started) == 1
    assert engine._global_inflight == 0
