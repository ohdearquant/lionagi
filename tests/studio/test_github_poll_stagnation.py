# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Persist settled GitHub observations independently of HTTP health and cursor movement."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from lionagi.state.db import StateDB
from lionagi.studio.scheduler import github as github_module
from lionagi.studio.scheduler import subprocess as subprocess_module
from lionagi.studio.scheduler.engine import SchedulerEngine
from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

_COUNTER = "poller_consecutive_cap_no_dispatch"
_NOW = 2_000_000_000.0


class _Service(_DBSchedulerStateService):
    def __init__(self, db):
        super().__init__()
        self.db = db

    @asynccontextmanager
    async def _db_context(self):
        yield self.db


@pytest_asyncio.fixture
async def stores(tmp_path):
    async with AsyncExitStack() as stack:
        path = tmp_path / "state.db"
        databases = [await stack.enter_async_context(StateDB(path)) for _ in range(3)]
        assert all(db._engine is not None for db in databases)
        assert len({id(db._engine) for db in databases}) == 3
        yield databases


async def _schedule(db, *, streak=2, **fields):
    await db.create_schedule(
        {
            "id": "poll",
            "name": "poll",
            "trigger_type": "github_poll",
            "github_repo": "owner/repo",
            "github_filter": {"event": "pr_merged"},
            "poll_interval_sec": 1,
            "action_kind": "agent",
            "action_model": "gpt-4.1-mini",
            "action_cwd": str(Path(__file__).parent),
            "action_prompt": "handle {{pr_number}}",
            **fields,
        }
    )
    for _ in range(streak):
        assert await db.record_github_poll_stagnation("poll", reset=False)
    return await db.get_schedule("poll")


async def _row(db):
    row = await db.get_schedule("poll")
    assert row is not None
    return row


def _pr(number, *, safe=True, draft=False):
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "user": {"login": "author"},
        "head": {"sha": f"head-{number}", "repo": None},
        "state": "closed",
        "draft": draft,
        "updated_at": "2026-10-24T10:20:00Z" if number == 1 else "2026-10-24T10:10:00Z",
        "merged_at": "2026-10-24T10:05:00Z" if safe else "2026-10-24T10:10:00Z",
    }


class _Response:
    def __init__(self, prs=(), *, status=200, more=False):
        self.status_code = status
        self.prs = list(prs)
        self.headers = {"x-ratelimit-remaining": "100"}
        if more:
            self.headers["link"] = '<https://api.github.com/next>; rel="next"'

    def json(self):
        return self.prs


class _Client:
    def __init__(self):
        self.responses = []
        self.response = _Response([_pr(1, safe=False), _pr(2, safe=False)], more=True)
        self.calls = 0
        self.before_get = None
        self.failure = None

    async def get(self, *args, **kwargs):
        self.calls += 1
        if self.before_get is not None:
            await self.before_get()
        if self.failure is not None:
            raise self.failure
        return self.responses.pop(0) if self.responses else self.response


@pytest.fixture
def client(monkeypatch):
    client = _Client()

    async def token(**kwargs):
        return "test-token"

    monkeypatch.setattr(github_module, "_cached_token", None)
    monkeypatch.setattr(github_module, "_get_gh_token", token)
    monkeypatch.setattr(github_module, "_get_client", lambda: client)
    monkeypatch.setattr(github_module, "_PER_PAGE", 2)
    monkeypatch.setattr(github_module, "_MERGED_MODE_MAX_PAGES", 1)
    return client


class _Pipe:
    def __init__(self, release):
        self.release = release

    async def read(self, size):
        await self.release.wait()
        return b""


class _Process:
    pid = None
    returncode = 0

    def __init__(self, release):
        self.release = release
        self.stdout = _Pipe(release)
        self.stderr = _Pipe(release)

    async def wait(self):
        await self.release.wait()
        return 0


@pytest.fixture
def process(monkeypatch):
    release = asyncio.Event()
    release.set()
    process = _Process(release)

    async def create(*args, **kwargs):
        return process

    async def terminate(proc, **kwargs):
        proc.release.set()
        await proc.wait()

    monkeypatch.setattr(subprocess_module, "build_argv", lambda *a, **k: (["test-child"], None))
    monkeypatch.setattr(subprocess_module, "resolve_li_executable", lambda: (["li"], None))
    monkeypatch.setattr(subprocess_module, "observe_process_group_leader", lambda *a, **k: None)
    monkeypatch.setattr(subprocess_module, "aterminate_process_group", terminate)
    monkeypatch.setattr(subprocess_module.asyncio, "create_subprocess_exec", create)
    return process


async def _reap(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)


