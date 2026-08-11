# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A github_poll trigger is consumed only if a process was started for it.

The cursor advance rides the occurrence insert in one transaction, durably
ahead of the spawn, so a poll that crashes mid-flight cannot re-fire events
that already ran. That at-most-once boundary is correct for anything that got
dispatched, and these tests pin it.

It must not apply to refusals that happen before dispatch. An execution root
that no longer resolves, an action that cannot be turned into a command line,
or a shutdown that lands before the child exists all leave nothing running, so
the event stays available and the next poll offers it again. Both halves are
pinned here: a pre-dispatch refusal leaves the cursor where it was, a
post-dispatch failure still moves it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.state.reasons import RunReasons
from lionagi.studio.scheduler.engine import SchedulerEngine
from lionagi.studio.scheduler.github import GithubPollItem, GithubPollResult

CURSOR_AT_TICK_START = "2026-07-07T09:00:00Z"
FIRST_EVENT_AT = "2026-07-07T10:00:00Z"
SECOND_EVENT_AT = "2026-07-07T11:00:00Z"


def _minimal_schedule(**overrides) -> dict:
    base = {
        "id": "sched-001",
        "name": "test-sched",
        "trigger_type": "github_poll",
        "github_repo": "acme/widgets",
        "github_cursor": CURSOR_AT_TICK_START,
        "action_kind": "agent",
        "action_model": "gpt-4.1-mini",
        "action_prompt": "handle {{pr_number}}",
        "action_agent": None,
        "action_playbook": None,
        # An execution root that resolves, so only the tests that mean to
        # break it break it.
        "action_cwd": "/",
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
    svc.create_schedule_run_and_advance = AsyncMock()
    svc.schedule_run_exists_since = AsyncMock(return_value=False)
    svc.update_schedule_run = AsyncMock()
    svc.create_invocation = AsyncMock()
    svc.update_invocation = AsyncMock()
    svc.update_status = AsyncMock()
    svc.list_sessions_for_invocation = AsyncMock(return_value=[])
    svc.count_schedule_runs = AsyncMock(return_value=0)
    svc.get_invocation = AsyncMock(return_value=None)
    svc.compute_files_overlap = AsyncMock(return_value={"count": 0, "top": []})
    return svc


def _item(pr_number: int, updated_at: str) -> GithubPollItem:
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
        dispatchable=True,
    )


def _poll(*items: GithubPollItem):
    return patch(
        "lionagi.studio.scheduler.github.github_poll",
        new=AsyncMock(return_value=GithubPollResult(items=list(items), scan_complete=True)),
    )


def _build_argv_ok():
    return patch(
        "lionagi.studio.scheduler.subprocess.build_argv",
        return_value=(["uv", "run", "li", "agent", "ping"], None),
    )


def _cursor_values_written(svc: AsyncMock) -> list[str]:
    """Every github_cursor value this engine tried to persist, from both
    write paths: the atomic occurrence-insert fold-in and the batched
    trailing write _tick_github does for filtered/undispatched tails."""
    written = [
        call.kwargs["schedule_fields"]["github_cursor"]
        for call in svc.create_schedule_run_and_advance.await_args_list
        if "github_cursor" in call.kwargs["schedule_fields"]
    ]
    written += [
        call.kwargs["github_cursor"]
        for call in svc.update_schedule.await_args_list
        if "github_cursor" in call.kwargs
    ]
    return written


def _run_status_calls(svc: AsyncMock, status: str) -> list:
    return [
        c
        for c in svc.update_status.await_args_list
        if c.args[:1] == ("schedule_run",) and c.kwargs.get("new_status") == status
    ]


# Pre-dispatch refusal: unresolvable execution root


