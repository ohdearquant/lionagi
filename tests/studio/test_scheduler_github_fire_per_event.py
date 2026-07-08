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
from lionagi.studio.scheduler.github import GithubPollItem, GithubPollResult


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
        patch(
            "lionagi.studio.scheduler.github.github_poll",
            new=AsyncMock(return_value=GithubPollResult(items=polled, scan_complete=True)),
        ),
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
        patch(
            "lionagi.studio.scheduler.github.github_poll",
            new=AsyncMock(return_value=GithubPollResult(items=polled, scan_complete=True)),
        ),
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
        patch(
            "lionagi.studio.scheduler.github.github_poll",
            new=AsyncMock(return_value=GithubPollResult(items=polled, scan_complete=True)),
        ),
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
        patch(
            "lionagi.studio.scheduler.github.github_poll",
            new=AsyncMock(return_value=GithubPollResult(items=polled, scan_complete=True)),
        ),
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
        patch(
            "lionagi.studio.scheduler.github.github_poll",
            new=AsyncMock(return_value=GithubPollResult(items=polled, scan_complete=True)),
        ),
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


def _pr(number, updated, *, state="open", merged_at=None):
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "user": {"login": "octocat"},
        "updated_at": updated,
        "draft": False,
        "head": {"sha": f"sha{number}"},
        "state": state,
        "merged_at": merged_at,
    }


class _FakeResp:
    def __init__(self, prs, link=None):
        self._prs = prs
        self.status_code = 200
        self.headers = {"x-ratelimit-remaining": "100", "etag": '"abc"'}
        if link:
            self.headers["link"] = link

    def json(self):
        return self._prs


class _FakeClient:
    def __init__(self, prs):
        self._prs = prs

    async def get(self, url, headers=None, params=None):
        return _FakeResp(self._prs)


class _FakePaginatedClient:
    """Serves a fixed page sequence, each carrying a Link: rel="next" header
    except the last -- mirrors the fake used in test_github_poller.py for
    exercising github_poll()'s merged-mode pagination loop end to end."""

    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self.requests: list[dict] = []

    async def get(self, url, headers=None, params=None):
        page_index = len(self.requests)
        self.requests.append({"url": url, "params": params})
        prs = self._pages[page_index] if page_index < len(self._pages) else []
        has_next = page_index + 1 < len(self._pages)
        link = None
        if has_next:
            next_url = f"https://api.github.com/repos/acme/widgets/pulls?page={page_index + 2}"
            link = f'<{next_url}>; rel="next"'
        return _FakeResp(prs, link=link)


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
    items = (await gh_mod.github_poll(schedule_next)).items
    assert [i.event["pr_number"] for i in items] == [2]


# ---------------------------------------------------------------------------
# Truncated merged-mode scan: no permanent skip, no duplicate dispatch
# across two consecutive ticks.
# ---------------------------------------------------------------------------


def _closed_page(hour: int, base_number: int, *, merges: dict[int, str] | None = None):
    """20 closed PRs at a fixed hour, minutes descending -- one fake
    "page" of a merged-mode poll response. ``merges`` maps a within-page
    index to a merged_at value for that PR (closed-but-unmerged otherwise)."""
    merges = merges or {}
    items = []
    for i in range(20):
        minute = 59 - i * 3
        updated_at = f"2026-07-06T{hour:02d}:{minute:02d}:00Z"
        items.append(_pr(base_number + i, updated_at, state="closed", merged_at=merges.get(i)))
    return items


@pytest.mark.asyncio
async def test_merged_mode_truncated_scan_no_skip_no_duplicate_across_two_ticks(monkeypatch):
    """A merged-mode poll that hits the page cap must not permanently skip an
    event too close to the unproven boundary, nor re-fire an event it already
    dispatched, once that event becomes safely reachable on a later poll."""
    schedule = _minimal_schedule(github_filter={"event": "pr_merged"})

    # Tick 1: 5 full pages of closed PRs (hits _MERGED_MODE_MAX_PAGES). The
    # 5th page itself is full and links to a 6th (never fetched) page, so it
    # is genuinely unsafe -- not a short/terminal page. PR 1405 sits mid
    # page 5, merged_at close to the truncation boundary -- unsafe to
    # dispatch this poll. PR 1410 merged long before the fetched window --
    # safely below the boundary, dispatchable this poll.
    pages_tick1 = [
        _closed_page(15, 1000),
        _closed_page(14, 1100),
        _closed_page(13, 1200),
        _closed_page(12, 1300),
        _closed_page(
            11,
            1400,
            merges={5: "2026-07-06T11:44:00Z", 10: "2020-01-01T00:00:00Z"},
        ),
        _closed_page(10, 1500),  # 6th page: proves page 5 had a real next link.
    ]

    async def _fake_token():
        return "faketoken"

    monkeypatch.setattr(gh_mod, "_get_gh_token", _fake_token)
    monkeypatch.setattr(gh_mod, "_get_client", lambda: _FakePaginatedClient(pages_tick1))

    svc = _make_svc()
    fired_prs: list[int] = []

    async def _create_schedule_run(payload):
        fired_prs.append(payload["trigger_context"]["github_events"][0]["pr_number"])

    svc.create_schedule_run = AsyncMock(side_effect=_create_schedule_run)
    svc.count_schedule_runs = AsyncMock(side_effect=lambda *a, **k: len(fired_prs))

    engine = SchedulerEngine(svc=svc)

    p_build, p_spawn = _spawn_patches()
    with p_build, p_spawn:
        await engine._tick_github(schedule, now=10_000.0)

    # Only the safely-below-the-boundary PR fired. PR 1405 (unsafe) did not.
    assert fired_prs == [1410]

    persisted_cursor = None
    for call in svc.update_schedule.await_args_list:
        if "github_cursor" in call.kwargs:
            persisted_cursor = call.kwargs["github_cursor"]
    assert persisted_cursor == "2020-01-01T00:00:00Z"

    # Tick 2: the underlying data has moved on (real GitHub would no longer
    # surface the now-stale closed-unmerged noise ahead of PR 1405) -- a
    # single short page is enough this time, so the scan completes safely.
    pages_tick2 = [
        [
            _pr(2000, "2026-07-06T12:00:00Z", state="closed", merged_at=None),
            _pr(1405, "2026-07-06T11:44:00Z", state="closed", merged_at="2026-07-06T11:44:00Z"),
        ]
    ]
    monkeypatch.setattr(gh_mod, "_get_client", lambda: _FakePaginatedClient(pages_tick2))

    schedule_next = {**schedule, "github_cursor": persisted_cursor, "last_fired_at": 10_000.0}
    p_build2, p_spawn2 = _spawn_patches()
    with p_build2, p_spawn2:
        await engine._tick_github(schedule_next, now=20_000.0)

    # PR 1405 is now dispatched exactly once; PR 1410 (already dispatched in
    # tick 1) is not re-fired.
    assert fired_prs == [1410, 1405]


