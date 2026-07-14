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
    """(a) A process death between the occurrence insert and cursor advance must roll back BOTH halves, not leave the cursor behind while the occurrence row survives -- otherwise a restart recomputing 'still due' from the stale cursor fires again for an already-recorded occurrence."""
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
    """(b) Two github_poll events dispatch in the same batch; the first's insert+cursor-advance commits, then the process dies mid-transaction for the second. A restart must see the first as already recorded (never re-fired) and the second still open, so the next poll re-fires ONLY the second."""
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
    """(c) Startup/missed-fire recovery must consult schedule_runs before queuing a recovery fire: a past-due schedule that already has a schedule_run row at-or-after that time (committed, then the process died before the terminal write) must NOT be re-fired -- only have its cursor advanced past it."""
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
    """(c2) A capacity-deferred fire writes an audit-only 'skipped' schedule_run row and deliberately leaves next_fire_at untouched so the occurrence retries next tick; recovery must not mistake that audit row for a genuine fire -- schedule_run_exists_since() excludes status='skipped' rows so this occurrence still gets a real recovery fire."""
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
async def test_missed_fire_recovery_still_fires_past_chain_child_only_row(tmp_path, monkeypatch):
    """(c3) A chain-child row (chain_depth > 0, e.g. an on_success/on_fail follow-on)
    shares the parent's schedule_id but is not itself a top-level occurrence of the
    schedule. schedule_run_exists_since() must exclude chain-child rows so a
    genuinely-due top-level occurrence is not masked and silently dropped."""
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    sid = "sched-c3"
    due_at = 1000.0

    async with StateDB(db_path) as db:
        await db.create_schedule(
            _schedule_row(sid, next_fire_at=due_at, missed_fire_policy="run_once")
        )
        # Only a chain-child row exists at-or-after due_at -- e.g. an
        # on_success follow-on from some earlier, unrelated top-level fire.
        # This must NOT satisfy the "already fired" check for the due
        # top-level occurrence. chain_parent_id is a self-referential FK, so
        # the parent row must exist first (its own fired_at is deliberately
        # well before due_at -- it is not itself a fresh top-level fire).
        await db.create_schedule_run(
            _run_row("run-parent", sid, fired_at=due_at - 500.0, status="completed")
        )
        await db.create_schedule_run(
            _run_row(
                "run-chain-child",
                sid,
                fired_at=due_at,
                status="completed",
                chain_parent_id="run-parent",
                chain_depth=1,
            )
        )
        exists = await db.schedule_run_exists_since(sid, since=due_at)

    # Direct check: a chain-child-only row must not count as a top-level fire.
    assert exists is False

    svc = _DBSchedulerStateService()
    engine = SchedulerEngine(svc=svc)

    with (
        patch.object(engine, "_recover_missed_fire_run_once") as mock_recover,
        patch.object(engine, "_record_missed_fire_skip") as mock_skip,
    ):
        await engine._check_missed_fires()

    # Recovery must take the run_once branch for this due occurrence rather
    # than silently advancing next_fire_at because the chain-child row was
    # mistaken for an already-recorded top-level fire.
    mock_recover.assert_called_once()
    mock_skip.assert_not_called()

    # A genuine top-level row at-or-after due_at is unaffected by the
    # chain_depth exclusion: it still counts as "already recorded".
    async with StateDB(db_path) as db:
        await db.create_schedule_run(
            _run_row("run-top-level", sid, fired_at=due_at, status="completed")
        )
        exists = await db.schedule_run_exists_since(sid, since=due_at)
    assert exists is True


