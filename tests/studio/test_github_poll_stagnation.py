# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Persist settled GitHub observations independently of HTTP health and cursor movement."""

from __future__ import annotations

import asyncio
import sys
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
from lionagi.studio.services.schedules import compute_schedule_health

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
async def test_capped_poll_health_explains_recovery(stores, client):
    db, _, reader = stores
    await _schedule(db, streak=0, github_filter={"event": "pr_merged", "merged_page_limit": 5})
    engine = SchedulerEngine(svc=_Service(db))
    for attempt in range(3):
        await engine._tick_github(await _row(db), _NOW + attempt * 2)
    row = await _row(reader)
    health = compute_schedule_health(row, {}, now=_NOW + 6)
    assert health["health_state"] == "overdue"
    assert health["health_cause"] == "page_limit_reached"
    assert "raise" in health["health_recovery"].lower()
    assert row["poller_last_capped_page_limit"] == 5
    await db.update_schedule("poll", github_filter={"event": "pr_merged", "merged_page_limit": 100})
    row = await _row(reader)
    health = compute_schedule_health(row, {}, now=_NOW + 6)
    assert health["health_state"] == "overdue"
    assert "5" in health["health_recovery"] and "raise" in health["health_recovery"].lower()
    assert "insufficient" not in health["health_recovery"].lower()
    await engine._tick_github(await _row(db), _NOW + 8)
    row = await _row(reader)
    assert row["poller_last_capped_page_limit"] == 100
    health = compute_schedule_health(row, {}, now=_NOW + 8)
    assert "insufficient" in health["health_recovery"].lower()
    assert "reconcil" in health["health_recovery"].lower()
    await db.update_schedule("poll", github_filter={"event": "pr_opened"})
    row = await _row(reader)
    health = compute_schedule_health(row, {}, now=_NOW + 10)
    assert row[_COUNTER] == 0
    assert row["poller_last_capped_page_limit"] is None
    assert "health_cause" not in health and "health_recovery" not in health


