# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Crash-interleaving regression tests for the scheduler's occurrence-insert
+ cursor-advance atomicity.

These exercise a real temp-dir sqlite ``StateDB`` (never the process-wide
``~/.lionagi/state.db``) rather than mocks, because the property under test
is a genuine transaction boundary: a simulated mid-write crash must leave
*zero* durable trace, and only a real database rollback proves that.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionagi.studio.scheduler.engine import SchedulerEngine
from lionagi.studio.services.scheduler_state import _DBSchedulerStateService


def _schedule_row(schedule_id: str, **overrides) -> dict:
    base = {
        "id": schedule_id,
        "name": f"sched-{schedule_id}",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_model": "gpt-4.1-mini",
        "action_prompt": "ping",
        "enabled": 1,
        "next_fire_at": 1_000.0,
        "missed_fire_policy": "skip",
    }
    base.update(overrides)
    return base


def _run_row(run_id: str, schedule_id: str, *, fired_at: float, **overrides) -> dict:
    base = {
        "id": run_id,
        "schedule_id": schedule_id,
        "trigger_context": {"source": "cron"},
        "action_kind": "agent",
        "action_args": {"prompt": "ping"},
        "status": "running",
        "fired_at": fired_at,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_crash_between_insert_and_advance_does_not_double_fire(tmp_path):
    """(a) A process death between the occurrence insert and the cursor
    advance must roll back BOTH halves -- not just leave the schedule's
    cursor behind while the occurrence row survives. Otherwise a restart
    that recomputes "still due" from the stale cursor would fire again for
    an occurrence that the crashed attempt already (partially) recorded.
    """
    db_path = tmp_path / "state.db"
    sid = "sched-a"

    async with StateDB(db_path) as db:
        await db.create_schedule(_schedule_row(sid, next_fire_at=1000.0))

    original_execute = AsyncConnection.execute

    async def _crash(self, statement, *args, **kwargs):
        if "UPDATE schedules" in str(statement):
            raise RuntimeError("simulated crash before cursor advance")
        return await original_execute(self, statement, *args, **kwargs)

    async with StateDB(db_path) as db:
        with patch.object(AsyncConnection, "execute", _crash):
            with pytest.raises(RuntimeError, match="simulated crash"):
                await db.create_schedule_run_and_advance(
                    _run_row("run-1", sid, fired_at=1000.0),
                    schedule_id=sid,
                    schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
                )

    # Post-crash: neither half landed -- the occurrence row is absent and
    # the schedule's cursor is untouched.
    async with StateDB(db_path) as db:
        runs = await db.list_schedule_runs(sid)
        schedule = await db.get_schedule(sid)
    assert runs == []
    assert schedule["next_fire_at"] == 1000.0

    # "Restart": the same occurrence is retried for real (no crash this
    # time) and must produce exactly one durable row -- proving the
    # crashed attempt left nothing behind to double up against.
    async with StateDB(db_path) as db:
        await db.create_schedule_run_and_advance(
            _run_row("run-1", sid, fired_at=1000.0),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
        )
        runs = await db.list_schedule_runs(sid)
        schedule = await db.get_schedule(sid)
    assert len(runs) == 1
    assert schedule["next_fire_at"] == 2000.0


@pytest.mark.asyncio
async def test_github_path_crash_before_second_event_refires_only_second(tmp_path):
    """(b) Two github_poll events are dispatched in the same poll batch.
    The first event's occurrence-insert + github_cursor advance commits
    cleanly; the process then dies mid-transaction for the second event.
    A restart must see the first event as already recorded (so it is never
    re-fired) while the second is still open (schedule_run_exists_since is
    False for it, and the cursor has not moved past it) -- so the next poll
    re-fires ONLY the second event, never both.
    """
    db_path = tmp_path / "state.db"
    sid = "sched-b"
    event1_time = "2026-07-07T10:00:00Z"
    event2_time = "2026-07-07T11:00:00Z"

    async with StateDB(db_path) as db:
        await db.create_schedule(
            _schedule_row(
                sid,
                trigger_type="github_poll",
                github_repo="acme/widgets",
                github_cursor=None,
            )
        )

        # Event 1: normal atomic commit.
        await db.create_schedule_run_and_advance(
            _run_row("run-event1", sid, fired_at=1000.0),
            schedule_id=sid,
            schedule_fields={"github_cursor": event1_time, "last_fired_at": 1000.0},
        )

    original_execute = AsyncConnection.execute

    async def _crash(self, statement, *args, **kwargs):
        if "UPDATE schedules" in str(statement):
            raise RuntimeError("simulated crash before cursor advance")
        return await original_execute(self, statement, *args, **kwargs)

    async with StateDB(db_path) as db:
        with patch.object(AsyncConnection, "execute", _crash):
            with pytest.raises(RuntimeError, match="simulated crash"):
                await db.create_schedule_run_and_advance(
                    _run_row("run-event2", sid, fired_at=1100.0),
                    schedule_id=sid,
                    schedule_fields={"github_cursor": event2_time, "last_fired_at": 1100.0},
                )

    async with StateDB(db_path) as db:
        runs = await db.list_schedule_runs(sid)
        schedule = await db.get_schedule(sid)
        exists_since_event2 = await db.schedule_run_exists_since(sid, since=1100.0)

    # Only event 1 is durable; the cursor sits at event 1, never advanced
    # to (or past) event 2.
    assert len(runs) == 1
    assert runs[0]["id"] == "run-event1"
    assert schedule["github_cursor"] == event1_time
    assert exists_since_event2 is False

    # "Restart": re-fire for event 2 only (event 1 is never re-dispatched
    # because the poll's own cursor filter -- github_cursor -- excludes it;
    # this call models the recorded outcome of that re-poll).
    async with StateDB(db_path) as db:
        await db.create_schedule_run_and_advance(
            _run_row("run-event2", sid, fired_at=1100.0),
            schedule_id=sid,
            schedule_fields={"github_cursor": event2_time, "last_fired_at": 1100.0},
        )
        runs = await db.list_schedule_runs(sid)
        schedule = await db.get_schedule(sid)

    assert len(runs) == 2
    assert {r["id"] for r in runs} == {"run-event1", "run-event2"}
    assert schedule["github_cursor"] == event2_time


@pytest.mark.asyncio
async def test_missed_fire_recovery_skips_occurrence_already_in_schedule_runs(
    tmp_path, monkeypatch
):
    """(c) Startup/missed-fire recovery must consult schedule_runs before
    queuing a recovery fire. A schedule whose next_fire_at is past-due but
    which already has a schedule_run row recorded at-or-after that time
    (the atomic transaction committed, then the process died before the
    run's terminal write) must NOT be re-fired -- only have its cursor
    advanced past the already-handled occurrence.
    """
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    sid = "sched-c"
    due_at = 1000.0

    async with StateDB(db_path) as db:
        await db.create_schedule(
            _schedule_row(sid, next_fire_at=due_at, missed_fire_policy="run_once")
        )
        # Simulate: the atomic transaction already committed this
        # occurrence + advanced the cursor once (to a value that is itself
        # still in the past relative to "now" in this test, so the
        # schedule is still seen as due -- exercising the recovery path
        # rather than the ordinary tick).
        await db.create_schedule_run_and_advance(
            _run_row("run-crashed", sid, fired_at=due_at),
            schedule_id=sid,
            schedule_fields={"next_fire_at": due_at, "last_fired_at": due_at},
        )

    svc = _DBSchedulerStateService()
    engine = SchedulerEngine(svc=svc)

    with (
        patch.object(engine, "_tracked_fire") as mock_tracked_fire,
        patch.object(engine, "_recover_missed_fire_run_once") as mock_recover,
        patch.object(engine, "_record_missed_fire_skip") as mock_skip,
    ):
        await engine._check_missed_fires()

    mock_tracked_fire.assert_not_called()
    mock_recover.assert_not_called()
    mock_skip.assert_not_called()

    async with StateDB(db_path) as db:
        schedule = await db.get_schedule(sid)
        runs = await db.list_schedule_runs(sid)

    # No second occurrence was recorded, and the cursor moved past the
    # already-handled fire time instead of staying stuck due-in-the-past.
    assert len(runs) == 1
    assert schedule["next_fire_at"] is not None
    assert schedule["next_fire_at"] > due_at


@pytest.mark.asyncio
async def test_missed_fire_recovery_still_fires_past_capacity_deferred_skip(tmp_path, monkeypatch):
    """(c2) A capacity-deferred fire (global concurrent-fire cap reached)
    writes an audit-only 'skipped' schedule_run row via
    _maybe_record_deferred() and deliberately leaves next_fire_at
    untouched, so the same due occurrence retries on the next tick. If the
    process restarts before that retry, the recovery scan must not mistake
    this audit row for a genuine fire -- schedule_run_exists_since()
    excludes status='skipped' rows precisely so this occurrence still gets
    a real recovery fire instead of being silently cursor-advanced past.
    """
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    sid = "sched-c2"
    due_at = 1000.0

    async with StateDB(db_path) as db:
        await db.create_schedule(
            _schedule_row(sid, next_fire_at=due_at, missed_fire_policy="run_once")
        )
        # The capacity-deferred audit row: status='skipped', schedule_id
        # cursor untouched (mirrors _maybe_record_deferred + create_skipped_run,
        # which calls create_schedule_run() directly -- never
        # create_schedule_run_and_advance() -- exactly so next_fire_at stays
        # put for the retry).
        await db.create_schedule_run(
            _run_row(
                "run-deferred",
                sid,
                fired_at=due_at,
                status="skipped",
                trigger_context={"deferred_capacity": True, "fired_at": due_at},
            )
        )

    svc = _DBSchedulerStateService()
    engine = SchedulerEngine(svc=svc)

    with (
        patch.object(engine, "_recover_missed_fire_run_once") as mock_recover,
        patch.object(engine, "_record_missed_fire_skip") as mock_skip,
    ):
        await engine._check_missed_fires()

    # The deferred-skip audit row must not be treated as "already fired":
    # recovery proceeds through the normal missed_fire_policy branch
    # instead of taking the already-recorded shortcut that would silently
    # advance the cursor without ever firing this occurrence.
    mock_recover.assert_called_once()
    mock_skip.assert_not_called()

    # Genuinely-fired rows are unaffected by the exclusion: a completed run
    # still counts as "already recorded".
    async with StateDB(db_path) as db:
        await db.create_schedule_run(
            _run_row("run-completed", sid, fired_at=due_at, status="completed")
        )
        exists = await db.schedule_run_exists_since(sid, since=due_at)
    assert exists is True


@pytest.mark.asyncio
async def test_recovery_refires_occurrence_committed_but_never_dispatched(tmp_path, monkeypatch):
    """(e) A crash between the occurrence-insert/cursor-advance transaction
    committing and spawn_and_wait() confirming the external process
    launched leaves a durable status='running' row with dispatched_at
    still NULL, and the schedule's cursor already moved past it -- so
    ordinary missed-fire recovery (schedule_run_exists_since) will never
    reconsider this schedule as due again; the occurrence would otherwise
    be silently lost. _recover_undispatched_fires() must tombstone the
    orphaned row (failed / FAILED_NEVER_DISPATCHED) and re-fire a fresh
    occurrence carrying the SAME trigger_context the orphaned attempt
    never got to use -- the at-least-once side of the delivery contract.
    """
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    sid = "sched-e"
    fired_at = 1000.0
    orphaned_trigger_context = {"source": "github_poll", "pr_number": 42}

    async with StateDB(db_path) as db:
        await db.create_schedule(_schedule_row(sid, next_fire_at=2000.0))
        # Simulate _fire_inner()'s atomic commit landing (row + cursor
        # advance both durable), then the daemon dying before
        # spawn_and_wait's on_launched callback ever stamps dispatched_at.
        await db.create_schedule_run_and_advance(
            _run_row(
                "run-orphaned",
                sid,
                fired_at=fired_at,
                trigger_context=orphaned_trigger_context,
            ),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 2000.0, "last_fired_at": fired_at},
        )

    svc = _DBSchedulerStateService()
    engine = SchedulerEngine(svc=svc)

    tracked_calls: list[tuple] = []
    original_tracked_fire = engine._tracked_fire

    def _spy_tracked_fire(*args, **kwargs):
        tracked_calls.append((args, kwargs))
        return original_tracked_fire(*args, **kwargs)

    engine._tracked_fire = _spy_tracked_fire  # type: ignore[method-assign]

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.resolve_li_executable",
            return_value=(["true"], None),
        ),
        patch("lionagi.studio.scheduler.subprocess.build_argv", return_value=(["true"], None)),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await engine._recover_undispatched_fires()
        if engine._fire_tasks:
            await asyncio.gather(*engine._fire_tasks)

    # The orphaned row is tombstoned, never left dangling at "running".
    async with StateDB(db_path) as db:
        orphaned = await db.get_schedule_run("run-orphaned")
        remaining_undispatched = await db.list_undispatched_schedule_runs()
    assert orphaned["status"] == "failed"
    assert orphaned["status_reason_code"] == RunReasons.FAILED_NEVER_DISPATCHED
    assert remaining_undispatched == []

    # A fresh occurrence was re-fired with the SAME trigger_context.
    assert len(tracked_calls) == 1
    _args, kwargs = tracked_calls[0]
    assert kwargs["trigger_context"] == orphaned_trigger_context

    async with StateDB(db_path) as db:
        runs = await db.list_schedule_runs(sid)
    statuses = {r["id"]: r["status"] for r in runs}
    assert statuses["run-orphaned"] == "failed"
    # The retried run landed a second, independent occurrence row.
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_recovery_never_touches_a_row_with_confirmed_dispatch(tmp_path, monkeypatch):
    """Once dispatched_at is set, the row is outside
    _recover_undispatched_fires()'s scan entirely -- the external process is
    confirmed to exist, so this is the contract's at-most-once boundary: a
    daemon crash from here on is left to the ordinary stale-run reaper
    (timed_out), never auto-retried, to avoid a duplicate real-world side
    effect from an action that may already be running or finished.
    """
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    sid = "sched-f"

    async with StateDB(db_path) as db:
        await db.create_schedule(_schedule_row(sid, next_fire_at=2000.0))
        await db.create_schedule_run_and_advance(
            _run_row("run-dispatched", sid, fired_at=1000.0),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
        )
        # Launch confirmed, mirroring spawn_and_wait's on_launched callback.
        await db.update_schedule_run("run-dispatched", dispatched_at=1000.5)

    svc = _DBSchedulerStateService()
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire") as mock_tracked_fire:
        await engine._recover_undispatched_fires()

    mock_tracked_fire.assert_not_called()

    async with StateDB(db_path) as db:
        run = await db.get_schedule_run("run-dispatched")
    # Untouched -- still "running", exactly as a genuinely in-flight (or
    # merely lost-outcome) dispatched action should be left for the
    # stale-run reaper, not this recovery scan.
    assert run["status"] == "running"