@pytest.mark.asyncio
async def test_recovery_refires_occurrence_committed_but_never_dispatched(tmp_path, monkeypatch):
    """(e) A crash between the occurrence-insert/cursor-advance commit and spawn_and_wait() confirming launch leaves a durable status='running' row with dispatched_at NULL, past the cursor -- invisible to ordinary missed-fire recovery. _recover_undispatched_fires() must tombstone the orphan and re-fire a fresh occurrence carrying the same trigger_context, the at-least-once side of the delivery contract."""
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
    """Once dispatched_at is set, the row is outside _recover_undispatched_fires()'s scan entirely -- the at-most-once boundary: a crash from here on is left to the ordinary stale-run reaper, never auto-retried, to avoid a duplicate real-world side effect."""
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
    """An undispatched chain child (chain_depth > 0) is tombstoned like any other orphan, but NOT auto-retried -- the documented gap in _fire_inner()'s delivery contract: only the follow-on step is lost, the parent occurrence's own recorded outcome is unaffected."""
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


@pytest.mark.asyncio
async def test_tombstone_and_replace_schedule_run_is_atomic(tmp_path):
    """The old two-write recovery shape (flip the orphan, then separately insert the replacement) could crash between the writes and lose the occurrence for good; tombstone_and_replace_schedule_run() does both in ONE transaction -- proven by forcing the replacement INSERT to raise mid-transaction and confirming the orphan's UPDATE rolled back too, still 'running' and visible to a fresh scan."""
    db_path = tmp_path / "state.db"
    sid = "sched-atomic"

    async with StateDB(db_path) as db:
        await db.create_schedule(_schedule_row(sid, next_fire_at=2000.0))
        await db.create_schedule_run_and_advance(
            _run_row("run-orphan", sid, fired_at=1000.0),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
        )

    original_execute = AsyncConnection.execute

    async def _crash_on_insert(self, statement, *args, **kwargs):
        if "INSERT INTO schedule_runs" in str(statement):
            raise RuntimeError("simulated crash between flip and insert")
        return await original_execute(self, statement, *args, **kwargs)

    async with StateDB(db_path) as db:
        with patch.object(AsyncConnection, "execute", _crash_on_insert):
            with pytest.raises(RuntimeError, match="simulated crash"):
                await db.tombstone_and_replace_schedule_run(
                    "run-orphan",
                    _run_row("run-replacement", sid, fired_at=1500.0),
                    expected_orphan_status="running",
                )

    # Post-crash: the orphan's flip rolled back along with the aborted
    # insert -- never neither, never "flipped but replacement missing".
    async with StateDB(db_path) as db:
        orphan = await db.get_schedule_run("run-orphan")
        replacement = await db.get_schedule_run("run-replacement")
        undispatched = await db.list_undispatched_schedule_runs()
    assert orphan["status"] == "running"
    assert orphan["dispatched_at"] is None
    assert replacement is None
    assert [r["id"] for r in undispatched] == ["run-orphan"]

    # "Restart": a real (non-crashing) call now succeeds atomically.
    async with StateDB(db_path) as db:
        applied = await db.tombstone_and_replace_schedule_run(
            "run-orphan",
            _run_row("run-replacement", sid, fired_at=1500.0),
            expected_orphan_status="running",
        )
        orphan = await db.get_schedule_run("run-orphan")
        replacement = await db.get_schedule_run("run-replacement")
    assert applied is True
    assert orphan["status"] == "failed"
    assert replacement["status"] == "running"
    assert replacement["dispatched_at"] is None


