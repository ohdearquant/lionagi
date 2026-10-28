# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for SchedulerEngine._fire() and helpers via a mocked service."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from lionagi.state.db import NO_CURSOR_CLAIM
from lionagi.studio.services.schedules import _run_trigger_facts
from tests._scheduler_claims import claim_holds, fire_with_claim, persisting_update_schedule

NY = ZoneInfo("America/New_York")


async def _cancel_after_launch(*args, on_launched=None, **kwargs):
    """spawn_and_wait double for a cancellation that arrives once the child
    process already exists.

    Calling ``on_launched`` first is what makes this the post-dispatch case:
    something ran, so the run is recorded as cancelled and its trigger stays
    consumed. A cancellation that arrives before the process exists takes the
    other branch entirely.
    """
    if on_launched is not None:
        await on_launched()
    raise asyncio.CancelledError()


# Helpers


def _minimal_schedule(**overrides) -> dict:
    base = {
        "id": "sched-001",
        "name": "test-sched",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_model": "gpt-4.1-mini",
        "action_prompt": "ping",
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
    """Return an AsyncMock that satisfies SchedulerStateService."""
    svc = AsyncMock()
    svc.get_schedule = AsyncMock(return_value=None)
    svc.list_schedules = AsyncMock(return_value=[])
    svc.update_schedule = AsyncMock()
    svc.create_schedule_run = AsyncMock()
    svc.create_schedule_run_and_advance = AsyncMock()
    svc.schedule_run_exists_since = AsyncMock(return_value=False)
    svc.list_undispatched_schedule_runs = AsyncMock(return_value=[])
    svc.update_schedule_run = AsyncMock()
    svc.create_invocation = AsyncMock()
    svc.update_invocation = AsyncMock()
    svc.update_status = AsyncMock()
    svc.list_sessions_for_invocation = AsyncMock(return_value=[])
    svc.count_schedule_runs = AsyncMock(return_value=0)
    svc.get_invocation = AsyncMock(return_value=None)
    svc.compute_files_overlap = AsyncMock(return_value={"count": 0, "top": []})
    return svc


# resolve_invocation_terminal tests (pure-logic, no DB)


@pytest.mark.asyncio
async def test_resolve_terminal_completed_ok():
    from lionagi.studio.services.scheduler_state import resolve_invocation_terminal

    svc = _make_svc()
    svc.list_sessions_for_invocation.return_value = [
        {"id": "s1", "status": "completed"},
        {"id": "s2", "status": "completed"},
    ]
    status, rc, rs, refs, meta = await resolve_invocation_terminal(
        svc, "inv-1", fallback_status="completed"
    )
    assert status == "completed"


@pytest.mark.asyncio
async def test_resolve_terminal_completed_empty_child_taints_invocation():
    """A completed_empty child (completion-trust gate) must not be silently
    averaged away by a sibling's real completion — the invocation as a whole
    stays untrustworthy so schedule on_fail chaining can see it."""
    from lionagi.studio.services.scheduler_state import resolve_invocation_terminal

    svc = _make_svc()
    svc.list_sessions_for_invocation.return_value = [
        {"id": "s1", "status": "completed"},
        {"id": "s2", "status": "completed_empty"},
    ]
    status, rc, rs, refs, meta = await resolve_invocation_terminal(
        svc, "inv-1", fallback_status="completed"
    )
    assert status == "completed_empty"


@pytest.mark.asyncio
async def test_resolve_terminal_nonterminal_child_not_trusted_as_completed():
    """A leader process exiting 0 is not evidence that a still-running child
    session's own work finished (the terminal stamp today comes from the
    leader's stderr pipe closing, not from the work ending). A
    child session that has not reached ANY terminal status must not be
    silently trusted via the fallback_status="completed" path -- it belongs
    on completed_empty (no positive evidence), the same bucket a
    known-empty child already uses."""
    from lionagi.state.reasons import RunReasons
    from lionagi.studio.services.scheduler_state import resolve_invocation_terminal

    svc = _make_svc()
    svc.list_sessions_for_invocation.return_value = [
        {"id": "s1", "status": "running"},
    ]
    status, rc, rs, refs, meta = await resolve_invocation_terminal(
        svc, "inv-1", fallback_status="completed", exit_code=0
    )
    assert status == "completed_empty"
    assert rc == RunReasons.COMPLETED_EMPTY_NO_EVIDENCE


@pytest.mark.asyncio
async def test_resolve_terminal_all_children_terminal_completed_still_trusted():
    """Positive control for the above: when every child session has
    genuinely reached a terminal 'completed' status, the invocation is
    still trusted as 'completed' -- the new non-terminal-child guard must
    not fire when there is nothing left running."""
    from lionagi.studio.services.scheduler_state import resolve_invocation_terminal

    svc = _make_svc()
    svc.list_sessions_for_invocation.return_value = [
        {"id": "s1", "status": "completed"},
        {"id": "s2", "status": "completed"},
    ]
    status, rc, rs, refs, meta = await resolve_invocation_terminal(
        svc, "inv-1", fallback_status="completed", exit_code=0
    )
    assert status == "completed"


@pytest.mark.asyncio
async def test_resolve_terminal_failed_child():
    from lionagi.studio.services.scheduler_state import resolve_invocation_terminal

    svc = _make_svc()
    svc.list_sessions_for_invocation.return_value = [
        {"id": "s1", "status": "completed"},
        {"id": "s2", "status": "failed"},
    ]
    status, rc, rs, refs, meta = await resolve_invocation_terminal(
        svc, "inv-1", fallback_status="failed"
    )
    assert status == "failed"


@pytest.mark.asyncio
async def test_resolve_terminal_timed_out_child():
    from lionagi.studio.services.scheduler_state import resolve_invocation_terminal

    svc = _make_svc()
    svc.list_sessions_for_invocation.return_value = [{"id": "s1", "status": "timed_out"}]
    status, *_ = await resolve_invocation_terminal(svc, "inv-1", fallback_status="completed")
    assert status == "timed_out"


@pytest.mark.asyncio
async def test_resolve_terminal_no_sessions_fallback_completed():
    from lionagi.studio.services.scheduler_state import resolve_invocation_terminal

    svc = _make_svc()
    svc.list_sessions_for_invocation.return_value = []
    status, *_ = await resolve_invocation_terminal(svc, "inv-1", fallback_status="completed")
    assert status == "completed"


@pytest.mark.asyncio
async def test_resolve_terminal_no_sessions_fallback_failed_exception():
    from lionagi.studio.services.scheduler_state import resolve_invocation_terminal

    svc = _make_svc()
    svc.list_sessions_for_invocation.return_value = []
    exc = RuntimeError("boom")
    status, rc, rs, refs, meta = await resolve_invocation_terminal(
        svc, "inv-1", fallback_status="failed", exception=exc
    )
    assert status == "failed"
    assert "RuntimeError" in rs


@pytest.mark.asyncio
async def test_resolve_terminal_nonzero_exit():
    from lionagi.studio.services.scheduler_state import resolve_invocation_terminal

    svc = _make_svc()
    svc.list_sessions_for_invocation.return_value = []
    status, rc, rs, refs, meta = await resolve_invocation_terminal(
        svc, "inv-1", fallback_status="failed", exit_code=1
    )
    assert status == "failed"
    assert "1" in rs


@pytest.mark.asyncio
async def test_resolve_terminal_cancelled():
    from lionagi.studio.services.scheduler_state import resolve_invocation_terminal

    svc = _make_svc()
    svc.list_sessions_for_invocation.return_value = []
    status, *_ = await resolve_invocation_terminal(svc, "inv-1", fallback_status="cancelled")
    assert status == "cancelled"


# SchedulerEngine._fire() — happy path (exit_code=0)


@pytest.mark.asyncio
async def test_fire_happy_path_records_invocation_and_run():
    """_fire() creates an invocation, schedule_run, updates status and schedule."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

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
        await fire_with_claim(engine, schedule, "run-001", trigger_context={"scheduled": True})

    svc.create_invocation.assert_awaited_once()
    # Occurrence-insert + cursor-advance land together, atomically, through
    # create_schedule_run_and_advance() -- not two separate service calls.
    svc.create_schedule_run.assert_not_awaited()
    svc.create_schedule_run_and_advance.assert_awaited_once()
    (run_payload,), kwargs = svc.create_schedule_run_and_advance.await_args
    assert run_payload["status"] == "running"
    assert kwargs["schedule_id"] == "sched-001"
    assert "last_fired_at" in kwargs["schedule_fields"]
    # The run's outcome columns are NOT written on their own. exit_code,
    # ended_at and error_detail belong to the terminal transition, so they ride
    # the guarded write and are subject to the same race check: a fire that
    # loses to a concurrent finalizer must leave the winner's values alone
    # rather than land its own beside a status it was refused.
    svc.update_schedule_run.assert_not_awaited()
    # update_status called for schedule_run AND invocation
    assert svc.update_status.await_count == 3  # running + completed + invocation
    # Only flush_run_telemetry's merge of node_metadata["coordination"]
    # remains; the ended_at stamp that used to precede the terminal write now
    # rides it. The metadata reaches the store as its own merge, so no
    # whole-column update_invocation() is issued for it.
    svc.update_invocation.assert_not_awaited()
    assert svc.merge_invocation_node_metadata.await_count == 1
    (created_invocation,), _ = svc.create_invocation.await_args
    (merge_inv_id, merge_patch), _ = svc.merge_invocation_node_metadata.await_args
    assert merge_inv_id == created_invocation["id"]
    # The patch is exactly the coordination key, carrying this run's signal
    # counters -- not a snapshot of the whole node_metadata column.
    assert set(merge_patch) == {"coordination"}
    assert merge_patch["coordination"]["signals"]["emitted"] == {"ScheduleRunSucceeded": 1}

    terminal_run = [
        call
        for call in svc.update_status.await_args_list
        if call.args[:2] == ("schedule_run", "run-001") and call.kwargs["new_status"] == "completed"
    ]
    assert len(terminal_run) == 1
    extra = terminal_run[0].kwargs["extra_fields"]
    assert set(extra) == {"exit_code", "ended_at", "error_detail"}
    assert extra["exit_code"] == 0
    assert extra["error_detail"] is None
    assert isinstance(extra["ended_at"], float) and extra["ended_at"] > 0
    assert terminal_run[0].kwargs["expected_statuses"] == {"running"}
    # update_schedule() itself isn't called for a plain fire -- its old job
    # (last_fired_at/next_fire_at) now rides create_schedule_run_and_advance's
    # schedule_fields above; update_schedule stays for other paths (backfill,
    # max_runs auto-disable, etc.).
    svc.update_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_fire_records_substituted_prompt_not_raw_template():
    """create_invocation's prompt field carries the {{var}}-substituted text
    actually sent, not the raw template stored on the schedule."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(action_prompt="review PR {{pr_number}}")

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "review PR 42"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await fire_with_claim(engine, schedule, "run-002", trigger_context={"pr_number": "42"})

    svc.create_invocation.assert_awaited_once()
    (invocation_payload,), _kwargs = svc.create_invocation.await_args
    assert invocation_payload["prompt"] == "review PR 42"


@pytest.mark.asyncio
async def test_fire_records_the_schedules_action_kind_on_the_invocation():
    """The invocation row has to say what kind of action it launched.

    The row was always written -- this call sits before every kind branch --
    but it carried no kind, so `li monitor` labelled a command-kind fire
    exactly like an agent one, and a fire that had happened read as a fire
    that had not. The `invocations` table has no column for the kind,
    so it rides node_metadata rather than a migration.
    """
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(action_kind="command")

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "probe.py"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await fire_with_claim(engine, schedule, "run-kind-001", trigger_context={})

    svc.create_invocation.assert_awaited_once()
    (invocation_payload,), _kwargs = svc.create_invocation.await_args
    assert invocation_payload["node_metadata"] == {"action_kind": "command"}


@pytest.mark.asyncio
async def test_fire_records_empty_rendered_prompt_as_is_not_playbook_fallback():
    """A template that renders to "" (e.g. an empty trigger_context value) is
    still what build_argv actually sends the child — it must not collapse
    into the action_playbook fallback, which would persist a value that
    differs from what was actually sent."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(action_prompt="{{payload}}", action_playbook="fallback-playbook")

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", ""], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await fire_with_claim(engine, schedule, "run-002b", trigger_context={"payload": ""})

    svc.create_invocation.assert_awaited_once()
    (invocation_payload,), _kwargs = svc.create_invocation.await_args
    assert invocation_payload["prompt"] == ""


@pytest.mark.asyncio
async def test_fire_executable_resolution_failure_records_failed_run_with_actionable_detail():
    """When resolve_li_executable() can't find an absolute `li` path, _fire()
    fails the schedule_run/invocation through the existing exception path with
    an error_detail naming what was tried — not a raw ENOENT from a bad spawn."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.resolve_li_executable",
            return_value=(None, "shutil.which found nothing; no venv-adjacent file"),
        ),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=AsyncMock()) as spawn_mock,
    ):
        await fire_with_claim(engine, schedule, "run-003", trigger_context={"scheduled": True})

    spawn_mock.assert_not_awaited()
    svc.create_schedule_run.assert_not_awaited()
    svc.create_schedule_run_and_advance.assert_awaited_once()
    (run_payload,), _kwargs = svc.create_schedule_run_and_advance.await_args
    assert run_payload["status"] == "failed"
    assert "resolve" in run_payload["error_detail"]
    assert "shutil.which" in run_payload["error_detail"]


@pytest.mark.asyncio
async def test_fire_nonzero_exit_records_failed_status():
    """Non-zero exit code produces a 'failed' schedule_run status."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(1, "error text")),
        ),
    ):
        await fire_with_claim(engine, schedule, "run-002", trigger_context={"scheduled": True})

    # Find the update_status call for "schedule_run" with new_status="failed"
    failed_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.args[0] == "schedule_run" and c.kwargs.get("new_status") == "failed"
    ]
    assert failed_calls, "Expected update_status('schedule_run', ..., new_status='failed')"


@pytest.mark.asyncio
async def test_fire_build_argv_exception_records_failed_run():
    """build_argv raising an exception records a failed run without calling spawn_and_wait."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            side_effect=ValueError("bad action_kind"),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(),
        ) as mock_spawn,
    ):
        await fire_with_claim(engine, schedule, "run-003", trigger_context={"scheduled": True})

    mock_spawn.assert_not_awaited()
    svc.create_schedule_run.assert_not_awaited()
    svc.create_schedule_run_and_advance.assert_awaited_once()
    failed_calls = [
        c for c in svc.update_status.await_args_list if c.kwargs.get("new_status") == "failed"
    ]
    assert failed_calls


@pytest.mark.asyncio
async def test_fire_cancellation_records_cancelled_run():
    """CancelledError propagates after recording a 'cancelled' run."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=_cancel_after_launch,
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await fire_with_claim(engine, schedule, "run-004", trigger_context={"scheduled": True})

    # "Scheduler shutdown" is a placeholder, not a measured cause, and it used
    # to be written unguarded ahead of the guarded transition. A run already
    # finalized by another writer (the deadline reaper recording a timeout, say)
    # would keep its real status while that placeholder replaced the cause it
    # recorded. The text now rides the guarded write, so losing the race writes
    # nothing at all.
    # on_launched's dispatched_at stamp is the only update_schedule_run write;
    # nothing writes a status or cause through it.
    assert svc.update_schedule_run.await_count == 1
    assert set(svc.update_schedule_run.await_args_list[0].kwargs) == {"dispatched_at"}
    cancelled_calls = [
        c for c in svc.update_status.await_args_list if c.kwargs.get("new_status") == "cancelled"
    ]
    assert cancelled_calls
    run_cancel = [c for c in cancelled_calls if c.args[:2] == ("schedule_run", "run-004")]
    assert len(run_cancel) == 1
    extra = run_cancel[0].kwargs["extra_fields"]
    assert extra["error_detail"] == "Scheduler shutdown"
    assert isinstance(extra["ended_at"], float) and extra["ended_at"] > 0
    assert run_cancel[0].kwargs["expected_statuses"] == {"running"}


@pytest.mark.asyncio
async def test_fire_inner_exception_records_failed_and_does_not_reraise():
    """Unexpected exception inside the main try block is caught, recorded, and swallowed."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    async def _raise_after_launch(*args, on_launched=None, **kwargs):
        # Confirms the launch first: a failure of something that started is
        # what gets recorded terminally. (An exception raised before any
        # process exists instead leaves the run for startup recovery.)
        if on_launched is not None:
            await on_launched()
        raise RuntimeError("unexpected")

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(side_effect=_raise_after_launch),
        ),
    ):
        # Should not raise
        await fire_with_claim(engine, schedule, "run-005", trigger_context={"scheduled": True})

    failed_calls = [
        c for c in svc.update_status.await_args_list if c.kwargs.get("new_status") == "failed"
    ]
    assert failed_calls


@pytest.mark.asyncio
async def test_fire_chain_depth_0_tracks_running():
    """chain_depth=0 adds the schedule to _running and removes it on completion."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()
    sid = schedule["id"]

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
        await fire_with_claim(engine, schedule, "run-006", trigger_context={}, chain_depth=0)

    assert sid not in engine._running


@pytest.mark.asyncio
async def test_fire_chain_depth_nonzero_does_not_track_running():
    """chain_depth>0 does not modify _running."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

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
        await fire_with_claim(
            engine,
            schedule,
            "run-007",
            trigger_context={},
            chain_depth=1,
            chain_parent_id="run-006",
        )

    assert schedule["id"] not in engine._running


