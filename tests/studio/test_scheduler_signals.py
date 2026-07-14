# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the OBSERVER W2 scheduler signal bus + mint call sites.

Covers:
  * SchedulerSignalBus.observe/unobserve/emit: type matching, predicate
    (reason_code) filtering, sync + async handler dispatch.
  * Failure semantics: emit() raises an ExceptionGroup (never a blanket
    swallow) while still running every matching handler; the engine's mint
    call site catches that group, writes a durable admin_events row, and
    the tick loop keeps going (a broken handler never stops unrelated
    schedules).
  * build_schedule_run_signal(): mint-site-agnostic field derivation from
    entity_id/new_status/reason_code (the same fields any
    _guarded_terminal_status caller already has).
  * SchedulerEngine._fire_inner() actually mints the right signal on each
    terminal path (succeeded, failed-nonzero-exit, failed-exception,
    cancelled).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.state.reasons import RunReasons
from lionagi.studio.scheduler.signals import (
    SchedulerSignalBus,
    ScheduleRunCancelled,
    ScheduleRunFailed,
    ScheduleRunSucceeded,
    build_schedule_run_signal,
    record_handler_failure,
)

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/studio/test_scheduler_engine.py's fixtures)
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


# ---------------------------------------------------------------------------
# SchedulerSignalBus — observe/emit matching + dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bus_dispatches_to_type_matching_handler():
    bus = SchedulerSignalBus()
    seen: list = []
    bus.observe(ScheduleRunSucceeded, handler=seen.append)

    sig = ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    results = await bus.emit(sig)

    assert seen == [sig]
    assert results == [None]  # list.append returns None


@pytest.mark.asyncio
async def test_bus_does_not_dispatch_to_non_matching_type():
    bus = SchedulerSignalBus()
    seen: list = []
    bus.observe(ScheduleRunFailed, handler=seen.append)

    sig = ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    results = await bus.emit(sig)

    assert seen == []
    assert results == []


@pytest.mark.asyncio
async def test_bus_predicate_filters_by_reason_code():
    """Predicate-only filtering (no topic/pattern machinery): a handler
    registered with a type + a reason_code predicate only fires for signals
    matching both."""
    bus = SchedulerSignalBus()
    matched: list = []
    bus.observe(
        ScheduleRunFailed,
        lambda s: s.reason_code == RunReasons.FAILED_MISSING_CWD,
        handler=matched.append,
    )

    missing_cwd_sig = ScheduleRunFailed(
        run_id="r1", schedule_id="s1", reason_code=RunReasons.FAILED_MISSING_CWD
    )
    other_failure_sig = ScheduleRunFailed(
        run_id="r2", schedule_id="s1", reason_code=RunReasons.FAILED_EXIT_NONZERO
    )

    await bus.emit(missing_cwd_sig)
    await bus.emit(other_failure_sig)

    assert matched == [missing_cwd_sig]


@pytest.mark.asyncio
async def test_bus_sync_and_async_handlers_both_run():
    bus = SchedulerSignalBus()
    sync_calls: list = []
    async_calls: list = []

    def _sync_handler(sig):
        sync_calls.append(sig)

    async def _async_handler(sig):
        async_calls.append(sig)

    bus.observe(ScheduleRunSucceeded, handler=_sync_handler)
    bus.observe(ScheduleRunSucceeded, handler=_async_handler)

    sig = ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    await bus.emit(sig)

    assert sync_calls == [sig]
    assert async_calls == [sig]


def test_bus_has_no_topic_or_route_stream_machinery():
    """SchedulerSignalBus is a stripped registry: only observe/unobserve/emit
    -- no Flow/route()/stream() (SessionObserver's DB-persistence-oriented
    surface), and no separate topic/pattern key type beyond isinstance +
    predicate."""
    bus = SchedulerSignalBus()
    assert not hasattr(bus, "route")
    assert not hasattr(bus, "stream")
    assert not hasattr(bus, "flow")


@pytest.mark.asyncio
async def test_unobserve_removes_handler_and_returns_count():
    bus = SchedulerSignalBus()
    seen: list = []

    def handler(sig):
        seen.append(sig)

    bus.observe(ScheduleRunSucceeded, handler=handler)
    bus.observe(ScheduleRunFailed, handler=handler)

    removed = bus.unobserve(handler)
    assert removed == 2

    await bus.emit(ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code="x"))
    assert seen == []


