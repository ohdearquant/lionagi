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

from lionagi.ln.concurrency import ExceptionGroup
from lionagi.state.db import TERMINAL_RUN_STATUSES
from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS, RunTerminalEnvelope
from lionagi.state.lifecycle.notify_settings import build_handler, resolve_notify_config
from lionagi.state.reasons import RunReasons, ScheduleReasons
from lionagi.studio.scheduler import subprocess as _subprocess
from lionagi.studio.scheduler import threshold as _threshold
from lionagi.studio.scheduler.admit import validate_rate_limit
from lionagi.studio.scheduler.signals import (
    SchedulerHandlerCancelled,
    SchedulerSignalBus,
    build_schedule_run_signal,
    record_handler_failure,
    register_default_handlers,
)
from lionagi.studio.services.scheduler_state import (
    SchedulerStateService,
    create_skipped_run,
    default_scheduler_state,
    flush_run_telemetry,
    resolve_invocation_terminal,
)

_log = logging.getLogger(__name__)

_MAX_CHAIN_DEPTH = 10
_TICK_INTERVAL = 30  # seconds
# Deferred-capacity skipped-run records are throttled to one per schedule per
# this many deferrals (the first deferral always emits), so sustained
# saturation doesn't spam schedule_runs.
_DEFERRED_RECORD_EVERY = 10


def _register_schedule_notify(
    inv_id: str, notify_on: list[str] | None, notify_command: str | None
) -> str | None:
    """Register the declared ``notify`` command on the invocation this fire
    spawns, scoped to *inv_id* and filtered to *notify_on* -- reuses the
    existing terminal-callback registry (the same machinery `li agent
    --notify` registers on its own session), never a second callback path.
    Returns the registration name to pass to ``_unregister_schedule_notify``
    in a ``finally``, or ``None`` if this schedule has no notify declared.
    """
    if not notify_on or not notify_command:
        return None
    resolved = resolve_notify_config(override=notify_command)
    if resolved is None:
        return None
    handler = build_handler(resolved)
    if handler is None:
        return None
    allowed = frozenset(notify_on)

    async def _filtered(envelope: RunTerminalEnvelope) -> None:
        if envelope.terminal_status in allowed:
            await handler(envelope)

    name = f"notify.schedule.invocation.{inv_id}"
    DEFAULT_TERMINAL_CALLBACKS.register(
        name, _filtered, kinds=["invocation"], ids=[inv_id], override=True
    )
    return name


def _unregister_schedule_notify(name: str | None) -> None:
    if name is not None:
        DEFAULT_TERMINAL_CALLBACKS.unregister(name)


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


class _RateLimitClaim:
    """One-shot reservation against a schedule's rolling-window fire cap."""

    __slots__ = ("_engine", "_schedule_id", "_token", "_released")

    def __init__(self, engine: SchedulerEngine, schedule_id: str, token: str) -> None:
        self._engine = engine
        self._schedule_id = schedule_id
        self._token = token
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._engine._release_rate_limit_claim(self._schedule_id, self._token)