@pytest.mark.asyncio
async def test_fire_on_success_chain_fires():
    """on_success chain action causes a recursive _fire() call."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        on_success={"kind": "agent", "prompt": "chained prompt", "model": "gpt-4.1-mini"}
    )

    fire_calls: list[tuple] = []
    original_fire = engine._fire

    async def _patched_fire(
        sched, run_id, *, trigger_context, chain_parent_id=None, chain_depth=0, **kw
    ):
        fire_calls.append((sched["id"], chain_depth))
        if chain_depth > 0:
            return
        return await original_fire(
            sched,
            run_id,
            trigger_context=trigger_context,
            chain_parent_id=chain_parent_id,
            chain_depth=chain_depth,
            **kw,
        )

    engine._fire = _patched_fire  # type: ignore[method-assign]

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
        await original_fire(
            schedule,
            "run-chain",
            trigger_context={},
            chain_depth=0,
            expect_next_fire_at=schedule.get("next_fire_at"),
        )

    # The chain should have been triggered
    chained = [c for c in fire_calls if c[1] == 1]
    assert chained, "Expected a chained _fire() call at depth=1"


@pytest.mark.asyncio
async def test_fire_running_child_with_on_success_does_not_run_success_chain():
    """A clean leader exit (exit_code=0) with a child session that has not
    independently reached a terminal status resolves to "completed_empty",
    not "completed" -- the leader's exit is not evidence the child's work
    finished. completed_empty is NOT success for scheduling purposes: the
    schedule_run row's status, reason_code, emitted signal class, and the
    on_success chain decision must all independently agree with that
    resolved outcome instead of the raw exit code."""
    from lionagi.state.reasons import RunReasons
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.list_sessions_for_invocation = AsyncMock(
        return_value=[{"id": "sess-1", "status": "running"}]
    )
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        on_success={"kind": "agent", "prompt": "chained prompt", "model": "gpt-4.1-mini"}
    )

    fire_calls: list[tuple] = []
    original_fire = engine._fire

    async def _patched_fire(
        sched, run_id, *, trigger_context, chain_parent_id=None, chain_depth=0, **kw
    ):
        fire_calls.append((sched["id"], chain_depth))
        if chain_depth > 0:
            return
        return await original_fire(
            sched,
            run_id,
            trigger_context=trigger_context,
            chain_parent_id=chain_parent_id,
            chain_depth=chain_depth,
            **kw,
        )

    engine._fire = _patched_fire  # type: ignore[method-assign]

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
        await original_fire(
            schedule,
            "run-empty-success",
            trigger_context={},
            chain_depth=0,
            expect_next_fire_at=schedule.get("next_fire_at"),
        )

    # Assertion 1: status. completed_empty must not be recorded as "completed".
    terminal_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.args[:2] == ("schedule_run", "run-empty-success")
        and c.kwargs.get("new_status") in ("completed", "failed")
    ]
    assert terminal_calls
    (call,) = terminal_calls
    assert call.kwargs["new_status"] == "failed"

    # Assertion 2: reason. The finer completed_empty distinction must survive
    # in the reason_code even though the coarse status is "failed".
    assert call.kwargs["reason_code"] == RunReasons.COMPLETED_EMPTY_NO_EVIDENCE

    # Assertion 3: signal class. build_schedule_run_signal() derives the
    # signal class from the same mapped status, so a completed_empty outcome
    # must mint ScheduleRunFailed, never ScheduleRunSucceeded.
    node_metadata_patches = [
        c.args[1] for c in svc.merge_invocation_node_metadata.await_args_list if len(c.args) > 1
    ]
    assert len(node_metadata_patches) == 1
    assert node_metadata_patches[0]["coordination"]["signals"]["emitted"] == {
        "ScheduleRunFailed": 1
    }

    # Assertion 4: chaining. on_success must not fire for a no-evidence outcome.
    chained = [c for c in fire_calls if c[1] == 1]
    assert not chained, "on_success must not fire for a completed_empty (no-evidence) outcome"


@pytest.mark.asyncio
async def test_fire_invocation_finalization_cas_miss_is_checked_and_does_not_raise():
    """A concurrent finalizer (e.g. the deadline reaper) may already have
    moved the invocation to a terminal status by the time _fire() records its
    own outcome. The write must be guarded (so a lost race is a checked
    no-op) and _fire() must not raise past that point — _check_max_runs()
    must still run."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()

    async def _update_status(entity_type, entity_id, *, new_status, **kwargs):
        if entity_type == "invocation":
            assert "expected_statuses" in kwargs, (
                "invocation terminal write must pass expected_statuses so a "
                "reaper-lost race is a checked no-op, not an unguarded write"
            )
            return False  # another writer already finalized this invocation
        return True

    svc.update_status = AsyncMock(side_effect=_update_status)
    engine = SchedulerEngine(svc=svc)
    # max_runs makes _check_max_runs() actually call count_schedule_runs(),
    # so its execution is directly observable as a side effect that must
    # survive the guarded, no-op invocation write above.
    schedule = _minimal_schedule(max_runs=100)

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
        await fire_with_claim(engine, schedule, "run-cas", trigger_context={"scheduled": True})

    svc.count_schedule_runs.assert_awaited()


@pytest.mark.asyncio
async def test_fire_exception_during_invocation_resolution_marks_run_failed_once():
    """resolve_invocation_terminal() is now consulted BEFORE the
    schedule_run terminal write in the normal path -- its resolved outcome
    must be used consistently for the schedule row's own status/reason, not
    only the invocation row (a completed_empty invocation must not let the
    schedule row or an on_success chain report success). A raise during
    that resolution therefore skips the normal-path schedule_run write
    entirely; the broad-except handler catches it, writes a single guarded
    'failed' schedule_run terminal status, and still finalizes the
    invocation via its own retried resolution call."""
    from lionagi.state.reasons import RunReasons
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    schedule_run_terminal_calls: list[dict] = []

    async def _update_status(entity_type, entity_id, *, new_status, **kwargs):
        if entity_type == "schedule_run" and new_status in ("completed", "failed"):
            schedule_run_terminal_calls.append(kwargs)
        return True

    svc.update_status = AsyncMock(side_effect=_update_status)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    # resolve_invocation_terminal() raises on its first call (now the very
    # first thing the normal path awaits after spawn_and_wait returns), but
    # the broad-except handler's own retry call to it must still succeed so
    # the test can observe the handler finalizing both rows in isolation.
    resolve_calls = {"n": 0}

    async def _resolve_invocation_terminal(*args, **kwargs):
        resolve_calls["n"] += 1
        if resolve_calls["n"] == 1:
            raise RuntimeError("boom")
        return ("failed", "run.failed.exception", "boom", [], {})

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
        patch(
            "lionagi.studio.scheduler.engine.resolve_invocation_terminal",
            new=AsyncMock(side_effect=_resolve_invocation_terminal),
        ),
    ):
        await fire_with_claim(engine, schedule, "run-late-exc", trigger_context={"scheduled": True})

    assert resolve_calls["n"] == 2, "the broad-except handler must retry invocation resolution"
    assert len(schedule_run_terminal_calls) == 1, (
        "the schedule_run terminal write only happens once, from the except handler, "
        "since resolving the invocation now gates the normal-path write itself"
    )
    assert schedule_run_terminal_calls[0]["reason_code"] == RunReasons.FAILED_EXCEPTION


@pytest.mark.asyncio
async def test_fire_chain_runs_when_terminal_write_loses_cas():
    """A lost CAS race on the invocation terminal write must not swallow
    on_success chaining — the chain still fires even though the write
    recording this run's own outcome was a no-op."""
    from lionagi.state.db import TransitionRejectedError
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()

    async def _update_status(entity_type, entity_id, *, new_status, **kwargs):
        if entity_type == "invocation":
            if "expected_statuses" not in kwargs:
                # Unguarded write against a row the reaper already finalized —
                # the real DB layer raises the terminal-status floor here.
                raise TransitionRejectedError("invocation", entity_id, "completed", new_status)
            return False
        return True

    svc.update_status = AsyncMock(side_effect=_update_status)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        on_success={"kind": "agent", "prompt": "chained prompt", "model": "gpt-4.1-mini"}
    )

    fire_calls: list[tuple] = []
    original_fire = engine._fire

    async def _patched_fire(
        sched, run_id, *, trigger_context, chain_parent_id=None, chain_depth=0, **kw
    ):
        fire_calls.append((sched["id"], chain_depth))
        if chain_depth > 0:
            return
        return await original_fire(
            sched,
            run_id,
            trigger_context=trigger_context,
            chain_parent_id=chain_parent_id,
            chain_depth=chain_depth,
            **kw,
        )

    engine._fire = _patched_fire  # type: ignore[method-assign]

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
        await original_fire(
            schedule,
            "run-chain-cas",
            trigger_context={},
            chain_depth=0,
            expect_next_fire_at=schedule.get("next_fire_at"),
        )

    chained = [c for c in fire_calls if c[1] == 1]
    assert chained, "on_success chain must still fire when the invocation write lost its CAS"


@pytest.mark.asyncio
async def test_fire_invalid_action_invocation_cas_miss_is_checked_and_does_not_raise():
    """The invalid-schedule-action branch (build_argv raising before any
    process is spawned) finalizes the invocation it already created as
    'running'. If a concurrent finalizer (e.g. the deadline reaper) wins that
    row first, the write must be guarded (expected_statuses) so a lost race
    is a checked no-op, not an unguarded write that raises past _fire() and
    drops _check_max_runs()."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()

    async def _update_status(entity_type, entity_id, *, new_status, **kwargs):
        if entity_type == "invocation":
            assert "expected_statuses" in kwargs, (
                "invalid-action invocation terminal write must pass "
                "expected_statuses so a reaper-lost race is a checked "
                "no-op, not an unguarded write"
            )
            return False  # another writer already finalized this invocation
        return True

    svc.update_status = AsyncMock(side_effect=_update_status)
    engine = SchedulerEngine(svc=svc)
    # max_runs makes _check_max_runs() actually call count_schedule_runs(),
    # so its execution is directly observable as a side effect that must
    # survive the guarded, no-op invocation write above.
    schedule = _minimal_schedule(max_runs=100)

    with patch(
        "lionagi.studio.scheduler.subprocess.build_argv",
        side_effect=RuntimeError("bad template"),
    ):
        await fire_with_claim(
            engine, schedule, "run-invalid-action", trigger_context={"scheduled": True}
        )

    svc.count_schedule_runs.assert_awaited()


@pytest.mark.asyncio
async def test_fire_cancellation_schedule_run_cas_miss_does_not_skip_side_effects():
    """Cancellation must not skip invocation finalization and
    _check_max_runs() when the schedule_run cancellation write loses its CAS
    race (e.g. another finalizer already moved the row to a terminal
    status). The write must be a checked no-op, not an unguarded write that
    raises and is swallowed by the handler's own except-and-log block."""
    from lionagi.state.db import TransitionRejectedError
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()

    async def _update_schedule_run(run_id, *, status=None, **kwargs):
        if status is not None:
            # The real DB layer raises the terminal-status floor here because
            # this call path (update_schedule_run -> _route_status_change ->
            # update_status) does not pass expected_statuses.
            raise TransitionRejectedError("schedule_run", run_id, "completed", status)

    svc.update_schedule_run = AsyncMock(side_effect=_update_schedule_run)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=100)

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=_cancel_after_launch,
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await fire_with_claim(
                engine, schedule, "run-cancel-cas", trigger_context={"scheduled": True}
            )

    # Invocation finalization must still be attempted despite the lost CAS
    # race on the schedule_run status write above.
    invocation_calls = [
        c for c in svc.update_status.await_args_list if c.args and c.args[0] == "invocation"
    ]
    assert invocation_calls, "invocation finalization must still run after a cancellation CAS miss"
    svc.count_schedule_runs.assert_awaited()


# Coordination telemetry: terminal-write races must not leak signal counters


def _lose_invocation_race(entity_type, entity_id, *, new_status, **kwargs):
    """svc.update_status side_effect: the invocation terminal write always
    loses its race (as if a concurrent finalizer, e.g. the deadline reaper,
    already claimed the row); every other entity_type's write succeeds."""
    return entity_type != "invocation"


@pytest.mark.asyncio
async def test_fire_normal_path_discards_counters_when_invocation_write_loses_race():
    """The normal completion path mints a ScheduleRunSucceeded signal (whose
    counters land on the bus) before the invocation's own terminal write
    happens. If that write loses its race, flush_run_telemetry() is never
    called to consume the counters -- they must be explicitly discarded
    instead of sitting in the bus's per-run_id map forever."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.update_status = AsyncMock(side_effect=_lose_invocation_race)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
        patch(
            "lionagi.studio.scheduler.engine.flush_run_telemetry",
            new=AsyncMock(),
        ) as flush_mock,
    ):
        await fire_with_claim(
            engine, schedule, "run-race-normal", trigger_context={"scheduled": True}
        )

    flush_mock.assert_not_awaited()
    assert engine._signal_bus.pop_run_counters("run-race-normal") is None


@pytest.mark.asyncio
async def test_fire_invalid_action_discards_counters_when_invocation_write_loses_race():
    """Same race, on the invalid-schedule-action terminal path (build_argv
    raising before any process spawns)."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.update_status = AsyncMock(side_effect=_lose_invocation_race)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            side_effect=RuntimeError("bad template"),
        ),
        patch(
            "lionagi.studio.scheduler.engine.flush_run_telemetry",
            new=AsyncMock(),
        ) as flush_mock,
    ):
        await fire_with_claim(
            engine, schedule, "run-race-invalid", trigger_context={"scheduled": True}
        )

    flush_mock.assert_not_awaited()
    assert engine._signal_bus.pop_run_counters("run-race-invalid") is None


@pytest.mark.asyncio
async def test_fire_cancellation_discards_counters_when_invocation_write_loses_race():
    """Same race, on the CancelledError terminal path."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.update_status = AsyncMock(side_effect=_lose_invocation_race)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        patch(
            "lionagi.studio.scheduler.engine.flush_run_telemetry",
            new=AsyncMock(),
        ) as flush_mock,
    ):
        with pytest.raises(asyncio.CancelledError):
            await fire_with_claim(
                engine, schedule, "run-race-cancel", trigger_context={"scheduled": True}
            )

    flush_mock.assert_not_awaited()
    assert engine._signal_bus.pop_run_counters("run-race-cancel") is None


@pytest.mark.asyncio
async def test_fire_exception_path_discards_counters_when_invocation_write_loses_race():
    """Same race, on the generic-exception terminal path."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.update_status = AsyncMock(side_effect=_lose_invocation_race)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch(
            "lionagi.studio.scheduler.engine.flush_run_telemetry",
            new=AsyncMock(),
        ) as flush_mock,
    ):
        await fire_with_claim(engine, schedule, "run-race-exc", trigger_context={"scheduled": True})

    flush_mock.assert_not_awaited()
    assert engine._signal_bus.pop_run_counters("run-race-exc") is None


@pytest.mark.asyncio
async def test_fire_telemetry_flush_failure_does_not_alter_run_outcome():
    """flush_run_telemetry() is best-effort (see
    scheduler_state.flush_run_telemetry): a failure computing coordination
    telemetry after the run's own terminal write has already committed must
    never rewrite that run's recorded outcome, and must never raise back
    into _fire()."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.compute_files_overlap = AsyncMock(side_effect=OSError("disk error"))
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

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
        await fire_with_claim(
            engine, schedule, "run-flush-fails", trigger_context={"scheduled": True}
        )

    # Exactly the one terminal write from the normal completion path -- no
    # second rewrite from a broad-except handler catching a telemetry failure
    # that leaked out of flush_run_telemetry(). The run's outcome columns ride
    # that write, so there is no standalone update_schedule_run to count.
    svc.update_schedule_run.assert_not_awaited()
    run_terminal_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.args[:2] == ("schedule_run", "run-flush-fails")
        and c.kwargs.get("new_status") != "running"
    ]
    assert len(run_terminal_calls) == 1
    assert run_terminal_calls[0].kwargs["new_status"] == "completed"
    assert run_terminal_calls[0].kwargs["extra_fields"]["error_detail"] is None
    # The invocation's own terminal write is untouched by the telemetry
    # failure too.
    invocation_terminal_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.args[0] == "invocation" and c.kwargs.get("new_status")
    ]
    assert len(invocation_terminal_calls) == 1
    assert invocation_terminal_calls[0].kwargs["new_status"] == "completed"


@pytest.mark.asyncio
async def test_fire_normal_completion_dispatches_signal_before_flush_pops_counters():
    """The terminal ScheduleRun* signal must be minted onto the bus BEFORE
    flush_run_telemetry() pops that run's counters, so a normal completion's
    persisted telemetry always includes its own terminal signal's emitted
    count -- not zero because the flush raced ahead of the mint."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

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
        await fire_with_claim(engine, schedule, "run-order", trigger_context={"scheduled": True})

    node_metadata_patches = [
        call.args[1]
        for call in svc.merge_invocation_node_metadata.await_args_list
        if len(call.args) > 1
    ]
    assert len(node_metadata_patches) == 1
    coordination = node_metadata_patches[0]["coordination"]
    assert coordination["signals"]["emitted"] == {"ScheduleRunSucceeded": 1}


# SchedulerEngine.fire_now() — delegates through service


@pytest.mark.asyncio
async def test_fire_now_returns_run_id_when_schedule_found():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.get_schedule.return_value = _minimal_schedule()
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire", return_value=MagicMock()):
        run_id = await engine.fire_now("sched-001")

    assert run_id is not None
    assert len(run_id) == 12


@pytest.mark.asyncio
async def test_fire_now_returns_none_when_schedule_missing():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.get_schedule.return_value = None
    engine = SchedulerEngine(svc=svc)

    run_id = await engine.fire_now("nonexistent")
    assert run_id is None