@pytest.mark.asyncio
async def test_recovery_leaves_orphan_untouched_when_refire_crashes_before_atomic_write(
    tmp_path, monkeypatch
):
    """A crash during recovery re-fire, at any point before _write_occurrence()'s atomic transaction runs, must leave the orphan completely untouched (nothing to roll back yet); a fresh recovery pass over the same state must find and retry it, proving the occurrence is never lost even when the re-fire attempt itself fails early."""
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    sid = "sched-early-crash"
    orphaned_trigger_context = {"source": "cron"}

    async with StateDB(db_path) as db:
        await db.create_schedule(_schedule_row(sid, next_fire_at=2000.0))
        await db.create_schedule_run_and_advance(
            _run_row(
                "run-orphaned", sid, fired_at=1000.0, trigger_context=orphaned_trigger_context
            ),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
        )

    svc = _DBSchedulerStateService()
    engine = SchedulerEngine(svc=svc)

    # Simulate a crash mid-fire, well before _write_occurrence() ever runs:
    # create_invocation() succeeds (it always durably lands first, same as
    # the ordinary happy path), but build_argv() (a plain sync function)
    # blows up with something that is NOT the ordinary "invalid action"
    # exception path -- an unrecoverable crash of the fire task itself.
    # A BaseException that is deliberately NOT KeyboardInterrupt/SystemExit:
    # those two are special-cased by asyncio's event loop and by pytest-xdist
    # itself (a KeyboardInterrupt propagating out of an awaited task reads as
    # a real Ctrl-C and takes the whole worker process down with it) -- this
    # only needs to be something _fire_inner()'s `except Exception` does not
    # catch, not literally the SIGINT-flavored crash signal.
    class _SimulatedHardCrash(BaseException):
        pass

    def _crash(*_args, **_kwargs):
        raise _SimulatedHardCrash("simulated hard crash mid-fire")

    with patch("lionagi.studio.scheduler.subprocess.build_argv", side_effect=_crash):
        await engine._recover_undispatched_fires()
        for task in list(engine._fire_tasks):
            with pytest.raises(_SimulatedHardCrash):
                await task

    # Untouched: no atomic write ever ran, so nothing changed.
    async with StateDB(db_path) as db:
        orphan = await db.get_schedule_run("run-orphaned")
        runs = await db.list_schedule_runs(sid)
        undispatched = await db.list_undispatched_schedule_runs()
    assert orphan["status"] == "running"
    assert orphan["dispatched_at"] is None
    assert len(runs) == 1  # no replacement row was ever inserted
    assert [r["id"] for r in undispatched] == ["run-orphaned"]

    # A fresh recovery pass (no crash this time) finds and retries it.
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

    async with StateDB(db_path) as db:
        orphan = await db.get_schedule_run("run-orphaned")
        runs = await db.list_schedule_runs(sid)
    assert orphan["status"] == "failed"
    assert orphan["status_reason_code"] == RunReasons.FAILED_NEVER_DISPATCHED
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_recovery_never_double_fires_across_two_passes(tmp_path, monkeypatch):
    """A second _recover_undispatched_fires() pass over state left by a first, fully-completed pass must find nothing left to do: the replacement occurrence is already dispatched and the original orphan already terminal -- two passes over the same recovery must produce exactly one re-fire."""
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    sid = "sched-no-double-fire"

    async with StateDB(db_path) as db:
        await db.create_schedule(_schedule_row(sid, next_fire_at=2000.0))
        await db.create_schedule_run_and_advance(
            _run_row("run-orphaned", sid, fired_at=1000.0),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
        )

    svc = _DBSchedulerStateService()
    engine = SchedulerEngine(svc=svc)

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
        # Pass one: finds the orphan, re-fires it, and the mocked
        # spawn_and_wait's on_launched callback stamps dispatched_at on
        # the replacement -- a fully successful recovery cycle.
        await engine._recover_undispatched_fires()
        if engine._fire_tasks:
            await asyncio.gather(*engine._fire_tasks)

        async with StateDB(db_path) as db:
            undispatched_after_pass_one = await db.list_undispatched_schedule_runs()
        assert undispatched_after_pass_one == []

        with patch.object(engine, "_tracked_fire") as mock_tracked_fire:
            # Pass two: nothing left to recover.
            await engine._recover_undispatched_fires()
        mock_tracked_fire.assert_not_called()

    async with StateDB(db_path) as db:
        runs = await db.list_schedule_runs(sid)
    # Exactly one re-fire happened across both passes: the original orphan
    # plus its single replacement, never a second independent retry.
    assert len(runs) == 2
    statuses = {r["id"]: r["status"] for r in runs}
    assert statuses["run-orphaned"] == "failed"
    replacement_id = next(rid for rid in statuses if rid != "run-orphaned")
    assert statuses[replacement_id] == "completed"