@pytest.mark.asyncio
async def test_emit_with_zero_handlers_is_a_noop():
    bus = SchedulerSignalBus()
    results = await bus.emit(
        ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    )
    assert results == []


# ---------------------------------------------------------------------------
# Per-run coordination counters (emitted/received/acted_on) + pop_run_counters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_counts_emitted_even_with_zero_handlers():
    """Emitted describes what the mint site dispatched, not what got
    delivered -- it counts regardless of whether anyone is listening."""
    bus = SchedulerSignalBus()
    await bus.emit(
        ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    )
    counters = bus.pop_run_counters("r1")
    assert counters == {
        "emitted": {"ScheduleRunSucceeded": 1},
        "received": 0,
        "acted_on": 0,
    }


@pytest.mark.asyncio
async def test_emit_counts_received_for_matching_handler_with_falsy_return():
    """A handler that matches but returns None/falsy (the default, opt-out
    convention) counts as received-only -- never acted_on."""
    bus = SchedulerSignalBus()
    bus.observe(ScheduleRunSucceeded, handler=lambda sig: None)

    await bus.emit(
        ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    )
    counters = bus.pop_run_counters("r1")
    assert counters["received"] == 1
    assert counters["acted_on"] == 0


@pytest.mark.asyncio
async def test_emit_counts_acted_on_when_handler_returns_truthy_marker():
    """The opt-in truthy-return convention: a handler that returns a truthy
    value counts as acted_on, on top of received."""
    bus = SchedulerSignalBus()
    bus.observe(ScheduleRunSucceeded, handler=lambda sig: "acted")

    await bus.emit(
        ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    )
    counters = bus.pop_run_counters("r1")
    assert counters["received"] == 1
    assert counters["acted_on"] == 1


@pytest.mark.asyncio
async def test_emit_async_handler_acted_on_marker_is_awaited_first():
    bus = SchedulerSignalBus()

    async def _handler(sig):
        return True

    bus.observe(ScheduleRunSucceeded, handler=_handler)
    await bus.emit(
        ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    )
    counters = bus.pop_run_counters("r1")
    assert counters["acted_on"] == 1


@pytest.mark.asyncio
async def test_emit_predicate_reject_not_counted_as_received():
    """A candidate whose type matched but whose predicate rejected the
    signal must not count as received (received == "predicate passed")."""
    bus = SchedulerSignalBus()
    bus.observe(
        ScheduleRunFailed,
        lambda s: s.reason_code == RunReasons.FAILED_MISSING_CWD,
        handler=lambda sig: True,
    )

    await bus.emit(
        ScheduleRunFailed(run_id="r1", schedule_id="s1", reason_code=RunReasons.FAILED_EXIT_NONZERO)
    )
    counters = bus.pop_run_counters("r1")
    assert counters["received"] == 0
    assert counters["acted_on"] == 0
    assert counters["emitted"] == {"ScheduleRunFailed": 1}


@pytest.mark.asyncio
async def test_emit_predicate_exception_not_counted_as_received():
    """A predicate that raises never reaches the "passed" branch -- it must
    not be counted as received even though emit() still surfaces the
    failure as an ExceptionGroup."""
    from lionagi.ln.concurrency import ExceptionGroup

    bus = SchedulerSignalBus()
    bus.observe(
        ScheduleRunFailed,
        lambda s: (_ for _ in ()).throw(RuntimeError("predicate bug")),
        handler=lambda sig: True,
    )

    with pytest.raises(ExceptionGroup):
        await bus.emit(
            ScheduleRunFailed(
                run_id="r1", schedule_id="s1", reason_code=RunReasons.FAILED_EXCEPTION
            )
        )
    counters = bus.pop_run_counters("r1")
    assert counters["received"] == 0


