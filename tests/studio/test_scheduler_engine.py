# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for SchedulerEngine._fire() and helpers via a mocked service."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    svc.update_schedule_run = AsyncMock()
    svc.create_invocation = AsyncMock()
    svc.update_invocation = AsyncMock()
    svc.update_status = AsyncMock()
    svc.list_sessions_for_invocation = AsyncMock(return_value=[])
    return svc


# ---------------------------------------------------------------------------
# resolve_invocation_terminal tests (pure-logic, no DB)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# SchedulerEngine._fire() — happy path (exit_code=0)
# ---------------------------------------------------------------------------


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
        await engine._fire(schedule, "run-001", trigger_context={"scheduled": True})

    svc.create_invocation.assert_awaited_once()
    svc.create_schedule_run.assert_awaited_once()
    svc.update_schedule_run.assert_awaited_once()
    # update_status called for schedule_run AND invocation
    assert svc.update_status.await_count == 3  # running + completed + invocation
    svc.update_invocation.assert_awaited_once()
    svc.update_schedule.assert_awaited()


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
        await engine._fire(schedule, "run-002", trigger_context={"scheduled": True})

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
        await engine._fire(schedule, "run-003", trigger_context={"scheduled": True})

    mock_spawn.assert_not_awaited()
    svc.create_schedule_run.assert_awaited_once()
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
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await engine._fire(schedule, "run-004", trigger_context={"scheduled": True})

    svc.update_schedule_run.assert_awaited()
    cancelled_calls = [
        c for c in svc.update_schedule_run.await_args_list if c.kwargs.get("status") == "cancelled"
    ]
    assert cancelled_calls


@pytest.mark.asyncio
async def test_fire_inner_exception_records_failed_and_does_not_reraise():
    """Unexpected exception inside the main try block is caught, recorded, and swallowed."""
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
            new=AsyncMock(side_effect=RuntimeError("unexpected")),
        ),
    ):
        # Should not raise
        await engine._fire(schedule, "run-005", trigger_context={"scheduled": True})

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
        await engine._fire(schedule, "run-006", trigger_context={}, chain_depth=0)

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
        await engine._fire(
            schedule, "run-007", trigger_context={}, chain_depth=1, chain_parent_id="run-006"
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

    async def _patched_fire(sched, run_id, *, trigger_context, chain_parent_id=None, chain_depth=0):
        fire_calls.append((sched["id"], chain_depth))
        if chain_depth > 0:
            return
        return await original_fire(
            sched,
            run_id,
            trigger_context=trigger_context,
            chain_parent_id=chain_parent_id,
            chain_depth=chain_depth,
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
        await original_fire(schedule, "run-chain", trigger_context={}, chain_depth=0)

    # The chain should have been triggered
    chained = [c for c in fire_calls if c[1] == 1]
    assert chained, "Expected a chained _fire() call at depth=1"


# ---------------------------------------------------------------------------
# SchedulerEngine.fire_now() — delegates through service
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _maybe_fire() tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# create_skipped_run helper
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# SchedulerEngine construction — default vs injected service
# ---------------------------------------------------------------------------


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