@pytest.mark.parametrize(
    ("fields", "names"),
    [
        (
            {
                "trigger_type": "github_poll",
                "action_kind": "command",
                "action_command": "uv",
                "action_command_args": ["run", "ci.py", "--pr", "{{pr_number}}"],
            },
            "{{pr_number}}",
        ),
        (
            {"action_prompt": "Check {{pr_title}} by {{author}} at {{fired_at}}"},
            "{{author}}, {{pr_title}}",
        ),
        # A play launch sends its prompt beside its playbook, under either spelling of the kind.
        *(
            (
                {
                    "action_kind": kind,
                    "action_playbook": "triage",
                    "action_prompt": "{{pr_number}}",
                },
                "{{pr_number}}",
            )
            for kind in ("play", "playbook")
        ),
        # An engine launch sends its prompt as the engine spec.
        (
            {
                "action_kind": "engine",
                "action_agent": "summarize",
                "action_prompt": "{{pr_number}}",
            },
            "{{pr_number}}",
        ),
    ],
)
@pytest.mark.asyncio
async def test_fire_now_refuses_a_template_a_manual_trigger_cannot_fill(monkeypatch, fields, names):
    """A manual trigger has no event, so a placeholder naming an event field would reach the
    run unfilled. It is refused, naming each such placeholder, before a slot or a run exists."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "uv")
    svc = _make_svc()
    svc.get_schedule.return_value = _minimal_schedule(**fields)
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        with pytest.raises(ValueError) as refused:
            await engine.fire_now("sched-001")

    assert f"has no value for {names} on a manual trigger" in str(refused.value)
    assert "which fills only {{manual}} and {{fired_at}}" in str(refused.value)
    mock_tracked.assert_not_called()
    assert engine.global_slot_holders == []


@pytest.mark.asyncio
async def test_fire_now_fires_a_template_the_manual_trigger_fills():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.get_schedule.return_value = _minimal_schedule(
        action_prompt="Nightly check at {{fired_at}} (manual={{manual}})"
    )
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire", return_value=MagicMock()) as mock_tracked:
        run_id = await engine.fire_now("sched-001")

    assert run_id is not None
    assert mock_tracked.call_args.kwargs["trigger_context"]["manual"] is True


@pytest.mark.parametrize(
    "fields",
    [
        # A command launch sends its arguments, not its prompt.
        {
            "action_kind": "command",
            "action_command": "uv",
            "action_command_args": ["run", "ci.py"],
            "action_prompt": "{{pr_number}}",
        },
        # A flow_yaml launch sends its spec file as written, and a play launch without a playbook
        # sends no prompt.
        {
            "action_kind": "flow_yaml",
            "action_flow_yaml": "prompt: summarize\n",
            "action_prompt": "{{pr_number}}",
        },
        *(
            {"action_kind": kind, "action_playbook": playbook, "action_prompt": "{{pr_number}}"}
            for kind in ("play", "playbook")
            for playbook in (None, "")
        ),
    ],
)
@pytest.mark.asyncio
async def test_fire_now_is_not_refused_for_a_placeholder_its_launch_does_not_send(
    monkeypatch, fields
):
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "uv")
    svc = _make_svc()
    svc.get_schedule.return_value = _minimal_schedule(**fields)
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire", return_value=MagicMock()) as mock_tracked:
        run_id = await engine.fire_now("sched-001")

    assert run_id is not None
    assert mock_tracked.call_args.kwargs["trigger_context"]["manual"] is True


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"action_kind": "Play"}, "Unknown action_kind 'Play'"),
        ({"action_kind": None}, "Unknown action_kind None"),
        (
            {"action_kind": "play", "action_playbook": "  "},
            "action_playbook '  ' contains characters not allowed",
        ),
        (
            {"action_kind": "agent", "action_model": "", "action_extra_args": ["x"]},
            "action_extra_args is not supported together with an empty action_model",
        ),
        (
            {
                "action_kind": "flow_yaml",
                "action_flow_yaml": "prompt: x\n",
                "action_extra_args": ["x"],
            },
            "flow_yaml launches do not accept action_extra_args",
        ),
        ({"action_kind": "engine"}, "engine launches require action_agent"),
        (
            {"action_kind": "engine", "action_agent": "summarize", "action_prompt": ""},
            "engine launches require action_prompt",
        ),
        (
            {"action_kind": "engine", "action_agent": "coding"},
            "the 'coding' engine kind requires a non-blank action_engine_options.test_cmd",
        ),
        ({"action_kind": "command", "action_command": ""}, "action_command is required"),
        (
            {"action_kind": "command", "action_command": "make"},
            "action_command 'make' is not in LIONAGI_SCHEDULER_COMMAND_ALLOWLIST",
        ),
        (
            {"action_kind": "command", "action_command": "uv", "action_extra_args": ["x"]},
            "command launches do not accept action_extra_args",
        ),
        (
            {"action_kind": "command", "action_command": "uv", "action_command_args": "x"},
            "action_command_args must be a list of strings",
        ),
        (
            {"action_cwd": "/nonexistent-lionagi-schedule-root"},
            "could not be resolved to an existing directory",
        ),
        # `li play` has no '--' separator, so a stored prompt it sends must not open with '-'.
        *(
            (
                {"action_kind": kind, "action_playbook": "triage", "action_prompt": "--bypass"},
                "action_prompt for a play launch must not start with '-'",
            )
            for kind in ("play", "playbook")
        ),
    ],
)
@pytest.mark.asyncio
async def test_fire_now_refuses_a_schedule_that_cannot_launch_for_that_reason(
    monkeypatch, fields, reason
):
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "uv")
    svc = _make_svc()
    svc.get_schedule.return_value = _minimal_schedule(
        **{"action_prompt": "{{pr_number}}", **fields}
    )
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        with pytest.raises(ValueError, match=reason) as refused:
            await engine.fire_now("sched-001")

    assert "has no value for" not in str(refused.value)
    mock_tracked.assert_not_called()
    assert engine.global_slot_holders == []


@pytest.mark.asyncio
async def test_fire_now_refusal_for_a_missing_root_names_that_root_and_not_the_daemon_cwd(
    monkeypatch, tmp_path
):
    from lionagi.studio.scheduler.engine import SchedulerEngine

    daemon_cwd = tmp_path / "daemon-working-dir"
    daemon_cwd.mkdir()
    monkeypatch.chdir(daemon_cwd)
    svc = _make_svc()
    svc.get_schedule.return_value = _minimal_schedule(
        action_cwd="/nonexistent-lionagi-schedule-root"
    )
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        with pytest.raises(ValueError) as refused:
            await engine.fire_now("sched-001")

    message = str(refused.value)
    assert "'sched-001'" in message
    assert "'/nonexistent-lionagi-schedule-root'" in message
    assert "daemon-working-dir" not in message
    # The original error, whose text names the daemon's directory, is not attached either.
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    mock_tracked.assert_not_called()


@pytest.mark.asyncio
async def test_fire_now_refuses_when_the_project_registry_cannot_be_read(monkeypatch, caplog):
    import sqlite3

    from lionagi.studio.scheduler.engine import SchedulerEngine
    from lionagi.studio.services import projects

    async def unreadable(name):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(projects, "get_project", unreadable)
    svc = _make_svc()
    svc.get_schedule.return_value = _minimal_schedule(action_project="proj")
    engine = SchedulerEngine(svc=svc)

    with caplog.at_level(logging.WARNING, logger="lionagi.studio.scheduler.engine"):
        with patch.object(engine, "_tracked_fire") as mock_tracked:
            with pytest.raises(
                ValueError, match=r"could not be read \(OperationalError\)"
            ) as refused:
                await engine.fire_now("sched-001")

    assert "database is locked" not in str(refused.value)
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    # A fault of the store, not the request: the operator's log keeps the error.
    (record,) = [r for r in caplog.records if "project registry could not be read" in r.message]
    assert record.exc_info is not None
    assert "database is locked" in str(record.exc_info[1])
    mock_tracked.assert_not_called()
    assert engine.global_slot_holders == []


@pytest.mark.asyncio
async def test_fire_now_refuses_when_the_execution_root_cannot_be_resolved(monkeypatch, caplog):
    from lionagi.studio.scheduler.engine import SchedulerEngine
    from lionagi.studio.services import projects

    async def denied(name):
        raise PermissionError(13, "Permission denied", "/private/registry.db")

    monkeypatch.setattr(projects, "get_project", denied)
    svc = _make_svc()
    svc.get_schedule.return_value = _minimal_schedule(action_project="proj")
    engine = SchedulerEngine(svc=svc)

    with caplog.at_level(logging.WARNING, logger="lionagi.studio.scheduler.engine"):
        with patch.object(engine, "_tracked_fire") as mock_tracked:
            with pytest.raises(
                ValueError,
                match=r"the project registry or the execution root its launch reads could not "
                r"be read \(PermissionError\)",
            ) as refused:
                await engine.fire_now("sched-001")

    assert "/private/registry.db" not in str(refused.value)
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    (record,) = [r for r in caplog.records if "project registry or execution root" in r.message]
    assert isinstance(record.exc_info[1], PermissionError)
    mock_tracked.assert_not_called()
    assert engine.global_slot_holders == []


@pytest.mark.parametrize(
    ("fields", "recorded"),
    [
        ({"action_kind": "agent"}, "check 7"),
        ({"action_kind": "play", "action_playbook": "triage"}, "check 7"),
        ({"action_kind": "engine", "action_agent": "summarize"}, "check 7"),
        ({"action_kind": "play"}, None),
        ({"action_kind": "playbook", "action_playbook": ""}, None),
        ({"action_kind": "flow_yaml", "action_flow_yaml": "prompt: summarize\n"}, None),
        ({"action_kind": "command", "action_command": "uv"}, None),
    ],
)
def test_the_rendered_prompt_is_only_one_the_launch_sends(fields, recorded):
    from lionagi.studio.scheduler.subprocess import render_action_prompt

    schedule = _minimal_schedule(action_prompt="check {{pr_number}}", **fields)
    poll_context = {"github_events": [{"pr_number": 7}], "fired_at": 1.0}
    assert render_action_prompt(schedule, poll_context) == recorded


def test_a_launch_that_sends_no_prompt_neither_renders_nor_checks_it(monkeypatch):
    from lionagi.studio.scheduler.subprocess import build_argv

    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "uv")
    command = {"action_kind": "command", "action_command": "uv", "action_command_args": ["-V"]}
    context = {"payload": "--"}
    argv, _ = build_argv(_minimal_schedule(action_prompt="{{payload}}", **command), context)
    assert argv == build_argv(_minimal_schedule(action_prompt="", **command), context)[0]
    # A launch that sends the same prompt still refuses what it renders to.
    with pytest.raises(ValueError, match="'--' is not allowed"):
        build_argv(_minimal_schedule(action_prompt="{{payload}}", action_kind="agent"), context)


def test_an_engine_spec_that_renders_empty_is_refused_when_the_launch_is_built():
    from lionagi.studio.scheduler.subprocess import build_argv, validate_launch_fields

    schedule = _minimal_schedule(
        action_kind="engine", action_agent="summarize", action_prompt="{{pr_body}}"
    )
    poll_context = {"github_events": [{"pr_body": ""}], "fired_at": 1.0}

    assert validate_launch_fields(schedule) == "engine"
    with pytest.raises(ValueError, match="engine launches require action_prompt"):
        build_argv(schedule, poll_context)


def test_unfilled_template_vars_reads_the_event_a_poll_fire_carries():
    from lionagi.studio.scheduler.subprocess import _render_command_arg, unfilled_template_vars

    schedule = _minimal_schedule(
        action_kind="command", action_command="uv", action_command_args=["{{pr_number}}"]
    )
    poll_context = {"github_events": [{"pr_number": 42}], "fired_at": 1.0}

    assert unfilled_template_vars(schedule, poll_context) == []
    assert unfilled_template_vars(schedule, {"manual": True}) == ["pr_number"]
    assert _render_command_arg("{{pr_number}}", poll_context) == "42"


# _maybe_fire() tests


@pytest.mark.asyncio
async def test_maybe_fire_skips_overlap_and_records_skipped_run():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(overlap_policy="skip")
    engine._running[schedule["id"]] = "existing-run"

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_not_called()
    svc.create_schedule_run.assert_awaited_once()
    # update_status called for the skipped run
    skipped_calls = [
        c for c in svc.update_status.await_args_list if c.kwargs.get("new_status") == "skipped"
    ]
    assert skipped_calls


@pytest.mark.asyncio
async def test_maybe_fire_fires_when_no_overlap():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(overlap_policy="skip")
    # no entry in _running

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._maybe_fire(schedule, now=1000.0)

    mock_tracked.assert_called_once()


# create_skipped_run helper


@pytest.mark.asyncio
async def test_create_skipped_run_calls_svc_create_and_update_status():
    from lionagi.state.reasons import ScheduleReasons
    from lionagi.studio.services.scheduler_state import create_skipped_run

    svc = _make_svc()
    schedule = _minimal_schedule()
    await create_skipped_run(
        svc,
        run_id="skip-001",
        schedule=schedule,
        trigger_context={"skipped_overlap": True},
        now=999.0,
        reason_code=ScheduleReasons.SKIPPED_OVERLAP,
        reason_summary="overlapped",
        metadata={"overlap_policy": "skip"},
    )
    svc.create_schedule_run.assert_awaited_once()
    svc.update_status.assert_awaited_once()
    call = svc.update_status.await_args
    assert call.kwargs["new_status"] == "skipped"


# SchedulerEngine construction — default vs injected service


def test_engine_uses_default_svc_when_none_provided():
    from lionagi.studio.scheduler.engine import SchedulerEngine
    from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

    engine = SchedulerEngine()
    assert isinstance(engine._svc, _DBSchedulerStateService)


def test_engine_uses_injected_svc():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    assert engine._svc is svc


# Cron timezone resolution — the P1 fix: cron_expr is resolved in the
# configured timezone (default: system local), not UTC. next_fire_at is
# still stored as a UTC epoch.


def test_compute_next_fire_uses_configured_timezone(monkeypatch):
    """(a) Cron resolved in a pinned non-UTC configured TZ produces the
    correct UTC epoch — pinned via LIONAGI_SCHEDULER_TZ so this doesn't
    depend on the CI host's local timezone."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(cron_expr="0 18 * * *")  # 18:00 local, daily

    # Reference: 2026-07-02 10:00:00 EDT — before today's 18:00 local fire.
    ref_epoch = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()

    next_at = engine._compute_next_fire(schedule, ref_epoch)
    assert next_at is not None

    # 18:00 EDT (UTC-4 in July) == 22:00 UTC same day. A UTC-only
    # implementation would resolve "0 18 * * *" against ref_epoch's raw UTC
    # clock fields and land on a different absolute instant.
    got_utc = datetime.fromtimestamp(next_at, tz=timezone.utc)
    assert got_utc == datetime(2026, 7, 2, 22, 0, 0, tzinfo=timezone.utc)
    assert datetime.fromtimestamp(next_at, tz=NY) == datetime(2026, 7, 2, 18, 0, 0, tzinfo=NY)


def test_compute_next_fire_date_pinned_cron_fires_same_day_not_next_year(monkeypatch):
    """(b) The July-2027 silent-skip bug: a date-pinned cron created after
    its UTC-clock moment but before its local-clock moment must fire
    *today*, not silently skip to the same date next year."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(cron_expr="30 17 2 7 *")  # 17:30 local, July 2 only

    # 19:00 UTC = 15:00 EDT on 2027-07-02: already past the cron's literal
    # "17:30" UTC-clock instant, but still before 17:30 EDT local — this is
    # exactly the window that broke 8 production schedules under UTC-only
    # resolution (created after the UTC moment, before the local moment).
    ref_epoch = datetime(2027, 7, 2, 19, 0, 0, tzinfo=timezone.utc).timestamp()

    next_at = engine._compute_next_fire(schedule, ref_epoch)
    assert next_at is not None

    got_local = datetime.fromtimestamp(next_at, tz=NY)
    assert got_local == datetime(2027, 7, 2, 17, 30, 0, tzinfo=NY)
    assert got_local.year == 2027  # NOT skipped to 2028


def test_invalid_scheduler_tz_falls_back_to_utc(monkeypatch, caplog):
    """An invalid LIONAGI_SCHEDULER_TZ must not crash cron resolution — it
    falls back to UTC with a warning."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "Not/A_Real_Zone")

    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(cron_expr="0 18 * * *")
    ref_epoch = datetime(2026, 7, 2, 10, 0, 0, tzinfo=timezone.utc).timestamp()

    with caplog.at_level(logging.WARNING):
        next_at = engine._compute_next_fire(schedule, ref_epoch)

    assert next_at is not None
    got_utc = datetime.fromtimestamp(next_at, tz=timezone.utc)
    assert got_utc == datetime(2026, 7, 2, 18, 0, 0, tzinfo=timezone.utc)
    assert any("is not a zone this host can load" in r.getMessage() for r in caplog.records)


def test_compute_next_fire_cron_prefers_resolved_timezone_over_scheduler_tz(monkeypatch):
    """A row with a declared resolved_timezone (set by the declarative apply
    path) must resolve cron against THAT zone, not the process-wide
    SCHEDULER_TZ -- even when the two clearly disagree."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "UTC")

    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(cron_expr="0 18 * * *", resolved_timezone="America/New_York")

    ref_epoch = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()
    next_at = engine._compute_next_fire(schedule, ref_epoch)
    assert next_at is not None

    # 18:00 EDT == 22:00 UTC. A SCHEDULER_TZ=UTC-only implementation would
    # instead land on 18:00 UTC == 14:00 EDT the same day.
    got_utc = datetime.fromtimestamp(next_at, tz=timezone.utc)
    assert got_utc == datetime(2026, 7, 2, 22, 0, 0, tzinfo=timezone.utc)


def test_compute_next_fire_cron_null_resolved_timezone_uses_scheduler_tz(monkeypatch):
    """A legacy row with no resolved_timezone (NULL) keeps resolving cron
    against SCHEDULER_TZ, unchanged."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(cron_expr="0 18 * * *", resolved_timezone=None)

    ref_epoch = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()
    next_at = engine._compute_next_fire(schedule, ref_epoch)
    assert next_at is not None
    got_utc = datetime.fromtimestamp(next_at, tz=timezone.utc)
    assert got_utc == datetime(2026, 7, 2, 22, 0, 0, tzinfo=timezone.utc)


# 'at' trigger — fire-once semantics (no next occurrence to compute; the
# fired row's next_fire_at must be explicitly cleared, not left in place).


def test_compute_next_fire_at_trigger_returns_none():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(trigger_type="at", cron_expr=None)
    assert engine._compute_next_fire(schedule, time.time()) is None


def test_next_fire_field_clears_next_fire_at_for_at_trigger():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(trigger_type="at", cron_expr=None)
    assert engine._next_fire_field(schedule, None) == {"next_fire_at": None}


def test_next_fire_field_leaves_other_triggers_untouched_on_none():
    """A None next_at for cron/interval/github_poll must never be merged in
    -- those trigger types always compute a real next fire; a None there
    would only ever come from a malformed row and must not blank out a
    value some other write already set."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    engine = SchedulerEngine(svc=_make_svc())
    schedule = _minimal_schedule(trigger_type="interval", cron_expr=None)
    assert engine._next_fire_field(schedule, None) == {}


@pytest.mark.asyncio
async def test_fire_at_trigger_persists_explicit_none_next_fire_at():
    """Firing an 'at' schedule must explicitly persist next_fire_at=None
    (not merely omit the key) so the row is never read back as still due."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(trigger_type="at", cron_expr=None, max_runs=1)

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
        await fire_with_claim(engine, schedule, "run-001", trigger_context={"scheduled": True})

    (_run_payload,), kwargs = svc.create_schedule_run_and_advance.await_args
    schedule_fields = kwargs["schedule_fields"]
    assert "next_fire_at" in schedule_fields
    assert schedule_fields["next_fire_at"] is None


@pytest.mark.asyncio
async def test_recover_missed_fire_run_once_reserves_cleared_next_fire_for_at_trigger():
    """Missed-fire recovery must reserve the 'at' trigger's terminal None
    synchronously (persist a cleared next_fire_at) before queueing the
    recovery fire -- otherwise the immediately-following tick still sees the
    past-due instant and queues a duplicate fire."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        trigger_type="at",
        cron_expr=None,
        max_runs=1,
        next_fire_at=time.time() - 60,
    )

    due_instant = schedule["next_fire_at"]
    fired: list[tuple[str, Any]] = []
    engine._tracked_fire = lambda sched, run_id, **kw: fired.append(
        (run_id, kw.get("expect_next_fire_at"))
    )

    await engine._recover_missed_fire_run_once(schedule, time.time())

    # The reserve claims the instant it is recovering, and the fire that follows claims the
    # value the reserve wrote, not the pre-reserve value still sitting in the local dict.
    svc.update_schedule.assert_awaited_once_with(
        "sched-001", expect_next_fire_at=due_instant, next_fire_at=None
    )
    assert len(fired) == 1
    assert fired[0][1] is None


@pytest.mark.asyncio
async def test_missed_fire_recovery_claims_the_instant_it_reserved_not_the_one_it_read():
    """The recovery moves the cursor itself, so it must claim the value it wrote.

    Claiming the pre-reserve value the local dict still holds refuses the recovery against
    its own reservation: no occurrence, and the invocation cancelled.
    """
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    due = time.time() - 60
    schedule = _minimal_schedule(next_fire_at=due, missed_fire_policy="run_once")
    # The row, kept apart from the dict the recovery holds: a real reserve does not write back
    # into the caller's snapshot, so the snapshot stays stale exactly as it does in production.
    stored: dict[str, Any] = {"next_fire_at": due}

    async def _reserve(sid, *, expect_next_fire_at=None, **fields):
        if stored["next_fire_at"] != expect_next_fire_at:
            return False
        stored.update(fields)
        return True

    svc.update_schedule = AsyncMock(side_effect=_reserve)

    fired: list[Any] = []
    engine._tracked_fire = lambda sched, run_id, **kw: fired.append(kw["expect_next_fire_at"])

    await engine._recover_missed_fire_run_once(schedule, time.time())

    assert len(fired) == 1
    reserved = stored["next_fire_at"]
    assert reserved != due, "the reserve did not move the cursor, so this proves nothing"
    assert schedule["next_fire_at"] == due, "the snapshot must stay stale for this to mean anything"
    assert fired[0] == reserved


@pytest.mark.asyncio
async def test_missed_fire_recovery_stands_down_when_another_scheduler_reserved_first():
    """Two daemons recovering one missed instant: the reserve is the claim, so one wins."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(next_fire_at=time.time() - 60, missed_fire_policy="run_once")
    # Already reserved by someone else: the row no longer holds the value this caller read.
    svc.update_schedule = AsyncMock(return_value=False)

    fired: list[Any] = []
    engine._tracked_fire = lambda sched, run_id, **kw: fired.append(run_id)

    await engine._recover_missed_fire_run_once(schedule, time.time())

    assert fired == []