@pytest.mark.asyncio
async def test_emit_multiple_handlers_each_contribute_to_received_and_acted_on():
    bus = SchedulerSignalBus()
    bus.observe(ScheduleRunSucceeded, handler=lambda sig: True)
    bus.observe(ScheduleRunSucceeded, handler=lambda sig: False)
    bus.observe(ScheduleRunSucceeded, handler=lambda sig: None)

    await bus.emit(
        ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    )
    counters = bus.pop_run_counters("r1")
    assert counters["received"] == 3
    assert counters["acted_on"] == 1


def test_pop_run_counters_returns_none_for_unknown_run():
    bus = SchedulerSignalBus()
    assert bus.pop_run_counters("never-emitted") is None


@pytest.mark.asyncio
async def test_pop_run_counters_removes_the_entry():
    """Pop, not peek -- the bus is a long-lived per-daemon singleton, so a
    run's counters must not linger past its one terminal flush."""
    bus = SchedulerSignalBus()
    await bus.emit(
        ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    )
    first = bus.pop_run_counters("r1")
    second = bus.pop_run_counters("r1")
    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_counters_are_isolated_per_run_id():
    bus = SchedulerSignalBus()
    bus.observe(ScheduleRunSucceeded, handler=lambda sig: True)

    await bus.emit(
        ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    )
    await bus.emit(
        ScheduleRunSucceeded(run_id="r2", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    )

    counters_r1 = bus.pop_run_counters("r1")
    counters_r2 = bus.pop_run_counters("r2")
    assert counters_r1 == {"emitted": {"ScheduleRunSucceeded": 1}, "received": 1, "acted_on": 1}
    assert counters_r2 == {"emitted": {"ScheduleRunSucceeded": 1}, "received": 1, "acted_on": 1}


# ---------------------------------------------------------------------------
# Failure semantics: ExceptionGroup, never a blanket swallow, siblings still run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_raises_exceptiongroup_when_a_handler_fails():
    from lionagi.ln.concurrency import ExceptionGroup

    bus = SchedulerSignalBus()

    def _boom(sig):
        raise RuntimeError("handler bug")

    bus.observe(ScheduleRunFailed, handler=_boom)

    with pytest.raises(ExceptionGroup) as exc_info:
        await bus.emit(
            ScheduleRunFailed(
                run_id="r1", schedule_id="s1", reason_code=RunReasons.FAILED_EXCEPTION
            )
        )

    assert len(exc_info.value.exceptions) == 1
    assert isinstance(exc_info.value.exceptions[0], RuntimeError)


@pytest.mark.asyncio
async def test_emit_runs_every_handler_even_when_one_raises():
    """return_exceptions=True: a broken handler must not prevent a sibling
    handler for the SAME signal from running."""
    bus = SchedulerSignalBus()
    ran: list = []

    def _boom(sig):
        raise RuntimeError("handler bug")

    def _good(sig):
        ran.append(sig)

    bus.observe(ScheduleRunFailed, handler=_boom)
    bus.observe(ScheduleRunFailed, handler=_good)

    from lionagi.ln.concurrency import ExceptionGroup

    sig = ScheduleRunFailed(run_id="r1", schedule_id="s1", reason_code=RunReasons.FAILED_EXCEPTION)
    with pytest.raises(ExceptionGroup):
        await bus.emit(sig)

    assert ran == [sig]


@pytest.mark.asyncio
async def test_emit_collects_all_failures_into_one_group():
    bus = SchedulerSignalBus()

    def _boom1(sig):
        raise RuntimeError("first")

    def _boom2(sig):
        raise ValueError("second")

    bus.observe(ScheduleRunFailed, handler=_boom1)
    bus.observe(ScheduleRunFailed, handler=_boom2)

    from lionagi.ln.concurrency import ExceptionGroup

    with pytest.raises(ExceptionGroup) as exc_info:
        await bus.emit(
            ScheduleRunFailed(
                run_id="r1", schedule_id="s1", reason_code=RunReasons.FAILED_EXCEPTION
            )
        )

    kinds = {type(e) for e in exc_info.value.exceptions}
    assert kinds == {RuntimeError, ValueError}


@pytest.mark.asyncio
async def test_emit_predicate_exception_does_not_abort_sibling_dispatch():
    """A raising predicate must be evaluated inside the same protected
    region as handler invocation -- it must not abort emit() before a
    sibling, independently-registered subscription ever gets to run."""
    from lionagi.ln.concurrency import ExceptionGroup

    bus = SchedulerSignalBus()
    ran: list = []

    def _boom_predicate(sig):
        raise RuntimeError("predicate bug")

    def _good(sig):
        ran.append(sig)

    bus.observe(ScheduleRunFailed, _boom_predicate, handler=lambda sig: None)
    bus.observe(ScheduleRunFailed, handler=_good)

    sig = ScheduleRunFailed(run_id="r1", schedule_id="s1", reason_code=RunReasons.FAILED_EXCEPTION)
    with pytest.raises(ExceptionGroup) as exc_info:
        await bus.emit(sig)

    assert ran == [sig]
    assert len(exc_info.value.exceptions) == 1
    assert isinstance(exc_info.value.exceptions[0], RuntimeError)


@pytest.mark.asyncio
async def test_emit_reraises_cancellation_without_nesting_baseexception():
    """gather(return_exceptions=True) can return a raw CancelledError.
    Folding that into ExceptionGroup would raise TypeError ("cannot nest
    BaseExceptions") -- cancellation must propagate as itself instead of
    being wrapped or swallowed, and sibling handlers must still run."""
    bus = SchedulerSignalBus()
    ran: list = []

    async def _cancelled(sig):
        raise asyncio.CancelledError()

    def _good(sig):
        ran.append(sig)

    bus.observe(ScheduleRunSucceeded, handler=_cancelled)
    bus.observe(ScheduleRunSucceeded, handler=_good)

    sig = ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    with pytest.raises(asyncio.CancelledError):
        await bus.emit(sig)

    assert ran == [sig]


@pytest.mark.asyncio
async def test_emit_reraises_cancellation_even_with_other_handler_failures():
    """A genuine handler bug alongside a cancellation must not turn into a
    TypeError from ExceptionGroup nesting a BaseException -- cancellation
    still wins and propagates, with the handler failure chained for
    visibility rather than silently dropped."""
    from lionagi.ln.concurrency import ExceptionGroup

    bus = SchedulerSignalBus()

    async def _cancelled(sig):
        raise asyncio.CancelledError()

    def _boom(sig):
        raise RuntimeError("handler bug")

    bus.observe(ScheduleRunSucceeded, handler=_cancelled)
    bus.observe(ScheduleRunSucceeded, handler=_boom)

    sig = ScheduleRunSucceeded(run_id="r1", schedule_id="s1", reason_code=RunReasons.COMPLETED_OK)
    with pytest.raises(asyncio.CancelledError) as exc_info:
        await bus.emit(sig)

    assert isinstance(exc_info.value.__cause__, ExceptionGroup)
    assert isinstance(exc_info.value.__cause__.exceptions[0], RuntimeError)


# ---------------------------------------------------------------------------
# record_handler_failure — durable surfaced record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_handler_failure_writes_admin_event(tmp_path, monkeypatch):
    import lionagi.state.db as state_db_mod
    from lionagi.ln.concurrency import ExceptionGroup
    from lionagi.state.db import StateDB

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    sig = ScheduleRunFailed(
        run_id="run-xyz", schedule_id="sched-xyz", reason_code=RunReasons.FAILED_EXCEPTION
    )
    eg = ExceptionGroup("handlers failed", [RuntimeError("boom")])

    await record_handler_failure(eg, sig)

    async with StateDB(fake_db) as db:
        events = await db.list_admin_events(action="scheduler_signal_handler_failed")

    assert len(events) == 1
    assert events[0]["target_id"] == "run-xyz"
    assert events[0]["actor"] == "scheduler"
    details = events[0]["details"]
    if isinstance(details, str):
        import json

        details = json.loads(details)
    assert "RuntimeError: boom" in details["errors"][0]
    assert details["signal_type"] == "ScheduleRunFailed"


@pytest.mark.asyncio
async def test_record_handler_failure_is_best_effort_and_never_raises(monkeypatch):
    """If StateDB itself is unavailable, record_handler_failure logs and
    swallows -- it must never become a second failure on top of the
    already-surfaced handler exception."""
    import lionagi.studio.scheduler.signals as signals_mod
    from lionagi.ln.concurrency import ExceptionGroup

    class _BoomStateDB:
        def __init__(self, *a, **kw):
            raise RuntimeError("db unavailable")

    monkeypatch.setattr("lionagi.state.db.StateDB", _BoomStateDB)

    sig = ScheduleRunFailed(
        run_id="run-1", schedule_id="s1", reason_code=RunReasons.FAILED_EXCEPTION
    )
    eg = ExceptionGroup("handlers failed", [RuntimeError("boom")])

    # Must not raise.
    await record_handler_failure(eg, sig)


# ---------------------------------------------------------------------------
# build_schedule_run_signal — mint-site-agnostic field derivation
# ---------------------------------------------------------------------------


def test_build_schedule_run_signal_completed_derives_succeeded():
    sig = build_schedule_run_signal(
        entity_id="run-1",
        new_status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        schedule_id="sched-1",
        action_kind="agent",
        chain_depth=0,
        trigger_context={"k": "v"},
    )
    assert isinstance(sig, ScheduleRunSucceeded)
    assert sig.run_id == "run-1"
    assert sig.schedule_id == "sched-1"
    assert sig.reason_code == RunReasons.COMPLETED_OK
    assert sig.action_kind == "agent"
    assert sig.chain_depth == 0
    assert sig.trigger_context == {"k": "v"}


def test_build_schedule_run_signal_failed_derives_failed_with_error_detail():
    sig = build_schedule_run_signal(
        entity_id="run-2",
        new_status="failed",
        reason_code=RunReasons.FAILED_EXIT_NONZERO,
        error_detail="exit 1",
    )
    assert isinstance(sig, ScheduleRunFailed)
    assert sig.error_detail == "exit 1"


def test_build_schedule_run_signal_cancelled_derives_cancelled():
    sig = build_schedule_run_signal(
        entity_id="run-3",
        new_status="cancelled",
        reason_code=RunReasons.CANCELLED_SYSTEM,
    )
    assert isinstance(sig, ScheduleRunCancelled)


def test_build_schedule_run_signal_unknown_status_raises():
    with pytest.raises(ValueError, match="no schedule_run signal registered"):
        build_schedule_run_signal(entity_id="run-4", new_status="running", reason_code="x")


# ---------------------------------------------------------------------------
# SchedulerEngine._fire_inner integration — mint on each terminal path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_happy_path_mints_schedule_run_succeeded():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    bus = SchedulerSignalBus()
    captured: list = []
    bus.observe(ScheduleRunSucceeded, handler=captured.append)
    engine = SchedulerEngine(svc=svc, signal_bus=bus)
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

    assert len(captured) == 1
    sig = captured[0]
    assert sig.run_id == "run-001"
    assert sig.schedule_id == "sched-001"
    assert sig.reason_code == RunReasons.COMPLETED_OK


@pytest.mark.asyncio
async def test_fire_nonzero_exit_mints_schedule_run_failed():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    bus = SchedulerSignalBus()
    captured: list = []
    bus.observe(ScheduleRunFailed, handler=captured.append)
    engine = SchedulerEngine(svc=svc, signal_bus=bus)
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

    assert len(captured) == 1
    assert captured[0].reason_code == RunReasons.FAILED_EXIT_NONZERO
    assert captured[0].error_detail == "error text"


@pytest.mark.asyncio
async def test_fire_inner_exception_mints_schedule_run_failed():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    bus = SchedulerSignalBus()
    captured: list = []
    bus.observe(ScheduleRunFailed, handler=captured.append)
    engine = SchedulerEngine(svc=svc, signal_bus=bus)
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
        await engine._fire(schedule, "run-005", trigger_context={"scheduled": True})

    assert len(captured) == 1
    assert captured[0].reason_code == RunReasons.FAILED_EXCEPTION
    assert "RuntimeError: unexpected" in captured[0].error_detail


@pytest.mark.asyncio
async def test_fire_cancellation_mints_schedule_run_cancelled():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    bus = SchedulerSignalBus()
    captured: list = []
    bus.observe(ScheduleRunCancelled, handler=captured.append)
    engine = SchedulerEngine(svc=svc, signal_bus=bus)
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

    assert len(captured) == 1
    assert captured[0].reason_code == RunReasons.CANCELLED_SYSTEM


@pytest.mark.asyncio
async def test_fire_does_not_mint_when_guarded_write_loses_race():
    """A lost race (a concurrent reaper already finalized the row) must not
    mint a signal describing an outcome this call didn't actually commit."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.update_status = AsyncMock(return_value=False)  # every terminal write "loses"
    bus = SchedulerSignalBus()
    captured: list = []
    bus.observe(ScheduleRunSucceeded, handler=captured.append)
    bus.observe(ScheduleRunFailed, handler=captured.append)
    engine = SchedulerEngine(svc=svc, signal_bus=bus)
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
        await engine._fire(schedule, "run-006", trigger_context={"scheduled": True})

    assert captured == []


@pytest.mark.asyncio
async def test_fire_build_argv_exception_mints_schedule_run_failed():
    """The pre-launch exception path (build_argv() raising, e.g. an invalid
    action_kind or an unresolvable executable) is a fourth schedule_run
    terminal write and must mint the same as the other three terminal
    paths."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    bus = SchedulerSignalBus()
    captured: list = []
    bus.observe(ScheduleRunFailed, handler=captured.append)
    engine = SchedulerEngine(svc=svc, signal_bus=bus)
    schedule = _minimal_schedule()

    with patch(
        "lionagi.studio.scheduler.subprocess.build_argv",
        side_effect=ValueError("bad action_kind"),
    ):
        await engine._fire(schedule, "run-010", trigger_context={"scheduled": True})

    assert len(captured) == 1
    sig = captured[0]
    assert sig.run_id == "run-010"
    assert sig.schedule_id == "sched-001"
    assert sig.reason_code == RunReasons.FAILED_EXCEPTION
    assert "bad action_kind" in sig.error_detail


@pytest.mark.asyncio
async def test_fire_build_argv_exception_does_not_mint_when_write_lost_race():
    """A lost race on the pre-launch failure write (a concurrent writer
    already finalized the row) must not mint a signal describing an outcome
    this call didn't actually commit."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.update_status = AsyncMock(return_value=False)  # every terminal write "loses"
    bus = SchedulerSignalBus()
    captured: list = []
    bus.observe(ScheduleRunFailed, handler=captured.append)
    engine = SchedulerEngine(svc=svc, signal_bus=bus)
    schedule = _minimal_schedule()

    with patch(
        "lionagi.studio.scheduler.subprocess.build_argv",
        side_effect=ValueError("bad action_kind"),
    ):
        await engine._fire(schedule, "run-011", trigger_context={"scheduled": True})

    assert captured == []


# ---------------------------------------------------------------------------
# Regression: a broken handler surfaces a durable record and the tick loop
# continues -- it must not stop this schedule's own bookkeeping, and a
# following, unrelated fire must still succeed normally.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broken_handler_surfaces_admin_event_and_fire_completes(tmp_path, monkeypatch):
    import lionagi.state.db as state_db_mod
    from lionagi.state.db import StateDB
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    svc = _make_svc()
    bus = SchedulerSignalBus()

    def _boom(sig):
        raise RuntimeError("handler bug")

    bus.observe(ScheduleRunSucceeded, handler=_boom)
    engine = SchedulerEngine(svc=svc, signal_bus=bus)
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
        # Must not raise -- the handler bug is contained at the mint call site.
        await engine._fire(schedule, "run-007", trigger_context={"scheduled": True})

    # The schedule_run's own bookkeeping (invocation/run rows, status writes,
    # max_runs check) still ran normally despite the broken handler.
    svc.create_invocation.assert_awaited_once()
    svc.create_schedule_run.assert_not_awaited()
    svc.create_schedule_run_and_advance.assert_awaited_once()
    assert svc.update_status.await_count == 3  # running + completed + invocation

    async with StateDB(fake_db) as db:
        events = await db.list_admin_events(action="scheduler_signal_handler_failed")
    assert len(events) == 1
    assert events[0]["target_id"] == "run-007"


@pytest.mark.asyncio
async def test_handler_cancelled_error_at_exit_mint_does_not_cancel_completed_run(
    tmp_path, monkeypatch
):
    """A handler-raised cancellation is a handler failure, not scheduler shutdown."""
    import lionagi.state.db as state_db_mod
    from lionagi.state.db import StateDB
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    svc = _make_svc()
    bus = SchedulerSignalBus()

    async def _cancelled(sig):
        raise asyncio.CancelledError()

    bus.observe(ScheduleRunSucceeded, handler=_cancelled)
    engine = SchedulerEngine(svc=svc, signal_bus=bus)

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
            _minimal_schedule(), "run-handler-cancel", trigger_context={"scheduled": True}
        )

    terminal_statuses = [
        call.kwargs.get("new_status") for call in svc.update_status.await_args_list
    ]
    assert "completed" in terminal_statuses
    assert "cancelled" not in terminal_statuses
    assert all(
        call.kwargs.get("error_detail") != "Scheduler shutdown"
        for call in svc.update_schedule_run.await_args_list
    )

    async with StateDB(fake_db) as db:
        events = await db.list_admin_events(action="scheduler_signal_handler_failed")
    assert len(events) == 1
    assert events[0]["target_id"] == "run-handler-cancel"


@pytest.mark.asyncio
async def test_dispatch_signal_preserves_real_task_cancellation():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    started = asyncio.Event()
    bus = SchedulerSignalBus()

    async def _wait_forever(sig):
        started.set()
        await asyncio.Event().wait()

    bus.observe(ScheduleRunSucceeded, handler=_wait_forever)
    engine = SchedulerEngine(svc=_make_svc(), signal_bus=bus)
    signal = ScheduleRunSucceeded(
        run_id="run-real-cancel",
        schedule_id="sched-001",
        reason_code=RunReasons.COMPLETED_OK,
    )

    with patch("lionagi.studio.scheduler.engine.record_handler_failure", new=AsyncMock()) as record:
        task = asyncio.create_task(engine._dispatch_signal(signal))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_broken_predicate_does_not_block_sibling_handler_and_surfaces_admin_event(
    tmp_path, monkeypatch
):
    """A predicate that raises during matching must not abort dispatch
    before a sibling, independently-registered handler ever runs -- the
    predicate failure becomes a collected dispatch failure, surfaced as the
    same durable admin_events record as any other handler bug."""
    import lionagi.state.db as state_db_mod
    from lionagi.state.db import StateDB
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    svc = _make_svc()
    bus = SchedulerSignalBus()
    healthy: list = []

    def _boom_predicate(sig):
        raise RuntimeError("predicate bug")

    bus.observe(ScheduleRunSucceeded, _boom_predicate, handler=lambda sig: None)
    bus.observe(ScheduleRunSucceeded, handler=healthy.append)
    engine = SchedulerEngine(svc=svc, signal_bus=bus)
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
        await engine._fire(schedule, "run-012", trigger_context={"scheduled": True})

    assert len(healthy) == 1  # sibling handler ran despite the raising predicate

    async with StateDB(fake_db) as db:
        events = await db.list_admin_events(action="scheduler_signal_handler_failed")
    assert len(events) == 1
    assert events[0]["target_id"] == "run-012"
    details = events[0]["details"]
    if isinstance(details, str):
        import json

        details = json.loads(details)
    assert "RuntimeError: predicate bug" in details["errors"][0]


@pytest.mark.asyncio
async def test_unrelated_fire_still_succeeds_after_a_prior_handler_failure(tmp_path, monkeypatch):
    """One schedule's broken handler must not poison a subsequent, unrelated
    fire on the same engine/bus."""
    import lionagi.state.db as state_db_mod
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    svc = _make_svc()
    bus = SchedulerSignalBus()

    def _boom(sig):
        raise RuntimeError("handler bug")

    bus.observe(ScheduleRunSucceeded, handler=_boom)
    engine = SchedulerEngine(svc=svc, signal_bus=bus)
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
        await engine._fire(schedule, "run-008", trigger_context={"scheduled": True})
        # Second, unrelated fire on the same engine/bus must still complete
        # normally -- the handler exception from the first fire must not
        # have crashed the tick loop or left the bus/engine in a bad state.
        await engine._fire(schedule, "run-009", trigger_context={"scheduled": True})

    assert svc.create_schedule_run_and_advance.await_count == 2
