# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Scheduler engine — in-process asyncio tick loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lionagi.state.reasons import RunReasons, ScheduleReasons
from lionagi.studio.scheduler import subprocess as _subprocess
from lionagi.studio.scheduler import threshold as _threshold
from lionagi.studio.services.scheduler_state import (
    SchedulerStateService,
    create_skipped_run,
    default_scheduler_state,
    resolve_invocation_terminal,
)

_log = logging.getLogger(__name__)

_MAX_CHAIN_DEPTH = 10
_TICK_INTERVAL = 30  # seconds
# Deferred-capacity skipped-run records are throttled to one per schedule per
# this many deferrals (the first deferral always emits), so sustained
# saturation doesn't spam schedule_runs.
_DEFERRED_RECORD_EVERY = 10


class _MaxRunsClaim:
    """One-shot handle for an in-process max_runs reservation.

    Returned by ``_reserve_max_runs_budget()`` when a top-level fire is
    allowed to proceed. The holder (``_fire()``) must call ``release()``
    exactly once, from a ``finally`` block that covers every exit path —
    normal completion, a caught exception, an uncaught exception raised out
    of bookkeeping code, or cancellation — so the claim never survives past
    the fire it was reserved for. ``release()`` is itself idempotent (a
    second call is a no-op) as a defense-in-depth measure, not because any
    call site is expected to invoke it twice.
    """

    __slots__ = ("_engine", "_schedule_id", "_released")

    def __init__(self, engine: SchedulerEngine, schedule_id: str) -> None:
        self._engine = engine
        self._schedule_id = schedule_id
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._engine._release_max_runs_claim(self._schedule_id)


class _GlobalSlotClaim:
    """One-shot handle for an in-process global concurrent-fire slot.

    Returned by ``_reserve_global_slot()`` when a top-level fire is allowed
    to proceed under the daemon-wide concurrency ceiling. The holder
    (``_fire()``) must call ``release()`` exactly once, from a ``finally``
    block that covers every exit path, so the slot never survives past the
    fire it was reserved for. ``release()`` is itself idempotent (a second
    call is a no-op), same defense-in-depth rationale as ``_MaxRunsClaim``.
    """

    __slots__ = ("_engine", "_released")

    def __init__(self, engine: SchedulerEngine) -> None:
        self._engine = engine
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._engine._release_global_slot()


def _resolve_scheduler_tzinfo(tz_name: str) -> ZoneInfo:
    """Resolve the configured scheduler timezone name to a ZoneInfo.

    Falls back to UTC (with a warning) if the configured name isn't a valid
    IANA zone — an invalid config must never crash cron resolution.
    """
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        _log.warning(
            "Invalid scheduler timezone %r (LIONAGI_SCHEDULER_TZ); falling back to UTC.",
            tz_name,
        )
        return ZoneInfo("UTC")


async def _resolve_action_cwd(schedule: dict) -> str | None:
    """Resolve the working directory for a scheduled subprocess spawn.

    Layered resolution (first hit wins):
      1. ``action_project`` — the registered project's stored path, if it
         exists on disk.
      2. ``LIONAGI_SCHEDULER_CWD`` — an operator-set fallback directory.
      3. ``None`` — inherit the daemon's own launch cwd (pre-existing
         behavior); a warning is logged since `uv run li` will fail to spawn
         if that directory has no project (e.g. the daemon was started at
         ``/``).

    Imports ``lionagi.studio.services.projects`` lazily so this module (and
    ``lionagi.studio.scheduler.subprocess``) stay importable without the
    ``studio`` extra (fastapi) — the scheduler engine only actually reaches
    this branch when ``action_project`` is set, i.e. inside a running studio
    daemon where fastapi is already a hard dependency.
    """
    action_project = schedule.get("action_project")
    if action_project:
        from lionagi.studio.services.projects import get_project

        project = await get_project(action_project)
        if project:
            path = project.get("path")
            if path and Path(path).is_dir():
                return path

    env_cwd = os.environ.get("LIONAGI_SCHEDULER_CWD")
    if env_cwd and Path(env_cwd).is_dir():
        return env_cwd

    _log.warning(
        "No resolvable cwd for schedule %s (action_project=%r); the scheduled "
        "action will inherit the daemon's own working directory and may fail "
        "to spawn (`uv run li` finds no project) if that directory has none.",
        schedule.get("id"),
        action_project,
    )
    return None