@pytest.mark.asyncio
async def test_recovery_claim_blocks_racing_fire_now_on_max_runs_one():
    """A queued recovery holds the max_runs claim, so a fire_now() racing it
    (before the recovery task's occurrence row lands) must be refused --
    otherwise an 'at' schedule's forced max_runs=1 admits two executions."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        trigger_type="at",
        cron_expr=None,
        max_runs=1,
        next_fire_at=time.time() - 60,
    )
    svc.get_schedule = AsyncMock(return_value=schedule)

    fired: list[str] = []
    # Recovery hands its claims to the (stubbed) fire task without releasing
    # them -- exactly the window fire_now can race into.
    engine._tracked_fire = lambda sched, run_id, **kw: fired.append(run_id)

    await engine._recover_missed_fire_run_once(schedule, time.time())
    assert len(fired) == 1

    with pytest.raises(ValueError, match="max_runs"):
        await engine.fire_now(schedule["id"])

    # No second execution admitted.
    assert len(fired) == 1
    svc.create_invocation.assert_not_awaited()
    svc.create_schedule_run_and_advance.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_refusal_leaves_at_row_due_not_stranded(monkeypatch):
    """A global-slot refusal must return BEFORE the next_fire_at reserve --
    clearing an 'at' trigger's due instant on a refused recovery would
    strand its single run permanently (nothing would ever re-schedule it)."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        trigger_type="at",
        cron_expr=None,
        max_runs=1,
        next_fire_at=time.time() - 60,
    )

    async def _no_slot(*args, **kwargs):
        return False, None

    monkeypatch.setattr(engine, "_reserve_global_slot", _no_slot)
    fired: list[str] = []
    engine._tracked_fire = lambda sched, run_id, **kw: fired.append(run_id)

    await engine._recover_missed_fire_run_once(schedule, time.time())

    assert fired == []
    # next_fire_at untouched: no update_schedule write happened at all.
    svc.update_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_fire_at_trigger_already_fired_refused_by_max_runs_gate():
    """Re-applying an unchanged/edited 'at' member resets next_fire_at to
    the past due instant again, but must not actually re-fire: the same
    max_runs=1 claim-before-fire gate every other bounded schedule uses
    refuses admission once a run already exists, and auto-disables instead."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.count_schedule_runs = AsyncMock(return_value=1)  # already fired once
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        trigger_type="at",
        cron_expr=None,
        max_runs=1,
        next_fire_at=time.time() - 5,
    )

    await engine._maybe_fire(schedule, time.time())

    svc.create_invocation.assert_not_awaited()
    svc.create_schedule_run_and_advance.assert_not_awaited()
    svc.update_schedule.assert_awaited_once_with(
        "sched-001", enabled=0, audit_context=ANY, reason=ANY
    )


# recompute_next_fire — shared recompute+log path for daemon start, PATCH,
# and disable->enable (services/schedules.py hooks it too; see
# tests/studio/test_schedule_tz_recompute.py for those integration paths).


@pytest.mark.asyncio
async def test_recompute_next_fire_persists_and_logs_on_shift(monkeypatch, caplog):
    """Recomputing a schedule whose stored next_fire_at is stale persists
    the new value and logs exactly once (old -> new)."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(cron_expr="0 18 * * *", next_fire_at=100.0)
    ref_epoch = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()

    with caplog.at_level(logging.INFO):
        new = await engine.recompute_next_fire(schedule, now=ref_epoch)

    assert new is not None
    assert new != 100.0
    # The same write also carries the zone the new value was resolved in, so
    # a shifted fire time and the interpretation that produced it land
    # together rather than as two separately-discoverable facts.
    svc.update_schedule.assert_awaited_once_with(
        schedule["id"],
        next_fire_at=new,
        effective_timezone="America/New_York",
        effective_timezone_source=studio_config.SCHEDULER_TZ_SOURCE,
    )
    shift_logs = [r for r in caplog.records if "next_fire_at shifted" in r.message]
    assert len(shift_logs) == 1


@pytest.mark.asyncio
async def test_recompute_next_fire_noop_when_unchanged(monkeypatch, caplog):
    """(d) A schedule already at the correct next_fire_at is a true no-op:
    no DB write, no log line."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(cron_expr="0 18 * * *")
    ref_epoch = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()

    first = await engine.recompute_next_fire(schedule, now=ref_epoch)
    schedule["next_fire_at"] = first
    svc.update_schedule.reset_mock()
    caplog.clear()

    with caplog.at_level(logging.INFO):
        second = await engine.recompute_next_fire(schedule, now=ref_epoch)

    assert second == first
    svc.update_schedule.assert_not_awaited()
    assert not any("shifted" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_recompute_armed_cron_schedules_shifts_and_logs_on_startup(monkeypatch, caplog):
    """(c1) Daemon-start recompute shifts a stale-but-still-future
    next_fire_at (the timezone-migration correction case this hook exists
    for) and logs once. A *past due* next_fire_at is a different case —
    see test_recompute_armed_cron_schedules_leaves_past_due_untouched below,
    it must not be touched here."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    fixed_now = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()
    monkeypatch.setattr(engine_mod.time, "time", lambda: fixed_now)

    # Stale but still-future next_fire_at, as if computed under the old
    # (wrong) timezone interpretation.
    stale_future = fixed_now + 3600
    stale_schedule = _minimal_schedule(
        id="sched-stale", cron_expr="0 18 * * *", next_fire_at=stale_future
    )
    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[stale_schedule])
    engine = SchedulerEngine(svc=svc)

    with caplog.at_level(logging.INFO):
        await engine._recompute_armed_cron_schedules()

    svc.update_schedule.assert_awaited_once()
    assert any("next_fire_at shifted" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_recompute_armed_cron_schedules_leaves_past_due_untouched(monkeypatch, caplog):
    """A schedule whose stored next_fire_at is already due at startup must
    not be recomputed into the future here -- that would erase the
    missed-fire recovery _check_missed_fires() is about to apply."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    fixed_now = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()
    monkeypatch.setattr(engine_mod.time, "time", lambda: fixed_now)

    past_due_schedule = _minimal_schedule(
        id="sched-past-due",
        cron_expr="0 18 * * *",
        next_fire_at=fixed_now - 3600,
        missed_fire_policy="run_once",
    )
    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[past_due_schedule])
    engine = SchedulerEngine(svc=svc)

    with caplog.at_level(logging.INFO):
        await engine._recompute_armed_cron_schedules()

    svc.update_schedule.assert_not_awaited()
    assert not any("next_fire_at shifted" in r.message for r in caplog.records)
    assert past_due_schedule["next_fire_at"] == pytest.approx(fixed_now - 3600)


@pytest.mark.asyncio
async def test_startup_missed_fire_run_once_recovers_and_advances(monkeypatch):
    """End-to-end startup ordering: a past-due cron schedule with
    missed_fire_policy="run_once" gets exactly one recovery fire through
    _check_missed_fires() (not erased by the earlier recompute pass), and
    next_fire_at ends up in the future once that fire completes."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    fixed_now = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()
    monkeypatch.setattr(engine_mod.time, "time", lambda: fixed_now)

    schedule = _minimal_schedule(
        id="sched-run-once",
        cron_expr="0 0 * * *",
        next_fire_at=fixed_now - 3600,
        missed_fire_policy="run_once",
    )
    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[schedule])

    # Persists into the same dict list_schedules() keeps returning, mirroring a real DB.
    svc.update_schedule = AsyncMock(side_effect=persisting_update_schedule(schedule))
    engine = SchedulerEngine(svc=svc)

    original_tracked_fire = engine._tracked_fire
    tracked_calls: list[tuple] = []

    def _spy_tracked_fire(*args, **kwargs):
        tracked_calls.append((args, kwargs))
        return original_tracked_fire(*args, **kwargs)

    engine._tracked_fire = _spy_tracked_fire  # type: ignore[method-assign]

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
        # Startup ordering: recompute pass first, then the missed-fire check
        # (mirrors start() -> _tick_loop()).
        await engine._recompute_armed_cron_schedules()
        await engine._check_missed_fires()
        if engine._fire_tasks:
            await asyncio.gather(*engine._fire_tasks)

    assert len(tracked_calls) == 1, "Expected exactly one recovery fire"
    assert tracked_calls[0][1]["trigger_context"]["missed_recovery"] is True
    assert _run_trigger_facts(tracked_calls[0][1])["scheduled"] is False

    update_calls = [
        c for c in svc.update_schedule.await_args_list if c.args and c.args[0] == "sched-run-once"
    ]
    assert update_calls, "Expected the recovery fire to persist a new next_fire_at"
    final_next_fire_at = update_calls[-1].kwargs.get("next_fire_at")
    assert final_next_fire_at is not None
    assert final_next_fire_at > fixed_now


@pytest.mark.asyncio
async def test_startup_missed_fire_run_once_not_double_fired_by_immediate_tick(
    monkeypatch, tmp_path
):
    """Reproduces the exact _tick_loop() startup ordering: _check_missed_fires()
    runs, then _tick() runs immediately after with no sleep in between (the
    tick loop only sleeps *between* iterations of the while-loop, not before
    its first one). A past-due run_once schedule must be fired exactly once
    total: the missed-fire recovery path must reserve/advance next_fire_at
    synchronously before _check_missed_fires() returns, so the immediately
    following _tick() does not see the same stale past-due next_fire_at and
    queue a second, duplicate fire for it."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.state.db as state_db_mod
    import lionagi.studio.config as studio_config
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")
    # _tick() also runs the dispatch-outbox scan and the D3 task-worker tick
    # (_deliver_due_dispatches / _run_task_worker_tick), both of which open a
    # StateDB() at the default path — redirect it so this test never touches
    # the real ~/.lionagi/state.db.
    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    fixed_now = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()
    monkeypatch.setattr(engine_mod.time, "time", lambda: fixed_now)

    schedule = _minimal_schedule(
        id="sched-run-once-tick",
        cron_expr="0 0 * * *",
        next_fire_at=fixed_now - 3600,
        missed_fire_policy="run_once",
    )
    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[schedule])

    # Persists into the same dict list_schedules() keeps returning, mirroring a real DB.
    svc.update_schedule = AsyncMock(side_effect=persisting_update_schedule(schedule))
    engine = SchedulerEngine(svc=svc)

    original_tracked_fire = engine._tracked_fire
    tracked_calls: list[tuple] = []

    def _spy_tracked_fire(*args, **kwargs):
        tracked_calls.append((args, kwargs))
        return original_tracked_fire(*args, **kwargs)

    engine._tracked_fire = _spy_tracked_fire  # type: ignore[method-assign]

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
        patch(
            "lionagi.studio.services.lifecycle.run_periodic_reapers",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "lionagi.studio.services.db_maintenance.checkpoint_state_db",
            new=AsyncMock(return_value=None),
        ),
    ):
        # Exact _tick_loop() ordering: _check_missed_fires() then _tick(),
        # with nothing awaited/slept in between (the recovery fire is a
        # tracked background task, not awaited here — same as production).
        await engine._recompute_armed_cron_schedules()
        await engine._check_missed_fires()
        await asyncio.wait_for(engine._tick(), timeout=5)
        if engine._fire_tasks:
            await asyncio.gather(*engine._fire_tasks)

    assert len(tracked_calls) == 1, (
        "Expected exactly one fire total (missed-fire recovery only); the "
        "immediately-following _tick() must not queue a second, duplicate "
        f"fire for the same past-due schedule. Got {len(tracked_calls)} "
        f"fires: {[c[1].get('trigger_context') for c in tracked_calls]}"
    )
    assert tracked_calls[0][1]["trigger_context"]["missed_recovery"] is True
    assert _run_trigger_facts(tracked_calls[0][1])["scheduled"] is False
    assert fake_db.exists(), (
        "_tick() must have opened/schema-applied the redirected StateDB "
        "(dispatch-outbox scan + D3 task-worker tick), proving isolation "
        "from the real ~/.lionagi/state.db rather than the tick silently "
        "no-op'ing on the fake path"
    )


@pytest.mark.asyncio
async def test_startup_missed_fire_run_once_reserve_failure_skips_recovery(monkeypatch, tmp_path):
    """Failure path of the synchronous reserve: if update_schedule raises
    while reserving next_fire_at, storage still holds the past-due value
    and the immediately-following _tick() will fire the schedule normally.
    The recovery path must therefore NOT queue its own fire on a failed
    reserve — otherwise the external action runs twice in one cycle. Net
    result: exactly one fire total, and it is the normal scheduled one,
    not a missed_recovery fire."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.state.db as state_db_mod
    import lionagi.studio.config as studio_config
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")
    # Same real-DB hazard as the sibling test above: _tick() opens StateDB()
    # at the default path via _deliver_due_dispatches / _run_task_worker_tick.
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", tmp_path / "state.db")

    fixed_now = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()
    monkeypatch.setattr(engine_mod.time, "time", lambda: fixed_now)

    schedule = _minimal_schedule(
        id="sched-run-once-reserve-fail",
        cron_expr="0 0 * * *",
        next_fire_at=fixed_now - 3600,
        missed_fire_policy="run_once",
    )
    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[schedule])

    calls = {"n": 0}

    _persist = persisting_update_schedule(schedule)

    async def _first_write_fails(sid, **kwargs):
        # The reserve (first write) hits a transient storage failure; later
        # writes (the normal fire's own advance) succeed and persist into
        # the same dict list_schedules() keeps returning.
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("storage briefly unavailable")
        return await _persist(sid, **kwargs)

    svc.update_schedule = AsyncMock(side_effect=_first_write_fails)
    engine = SchedulerEngine(svc=svc)

    original_tracked_fire = engine._tracked_fire
    tracked_calls: list[tuple] = []

    def _spy_tracked_fire(*args, **kwargs):
        tracked_calls.append((args, kwargs))
        return original_tracked_fire(*args, **kwargs)

    engine._tracked_fire = _spy_tracked_fire  # type: ignore[method-assign]

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
        patch(
            "lionagi.studio.services.lifecycle.run_periodic_reapers",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "lionagi.studio.services.db_maintenance.checkpoint_state_db",
            new=AsyncMock(return_value=None),
        ),
    ):
        await engine._recompute_armed_cron_schedules()
        await engine._check_missed_fires()
        await asyncio.wait_for(engine._tick(), timeout=5)
        if engine._fire_tasks:
            await asyncio.gather(*engine._fire_tasks)

    assert len(tracked_calls) == 1, (
        "Expected exactly one fire total when the reserve write fails: the "
        "recovery must stand down and let the normal tick own the fire. Got "
        f"{len(tracked_calls)} fires: "
        f"{[c[1].get('trigger_context') for c in tracked_calls]}"
    )
    ctx = tracked_calls[0][1].get("trigger_context") or {}
    assert not ctx.get("missed_recovery"), (
        f"The single fire must be the normal scheduled one, not a recovery fire: {ctx}"
    )


@pytest.mark.asyncio
async def test_startup_missed_fire_skip_records_no_recovery_and_advances(monkeypatch):
    """Same startup ordering, but missed_fire_policy="skip": no recovery
    fire is created, and next_fire_at still ends up in the future (advanced
    by the skip-recording path itself)."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    fixed_now = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()
    monkeypatch.setattr(engine_mod.time, "time", lambda: fixed_now)

    schedule = _minimal_schedule(
        id="sched-skip",
        cron_expr="0 0 * * *",
        next_fire_at=fixed_now - 3600,
        missed_fire_policy="skip",
    )
    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[schedule])

    svc.update_schedule = AsyncMock(side_effect=persisting_update_schedule(schedule))
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._recompute_armed_cron_schedules()
        await engine._check_missed_fires()

    mock_tracked.assert_not_called()

    update_calls = [
        c for c in svc.update_schedule.await_args_list if c.args and c.args[0] == "sched-skip"
    ]
    assert update_calls, "Expected the skip path to persist a new next_fire_at"
    final_next_fire_at = update_calls[-1].kwargs.get("next_fire_at")
    assert final_next_fire_at is not None
    assert final_next_fire_at > fixed_now


@pytest.mark.asyncio
async def test_check_missed_fires_run_once_equality_boundary_is_due(monkeypatch):
    """next_fire_at == now must be treated as due by _check_missed_fires(),
    not bypassed to the normal tick path: the startup recompute treats
    <= now as past-due (see _recompute_armed_cron_schedules), so the
    missed-fire guard must match with > now (strictly future), not >= now."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    fixed_now = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()
    monkeypatch.setattr(engine_mod.time, "time", lambda: fixed_now)

    schedule = _minimal_schedule(
        id="sched-run-once-eq",
        cron_expr="0 0 * * *",
        next_fire_at=fixed_now,
        missed_fire_policy="run_once",
    )
    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[schedule])
    svc.update_schedule = AsyncMock()
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._check_missed_fires()

    mock_tracked.assert_called_once()
    assert mock_tracked.call_args.kwargs["trigger_context"]["missed_recovery"] is True
    assert _run_trigger_facts(mock_tracked.call_args.kwargs)["scheduled"] is False


@pytest.mark.asyncio
async def test_check_missed_fires_skip_equality_boundary_is_due(monkeypatch):
    """Same equality boundary for missed_fire_policy="skip": next_fire_at
    == now must be recorded as a missed-fire skip, not silently fall
    through to the normal tick's due-check."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    fixed_now = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()
    monkeypatch.setattr(engine_mod.time, "time", lambda: fixed_now)

    from lionagi.state.reasons import ScheduleReasons

    schedule = _minimal_schedule(
        id="sched-skip-eq",
        cron_expr="0 0 * * *",
        next_fire_at=fixed_now,
        missed_fire_policy="skip",
    )
    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[schedule])
    svc.update_schedule = AsyncMock()
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._check_missed_fires()

    mock_tracked.assert_not_called()
    svc.create_schedule_run.assert_awaited_once()
    reason_kwargs = svc.update_status.await_args_list[-1].kwargs
    assert reason_kwargs.get("reason_code") == ScheduleReasons.SKIPPED_MISSED_FIRE


@pytest.mark.asyncio
async def test_recompute_armed_cron_schedules_unchanged_no_log(monkeypatch, caplog):
    """(d) A schedule that's already correct produces no write and no log
    during the daemon-start sweep."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

    fixed_now = datetime(2026, 7, 2, 10, 0, 0, tzinfo=NY).timestamp()
    monkeypatch.setattr(engine_mod.time, "time", lambda: fixed_now)

    schedule = _minimal_schedule(id="sched-stable", cron_expr="0 18 * * *")
    probe = SchedulerEngine(svc=_make_svc())
    schedule["next_fire_at"] = probe._compute_next_fire(schedule, fixed_now)

    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[schedule])
    engine = SchedulerEngine(svc=svc)

    with caplog.at_level(logging.INFO):
        await engine._recompute_armed_cron_schedules()

    svc.update_schedule.assert_not_awaited()
    assert not any("shifted" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_check_missed_fires_excludes_github_poll(monkeypatch):
    """A stale github_poll next_fire_at is not a missed scheduled occurrence
    -- that trigger's cadence is driven by last_fired_at/poll_interval_sec,
    not next_fire_at (see _tick_github). _check_missed_fires() must leave it
    alone. An interval schedule with the same stale next_fire_at is the
    positive control: it must still record a missed-fire skip."""
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.state.reasons import ScheduleReasons
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fixed_now = time.time()
    monkeypatch.setattr(engine_mod.time, "time", lambda: fixed_now)

    github_schedule = _minimal_schedule(
        id="sched-gh",
        trigger_type="github_poll",
        cron_expr=None,
        next_fire_at=fixed_now - 3600,
        missed_fire_policy="skip",
    )
    interval_schedule = _minimal_schedule(
        id="sched-interval",
        trigger_type="interval",
        cron_expr=None,
        interval_sec=60,
        next_fire_at=fixed_now - 3600,
        missed_fire_policy="skip",
    )
    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[github_schedule, interval_schedule])
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire") as mock_tracked:
        await engine._check_missed_fires()

    mock_tracked.assert_not_called()
    skip_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.kwargs.get("reason_code") == ScheduleReasons.SKIPPED_MISSED_FIRE
    ]
    skipped_run_schedule_ids = set()
    for c in skip_calls:
        for ref in c.kwargs.get("evidence_refs") or []:
            if ref.get("kind") == "schedule":
                skipped_run_schedule_ids.add(ref.get("id"))

    assert "sched-gh" not in skipped_run_schedule_ids
    assert "sched-interval" in skipped_run_schedule_ids


