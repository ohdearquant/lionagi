# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for SchedulerEngine._fire() and helpers via a mocked service."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

NY = ZoneInfo("America/New_York")

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
    svc.count_schedule_runs = AsyncMock(return_value=0)
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
        await engine._fire(schedule, "run-002", trigger_context={"pr_number": "42"})

    svc.create_invocation.assert_awaited_once()
    (invocation_payload,), _kwargs = svc.create_invocation.await_args
    assert invocation_payload["prompt"] == "review PR 42"


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
        await engine._fire(schedule, "run-002b", trigger_context={"payload": ""})

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
        await engine._fire(schedule, "run-003", trigger_context={"scheduled": True})

    spawn_mock.assert_not_awaited()
    svc.create_schedule_run.assert_awaited_once()
    (run_payload,), _kwargs = svc.create_schedule_run.await_args
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
        await engine._fire(schedule, "run-cas", trigger_context={"scheduled": True})

    svc.count_schedule_runs.assert_awaited()


@pytest.mark.asyncio
async def test_fire_exception_after_terminal_schedule_run_does_not_rewrite_failed():
    """A late exception after the schedule_run terminal write already
    succeeded (e.g. resolve_invocation_terminal blowing up) must not attempt
    an unguarded terminal rewrite from the broad-except handler."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    schedule_run_terminal_calls: list[dict] = []

    async def _update_status(entity_type, entity_id, *, new_status, **kwargs):
        if entity_type == "schedule_run" and new_status in ("completed", "failed"):
            schedule_run_terminal_calls.append(kwargs)
            if len(schedule_run_terminal_calls) > 1:
                assert "expected_statuses" in kwargs, (
                    "a second schedule_run terminal write from the broad-except "
                    "handler must be guarded, not an unconditional overwrite"
                )
                return False
        return True

    svc.update_status = AsyncMock(side_effect=_update_status)
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()

    # resolve_invocation_terminal() raises on its first call (right after the
    # schedule_run terminal write in the normal path), but the broad-except
    # handler's own call to it must still succeed so the test can observe the
    # handler's schedule_run rewrite attempt in isolation.
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
        await engine._fire(schedule, "run-late-exc", trigger_context={"scheduled": True})

    assert len(schedule_run_terminal_calls) == 2


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
        await original_fire(schedule, "run-chain-cas", trigger_context={}, chain_depth=0)

    chained = [c for c in fire_calls if c[1] == 1]
    assert chained, "on_success chain must still fire when the invocation write lost its CAS"


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


# ---------------------------------------------------------------------------
# Cron timezone resolution — the P1 fix: cron_expr is resolved in the
# configured timezone (default: system local), not UTC. next_fire_at is
# still stored as a UTC epoch.
# ---------------------------------------------------------------------------


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
    assert any("Invalid scheduler timezone" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# recompute_next_fire — shared recompute+log path for daemon start, PATCH,
# and disable->enable (services/schedules.py hooks it too; see
# tests/studio/test_schedule_tz_recompute.py for those integration paths).
# ---------------------------------------------------------------------------


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
    svc.update_schedule.assert_awaited_once_with(schedule["id"], next_fire_at=new)
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

    async def _persist_update_schedule(sid, **fields):
        # Mutate the same dict list_schedules() keeps returning, mirroring
        # a real DB: a persisted update must be visible to the next fetch.
        if sid == schedule["id"]:
            schedule.update(fields)

    svc.update_schedule = AsyncMock(side_effect=_persist_update_schedule)
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

    update_calls = [
        c for c in svc.update_schedule.await_args_list if c.args and c.args[0] == "sched-run-once"
    ]
    assert update_calls, "Expected the recovery fire to persist a new next_fire_at"
    final_next_fire_at = update_calls[-1].kwargs.get("next_fire_at")
    assert final_next_fire_at is not None
    assert final_next_fire_at > fixed_now


@pytest.mark.asyncio
async def test_startup_missed_fire_run_once_not_double_fired_by_immediate_tick(monkeypatch):
    """Reproduces the exact _tick_loop() startup ordering: _check_missed_fires()
    runs, then _tick() runs immediately after with no sleep in between (the
    tick loop only sleeps *between* iterations of the while-loop, not before
    its first one). A past-due run_once schedule must be fired exactly once
    total: the missed-fire recovery path must reserve/advance next_fire_at
    synchronously before _check_missed_fires() returns, so the immediately
    following _tick() does not see the same stale past-due next_fire_at and
    queue a second, duplicate fire for it."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

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

    async def _persist_update_schedule(sid, **fields):
        # Mutate the same dict list_schedules() keeps returning, mirroring
        # a real DB: a persisted update must be visible to the next fetch.
        if sid == schedule["id"]:
            schedule.update(fields)

    svc.update_schedule = AsyncMock(side_effect=_persist_update_schedule)
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
        await engine._tick()
        if engine._fire_tasks:
            await asyncio.gather(*engine._fire_tasks)

    assert len(tracked_calls) == 1, (
        "Expected exactly one fire total (missed-fire recovery only); the "
        "immediately-following _tick() must not queue a second, duplicate "
        f"fire for the same past-due schedule. Got {len(tracked_calls)} "
        f"fires: {[c[1].get('trigger_context') for c in tracked_calls]}"
    )
    assert tracked_calls[0][1]["trigger_context"]["missed_recovery"] is True