@pytest.mark.asyncio
async def test_recovery_tombstones_undispatched_chain_child_without_retry(tmp_path, monkeypatch):
    """An undispatched chain child (chain_depth > 0, an on_success/on_fail
    follow-on) is tombstoned like any other orphan, but NOT auto-retried --
    the narrower, documented gap in _fire_inner()'s delivery contract: the
    parent occurrence's own recorded outcome is unaffected, only a
    follow-on step is lost rather than automatically replayed.
    """
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    sid = "sched-g"

    async with StateDB(db_path) as db:
        await db.create_schedule(_schedule_row(sid, next_fire_at=2000.0))
        # The chain-parent row must exist first -- chain_parent_id is a real
        # FK reference to schedule_runs(id). Its own status is irrelevant to
        # this test; only its existence satisfies the constraint.
        await db.create_schedule_run_and_advance(
            _run_row("run-parent", sid, fired_at=999.0),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 1000.0, "last_fired_at": 999.0},
        )
        # Mark the parent as confirmed-dispatched so it isn't itself picked
        # up by the recovery scan below (it's top-level, chain_depth == 0) --
        # this test isolates the chain-child-specific tombstone-without-retry
        # behavior from the ordinary top-level re-fire path.
        await db.update_schedule_run("run-parent", dispatched_at=999.5)
        await db.create_schedule_run_and_advance(
            _run_row(
                "run-chain-child",
                sid,
                fired_at=1000.0,
                chain_parent_id="run-parent",
                chain_depth=1,
            ),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
        )

    svc = _DBSchedulerStateService()
    engine = SchedulerEngine(svc=svc)

    with patch.object(engine, "_tracked_fire") as mock_tracked_fire:
        await engine._recover_undispatched_fires()

    mock_tracked_fire.assert_not_called()

    async with StateDB(db_path) as db:
        run = await db.get_schedule_run("run-chain-child")
    assert run["status"] == "failed"
    assert run["status_reason_code"] == RunReasons.FAILED_NEVER_DISPATCHED