@pytest.mark.asyncio
async def test_tick_does_not_await_worker_pass_before_evaluating_schedules(monkeypatch, tmp_path):
    """_tick() must not await the ad-hoc task-worker pass before loading and
    evaluating due schedules -- a hung/slow worker pass would otherwise defer
    every schedule's evaluation for the whole pass. The worker
    pass here never completes; _tick() must still return promptly and fire
    the due schedule in the same cycle."""
    import lionagi.state.db as state_db_mod
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    fixed_now = 1_000_000.0
    monkeypatch.setattr(engine_mod.time, "time", lambda: fixed_now)

    schedule = _minimal_schedule(
        id="sched-due-mid-pass",
        trigger_type="interval",
        cron_expr=None,
        interval_sec=60,
        next_fire_at=fixed_now - 1,
    )
    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[schedule])
    engine = SchedulerEngine(svc=svc)

    worker_started = asyncio.Event()
    worker_may_finish = asyncio.Event()

    async def _hung_worker_tick(now):
        worker_started.set()
        await worker_may_finish.wait()

    engine._run_task_worker_tick = _hung_worker_tick  # type: ignore[method-assign]

    with (
        patch(
            "lionagi.studio.services.lifecycle.run_periodic_reapers",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "lionagi.studio.services.db_maintenance.checkpoint_state_db",
            new=AsyncMock(return_value=None),
        ),
        patch.object(engine, "_maybe_fire", new=AsyncMock()) as mock_fire,
    ):
        # If _tick() still awaits the worker pass inline, this times out
        # because worker_may_finish is never set before the deadline.
        await asyncio.wait_for(engine._tick(), timeout=2.0)

    # asyncio.wait_for() does not guarantee that the coroutine it awaits runs
    # as a separately scheduled Task -- CPython >=3.12 drives a plain
    # coroutine inline within the caller's own step when timeout > 0, so the
    # worker task created inside _tick() may not get a scheduling turn
    # before wait_for() returns. An explicit checkpoint gives the event loop
    # one turn to run any already-scheduled callback, which is what actually
    # proves the worker task was created and is runnable -- independent of
    # whichever internal strategy wait_for() happens to use.
    await asyncio.sleep(0)

    assert worker_started.is_set(), "worker pass never started"
    mock_fire.assert_awaited_once()

    worker_may_finish.set()
    if engine._worker_task is not None:
        await asyncio.wait_for(engine._worker_task, timeout=2.0)


@pytest.mark.asyncio
async def test_tick_worker_pass_is_single_flight(monkeypatch, tmp_path):
    """A second _tick() must not start a second worker-pass task while the
    first is still running -- single-flight, not an unguarded task per tick."""
    import lionagi.state.db as state_db_mod
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(engine_mod.time, "time", lambda: 2_000_000.0)

    svc = _make_svc()
    svc.list_schedules = AsyncMock(return_value=[])
    engine = SchedulerEngine(svc=svc)

    start_count = 0
    worker_may_finish = asyncio.Event()

    async def _hung_worker_tick(now):
        nonlocal start_count
        start_count += 1
        await worker_may_finish.wait()

    engine._run_task_worker_tick = _hung_worker_tick  # type: ignore[method-assign]

    with (
        patch(
            "lionagi.studio.services.lifecycle.run_periodic_reapers",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "lionagi.studio.services.db_maintenance.checkpoint_state_db",
            new=AsyncMock(return_value=None),
        ),
    ):
        await asyncio.wait_for(engine._tick(), timeout=2.0)
        await asyncio.wait_for(engine._tick(), timeout=2.0)

    assert start_count == 1, (
        f"Expected exactly one worker-pass task started while the first is "
        f"still in flight, got {start_count}"
    )

    worker_may_finish.set()
    if engine._worker_task is not None:
        await asyncio.wait_for(engine._worker_task, timeout=2.0)


# max_runs / one-shot semantics


@pytest.mark.asyncio
async def test_max_runs_reached_auto_disables_schedule():
    """Once fired top-level runs hit max_runs, the schedule is disabled."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.count_schedule_runs = AsyncMock(return_value=3)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=3)

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
        await fire_with_claim(engine, schedule, "run-once-1", trigger_context={"scheduled": True})

    svc.count_schedule_runs.assert_awaited_with("sched-001", chain_depth=0)
    disable_calls = [c for c in svc.update_schedule.await_args_list if c.kwargs.get("enabled") == 0]
    assert disable_calls, "Expected update_schedule(..., enabled=0) once max_runs is reached"


@pytest.mark.asyncio
async def test_max_runs_admission_counts_inflight_running_row():
    """A bounded schedule with a persisted 'running' occurrence must refuse a
    new admission even with no in-process claim held (the daemon-restart
    shape: claims are in-memory, the running row is not). A fired run
    consumes budget when it fires, not when it resolves."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()

    async def count(sid, *, chain_depth=0, statuses=None, fired_after=None):
        # One run is mid-execution: zero terminal rows, one 'running' row.
        if statuses and "running" in statuses:
            return 1
        return 0

    svc.count_schedule_runs = AsyncMock(side_effect=count)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=1)

    allowed, claim = await engine._reserve_max_runs_budget(schedule)
    assert allowed is False
    assert claim is None


@pytest.mark.asyncio
async def test_max_runs_admission_does_not_double_count_transferred_fire():
    """A fire whose claim has transferred to its durable 'running' row must
    consume exactly one unit of budget: with max_runs=2 and one fire
    mid-execution (row written, claim released), a second admission must
    still be allowed."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()

    async def count(sid, *, chain_depth=0, statuses=None, fired_after=None):
        if statuses and "running" in statuses:
            return 1  # the mid-execution fire's running row
        return 0  # no terminal rows yet

    svc.count_schedule_runs = AsyncMock(side_effect=count)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=2)

    # The mid-execution fire's claim was released when its occurrence row
    # committed (the _fire_inner transfer), so only the row represents it.
    allowed, claim = await engine._reserve_max_runs_budget(schedule)
    assert allowed is True, "a transferred fire must consume one unit, not two"
    assert claim is not None
    claim.release()


@pytest.mark.asyncio
async def test_max_runs_restart_row_and_new_claim_are_distinct_fires():
    """A restart-orphaned 'running' row (its claim died with the process)
    and a fresh claim-only admission are DIFFERENT fires: with max_runs=2,
    one orphan row plus one admitted claim must exhaust the budget. A max()
    of the two views would collapse them and admit a third fire."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()

    async def count(sid, *, chain_depth=0, statuses=None, fired_after=None):
        # The pre-restart orphan: one durable running row, zero terminal.
        if statuses and "running" in statuses:
            return 1
        return 0

    svc.count_schedule_runs = AsyncMock(side_effect=count)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=2)

    # First new admission after restart: orphan row (1) + no claims -> allowed.
    allowed_first, claim_first = await engine._reserve_max_runs_budget(schedule)
    assert allowed_first is True and claim_first is not None

    # Second admission before the first writes its row: orphan row (1) +
    # first fire's claim (1) fills max_runs=2 -> must refuse.
    allowed_second, claim_second = await engine._reserve_max_runs_budget(schedule)
    assert allowed_second is False, "orphan row + new claim are two fires against a cap of two"
    assert claim_second is None
    claim_first.release()


@pytest.mark.asyncio
async def test_fire_transfers_max_runs_claim_at_occurrence_commit():
    """_fire() must release the max_runs claim as soon as the occurrence row
    commits — not only from its terminal finally block — so budget ownership
    lives in exactly one place while the action is still executing."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=2)

    allowed, claim = await engine._reserve_max_runs_budget(schedule)
    assert allowed is True and claim is not None
    assert engine._max_runs_inflight.get("sched-001") == 1

    inflight_during_action: list[int] = []

    async def spawn(*args, **kwargs):
        # Runs after the occurrence row committed, before terminal writes.
        inflight_during_action.append(engine._max_runs_inflight.get("sched-001", 0))
        return (0, "")

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(side_effect=spawn),
        ),
    ):
        await fire_with_claim(
            engine, schedule, "run-xfer", trigger_context={"scheduled": True}, max_runs_claim=claim
        )

    assert inflight_during_action == [0], (
        "claim must transfer to the durable row before the action executes"
    )
    assert engine._max_runs_inflight.get("sched-001", 0) == 0


@pytest.mark.asyncio
async def test_max_runs_not_reached_leaves_schedule_enabled():
    """Fewer fired runs than max_runs must not touch the enabled flag."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.count_schedule_runs = AsyncMock(return_value=1)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=3)

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
        await fire_with_claim(engine, schedule, "run-once-2", trigger_context={"scheduled": True})

    disable_calls = [c for c in svc.update_schedule.await_args_list if c.kwargs.get("enabled") == 0]
    assert not disable_calls


@pytest.mark.asyncio
async def test_max_runs_none_is_unlimited_never_checks_count():
    """max_runs=None (the default/unlimited case) must not query run counts at all."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()  # no max_runs key -> schedule.get("max_runs") is None

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
        await fire_with_claim(
            engine, schedule, "run-unlimited", trigger_context={"scheduled": True}
        )

    svc.count_schedule_runs.assert_not_awaited()
    disable_calls = [c for c in svc.update_schedule.await_args_list if c.kwargs.get("enabled") == 0]
    assert not disable_calls


@pytest.mark.asyncio
async def test_max_runs_chain_child_never_checked():
    """chain_depth>0 (on_success/on_fail children) never consumes the parent's budget."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=1)

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
        await fire_with_claim(
            engine,
            schedule,
            "run-child",
            trigger_context={"scheduled": True},
            chain_depth=1,
            chain_parent_id="run-parent",
        )

    svc.count_schedule_runs.assert_not_awaited()
    disable_calls = [c for c in svc.update_schedule.await_args_list if c.kwargs.get("enabled") == 0]
    assert not disable_calls


@pytest.mark.asyncio
async def test_max_runs_build_argv_exception_still_checked():
    """A build_argv failure still records a terminal run and checks max_runs."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.count_schedule_runs = AsyncMock(return_value=1)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(max_runs=1)

    with patch(
        "lionagi.studio.scheduler.subprocess.build_argv",
        side_effect=ValueError("bad action_kind"),
    ):
        await fire_with_claim(engine, schedule, "run-badargv", trigger_context={"scheduled": True})

    svc.count_schedule_runs.assert_awaited_with("sched-001", chain_depth=0)
    disable_calls = [c for c in svc.update_schedule.await_args_list if c.kwargs.get("enabled") == 0]
    assert disable_calls


# max_runs enforcement BEFORE firing (pre-flight reservation), not just after


class _StatefulSvc:
    """Minimal stateful fake mirroring real StateDB run bookkeeping.

    Unlike _make_svc()'s AsyncMock (fixed return values), this actually
    records schedule_runs and derives count_schedule_runs() from them —
    needed to pin the pre-flight max_runs reservation, which depends on the
    real interaction between "check the count" and "record a new run".
    """

    def __init__(
        self,
        existing_runs: dict[str, dict] | None = None,
        fail_create_invocation_times: int = 0,
        schedule_row: dict | None = None,
    ):
        # Stands for the schedule row the claims are decided against. Empty by default, which
        # reads as a row whose cursors are NULL; a test modelling a refusal seeds it. Held by
        # reference rather than copied, so a caller passing the schedule dict it also hands the
        # engine sees its own writes, the way a caller re-reading the row would.
        self.schedule_row: dict = schedule_row if schedule_row is not None else {}
        self.runs: dict[str, dict] = dict(existing_runs or {})
        self.schedule_updates: list[tuple[str, dict]] = []
        self._fail_create_invocation_times = fail_create_invocation_times
        self.create_invocation_calls = 0

    async def get_schedule(self, schedule_id):
        return None

    async def list_schedules(self, *, enabled=None):
        return []

    def _claims_hold(self, expect_next_fire_at, expect_github_cursor) -> bool:
        """Decide both claims against the modelled row rather than accepting either.

        Returning True regardless would let a claim-dependent test pass without the claim
        being checked at all, which is the same as not having written the claim.
        """
        return claim_holds(self.schedule_row.get("next_fire_at"), expect_next_fire_at) and (
            claim_holds(self.schedule_row.get("github_cursor"), expect_github_cursor)
        )

    async def update_schedule(
        self,
        schedule_id,
        *,
        guard_cursor_forward=False,
        expect_next_fire_at=NO_CURSOR_CLAIM,
        expect_github_cursor=NO_CURSOR_CLAIM,
        audit_context=None,
        reason=None,
        **fields,
    ):
        # Mirrors the real signature and return type. Absorbing the claims into **fields
        # would record them as schedule columns and return None, which the recovery path
        # reads as a refusal, so a test reusing this fake would model a different interface.
        if not self._claims_hold(expect_next_fire_at, expect_github_cursor):
            return False
        self.schedule_updates.append((schedule_id, fields))
        self.schedule_row.update(fields)
        return True

    async def count_schedule_runs(
        self,
        schedule_id,
        *,
        chain_depth=0,
        statuses=("completed", "failed", "cancelled", "timed_out"),
        fired_after=None,
    ):
        return sum(
            1
            for r in self.runs.values()
            if r.get("schedule_id") == schedule_id
            and r.get("chain_depth", 0) == chain_depth
            and r.get("status") in set(statuses)
            and (fired_after is None or (r.get("fired_at") or 0) >= fired_after)
        )

    async def create_schedule_run(self, run):
        self.runs[run["id"]] = dict(run)

    async def create_schedule_run_and_advance(
        self,
        run,
        *,
        schedule_id,
        schedule_fields,
        expect_next_fire_at,
        expect_github_cursor=NO_CURSOR_CLAIM,
    ):
        if not self._claims_hold(expect_next_fire_at, expect_github_cursor):
            return False
        self.runs[run["id"]] = dict(run)
        self.schedule_updates.append((schedule_id, dict(schedule_fields)))
        self.schedule_row.update(schedule_fields)
        return True

    async def schedule_run_exists_since(self, schedule_id, since):
        return any(
            r.get("schedule_id") == schedule_id and (r.get("fired_at") or 0) >= since
            for r in self.runs.values()
        )

    async def update_schedule_run(self, run_id, **fields):
        self.runs[run_id].update(fields)

    async def create_invocation(self, invocation):
        self.create_invocation_calls += 1
        if self.create_invocation_calls <= self._fail_create_invocation_times:
            raise RuntimeError("transient invocation insert failure")

    async def update_invocation(self, inv_id, **fields):
        pass

    async def merge_invocation_node_metadata(self, inv_id, patch):
        pass

    async def update_status(self, entity_type, entity_id, *, new_status, **kwargs):
        if entity_type == "schedule_run":
            self.runs[entity_id]["status"] = new_status

    async def list_sessions_for_invocation(self, invocation_id):
        return []


@pytest.mark.asyncio
async def test_max_runs_exhausted_schedule_refuses_to_fire_again():
    """A schedule that already has a terminal run at its max_runs cap must not
    fire again — the budget check happens BEFORE queueing the fire, not only
    after it completes."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _StatefulSvc(
        existing_runs={
            "old-run": {"schedule_id": "sched-once", "chain_depth": 0, "status": "completed"}
        }
    )
    engine = SchedulerEngine(svc)
    schedule = _minimal_schedule(id="sched-once", max_runs=1)

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["true"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await engine._maybe_fire(schedule, now=1000.0)
        await asyncio.gather(*list(engine._fire_tasks))

    assert await svc.count_schedule_runs("sched-once", chain_depth=0) == 1
    disable_calls = [c for c in svc.schedule_updates if c[1].get("enabled") == 0]
    assert disable_calls


@pytest.mark.asyncio
async def test_max_runs_sequential_maybe_fire_calls_do_not_overshoot():
    """Two back-to-back _maybe_fire() calls for a fresh max_runs=1 schedule
    must produce exactly one terminal run, not two — the pre-flight claim is
    made (and visible) before the first call's background fire even starts
    running, so the second call's check sees the claim."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _StatefulSvc()
    engine = SchedulerEngine(svc)
    schedule = _minimal_schedule(id="sched-once", max_runs=1)

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["true"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await engine._maybe_fire(schedule, now=1000.0)
        await engine._maybe_fire(schedule, now=1000.0)
        await asyncio.gather(*list(engine._fire_tasks))

    assert len(svc.runs) == 1
    assert sorted(r["status"] for r in svc.runs.values()) == ["completed"]


@pytest.mark.asyncio
async def test_max_runs_reservation_released_lets_next_schedule_check_run():
    """After a claimed fire completes, its in-process reservation is released
    so a later _maybe_fire() call correctly sees the up-to-date persisted
    count (not an over-counted stale claim)."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    schedule = _minimal_schedule(id="sched-multi", max_runs=2)
    # The fake decides the due-instant claim against this row, so it is the same object the
    # engine is handed: a fire advances it, and the next fire claims the advanced value.
    svc = _StatefulSvc(schedule_row=schedule)
    engine = SchedulerEngine(svc)

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["true"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await engine._maybe_fire(schedule, now=1000.0)
        await asyncio.gather(*list(engine._fire_tasks))
        assert engine._max_runs_inflight.get("sched-multi", 0) == 0

        await engine._maybe_fire(schedule, now=1001.0)
        await asyncio.gather(*list(engine._fire_tasks))

    assert len(svc.runs) == 2
    disable_calls = [c for c in svc.schedule_updates if c[1].get("enabled") == 0]
    assert disable_calls  # the second fire reaches max_runs=2 and disables


@pytest.mark.asyncio
async def test_fire_now_refuses_manual_trigger_when_max_runs_exhausted():
    """fire_now() (manual `li schedule trigger`) must also respect max_runs —
    it is a top-level fire like any other."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _StatefulSvc(
        existing_runs={
            "old-run": {"schedule_id": "sched-once", "chain_depth": 0, "status": "completed"}
        }
    )
    svc.get_schedule = AsyncMock(return_value=_minimal_schedule(id="sched-once", max_runs=1))
    engine = SchedulerEngine(svc)

    with pytest.raises(ValueError, match="max_runs"):
        await engine.fire_now("sched-once")

    assert len(engine._fire_tasks) == 0


@pytest.mark.asyncio
async def test_max_runs_claim_released_on_pre_run_failure_allows_retry():
    """A max_runs claim must not leak when the fire fails before a terminal
    schedule_run is ever recorded (e.g. create_invocation() raising).

    Reserves the budget, lets create_invocation
    blow up once, confirms the claim is released (not stuck inflight with zero
    terminal runs), then confirm a retry fire succeeds and the schedule
    completes exactly max_runs times total — not zero (stuck) and not more
    than max_runs (double-fired)."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _StatefulSvc(fail_create_invocation_times=1)
    svc.get_schedule = AsyncMock(return_value=_minimal_schedule(id="sched-once", max_runs=1))
    engine = SchedulerEngine(svc)

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["true"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        first = await engine.fire_now("sched-once")
        await asyncio.gather(*list(engine._fire_tasks), return_exceptions=True)

        # The first fire's create_invocation() raised before any terminal
        # schedule_run was recorded — the claim must have been released, not
        # left stuck inflight.
        assert first is not None
        assert await svc.count_schedule_runs("sched-once", chain_depth=0) == 0
        assert engine._max_runs_inflight.get("sched-once", 0) == 0

        # A retry must be allowed (the exhausted-budget ValueError must NOT
        # fire here — that would mean the claim leaked) and must complete.
        second = await engine.fire_now("sched-once")
        await asyncio.gather(*list(engine._fire_tasks), return_exceptions=True)

    assert second is not None
    assert await svc.count_schedule_runs("sched-once", chain_depth=0) == 1
    disable_calls = [c for c in svc.schedule_updates if c[1].get("enabled") == 0]
    assert disable_calls  # exactly max_runs=1 total run reached; auto-disabled


@pytest.mark.asyncio
async def test_max_runs_reservation_snapshots_inflight_before_stale_count_read():
    """A concurrent reserve must not overshoot max_runs by combining a stale
    persisted count with an already-released in-flight claim. Forces fire A
    to complete (recording its run and releasing its claim) entirely inside
    the window where B's own count_schedule_runs() read is suspended, so B's
    read comes back stale. See docs/internals/studio.md's
    scheduler/engine.py section (`_reserve_max_runs_budget`) for why reading
    `inflight` before that await is what keeps this safe."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _StatefulSvc()
    engine = SchedulerEngine(svc)
    schedule = _minimal_schedule(id="sched-once", max_runs=1)

    # Fire A claims the budget first (simulates A's fire already in-flight,
    # not yet terminal).
    allowed_a, claim_a = await engine._reserve_max_runs_budget(schedule)
    assert allowed_a
    assert claim_a is not None
    assert engine._max_runs_inflight.get("sched-once") == 1

    count_started = asyncio.Event()
    resume_count = asyncio.Event()
    real_count = svc.count_schedule_runs

    async def stalling_count(schedule_id, *, chain_depth=0, **kwargs):
        # Read the count as of THIS moment (before A's terminal write
        # lands), but don't return it until told to -- after A has both
        # recorded its terminal run and released its claim.
        snapshot = await real_count(schedule_id, chain_depth=chain_depth, **kwargs)
        count_started.set()
        await resume_count.wait()
        return snapshot

    svc.count_schedule_runs = stalling_count

    b_task = asyncio.create_task(engine._reserve_max_runs_budget(schedule))
    await count_started.wait()

    # Fire A "completes" while B's count read is still suspended: record its
    # terminal run, then release its claim -- exactly what _fire()'s finally
    # does at the end of a real fire.
    await svc.create_schedule_run(
        {"id": "run-a", "schedule_id": "sched-once", "chain_depth": 0, "status": "completed"}
    )
    claim_a.release()
    assert engine._max_runs_inflight.get("sched-once", 0) == 0

    resume_count.set()
    allowed_b, claim_b = await b_task

    assert not allowed_b
    assert claim_b is None
    # Exactly one terminal run for max_runs=1 -- B must not have overshot it.
    assert await real_count("sched-once", chain_depth=0) == 1


# _fire() — action_kind='command'


@pytest.mark.asyncio
async def test_fire_command_kind_skips_li_resolution():
    """kind='command' spawns an allow-listed executable directly, never
    through `li` -- _fire() must not call resolve_li_executable() for it
    (a daemon host where `li` is unresolvable must not block a command fire)."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        action_kind="command",
        action_model=None,
        action_prompt=None,
        action_command="kdev",
        action_command_args=["review-pr"],
    )

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.resolve_li_executable",
            return_value=(None, "must not be called for kind='command'"),
        ) as resolve_mock,
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["kdev", "review-pr"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await fire_with_claim(engine, schedule, "run-cmd-001", trigger_context={"scheduled": True})

    resolve_mock.assert_not_called()
    svc.create_schedule_run_and_advance.assert_awaited_once()
    failed_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.args[0] == "schedule_run" and c.kwargs.get("new_status") == "failed"
    ]
    assert not failed_calls, "command-kind fire must not fail from a missing `li` resolution"


@pytest.mark.asyncio
async def test_fire_command_kind_nonzero_exit_records_failed_status():
    """Exit-code semantics for kind='command' are unmodified: nonzero exit
    still produces a 'failed' schedule_run status, same as every other kind."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(
        action_kind="command",
        action_model=None,
        action_prompt=None,
        action_command="kdev",
        action_command_args=[],
    )

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["kdev"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(1, "command failed")),
        ),
    ):
        await fire_with_claim(engine, schedule, "run-cmd-002", trigger_context={"scheduled": True})

    failed_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.args[0] == "schedule_run" and c.kwargs.get("new_status") == "failed"
    ]
    assert failed_calls, "Expected update_status('schedule_run', ..., new_status='failed')"


# error_detail on the broad-except handler (real StateDB)


class _DbSvc:
    """SchedulerStateService over one open StateDB (in-memory, one connection)."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def compute_files_overlap(self, invocation_id: str, *, top_n: int = 5) -> dict:
        return {"count": 0, "top": []}


async def _seed_schedule(db, schedule: dict) -> None:
    await db.create_schedule(
        {
            "id": schedule["id"],
            "name": schedule["name"],
            "trigger_type": "interval",
            "interval_sec": 3600,
            "action_kind": schedule["action_kind"],
        }
    )


@pytest_asyncio.fixture
async def state_db():
    from lionagi.state.db import StateDB

    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


@pytest.mark.asyncio
async def test_fire_exception_keeps_the_error_detail_a_prior_finalizer_wrote(state_db):
    """A concurrent finalizer that already moved the run to a terminal status
    owns its error_detail. The broad-except handler's write is guarded on the
    row still being 'running', so a lost race must leave the winner's text in
    place instead of replacing it with the handler's own."""
    from lionagi.state.reasons import RunReasons
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _DbSvc(state_db)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()
    await _seed_schedule(state_db, schedule)
    run_id = "run-race-detail"

    async def _reaper_then_raise(*args, **kwargs):
        # Stand in for the deadline reaper winning this row mid-fire: it
        # finalizes with the real cause, then the fire path blows up.
        await state_db.update_status(
            "schedule_run",
            run_id,
            new_status="timed_out",
            reason_code=RunReasons.TIMED_OUT_DEADLINE,
            reason_summary="Run exceeded its deadline.",
            evidence_refs=[],
            source="system",
            actor="reaper",
            expected_statuses={"running"},
            extra_fields={"error_detail": "TimeoutError: run exceeded its deadline"},
        )
        raise RuntimeError("spawn exploded")

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(side_effect=_reaper_then_raise),
        ),
    ):
        await fire_with_claim(engine, schedule, run_id, trigger_context={"scheduled": True})

    row = await state_db.get_schedule_run(run_id)
    assert row["error_detail"] == "TimeoutError: run exceeded its deadline"