class SchedulerEngine:
    def __init__(self, svc: SchedulerStateService | None = None) -> None:
        self._svc = svc if svc is not None else default_scheduler_state
        self._task: asyncio.Task | None = None
        self._running: dict[str, str] = {}  # schedule_id -> run_id
        self._stopping = False
        self._fire_tasks: set[asyncio.Task] = set()
        self._last_reaper_run: float = 0.0
        self._last_checkpoint_run: float = 0.0
        # max_runs budget reservation (single-process; see _reserve_max_runs_budget).
        self._max_runs_lock = asyncio.Lock()
        self._max_runs_inflight: dict[
            str, int
        ] = {}  # schedule_id -> claimed-not-yet-terminal count
        # global concurrent-fire cap (single-process; see _reserve_global_slot).
        self._global_slot_lock = asyncio.Lock()
        self._global_inflight = 0
        self._deferred_log_counts: dict[str, int] = {}  # schedule_id -> deferrals since last record

    async def start(self) -> None:
        _log.info("Scheduler engine starting")
        self._stopping = False
        await self._recompute_armed_cron_schedules()
        self._task = asyncio.create_task(self._tick_loop())

    async def _recompute_armed_cron_schedules(self) -> None:
        """Re-resolve every enabled cron schedule's next_fire_at under the
        current timezone interpretation before the tick loop starts.

        Guards against silently-stale fire times if LIONAGI_SCHEDULER_TZ (or
        the host's local timezone) changed since a schedule was last armed —
        the same interpretation change that PATCH and enable also trigger via
        recompute_next_fire().

        A schedule whose stored next_fire_at is already due (<= now) is left
        untouched here: it must flow through _check_missed_fires() first, so
        missed_fire_policy ("run_once" / "skip") gets a chance to run before
        anything advances next_fire_at into the future. _check_missed_fires()
        runs right after this method returns (see _tick_loop), and the
        recovery path (_recover_missed_fire_run_once() / _record_missed_
        fire_skip()) is what advances next_fire_at once the policy has been
        applied — synchronously, before _check_missed_fires() returns, so
        the _tick() call that immediately follows never observes the same
        past-due timestamp. Only schedules whose stored next_fire_at is
        still ahead of now — the timezone-migration correction case this
        hook exists for — are recomputed here.
        """
        try:
            schedules = await self._svc.list_schedules(enabled=True)
        except Exception:
            _log.exception("Failed to load schedules for startup timezone recompute")
            return
        now = time.time()
        for s in schedules:
            if s.get("trigger_type") == "cron" and not s.get("cron_expr"):
                _log.warning(
                    "Schedule %s is enabled with trigger_type='cron' but has no "
                    "cron_expr; it will never fire until re-configured",
                    s.get("id"),
                )
                continue
            if s.get("trigger_type") == "interval" and not s.get("interval_sec"):
                _log.warning(
                    "Schedule %s is enabled with trigger_type='interval' but has "
                    "no interval_sec; it will never fire until re-configured",
                    s.get("id"),
                )
                continue
            next_fire_at = s.get("next_fire_at")
            if next_fire_at is not None and next_fire_at <= now:
                continue
            try:
                await self.recompute_next_fire(s, now=now)
            except Exception:
                _log.exception(
                    "Failed to recompute next_fire_at for schedule %s on startup", s.get("id")
                )

    async def recompute_next_fire(
        self, schedule: dict, *, now: float | None = None
    ) -> float | None:
        """Recompute + persist a cron schedule's next_fire_at, logging once
        if (and only if) the value actually shifts from what was stored.

        This is the single shared code path for every situation where the
        cron interpretation may have changed under a schedule: daemon
        startup (_recompute_armed_cron_schedules), a PATCH that touches
        cron_expr/trigger fields, and the disable→enable transition (both in
        services/schedules.py). Never shifts a fire time silently — an
        unchanged recomputation is a no-op (no write, no log).
        """
        if schedule.get("trigger_type") != "cron" or not schedule.get("cron_expr"):
            return None
        ref_time = now if now is not None else time.time()
        old = schedule.get("next_fire_at")
        new = self._compute_next_fire(schedule, ref_time)
        if new is None:
            return None
        if old is not None and abs(new - old) < 1e-6:
            return new
        await self._svc.update_schedule(schedule["id"], next_fire_at=new)
        if old is not None:
            from lionagi.studio.config import SCHEDULER_TZ

            _log.info(
                "next_fire_at shifted for schedule %s (%s): %s -> %s (tz=%s)",
                schedule.get("name"),
                schedule.get("id"),
                datetime.fromtimestamp(old, tz=timezone.utc).isoformat(),
                datetime.fromtimestamp(new, tz=timezone.utc).isoformat(),
                SCHEDULER_TZ,
            )
        return new

    async def stop(self) -> None:
        _log.info("Scheduler engine stopping")
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._fire_tasks:
            for ft in list(self._fire_tasks):
                ft.cancel()
            await asyncio.gather(*self._fire_tasks, return_exceptions=True)
            self._fire_tasks.clear()

    def _tracked_fire(self, *args: Any, **kwargs: Any) -> asyncio.Task:
        """Create a tracked _fire task; prevents orphans surviving shutdown."""
        task = asyncio.create_task(self._fire(*args, **kwargs))
        self._fire_tasks.add(task)
        task.add_done_callback(self._fire_tasks.discard)
        return task

    async def fire_now(self, schedule_id: str) -> str | None:
        schedule = await self._svc.get_schedule(schedule_id)
        if not schedule:
            return None
        if await self._check_budget(schedule):
            raise ValueError(
                f"Schedule {schedule_id!r} has exhausted its budget; manual trigger refused."
            )
        allowed, claim = await self._reserve_max_runs_budget(schedule)
        if not allowed:
            raise ValueError(
                f"Schedule {schedule_id!r} has already reached its max_runs="
                f"{schedule.get('max_runs')} limit; manual trigger refused."
            )
        # A human is waiting on a manual trigger, so at-capacity is refused
        # outright rather than deferred like the automatic fire paths below.
        slot_allowed, slot_claim = await self._reserve_global_slot()
        if not slot_allowed:
            if claim is not None:
                claim.release()
            from lionagi.studio.config import MAX_SCHEDULED_CONCURRENT

            raise ValueError(
                f"Scheduler at capacity ({MAX_SCHEDULED_CONCURRENT} concurrent "
                "fires); manual trigger refused. Retry shortly."
            )
        run_id = uuid.uuid4().hex[:12]
        self._tracked_fire(
            schedule,
            run_id,
            trigger_context={"manual": True, "fired_at": time.time()},
            max_runs_claim=claim,
            global_slot_claim=slot_claim,
        )
        return run_id

    async def _tick_loop(self) -> None:
        await self._check_missed_fires()
        while not self._stopping:
            try:
                await self._tick()
            except Exception:
                _log.exception("Scheduler tick error")
            await asyncio.sleep(_TICK_INTERVAL)

    async def _check_missed_fires(self) -> None:
        try:
            schedules = await self._svc.list_schedules(enabled=True)
            now = time.time()
            for s in schedules:
                next_fire_at = s.get("next_fire_at")
                if next_fire_at is None or next_fire_at > now:
                    continue
                policy = s.get("missed_fire_policy")
                if policy == "run_once":
                    await self._recover_missed_fire_run_once(s, now)
                else:
                    await self._record_missed_fire_skip(s, now)
        except Exception:
            _log.exception("Missed fire check error")

    async def _recover_missed_fire_run_once(self, schedule: dict, now: float) -> None:
        """Queue exactly one recovery fire for a past-due run_once schedule,
        reserving its next_fire_at synchronously first.

        _tick_loop() calls _check_missed_fires() and then _tick() back to
        back with nothing awaited in between (the tick loop only sleeps
        *between* iterations, not before its first one). The recovery fire
        itself is queued as a background task via _tracked_fire() — it does
        the real work (spawns the action) and, once it runs, persists its
        own next_fire_at through the same _compute_next_fire() path every
        other fire uses. But that write may not have happened yet by the
        time the very next _tick() reloads schedules from storage: without
        a synchronous reserve here, _tick() would see the same past-due
        next_fire_at and queue a second, duplicate fire for it.

        Reserving next_fire_at here (before returning to _tick_loop) closes
        that window: _fire() recomputes and persists next_fire_at again
        once it actually runs, so this reserve only has to survive long
        enough for the immediately-following _tick() to reload schedules —
        it is a stopgap, not a duplicate of the fire path's own bookkeeping.
        If the process crashes between this reserve and the recovery fire
        landing, the recovery run is lost for this cycle, but the schedule
        is not stuck: it already holds a legitimate future next_fire_at and
        resumes firing normally next time, equivalent to one skipped run
        rather than indefinite starvation.
        """
        next_at = self._compute_next_fire(schedule, now)
        if next_at is not None:
            try:
                await self._svc.update_schedule(schedule["id"], next_fire_at=next_at)
            except Exception:
                # The reserve did not land, so storage still holds the
                # past-due next_fire_at and the immediately-following
                # _tick() will queue its own normal fire for it. Queuing a
                # recovery fire on top of that would run the external
                # action twice, so skip recovery entirely and let the
                # normal tick own this cycle's single fire (or, if storage
                # stays unavailable, a later missed-fire check retries).
                _log.exception(
                    "Failed to reserve next_fire_at ahead of missed-fire recovery for schedule %s"
                    "; skipping recovery this cycle",
                    schedule.get("id"),
                )
                return
        run_id = uuid.uuid4().hex[:12]
        _log.info(
            "Missed fire recovery for schedule %s (%s)",
            schedule["name"],
            schedule["id"],
        )
        self._tracked_fire(
            schedule,
            run_id,
            trigger_context={"missed_recovery": True, "fired_at": now},
        )

    async def _record_missed_fire_skip(self, schedule: dict, now: float) -> None:
        """Record missed-fire skip and advance next_fire_at."""
        skipped_run_id = uuid.uuid4().hex[:12]
        try:
            await create_skipped_run(
                self._svc,
                run_id=skipped_run_id,
                schedule=schedule,
                trigger_context={
                    "skipped_missed_fire": True,
                    "missed_fire_at": schedule.get("next_fire_at"),
                    "checked_at": now,
                },
                now=now,
                reason_code=ScheduleReasons.SKIPPED_MISSED_FIRE,
                reason_summary=(
                    "Schedule fire skipped because the scheduled time "
                    "passed while the server was down or the tick was "
                    "delayed (missed_fire_policy=skip)."
                ),
                metadata={
                    "missed_fire_policy": schedule.get("missed_fire_policy"),
                    "missed_fire_at": schedule.get("next_fire_at"),
                },
            )
            next_at = self._compute_next_fire(schedule, now)
            if next_at:
                await self._svc.update_schedule(schedule["id"], next_fire_at=next_at)
        except Exception:
            _log.exception(
                "Failed to record missed-fire skip for schedule %s",
                schedule.get("id"),
            )

    async def _tick(self) -> None:
        now = time.time()

        from lionagi.studio.config import REAPER_INTERVAL_SECONDS
        from lionagi.studio.services.lifecycle import run_periodic_reapers

        if now - self._last_reaper_run >= REAPER_INTERVAL_SECONDS:
            try:
                await run_periodic_reapers(now=now)
            except Exception:
                _log.exception("Periodic reaper error")
            self._last_reaper_run = now

        from lionagi.studio.config import CHECKPOINT_INTERVAL_SECONDS
        from lionagi.studio.services.db_maintenance import checkpoint_state_db

        if now - self._last_checkpoint_run >= CHECKPOINT_INTERVAL_SECONDS:
            try:
                await checkpoint_state_db(actor="scheduler_tick")
            except Exception:
                _log.exception("Periodic checkpoint error")
            self._last_checkpoint_run = now

        try:
            await self._deliver_due_dispatches(now)
        except Exception:
            _log.exception("Dispatch outbox delivery scan error")

        schedules = await self._svc.list_schedules(enabled=True)

        for s in schedules:
            try:
                if s["trigger_type"] == "github_poll":
                    await self._tick_github(s, now)
                else:
                    nfa = s.get("next_fire_at")
                    if nfa is not None and nfa <= now:
                        await self._maybe_fire(s, now)
                    elif nfa is None:
                        next_at = self._compute_next_fire(s, now)
                        if next_at:
                            await self._svc.update_schedule(s["id"], next_fire_at=next_at)
            except Exception:
                _log.exception("Error evaluating schedule %s", s.get("name"))

    async def _deliver_due_dispatches(self, now: float) -> None:
        """Scan due dispatch_outbox rows and attempt delivery (ADR-0092 slice 1).

        Unlike the reaper/checkpoint maintenance above, this is not
        interval-gated: the 30s tick itself is the latency floor the ADR
        accepts, and the due-scan's own ``next_attempt_at`` filter already
        bounds how often any single row is retried.
        """
        from lionagi.dispatch import deliver_due_dispatches
        from lionagi.state.db import StateDB

        async with StateDB() as db:
            await deliver_due_dispatches(db, now=now)

    async def _tick_github(self, schedule: dict, now: float) -> None:
        poll_interval = schedule.get("poll_interval_sec") or schedule.get("interval_sec") or 300
        last = schedule.get("last_fired_at") or 0
        if now - last < poll_interval:
            return

        if await self._check_budget(schedule):
            await self._disable_for_budget_exhausted(schedule, now)
            return

        # Reserve the global slot before polling: a filtered/no-slot poll
        # must not fetch-and-advance-cursor-then-discard.
        slot_allowed, slot_claim = await self._reserve_global_slot()
        if not slot_allowed:
            await self._maybe_record_deferred(schedule, now)
            return

        from .github import github_poll

        # Every await between reserving the slot and handing it to _fire()
        # (github_poll, the max_runs reservation, or a cancellation at either)
        # must release the slot on failure — otherwise a transient DB/count
        # error mid-poll leaks the slot permanently and eventually saturates
        # the cap until restart. A single finally covers all of them; once the
        # claims are passed to _fire() (which owns their release from then on)
        # handed_off is set so this finally leaves them alone.
        max_runs_claim: _MaxRunsClaim | None = None
        handed_off = False
        try:
            new_events = await github_poll(schedule)
            if not new_events:
                return

            allowed, max_runs_claim = await self._reserve_max_runs_budget(schedule)
            if not allowed:
                _log.info(
                    "Schedule %s (%s) has exhausted max_runs; skipping github_poll fire",
                    schedule.get("name"),
                    schedule["id"],
                )
                return
            ctx = {
                "github_events": new_events,
                "repo": schedule.get("github_repo"),
                "fired_at": now,
            }
            run_id = uuid.uuid4().hex[:12]
            handed_off = True
            await self._fire(
                schedule,
                run_id,
                trigger_context=ctx,
                max_runs_claim=max_runs_claim,
                global_slot_claim=slot_claim,
            )
        finally:
            if not handed_off:
                if slot_claim is not None:
                    slot_claim.release()
                if max_runs_claim is not None:
                    max_runs_claim.release()

    async def _reserve_max_runs_budget(self, schedule: dict) -> tuple[bool, _MaxRunsClaim | None]:
        """Atomically claim one top-level fire against schedule['max_runs'].

        Returns ``(allowed, claim)``. ``allowed`` is False only when the
        schedule is bounded (``max_runs`` set) and has already consumed its
        budget — persisted terminal runs plus runs already
        claimed-but-not-yet-terminal in this process; callers must refuse to
        fire in that case. ``claim`` is a ``_MaxRunsClaim`` token when a
        bounded schedule's budget was actually reserved, or ``None`` when
        the schedule is unbounded (``max_runs`` unset — always allowed, no
        claim to release). Guarded by an engine-wide lock so concurrent
        callers — the tick loop, fire_now(), github polling — can't both
        read the same count and both claim it before either claim is
        visible. This is the single-process analogue of a DB-backed
        compare-and-set; only one scheduler process runs today, so a
        DB-backed reservation is not needed. Chain children (chain_depth>0)
        never call this — only top-level fires consume budget.

        Whenever ``allowed`` is True the caller MUST pass ``claim`` through
        to ``_fire()`` as ``max_runs_claim=`` (even when it is ``None``) so
        it gets released — exactly once, on every exit path including
        pre-run failures — from ``_fire()``'s own ``finally`` block. Unlike
        the round-1 shape, the claim is no longer released from inside
        ``_check_max_runs()`` alone: a fire that fails before ever reaching
        ``_check_max_runs()`` (e.g. ``create_invocation`` raising) would
        otherwise leak the claim permanently for the life of the process,
        since nothing else would ever release it.

        Snapshot ordering matters here: ``inflight`` is read BEFORE the
        awaited ``count_schedule_runs()`` call, not after. ``release()`` is
        deliberately lock-free (a claim must still release from a
        cancelled/failing ``_fire()``'s ``finally`` without depending on
        this lock, which would otherwise reintroduce cancellation-unsafe
        lock-acquire-in-finally hazards), so a concurrent fire's claim can
        be released by another task while this call is suspended awaiting
        the DB. If ``inflight`` were read *after* that await (the round-2
        shape), a fire that both completes its terminal write and releases
        its claim entirely within this call's await window would vanish
        from both the persisted count (read too early, before the write)
        and the in-flight snapshot (read too late, after the release) —
        the exact gap the round-3 review's forced interleaving exploited.
        Reading ``inflight`` first captures that other fire's claim before
        it can disappear: the persisted count may still be stale, but the
        in-flight snapshot backstops it, so the sum can only ever
        over-count (spurious refusal, safe and self-correcting on the next
        tick) — never under-count (an actual overshoot).
        """
        max_runs = schedule.get("max_runs")
        if not max_runs:
            return True, None
        sid = schedule["id"]
        async with self._max_runs_lock:
            inflight = self._max_runs_inflight.get(sid, 0)
            used = await self._svc.count_schedule_runs(sid, chain_depth=0)
            if used + inflight >= max_runs:
                return False, None
            self._max_runs_inflight[sid] = inflight + 1
            return True, _MaxRunsClaim(self, sid)

    def _release_max_runs_claim(self, schedule_id: str) -> None:
        remaining = self._max_runs_inflight.get(schedule_id, 0) - 1
        if remaining > 0:
            self._max_runs_inflight[schedule_id] = remaining
        else:
            self._max_runs_inflight.pop(schedule_id, None)

    async def _reserve_global_slot(self) -> tuple[bool, _GlobalSlotClaim | None]:
        """Atomically claim one global concurrent-fire slot.

        Returns ``(allowed, claim)``, mirroring ``_reserve_max_runs_budget()``.
        ``allowed`` is False only when ``MAX_SCHEDULED_CONCURRENT`` is set
        (nonzero) and every slot is already in use; callers must defer rather
        than fire in that case. ``claim`` is a ``_GlobalSlotClaim`` token when
        a slot was actually reserved, or ``None`` when the cap is unlimited
        (0 — always allowed, no claim to release). Guarded by an engine-wide
        lock for the same reason ``_max_runs_lock`` exists: the tick loop,
        fire_now(), and github polling can all reserve concurrently. Chain
        children never call this — only top-level fires consume a slot, same
        rule as max_runs.
        """
        from lionagi.studio.config import MAX_SCHEDULED_CONCURRENT

        if MAX_SCHEDULED_CONCURRENT <= 0:
            return True, None
        async with self._global_slot_lock:
            if self._global_inflight >= MAX_SCHEDULED_CONCURRENT:
                return False, None
            self._global_inflight += 1
            return True, _GlobalSlotClaim(self)

    def _release_global_slot(self) -> None:
        self._global_inflight = max(0, self._global_inflight - 1)

    async def _maybe_record_deferred(self, schedule: dict, now: float) -> None:
        """Emit a throttled skipped-run record for a capacity-deferred fire.

        Every deferral increments a per-schedule counter; a record is only
        written on the first deferral and every _DEFERRED_RECORD_EVERY-th one
        after that, so sustained saturation doesn't spam schedule_runs.
        """
        sid = schedule["id"]
        count = self._deferred_log_counts.get(sid, 0) + 1
        self._deferred_log_counts[sid] = count
        if count % _DEFERRED_RECORD_EVERY != 1:
            return
        skipped_run_id = uuid.uuid4().hex[:12]
        await create_skipped_run(
            self._svc,
            run_id=skipped_run_id,
            schedule=schedule,
            trigger_context={"deferred_capacity": True, "fired_at": now},
            now=now,
            reason_code=ScheduleReasons.DEFERRED_CAPACITY,
            reason_summary=(
                "Schedule fire deferred: global concurrent-fire cap reached; will retry next tick."
            ),
            metadata={"deferral_count": count},
        )

    async def _check_budget(self, schedule: dict) -> bool:
        """Return True if the schedule has exhausted its configured spend budget.

        Pre-fire cumulative gate, not a mid-run interrupt: a run already in
        flight is not killed when it crosses the budget line, because its
        cost is unknown until it terminates. So a schedule may overshoot its
        budget by up to one run's cost before the next fire is refused. Pair
        with LIONAGI_STUDIO_INVOCATION_DEADLINE_SECONDS to bound a single
        run's worst-case spend.

        Unlike max_runs / the global slot this is a pure DB read with
        nothing to reserve or release -- both budget_usd and budget_tokens
        unset means unbounded (always False). Either configured bound
        tripping is sufficient to report exhausted.
        """
        budget_usd = schedule.get("budget_usd")
        budget_tokens = schedule.get("budget_tokens")
        if not budget_usd and not budget_tokens:
            return False
        spend = await self._svc.sum_schedule_spend(schedule["id"])
        if budget_usd and spend["cost_usd"] >= budget_usd:
            return True
        if budget_tokens and spend["tokens"] >= budget_tokens:
            return True
        return False

    async def _disable_for_budget_exhausted(self, schedule: dict, now: float) -> None:
        """Auto-disable a schedule that has exhausted its spend budget, recording why.

        Shared by the two tick-loop fire paths (_maybe_fire, _tick_github);
        fire_now() refuses instead of disabling (mirrors max_runs).
        """
        _log.info(
            "Schedule %s (%s) has exhausted its budget (budget_usd=%s, budget_tokens=%s); "
            "disabling instead of firing",
            schedule.get("name"),
            schedule["id"],
            schedule.get("budget_usd"),
            schedule.get("budget_tokens"),
        )
        skipped_run_id = uuid.uuid4().hex[:12]
        await create_skipped_run(
            self._svc,
            run_id=skipped_run_id,
            schedule=schedule,
            trigger_context={"budget_exhausted": True, "fired_at": now},
            now=now,
            reason_code=ScheduleReasons.BUDGET_EXHAUSTED,
            reason_summary=(
                "Schedule fire refused and the schedule disabled because its "
                "configured spend budget is exhausted."
            ),
            metadata={
                "budget_usd": schedule.get("budget_usd"),
                "budget_tokens": schedule.get("budget_tokens"),
            },
        )
        await self._svc.update_schedule(schedule["id"], enabled=0)

    async def _evaluate_threshold_breach(self, schedule: dict, now: float) -> dict[str, Any] | None:
        """Evaluate ``schedule["threshold_config"]`` against live metrics.

        Returns a breach dict (``metric``, ``op``, ``value`` = observed,
        ``threshold`` = configured, ``window_minutes``) that renders into
        ``{{metric}}``/``{{value}}``/``{{threshold}}`` action-prompt
        templates (see ``_subprocess.render_action_prompt`` and the
        github_poll precedent it already handles), or ``None`` when the
        metric is within bounds.
        """
        config = schedule.get("threshold_config")
        if not config:
            return None
        metric = config["metric"]
        op = config["op"]
        threshold_value = float(config["value"])
        window_minutes = int(config["window_minutes"])
        window_start = now - window_minutes * 60
        observed = await self._svc.metric_value(metric, window_start)
        if not _threshold.compare(op, observed, threshold_value):
            return None
        return {
            "metric": metric,
            "op": op,
            "value": observed,
            "threshold": threshold_value,
            "window_minutes": window_minutes,
        }

    async def _advance_next_fire_only(self, schedule: dict, now: float) -> None:
        """Advance next_fire_at without firing the schedule's action.

        Used by the threshold-alert paths in ``_maybe_fire`` where the
        cadence tick fires (so the metric is re-checked next time) but no
        breach (or an in-cooldown breach) means no action should spawn.
        """
        next_at = self._compute_next_fire(schedule, now)
        if next_at:
            await self._svc.update_schedule(schedule["id"], next_fire_at=next_at)

    async def _maybe_fire(self, schedule: dict, now: float) -> None:
        threshold_extra: dict[str, Any] | None = None
        if schedule.get("threshold_config"):
            breach = await self._evaluate_threshold_breach(schedule, now)
            if breach is None:
                await self._advance_next_fire_only(schedule, now)
                return
            # Cooldown: suppress refiring while still within the metric's
            # own window of the last alert, so a sustained breach doesn't
            # fire on every tick. The cadence still advances underneath —
            # the next tick re-checks the metric once the cooldown lapses.
            cooldown_sec = breach["window_minutes"] * 60
            last_alert_at = schedule.get("last_alert_at")
            if last_alert_at is not None and now - last_alert_at < cooldown_sec:
                await self._advance_next_fire_only(schedule, now)
                return
            threshold_extra = breach

        if schedule.get("overlap_policy") == "skip" and schedule["id"] in self._running:
            _log.debug("Skipping overlapping fire for %s", schedule["name"])
            skipped_run_id = uuid.uuid4().hex[:12]
            await create_skipped_run(
                self._svc,
                run_id=skipped_run_id,
                schedule=schedule,
                trigger_context={"skipped_overlap": True, "fired_at": now},
                now=now,
                reason_code=ScheduleReasons.SKIPPED_OVERLAP,
                reason_summary="Schedule fire skipped because overlap_policy=skip and a prior run is still active.",
                metadata={"overlap_policy": schedule.get("overlap_policy")},
            )
            next_at = self._compute_next_fire(schedule, now)
            if next_at:
                await self._svc.update_schedule(schedule["id"], next_fire_at=next_at)
            return

        if await self._check_budget(schedule):
            await self._disable_for_budget_exhausted(schedule, now)
            return

        allowed, claim = await self._reserve_max_runs_budget(schedule)
        if not allowed:
            _log.info(
                "Schedule %s (%s) has exhausted max_runs=%s; disabling instead of firing",
                schedule.get("name"),
                schedule["id"],
                schedule.get("max_runs"),
            )
            await self._svc.update_schedule(schedule["id"], enabled=0)
            return

        slot_allowed, slot_claim = await self._reserve_global_slot()
        if not slot_allowed:
            if claim is not None:
                # Give the max_runs reservation back -- we're deferring this
                # fire, not consuming a run against its budget.
                claim.release()
            await self._maybe_record_deferred(schedule, now)
            # Leave next_fire_at untouched (still due) so the next tick
            # retries this schedule instead of skipping it.
            return

        run_id = uuid.uuid4().hex[:12]
        ctx = {"scheduled": True, "fired_at": now, "next_fire_at": schedule.get("next_fire_at")}
        if threshold_extra:
            ctx.update(threshold_extra)
            # Stamp last_alert_at only now that every gate (overlap, budget,
            # max_runs, global slot) has actually passed and the fire is
            # really about to happen -- stamping it earlier (before those
            # checks) would consume the cooldown on a deferred/skipped tick
            # that never spawned an action, silently swallowing the alert.
            await self._svc.update_schedule(schedule["id"], last_alert_at=now)
        self._tracked_fire(
            schedule,
            run_id,
            trigger_context=ctx,
            max_runs_claim=claim,
            global_slot_claim=slot_claim,
        )

    async def _guarded_terminal_status(
        self,
        entity_type: str,
        entity_id: str,
        *,
        new_status: str,
        reason_code: str,
        reason_summary: str,
        evidence_refs: list[dict],
        source: str,
        actor: str,
        metadata: dict | None = None,
    ) -> bool:
        """Write a terminal ``schedule_run``/``invocation`` status without
        crashing (or losing follow-on side effects) when the row is already
        terminal — a concurrent writer (e.g. the deadline reaper) may have
        finalized it first. Guarded on the row still being ``running``, so a
        lost race is a checked no-op rather than a raised exception.
        """
        written = await self._svc.update_status(
            entity_type,
            entity_id,
            new_status=new_status,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=evidence_refs,
            source=source,
            actor=actor,
            metadata=metadata,
            expected_statuses={"running"},
        )
        if not written:
            _log.debug(
                "%s %s already finalized; continuing scheduler side effects",
                entity_type,
                entity_id,
            )
        return written

    async def _check_max_runs(self, schedule: dict, chain_depth: int) -> None:
        """Auto-disable a schedule once its fired top-level runs hit max_runs.

        Only chain_depth == 0 fires consume the budget — on_success/on_fail
        chain children are follow-on actions of a single top-level run, not
        additional scheduled runs, so they never count toward it. Reuses the
        existing enabled flag (the same mechanism enable/disable already use)
        rather than introducing a new schedule state.

        This no longer releases the in-process max_runs claim — that is
        _fire()'s responsibility now (via its own finally block, using the
        max_runs_claim token), so the claim is released on every exit path,
        not only the ones that reach this call.
        """
        if chain_depth != 0:
            return
        sid = schedule["id"]
        max_runs = schedule.get("max_runs")
        if not max_runs:
            return
        count = await self._svc.count_schedule_runs(sid, chain_depth=0)
        if count >= max_runs:
            _log.info(
                "Schedule %s (%s) reached max_runs=%d after %d run(s); auto-disabling",
                schedule.get("name"),
                sid,
                max_runs,
                count,
            )
            await self._svc.update_schedule(sid, enabled=0)

    async def _fire(
        self,
        schedule: dict,
        run_id: str,
        *,
        trigger_context: dict,
        chain_parent_id: str | None = None,
        chain_depth: int = 0,
        max_runs_claim: _MaxRunsClaim | None = None,
        global_slot_claim: _GlobalSlotClaim | None = None,
    ) -> None:
        """Thin wrapper around _fire_inner() that guarantees max_runs_claim
        and global_slot_claim are each released exactly once on every exit
        path.

        Only top-level callers (_maybe_fire, fire_now, _tick_github) that
        got an allowed reservation from _reserve_max_runs_budget() /
        _reserve_global_slot() pass a non-None claim; chain children never
        do. The release lives here — not inside _check_max_runs() — precisely
        so it still fires even when _fire_inner() blows up before ever
        reaching _check_max_runs() (e.g. create_invocation() raising), which
        is the leak this wrapper exists to close.
        """
        try:
            await self._fire_inner(
                schedule,
                run_id,
                trigger_context=trigger_context,
                chain_parent_id=chain_parent_id,
                chain_depth=chain_depth,
            )
        finally:
            if max_runs_claim is not None:
                max_runs_claim.release()
            if global_slot_claim is not None:
                global_slot_claim.release()

    async def _fire_inner(
        self,
        schedule: dict,
        run_id: str,
        *,
        trigger_context: dict,
        chain_parent_id: str | None = None,
        chain_depth: int = 0,
    ) -> None:
        sid = schedule["id"]
        now = time.time()

        inv_id = uuid.uuid4().hex[:12]
        # Record what was actually sent, not the raw {{var}} template: the
        # operator-facing invocation should show the substituted prompt.
        rendered_prompt = _subprocess.render_action_prompt(schedule, trigger_context)
        await self._svc.create_invocation(
            {
                "id": inv_id,
                "skill": f"scheduled:{schedule['name']}",
                "plugin": schedule["trigger_type"],
                # An explicit None check (not `or`) matters here: a template
                # can render to "" (e.g. an empty trigger_context value),
                # which build_argv sends to the child as-is. Falling back to
                # action_playbook on an empty-but-rendered prompt would
                # persist a value that differs from what was actually sent.
                "prompt": (
                    rendered_prompt
                    if rendered_prompt is not None
                    else schedule.get("action_playbook")
                ),
                "started_at": now,
                "status": "running",
            }
        )

        try:
            li_prefix, li_resolve_error = _subprocess.resolve_li_executable()
            if li_prefix is None:
                raise RuntimeError(
                    "Cannot spawn scheduled action: unable to resolve an "
                    f"absolute path to the `li` executable ({li_resolve_error})"
                )
            argv, _tmp_path = _subprocess.build_argv(
                schedule, trigger_context, executable_prefix=li_prefix
            )
        except Exception as exc:
            _log.exception("Invalid schedule action for %s (run %s)", schedule.get("name"), run_id)
            _end_time = time.time()
            next_at = self._compute_next_fire(schedule, now)
            await self._svc.create_schedule_run(
                {
                    "id": run_id,
                    "schedule_id": sid,
                    "invocation_id": inv_id,
                    "trigger_context": trigger_context,
                    "action_kind": schedule.get("action_kind"),
                    "action_args": [],
                    "status": "failed",
                    "chain_parent_id": chain_parent_id,
                    "chain_depth": chain_depth,
                    "fired_at": now,
                    "ended_at": _end_time,
                    "error_detail": str(exc),
                }
            )
            await self._svc.update_status(
                "schedule_run",
                run_id,
                new_status="failed",
                reason_code=RunReasons.FAILED_EXCEPTION,
                reason_summary=f"{type(exc).__name__}: {exc}",
                evidence_refs=[{"kind": "schedule", "id": sid}],
                source="executor",
                actor=run_id,
                metadata={"exception_class": type(exc).__name__},
            )
            inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                self._svc, inv_id, fallback_status="failed", exception=exc
            )
            await self._svc.update_invocation(inv_id, ended_at=_end_time)
            await self._guarded_terminal_status(
                "invocation",
                inv_id,
                new_status=inv_status,
                reason_code=inv_rc,
                reason_summary=inv_rs,
                evidence_refs=inv_ev,
                source="executor",
                actor=inv_id,
                metadata=inv_meta,
            )
            update_fields: dict[str, Any] = {"last_fired_at": now}
            if next_at:
                update_fields["next_fire_at"] = next_at
            await self._svc.update_schedule(sid, **update_fields)
            await self._check_max_runs(schedule, chain_depth)
            return

        # Ensure the flow_yaml tmp file is removed on any exception or
        # cancellation in the DB ops below, before spawn_and_wait() runs.
        # suppress(OSError) makes double-unlink (spawn_and_wait already cleaned up) safe.
        try:
            await self._svc.create_schedule_run(
                {
                    "id": run_id,
                    "schedule_id": sid,
                    "invocation_id": inv_id,
                    "trigger_context": trigger_context,
                    "action_kind": schedule["action_kind"],
                    "action_args": argv,
                    "status": "running",
                    "chain_parent_id": chain_parent_id,
                    "chain_depth": chain_depth,
                    "fired_at": now,
                }
            )
            await self._svc.update_status(
                "schedule_run",
                run_id,
                new_status="running",
                reason_code=ScheduleReasons.FIRED_DUE,
                reason_summary="Schedule run fired because the trigger was due.",
                evidence_refs=[{"kind": "schedule", "id": sid}],
                source="system",
                actor=sid,
                metadata={"trigger_context": trigger_context, "chain_depth": chain_depth},
            )

            if chain_depth == 0:
                self._running[sid] = run_id

            next_at = self._compute_next_fire(schedule, now)
            update_fields = {"last_fired_at": now}
            if next_at:
                update_fields["next_fire_at"] = next_at
            await self._svc.update_schedule(sid, **update_fields)

            _log.info(
                "Firing schedule %s (run %s, chain_depth=%d)", schedule["name"], run_id, chain_depth
            )

            action_cwd = await _resolve_action_cwd(schedule)
            exit_code, stderr_tail = await _subprocess.spawn_and_wait(
                argv, inv_id, tmp_path=_tmp_path, cwd=action_cwd
            )
            end_time = time.time()
            status = "completed" if exit_code == 0 else "failed"
            reason_code = (
                RunReasons.COMPLETED_OK if exit_code == 0 else RunReasons.FAILED_EXIT_NONZERO
            )
            reason_summary = (
                "Scheduled process completed successfully."
                if exit_code == 0
                else f"Scheduled process exited non-zero: {exit_code}."
            )

            await self._svc.update_schedule_run(
                run_id,
                exit_code=exit_code,
                ended_at=end_time,
                error_detail=stderr_tail if exit_code != 0 else None,
            )
            await self._guarded_terminal_status(
                "schedule_run",
                run_id,
                new_status=status,
                reason_code=reason_code,
                reason_summary=reason_summary,
                evidence_refs=[{"kind": "invocation", "id": inv_id}],
                source="executor",
                actor=run_id,
                metadata={"exit_code": exit_code},
            )
            inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                self._svc, inv_id, fallback_status=status, exit_code=exit_code
            )
            await self._svc.update_invocation(inv_id, ended_at=end_time)
            await self._guarded_terminal_status(
                "invocation",
                inv_id,
                new_status=inv_status,
                reason_code=inv_rc,
                reason_summary=inv_rs,
                evidence_refs=inv_ev,
                source="executor",
                actor=inv_id,
                metadata=inv_meta,
            )
            await self._check_max_runs(schedule, chain_depth)

            if chain_depth < _MAX_CHAIN_DEPTH:
                chain_action = None
                if exit_code == 0 and schedule.get("on_success"):
                    chain_action = schedule["on_success"]
                elif exit_code != 0 and schedule.get("on_fail"):
                    chain_action = schedule["on_fail"]

                if chain_action:
                    chain_schedule = {**schedule, **chain_action}
                    chain_schedule["action_kind"] = chain_action.get(
                        "kind", chain_action.get("action_kind", schedule["action_kind"])
                    )
                    if "model" in chain_action:
                        chain_schedule["action_model"] = chain_action["model"]
                    if "prompt" in chain_action:
                        chain_schedule["action_prompt"] = chain_action["prompt"]
                    if "agent" in chain_action:
                        chain_schedule["action_agent"] = chain_action["agent"]
                    if "playbook" in chain_action:
                        chain_schedule["action_playbook"] = chain_action["playbook"]

                    chain_ctx = {
                        **trigger_context,
                        "chain_from": run_id,
                        "parent_exit_code": exit_code,
                        "parent_status": status,
                    }
                    chain_run_id = uuid.uuid4().hex[:12]
                    await self._fire(
                        chain_schedule,
                        chain_run_id,
                        trigger_context=chain_ctx,
                        chain_parent_id=run_id,
                        chain_depth=chain_depth + 1,
                    )

        except asyncio.CancelledError:
            _log.info("Schedule fire cancelled %s (run %s)", schedule.get("name"), run_id)
            _end_time = time.time()
            try:
                await self._svc.update_schedule_run(
                    run_id,
                    ended_at=_end_time,
                    error_detail="Scheduler shutdown",
                )
                await self._guarded_terminal_status(
                    "schedule_run",
                    run_id,
                    new_status="cancelled",
                    reason_code=RunReasons.CANCELLED_SYSTEM,
                    reason_summary="Schedule run cancelled by scheduler shutdown.",
                    evidence_refs=[{"kind": "schedule", "id": sid}],
                    source="executor",
                    actor=run_id,
                )
                inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                    self._svc, inv_id, fallback_status="cancelled"
                )
                await self._svc.update_invocation(inv_id, ended_at=_end_time)
                await self._guarded_terminal_status(
                    "invocation",
                    inv_id,
                    new_status=inv_status,
                    reason_code=inv_rc,
                    reason_summary=inv_rs,
                    evidence_refs=inv_ev,
                    source="executor",
                    actor=inv_id,
                    metadata=inv_meta,
                )
                await self._check_max_runs(schedule, chain_depth)
            except Exception:
                _log.exception("Failed to record cancellation for run %s during shutdown", run_id)
            raise
        except Exception as exc:
            _log.exception("Error in schedule fire %s (run %s)", schedule.get("name"), run_id)
            _end_time = time.time()
            await self._svc.update_schedule_run(
                run_id,
                ended_at=_end_time,
                error_detail="Internal scheduler error",
            )
            await self._guarded_terminal_status(
                "schedule_run",
                run_id,
                new_status="failed",
                reason_code=RunReasons.FAILED_EXCEPTION,
                reason_summary=f"{type(exc).__name__}: {exc}",
                evidence_refs=[{"kind": "schedule", "id": sid}],
                source="executor",
                actor=run_id,
                metadata={"exception_class": type(exc).__name__},
            )
            inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                self._svc, inv_id, fallback_status="failed", exception=exc
            )
            await self._svc.update_invocation(inv_id, ended_at=_end_time)
            await self._guarded_terminal_status(
                "invocation",
                inv_id,
                new_status=inv_status,
                reason_code=inv_rc,
                reason_summary=inv_rs,
                evidence_refs=inv_ev,
                source="executor",
                actor=inv_id,
                metadata=inv_meta,
            )
            await self._check_max_runs(schedule, chain_depth)
        finally:
            if chain_depth == 0:
                self._running.pop(sid, None)
            if _tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(_tmp_path)

    def _compute_next_fire(self, schedule: dict, ref_time: float) -> float | None:
        if schedule["trigger_type"] == "cron":
            expr = schedule.get("cron_expr")
            if not expr:
                return None
            try:
                from croniter import croniter

                from lionagi.studio.config import SCHEDULER_TZ

                # Resolve the cron expression's wall-clock fields in the
                # configured timezone (default: system local), not UTC.
                # croniter honors DST transitions when given a tz-aware
                # start_time; get_next(float) still returns an absolute UTC
                # epoch, which is what next_fire_at stores.
                tz = _resolve_scheduler_tzinfo(SCHEDULER_TZ)
                start = datetime.fromtimestamp(ref_time, tz=tz)
                return croniter(expr, start_time=start).get_next(float)
            except Exception:
                _log.exception("Invalid cron expression: %s", expr)
                return None
        elif schedule["trigger_type"] == "interval":
            interval = schedule.get("interval_sec")
            if not interval:
                return None
            return ref_time + interval
        elif schedule["trigger_type"] == "github_poll":
            poll = schedule.get("poll_interval_sec") or schedule.get("interval_sec") or 300
            return ref_time + poll
        return None


scheduler = SchedulerEngine()