@pytest.mark.asyncio
async def test_repeated_actual_capped_polls_count_without_downgrading_http_health(stores, client):
    db, _, reader = stores
    await _schedule(db, streak=0)
    engine = SchedulerEngine(svc=_Service(db))
    for attempt in range(1, 5):
        await engine._tick_github(await _row(db), _NOW + attempt * 2)
        row = await _row(reader)
        assert row[_COUNTER] == attempt
        assert row["last_healthy_poll_at"] == _NOW + attempt * 2
        assert row["poller_consecutive_401"] == 0
        assert row["github_cursor"] is None
    assert client.calls == 4


@pytest.mark.asyncio
async def test_capped_ok_recovers_auth_streak_before_next_401(stores, client):
    db, _, reader = stores
    await _schedule(db)
    engine = SchedulerEngine(svc=_Service(db))
    client.response = _Response(status=401)
    await engine._tick_github(await _row(db), _NOW)
    assert (await _row(reader))["poller_consecutive_401"] == 1
    client.response = _Response([_pr(1, safe=False), _pr(2, safe=False)], more=True)
    await engine._tick_github(await _row(db), _NOW + 2)
    row = await _row(reader)
    assert row[_COUNTER] == 3
    assert row["poller_consecutive_401"] == 0
    assert row["last_healthy_poll_at"] == _NOW + 2
    client.response = _Response(status=401)
    await engine._tick_github(await _row(db), _NOW + 4)
    row = await _row(reader)
    assert row["poller_consecutive_401"] == 1
    assert row[_COUNTER] == 3
    assert row["last_healthy_poll_at"] == _NOW + 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 304])
async def test_complete_empty_poll_resets_without_dispatch(stores, client, status):
    db, _, reader = stores
    schedule = await _schedule(db)
    client.response = _Response(status=status)
    await SchedulerEngine(svc=_Service(db))._tick_github(schedule, _NOW)
    assert (await _row(reader))[_COUNTER] == 0
    assert client.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_advanced", [False, True])
async def test_filtered_cursor_progress_is_not_dispatch(stores, client, operator_advanced):
    db, _, reader = stores
    schedule = await _schedule(db, github_filter={"event": "pr_merged", "draft": False})
    client.response = _Response([_pr(1, draft=True), _pr(2, safe=False)], more=True)
    newer = "2026-10-25T00:00:00Z"
    if operator_advanced:

        async def advance():
            await reader.update_schedule("poll", github_cursor=newer)

        client.before_get = advance
    await SchedulerEngine(svc=_Service(db))._tick_github(schedule, _NOW)
    row = await _row(reader)
    assert row[_COUNTER] == 3
    if operator_advanced:
        assert row["github_cursor"] == newer
    else:
        assert row["github_cursor"] == github_module._cursor_for("2026-10-24T10:05:00Z", 1)
    assert await db.list_schedule_runs("poll") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("capped", [False, True])
async def test_held_and_reheld_batch_are_neutral_then_no_poll_launch_resets(
    stores, client, process, monkeypatch, capped
):
    import lionagi.studio.config as config

    db, _, reader = stores
    await _schedule(db)
    client.response = _Response([_pr(1), _pr(2, safe=False)] if capped else [_pr(1)], more=capped)
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    engine = SchedulerEngine(svc=_Service(db))
    allowed, holder = await engine._reserve_global_slot("other", path="scheduled")
    assert allowed
    try:
        await engine._tick_github(await _row(db), _NOW)
        assert (await _row(reader))[_COUNTER] == 2
        assert "poll" in engine._github_batches
        # Admission of a held batch at a full cap does not start a pass or poll.
        await engine._admit_github_tick(await _row(db), _NOW + 2)
        assert not engine._github_tick_tasks
        assert (await _row(reader))[_COUNTER] == 2
        # A due pass that re-holds the matched batch also leaves accounting alone.
        await engine._tick_github(await _row(db), _NOW + 4)
        assert (await _row(reader))[_COUNTER] == 2
        assert client.calls == 1
    finally:
        holder.release()
    await engine._tick_github(await _row(db), _NOW + 6)
    assert (await _row(reader))[_COUNTER] == 0
    assert client.calls == 1
    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_stale_batch_uses_actual_fresh_complete_poll(stores, client, process, monkeypatch):
    import lionagi.studio.config as config

    db, _, reader = stores
    await _schedule(db)
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    client.response = _Response([_pr(1)])
    engine = SchedulerEngine(svc=_Service(db))
    _, holder = await engine._reserve_global_slot("other", path="scheduled")
    try:
        await engine._tick_github(await _row(db), _NOW)
    finally:
        holder.release()
    await db.update_schedule("poll", github_cursor="2026-10-25T00:00:00Z")
    client.response = _Response(status=304)
    await engine._tick_github(await _row(db), _NOW + 2)
    assert (await _row(reader))[_COUNTER] == 0
    assert client.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 503])