@pytest.mark.asyncio
async def test_fire_cancellation_keeps_the_error_detail_a_prior_finalizer_wrote(state_db):
    """Same guarantee on the cancellation branch. "Scheduler shutdown" is a
    placeholder rather than a measured cause, so a shutdown arriving after some
    other writer already finalized the row must not replace the cause that
    writer recorded."""
    from lionagi.state.reasons import RunReasons
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _DbSvc(state_db)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()
    await _seed_schedule(state_db, schedule)
    run_id = "run-race-cancel"

    async def _reaper_then_cancel(*args, **kwargs):
        await state_db.update_status(
            "schedule_run",
            run_id,
            new_status="timed_out",
            reason_code=RunReasons.TIMED_OUT_DEADLINE,
            reason_summary="Run exceeded its deadline.",
            evidence_refs=[],
            source="system",
            actor="reaper",
            expected_statuses={"running"},
            extra_fields={"error_detail": "TimeoutError: run exceeded its deadline"},
        )
        raise asyncio.CancelledError()

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(side_effect=_reaper_then_cancel),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await fire_with_claim(engine, schedule, run_id, trigger_context={"scheduled": True})

    row = await state_db.get_schedule_run(run_id)
    assert row["status"] == "timed_out"
    assert row["error_detail"] == "TimeoutError: run exceeded its deadline"


@pytest.mark.asyncio
async def test_fire_completion_keeps_the_outcome_a_prior_finalizer_wrote(state_db):
    """And on the ordinary completion path, whose columns are measured rather
    than placeholders. A nonzero exit still does not entitle this path to
    overwrite the exit_code, end time and cause of a row someone else already
    finalized -- the terminal record belongs to whoever won the transition."""
    from lionagi.state.reasons import RunReasons
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _DbSvc(state_db)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()
    await _seed_schedule(state_db, schedule)
    run_id = "run-race-complete"

    async def _reaper_then_exit_nonzero(*args, **kwargs):
        # Deliberately writes only error_detail through extra_fields. Adding
        # exit_code here would make this fixture itself unrunnable against a
        # tree whose allowlist lacks that key, and the test would then fail for
        # its own incompatibility rather than for the overwrite it exists to
        # catch. exit_code is instead asserted to stay unset below.
        await state_db.update_status(
            "schedule_run",
            run_id,
            new_status="timed_out",
            reason_code=RunReasons.TIMED_OUT_DEADLINE,
            reason_summary="Run exceeded its deadline.",
            evidence_refs=[],
            source="system",
            actor="reaper",
            expected_statuses={"running"},
            extra_fields={"error_detail": "TimeoutError: run exceeded its deadline"},
        )
        return (2, "some stderr the loser measured")

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(side_effect=_reaper_then_exit_nonzero),
        ),
    ):
        await fire_with_claim(engine, schedule, run_id, trigger_context={"scheduled": True})

    row = await state_db.get_schedule_run(run_id)
    assert row["status"] == "timed_out"
    assert row["error_detail"] == "TimeoutError: run exceeded its deadline"
    # The losing path measured exit code 2 and its own stderr. Neither belongs
    # on a row it did not finalize.
    assert row["exit_code"] is None


@pytest.mark.asyncio
async def test_fire_exception_records_the_real_exception_text_as_error_detail(state_db):
    """On the ordinary path (nothing else finalized the row) the handler owns
    the record, and the error_detail it stores is the same text the signal
    carries — real exception type and message, not a generic placeholder."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _DbSvc(state_db)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()
    await _seed_schedule(state_db, schedule)
    run_id = "run-real-detail"

    async def _raise_after_launch(*args, on_launched=None, **kwargs):
        # A failure of something that started: that is what the handler
        # records terminally (an exception before any process exists leaves
        # the run undispatched for startup recovery instead).
        if on_launched is not None:
            await on_launched()
        raise ModuleNotFoundError("No module named 'nope'")

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(side_effect=_raise_after_launch),
        ),
    ):
        await fire_with_claim(engine, schedule, run_id, trigger_context={"scheduled": True})

    row = await state_db.get_schedule_run(run_id)
    assert row["status"] == "failed"
    assert row["error_detail"] == "ModuleNotFoundError: No module named 'nope'"
    assert row["ended_at"] is not None


@pytest.mark.asyncio
async def test_fire_deadline_records_one_timed_out_terminal_and_releases_slot(
    state_db, monkeypatch
):
    """A launcher deadline is a timed-out outcome, not a generic failed exception."""
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler import subprocess as subprocess_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 1)
    svc = _DbSvc(state_db)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()
    await _seed_schedule(state_db, schedule)
    run_id = "run-deadline"
    _, slot_claim = await engine._reserve_global_slot("test-holder", path="scheduled")
    assert engine._global_inflight == 1

    async def _deadline(*_args, on_launched=None, **_kwargs):
        if on_launched is not None:
            await on_launched()
        raise subprocess_mod.SubprocessDeadlineExceededError(
            invocation_id="inv-deadline",
            deadline_seconds=2,
        )

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(side_effect=_deadline),
        ),
    ):
        await fire_with_claim(
            engine,
            schedule,
            run_id,
            trigger_context={"scheduled": True},
            global_slot_claim=slot_claim,
        )

    run = await state_db.get_schedule_run(run_id)
    invocation = await state_db.get_invocation(run["invocation_id"])
    assert run["status"] == "timed_out"
    assert run["status_reason_code"] == "run.timed_out.deadline"
    assert invocation["status"] == "timed_out"
    assert invocation["status_reason_code"] == "run.timed_out.deadline"
    terminal_events = await state_db.fetch_all(
        "SELECT entity_id, status FROM status_transitions "
        "WHERE entity_id IN (?, ?) AND status = 'timed_out'",
        (run_id, invocation["id"]),
    )
    assert sorted(event["entity_id"] for event in terminal_events) == sorted(
        [run_id, invocation["id"]]
    )
    assert engine._global_inflight == 0


# Tick-loop supervision


async def _until(predicate, timeout: float = 5.0) -> None:
    """Wait for a condition the loop reaches on its own, rather than sleeping a guess."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never held")


async def _supervised_engine(monkeypatch, ticks: list):
    from lionagi.studio.scheduler import engine as engine_mod

    monkeypatch.setattr(engine_mod, "_TICK_INTERVAL", 0.005)
    monkeypatch.setattr(engine_mod, "_TICK_RESTART_BACKOFF", (0.005,))
    engine = engine_mod.SchedulerEngine(svc=_make_svc())

    async def _tick():
        ticks.append(time.monotonic())

    monkeypatch.setattr(engine, "_tick", _tick)
    monkeypatch.setattr(engine, "_backfill_action_cwd", AsyncMock())
    monkeypatch.setattr(engine, "_stamp_effective_timezones", AsyncMock())
    monkeypatch.setattr(engine, "_recompute_armed_cron_schedules", AsyncMock())
    monkeypatch.setattr(engine, "_recover_undispatched_fires", AsyncMock())
    monkeypatch.setattr(engine, "_reconcile_dispatched_orphans", AsyncMock())
    monkeypatch.setattr(engine, "_check_missed_fires", AsyncMock())
    return engine


@pytest.mark.asyncio
async def test_a_cancel_inside_startup_recovery_does_not_cost_the_later_passes(monkeypatch):
    """Recovery repairs durable state, so abandoning the rest of it leaves half-repairs behind."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    ran: list = []

    async def _cancelled_pass():
        ran.append("first")
        raise asyncio.CancelledError

    async def _second():
        ran.append("second")

    async def _third():
        ran.append("third")

    monkeypatch.setattr(engine, "_recover_undispatched_fires", _cancelled_pass)
    monkeypatch.setattr(engine, "_reconcile_dispatched_orphans", _second)
    monkeypatch.setattr(engine, "_check_missed_fires", _third)

    await engine.start()
    try:
        await _until(lambda: len(ticks) >= 1)
        assert ran == ["first", "second", "third"]
        assert engine._tick_loop_restarts == 0
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_cancel_aimed_at_the_loop_lets_the_recovery_pass_in_flight_finish(monkeypatch):
    """A pass interrupted after it has finalized a run has no successor to finish the job.

    Every later scan selects rows that are still running, which such a row no longer is, so
    tearing a pass in half leaves durable state nothing repairs. Absorbing the cancel and
    moving to the next pass is not enough; the pass in flight has to complete.
    """
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    entered = asyncio.Event()
    ran: list = []

    async def _slow_first():
        ran.append("entered")
        entered.set()
        await asyncio.sleep(0.05)
        ran.append("finished")

    monkeypatch.setattr(engine, "_recover_undispatched_fires", _slow_first)
    monkeypatch.setattr(
        engine, "_reconcile_dispatched_orphans", AsyncMock(side_effect=lambda: ran.append("second"))
    )
    monkeypatch.setattr(
        engine, "_check_missed_fires", AsyncMock(side_effect=lambda: ran.append("third"))
    )

    await engine.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        engine._task.cancel()
        await _until(lambda: len(ticks) >= 1)
        assert "finished" in ran, f"the interrupted pass was abandoned: {ran}"
        assert ran == ["entered", "finished", "second", "third"]
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_repeated_cancels_aimed_at_the_loop_still_let_the_recovery_pass_finish(monkeypatch):
    """One absorbed cancel is not the guarantee; the pass has to survive every one of them.

    Absorbing the first and then awaiting the task directly leaves the second cancel free to
    interrupt exactly the repair the first was absorbed to protect, so this fires three.
    """
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    entered = asyncio.Event()
    ran: list = []

    async def _slow_first():
        ran.append("entered")
        entered.set()
        await asyncio.sleep(0.15)
        ran.append("finished")

    monkeypatch.setattr(engine, "_recover_undispatched_fires", _slow_first)
    monkeypatch.setattr(
        engine, "_reconcile_dispatched_orphans", AsyncMock(side_effect=lambda: ran.append("second"))
    )
    monkeypatch.setattr(
        engine, "_check_missed_fires", AsyncMock(side_effect=lambda: ran.append("third"))
    )

    await engine.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        for _ in range(3):
            engine._task.cancel()
            await asyncio.sleep(0)
        await _until(lambda: len(ticks) >= 1)
        assert "finished" in ran, f"a later cancel abandoned the pass: {ran}"
        assert ran == ["entered", "finished", "second", "third"]
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_stop_during_recovery_does_not_wait_for_the_pass_in_flight(monkeypatch):
    """Control: a shutdown that cannot interrupt recovery is a shutdown that hangs."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    entered = asyncio.Event()
    ran: list = []

    async def _very_slow_first():
        entered.set()
        await asyncio.sleep(30)
        ran.append("finished")

    monkeypatch.setattr(engine, "_recover_undispatched_fires", _very_slow_first)

    await engine.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    started = time.monotonic()
    await asyncio.wait_for(engine.stop(), timeout=5)
    assert time.monotonic() - started < 5
    assert ran == []


@pytest.mark.asyncio
async def test_stop_drains_recovery_cancellation_before_closing_the_state_service(monkeypatch):
    """Cancellation cleanup may still use the store, so close must come after it."""
    from lionagi.studio.scheduler.engine import SchedulerEngine
    from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

    svc = _DBSchedulerStateService(persistent=True)
    close = AsyncMock()
    monkeypatch.setattr(svc, "close", close)
    engine = SchedulerEngine(svc=svc)
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def _recovery_with_delayed_cleanup():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleanup_finished.set()

    monkeypatch.setattr(engine, "_startup_recovery_passes", _recovery_with_delayed_cleanup)
    engine._task = asyncio.create_task(engine._run_startup_recovery())
    await asyncio.wait_for(entered.wait(), timeout=2)

    stopping = asyncio.create_task(engine.stop())
    await asyncio.wait_for(cleanup_started.wait(), timeout=2)
    try:
        await asyncio.sleep(0)
        assert not stopping.done(), "stop returned while recovery cleanup was still running"
        close.assert_not_awaited()
    finally:
        release_cleanup.set()
        await asyncio.wait_for(stopping, timeout=2)

    assert cleanup_finished.is_set()
    close.assert_awaited_once()


@pytest.mark.slow_timing
@pytest.mark.asyncio
async def test_a_recovery_cleanup_that_never_returns_holds_stop_only_for_the_unwind_bound(
    monkeypatch, caplog
):
    """stop() waits for the unwind, but a cleanup that never returns must not hang shutdown."""
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine
    from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

    monkeypatch.setattr(engine_mod, "_RECOVERY_UNWIND_SECONDS", 0.2)
    svc = _DBSchedulerStateService(persistent=True)
    close = AsyncMock()
    monkeypatch.setattr(svc, "close", close)
    engine = SchedulerEngine(svc=svc)
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    passes: list[asyncio.Task] = []

    async def _recovery_whose_cleanup_does_not_return():
        passes.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    monkeypatch.setattr(engine, "_startup_recovery_passes", _recovery_whose_cleanup_does_not_return)
    engine._task = asyncio.create_task(engine._run_startup_recovery())
    await asyncio.wait_for(entered.wait(), timeout=2)

    started = time.monotonic()
    stopping = asyncio.create_task(engine.stop())
    await asyncio.wait_for(cleanup_started.wait(), timeout=2)
    try:
        await asyncio.sleep(0)
        assert not stopping.done(), "stop returned without waiting for the unwind"
        # asyncio.wait, not wait_for: stop() absorbs a cancel of its own caller, so a timeout
        # that cancels it would read as stop() returning
        with caplog.at_level(logging.WARNING, logger="lionagi.studio.scheduler.engine"):
            done, _ = await asyncio.wait({stopping}, timeout=5)
        assert stopping in done, "stop is still waiting on a cleanup that never returns"
        elapsed = time.monotonic() - started
        assert elapsed >= 0.2, "stop returned before the unwind bound"
        # ten times the bound, the margin a real-deadline test keeps for a slow machine
        assert elapsed < 2.0, "stop waited well past the unwind bound"
        close.assert_awaited_once()
        assert not passes[0].done()
        assert "still unwinding" in caplog.text
    finally:
        release_cleanup.set()
        await asyncio.wait([*passes, stopping], timeout=2)