@pytest.mark.asyncio
async def test_unresolvable_execution_root_leaves_the_cursor_unmoved():
    """The resolver refuses rather than run the action under a substituted
    working directory. Nothing was dispatched, so the event must survive."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(action_cwd="/nonexistent/pruned-worktree")

    spawn = AsyncMock(return_value=(0, ""))
    with (
        _poll(_item(1, FIRST_EVENT_AT)),
        _build_argv_ok(),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=spawn),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    spawn.assert_not_awaited()
    assert _cursor_values_written(svc) == []

    # The run record for the refusal is unchanged: failed, with the reason
    # code that names the refusal, and no exit code because nothing ran.
    failed = _run_status_calls(svc, "failed")
    assert len(failed) == 1
    assert failed[0].kwargs["reason_code"] == RunReasons.FAILED_CWD_INHERIT_REFUSED
    inserted = svc.create_schedule_run_and_advance.await_args_list[0].args[0]
    assert inserted["status"] == "failed"
    assert inserted.get("exit_code") is None


@pytest.mark.asyncio
async def test_unresolvable_execution_root_still_advances_next_fire_at():
    """Only the event-consuming cursor is held back. A cron schedule's own
    clock still moves, so a refusing schedule does not spin on one tick."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(action_cwd="/nonexistent/pruned-worktree")

    with (
        _poll(_item(1, FIRST_EVENT_AT)),
        _build_argv_ok(),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=AsyncMock()),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    fields = svc.create_schedule_run_and_advance.await_args_list[0].kwargs["schedule_fields"]
    assert fields["last_fired_at"] > 0
    assert "github_cursor" not in fields


@pytest.mark.asyncio
async def test_refusal_stops_the_poll_without_consuming_later_events():
    """A refusal is a property of the schedule, not of the event, so the rest
    of the batch would refuse identically. Stop, and leave every event of the
    batch available."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(action_cwd="/nonexistent/pruned-worktree")

    with (
        _poll(_item(1, FIRST_EVENT_AT), _item(2, SECOND_EVENT_AT)),
        _build_argv_ok(),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=AsyncMock()),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    assert svc.create_invocation.await_count == 1
    assert _cursor_values_written(svc) == []


@pytest.mark.asyncio
async def test_unbuildable_action_args_leave_the_cursor_unmoved():
    """The other pre-dispatch refusal: the action cannot be turned into a
    command line, so no process is ever started for the event."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        _poll(_item(1, FIRST_EVENT_AT)),
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            side_effect=ValueError("bad action_kind"),
        ),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=AsyncMock()),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    assert _cursor_values_written(svc) == []
    failed = _run_status_calls(svc, "failed")
    assert len(failed) == 1
    assert failed[0].kwargs["reason_code"] == RunReasons.FAILED_EXCEPTION


# Post-dispatch failure: at-most-once is preserved


@pytest.mark.asyncio
async def test_nonzero_exit_still_advances_the_cursor():
    """A process ran and failed. Re-firing it would be a re-execution, which
    is exactly the hazard the atomic advance exists to prevent."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        _poll(_item(1, FIRST_EVENT_AT)),
        _build_argv_ok(),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(3, "boom")),
        ),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    fields = svc.create_schedule_run_and_advance.await_args_list[0].kwargs["schedule_fields"]
    assert fields["github_cursor"] == FIRST_EVENT_AT
    failed = _run_status_calls(svc, "failed")
    assert len(failed) == 1
    assert failed[0].kwargs["reason_code"] == RunReasons.FAILED_EXIT_NONZERO


@pytest.mark.asyncio
async def test_successful_run_advances_the_cursor():
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        _poll(_item(1, FIRST_EVENT_AT), _item(2, SECOND_EVENT_AT)),
        _build_argv_ok(),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    advanced = [
        call.kwargs["schedule_fields"]["github_cursor"]
        for call in svc.create_schedule_run_and_advance.await_args_list
    ]
    assert advanced == [FIRST_EVENT_AT, SECOND_EVENT_AT]


# Cancellation: which side of the split it lands on depends on the process


@pytest.mark.asyncio
async def test_cancellation_before_launch_leaves_the_run_for_startup_recovery():
    """A shutdown that lands after the occurrence committed but before the
    child exists leaves the run exactly as a crash in that window would:
    still "running", never dispatched. That is what startup recovery re-fires
    from the run's own trigger_context, so the event is not spent. The
    cancellation still propagates -- the daemon has to shut down."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    async def _cancel_before_launch(*args, **kwargs):
        raise asyncio.CancelledError()

    with (
        _build_argv_ok(),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=_cancel_before_launch),
    ):
        with pytest.raises(asyncio.CancelledError):
            await engine._fire(
                schedule,
                "run-cancel-pre",
                trigger_context={"github_events": [_item(1, FIRST_EVENT_AT).event]},
                extra_schedule_fields={"github_cursor": FIRST_EVENT_AT},
            )

    # No terminal write, so the row stays in the undispatched-recovery lane.
    assert _run_status_calls(svc, "cancelled") == []
    svc.update_schedule_run.assert_not_awaited()
    inserted = svc.create_schedule_run_and_advance.await_args_list[0].args[0]
    assert inserted["status"] == "running"
    assert "dispatched_at" not in inserted