class _ThresholdCooldownClaim:
    """One-shot handle for an in-process threshold-alert cooldown reservation.

    ``_maybe_fire()`` reserves a schedule's cooldown SYNCHRONOUSLY -- adding
    its id to ``_threshold_pending`` with no ``await`` between the
    ``last_alert_at`` gate check and the add -- before ``_tracked_fire()``
    launches. Without this in-process gate, two ticks separated by
    ``_TICK_INTERVAL`` could both read the same stale (not-yet-durably-
    stamped) ``last_alert_at``, both pass the cooldown check, and both fire
    before either fire's background task reaches the durable stamp --
    duplicate alerts inside the cooldown window, the exact dedup this
    feature promises to prevent.

    Held for the full ``_fire_inner()`` duration and released in ``_fire()``
    's wrapping ``try/finally`` -- same lifecycle as ``_MaxRunsClaim``/
    ``_GlobalSlotClaim`` -- so it is guaranteed to be released on every exit
    path, including a failure before the durable stamp is ever written. A
    leaked reservation would permanently mute the alert, which is worse
    than the duplicate it exists to prevent.
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
        self._engine._threshold_pending.discard(self._schedule_id)


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


async def _resolve_action_cwd(schedule: dict) -> tuple[str | None, str | None]:
    """Resolve the working directory for a scheduled subprocess spawn.

    Layered resolution (first hit wins):
      1. ``action_cwd`` — the schedule's own persisted execution root
         (ADR-0070 delta 1), snapshotted once at creation time, if it still
         exists on disk.
      2. ``action_project`` — the registered project's stored path, if it
         exists on disk.
      3. ``LIONAGI_SCHEDULER_CWD`` — an operator-set fallback directory.
      4. ``None`` — inherit the daemon's own launch cwd. Only pre-migration
         rows (``action_cwd`` never set) reach this tier; a loud deprecation
         warning is logged since `uv run li` will fail to spawn if that
         directory has no project (e.g. the daemon was started at ``/``).

    Returns ``(cwd, missing_path)``. ``missing_path`` is set only when a
    stored path (``action_cwd`` or ``action_project``'s registered path) no
    longer exists on disk (e.g. a pruned worktree) and nothing else resolved
    either -- i.e. exactly the case where the eventual inherit-daemon-cwd
    fallback risks a deep, opaque ``FileNotFoundError`` from the spawned
    process. The caller uses it to attribute a subsequent non-zero exit to a
    stale cwd via a specific status_reason instead of the generic "process
    exited non-zero".

    Imports ``lionagi.studio.services.projects`` lazily so this module (and
    ``lionagi.studio.scheduler.subprocess``) stay importable without the
    ``studio`` extra (fastapi) — the scheduler engine only actually reaches
    this branch when ``action_project`` is set, i.e. inside a running studio
    daemon where fastapi is already a hard dependency.
    """
    stale_path: str | None = None

    action_cwd = schedule.get("action_cwd")
    if action_cwd:
        if Path(action_cwd).is_dir():
            return action_cwd, None
        stale_path = action_cwd
        _log.warning(
            "Schedule %s: persisted execution root %r no longer exists on "
            "disk (e.g. a pruned worktree); falling back instead of "
            "spawning into a missing directory.",
            schedule.get("id"),
            action_cwd,
        )

    action_project = schedule.get("action_project")
    if action_project:
        from lionagi.studio.services.projects import get_project

        project = await get_project(action_project)
        if project:
            path = project.get("path")
            if path:
                if Path(path).is_dir():
                    return path, None
                stale_path = stale_path or path
                _log.warning(
                    "Schedule %s: action_project %r is registered at %r, but "
                    "that path no longer exists on disk (e.g. a pruned "
                    "worktree); falling back instead of spawning into a "
                    "missing directory.",
                    schedule.get("id"),
                    action_project,
                    path,
                )

    env_cwd = os.environ.get("LIONAGI_SCHEDULER_CWD")
    if env_cwd and Path(env_cwd).is_dir():
        return env_cwd, None

    if action_cwd is None:
        _log.warning(
            "Schedule %s has no persisted execution root (action_cwd) -- a "
            "pre-migration row -- and no action_project or "
            "LIONAGI_SCHEDULER_CWD resolved either; the scheduled action "
            "will inherit the daemon's own working directory and may fail "
            "to spawn (`uv run li` finds no project) if that directory has "
            "none. DEPRECATED: this schedule should be backfilled (restart "
            "the daemon) or updated with an explicit execution root.",
            schedule.get("id"),
        )
    else:
        _log.warning(
            "No resolvable cwd for schedule %s (action_project=%r); the scheduled "
            "action will inherit the daemon's own working directory and may fail "
            "to spawn (`uv run li` finds no project) if that directory has none.",
            schedule.get("id"),
            action_project,
        )
    return None, stale_path


class SchedulerEngine:
    def __init__(
        self,
        svc: SchedulerStateService | None = None,
        signal_bus: SchedulerSignalBus | None = None,
    ) -> None:
        self._svc = svc if svc is not None else default_scheduler_state
        self._signal_bus = signal_bus if signal_bus is not None else SchedulerSignalBus()
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
        # Rolling-window reservations bridge the admission-read -> terminal-row
        # window so concurrent tick/manual/github paths cannot all observe the
        # same persisted count and overshoot max_fires.
        self._rate_limit_lock = asyncio.Lock()
        self._rate_limit_inflight: dict[str, dict[str, float]] = {}
        # global concurrent-fire cap (single-process; see _reserve_global_slot).
        self._global_slot_lock = asyncio.Lock()
        self._global_inflight = 0
        self._deferred_log_counts: dict[str, int] = {}  # schedule_id -> deferrals since last record
        # threshold-alert cooldown reservations (single-process; see
        # _ThresholdCooldownClaim). Membership means "a fire for this
        # schedule's current breach is in flight or was just reserved" --
        # closes the race a DB-only last_alert_at check can't (see
        # _maybe_fire).
        self._threshold_pending: set[str] = set()
        # ADR-0071 D4: this daemon process is the one host worker (v1).
        self._task_worker_id = f"host:{uuid.uuid4().hex[:8]}"

    async def start(self) -> None:
        _log.info("Scheduler engine starting")
        self._stopping = False
        await self._backfill_action_cwd()
        await self._recompute_armed_cron_schedules()
        self._task = asyncio.create_task(self._tick_loop())

    async def _backfill_action_cwd(self) -> None:
        """One-shot startup backfill: give pre-migration schedules a persisted execution root.

        ADR-0070 delta 1. ``action_cwd`` is additive and nullable (see
        ``MIGRATION_COLUMNS``), so rows created before this feature shipped
        have it unset. For any such row whose ``action_project`` resolves to
        a directory that still exists on disk, snapshot that path into
        ``action_cwd`` -- the same derivation `create_schedule()` performs
        for newly created schedules. A row with no resolvable
        ``action_project`` is left with ``action_cwd`` unset; it keeps using
        the pre-existing ``LIONAGI_SCHEDULER_CWD`` / daemon-cwd-inherit
        fallback in ``_resolve_action_cwd()`` until it is explicitly updated.

        Idempotent: only rows where ``action_cwd`` is still ``None`` are
        touched, so re-running this on every daemon startup is a no-op once
        every backfillable row has been filled in.
        """
        try:
            schedules = await self._svc.list_schedules()
        except Exception:
            _log.exception("Failed to load schedules for startup action_cwd backfill")
            return
        for s in schedules:
            if s.get("action_cwd") or not s.get("action_project"):
                continue
            try:
                from lionagi.studio.services.projects import get_project

                project = await get_project(s["action_project"])
                path = project.get("path") if project else None
                if path and Path(path).is_dir():
                    await self._svc.update_schedule(s["id"], action_cwd=path)
                    _log.info(
                        "Backfilled execution root for schedule %s from action_project %r: %s",
                        s.get("id"),
                        s["action_project"],
                        path,
                    )
            except Exception:
                _log.exception("Failed to backfill action_cwd for schedule %s", s.get("id"))

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

        This method never fires anything itself, so it has no
        occurrence-insert to reconcile against schedule_runs -- that
        consultation happens in _check_missed_fires() for any due schedule.
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
        rate_claim: _RateLimitClaim | None = None
        claim: _MaxRunsClaim | None = None
        slot_claim: _GlobalSlotClaim | None = None
        handed_off = False
        now = time.time()
        try:
            rate_allowed, rate_claim = await self._reserve_rate_limit(schedule, now=now)
            if not rate_allowed:
                raise ValueError(
                    f"Schedule {schedule_id!r} has reached its rolling rate limit; "
                    "manual trigger refused. Retry after the configured window advances."
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
                from lionagi.studio.config import MAX_SCHEDULED_CONCURRENT

                raise ValueError(
                    f"Scheduler at capacity ({MAX_SCHEDULED_CONCURRENT} concurrent "
                    "fires); manual trigger refused. Retry shortly."
                )
            run_id = uuid.uuid4().hex[:12]
            self._tracked_fire(
                schedule,
                run_id,
                trigger_context={"manual": True, "fired_at": now},
                rate_limit_claim=rate_claim,
                max_runs_claim=claim,
                global_slot_claim=slot_claim,
            )
            handed_off = True
            return run_id
        finally:
            if not handed_off:
                if rate_claim is not None:
                    rate_claim.release()
                if claim is not None:
                    claim.release()
                if slot_claim is not None:
                    slot_claim.release()

    async def _tick_loop(self) -> None:
        await self._recover_undispatched_fires()
        await self._check_missed_fires()
        while not self._stopping:
            try:
                await self._tick()
            except Exception:
                _log.exception("Scheduler tick error")
            await asyncio.sleep(_TICK_INTERVAL)

    async def _mark_dispatched(self, run_id: str) -> None:
        """Stamp ``dispatched_at`` the instant spawn_and_wait confirms the
        external process exists -- see _fire_inner()'s delivery-contract
        docstring for what this closes."""
        await self._svc.update_schedule_run(run_id, dispatched_at=time.time())

    async def _recover_undispatched_fires(self) -> None:
        """Startup-only scan for occurrences whose transaction committed but
        whose launch was never confirmed (see _fire_inner()'s delivery
        contract). Chain children and orphans of a missing/disabled schedule
        are tombstoned directly (no replacement to race against); everything
        else is re-fired via ``_tracked_fire(..., supersedes_run_id=...)``,
        which tombstones the orphan and inserts the replacement atomically.
        """
        try:
            orphans = await self._svc.list_undispatched_schedule_runs()
        except Exception:
            _log.exception("Failed to scan for undispatched schedule_runs")
            return

        for row in orphans:
            run_id = row["id"]
            sid = row.get("schedule_id")

            if row.get("chain_depth", 0) != 0:
                await self._tombstone_orphan_only(
                    run_id, sid=sid, log_note="chain-child, not auto-retried"
                )
                continue

            schedule = await self._svc.get_schedule(sid) if sid else None
            if schedule is None or not schedule.get("enabled"):
                await self._tombstone_orphan_only(
                    run_id,
                    sid=sid,
                    log_note=f"owning schedule {sid} missing or disabled, not auto-retried",
                )
                continue

            new_run_id = uuid.uuid4().hex[:12]
            _log.info(
                "Re-firing undispatched schedule_run %s as %s for schedule %s",
                run_id,
                new_run_id,
                sid,
            )
            self._tracked_fire(
                schedule,
                new_run_id,
                trigger_context=row.get("trigger_context") or {},
                supersedes_run_id=run_id,
            )

    async def _tombstone_orphan_only(self, run_id: str, *, sid: str | None, log_note: str) -> None:
        """CAS-tombstone an undispatched orphan with no replacement to
        follow (chain child, or owning schedule missing/disabled)."""
        try:
            written = await self._svc.update_status(
                "schedule_run",
                run_id,
                new_status="failed",
                reason_code=RunReasons.FAILED_NEVER_DISPATCHED,
                reason_summary=(
                    "Scheduler crashed after committing this occurrence but "
                    "before confirming the external process launched."
                ),
                evidence_refs=[{"kind": "schedule", "id": sid}] if sid else [],
                source="system",
                actor="scheduler_startup_recovery",
                expected_statuses={"running"},
            )
        except Exception:
            _log.exception("Failed to tombstone undispatched schedule_run %s", run_id)
            return
        if written:
            _log.info("Undispatched schedule_run %s tombstoned: %s", run_id, log_note)
        else:
            # Raced with something else finalizing this row between the
            # scan and here (e.g. the stale-run reaper); nothing left to
            # recover -- it already resolved through some other path.
            pass

    async def _check_missed_fires(self) -> None:
        try:
            schedules = await self._svc.list_schedules(enabled=True)
            now = time.time()
            for s in schedules:
                next_fire_at = s.get("next_fire_at")
                if next_fire_at is None or next_fire_at > now:
                    continue
                # Recovery scan before recompute: with occurrence-insert +
                # cursor-advance atomic (create_schedule_run_and_advance),
                # a schedule_run row can only exist here for one of two
                # reasons -- (a) the atomic transaction committed and then
                # the process died before spawn_and_wait/its terminal write
                # (next_fire_at should already be in the future in that
                # case, so this branch shouldn't normally see it due at
                # all), or (b) a pre-existing row from before this fix
                # shipped, or some other write path, left an occurrence
                # recorded without the cursor having moved. Either way,
                # firing again here would double-execute the external
                # action for an occurrence that already has a durable row
                # -- so treat "already recorded" as evidence the slot was
                # handled and just advance the cursor past it, the same
                # bookkeeping _record_missed_fire_skip does, without
                # queuing a second fire.
                if await self._svc.schedule_run_exists_since(s["id"], next_fire_at):
                    next_at = self._compute_next_fire(s, now)
                    fields = self._next_fire_field(s, next_at)
                    if fields:
                        try:
                            await self._svc.update_schedule(s["id"], **fields)
                        except Exception:
                            _log.exception(
                                "Failed to advance next_fire_at past an already-recorded "
                                "occurrence for schedule %s",
                                s.get("id"),
                            )
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
        reserving its admission claims and next_fire_at synchronously first.

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
        rather than indefinite starvation. For an 'at' trigger the reserve
        clears next_fire_at, so that same crash window loses its single run
        permanently -- accepted for now over the alternative (fire before
        reserve), which reopens the duplicate-fire window; the max-runs
        claim gate remains the second defense against duplicates.
        """
        # Admission claims FIRST (same sequence as a normal tick fire --
        # without the max_runs reservation, a concurrent fire_now() or
        # re-apply racing this queued recovery could observe zero durable
        # runs, take the sole claim, and admit a second execution), and only
        # THEN the next_fire_at reserve: a rate/slot refusal must leave the
        # row untouched and still due for a later cycle -- clearing an 'at'
        # trigger's next_fire_at before a refusal would strand its single
        # run permanently.
        rate_claim: _RateLimitClaim | None = None
        claim: _MaxRunsClaim | None = None
        slot_claim: _GlobalSlotClaim | None = None
        handed_off = False
        try:
            rate_allowed, rate_claim = await self._reserve_rate_limit(schedule, now=now)
            if not rate_allowed:
                return
            allowed, claim = await self._reserve_max_runs_budget(schedule)
            if not allowed:
                await self._svc.update_schedule(schedule["id"], enabled=0)
                return
            slot_allowed, slot_claim = await self._reserve_global_slot()
            if not slot_allowed:
                return

            next_at = self._compute_next_fire(schedule, now)
            # _next_fire_field, not a bare not-None check: an 'at' trigger's
            # terminal None must be reserved too (persisted as a cleared
            # next_fire_at), or the immediately-following _tick() still sees
            # the past-due instant and queues a duplicate fire.
            fields = self._next_fire_field(schedule, next_at)
            if fields:
                try:
                    await self._svc.update_schedule(schedule["id"], **fields)
                except Exception:
                    # The reserve did not land, so storage still holds the
                    # past-due next_fire_at and the immediately-following
                    # _tick() will queue its own normal fire for it. Queuing
                    # a recovery fire on top of that would run the external
                    # action twice, so skip recovery entirely (releasing the
                    # claims below) and let the normal tick own this cycle's
                    # single fire (or, if storage stays unavailable, a later
                    # missed-fire check retries).
                    _log.exception(
                        "Failed to reserve next_fire_at ahead of missed-fire recovery for "
                        "schedule %s; skipping recovery this cycle",
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
                rate_limit_claim=rate_claim,
                max_runs_claim=claim,
                global_slot_claim=slot_claim,
            )
            handed_off = True
        finally:
            if not handed_off:
                if rate_claim is not None:
                    rate_claim.release()
                if claim is not None:
                    claim.release()
                if slot_claim is not None:
                    slot_claim.release()

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
            fields = self._next_fire_field(schedule, next_at)
            if fields:
                await self._svc.update_schedule(schedule["id"], **fields)
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

        try:
            await self._run_task_worker_tick(now)
        except Exception:
            _log.exception("Task worker tick error")

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
        """Scan due dispatch_outbox rows and attempt delivery (ADR-0059 slice 1).

        Unlike the reaper/checkpoint maintenance above, this is not
        interval-gated: the 30s tick itself is the latency floor the ADR
        accepts, and the due-scan's own ``next_attempt_at`` filter already
        bounds how often any single row is retried.
        """
        from lionagi.dispatch import deliver_due_dispatches
        from lionagi.state.db import StateDB

        async with StateDB() as db:
            await deliver_due_dispatches(db, now=now)

    async def _run_task_worker_tick(self, now: float) -> None:
        """ADR-0071 D4: reap lapsed leases and claim/execute eligible host
        task applications. Not interval-gated for the same reason as
        ``_deliver_due_dispatches`` — the 30s tick is the latency floor.
        """
        from lionagi.state.db import StateDB
        from lionagi.studio.scheduler import worker as _worker

        if not _worker.TASK_WORKER_ENABLED:
            return
        async with StateDB() as db:
            await _worker.worker_tick(db, worker_id=self._task_worker_id, now=now)

    async def _tick_github(self, schedule: dict, now: float) -> None:
        poll_interval = schedule.get("poll_interval_sec") or schedule.get("interval_sec") or 300
        last = schedule.get("last_fired_at") or 0
        if now - last < poll_interval:
            return

        if await self._check_budget(schedule):
            await self._disable_for_budget_exhausted(schedule, now)
            return

        rate_allowed, pre_rate_claim = await self._reserve_rate_limit(schedule, now=now)
        if not rate_allowed:
            _log.info(
                "Schedule %s (%s) reached rolling rate limit %s; "
                "github events deferred without polling or disabling",
                schedule.get("name"),
                schedule["id"],
                schedule.get("rate_limit"),
            )
            return

        # Reserve one global slot before polling: a filtered/no-slot poll
        # must not fetch-and-advance-cursor-then-discard. This first slot is
        # handed to whichever event ends up firing first below; any further
        # dispatched events in the same poll reserve their own slot.
        slot_allowed, pre_slot_claim = await self._reserve_global_slot()
        if not slot_allowed:
            if pre_rate_claim is not None:
                pre_rate_claim.release()
            await self._maybe_record_deferred(schedule, now)
            return

        from .github import github_poll

        sid = schedule["id"]
        # Every await between reserving pre_slot_claim and handing it off to
        # the first dispatched _fire() (github_poll, that event's max_runs
        # reservation, or a cancellation at either) must release it on
        # failure — otherwise a transient DB/count error mid-poll leaks the
        # slot permanently. pre_slot_claim is nulled out the moment it is
        # either handed to _fire() (which owns its release from then on) or
        # released inline (e.g. a max_runs refusal on the first event), so
        # this finally only ever fires for the untouched case.
        try:
            poll_result = await github_poll(schedule)
            polled = poll_result.items
            if not poll_result.scan_complete:
                _log.info(
                    "Schedule %s (%s): merged-PR scan truncated this poll "
                    "(page cap reached or a pagination fetch error) -- "
                    "event(s) too close to the unproven boundary are held "
                    "back for a later poll",
                    schedule.get("name"),
                    sid,
                )

            # Observer self-health: stamp the schedule's health columns from
            # this poll's outcome regardless of whether it returned items --
            # a healthy-empty poll ("ok") must reset the blind clock exactly
            # like a poll that found PRs, so a quiet repo never false-alarms
            # on github_poll_healthy_age_minutes. "error" (network failure,
            # missing/invalid repo, no token available) leaves both columns
            # untouched -- the age metric climbs on its own since
            # last_healthy_poll_at doesn't move.
            if poll_result.poll_status == "ok":
                await self._svc.update_schedule(
                    sid, last_healthy_poll_at=now, poller_consecutive_401=0
                )
            elif poll_result.poll_status == "auth_error":
                await self._svc.update_schedule(
                    sid,
                    poller_consecutive_401=(schedule.get("poller_consecutive_401") or 0) + 1,
                )

            if not polled:
                return

            cursor = schedule.get("github_cursor")
            drop_reason: str | None = None
            dropped_prs: list[Any] = []

            for idx, item in enumerate(polled):
                if not item.dispatchable:
                    # Filtered-out PRs (e.g. drafts under a non-draft filter)
                    # consume no budget; the cursor can always advance past
                    # them so they aren't re-listed forever.
                    cursor = item.updated_at
                    continue

                rate_claim: _RateLimitClaim | None = None
                max_runs_claim: _MaxRunsClaim | None = None
                slot_claim: _GlobalSlotClaim | None = None
                admission_handed_off = False
                try:
                    if pre_rate_claim is not None:
                        rate_claim, pre_rate_claim = pre_rate_claim, None
                    else:
                        rate_allowed, rate_claim = await self._reserve_rate_limit(schedule, now=now)
                        if not rate_allowed:
                            drop_reason = f"rolling rate limit {schedule.get('rate_limit')} reached"
                            dropped_prs = [
                                e.event.get("pr_number") for e in polled[idx:] if e.dispatchable
                            ]
                            break

                    if pre_slot_claim is not None:
                        slot_claim, pre_slot_claim = pre_slot_claim, None
                    else:
                        slot_allowed, slot_claim = await self._reserve_global_slot()
                        if not slot_allowed:
                            drop_reason = "global concurrent-fire cap reached"
                            dropped_prs = [
                                e.event.get("pr_number") for e in polled[idx:] if e.dispatchable
                            ]
                            break

                    allowed, max_runs_claim = await self._reserve_max_runs_budget(schedule)
                    if not allowed:
                        drop_reason = f"max_runs={schedule.get('max_runs')} exhausted"
                        dropped_prs = [
                            e.event.get("pr_number") for e in polled[idx:] if e.dispatchable
                        ]
                        break

                    ctx = {
                        "github_events": [item.event],
                        "repo": schedule.get("github_repo"),
                        "fired_at": now,
                    }
                    run_id = uuid.uuid4().hex[:12]
                    admission_handed_off = True
                    await self._fire(
                        schedule,
                        run_id,
                        trigger_context=ctx,
                        rate_limit_claim=rate_claim,
                        max_runs_claim=max_runs_claim,
                        global_slot_claim=slot_claim,
                        # Advances github_cursor to this event's own
                        # updated_at INSIDE the same atomic transaction as
                        # this event's occurrence insert (_fire_inner ->
                        # create_schedule_run_and_advance), durably before
                        # spawn_and_wait() runs the actual action. This is
                        # what closes the double-fire hazard: batching the
                        # cursor write until after the whole loop (the old
                        # shape, still mirrored below for trailing
                        # non-dispatched items) left a window where 1..N
                        # events could be fully fired and executed while the
                        # persisted cursor still pointed before all of them,
                        # so a crash mid-poll made the next poll re-fetch
                        # and re-fire every already-executed event.
                        extra_schedule_fields={"github_cursor": item.updated_at},
                    )
                    # Track locally too, for the batched trailing-write
                    # safety net below (covers only non-dispatched/filtered
                    # items after the last fire, or an all-filtered poll
                    # with no fire at all -- harmless/idempotent to re-write
                    # the same value this event's own fire already
                    # persisted).
                    cursor = item.updated_at
                finally:
                    if not admission_handed_off:
                        if rate_claim is not None:
                            rate_claim.release()
                        if max_runs_claim is not None:
                            max_runs_claim.release()
                        if slot_claim is not None:
                            slot_claim.release()

            if drop_reason and dropped_prs:
                _log.info(
                    "Schedule %s (%s): %d github event(s) not dispatched this "
                    "poll (%s); PR(s) %s deferred to the next poll",
                    schedule.get("name"),
                    sid,
                    len(dropped_prs),
                    drop_reason,
                    dropped_prs,
                )

            # Safety-net batched write: every DISPATCHED event already
            # advanced github_cursor atomically with its own occurrence
            # insert above. This only still does work when the loop ends
            # on non-dispatched/filtered items (dispatchable=False, cursor
            # advances past them with no fire) or when nothing was fired
            # at all -- both no-occurrence cases with nothing to be
            # atomic with. For a dispatched item it re-writes the same
            # value already committed, a harmless no-op.
            if cursor != schedule.get("github_cursor"):
                await self._svc.update_schedule(sid, github_cursor=cursor)
        finally:
            if pre_rate_claim is not None:
                pre_rate_claim.release()
            if pre_slot_claim is not None:
                pre_slot_claim.release()

    async def _reserve_max_runs_budget(self, schedule: dict) -> tuple[bool, _MaxRunsClaim | None]:
        """Atomically claim one top-level fire against schedule['max_runs'].

        Returns ``(allowed, claim)``. ``allowed`` is False only when the
        schedule is bounded (``max_runs`` set) and has already consumed its
        budget — persisted fired rows (running or resolved) plus fires
        claimed in this process whose occurrence rows have not yet
        committed; callers must refuse to fire in that case. ``claim`` is a ``_MaxRunsClaim`` token when a
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
        an earlier implementation, the claim is no longer released from inside
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
        the DB. If ``inflight`` were read *after* that await (an intermediate
        design), a fire that both persists its occurrence row and releases
        its claim entirely within this call's await window would vanish
        from both the persisted count (read too early, before the write)
        and the in-flight snapshot (read too late, after the release) —
        the exact gap that adversarial concurrency testing exploited.
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
            # A fired run consumes budget the moment it fires, not when it
            # resolves — so persisted 'running' rows count alongside terminal
            # ones, or a bounded schedule under overlap_policy=allow admits
            # fires past its budget while a long action is still executing.
            # Claims and rows are disjoint representations of a fire: a claim
            # covers only the window before the occurrence row commits, and
            # _fire_inner() releases it the moment _write_occurrence()
            # succeeds (the same ownership transfer the rate-limit claim
            # does), so summing the two counts each fire exactly once. In
            # the transfer instant a fire can briefly appear as both — that
            # overlap only ever over-counts (a spurious refusal, corrected
            # on the next tick), the safe direction. A restart-orphaned
            # 'running' row whose claim died with the process, and a fresh
            # claim-only admission, are DIFFERENT fires and both count —
            # taking a max() of the two views instead of their sum would
            # collapse them and admit past the cap.
            fired = await self._svc.count_schedule_runs(
                sid,
                chain_depth=0,
                statuses=("running", *TERMINAL_RUN_STATUSES),
            )
            if fired + inflight >= max_runs:
                return False, None
            self._max_runs_inflight[sid] = inflight + 1
            return True, _MaxRunsClaim(self, sid)

    def _release_max_runs_claim(self, schedule_id: str) -> None:
        remaining = self._max_runs_inflight.get(schedule_id, 0) - 1
        if remaining > 0:
            self._max_runs_inflight[schedule_id] = remaining
        else:
            self._max_runs_inflight.pop(schedule_id, None)

    async def _reserve_rate_limit(
        self, schedule: dict, *, now: float
    ) -> tuple[bool, _RateLimitClaim | None]:
        """Reserve one fire inside the schedule's rolling time window.

        Persisted top-level rows that reached ``running`` or a terminal state
        provide the durable count. In-process claims cover admitted fires until
        their occurrence row commits, closing the concurrent-admission and
        process-restart gaps. Exhaustion is a temporary refusal: automatic
        callers leave the schedule enabled and its due cursor untouched so a
        later tick retries after the window rolls forward.
        """
        config = validate_rate_limit(schedule.get("rate_limit"))
        if config is None:
            return True, None
        max_fires, window_sec = config
        sid = schedule["id"]
        cutoff = now - window_sec
        async with self._rate_limit_lock:
            reservations = self._rate_limit_inflight.get(sid, {})
            active = {
                token: reserved_at
                for token, reserved_at in reservations.items()
                if reserved_at >= cutoff
            }
            if active:
                self._rate_limit_inflight[sid] = active
            else:
                self._rate_limit_inflight.pop(sid, None)
            inflight = len(active)
            used = await self._svc.count_schedule_runs(
                sid,
                chain_depth=0,
                statuses=("running", *TERMINAL_RUN_STATUSES),
                fired_after=cutoff,
            )
            if used + inflight >= max_fires:
                return False, None
            token = uuid.uuid4().hex
            active[token] = now
            self._rate_limit_inflight[sid] = active
            return True, _RateLimitClaim(self, sid, token)

    def _release_rate_limit_claim(self, schedule_id: str, token: str) -> None:
        reservations = self._rate_limit_inflight.get(schedule_id)
        if reservations is None:
            return
        reservations.pop(token, None)
        if not reservations:
            self._rate_limit_inflight.pop(schedule_id, None)

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
        threshold_claim: _ThresholdCooldownClaim | None = None
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
            sid = schedule["id"]
            last_alert_at = schedule.get("last_alert_at")
            in_cooldown = last_alert_at is not None and now - last_alert_at < cooldown_sec
            # in_pending closes the race last_alert_at alone can't: a fire
            # reserved by an earlier tick whose durable stamp hasn't landed
            # yet still reads as "not in cooldown" from the DB alone. This
            # check and the reservation immediately below it are both
            # synchronous -- no await in between -- so a second tick can't
            # slip in between the gate and the reservation becoming visible.
            if in_cooldown or sid in self._threshold_pending:
                await self._advance_next_fire_only(schedule, now)
                return
            self._threshold_pending.add(sid)
            threshold_claim = _ThresholdCooldownClaim(self, sid)
            threshold_extra = breach

        # Every await from here through _tracked_fire() (create_skipped_run,
        # _check_budget, _reserve_max_runs_budget, _reserve_global_slot, or
        # a cancellation at any of them) must release threshold_claim (and,
        # once reserved, claim/slot_claim) on failure -- a raise mid-gate
        # would otherwise leak the reservation permanently, muting the
        # alert until an engine restart. handed_off flips True only once
        # _tracked_fire() has actually launched and taken ownership of the
        # claims, mirroring _tick_github's use of the same pattern for its
        # own claims below.
        rate_claim: _RateLimitClaim | None = None
        claim: _MaxRunsClaim | None = None
        slot_claim: _GlobalSlotClaim | None = None
        handed_off = False
        try:
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
                fields = self._next_fire_field(schedule, next_at)
                if fields:
                    await self._svc.update_schedule(schedule["id"], **fields)
                return

            if await self._check_budget(schedule):
                await self._disable_for_budget_exhausted(schedule, now)
                return

            rate_allowed, rate_claim = await self._reserve_rate_limit(schedule, now=now)
            if not rate_allowed:
                _log.info(
                    "Schedule %s (%s) reached rolling rate limit %s; "
                    "deferring without disabling or advancing next_fire_at",
                    schedule.get("name"),
                    schedule["id"],
                    schedule.get("rate_limit"),
                )
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
                await self._maybe_record_deferred(schedule, now)
                # Leave next_fire_at untouched (still due) so the next tick
                # retries this schedule instead of skipping it. claim (and
                # threshold_claim, if reserved) are given back by the
                # finally below -- we're deferring this fire, not
                # consuming a run against its budget or abandoning the
                # cooldown.
                return

            run_id = uuid.uuid4().hex[:12]
            ctx = {
                "scheduled": True,
                "fired_at": now,
                "next_fire_at": schedule.get("next_fire_at"),
            }
            if threshold_extra:
                ctx.update(threshold_extra)
                # last_alert_at is NOT stamped here. Every gate above
                # (overlap, budget, max_runs, global slot) has passed, but
                # _fire_inner() can still fail before persisting any
                # schedule_run row (e.g. create_invocation() raising) --
                # stamping this early would consume the cooldown with zero
                # durable record an alert was ever attempted, the exact
                # silent-loss shape this feature exists to prevent. See
                # _fire_inner's own stamp, which only fires once a
                # schedule_run row actually exists. The in-process
                # threshold_claim (released in _fire()'s finally once
                # handed off) is what actually closes the duplicate-fire
                # race in the meantime.
            self._tracked_fire(
                schedule,
                run_id,
                trigger_context=ctx,
                rate_limit_claim=rate_claim,
                max_runs_claim=claim,
                global_slot_claim=slot_claim,
                threshold_cooldown_claim=threshold_claim,
            )
            # Flipped only after _tracked_fire() returns, so even a
            # synchronous task-launch failure releases the claims below.
            # Release is idempotent, so no double-free against _fire()'s
            # own finally once the task is running.
            handed_off = True
        finally:
            if not handed_off:
                if rate_claim is not None:
                    rate_claim.release()
                if claim is not None:
                    claim.release()
                if slot_claim is not None:
                    slot_claim.release()
                if threshold_claim is not None:
                    threshold_claim.release()

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

    async def _dispatch_signal(self, signal: Any) -> None:
        """Emit *signal* on the scheduler's signal bus.

        The schedule_run/invocation row this signal describes is already
        committed by the time this runs (mint happens after
        ``_guarded_terminal_status`` returns ``True``), so a handler
        exception here must never be allowed to look like it undid that
        write or to stop the tick loop from continuing on to the next
        schedule. ``SchedulerSignalBus.emit`` fails loud (raises an
        ``ExceptionGroup`` or ``SchedulerHandlerCancelled``, never swallows);
        this call site records handler failures while preserving a genuine
        cancellation request against the scheduler task.
        """
        try:
            await self._signal_bus.emit(signal)
        except ExceptionGroup as eg:
            _log.error("Scheduler signal handler(s) failed for %s: %s", type(signal).__name__, eg)
            await record_handler_failure(eg, signal)
        except SchedulerHandlerCancelled as exc:
            _log.error(
                "Scheduler signal handler raised CancelledError for %s",
                type(signal).__name__,
            )
            await record_handler_failure(exc, signal)

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
        rate_limit_claim: _RateLimitClaim | None = None,
        max_runs_claim: _MaxRunsClaim | None = None,
        global_slot_claim: _GlobalSlotClaim | None = None,
        threshold_cooldown_claim: _ThresholdCooldownClaim | None = None,
        extra_schedule_fields: dict[str, Any] | None = None,
        supersedes_run_id: str | None = None,
    ) -> None:
        """Thin wrapper that releases every admission claim on all exit paths.

        Only top-level callers (_maybe_fire, fire_now, _tick_github) that
        got an allowed admission reservation pass a non-None claim; chain
        children never do. Rate-limit and max-runs ownership transfer to
        the durable occurrence row as soon as it commits. The idempotent
        releases here remain the all-exit-path safety net, including
        failures before an occurrence can be written.

        *extra_schedule_fields* and *supersedes_run_id* pass straight
        through to _fire_inner() (github cursor fold-in and recovery
        re-fire, respectively -- see its docstring).
        """
        try:
            await self._fire_inner(
                schedule,
                run_id,
                trigger_context=trigger_context,
                chain_parent_id=chain_parent_id,
                chain_depth=chain_depth,
                rate_limit_claim=rate_limit_claim,
                max_runs_claim=max_runs_claim,
                extra_schedule_fields=extra_schedule_fields,
                supersedes_run_id=supersedes_run_id,
            )
        finally:
            if rate_limit_claim is not None:
                rate_limit_claim.release()
            if max_runs_claim is not None:
                max_runs_claim.release()
            if global_slot_claim is not None:
                global_slot_claim.release()
            if threshold_cooldown_claim is not None:
                threshold_cooldown_claim.release()

    def _threshold_alert_update_fields(
        self, schedule: dict, chain_depth: int, now: float
    ) -> dict[str, Any]:
        """Extra ``update_schedule()`` fields for a threshold-alert fire.

        Folded into the SAME schedule update call that already writes
        ``last_fired_at``/``next_fire_at`` inside ``_fire_inner()`` --
        deliberately placed AFTER ``create_schedule_run()`` has durably
        persisted the run row (both the invalid-action-failure branch and
        the normal running branch call this only once their own
        ``create_schedule_run()`` has already succeeded). Stamping the
        cooldown any earlier (e.g. in ``_maybe_fire()`` before ``_fire()``
        even starts) risks consuming it on a ``create_invocation()`` (or
        other pre-persistence) failure that leaves zero durable record an
        alert was ever attempted -- the exact silent-loss shape this
        feature exists to prevent.

        Only top-level fires (``chain_depth == 0``) of a
        threshold-configured schedule stamp the cooldown; on_success/
        on_fail chain children are follow-on actions of the same alert
        cycle, not a new one, and must not restamp it.
        """
        if chain_depth != 0 or not schedule.get("threshold_config"):
            return {}
        return {"last_alert_at": now}

    async def _write_occurrence(
        self,
        run: dict[str, Any],
        *,
        schedule_id: str,
        schedule_fields: dict[str, Any],
        supersedes_run_id: str | None,
    ) -> bool:
        """Durably record one occurrence row -- the choke point both
        _fire_inner() write sites go through. An ordinary fire is atomic
        with the schedule's cursor advance; a recovery re-fire
        (*supersedes_run_id* set) is instead atomic with tombstoning the
        orphan it replaces, and skips the cursor advance. Returns ``False``
        only for the recovery path, when the orphan no longer qualified for
        tombstoning by the time this write landed -- nothing is inserted.
        """
        if supersedes_run_id is not None:
            applied = await self._svc.tombstone_and_replace_schedule_run(
                supersedes_run_id, run, expected_orphan_status="running"
            )
            if applied:
                # The atomic write above only sets status + updated_at (no
                # reason columns -- see tombstone_and_replace_schedule_run()'s
                # docstring); layer the reason code/history on now, same
                # pattern as create_schedule_run_and_advance()'s own callers
                # (they set status directly in the INSERT and only add
                # reason/history with a separate follow-up update_status()
                # call). A same-status "failed"->"failed" append, not a CAS
                # -- the orphan is already durably terminal by this point.
                await self._svc.update_status(
                    "schedule_run",
                    supersedes_run_id,
                    new_status="failed",
                    reason_code=RunReasons.FAILED_NEVER_DISPATCHED,
                    reason_summary=(
                        "Scheduler crashed after committing this occurrence but "
                        "before confirming the external process launched."
                    ),
                    evidence_refs=[{"kind": "schedule_run", "id": run["id"]}],
                    source="system",
                    actor="scheduler_startup_recovery",
                )
            return applied
        await self._svc.create_schedule_run_and_advance(
            run, schedule_id=schedule_id, schedule_fields=schedule_fields
        )
        return True

    async def _abandon_superseded_recovery_fire(self, inv_id: str, *, orphan_id: str) -> None:
        """A recovery re-fire's occurrence write was refused -- the orphan it
        was meant to supersede no longer qualified by the time the write
        landed. No schedule_run row was created for this attempt; only its
        own invocation (never the orphan's) needs cleaning up.
        """
        _log.info(
            "Abandoning recovery re-fire for invocation %s: orphan %s was "
            "already resolved by something else",
            inv_id,
            orphan_id,
        )
        await self._svc.update_invocation(inv_id, ended_at=time.time())
        await self._guarded_terminal_status(
            "invocation",
            inv_id,
            new_status="cancelled",
            reason_code=RunReasons.CANCELLED_STALE_AUTO,
            reason_summary=(
                f"Recovery re-fire abandoned: the orphaned schedule_run "
                f"{orphan_id} it was meant to supersede was already resolved "
                "by something else before this re-fire's own write landed."
            ),
            evidence_refs=[{"kind": "schedule_run", "id": orphan_id}],
            source="system",
            actor="scheduler_startup_recovery",
        )

    async def _fire_inner(
        self,
        schedule: dict,
        run_id: str,
        *,
        trigger_context: dict,
        chain_parent_id: str | None = None,
        chain_depth: int = 0,
        rate_limit_claim: _RateLimitClaim | None = None,
        max_runs_claim: _MaxRunsClaim | None = None,
        extra_schedule_fields: dict[str, Any] | None = None,
        supersedes_run_id: str | None = None,
    ) -> None:
        """Fire one occurrence of *schedule*.

        DELIVERY CONTRACT -- at-least-once up to confirmed process launch,
        at-most-once past it. Three windows: (1) before the occurrence
        transaction commits, a crash leaves nothing durable, so a restart
        fires fresh -- never a duplicate. (2) Between commit and
        ``spawn_and_wait()`` confirming launch (``on_launched`` stamping
        ``dispatched_at``), the row is durable but undispatched;
        ``_recover_undispatched_fires()`` finds it on startup and re-fires
        via *supersedes_run_id*, which routes the occurrence insert through
        ``tombstone_and_replace_schedule_run()`` to tombstone the orphan and
        insert the replacement atomically (its CAS also requires
        ``dispatched_at IS NULL``, so a launch that gets confirmed in the
        race against recovery wins and the tombstone is a no-op). (3) Once
        ``dispatched_at`` is confirmed, the process genuinely exists and is
        never re-fired -- the row is resolved by the stale-run reaper or its
        own terminal write. This boundary is deliberate: a duplicate
        real-world side effect is worse than one unretried outcome.
        """
        sid = schedule["id"]
        now = time.time()

        inv_id = uuid.uuid4().hex[:12]
        # Registered before the invocation can possibly reach a terminal
        # status, unregistered on every exit path below (including the
        # early build_argv-failure return) so a matching registration never
        # outlives this fire.
        notify_scope = _register_schedule_notify(
            inv_id, schedule.get("notify_on"), schedule.get("notify_command")
        )
        try:
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
        except BaseException:
            # No invocation row exists yet, so no terminal transition can
            # ever fire for this registration; drop it before propagating.
            _unregister_schedule_notify(notify_scope)
            raise

        try:
            # kind='command' spawns an allow-listed executable directly, never
            # through `li` -- resolving the `li` executable is unnecessary
            # (and would wrongly block a command-kind fire on a daemon host
            # where `li` itself is unresolvable).
            li_prefix: list[str] | None = None
            if schedule.get("action_kind") != "command":
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
            # The notify unregister lives in this handler's own finally:
            # every exit (including a failing terminal write below) drops
            # the registration, and any terminal write that does land
            # happens inside the try, before the unregister.
            try:
                _end_time = time.time()
                next_at = self._compute_next_fire(schedule, now)
                failed_schedule_fields: dict[str, Any] = {"last_fired_at": now}
                failed_schedule_fields.update(self._next_fire_field(schedule, next_at))
                failed_schedule_fields.update(
                    self._threshold_alert_update_fields(schedule, chain_depth, now)
                )
                if extra_schedule_fields:
                    failed_schedule_fields.update(extra_schedule_fields)
                # Occurrence-insert + cursor-advance atomic even on this
                # invalid-action failure path -- otherwise a permanently
                # misconfigured github_poll schedule would never advance its
                # cursor past the offending event and re-fail it forever.
                # (A recovery re-fire skips the cursor advance and is instead
                # atomic with tombstoning the orphan it supersedes -- see
                # _write_occurrence()'s docstring.)
                written_occurrence = await self._write_occurrence(
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
                    },
                    schedule_id=sid,
                    schedule_fields=failed_schedule_fields,
                    supersedes_run_id=supersedes_run_id,
                )
                if not written_occurrence:
                    # Abandon writes the invocation's cancelled terminal
                    # status; the enclosing finally unregisters only after
                    # it, so a declared notify on that status still fires.
                    await self._abandon_superseded_recovery_fire(
                        inv_id, orphan_id=supersedes_run_id
                    )
                    return
                if rate_limit_claim is not None:
                    # The durable row now accounts for this fire across process
                    # restarts; keeping the in-memory reservation would count it twice.
                    rate_limit_claim.release()
                if max_runs_claim is not None:
                    # Same transfer: the persisted row (counted via its fired
                    # status) now carries this fire's max_runs budget unit.
                    max_runs_claim.release()
                written = await self._svc.update_status(
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
                if written:
                    await self._dispatch_signal(
                        build_schedule_run_signal(
                            entity_id=run_id,
                            new_status="failed",
                            reason_code=RunReasons.FAILED_EXCEPTION,
                            schedule_id=sid,
                            action_kind=schedule.get("action_kind", ""),
                            chain_depth=chain_depth,
                            trigger_context=trigger_context,
                            error_detail=f"{type(exc).__name__}: {exc}",
                        )
                    )
                inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                    self._svc, inv_id, fallback_status="failed", exception=exc
                )
                await self._svc.update_invocation(inv_id, ended_at=_end_time)
                inv_written = await self._guarded_terminal_status(
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
                if inv_written:
                    await flush_run_telemetry(
                        self._svc, self._signal_bus, run_id=run_id, invocation_id=inv_id
                    )
                else:
                    # Another finalizer already wrote this invocation's terminal
                    # status, so no flush happens here -- but a schedule_run
                    # signal was still minted onto the bus above. Drop its
                    # counters now instead of letting them sit in the bus's
                    # per-run_id map forever (it never gets a second flush call
                    # for this run_id to consume them).
                    self._signal_bus.pop_run_counters(run_id)
                # last_fired_at/next_fire_at (and any extra_schedule_fields)
                # already landed atomically with the occurrence insert above.
                await self._check_max_runs(schedule, chain_depth)
                return
            finally:
                _unregister_schedule_notify(notify_scope)
        except BaseException:
            # Cancellation (or any other non-Exception) during action setup
            # is not an invalid action: propagate it untouched. This window
            # sits before the main try/finally below, so the registration
            # must be dropped here; no invocation terminal write has
            # happened yet on this path.
            _unregister_schedule_notify(notify_scope)
            raise

        # Ensure the flow_yaml tmp file is removed on any exception or
        # cancellation in the DB ops below, before spawn_and_wait() runs.
        # suppress(OSError) makes double-unlink (spawn_and_wait already cleaned up) safe.
        try:
            next_at = self._compute_next_fire(schedule, now)
            update_fields: dict[str, Any] = {"last_fired_at": now}
            update_fields.update(self._next_fire_field(schedule, next_at))
            update_fields.update(self._threshold_alert_update_fields(schedule, chain_depth, now))
            if extra_schedule_fields:
                update_fields.update(extra_schedule_fields)

            # Occurrence-insert + cursor-advance MUST land atomically: a
            # crash between two independently-committed writes here is
            # exactly what let a restart re-derive "still due" for an
            # occurrence that was already durably recorded (double-fire).
            # spawn_and_wait() below always runs AFTER this transaction
            # commits, never inside it, so a crash before this call can at
            # worst discard an occurrence that was never durably recorded.
            # A crash AFTER this commits but before spawn_and_wait confirms
            # launch is the second window in this method's delivery-
            # contract docstring above -- _recover_undispatched_fires()
            # handles it at the next startup, not here. (A recovery
            # re-fire skips the cursor advance and is instead atomic with
            # tombstoning the orphan it supersedes -- see
            # _write_occurrence()'s docstring.)
            written_occurrence = await self._write_occurrence(
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
                },
                schedule_id=sid,
                schedule_fields=update_fields,
                supersedes_run_id=supersedes_run_id,
            )
            if not written_occurrence:
                await self._abandon_superseded_recovery_fire(inv_id, orphan_id=supersedes_run_id)
                return
            if rate_limit_claim is not None:
                # The durable running row now owns the rolling-window slot.
                rate_limit_claim.release()
            if max_runs_claim is not None:
                # The durable running row now owns this fire's max_runs unit.
                max_runs_claim.release()
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

            _log.info(
                "Firing schedule %s (run %s, chain_depth=%d)", schedule["name"], run_id, chain_depth
            )

            action_cwd, missing_cwd_path = await _resolve_action_cwd(schedule)
            exit_code, stderr_tail = await _subprocess.spawn_and_wait(
                argv,
                inv_id,
                tmp_path=_tmp_path,
                cwd=action_cwd,
                action_kind=schedule.get("action_kind"),
                # Stamps dispatched_at the instant the OS process is
                # confirmed to exist -- the signal _recover_undispatched_
                # fires() uses to tell "committed but never launched" (safe
                # to re-fire) apart from "launched, outcome merely lost"
                # (never re-fired; see this method's docstring).
                on_launched=lambda: self._mark_dispatched(run_id),
            )
            end_time = time.time()
            status = "completed" if exit_code == 0 else "failed"
            if exit_code == 0:
                reason_code = RunReasons.COMPLETED_OK
                reason_summary = "Scheduled process completed successfully."
            elif missing_cwd_path:
                # The configured execution root (action_cwd) or project
                # directory was gone at fire time (e.g. a pruned worktree);
                # the process fell back to the daemon's own cwd instead and
                # then failed -- attribute that plainly rather than leaving
                # only a generic non-zero exit code.
                reason_code = RunReasons.FAILED_MISSING_CWD
                reason_summary = (
                    f"Scheduled process exited non-zero ({exit_code}) after its "
                    f"configured working directory {missing_cwd_path!r} no "
                    "longer existed on disk; it ran with the daemon's own "
                    "working directory instead."
                )
            else:
                reason_code = RunReasons.FAILED_EXIT_NONZERO
                reason_summary = f"Scheduled process exited non-zero: {exit_code}."

            await self._svc.update_schedule_run(
                run_id,
                exit_code=exit_code,
                ended_at=end_time,
                error_detail=stderr_tail if exit_code != 0 else None,
            )
            written = await self._guarded_terminal_status(
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
            if written:
                await self._dispatch_signal(
                    build_schedule_run_signal(
                        entity_id=run_id,
                        new_status=status,
                        reason_code=reason_code,
                        schedule_id=sid,
                        action_kind=schedule.get("action_kind", ""),
                        chain_depth=chain_depth,
                        trigger_context=trigger_context,
                        error_detail=stderr_tail if exit_code != 0 else "",
                    )
                )
            inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                self._svc, inv_id, fallback_status=status, exit_code=exit_code
            )
            await self._svc.update_invocation(inv_id, ended_at=end_time)
            inv_written = await self._guarded_terminal_status(
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
            if inv_written:
                await flush_run_telemetry(
                    self._svc, self._signal_bus, run_id=run_id, invocation_id=inv_id
                )
            else:
                # Another finalizer already wrote this invocation's terminal
                # status, so no flush happens here -- but a schedule_run
                # signal was still minted onto the bus above. Drop its
                # counters now instead of letting them sit in the bus's
                # per-run_id map forever (it never gets a second flush call
                # for this run_id to consume them).
                self._signal_bus.pop_run_counters(run_id)
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
                written = await self._guarded_terminal_status(
                    "schedule_run",
                    run_id,
                    new_status="cancelled",
                    reason_code=RunReasons.CANCELLED_SYSTEM,
                    reason_summary="Schedule run cancelled by scheduler shutdown.",
                    evidence_refs=[{"kind": "schedule", "id": sid}],
                    source="executor",
                    actor=run_id,
                )
                if written:
                    await self._dispatch_signal(
                        build_schedule_run_signal(
                            entity_id=run_id,
                            new_status="cancelled",
                            reason_code=RunReasons.CANCELLED_SYSTEM,
                            schedule_id=sid,
                            action_kind=schedule.get("action_kind", ""),
                            chain_depth=chain_depth,
                            trigger_context=trigger_context,
                        )
                    )
                inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                    self._svc, inv_id, fallback_status="cancelled"
                )
                await self._svc.update_invocation(inv_id, ended_at=_end_time)
                inv_written = await self._guarded_terminal_status(
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
                if inv_written:
                    await flush_run_telemetry(
                        self._svc, self._signal_bus, run_id=run_id, invocation_id=inv_id
                    )
                else:
                    # Another finalizer already wrote this invocation's
                    # terminal status, so no flush happens here -- but a
                    # schedule_run signal was still minted onto the bus
                    # above. Drop its counters now instead of letting them
                    # sit in the bus's per-run_id map forever.
                    self._signal_bus.pop_run_counters(run_id)
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
            written = await self._guarded_terminal_status(
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
            if written:
                await self._dispatch_signal(
                    build_schedule_run_signal(
                        entity_id=run_id,
                        new_status="failed",
                        reason_code=RunReasons.FAILED_EXCEPTION,
                        schedule_id=sid,
                        action_kind=schedule.get("action_kind", ""),
                        chain_depth=chain_depth,
                        trigger_context=trigger_context,
                        error_detail=f"{type(exc).__name__}: {exc}",
                    )
                )
            inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                self._svc, inv_id, fallback_status="failed", exception=exc
            )
            await self._svc.update_invocation(inv_id, ended_at=_end_time)
            inv_written = await self._guarded_terminal_status(
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
            if inv_written:
                await flush_run_telemetry(
                    self._svc, self._signal_bus, run_id=run_id, invocation_id=inv_id
                )
            else:
                # Another finalizer already wrote this invocation's terminal
                # status, so no flush happens here -- but a schedule_run
                # signal was still minted onto the bus above. Drop its
                # counters now instead of letting them sit in the bus's
                # per-run_id map forever.
                self._signal_bus.pop_run_counters(run_id)
            await self._check_max_runs(schedule, chain_depth)
        finally:
            _unregister_schedule_notify(notify_scope)
            if chain_depth == 0:
                self._running.pop(sid, None)
            if _tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(_tmp_path)

    def _next_fire_field(self, schedule: dict, next_at: float | None) -> dict[str, float | None]:
        """Field(s) to merge into an ``update_schedule()`` call for *next_at*.

        ``None`` normally means "leave next_fire_at untouched" -- interval/
        cron/github_poll rows always compute their own future fire, so a
        ``None`` there would only ever come from a malformed row and must
        not blank out a value some other write already set. An ``at``
        trigger is the one case where ``None`` is the terminal, correct
        answer: it must be persisted (not merely omitted) so a schedule that
        already fired its single instant is never read back as still due.
        """
        if next_at is not None:
            return {"next_fire_at": next_at}
        if schedule.get("trigger_type") == "at":
            return {"next_fire_at": None}
        return {}

    def _compute_next_fire(self, schedule: dict, ref_time: float) -> float | None:
        if schedule["trigger_type"] == "cron":
            expr = schedule.get("cron_expr")
            if not expr:
                return None
            try:
                from croniter import croniter

                from lionagi.studio.config import SCHEDULER_TZ

                # Resolve the cron expression's wall-clock fields in the
                # schedule's own declared timezone when it has one (set by
                # the declarative apply path); legacy rows with no
                # resolved_timezone keep resolving against the process-wide
                # default. croniter honors DST transitions when given a
                # tz-aware start_time; get_next(float) still returns an
                # absolute UTC epoch, which is what next_fire_at stores.
                tz_name = schedule.get("resolved_timezone") or SCHEDULER_TZ
                tz = _resolve_scheduler_tzinfo(tz_name)
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
        elif schedule["trigger_type"] == "at":
            # A point-in-time trigger fires exactly once -- there is no next
            # occurrence to compute. Callers use _next_fire_field() to turn
            # this None into an explicit persisted None, rather than leaving
            # a past next_fire_at in place.
            return None
        return None


scheduler = SchedulerEngine()
register_default_handlers(scheduler._signal_bus)