@pytest.mark.asyncio
async def test_a_second_stop_during_the_recovery_unwind_still_waits_for_it(monkeypatch):
    """A second stop() during the unwind waits for the first; neither closes the store before
    the cleanup has finished."""
    from lionagi.studio.scheduler.engine import SchedulerEngine
    from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

    svc = _DBSchedulerStateService(persistent=True)
    cleanup_finished = asyncio.Event()
    closed_after_cleanup: list[bool] = []
    close = AsyncMock(side_effect=lambda: closed_after_cleanup.append(cleanup_finished.is_set()))
    monkeypatch.setattr(svc, "close", close)
    engine = SchedulerEngine(svc=svc)
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def _recovery_with_delayed_cleanup():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleanup_finished.set()

    monkeypatch.setattr(engine, "_startup_recovery_passes", _recovery_with_delayed_cleanup)
    engine._task = asyncio.create_task(engine._run_startup_recovery())
    await asyncio.wait_for(entered.wait(), timeout=2)

    first = asyncio.create_task(engine.stop())
    await asyncio.wait_for(cleanup_started.wait(), timeout=2)
    second = asyncio.create_task(engine.stop())
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert not first.done() and not second.done()
        close.assert_not_awaited()
    finally:
        release_cleanup.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert closed_after_cleanup and all(closed_after_cleanup)


@pytest.mark.asyncio
async def test_a_second_stop_does_not_close_the_store_while_the_first_is_closing_it(monkeypatch):
    """stop() calls run one at a time: a second one waits for the first to finish closing."""
    from lionagi.studio.scheduler.engine import SchedulerEngine
    from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

    svc = _DBSchedulerStateService(persistent=True)
    closing = 0
    overlapped: list[bool] = []
    first_closing = asyncio.Event()
    release_close = asyncio.Event()

    async def _held_close():
        nonlocal closing
        closing += 1
        overlapped.append(closing > 1)
        first_closing.set()
        await release_close.wait()
        closing -= 1

    monkeypatch.setattr(svc, "close", _held_close)
    engine = SchedulerEngine(svc=svc)

    first = asyncio.create_task(engine.stop())
    await asyncio.wait_for(first_closing.wait(), timeout=2)
    second = asyncio.create_task(engine.stop())
    try:
        await asyncio.sleep(0.2)
        assert not second.done()
        assert closing == 1, "the second stop started closing while the first was"
    finally:
        release_close.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert overlapped == [False, False]


@pytest.mark.asyncio
async def test_a_cancel_of_stops_caller_during_the_recovery_unwind_still_waits_for_it(monkeypatch):
    """stop() awaits the loop, so a cancel of its caller reaches the recovery wait, which must
    absorb it rather than let the close overtake the cleanup. stop() then closes the store and
    returns normally, as it did before when that cancel reached it while it awaited the loop."""
    from lionagi.studio.scheduler.engine import SchedulerEngine
    from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

    svc = _DBSchedulerStateService(persistent=True)
    cleanup_finished = asyncio.Event()
    closed_after_cleanup: list[bool] = []
    close = AsyncMock(side_effect=lambda: closed_after_cleanup.append(cleanup_finished.is_set()))
    monkeypatch.setattr(svc, "close", close)
    engine = SchedulerEngine(svc=svc)
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def _recovery_with_delayed_cleanup():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleanup_finished.set()

    monkeypatch.setattr(engine, "_startup_recovery_passes", _recovery_with_delayed_cleanup)
    engine._task = asyncio.create_task(engine._run_startup_recovery())
    await asyncio.wait_for(entered.wait(), timeout=2)

    stopping = asyncio.create_task(engine.stop())
    await asyncio.wait_for(cleanup_started.wait(), timeout=2)
    stopping.cancel()
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        close.assert_not_awaited()
    finally:
        release_cleanup.set()
        await asyncio.wait({stopping}, timeout=2)

    assert stopping.done()
    assert not stopping.cancelled()
    assert stopping.result() is None
    assert closed_after_cleanup == [True]


def _live_tick_loops() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_coro().__qualname__ == "SchedulerEngine._tick_loop"
    ]


async def _end_tick_loops(engine) -> None:
    """Teardown for a failed test: a loop takes a cancel without a stop for a stray one."""
    engine._stopping = True
    loops = _live_tick_loops()
    for loop_task in loops:
        loop_task.cancel()
    if loops:
        await asyncio.wait(loops, timeout=2)


@pytest.mark.asyncio
async def test_a_start_during_the_recovery_unwind_waits_for_the_stop(monkeypatch):
    """stop() can wait for a recovery pass to unwind, so a start() can arrive inside that wait.
    It must not run until the stop has returned: clearing the stop flag under the old loop would
    let it run its later passes and keep ticking beside the new loop, and the start would come
    before the close."""
    from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    order: list[str] = []
    svc = _DBSchedulerStateService(persistent=True)
    monkeypatch.setattr(svc, "close", AsyncMock(side_effect=lambda: order.append("close")))
    engine._svc = svc
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    calls = 0
    old_passes: list[asyncio.Task] = []
    later_pass_tasks: list[asyncio.Task] = []

    async def _first_pass_with_held_cleanup():
        nonlocal calls
        calls += 1
        if calls > 1:
            return
        old_passes.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    async def _second_pass():
        later_pass_tasks.append(asyncio.current_task())

    monkeypatch.setattr(engine, "_recover_undispatched_fires", _first_pass_with_held_cleanup)
    monkeypatch.setattr(engine, "_reconcile_dispatched_orphans", _second_pass)
    await engine.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    monkeypatch.setattr(
        engine, "_backfill_action_cwd", AsyncMock(side_effect=lambda: order.append("start"))
    )
    stopping = asyncio.create_task(engine.stop())
    await asyncio.wait_for(cleanup_started.wait(), timeout=2)
    starting = asyncio.create_task(engine.start())
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert not starting.done(), "start ran inside the stop's wait"
        assert engine._stopping
        release_cleanup.set()
        await asyncio.wait_for(asyncio.gather(stopping, starting), timeout=2)
        assert order == ["close", "start"]
        await _until(lambda: len(ticks) >= 1)
        assert old_passes[0] not in later_pass_tasks, "the stopped loop ran a later pass"
        assert _live_tick_loops() == [engine._task]
        await asyncio.wait_for(engine.stop(), timeout=2)
        assert _live_tick_loops() == []
    finally:
        release_cleanup.set()
        await _end_tick_loops(engine)
        await asyncio.wait({stopping, starting}, timeout=2)


@pytest.mark.asyncio
async def test_a_start_created_with_a_stop_does_not_hold_the_stop(monkeypatch):
    """A start() created in the same loop step as a stop() must not clear the stop flag before
    the loop handles the stop's cancel, or the loop reads that cancel as a stray one and the stop
    waits on recovery for as long as it runs."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    entered = asyncio.Event()
    calls = 0

    async def _first_pass_that_never_returns():
        nonlocal calls
        calls += 1
        if calls > 1:
            return
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(engine, "_recover_undispatched_fires", _first_pass_that_never_returns)
    await engine.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    stopping = asyncio.create_task(engine.stop())
    starting = asyncio.create_task(engine.start())
    try:
        done, _ = await asyncio.wait({stopping}, timeout=2)
        assert stopping in done, "stop is waiting on a recovery pass that never returns"
        await asyncio.wait_for(starting, timeout=2)
        await _until(lambda: len(ticks) >= 1)
        assert _live_tick_loops() == [engine._task]
        await asyncio.wait_for(engine.stop(), timeout=2)
        assert _live_tick_loops() == []
    finally:
        await _end_tick_loops(engine)
        await asyncio.wait({stopping, starting}, timeout=2)


@pytest.mark.asyncio
async def test_a_stop_starts_no_later_pass_when_a_pass_cleanup_replaces_its_cancel(monkeypatch):
    """The stop's cancel reaches only the pass in flight; if that pass's cleanup raises another
    error instead, the passes after it must still not start during the stop."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    entered = asyncio.Event()
    ran: list = []

    async def _first_with_failing_cleanup():
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            raise RuntimeError("cleanup failed")

    async def _second():
        ran.append("second")

    async def _third():
        ran.append("third")

    monkeypatch.setattr(engine, "_recover_undispatched_fires", _first_with_failing_cleanup)
    monkeypatch.setattr(engine, "_reconcile_dispatched_orphans", _second)
    monkeypatch.setattr(engine, "_check_missed_fires", _third)

    await engine.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    await asyncio.wait_for(engine.stop(), timeout=5)
    assert ran == []


@pytest.mark.asyncio
async def test_a_cancel_during_the_inter_tick_wait_neither_ends_the_loop_nor_skips_the_delay(
    monkeypatch,
):
    """Absorbing a cancel and returning early let repeated cancels
    drive the tick in a tight loop."""
    from lionagi.studio.scheduler import engine as engine_mod

    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    monkeypatch.setattr(engine_mod, "_TICK_INTERVAL", 0.2)

    await engine.start()
    try:
        await _until(lambda: len(ticks) >= 1)
        started = time.monotonic()
        for _ in range(5):
            engine._task.cancel()
            await asyncio.sleep(0.01)
        # The wait is measured against its own deadline, so five cancels cannot buy a sixth tick.
        assert len(ticks) == 1, ticks
        assert engine._tick_loop_restarts == 0
        await _until(lambda: len(ticks) >= 2, timeout=2.0)
        assert time.monotonic() - started >= 0.15
    finally:
        await engine.stop()


@pytest.mark.parametrize("death", ("returns", "raises"))
@pytest.mark.asyncio
async def test_a_tick_loop_that_ends_while_running_is_replaced(monkeypatch, death):
    """A loop that ends unexpectedly is restarted even while its process remains up."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    calls: list = []

    async def _short_lived_loop():
        calls.append(time.monotonic())
        if len(calls) <= 2:
            if death == "raises":
                raise RuntimeError("loop blew up")
            return
        await asyncio.sleep(3600)

    monkeypatch.setattr(engine, "_tick_loop", _short_lived_loop)
    await engine.start()
    try:
        await _until(lambda: len(calls) >= 3)
        assert engine._tick_loop_restarts >= 2
        when, reason = engine._last_tick_loop_failure
        assert when > 0
        assert ("RuntimeError" in reason) is (death == "raises"), reason
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_an_external_cancel_is_absorbed_rather_than_ending_the_loop(monkeypatch):
    """Only stop() stops it. A cancel from anywhere else is the failure mode, not the request."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    await engine.start()
    try:
        await _until(lambda: len(ticks) >= 1)
        original = engine._task
        original.cancel()

        advanced = len(ticks)
        await _until(lambda: len(ticks) > advanced + 1)
        assert engine._task is original, "absorbed in place, so no restart was needed"
        assert engine._tick_loop_restarts == 0
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_stray_cancel_inside_a_tick_does_not_end_the_loop(monkeypatch):
    """A cancel escaping from something the tick awaited is not a shutdown request."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    raised = []

    async def _tick():
        ticks.append(time.monotonic())
        if not raised:
            raised.append(True)
            raise asyncio.CancelledError()

    monkeypatch.setattr(engine, "_tick", _tick)
    await engine.start()
    try:
        await _until(lambda: len(ticks) >= 3)
        assert raised, "the cancelling tick never ran, so this proves nothing"
        assert engine._tick_loop_restarts == 0, "the loop survived in place, without a restart"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_failing_startup_recovery_pass_does_not_cost_every_tick(monkeypatch):
    """Startup recovery is best effort; one bad pass must not take the loop with it."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    monkeypatch.setattr(
        engine, "_reconcile_dispatched_orphans", AsyncMock(side_effect=RuntimeError("db locked"))
    )
    await engine.start()
    try:
        await _until(lambda: len(ticks) >= 2)
        engine._check_missed_fires.assert_awaited()
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_raising_tick_does_not_end_the_loop(monkeypatch):
    """The pre-existing guard, pinned so the restructure cannot quietly drop it."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)

    async def _tick():
        ticks.append(time.monotonic())
        raise RuntimeError("tick blew up")

    monkeypatch.setattr(engine, "_tick", _tick)
    await engine.start()
    try:
        await _until(lambda: len(ticks) >= 3)
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_stop_ends_the_loop_rather_than_restarting_it(monkeypatch):
    """The control: supervision must not resurrect a deliberate shutdown."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    await engine.start()
    try:
        await _until(lambda: len(ticks) >= 1)
        await engine.stop()

        assert engine._task is None
        settled = len(ticks)
        await asyncio.sleep(0.05)
        assert len(ticks) == settled
        assert engine._tick_loop_restarts == 0
    finally:
        # stop() is idempotent, so this only matters when an assertion above never reached it:
        # a live loop outliving its test runs during every later one.
        await engine.stop()


@pytest.mark.asyncio
async def test_a_scheduler_that_is_stopping_is_not_ready():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    engine = SchedulerEngine(svc=_make_svc())
    # stop() has begun and the tick loop is still winding down, so its task is still set.
    engine._task = asyncio.current_task()
    engine._started_at = time.time() - 10
    engine._last_tick_completed_at = time.time()
    engine._stopping = True
    report = engine.health()
    assert report["status"] == "stopping"
    assert report["ready"] is False