@pytest.mark.parametrize("held", [False, True])
async def test_fresh_pagination_error_precedes_safe_prefix_launch_or_hold(
    stores, client, process, monkeypatch, status, held
):
    import lionagi.studio.config as config

    db, _, reader = stores
    await _schedule(db)
    monkeypatch.setattr(github_module, "_MERGED_MODE_MAX_PAGES", 2)
    client.responses = [
        _Response([_pr(1), _pr(2, safe=False)], more=True),
        _Response(status=status),
    ]
    engine = SchedulerEngine(svc=_Service(db))
    holder = None
    if held:
        monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
        _, holder = await engine._reserve_global_slot("other", path="scheduled")
    try:
        await engine._tick_github(await _row(db), _NOW)
    finally:
        if holder is not None:
            holder.release()
    row = await _row(reader)
    assert row[_COUNTER] == 2
    assert row["last_healthy_poll_at"] is None
    assert row["poller_consecutive_401"] == (1 if status == 401 else 0)
    runs = await db.list_schedule_runs("poll")
    assert any(run["dispatched_at"] is not None for run in runs) is not held
    assert ("poll" in engine._github_batches) is held


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["cadence", "budget", "rate"])
async def test_pre_poll_boundaries_leave_counter_and_http_untouched(
    stores, client, monkeypatch, boundary
):
    db, _, reader = stores
    schedule = await _schedule(db)
    engine = SchedulerEngine(svc=_Service(db))
    now = 0.5 if boundary == "cadence" else _NOW
    if boundary == "budget":

        async def exhausted(*args):
            return True

        monkeypatch.setattr(engine, "_check_budget", exhausted)
    elif boundary == "rate":

        async def refused(*args, **kwargs):
            return False, None

        monkeypatch.setattr(engine, "_reserve_rate_limit", refused)
    await engine._tick_github(schedule, now)
    assert (await _row(reader))[_COUNTER] == 2
    assert client.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_interrupted_poll_without_launch_is_neutral(stores, client, failure_type):
    db, _, reader = stores
    schedule = await _schedule(db)
    failure = failure_type("poll interrupted")
    client.failure = failure
    with pytest.raises(failure_type) as caught:
        await SchedulerEngine(svc=_Service(db))._tick_github(schedule, _NOW)
    assert caught.value is failure
    assert (await _row(reader))[_COUNTER] == 2