@pytest.mark.asyncio
async def test_cancellation_after_launch_still_records_a_cancelled_run():
    """The other side: the child process exists, so something ran. The run is
    recorded terminally and its trigger stays consumed."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    async def _cancel_after_launch(*args, on_launched=None, **kwargs):
        await on_launched()
        raise asyncio.CancelledError()

    with (
        _build_argv_ok(),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=_cancel_after_launch),
    ):
        with pytest.raises(asyncio.CancelledError):
            await engine._fire(
                schedule,
                "run-cancel-post",
                trigger_context={"github_events": [_item(1, FIRST_EVENT_AT).event]},
                extra_schedule_fields={"github_cursor": FIRST_EVENT_AT},
            )

    cancelled = _run_status_calls(svc, "cancelled")
    assert len(cancelled) == 1
    assert cancelled[0].kwargs["reason_code"] == RunReasons.CANCELLED_SYSTEM
    fields = svc.create_schedule_run_and_advance.await_args_list[0].kwargs["schedule_fields"]
    assert fields["github_cursor"] == FIRST_EVENT_AT


# The refusal is not always a property of the schedule: bounded retry


def _poison_command_schedule(**overrides) -> dict:
    """A command schedule whose one argument is rendered from the event's own
    PR title -- so whether argv can be built at all depends on which event is
    being fired, not on the schedule alone."""
    base = _minimal_schedule(
        action_kind="command",
        action_command="echo",
        action_command_args=["{{pr_title}}"],
        action_extra_args=[],
    )
    base.update(overrides)
    return base


def _titled(pr_number: int, updated_at: str, title: str) -> GithubPollItem:
    item = _item(pr_number, updated_at)
    item.event["pr_title"] = title
    return item


# Renders to a leading '-', which build_argv rejects as flag injection.
POISON_TITLE = "-dangerous"
SAFE_TITLE = "a safe title"


@pytest.fixture
def _command_allowlisted(monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "echo")


@pytest.mark.asyncio
async def test_event_specific_argv_failure_retries_before_the_limit(_command_allowlisted):
    """Event 1's title cannot be rendered into an argument; event 2's can.
    Below the refusal limit the poll still holds the cursor at event 1 and
    stops, so the refusal is retried loudly rather than swallowed."""
    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _poison_command_schedule()

    spawn = AsyncMock(return_value=(0, ""))
    with (
        _poll(
            _titled(1, FIRST_EVENT_AT, POISON_TITLE),
            _titled(2, SECOND_EVENT_AT, SAFE_TITLE),
        ),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=spawn),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    spawn.assert_not_awaited()
    assert _cursor_values_written(svc) == []
    # The streak is recorded against the event it is holding back, so a later
    # poll can tell a repeat of the same refusal from a fresh one.
    streak = [
        c for c in svc.update_schedule.await_args_list if "predispatch_refusal_count" in c.kwargs
    ]
    assert len(streak) == 1
    assert streak[0].kwargs["predispatch_refusal_count"] == 1
    assert streak[0].kwargs["predispatch_refusal_event"] == FIRST_EVENT_AT


@pytest.mark.asyncio
async def test_event_specific_argv_failure_progresses_at_the_limit(_command_allowlisted):
    """At the limit the same refusal is taken as terminal for that one event:
    the cursor moves past it and the next event -- which renders fine -- is
    dispatched, so one poison title cannot block the queue behind it."""
    from lionagi.studio.scheduler.engine import _MAX_PREDISPATCH_REFUSALS

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _poison_command_schedule(
        predispatch_refusal_event=FIRST_EVENT_AT,
        predispatch_refusal_count=_MAX_PREDISPATCH_REFUSALS - 1,
    )

    spawn = AsyncMock(return_value=(0, ""))
    with (
        _poll(
            _titled(1, FIRST_EVENT_AT, POISON_TITLE),
            _titled(2, SECOND_EVENT_AT, SAFE_TITLE),
        ),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=spawn),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    # Event 2 actually ran, with its own rendered argument.
    spawn.assert_awaited_once()
    assert spawn.await_args.args[0] == ["echo", SAFE_TITLE]
    # The cursor is now past BOTH events -- event 1 because its refusal was
    # taken as terminal, event 2 because it was dispatched. (Event 1's own
    # advance is never written on its own: event 2's fire commits the later
    # value in the same poll.)
    written = _cursor_values_written(svc)
    assert written
    assert all(v == SECOND_EVENT_AT for v in written)
    assert SECOND_EVENT_AT > FIRST_EVENT_AT
    # The streak is cleared once the cursor is past the event it counted.
    cleared = [
        c
        for c in svc.update_schedule.await_args_list
        if c.kwargs.get("predispatch_refusal_count") == 0
    ]
    assert cleared
    assert cleared[0].kwargs["predispatch_refusal_event"] is None


# Committed occurrence, no launch: the row stays in the recovery lane


@pytest.mark.asyncio
async def test_pre_launch_status_write_failure_leaves_the_run_recoverable():
    """The running-status write between the occurrence commit and the spawn is
    a separate awaited call. If it raises, the cursor is already spent, so
    finalizing the run would strand the event with nothing ever launched for
    it. The row must stay in the undispatched-recovery lane instead."""
    svc = _make_svc()

    async def _fail_the_pre_launch_status(entity_type, entity_id, **kwargs):
        if entity_type == "schedule_run" and kwargs.get("new_status") == "running":
            raise RuntimeError("state service unavailable")

    svc.update_status = AsyncMock(side_effect=_fail_the_pre_launch_status)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    spawn = AsyncMock(return_value=(0, ""))
    with (
        _poll(_item(1, FIRST_EVENT_AT)),
        _build_argv_ok(),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=spawn),
    ):
        await engine._tick_github(schedule, now=10_000.0)

    spawn.assert_not_awaited()
    # Committed as running with the cursor advance, and never stamped
    # dispatched -- exactly the shape startup recovery scans for.
    inserted = svc.create_schedule_run_and_advance.await_args_list[0].args[0]
    assert inserted["status"] == "running"
    assert inserted.get("dispatched_at") is None
    # Only this event's own advance, committed with the occurrence (the
    # trailing batched write re-states the same value).
    assert set(_cursor_values_written(svc)) == {FIRST_EVENT_AT}
    # Nothing terminal was written for it, so it is still in the lane.
    assert _run_status_calls(svc, "failed") == []
    assert _run_status_calls(svc, "cancelled") == []


@pytest.mark.asyncio
async def test_run_undispatched_by_a_pre_launch_failure_is_refired_on_startup():
    """The other half of the same claim: a row left that way is re-fired from
    its own trigger context by the startup scan, so the event is not lost."""
    svc = _make_svc()
    schedule = _minimal_schedule()
    svc.get_schedule = AsyncMock(return_value={**schedule, "enabled": 1})
    svc.list_undispatched_schedule_runs = AsyncMock(
        return_value=[
            {
                "id": "run-stranded",
                "schedule_id": schedule["id"],
                "chain_depth": 0,
                "trigger_context": {"github_events": [_item(1, FIRST_EVENT_AT).event]},
            }
        ]
    )
    engine = SchedulerEngine(svc=svc)

    spawn = AsyncMock(return_value=(0, ""))
    with (
        _build_argv_ok(),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=spawn),
    ):
        await engine._recover_undispatched_fires()
        await asyncio.gather(*list(engine._fire_tasks), return_exceptions=True)

    spawn.assert_awaited_once()
    # Re-fired atomically with tombstoning the stranded row it replaces.
    svc.tombstone_and_replace_schedule_run.assert_awaited_once()
    orphan_id, replacement = svc.tombstone_and_replace_schedule_run.await_args.args
    assert orphan_id == "run-stranded"
    assert replacement["trigger_context"]["github_events"][0]["pr_number"] == 1