@pytest.mark.asyncio
async def test_a_scheduler_that_has_stopped_advancing_reports_stalled(monkeypatch):
    """The failure the store probe cannot see: nothing fires, every read still answers."""
    from lionagi.studio.scheduler import engine as engine_mod

    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    try:
        await engine.start()
        await _until(lambda: engine._last_tick_completed_at is not None)
        assert engine.health()["status"] == "healthy"

        # Age the last completed tick past the threshold. Nothing else changes: the
        # task is alive, the process is up, and a store probe would still answer.
        engine._last_tick_completed_at = time.time() - (engine_mod._TICK_STALL_AFTER + 1)
        report = engine.health()
        assert report["status"] == "stalled"
        assert report["ready"] is False
        assert report["seconds_since_last_tick"] > engine_mod._TICK_STALL_AFTER
        assert "no scheduler tick has completed" in report["detail"]
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_tick_that_raises_does_not_count_as_advancing(monkeypatch):
    """A tick that raised fired nothing, so stamping it would report false progress."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)

    async def _raising_tick():
        ticks.append(time.monotonic())
        raise RuntimeError("tick blew up")

    monkeypatch.setattr(engine, "_tick", _raising_tick)
    try:
        await engine.start()
        await _until(lambda: len(ticks) >= 3)
        # The loop survives a raising tick by design, so liveness cannot be inferred
        # from the loop being alive -- only from a tick having completed.
        assert engine._tick_loop_restarts == 0
        assert engine._last_tick_completed_at is None
        report = engine.health()
        assert report["status"] == "starting"
        # Asserted rather than left out: this test reaches the state where every
        # tick raises, which is precisely where `ready` is the field that can be
        # wrong. Checking only `status` here lets a scheduler that never advances
        # keep reporting ready, and the two neighbouring tests both assert it.
        # Inside the startup window ready is still True -- the deadline below is
        # what ends that.
        assert report["ready"] is True
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_scheduler_that_never_completes_a_tick_stops_being_ready(monkeypatch):
    """The failure the stall branch cannot see, because it has no tick to measure from.

    Every tick raising from process start leaves `_last_tick_completed_at` None
    forever, so the engine sits in `starting` for the life of the process. Without
    a deadline on that state it reports ready the whole time, and readiness ANDs
    that answer into the daemon's.
    """
    from lionagi.studio.scheduler import engine as engine_mod

    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)

    async def _raising_tick():
        ticks.append(time.monotonic())
        raise RuntimeError("tick blew up")

    monkeypatch.setattr(engine, "_tick", _raising_tick)
    try:
        await engine.start()
        await _until(lambda: len(ticks) >= 3)
        assert engine._last_tick_completed_at is None

        # Age the start past the window. Nothing else changes: the loop is alive,
        # the process is up, and no tick has ever completed -- which is the point.
        engine._started_at = time.time() - (engine_mod._TICK_STALL_AFTER + 1)
        report = engine.health()
        assert report["ready"] is False
        assert report["status"] == "starting"
        assert report["last_tick_completed_at"] is None
        assert "no tick has completed" in report["detail"]
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_tick_with_every_schedule_write_failing_does_not_stamp_progress(monkeypatch):
    """A pass whose schedule updates all fail must not count as scheduler progress."""
    from lionagi.studio.scheduler import engine as engine_mod

    now = time.time()
    svc = _make_svc()
    svc.list_schedules = AsyncMock(
        return_value=[
            _minimal_schedule(id="interval-1", trigger_type="interval", next_fire_at=None),
            _minimal_schedule(id="interval-2", trigger_type="interval", next_fire_at=None),
        ]
    )
    svc.update_schedule = AsyncMock(side_effect=RuntimeError("store write failed"))
    engine = engine_mod.SchedulerEngine(svc=svc)
    engine._last_reaper_run = now
    engine._last_checkpoint_run = now
    engine._started_at = now - engine_mod._TICK_STALL_AFTER - 1
    engine._task = asyncio.current_task()
    engine._deliver_due_dispatches = AsyncMock()
    engine._maybe_start_prune = MagicMock()
    engine._maybe_start_worker_pass = MagicMock()
    engine._compute_next_fire = MagicMock(return_value=now + 60)
    engine._effective_timezone_fields = MagicMock(return_value={})
    engine._run_startup_recovery = AsyncMock()

    async def stop_after_tick() -> None:
        assert engine.health()["ready"] is False
        engine._stopping = True

    monkeypatch.setattr(engine, "_sleep_between_ticks", stop_after_tick)

    await engine._tick_loop()

    assert svc.update_schedule.await_count == 2
    assert engine._last_tick_completed_at is None


@pytest.mark.asyncio
async def test_a_stopped_scheduler_is_not_reported_ready(monkeypatch):
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)

    await engine.start()
    await _until(lambda: engine._last_tick_completed_at is not None)
    await engine.stop()

    report = engine.health()
    assert report["status"] == "stopped"
    assert report["ready"] is False


@pytest.mark.asyncio
async def test_a_github_pass_past_the_threshold_makes_the_scheduler_unready(monkeypatch):
    from lionagi.studio.scheduler import engine as engine_mod

    clock = [1000.0]

    class FakeClock:
        @staticmethod
        def time() -> float:
            return clock[0]

    monkeypatch.setattr(engine_mod, "time", FakeClock())
    engine = engine_mod.SchedulerEngine(svc=_make_svc())
    engine._task = asyncio.current_task()
    engine._started_at = clock[0] - 10
    engine._last_tick_completed_at = clock[0]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stalled_pass(schedule: dict, now: float) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(engine, "_tick_github", stalled_pass)
    engine._start_github_tick(
        _minimal_schedule(id="github-1", trigger_type="github_poll"), clock[0]
    )
    task = engine._github_tick_tasks["github-1"]

    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        clock[0] += engine_mod._TICK_STALL_AFTER + 1
        engine._last_tick_completed_at = clock[0]

        report = engine.health()

        assert report["ready"] is False
        assert "github" in report["detail"].lower()
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("hang", ["poll", "store", "cursor"])
async def test_a_github_pass_hung_in_its_poll_or_a_store_write_reads_stalled(monkeypatch, hang):
    from lionagi.studio.scheduler import engine as engine_mod
    from lionagi.studio.scheduler.github import GithubPollResult
    from tests.studio.test_scheduler_github_fire_per_event import _item

    clock = [1000.0]

    class FakeClock:
        @staticmethod
        def time() -> float:
            return clock[0]

        def __getattr__(self, name):
            return getattr(time, name)

    monkeypatch.setattr(engine_mod, "time", FakeClock())
    engine = engine_mod.SchedulerEngine(svc=_make_svc())
    engine._task = asyncio.current_task()
    engine._started_at = clock[0] - 10
    engine._last_tick_completed_at = clock[0]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_here() -> None:
        entered.set()
        await release.wait()

    async def update_schedule(schedule_id, **fields):
        # The store write that clears the event's refusal streak once the action returns.
        if hang == "store" and "predispatch_refusal_count" in fields:
            await wait_here()
        # The cursor write that ends the pass.
        if hang == "cursor" and "guard_cursor_forward" in fields:
            await wait_here()

    engine._svc.update_schedule.side_effect = update_schedule

    async def poll(*args, **kwargs):
        if hang == "poll":
            await wait_here()
        return GithubPollResult(items=[_item(1, "2026-01-01T00:00:00Z")], scan_complete=True)

    async def fire(schedule, run_id, *, global_slot_claim, **kwargs):
        global_slot_claim.release()
        return True

    monkeypatch.setattr("lionagi.studio.scheduler.github.github_poll", poll)
    monkeypatch.setattr(engine, "_fire", fire)
    schedule = _minimal_schedule(
        id="github-1",
        trigger_type="github_poll",
        github_repo="acme/widgets",
        last_fired_at=0,
        predispatch_refusal_count=1,
    )
    engine._start_github_tick(schedule, clock[0])
    task = engine._github_tick_tasks["github-1"]

    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        clock[0] += engine_mod._TICK_STALL_AFTER + 1
        engine._last_tick_completed_at = clock[0]

        report = engine.health()

        assert report["ready"] is False
        stalled_for = engine_mod._TICK_STALL_AFTER + 1
        assert f"has run for {stalled_for:.0f}s outside its actions" in report["detail"]
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hang",
    [
        "invocation",
        "occurrence",
        "launch",
        "dispatched",
        "dispatched_in_its_own_task",
        "process",
        "terminal",
    ],
)
async def test_a_github_pass_is_timed_through_its_fire_except_while_the_process_runs(
    monkeypatch, tmp_path, hang
):
    import lionagi.studio.config as config
    from lionagi.studio.scheduler import engine as engine_mod
    from lionagi.studio.scheduler import subprocess as sched_subprocess
    from lionagi.studio.scheduler.github import GithubPollResult
    from tests.studio.test_scheduler_github_fire_per_event import _item

    clock = [1000.0]

    class FakeClock:
        @staticmethod
        def time() -> float:
            return clock[0]

        def __getattr__(self, name):
            return getattr(time, name)

    monkeypatch.setattr(engine_mod, "time", FakeClock())
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 10)
    engine = engine_mod.SchedulerEngine(svc=_make_svc())
    engine._task = asyncio.current_task()
    engine._started_at = clock[0] - 10
    engine._last_tick_completed_at = clock[0]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_here(*args, **kwargs):
        entered.set()
        await release.wait()

    if hang == "invocation":
        engine._svc.create_invocation.side_effect = wait_here
    elif hang == "occurrence":
        engine._svc.create_schedule_run_and_advance.side_effect = wait_here
    elif hang in ("dispatched", "dispatched_in_its_own_task"):
        engine._svc.update_schedule_run.side_effect = wait_here

    async def update_status(entity_type, entity_id, **fields):
        # The guarded write of the run's outcome once its process has exited.
        if hang == "terminal" and fields.get("expected_statuses") == {"running"}:
            await wait_here()
        return True

    engine._svc.update_status.side_effect = update_status

    async def spawn_and_wait(argv, inv_id, *, on_launched=None, **kwargs):
        # The process starts and runs for as long as it takes; the pass waits on it.
        if hang == "launch":
            await wait_here()
        if hang == "dispatched_in_its_own_task":
            # On Python 3.10 and 3.11, asyncio.wait_for runs the coroutine it awaits in a task of
            # its own, and spawn_and_wait awaits on_launched through it.
            await asyncio.ensure_future(on_launched())
        else:
            await on_launched()
        if hang == "process":
            await wait_here()
        return 0, ""

    async def poll(*args, **kwargs):
        return GithubPollResult(items=[_item(1, "2026-01-01T00:00:00Z")], scan_complete=True)

    monkeypatch.setattr(sched_subprocess, "resolve_li_executable", lambda: (["/usr/bin/li"], None))
    monkeypatch.setattr(sched_subprocess, "spawn_and_wait", spawn_and_wait)
    monkeypatch.setattr("lionagi.studio.scheduler.github.github_poll", poll)
    schedule = _minimal_schedule(
        id="github-1",
        trigger_type="github_poll",
        github_repo="acme/widgets",
        last_fired_at=0,
        action_cwd=str(tmp_path),
    )
    engine._start_github_tick(schedule, clock[0])
    task = engine._github_tick_tasks["github-1"]

    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        clock[0] += engine_mod._TICK_STALL_AFTER + 1
        engine._last_tick_completed_at = clock[0]

        report = engine.health()

        if hang in ("launch", "process"):
            assert report["ready"] is True
            assert report["status"] == "healthy"
        else:
            assert report["ready"] is False
            stalled_for = engine_mod._TICK_STALL_AFTER + 1
            assert f"has run for {stalled_for:.0f}s outside its actions" in report["detail"]
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=1)

    assert engine._github_tick_started_at == {}
    assert engine._global_inflight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_for_runs_in_its_own_task", [False, True])
async def test_a_github_pass_is_timed_through_the_dispatched_stamp_of_the_real_launch(
    monkeypatch, tmp_path, wait_for_runs_in_its_own_task
):
    import lionagi.studio.config as config
    from lionagi.studio.scheduler import engine as engine_mod
    from lionagi.studio.scheduler import subprocess as sched_subprocess
    from lionagi.studio.scheduler.github import GithubPollResult
    from tests.studio.test_scheduler_github_fire_per_event import _item

    class Pipe:
        async def read(self, _n):
            return b""

    class Process:
        pid = None
        returncode = None
        stdout = Pipe()
        stderr = Pipe()

        async def wait(self):
            self.returncode = 0
            return 0

    class AsyncioInLaunch:
        """The asyncio name inside the launch module: no process is created, and wait_for runs
        the coroutine it awaits in a task of its own when asked, as on Python 3.10 and 3.11."""

        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def create_subprocess_exec(self, *args, **kwargs):
            return Process()

        async def wait_for(self, fut, timeout=None):
            if not wait_for_runs_in_its_own_task or timeout is None:
                return await asyncio.wait_for(fut, timeout)
            task = asyncio.ensure_future(fut)
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if task in done:
                return task.result()
            task.cancel()
            raise asyncio.TimeoutError()

    clock = [1000.0]

    class FakeClock:
        @staticmethod
        def time() -> float:
            return clock[0]

        def __getattr__(self, name):
            return getattr(time, name)

    monkeypatch.setattr(engine_mod, "time", FakeClock())
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 10)
    monkeypatch.setattr(sched_subprocess, "asyncio", AsyncioInLaunch())
    monkeypatch.setattr(sched_subprocess, "observe_process_group_leader", lambda *a, **k: None)
    monkeypatch.setattr(sched_subprocess, "resolve_li_executable", lambda: (["/usr/bin/li"], None))

    async def poll(*args, **kwargs):
        return GithubPollResult(items=[_item(1, "2026-01-01T00:00:00Z")], scan_complete=True)

    monkeypatch.setattr("lionagi.studio.scheduler.github.github_poll", poll)
    engine = engine_mod.SchedulerEngine(svc=_make_svc())
    engine._task = asyncio.current_task()
    engine._started_at = clock[0] - 10
    engine._last_tick_completed_at = clock[0]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def dispatched_stamp_hangs(*args, **kwargs):
        entered.set()
        await release.wait()

    async def update_status(*args, **kwargs):
        return True

    engine._svc.update_schedule_run.side_effect = dispatched_stamp_hangs
    engine._svc.update_status.side_effect = update_status
    schedule = _minimal_schedule(
        id="github-1",
        trigger_type="github_poll",
        github_repo="acme/widgets",
        last_fired_at=0,
        action_cwd=str(tmp_path),
    )
    engine._start_github_tick(schedule, clock[0])
    task = engine._github_tick_tasks["github-1"]

    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        clock[0] += engine_mod._TICK_STALL_AFTER + 1
        engine._last_tick_completed_at = clock[0]

        report = engine.health()

        assert report["ready"] is False
        stalled_for = engine_mod._TICK_STALL_AFTER + 1
        assert f"has run for {stalled_for:.0f}s outside its actions" in report["detail"]
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=2)

    assert engine._github_tick_started_at == {}
    assert engine._global_inflight == 0


@pytest.mark.asyncio
async def test_an_earlier_pass_ending_leaves_the_newer_pass_clock_alone():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    engine = SchedulerEngine(svc=_make_svc())
    earlier = asyncio.create_task(asyncio.sleep(0))
    await earlier
    newer = asyncio.create_task(asyncio.sleep(3600))
    try:
        engine._github_tick_tasks["github-1"] = newer
        engine._github_tick_started_at["github-1"] = 1234.0

        engine._discard_github_tick("github-1", earlier)

        assert engine._github_tick_started_at["github-1"] == 1234.0
        assert engine._github_tick_tasks["github-1"] is newer
    finally:
        newer.cancel()
        await asyncio.gather(newer, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_restart_does_not_inherit_the_previous_runs_completed_tick(monkeypatch):
    """A second run in one process must not answer with the first run's evidence.

    Everything else here measures a single run. `start()` can be called again
    after `stop()` in the same process, and the fields health() reads to describe
    the run outlive the run unless something clears them. That turns the check
    inside out: a loop that has never completed a tick reports itself as
    advancing, on the strength of a tick that a previous loop completed.

    The first run really does advance rather than having its timestamp set by
    hand, because a hand-set value would prove only that the reset line runs, not
    that the engine produces the state the reset exists to clear.
    """
    from lionagi.studio.scheduler import engine as engine_mod

    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)

    await engine.start()
    try:
        await _until(lambda: engine._last_tick_completed_at is not None)
    finally:
        await engine.stop()
    assert engine._last_tick_completed_at is not None, (
        "the first run has to advance, or there is no inherited value to be wrong about"
    )

    async def _raising_tick():
        ticks.append(time.monotonic())
        raise RuntimeError("tick blew up")

    monkeypatch.setattr(engine, "_tick", _raising_tick)
    before = len(ticks)
    await engine.start()
    try:
        await _until(lambda: len(ticks) >= before + 3)
        assert engine._last_tick_completed_at is None, (
            "the new run is reporting the previous run's tick as its own"
        )

        # Same ageing the neighbouring tests use: past the startup window, a run
        # that has never completed a tick is not ready. Before the fix this read
        # ready, because the inherited timestamp was recent.
        engine._started_at = time.time() - (engine_mod._TICK_STALL_AFTER + 1)
        report = engine.health()
        assert report["last_tick_completed_at"] is None
        assert report["ready"] is False
        assert report["status"] == "starting"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_restart_does_not_inherit_the_previous_runs_loop_failure(monkeypatch):
    """The mirror case, and the one that reports a pair that cannot both be true.

    `_tick_loop_restarts` was already cleared on start and `_last_tick_loop_failure`
    was not, so a restarted engine reported zero restarts beside the failure that
    caused one. Either number alone looks plausible; only together are they
    visibly impossible, which is why this asserts both in one read.
    """
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    calls: list = []

    async def _short_lived_loop():
        calls.append(time.monotonic())
        if len(calls) <= 2:
            raise RuntimeError("loop blew up")
        await asyncio.sleep(3600)

    monkeypatch.setattr(engine, "_tick_loop", _short_lived_loop)
    await engine.start()
    try:
        await _until(lambda: len(calls) >= 3)
        assert engine._last_tick_loop_failure is not None
        assert engine._tick_loop_restarts >= 2
    finally:
        await engine.stop()

    async def _healthy_loop():
        await asyncio.sleep(3600)

    monkeypatch.setattr(engine, "_tick_loop", _healthy_loop)
    await engine.start()
    try:
        report = engine.health()
        assert engine._last_tick_loop_failure is None, (
            "the new run is reporting the previous run's loop failure"
        )
        assert engine._tick_loop_restarts == 0
        assert report["last_tick_loop_failure"] is None
        assert report["tick_loop_restarts"] == 0
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_restarting_tick_loop_does_not_refresh_the_startup_deadline(monkeypatch):
    """A deadline a restart can reset is not a deadline.

    The loop is supervised, so a scheduler that cannot tick dies and respawns. If
    respawning re-opened the startup window, that scheduler would hold itself
    ready forever by failing repeatedly -- the guard would be defeated by exactly
    the condition it is meant to catch.
    """
    from lionagi.studio.scheduler import engine as engine_mod

    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    attempts: list = []

    async def _raising_tick():
        ticks.append(time.monotonic())
        raise RuntimeError("tick blew up")

    async def _recovery_that_fails_once():
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise RuntimeError("startup recovery blew up")

    # A raising _tick keeps the loop alive and stamps nothing; a raising startup
    # recovery is what actually kills it. Both, so the loop really restarts and
    # still never completes a tick.
    monkeypatch.setattr(engine, "_tick", _raising_tick)
    monkeypatch.setattr(engine, "_run_startup_recovery", _recovery_that_fails_once)
    try:
        await engine.start()
        started_at = engine._started_at
        assert started_at is not None

        await _until(lambda: engine._tick_loop_restarts >= 1)
        await _until(lambda: len(ticks) >= 1)
        assert engine._last_tick_completed_at is None
        assert engine._started_at == started_at, "a respawned tick loop reopened the startup window"

        engine._started_at = time.time() - (engine_mod._TICK_STALL_AFTER + 1)
        assert engine.health()["ready"] is False
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_scheduler_that_was_never_started_does_not_block_readiness(monkeypatch):
    """A host that runs no scheduler is not failing to schedule."""
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    report = engine.health()
    assert report["status"] == "offline"
    assert report["ready"] is True
    assert report["last_tick_completed_at"] is None
    assert report["seconds_since_last_tick"] is None


@pytest.mark.asyncio
async def test_a_replacement_tick_loop_actually_advances_again(monkeypatch):
    """Restart acceptance is behavioral: a NEW loop must advance, not merely exist.

    The restart tests above replace `_tick_loop` wholesale, so they prove a
    replacement was spawned and never that it does the job. This drives a real
    loop through a real death -- startup recovery raising -- and then asserts
    advancement resumed, read off the same field readiness reports.
    """
    ticks: list = []
    engine = await _supervised_engine(monkeypatch, ticks)
    attempts: list = []

    async def _recovery_that_fails_once():
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise RuntimeError("startup recovery blew up")

    monkeypatch.setattr(engine, "_run_startup_recovery", _recovery_that_fails_once)
    await engine.start()
    try:
        await _until(lambda: engine._tick_loop_restarts >= 1)
        assert engine._last_tick_loop_failure is not None
        _, reason = engine._last_tick_loop_failure
        assert "RuntimeError" in reason, reason

        # The point of the whole exercise: the replacement is advancing.
        await _until(lambda: engine._last_tick_completed_at is not None)
        advanced = len(ticks)
        await _until(lambda: len(ticks) > advanced)
        assert engine.health()["status"] == "healthy"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_a_scheduled_fire_of_a_stored_dash_play_prompt_fails_its_run_and_spawns_nothing(
    monkeypatch,
):
    """A row stored before the dash-prompt check is refused when it fires on its schedule: the
    run is recorded, as every scheduled fire's is, and fails naming the refusal, and no process
    starts. Only a manual trigger refuses it before a run is recorded."""
    from lionagi.studio.scheduler import engine as engine_mod
    from lionagi.studio.scheduler import subprocess as sched_subprocess
    from lionagi.studio.scheduler.signals import SchedulerSignalBus, ScheduleRunFailed

    monkeypatch.setattr(sched_subprocess, "resolve_li_executable", lambda: (["li"], None))
    spawn = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(sched_subprocess, "spawn_and_wait", spawn)
    svc = _make_svc()
    bus = SchedulerSignalBus()
    failed: list = []
    bus.observe(ScheduleRunFailed, handler=failed.append)
    engine = engine_mod.SchedulerEngine(svc=svc, signal_bus=bus)
    schedule = _minimal_schedule(
        action_kind="play", action_playbook="triage", action_prompt="--bypass"
    )

    await fire_with_claim(engine, schedule, "run-dash", trigger_context={"scheduled": True})

    assert spawn.await_count == 0
    assert svc.create_invocation.await_count == 1
    assert [(s.reason_code, s.error_detail) for s in failed] == [
        (
            "run.failed.exception",
            "ValueError: action_prompt for a play launch must not start with '-'",
        )
    ]
    # The run row the operator reads carries the refusal too, not only the signal.
    stored = [
        args[0].get("error_detail")
        for args, _ in svc.create_schedule_run_and_advance.await_args_list
        if args and isinstance(args[0], dict)
    ]
    assert stored == ["action_prompt for a play launch must not start with '-'"]


@pytest.mark.asyncio
async def test_a_scheduled_fire_of_a_play_playbook_named_for_a_subcommand_spawns_nothing(
    monkeypatch,
):
    from lionagi.studio.scheduler import engine as engine_mod
    from lionagi.studio.scheduler import subprocess as sched_subprocess
    from lionagi.studio.scheduler.signals import SchedulerSignalBus, ScheduleRunFailed

    monkeypatch.setattr(sched_subprocess, "resolve_li_executable", lambda: (["li"], None))
    spawn = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(sched_subprocess, "spawn_and_wait", spawn)
    svc = _make_svc()
    bus = SchedulerSignalBus()
    failed: list = []
    bus.observe(ScheduleRunFailed, handler=failed.append)
    engine = engine_mod.SchedulerEngine(svc=svc, signal_bus=bus)
    schedule = _minimal_schedule(action_kind="play", action_playbook="list", action_prompt="")

    await fire_with_claim(engine, schedule, "run-list", trigger_context={"scheduled": True})

    assert spawn.await_count == 0
    assert [s.error_detail for s in failed] == [
        "ValueError: action_playbook 'list' names a `li play` subcommand, not a playbook"
    ]


def test_merge_chain_action_overlays_the_named_action_fields():
    from lionagi.studio.scheduler.engine import _merge_chain_action

    shared = {"k": "v"}
    schedule = {
        "id": "s1",
        "action_kind": "agent",
        "action_model": "base-model",
        "action_prompt": "base prompt",
        "action_agent": "base-agent",
        "action_playbook": "base-playbook",
        "extra": shared,
    }

    merged = _merge_chain_action(schedule, {"kind": "play", "prompt": None, "agent": "next"})
    assert merged["action_kind"] == "play"
    assert merged["action_prompt"] is None, "an explicit None replaces the field"
    assert merged["action_agent"] == "next"
    assert merged["action_model"] == "base-model"
    assert merged["action_playbook"] == "base-playbook"
    assert merged["extra"] is shared, "the overlay is shallow"
    assert schedule["action_kind"] == "agent", "the parent schedule is not modified"

    assert _merge_chain_action(schedule, {"action_kind": "flow"})["action_kind"] == "flow"
    both = _merge_chain_action(schedule, {"kind": "play", "action_kind": "flow"})
    assert both["action_kind"] == "play"
    assert _merge_chain_action(schedule, {"model": "m2"})["action_kind"] == "agent"