@pytest.mark.asyncio
async def test_committed_but_unlaunched_true_is_not_progress(stores, client, process, monkeypatch):
    db, _, reader = stores
    schedule = await _schedule(db)
    client.response = _Response([_pr(1), _pr(2, safe=False)], more=True)
    svc = _Service(db)
    update_status = svc.update_status

    async def fail_running(entity_type, entity_id, **kwargs):
        if entity_type == "schedule_run" and kwargs["new_status"] == "running":
            raise RuntimeError("write failed after occurrence commit")
        return await update_status(entity_type, entity_id, **kwargs)

    monkeypatch.setattr(svc, "update_status", fail_running)
    engine = SchedulerEngine(svc=svc)
    fire = engine._fire
    outcomes = []

    async def observe_fire(*args, **kwargs):
        outcome = await fire(*args, **kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(engine, "_fire", observe_fire)
    await engine._tick_github(schedule, _NOW)
    assert outcomes == [True]
    assert (await _row(reader))[_COUNTER] == 3
    runs = await db.list_schedule_runs("poll")
    assert len(runs) == 1
    assert runs[0]["dispatched_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("accounting_fails", [False, True])
async def test_cross_task_launch_before_dispatch_stamp_survives_cancellation(
    stores, client, process, monkeypatch, accounting_fails
):
    db, _, reader = stores
    await _schedule(db)
    client.response = _Response([_pr(1), _pr(2, safe=False)], more=True)
    process.release.clear()
    svc = _Service(db)
    if accounting_fails:

        async def fail_accounting(*args, **kwargs):
            raise RuntimeError("accounting unavailable")

        monkeypatch.setattr(svc, "record_github_poll_stagnation", fail_accounting)
    engine = SchedulerEngine(svc=svc)
    mark_started = asyncio.Event()
    release_mark = asyncio.Event()
    callback_tasks = []

    async def blocked_stamp(run_id):
        callback_tasks.append(asyncio.current_task())
        mark_started.set()
        await release_mark.wait()

    monkeypatch.setattr(engine, "_mark_dispatched", blocked_stamp)
    engine._start_github_tick(await _row(db), _NOW)
    task = engine._github_tick_tasks["poll"]
    try:
        await asyncio.wait_for(mark_started.wait(), timeout=5)
        assert callback_tasks[0] is not task
        # The real spawn_and_wait awaits its callback in wait_for's child task. Cancel before
        # the dispatch-stamp await or exit-code fallback can establish any further evidence.
        task.cancel()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert (await _row(reader))[_COUNTER] == (2 if accounting_fails else 0)
        assert engine._global_inflight == 0
    finally:
        release_mark.set()
        process.release.set()
        await _reap([task])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_post_launch_accounting_failure_preserves_original_interruption(
    stores, client, process, monkeypatch, failure_type
):
    db, _, reader = stores
    schedule = await _schedule(db)
    client.response = _Response([_pr(1), _pr(2, safe=False)], more=True)
    svc = _Service(db)
    update = svc.update_schedule
    failure = failure_type("cursor write interrupted")

    async def interrupt_cursor(schedule_id, **fields):
        if "github_cursor" in fields:
            raise failure
        return await update(schedule_id, **fields)

    async def fail_accounting(*args, **kwargs):
        raise ValueError("accounting also failed")

    monkeypatch.setattr(svc, "update_schedule", interrupt_cursor)
    monkeypatch.setattr(svc, "record_github_poll_stagnation", fail_accounting)
    with pytest.raises(failure_type) as caught:
        await SchedulerEngine(svc=svc)._tick_github(schedule, _NOW)
    assert caught.value is failure
    assert (await _row(reader))[_COUNTER] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_between", [False, True])
async def test_independent_producers_account_in_commit_order(
    stores, client, monkeypatch, reset_between
):
    first, second, reader = stores
    await _schedule(first)
    snapshots = [await _row(first), await _row(second)]
    assert [row[_COUNTER] for row in snapshots] == [2, 2]
    services = [_Service(first), _Service(second)]
    engines = [SchedulerEngine(svc=svc) for svc in services]
    ready = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]

    def gate(index, db):
        record = db.record_github_poll_stagnation
        transaction = db._tx
        accounting = False

        async def observe(schedule_id, *, reset):
            nonlocal accounting
            accounting = True
            try:
                return await record(schedule_id, reset=reset)
            finally:
                accounting = False

        @asynccontextmanager
        async def enter_transaction():
            if accounting:
                # Native SQLite writes take BEGIN IMMEDIATE at transaction entry. Pause before
                # that lock, after any preparatory read, so both actual producers can arrive.
                ready[index].set()
                await asyncio.wait_for(release[index].wait(), timeout=5)
            async with transaction() as conn:
                yield conn

        monkeypatch.setattr(db, "record_github_poll_stagnation", observe)
        monkeypatch.setattr(db, "_tx", enter_transaction)

    for index, db in enumerate((first, second)):
        gate(index, db)
    tasks = [
        asyncio.create_task(engine._tick_github(snapshot, _NOW))
        for engine, snapshot in zip(engines, snapshots)
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), timeout=5)
        release[0].set()
        await asyncio.wait_for(tasks[0], timeout=5)
        assert (await _row(reader))[_COUNTER] == 3
        if reset_between:
            client.response = _Response(status=304)
            await SchedulerEngine(svc=_Service(reader))._tick_github(await _row(reader), _NOW + 2)
            assert (await _row(reader))[_COUNTER] == 0
        release[1].set()
        await asyncio.wait_for(tasks[1], timeout=5)
        assert (await _row(reader))[_COUNTER] == (1 if reset_between else 4)
        assert client.calls == (3 if reset_between else 2)
    finally:
        for event in release:
            event.set()
        await _reap(tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["forbidden", "server_error", "network_error"])
async def test_initial_http_errors_leave_both_observers_unchanged(stores, client, outcome):
    db, _, reader = stores
    schedule = await _schedule(db)
    if outcome == "network_error":
        client.failure = httpx.HTTPError("request unavailable")
    else:
        client.response = _Response(status=403 if outcome == "forbidden" else 500)
    await SchedulerEngine(svc=_Service(db))._tick_github(schedule, _NOW)
    row = await _row(reader)
    assert row[_COUNTER] == 2
    assert row["last_healthy_poll_at"] is None
    assert row["poller_consecutive_401"] == 0


@pytest.mark.asyncio
async def test_confirmed_exit_resets_even_without_launch_callback(stores, client, monkeypatch):
    db, _, reader = stores
    schedule = await _schedule(db)
    client.response = _Response([_pr(1), _pr(2, safe=False)], more=True)
    monkeypatch.setattr(subprocess_module, "build_argv", lambda *a, **k: (["test-child"], None))
    monkeypatch.setattr(subprocess_module, "resolve_li_executable", lambda: (["li"], None))

    async def confirmed_exit(*args, **kwargs):
        return 0, ""

    monkeypatch.setattr(subprocess_module, "spawn_and_wait", confirmed_exit)
    await SchedulerEngine(svc=_Service(db))._tick_github(schedule, _NOW)
    assert (await _row(reader))[_COUNTER] == 0