@pytest.mark.asyncio
async def test_capped_poll_records_limit_from_start_despite_concurrent_edit(
    stores, client, monkeypatch
):
    from lionagi.studio.services import schedules

    db, editor, reader = stores
    await _schedule(db, streak=0, github_filter={"event": "pr_merged", "merged_page_limit": 5})

    @asynccontextmanager
    async def edit_context():
        yield editor

    monkeypatch.setattr(schedules, "StateDB", edit_context)

    async def change_limit():
        client.before_get = None
        await schedules.update_schedule(
            "poll", {"github_filter": {"event": "pr_merged", "merged_page_limit": 100}}
        )

    client.before_get = change_limit
    await SchedulerEngine(svc=_Service(db))._tick_github(await _row(db), _NOW)
    row = await _row(reader)
    assert client.calls == 5
    assert row["github_filter"]["merged_page_limit"] == 100
    assert (row[_COUNTER], row["poller_last_capped_page_limit"]) == (1, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "retained"),
    [
        ({"github_repo": "other/repo"}, False),
        ({"github_cursor": "2026-10-24T10:00:00Z"}, False),
        ({"github_filter": {"event": "pr_opened"}}, False),
        ({"github_filter": {"event": "pr_merged", "base": "dev"}}, False),
        ({"github_filter": {"event": "pr_merged", "merged_page_limit": 6}}, True),
        ({"github_repo": "owner/repo"}, True),
    ],
)
async def test_target_patch_clears_stagnation_but_limit_only_preserves_it(
    stores, client, monkeypatch, patch, retained
):
    from lionagi.studio.services import schedules

    db, editor, reader = stores
    await _schedule(db, streak=0, github_filter={"event": "pr_merged", "merged_page_limit": 5})
    engine = SchedulerEngine(svc=_Service(db))
    for attempt in range(3):
        await engine._tick_github(await _row(db), _NOW + attempt * 2)
    assert compute_schedule_health(await _row(reader), {}, now=_NOW + 6)["health_cause"] == (
        "page_limit_reached"
    )

    @asynccontextmanager
    async def edit_context():
        yield editor

    monkeypatch.setattr(schedules, "StateDB", edit_context)
    assert await schedules.update_schedule("poll", patch)
    row = await _row(reader)
    assert (row[_COUNTER], row["poller_last_capped_page_limit"]) == (
        (3, 5) if retained else (0, None)
    )
    assert row["poller_target_generation"] == (0 if retained else 1)
    if not retained:
        assert "health_cause" not in compute_schedule_health(row, {}, now=_NOW + 6)


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_old_target", [False, True], ids=["changed", "changed_back"])
async def test_old_inflight_poll_cannot_restore_stagnation_after_target_patch(
    stores, client, monkeypatch, restore_old_target
):
    from lionagi.studio.services import schedules

    db, editor, reader = stores
    schedule = await _schedule(
        db, streak=0, github_filter={"event": "pr_merged", "merged_page_limit": 5}
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_first_page():
        client.before_get = None
        entered.set()
        await release.wait()

    @asynccontextmanager
    async def edit_context():
        yield editor

    client.before_get = pause_first_page
    monkeypatch.setattr(schedules, "StateDB", edit_context)
    task = asyncio.create_task(SchedulerEngine(svc=_Service(db))._tick_github(schedule, _NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await schedules.update_schedule("poll", {"github_repo": "other/repo"})
        if restore_old_target:
            assert await schedules.update_schedule("poll", {"github_repo": "owner/repo"})
        release.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        await _reap([task])
    row = await _row(reader)
    assert client.calls == 5
    assert (row[_COUNTER], row["poller_last_capped_page_limit"]) == (0, None)
    assert row["poller_target_generation"] == (2 if restore_old_target else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_scan_complete", [False, True], ids=["capped", "complete"])
async def test_inflight_limit_and_cursor_change_preserves_cursor_and_next_poll(
    stores, client, process, first_scan_complete
):
    db, editor, reader = stores
    initial = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    advanced = github_module._cursor_for("2026-10-24T10:07:00Z", 0)
    newer = github_module._cursor_for("2026-10-24T10:10:00Z", 2)
    schedule = await _schedule(
        db,
        streak=0,
        github_cursor=initial,
        github_filter={"event": "pr_merged", "merged_page_limit": 5},
    )
    older_pr = _pr(1)
    newer_pr = _pr(2, safe=False)
    newer_pr["updated_at"] = "2026-10-24T10:20:00Z"
    filler = _pr(3)
    filler.update(updated_at="2026-10-24T10:20:00Z", merged_at=None)
    first_pages = [
        _Response([older_pr, newer_pr], more=True),
        *[_Response([filler, filler], more=True) for _ in range(3)],
        _Response([filler] if first_scan_complete else [filler, filler], more=True),
    ]
    next_pages = [
        _Response([older_pr, newer_pr], more=True),
        *[_Response([filler, filler], more=True) for _ in range(4)],
        _Response([filler]),
    ]
    client.responses = first_pages + next_pages
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_first_page():
        client.before_get = None
        entered.set()
        await release.wait()

    client.before_get = pause_first_page
    engine = SchedulerEngine(svc=_Service(db))
    task = asyncio.create_task(engine._tick_github(schedule, _NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await editor.update_schedule(
            "poll",
            github_cursor=advanced,
            github_filter={"event": "pr_merged", "merged_page_limit": 6},
        )
        release.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        await _reap([task])

    row = await _row(reader)
    assert client.calls == 5
    assert row["github_cursor"] == advanced
    assert row["github_filter"]["merged_page_limit"] == 6
    assert await reader.list_schedule_runs("poll") == []

    await engine._tick_github(await _row(db), _NOW + 2)
    row = await _row(reader)
    assert client.calls == 11
    assert row["github_cursor"] == newer
    runs = await reader.list_schedule_runs("poll")
    assert [run["trigger_context"]["github_events"][0]["pr_number"] for run in runs] == [2]


@pytest.mark.asyncio
async def test_raised_limit_recovers_ordered_events_after_restart(
    stores, client, process, monkeypatch
):
    db, _, reader = stores
    cursor = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    await _schedule(db, streak=0, github_cursor=cursor)
    monkeypatch.setattr(github_module, "_MERGED_MODE_MAX_PAGES", 5)
    pages = []
    for index in range(5):
        items = []
        for number in (100 + index * 2, 101 + index * 2):
            item = _pr(number)
            item.update(updated_at="2026-10-24T10:20:00Z", merged_at=None)
            items.append(item)
        pages.append(items)
    newer = _pr(1)
    newer.update(updated_at="2026-10-24T10:20:00Z", merged_at="2026-10-24T10:10:00Z")
    pages[0][0] = newer
    older = _pr(2)
    older.update(updated_at="2026-10-24T10:15:00Z", merged_at="2026-10-24T10:05:00Z")
    pages.append([older])

    class Pages:
        index = 0

        async def get(self, url, headers=None, params=None):
            if params is not None:
                self.index = 0
            index = self.index
            self.index += 1
            return _Response(pages[index], more=index + 1 < len(pages))

    monkeypatch.setattr(github_module, "_get_client", lambda: Pages())
    service = _Service(db)
    fired = []
    original = service.create_schedule_run_and_advance

    async def record(payload, **kwargs):
        result = await original(payload, **kwargs)
        if result:
            fired.append(payload["trigger_context"]["github_events"][0]["pr_number"])
        return result

    monkeypatch.setattr(service, "create_schedule_run_and_advance", record)
    engine = SchedulerEngine(svc=service)
    for index in range(3):
        await engine._tick_github(await _row(db), _NOW + index * 2)
    row = await _row(reader)
    assert row["github_cursor"] == cursor and row[_COUNTER] == 3
    assert compute_schedule_health(row, {}, now=_NOW + 6)["health_cause"] == "page_limit_reached"
    assert fired == []
    await db.update_schedule("poll", github_filter={"event": "pr_merged", "merged_page_limit": 6})
    engine = SchedulerEngine(svc=service)
    await engine._tick_github(await _row(db), _NOW + 6)
    row = await _row(reader)
    assert fired == [2, 1]
    assert row[_COUNTER] == 0
    assert row["github_cursor"] == github_module._cursor_for("2026-10-24T10:10:00Z", 1)
    assert "health_cause" not in compute_schedule_health(row, {}, now=_NOW + 6)
    await SchedulerEngine(svc=service)._tick_github(await _row(db), _NOW + 8)
    assert fired == [2, 1]
    runs = await reader.list_schedule_runs("poll")
    assert sorted(run["trigger_context"]["github_events"][0]["pr_number"] for run in runs) == [1, 2]


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
@pytest.mark.parametrize(("event", "status"), [("pr_merged", 200), ("pr_opened", 304)])
async def test_complete_empty_poll_resets_without_dispatch(stores, client, event, status):
    db, _, reader = stores
    schedule = await _schedule(db, github_filter={"event": event})
    client.response = _Response(status=status)
    await SchedulerEngine(svc=_Service(db))._tick_github(schedule, _NOW)
    assert (await _row(reader))[_COUNTER] == 0
    assert client.calls == 1


@pytest.mark.asyncio
async def test_unconditional_merged_304_keeps_progress_and_http_health(stores, client, monkeypatch):
    db, _, reader = stores
    cursor = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    await _schedule(db, streak=2, github_cursor=cursor)
    await db.record_github_poll_stagnation("poll", reset=False, page_limit=5)
    await db.update_schedule("poll", last_healthy_poll_at=_NOW - 10)
    client.response = _Response(status=304)
    results = []
    real_poll = github_module.github_poll

    async def observe(schedule):
        result = await real_poll(schedule)
        results.append(result)
        return result

    monkeypatch.setattr(github_module, "github_poll", observe)
    await SchedulerEngine(svc=_Service(db))._tick_github(await _row(db), _NOW)
    row = await _row(reader)
    assert len(results) == 1
    assert results[0].items == []
    assert results[0].scan_complete is False
    assert results[0].poll_status == "error"
    assert row[_COUNTER] == 3
    assert row["poller_last_capped_page_limit"] == 5
    assert row["github_cursor"] == cursor
    assert row["last_healthy_poll_at"] == _NOW - 10
    assert await reader.list_schedule_runs("poll") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_advanced", [False, True])
async def test_filtered_cursor_progress_is_not_dispatch(stores, client, operator_advanced):
    db, _, reader = stores
    schedule = await _schedule(db, github_filter={"event": "pr_merged", "draft": False})
    # A complete scan can advance past a filtered event without launching it.
    client.response = _Response([_pr(1, draft=True)])
    newer = "2026-10-25T00:00:00Z"
    if operator_advanced:

        async def advance():
            await reader.update_schedule("poll", github_cursor=newer)

        client.before_get = advance
    await SchedulerEngine(svc=_Service(db))._tick_github(schedule, _NOW)
    row = await _row(reader)
    assert row[_COUNTER] == 0
    if operator_advanced:
        assert row["github_cursor"] == newer
    else:
        assert row["github_cursor"] == github_module._cursor_for("2026-10-24T10:05:00Z", 1)
    assert await db.list_schedule_runs("poll") == []


@pytest.mark.asyncio
async def test_capped_filtered_scan_preserves_cursor_and_counts_as_stagnation(
    stores, client, process, monkeypatch
):
    db, _, reader = stores
    initial_cursor = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    await _schedule(
        db,
        streak=2,
        github_cursor=initial_cursor,
        github_filter={"event": "pr_merged", "draft": False},
    )
    before = await _row(reader)
    assert before["github_cursor"] == initial_cursor
    assert before[_COUNTER] == 2
    client.response = _Response([_pr(1, draft=True), _pr(2, safe=False)], more=True)
    poll_results = []
    github_poll = github_module.github_poll

    async def observe_poll(schedule):
        result = await github_poll(schedule)
        poll_results.append(result)
        return result

    monkeypatch.setattr(github_module, "github_poll", observe_poll)
    engine = SchedulerEngine(svc=_Service(db))
    await engine._tick_github(await _row(db), _NOW)

    assert len(poll_results) == 1
    result = poll_results[0]
    assert result.poll_status == "ok"
    assert result.scan_complete is False
    assert result.items == []
    row = await _row(reader)
    assert row["github_cursor"] == initial_cursor
    assert row[_COUNTER] == 3
    assert row["last_healthy_poll_at"] == _NOW
    assert await reader.list_schedule_runs("poll") == []
    assert "poll" not in engine._github_batches
    assert engine._global_inflight == 0
    assert client.calls == 1
    assert compute_schedule_health(row, {}, now=_NOW)["health_state"] == "overdue"


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
        if capped:
            # An incomplete poll has no fire items to put in a scheduler batch.
            assert (await _row(reader))[_COUNTER] == 3
            assert "poll" not in engine._github_batches
            assert await reader.list_schedule_runs("poll") == []
            assert engine._global_inflight == 1
            assert client.calls == 1
        else:
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
    if capped:
        assert (await _row(reader))[_COUNTER] == 4
        assert "poll" not in engine._github_batches
        assert await reader.list_schedule_runs("poll") == []
        assert client.calls == 2
    else:
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
    client.response = _Response(status=200)
    await engine._tick_github(await _row(db), _NOW + 2)
    assert (await _row(reader))[_COUNTER] == 0
    assert client.calls == 2


@pytest.mark.asyncio
async def test_limit_change_keeps_held_batch_without_skipping(stores, client, process, monkeypatch):
    import lionagi.studio.config as config

    db, _, reader = stores
    await _schedule(db, streak=0, github_filter={"event": "pr_merged", "merged_page_limit": 5})
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    client.response = _Response([_pr(1)])
    engine = SchedulerEngine(svc=_Service(db))
    _, holder = await engine._reserve_global_slot("other", path="scheduled")
    try:
        await engine._tick_github(await _row(db), _NOW)
        assert "poll" in engine._github_batches
        assert (await _row(reader))["github_cursor"] is None
        await db.update_schedule(
            "poll", github_filter={"event": "pr_merged", "merged_page_limit": 6}
        )
    finally:
        holder.release()
    await engine._tick_github(await _row(db), _NOW + 2)
    assert client.calls == 1
    assert "poll" not in engine._github_batches
    runs = await reader.list_schedule_runs("poll")
    completed = [run for run in runs if run["status"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["trigger_context"]["github_events"][0]["pr_number"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 503])
@pytest.mark.parametrize("capacity_occupied", [False, True])
async def test_fresh_pagination_error_releases_no_events_or_held_batch(
    stores, client, process, monkeypatch, status, capacity_occupied
):
    import lionagi.studio.config as config

    db, _, reader = stores
    await _schedule(db)
    monkeypatch.setattr(github_module, "_MERGED_MODE_MAX_PAGES", 2)
    client.responses = [
        _Response([_pr(1), _pr(2, safe=False)], more=True),
        _Response(status=status),
    ]
    poll_results = []
    github_poll = github_module.github_poll

    async def observe_poll(schedule):
        result = await github_poll(schedule)
        poll_results.append(result)
        return result

    monkeypatch.setattr(github_module, "github_poll", observe_poll)
    engine = SchedulerEngine(svc=_Service(db))
    holder = None
    if capacity_occupied:
        monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
        _, holder = await engine._reserve_global_slot("other", path="scheduled")
    try:
        await engine._tick_github(await _row(db), _NOW)
    finally:
        if holder is not None:
            holder.release()
    assert len(poll_results) == 1
    result = poll_results[0]
    assert result.items == []
    assert result.scan_complete is False
    assert result.poll_status == ("auth_error" if status == 401 else "error")
    row = await _row(reader)
    assert row[_COUNTER] == 2
    assert row["last_healthy_poll_at"] is None
    assert row["poller_consecutive_401"] == (1 if status == 401 else 0)
    assert row["github_cursor"] is None
    assert await db.list_schedule_runs("poll") == []
    assert "poll" not in engine._github_batches
    assert engine._global_inflight == 0
    assert client.calls == 2


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
    import lionagi.studio.config as config

    db, _, reader = stores
    await _schedule(db)
    client.response = _Response([_pr(1)])
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
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
    allowed, holder = await engine._reserve_global_slot("other", path="scheduled")
    assert allowed
    try:
        await engine._tick_github(await _row(db), _NOW)
        assert engine._github_batches["poll"].matches(await _row(reader))
        assert (await _row(reader))[_COUNTER] == 2
        assert outcomes == []
        held_rows = await db.list_schedule_runs("poll")
        held_rows_by_id = {
            run["id"]: (run["status"], run["status_reason_code"], run["dispatched_at"])
            for run in held_rows
        }
        assert len(held_rows_by_id) == 1
        assert set(held_rows_by_id.values()) == {("skipped", "schedule.deferred.capacity", None)}
    finally:
        holder.release()
    # Consume the real matching batch without a fresh complete-poll reset.
    await engine._tick_github(await _row(db), _NOW + 2)
    assert outcomes == [True]
    assert (await _row(reader))[_COUNTER] == 2
    runs = await db.list_schedule_runs("poll")
    assert len(runs) == len(held_rows_by_id) + 1
    assert {
        run["id"]: (run["status"], run["status_reason_code"], run["dispatched_at"])
        for run in runs
        if run["id"] in held_rows_by_id
    } == held_rows_by_id
    new_runs = [run for run in runs if run["id"] not in held_rows_by_id]
    assert len(new_runs) == 1
    run = new_runs[0]
    assert run["invocation_id"] is not None
    assert run["trigger_context"]["github_events"][0]["pr_number"] == 1
    assert run["status"] == "running"
    assert run["dispatched_at"] is None
    assert run["exit_code"] is None
    assert "poll" not in engine._github_batches
    assert engine._global_inflight == 0
    assert client.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("accounting_fails", [False, True])
async def test_cross_task_launch_before_dispatch_stamp_survives_cancellation(
    stores, client, process, monkeypatch, accounting_fails
):
    db, _, reader = stores
    await _schedule(db)
    client.response = _Response([_pr(1)])
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
        if sys.version_info < (3, 12):
            assert callback_tasks[0] is not task
        # Cancel before the dispatch-stamp await or exit-code fallback can establish
        # any further evidence; wait_for uses a separate task only before Python 3.12.
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
    client.response = _Response([_pr(1)])
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

        async def observe(schedule_id, *, reset, page_limit=None, expected_target_generation=None):
            nonlocal accounting
            accounting = True
            try:
                return await record(
                    schedule_id,
                    reset=reset,
                    page_limit=page_limit,
                    expected_target_generation=expected_target_generation,
                )
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
            client.response = _Response(status=200)
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
    import lionagi.studio.config as config

    db, _, reader = stores
    await _schedule(db)
    client.response = _Response([_pr(1)])
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 1)
    monkeypatch.setattr(subprocess_module, "build_argv", lambda *a, **k: (["test-child"], None))
    monkeypatch.setattr(subprocess_module, "resolve_li_executable", lambda: (["li"], None))
    launch_callbacks = []

    async def confirmed_exit(*args, **kwargs):
        launch_callbacks.append(kwargs["on_launched"])
        return 0, ""

    monkeypatch.setattr(subprocess_module, "spawn_and_wait", confirmed_exit)
    engine = SchedulerEngine(svc=_Service(db))
    allowed, holder = await engine._reserve_global_slot("other", path="scheduled")
    assert allowed
    try:
        await engine._tick_github(await _row(db), _NOW)
        assert engine._github_batches["poll"].matches(await _row(reader))
        assert (await _row(reader))[_COUNTER] == 2
        assert launch_callbacks == []
        held_rows = await db.list_schedule_runs("poll")
        held_rows_by_id = {
            run["id"]: (run["status"], run["status_reason_code"], run["dispatched_at"])
            for run in held_rows
        }
        assert len(held_rows_by_id) == 1
        assert set(held_rows_by_id.values()) == {("skipped", "schedule.deferred.capacity", None)}
    finally:
        holder.release()
    # The callback is passed but never invoked; only the returned exit confirms launch.
    await engine._tick_github(await _row(db), _NOW + 2)
    assert len(launch_callbacks) == 1
    assert callable(launch_callbacks[0])
    assert (await _row(reader))[_COUNTER] == 0
    runs = await db.list_schedule_runs("poll")
    assert len(runs) == len(held_rows_by_id) + 1
    assert {
        run["id"]: (run["status"], run["status_reason_code"], run["dispatched_at"])
        for run in runs
        if run["id"] in held_rows_by_id
    } == held_rows_by_id
    new_runs = [run for run in runs if run["id"] not in held_rows_by_id]
    assert len(new_runs) == 1
    run = new_runs[0]
    assert run["invocation_id"] is not None
    assert run["trigger_context"]["github_events"][0]["pr_number"] == 1
    assert run["status"] == "completed"
    assert run["status_reason_code"] == "run.completed.ok"
    assert run["exit_code"] == 0
    # The exit confirms launch even though the unused callback never stamps the row.
    assert run["dispatched_at"] is None
    assert "poll" not in engine._github_batches
    assert engine._global_inflight == 0
    assert client.calls == 1


@pytest.mark.asyncio
async def test_repeated_truncated_polls_never_release_an_older_merge_and_become_overdue(
    stores, client, process, monkeypatch
):
    db, _, reader = stores
    initial_cursor = github_module._cursor_for("2026-10-24T10:00:00Z", 0)
    await _schedule(
        db,
        streak=0,
        github_repo="owner/truncated-release-regression",
        github_cursor=initial_cursor,
    )
    # PR 1 merges below the oldest fetched updated_at; the old split releases it.
    client.response = _Response([_pr(1, safe=True), _pr(2, safe=False)], more=True)
    poll_results = []
    github_poll = github_module.github_poll

    async def observe_poll(schedule):
        result = await github_poll(schedule)
        poll_results.append(result)
        return result

    monkeypatch.setattr(github_module, "github_poll", observe_poll)
    engine = SchedulerEngine(svc=_Service(db))
    observations = []
    for attempt in range(1, 4):
        now = _NOW + attempt * 2
        await engine._tick_github(await _row(db), now)
        row = await _row(reader)
        observations.append(
            (
                row,
                await reader.list_schedule_runs("poll"),
                "poll" in engine._github_batches,
                engine._global_inflight,
                compute_schedule_health(row, {}, now=now)["health_state"],
            )
        )

    assert len(poll_results) == 3
    assert client.calls == 3
    for attempt, (result, observed) in enumerate(zip(poll_results, observations), start=1):
        row, runs, held, inflight, health = observed
        assert result.items == []
        assert result.scan_complete is False
        assert result.poll_status == "ok"
        assert row["github_cursor"] == initial_cursor
        assert row[_COUNTER] == attempt
        assert row["last_healthy_poll_at"] == _NOW + attempt * 2
        assert row["poller_consecutive_401"] == 0
        assert runs == []
        assert held is False
        assert inflight == 0
        assert health == ("overdue" if attempt == 3 else "healthy")