@pytest.mark.asyncio
async def test_tombstone_and_replace_schedule_run_refuses_when_dispatch_confirmed(tmp_path):
    """The CAS predicate requires dispatched_at IS NULL, not just status='running': a launch confirmation landing between a recovery scan and this write means the row is no longer undispatched, and tombstoning it would flip an actually-launched run to 'failed' and fire a duplicate -- reviewer repro: dispatched_at already set must refuse (applied=False), inserting nothing."""
    db_path = tmp_path / "state.db"
    sid = "sched-dispatched-race"

    async with StateDB(db_path) as db:
        await db.create_schedule(_schedule_row(sid, next_fire_at=2000.0))
        await db.create_schedule_run_and_advance(
            _run_row("run-live", sid, fired_at=1000.0),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
        )
        # Launch confirmed -- status stays 'running', only dispatched_at
        # moves (mirrors spawn_and_wait's on_launched callback).
        await db.update_schedule_run("run-live", dispatched_at=1000.5)

    async with StateDB(db_path) as db:
        applied = await db.tombstone_and_replace_schedule_run(
            "run-live",
            _run_row("run-replacement", sid, fired_at=1500.0),
            expected_orphan_status="running",
        )
        live = await db.get_schedule_run("run-live")
        replacement = await db.get_schedule_run("run-replacement")

    assert applied is False
    assert live["status"] == "running"
    assert live["dispatched_at"] == 1000.5
    assert replacement is None


@pytest.mark.asyncio
async def test_recovery_refire_is_noop_when_dispatch_confirmed_mid_race(tmp_path, monkeypatch):
    """The reviewer-specified race: a concurrently-confirmed launch (dispatched_at stamped) lands after recovery's scan but before its atomic re-fire write; the strengthened CAS makes that write a no-op, leaving the live run completely untouched, and abandonment cancels only the doomed pre-spawn recovery invocation, never the live run's own."""
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    sid = "sched-race"

    async with StateDB(db_path) as db:
        await db.create_schedule(_schedule_row(sid, next_fire_at=2000.0))
        await db.create_invocation({"id": "inv-live", "skill": "test", "started_at": 1000.0})
        await db.create_schedule_run_and_advance(
            _run_row("run-live", sid, fired_at=1000.0, invocation_id="inv-live"),
            schedule_id=sid,
            schedule_fields={"next_fire_at": 2000.0, "last_fired_at": 1000.0},
        )

    svc = _DBSchedulerStateService()
    engine = SchedulerEngine(svc=svc)

    original_create_invocation = svc.create_invocation
    recovery_inv_ids: list[str] = []

    async def _create_invocation_then_confirm_launch(invocation):
        # Recovery already scanned and decided to re-fire "run-live" by the
        # time this runs (it is _fire_inner()'s very first durable write for
        # the re-fire attempt). Simulate a concurrent scheduler -- or the
        # original background task -- confirming the ORIGINAL launch right
        # here, strictly between the scan and the re-fire's own atomic
        # tombstone-and-replace write further down in _fire_inner().
        recovery_inv_ids.append(invocation["id"])
        await original_create_invocation(invocation)
        await svc.update_schedule_run("run-live", dispatched_at=1234.5)

    with (
        patch.object(svc, "create_invocation", side_effect=_create_invocation_then_confirm_launch),
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

    assert len(recovery_inv_ids) == 1
    recovery_inv_id = recovery_inv_ids[0]

    async with StateDB(db_path) as db:
        live = await db.get_schedule_run("run-live")
        runs = await db.list_schedule_runs(sid)
        live_invocation = await db.get_invocation("inv-live")
        recovery_invocation = await db.get_invocation(recovery_inv_id)

    # The live, actually-launched run is untouched: never flipped to
    # 'failed', and its dispatched_at survives exactly as the racing
    # confirmation set it. No replacement occurrence was ever inserted.
    assert live["status"] == "running"
    assert live["dispatched_at"] == 1234.5
    assert len(runs) == 1

    # The live run's OWN invocation is completely untouched.
    assert live_invocation["status"] == "running"

    # Only the doomed recovery attempt's invocation was cancelled.
    assert recovery_invocation["status"] == "cancelled"
    assert recovery_invocation["status_reason_code"] == RunReasons.CANCELLED_STALE_AUTO
