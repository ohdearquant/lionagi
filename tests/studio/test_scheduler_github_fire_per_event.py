# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for SchedulerEngine._tick_github()'s fire-per-event dispatch.

A poll window can turn up more than one new/updated PR. Each dispatchable PR
must get its own top-level fire (its own trigger_context, its own max_runs +
global-slot reservation) rather than a single fire seeing only the first
event. The persisted github_cursor must advance only past PRs that were
actually dispatched (or intentionally filtered out) -- never past a PR that
was skipped for lack of budget/slot, or it would never be re-listed again.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.studio.scheduler import github as gh_mod
from lionagi.studio.scheduler.engine import SchedulerEngine
from lionagi.studio.scheduler.github import GithubPollItem


def _minimal_schedule(**overrides) -> dict:
    base = {
        "id": "sched-001",
        "name": "test-sched",
        "trigger_type": "github_poll",
        "github_repo": "acme/widgets",
        "action_kind": "agent",
        "action_model": "gpt-4.1-mini",
        "action_prompt": "handle {{pr_number}}",
        "action_agent": None,
        "action_playbook": None,
        "action_project": None,
        "action_extra_args": [],
        "action_flow_yaml": None,
        "on_success": None,
        "on_fail": None,
        "overlap_policy": "skip",
        "missed_fire_policy": "skip",
        "last_fired_at": 0,
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


def _item(pr_number, updated_at, *, dispatchable=True):
    return GithubPollItem(
        event={
            "pr_number": pr_number,
            "pr_title": f"PR {pr_number}",
            "pr_url": f"https://github.com/acme/widgets/pull/{pr_number}",
            "pr_author": "octocat",
            "updated_at": updated_at,
            "head_sha": f"sha{pr_number}",
            "draft": False,
        },
        updated_at=updated_at,
        dispatchable=dispatchable,
    )


def _spawn_patches():
    return (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    )


# ---------------------------------------------------------------------------
# Multi-event poll -> N fires, each single-event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_event_poll_fires_once_per_dispatchable_event():
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    polled = [
        _item(1, "2026-07-07T10:00:00Z"),
        _item(2, "2026-07-07T11:00:00Z"),
        _item(3, "2026-07-07T12:00:00Z"),
    ]

    p_build, p_spawn = _spawn_patches()
    with (
        patch("lionagi.studio.scheduler.github.github_poll", new=AsyncMock(return_value=polled)),
        p_build,
        p_spawn,
    ):
        await engine._tick_github(schedule, now=10_000.0)

    # One create_invocation per dispatched event -- not one for the whole batch.
    assert svc.create_invocation.await_count == 3
    contexts = [
        call.args[0]["trigger_context"]["github_events"]
        for call in svc.create_schedule_run.await_args_list
    ]
    assert [ctx[0]["pr_number"] for ctx in contexts] == [1, 2, 3]
    for ctx in contexts:
        assert len(ctx) == 1  # each fire's trigger_context carries exactly its own event

    # Cursor advances to the newest dispatched event.
    svc.update_schedule.assert_any_call("sched-001", github_cursor="2026-07-07T12:00:00Z")
    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_single_event_poll_behavior_unchanged():
    """A single-event poll fires exactly once, with the same single-event
    trigger_context shape as the multi-event case (regression guard for the
    common case)."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    polled = [_item(7, "2026-07-07T10:00:00Z")]

    p_build, p_spawn = _spawn_patches()
    with (
        patch("lionagi.studio.scheduler.github.github_poll", new=AsyncMock(return_value=polled)),
        p_build,
        p_spawn,
    ):
        await engine._tick_github(schedule, now=10_000.0)

    assert svc.create_invocation.await_count == 1
    (run_payload,), _ = svc.create_schedule_run.await_args
    assert [e["pr_number"] for e in run_payload["trigger_context"]["github_events"]] == [7]
    assert run_payload["trigger_context"]["repo"] == "acme/widgets"
    svc.update_schedule.assert_any_call("sched-001", github_cursor="2026-07-07T10:00:00Z")
    assert engine._global_inflight == 0


# ---------------------------------------------------------------------------
# Budget exhaustion mid-batch -> cursor stops before first undispatched event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_runs_exhaustion_mid_batch_stops_cursor_before_undispatched(caplog):
    svc = _make_svc()
    fired = 0

    async def _create_schedule_run(_payload):
        nonlocal fired
        fired += 1

    svc.create_schedule_run = AsyncMock(side_effect=_create_schedule_run)
    svc.count_schedule_runs = AsyncMock(side_effect=lambda *a, **k: fired)

    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=2)

    polled = [
        _item(1, "2026-07-07T10:00:00Z"),
        _item(2, "2026-07-07T11:00:00Z"),
        _item(3, "2026-07-07T12:00:00Z"),
    ]

    p_build, p_spawn = _spawn_patches()
    with (
        patch("lionagi.studio.scheduler.github.github_poll", new=AsyncMock(return_value=polled)),
        p_build,
        p_spawn,
        caplog.at_level("INFO"),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    # Only the first two (max_runs=2) fired.
    assert fired == 2
    assert svc.create_invocation.await_count == 2

    # Cursor stops at PR 2's updated_at, not PR 3's -- PR 3 was never dispatched.
    svc.update_schedule.assert_any_call("sched-001", github_cursor="2026-07-07T11:00:00Z")
    cursor_calls = [c for c in svc.update_schedule.await_args_list if "github_cursor" in c.kwargs]
    assert all(c.kwargs["github_cursor"] != "2026-07-07T12:00:00Z" for c in cursor_calls)

    # The drop is logged with schedule id and the deferred PR number.
    assert any(
        "sched-001" in r.message and "3" in r.message and "max_runs" in r.message
        for r in caplog.records
    )
    assert engine._global_inflight == 0
    assert engine._max_runs_inflight.get("sched-001", 0) == 0


@pytest.mark.asyncio
async def test_global_slot_exhaustion_mid_batch_stops_cursor_before_undispatched(caplog):
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    polled = [
        _item(1, "2026-07-07T10:00:00Z"),
        _item(2, "2026-07-07T11:00:00Z"),
    ]

    real_reserve = engine._reserve_global_slot
    call_count = 0

    async def _reserve_limited():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return await real_reserve()
        return False, None

    p_build, p_spawn = _spawn_patches()
    with (
        patch("lionagi.studio.scheduler.github.github_poll", new=AsyncMock(return_value=polled)),
        patch.object(engine, "_reserve_global_slot", side_effect=_reserve_limited),
        p_build,
        p_spawn,
        caplog.at_level("INFO"),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    assert svc.create_invocation.await_count == 1
    svc.update_schedule.assert_any_call("sched-001", github_cursor="2026-07-07T10:00:00Z")
    assert any(
        "sched-001" in r.message and "2" in r.message and "concurrent-fire" in r.message
        for r in caplog.records
    )
    assert engine._global_inflight == 0


# ---------------------------------------------------------------------------
# Draft-filtered events interleaved with dispatchable ones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filtered_event_between_dispatched_events_still_advances_cursor():
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    polled = [
        _item(1, "2026-07-07T10:00:00Z", dispatchable=True),
        _item(2, "2026-07-07T11:00:00Z", dispatchable=False),
        _item(3, "2026-07-07T12:00:00Z", dispatchable=True),
    ]

    p_build, p_spawn = _spawn_patches()
    with (
        patch("lionagi.studio.scheduler.github.github_poll", new=AsyncMock(return_value=polled)),
        p_build,
        p_spawn,
    ):
        await engine._tick_github(schedule, now=10_000.0)

    assert svc.create_invocation.await_count == 2
    svc.update_schedule.assert_any_call("sched-001", github_cursor="2026-07-07T12:00:00Z")


# ---------------------------------------------------------------------------
# Cursor/re-listing regression: an event dropped for budget must reappear on
# the next real github_poll() call once the persisted cursor reflects only
# what was actually dispatched.
# ---------------------------------------------------------------------------


def _pr(number, updated):
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "user": {"login": "octocat"},
        "updated_at": updated,
        "draft": False,
        "head": {"sha": f"sha{number}"},
    }


class _FakeResp:
    def __init__(self, prs):
        self._prs = prs
        self.status_code = 200
        self.headers = {"x-ratelimit-remaining": "100", "etag": '"abc"'}

    def json(self):
        return self._prs


class _FakeClient:
    def __init__(self, prs):
        self._prs = prs

    async def get(self, url, headers=None, params=None):
        return _FakeResp(self._prs)


@pytest.mark.asyncio
async def test_undispatched_event_is_relisted_on_next_poll(monkeypatch, caplog):
    """End-to-end across the github_poll/_tick_github boundary: a poll that
    finds two new PRs but can only afford to dispatch one persists a cursor
    that still sits before the undispatched PR, so a subsequent github_poll()
    call (as the next tick would issue) returns it again instead of silently
    dropping it forever."""
    # The real GitHub API returns PRs sorted by updated_at desc (newest
    # first) -- github_poll() reverses this to oldest-first before handing
    # items to the engine, so the fake response here must match that order.
    prs = [_pr(2, "2026-07-07T11:00:00Z"), _pr(1, "2026-07-07T10:00:00Z")]

    async def _fake_token():
        return "faketoken"

    monkeypatch.setattr(gh_mod, "_get_gh_token", _fake_token)
    monkeypatch.setattr(gh_mod, "_get_client", lambda: _FakeClient(prs))

    svc = _make_svc()
    fired = 0

    async def _create_schedule_run(_payload):
        nonlocal fired
        fired += 1

    svc.create_schedule_run = AsyncMock(side_effect=_create_schedule_run)
    svc.count_schedule_runs = AsyncMock(side_effect=lambda *a, **k: fired)

    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=1)

    p_build, p_spawn = _spawn_patches()
    with p_build, p_spawn, caplog.at_level("INFO"):
        await engine._tick_github(schedule, now=10_000.0)

    assert fired == 1
    persisted_cursor = None
    for call in svc.update_schedule.await_args_list:
        if "github_cursor" in call.kwargs:
            persisted_cursor = call.kwargs["github_cursor"]
    assert persisted_cursor == "2026-07-07T10:00:00Z"

    # Simulate the next tick's poll with the persisted cursor.
    schedule_next = {**schedule, "github_cursor": persisted_cursor}
    items = await gh_mod.github_poll(schedule_next)
    assert [i.event["pr_number"] for i in items] == [2]