# ---------------------------------------------------------------------------
# Observer self-health: _tick_github stamps last_healthy_poll_at /
# poller_consecutive_401 from GithubPollResult.poll_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_github_ok_poll_stamps_healthy_and_resets_401_counter():
    """A healthy poll (even an empty one) sets last_healthy_poll_at = now and
    zeroes the 401 counter -- this is the reset half of the self-health
    signal, so a token fix (or a quiet repo) clears a prior alarm."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(poller_consecutive_401=3)

    with patch(
        "lionagi.studio.scheduler.github.github_poll",
        new=AsyncMock(
            return_value=GithubPollResult(items=[], scan_complete=True, poll_status="ok")
        ),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    svc.update_schedule.assert_any_call(
        "sched-001", last_healthy_poll_at=10_000.0, poller_consecutive_401=0
    )


@pytest.mark.asyncio
async def test_tick_github_ok_poll_with_items_also_stamps_healthy():
    """A healthy poll that finds items still stamps the health columns --
    dispatching PRs doesn't skip the self-health write."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()
    polled = [_item(1, "2026-07-07T10:00:00Z")]

    p_build, p_spawn = _spawn_patches()
    with (
        patch(
            "lionagi.studio.scheduler.github.github_poll",
            new=AsyncMock(
                return_value=GithubPollResult(items=polled, scan_complete=True, poll_status="ok")
            ),
        ),
        p_build,
        p_spawn,
    ):
        await engine._tick_github(schedule, now=10_000.0)

    svc.update_schedule.assert_any_call(
        "sched-001", last_healthy_poll_at=10_000.0, poller_consecutive_401=0
    )


@pytest.mark.asyncio
async def test_tick_github_auth_error_increments_401_counter_leaves_healthy_at_untouched():
    """A 401 (surviving the gh-CLI-token fallback) increments the
    consecutive-401 counter from the schedule's prior value, and does NOT
    touch last_healthy_poll_at -- the blind clock keeps climbing."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(poller_consecutive_401=2)

    with patch(
        "lionagi.studio.scheduler.github.github_poll",
        new=AsyncMock(
            return_value=GithubPollResult(items=[], scan_complete=True, poll_status="auth_error")
        ),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    svc.update_schedule.assert_any_call("sched-001", poller_consecutive_401=3)
    for call in svc.update_schedule.await_args_list:
        assert "last_healthy_poll_at" not in call.kwargs


@pytest.mark.asyncio
async def test_tick_github_auth_error_first_401_counts_from_zero():
    """A schedule with no prior poller_consecutive_401 (fresh schedule, key
    absent from the dict) starts the counter at 1 on its first 401, not a
    crash on a missing key."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()  # no poller_consecutive_401 key at all

    with patch(
        "lionagi.studio.scheduler.github.github_poll",
        new=AsyncMock(
            return_value=GithubPollResult(items=[], scan_complete=True, poll_status="auth_error")
        ),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    svc.update_schedule.assert_any_call("sched-001", poller_consecutive_401=1)


@pytest.mark.asyncio
async def test_tick_github_network_error_leaves_health_columns_untouched():
    """A network/config failure ('error') writes nothing -- neither counter
    nor healthy timestamp move, so the age metric climbs purely from the
    passage of time since the last real healthy poll."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with patch(
        "lionagi.studio.scheduler.github.github_poll",
        new=AsyncMock(
            return_value=GithubPollResult(items=[], scan_complete=True, poll_status="error")
        ),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    svc.update_schedule.assert_not_awaited()