@pytest.mark.asyncio
async def test_startup_missed_fire_run_once_reserve_failure_skips_recovery(monkeypatch):
    """Failure path of the synchronous reserve: if update_schedule raises
    while reserving next_fire_at, storage still holds the past-due value
    and the immediately-following _tick() will fire the schedule normally.
    The recovery path must therefore NOT queue its own fire on a failed
    reserve — otherwise the external action runs twice in one cycle. Net
    result: exactly one fire total, and it is the normal scheduled one,
    not a missed_recovery fire."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    import lionagi.studio.config as studio_config
    import lionagi.studio.scheduler.engine as engine_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")

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

    async def _first_write_fails(sid, **fields):
        # The reserve (first write) hits a transient storage failure; later
        # writes (the normal fire's own advance) succeed and persist into
        # the same dict list_schedules() keeps returning.
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("storage briefly unavailable")
        if sid == schedule["id"]:
            schedule.update(fields)

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
        await engine._tick()
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

    async def _persist_update_schedule(sid, **fields):
        if sid == schedule["id"]:
            schedule.update(fields)

    svc.update_schedule = AsyncMock(side_effect=_persist_update_schedule)
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


# ---------------------------------------------------------------------------
# max_runs / one-shot semantics
# ---------------------------------------------------------------------------


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
        await engine._fire(schedule, "run-once-1", trigger_context={"scheduled": True})

    svc.count_schedule_runs.assert_awaited_with("sched-001", chain_depth=0)
    disable_calls = [c for c in svc.update_schedule.await_args_list if c.kwargs.get("enabled") == 0]
    assert disable_calls, "Expected update_schedule(..., enabled=0) once max_runs is reached"


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
        await engine._fire(schedule, "run-once-2", trigger_context={"scheduled": True})

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
        await engine._fire(schedule, "run-unlimited", trigger_context={"scheduled": True})

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
        await engine._fire(
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
        await engine._fire(schedule, "run-badargv", trigger_context={"scheduled": True})

    svc.count_schedule_runs.assert_awaited_with("sched-001", chain_depth=0)
    disable_calls = [c for c in svc.update_schedule.await_args_list if c.kwargs.get("enabled") == 0]
    assert disable_calls


# ---------------------------------------------------------------------------
# max_runs enforcement BEFORE firing (pre-flight reservation), not just after
# ---------------------------------------------------------------------------


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
    ):
        self.runs: dict[str, dict] = dict(existing_runs or {})
        self.schedule_updates: list[tuple[str, dict]] = []
        self._fail_create_invocation_times = fail_create_invocation_times
        self.create_invocation_calls = 0

    async def get_schedule(self, schedule_id):
        return None

    async def list_schedules(self, *, enabled=None):
        return []

    async def update_schedule(self, schedule_id, **fields):
        self.schedule_updates.append((schedule_id, fields))

    async def count_schedule_runs(self, schedule_id, *, chain_depth=0):
        return sum(
            1
            for r in self.runs.values()
            if r.get("schedule_id") == schedule_id
            and r.get("chain_depth", 0) == chain_depth
            and r.get("status") in {"completed", "failed", "cancelled"}
        )

    async def create_schedule_run(self, run):
        self.runs[run["id"]] = dict(run)

    async def update_schedule_run(self, run_id, **fields):
        self.runs[run_id].update(fields)

    async def create_invocation(self, invocation):
        self.create_invocation_calls += 1
        if self.create_invocation_calls <= self._fail_create_invocation_times:
            raise RuntimeError("transient invocation insert failure")

    async def update_invocation(self, inv_id, **fields):
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

    svc = _StatefulSvc()
    engine = SchedulerEngine(svc)
    schedule = _minimal_schedule(id="sched-multi", max_runs=2)

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

    Reproduces the round-2 finding: reserve the budget, let create_invocation
    blow up once, confirm the claim is released (not stuck inflight with zero
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
    """Pins the round-3 finding: a concurrent reserve must not overshoot
    max_runs by combining a stale persisted count with an already-released
    in-flight claim.

    Forces the exact interleaving the reviewer's reproducer exploited:
    fire A holds a claim (in-flight, not yet terminal). Reserve B starts its
    admission check and its count_schedule_runs() read is suspended
    mid-flight. While B is suspended, A completes: its terminal run is
    recorded AND its claim is released — both entirely inside B's await
    window. B's count() then resumes and returns the count as it was
    when the read started (stale — before A's write), simulating a real
    DB read that began before the write landed. If _reserve_max_runs_budget
    read `inflight` only after this await (the round-2 shape), it would see
    inflight=0 (already released) + used=0 (stale) and incorrectly admit a
    second top-level fire for max_runs=1. Reading `inflight` before the
    await (the round-3 fix) must still see A's claim and refuse B."""
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

    async def stalling_count(schedule_id, *, chain_depth=0):
        # Read the count as of THIS moment (before A's terminal write
        # lands), but don't return it until told to -- after A has both
        # recorded its terminal run and released its claim.
        snapshot = await real_count(schedule_id, chain_depth=chain_depth)
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
