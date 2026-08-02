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
from dataclasses import dataclass
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
_DEFERRED_RECORD_EVERY = 10  # one deferred-run record per this many deferrals per schedule
_MAX_PREDISPATCH_REFUSALS = 3  # after this many refusals, tombstone the event and advance past it


def _register_schedule_notify(
    inv_id: str, notify_on: list[str] | None, notify_command: str | None
) -> str | None:
    """Register schedule's declared notify command on this fire's invocation; returns the name to unregister, or None."""
    if not notify_on or not notify_command:
        return None
    resolved = resolve_notify_config(override=notify_command).handler
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
    """One-shot max_runs reservation; _fire() must release() exactly once from a finally."""

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
    """One-shot global concurrent-fire slot claim; same release-once-in-finally lifecycle as _MaxRunsClaim."""

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
    """One-shot threshold-alert cooldown reservation, added synchronously (no await) to close
    the race between two ticks both reading a stale last_alert_at."""

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


@dataclass(frozen=True)
class ScheduleTimezone:
    """The zone a schedule's cron fields are interpreted in, plus provenance.

    ``source`` distinguishes a UTC an operator requested from a UTC that is a fallback —
    ``name`` alone can't tell them apart.
    """

    name: str
    source: str
    tzinfo: ZoneInfo


def resolve_schedule_timezone(schedule: dict) -> ScheduleTimezone:
    """Resolve the zone *schedule*'s cron expression is interpreted in.

    Uses the row's own ``resolved_timezone`` if set, else the process default; an
    unloadable zone name falls back to UTC (logged), tagged with its own source. Pure
    read — never touches the ``effective_timezone*`` columns.
    """
    from lionagi.studio.config import (
        SCHEDULER_TZ,
        SCHEDULER_TZ_SOURCE,
        TZ_SOURCE_SCHEDULE_DECLARED,
        TZ_SOURCE_UTC_UNLOADABLE_NAME,
    )

    declared = schedule.get("resolved_timezone")
    if declared:
        requested, source = declared, TZ_SOURCE_SCHEDULE_DECLARED
    else:
        requested, source = SCHEDULER_TZ, SCHEDULER_TZ_SOURCE
    try:
        return ScheduleTimezone(requested, source, ZoneInfo(requested))
    except (ZoneInfoNotFoundError, ValueError):
        _log.warning(
            "Schedule %s: timezone %r (from %s) is not a zone this host can "
            "load; interpreting its cron expression in UTC instead. Every "
            "fire time this schedule computes is shifted by the offset of "
            "the zone that was asked for.",
            schedule.get("id"),
            requested,
            source,
        )
        return ScheduleTimezone("UTC", TZ_SOURCE_UTC_UNLOADABLE_NAME, ZoneInfo("UTC"))


class SchedulerCwdInheritRefusedError(RuntimeError):
    """A schedule carrying an explicit execution root could not resolve any of
    its configured directories, so the resolver refused to inherit the
    daemon's own working directory instead of silently substituting it.
    """

    def __init__(
        self,
        schedule_id: str | None,
        configured_root: str | None,
        daemon_cwd: str,
    ) -> None:
        self.schedule_id = schedule_id
        self.configured_root = configured_root
        self.daemon_cwd = daemon_cwd
        super().__init__(
            f"Schedule {schedule_id}: configured execution root "
            f"{configured_root!r} could not be resolved to an existing "
            f"directory, and the only remaining fallback is inheriting the "
            f"daemon working directory {daemon_cwd!r}. Refusing to run the "
            f"scheduled action under a substituted working directory; point "
            f"this schedule at an existing action_cwd/action_project. "
            f"LIONAGI_SCHEDULER_CWD does not apply to a schedule that carries "
            f"its own execution root."
        )


def _is_usable_execution_root(root: str | None) -> bool:
    """A usable execution root is an existing absolute directory (a relative path would
    resolve against the daemon's own cwd, not the schedule's configured root)."""
    if not root:
        return False
    path = Path(root)
    return path.is_absolute() and path.is_dir()


async def _resolve_action_cwd(schedule: dict) -> str | None:
    """Resolve the working directory for a scheduled subprocess spawn: action_cwd, then
    action_project's path, then LIONAGI_SCHEDULER_CWD for ownerless rows. An owner-carrying
    row that can't resolve raises SchedulerCwdInheritRefusedError instead of silently
    inheriting the daemon's cwd.
    """
    action_cwd = schedule.get("action_cwd")
    if action_cwd:
        if _is_usable_execution_root(action_cwd):
            return action_cwd
        _log.warning(
            "Schedule %s: persisted execution root %r is not usable -- it must "
            "be an existing absolute directory. It may be a pruned worktree, "
            "or a relative path, which would resolve against the daemon's own "
            "cwd rather than the configured root. Trying action_project, then "
            "refusing rather than spawning into a missing or substituted "
            "directory.",
            schedule.get("id"),
            action_cwd,
        )
    elif action_cwd is not None:
        # Present-but-empty root: Path("") is Path(".") and would otherwise pass as usable.
        _log.warning(
            "Schedule %s: persisted execution root is empty, which is not a "
            "usable directory; trying action_project, then refusing rather "
            "than spawning into a missing or substituted directory.",
            schedule.get("id"),
        )

    action_project = schedule.get("action_project")
    if action_project:
        from lionagi.studio.services.projects import get_project

        project = await get_project(action_project)
        if project:
            path = project.get("path")
            if path:
                if _is_usable_execution_root(path):
                    return path
                _log.warning(
                    "Schedule %s: action_project %r is registered at %r, which "
                    "is not a usable execution root -- it must be an existing "
                    "absolute directory. The path may no longer exist (e.g. a "
                    "pruned worktree), or be relative, which would resolve "
                    "against the daemon's own cwd. Registered project paths "
                    "are not validated on the way in, so this is checked here. "
                    "Refusing rather than spawning into a missing or "
                    "substituted directory.",
                    schedule.get("id"),
                    action_project,
                    path,
                )

    if action_cwd is not None or action_project is not None:
        # `is not None`, not truthiness: a present-but-empty root ("") fails closed here too.
        raise SchedulerCwdInheritRefusedError(
            schedule_id=schedule.get("id"),
            configured_root=action_cwd if action_cwd is not None else action_project,
            daemon_cwd=str(Path.cwd()),
        )

    # Ownerless (pre-migration) rows only: fall back to an operator-set default.
    env_cwd = os.environ.get("LIONAGI_SCHEDULER_CWD")
    if _is_usable_execution_root(env_cwd):
        return env_cwd

    _log.warning(
        "Schedule %s has no persisted execution root (action_cwd) -- a "
        "pre-migration row -- and no action_project or LIONAGI_SCHEDULER_CWD "
        "resolved either; the scheduled action will inherit the daemon's own "
        "working directory and may fail to spawn (`uv run li` finds no "
        "project) if that directory has none. DEPRECATED: this schedule "
        "should be backfilled (restart the daemon) or updated with an "
        "explicit execution root.",
        schedule.get("id"),
    )
    return None


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
        # bridges the admission-read -> terminal-row window so concurrent callers can't overshoot max_fires
        self._rate_limit_lock = asyncio.Lock()
        self._rate_limit_inflight: dict[str, dict[str, float]] = {}
        # global concurrent-fire cap (single-process; see _reserve_global_slot).
        self._global_slot_lock = asyncio.Lock()
        self._global_inflight = 0
        self._deferred_log_counts: dict[str, int] = {}  # schedule_id -> deferrals since last record
        # membership = a fire for this schedule's breach is in flight or reserved; closes a DB-only last_alert_at race
        self._threshold_pending: set[str] = set()
        # ADR-0071 D4: this daemon process is the one host worker (v1).
        self._task_worker_id = f"host:{uuid.uuid4().hex[:8]}"

    async def start(self) -> None:
        _log.info("Scheduler engine starting")
        self._stopping = False
        self._log_scheduler_timezone()
        await self._backfill_action_cwd()
        await self._stamp_effective_timezones()
        await self._recompute_armed_cron_schedules()
        self._task = asyncio.create_task(self._tick_loop())

    def _log_scheduler_timezone(self) -> None:
        """Log the effective cron timezone once at startup; a UTC fallback logs at warning
        since it silently shifts every cron schedule by the host offset."""
        from lionagi.studio.config import TZ_UTC_FALLBACK_SOURCES, scheduler_timezone_report

        report = scheduler_timezone_report()
        if report["source"] in TZ_UTC_FALLBACK_SOURCES:
            _log.warning(
                "Scheduler cron timezone FELL BACK to %s (source=%s, from=%s) -- "
                "this is not a configured zone, and every cron schedule without "
                "its own declared timezone is being interpreted in it. Set "
                "LIONAGI_SCHEDULER_TZ to an IANA zone name to choose one.",
                report["name"],
                report["source"],
                report["source_detail"],
            )
        else:
            _log.info(
                "Scheduler cron timezone: %s (source=%s, from=%s). Cron "
                "schedules without their own declared timezone are "
                "interpreted in this zone.",
                report["name"],
                report["source"],
                report["source_detail"],
            )

    async def _stamp_effective_timezones(self) -> None:
        """Stamp every cron schedule row with its resolved effective timezone and source;
        idempotent (skips rows already matching)."""
        try:
            schedules = await self._svc.list_schedules()
        except Exception:
            _log.exception("Failed to load schedules for startup timezone stamping")
            return
        for s in schedules:
            fields = self._effective_timezone_fields(s)
            if not fields or all(s.get(key) == value for key, value in fields.items()):
                continue
            try:
                await self._svc.update_schedule(s["id"], **fields)
            except Exception:
                _log.exception("Failed to stamp effective timezone for schedule %s", s.get("id"))

    def _effective_timezone_fields(self, schedule: dict) -> dict[str, str]:
        """Effective-timezone columns for *schedule*; empty for interval/at/github_poll
        triggers (no zone in play to stamp)."""
        if schedule.get("trigger_type") != "cron" or not schedule.get("cron_expr"):
            return {}
        resolution = resolve_schedule_timezone(schedule)
        return {
            "effective_timezone": resolution.name,
            "effective_timezone_source": resolution.source,
        }

    async def _backfill_action_cwd(self) -> None:
        """One-shot backfill: snapshot action_project's resolved path into action_cwd for
        pre-migration rows still missing it; idempotent."""
        try:
            schedules = await self._svc.list_schedules()
        except Exception:
            _log.exception("Failed to load schedules for startup action_cwd backfill")
            return
        for s in schedules:
            # `is not None`, not truthiness: a present-but-empty action_cwd is a root the
            # resolver already fails closed on, not one to backfill over.
            if s.get("action_cwd") is not None or not s.get("action_project"):
                continue
            try:
                from lionagi.studio.services.projects import get_project

                project = await get_project(s["action_project"])
                path = project.get("path") if project else None
                # Same usability rule as the resolver: a relative path must not be persisted.
                if _is_usable_execution_root(path):
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
        """Re-resolve every enabled cron schedule's next_fire_at under the current timezone
        before the tick loop starts; skips already-due rows so _check_missed_fires() applies
        missed_fire_policy first instead of this hook advancing them silently."""
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
        """Recompute + persist a cron schedule's next_fire_at, logging only if it shifts.

        Shared by daemon startup, cron-field PATCH, and disable→enable (services/schedules.py).
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
        await self._svc.update_schedule(
            schedule["id"], next_fire_at=new, **self._effective_timezone_fields(schedule)
        )
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
            # A human is waiting: refused outright rather than deferred like automatic fires.
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
        """Stamp dispatched_at the instant spawn_and_wait confirms the process exists
        (see _fire_inner()'s delivery-contract docstring)."""
        await self._svc.update_schedule_run(run_id, dispatched_at=time.time())

    async def _recover_undispatched_fires(self) -> None:
        """Startup scan for occurrences committed but never confirmed launched (see
        _fire_inner()'s delivery contract); chain children and orphans of a dead schedule are
        tombstoned directly, everything else re-fired via supersedes_run_id."""
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
        """CAS-tombstone an undispatched orphan with no replacement to follow."""
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
            pass  # raced with something else finalizing this row between the scan and here

    async def _check_missed_fires(self) -> None:
        try:
            schedules = await self._svc.list_schedules(enabled=True)
            now = time.time()
            for s in schedules:
                next_fire_at = s.get("next_fire_at")
                if next_fire_at is None or next_fire_at > now:
                    continue
                # A schedule_run already recorded means the slot was handled; advance the
                # cursor past it instead of firing again and double-executing the action.
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
        """Queue exactly one recovery fire for a past-due run_once schedule, reserving
        admission claims and next_fire_at synchronously before _tick() can see the same
        past-due value and double-fire."""
        # Admission claims first, then next_fire_at: a refusal must leave an 'at' trigger's
        # next_fire_at untouched, or its single run would be stranded permanently.
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
            # _next_fire_field, not a bare not-None check: an 'at' trigger's terminal None
            # must be reserved too, or the next _tick() queues a duplicate fire.
            fields = self._next_fire_field(schedule, next_at)
            if fields:
                try:
                    await self._svc.update_schedule(schedule["id"], **fields)
                except Exception:
                    # Reserve didn't land: let the normal tick own this cycle's fire instead.
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
                            await self._svc.update_schedule(
                                s["id"],
                                next_fire_at=next_at,
                                **self._effective_timezone_fields(s),
                            )
            except Exception:
                _log.exception("Error evaluating schedule %s", s.get("name"))

    async def _deliver_due_dispatches(self, now: float) -> None:
        """Scan due dispatch_outbox rows and attempt delivery; not interval-gated, the 30s
        tick itself is the latency floor (ADR-0059 slice 1)."""
        from lionagi.dispatch import deliver_due_dispatches
        from lionagi.state.db import StateDB

        async with StateDB() as db:
            await deliver_due_dispatches(db, now=now)

    async def _run_task_worker_tick(self, now: float) -> None:
        """Reap lapsed leases and claim/execute eligible host task applications; not
        interval-gated, same reason as _deliver_due_dispatches (ADR-0071 D4)."""
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

        # Reserve one global slot before polling so a no-slot poll can't fetch-advance-discard;
        # handed to whichever event fires first below, further events reserve their own.
        slot_allowed, pre_slot_claim = await self._reserve_global_slot()
        if not slot_allowed:
            if pre_rate_claim is not None:
                pre_rate_claim.release()
            await self._maybe_record_deferred(schedule, now)
            return

        from .github import github_poll

        sid = schedule["id"]
        # pre_slot_claim is nulled the moment it's handed to _fire() or released inline, so
        # this finally only fires for the untouched case (avoids leaking the slot on failure).
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

            # Stamp health columns from the poll outcome regardless of items found, so a
            # healthy-empty poll resets the blind clock and a quiet repo never false-alarms.
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
                    # Filtered-out PRs consume no budget; the cursor advances so they aren't re-listed.
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
                    fired = await self._fire(
                        schedule,
                        run_id,
                        trigger_context=ctx,
                        rate_limit_claim=rate_claim,
                        max_runs_claim=max_runs_claim,
                        global_slot_claim=slot_claim,
                        # Advances github_cursor atomically with the occurrence insert, closing
                        # the double-fire hazard of batching the cursor write until after the loop.
                        extra_schedule_fields={"github_cursor": item.updated_at},
                    )
                    if not fired:
                        # Refusal before a process started means nothing ran, so re-offering
                        # isn't a re-execution -- but bounded, or a poison event blocks the queue forever.
                        refusals = await self._record_predispatch_refusal(schedule, item.updated_at)
                        if refusals < _MAX_PREDISPATCH_REFUSALS:
                            # Stop rather than trying the rest: if the cause is the schedule,
                            # later events refuse identically and each burns a budget unit.
                            drop_reason = (
                                f"an earlier event refused before dispatch "
                                f"({refusals}/{_MAX_PREDISPATCH_REFUSALS} attempts)"
                            )
                            dropped_prs = [
                                e.event.get("pr_number") for e in polled[idx:] if e.dispatchable
                            ]
                            break
                        _log.warning(
                            "Schedule %s (%s): event (PR %s, updated_at %s) refused "
                            "before dispatch %d times; recording the refusal as "
                            "terminal for it and advancing the cursor past it so "
                            "later events are not blocked behind it",
                            schedule.get("name"),
                            sid,
                            item.event.get("pr_number"),
                            item.updated_at,
                            refusals,
                        )
                        cursor = item.updated_at
                        await self._clear_predispatch_refusals(schedule)
                        # Advance rides the trailing batched write below; a crash here just
                        # re-offers the event like every earlier attempt did.
                        continue
                    await self._clear_predispatch_refusals(schedule)
                    # Tracked for the batched trailing write below; idempotent if already persisted.
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

            # Safety-net batched write: dispatched events already advanced the cursor atomically;
            # this covers filtered/nothing-fired cases, otherwise a harmless no-op re-write.
            if cursor != schedule.get("github_cursor"):
                # guard_cursor_forward: must not undo a cursor an operator moved forward mid-poll.
                await self._svc.update_schedule(
                    sid, github_cursor=cursor, guard_cursor_forward=True
                )
        finally:
            if pre_rate_claim is not None:
                pre_rate_claim.release()
            if pre_slot_claim is not None:
                pre_slot_claim.release()

    async def _record_predispatch_refusal(self, schedule: dict, event_cursor: str) -> int:
        """Count one pre-dispatch refusal of the event at *event_cursor*; keyed per event (not
        a running tally) and persisted since retries span polls/restarts."""
        prior = schedule.get("predispatch_refusal_count") or 0
        if schedule.get("predispatch_refusal_event") != event_cursor:
            prior = 0
        count = prior + 1
        await self._svc.update_schedule(
            schedule["id"],
            predispatch_refusal_event=event_cursor,
            predispatch_refusal_count=count,
        )
        # Keep this tick's snapshot in step so a second refusal in the same poll counts right.
        schedule["predispatch_refusal_event"] = event_cursor
        schedule["predispatch_refusal_count"] = count
        return count

    async def _clear_predispatch_refusals(self, schedule: dict) -> None:
        """Drop the pre-dispatch refusal streak once the cursor moves past the event it was counting."""
        if not schedule.get("predispatch_refusal_count") and not schedule.get(
            "predispatch_refusal_event"
        ):
            return
        await self._svc.update_schedule(
            schedule["id"],
            predispatch_refusal_event=None,
            predispatch_refusal_count=0,
        )
        schedule["predispatch_refusal_event"] = None
        schedule["predispatch_refusal_count"] = 0

    async def _reserve_max_runs_budget(self, schedule: dict) -> tuple[bool, _MaxRunsClaim | None]:
        """Atomically claim one top-level fire against schedule['max_runs'].

        Returns (allowed, claim); claim is None when unbounded. Caller MUST pass claim
        through to _fire()'s max_runs_claim= on every exit path (even pre-run failures), or
        it leaks permanently. inflight is read BEFORE the awaited count_schedule_runs() call,
        not after -- reading it after would let a fire's commit-and-release race vanish from
        both counts and overshoot max_runs (only over-counting, a spurious refusal, is safe).
        """
        max_runs = schedule.get("max_runs")
        if not max_runs:
            return True, None
        sid = schedule["id"]
        async with self._max_runs_lock:
            inflight = self._max_runs_inflight.get(sid, 0)
            # Budget is consumed on fire, not resolution; claims and rows are disjoint
            # windows of the same fire, so they're summed, not maxed.
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
        """Reserve one fire inside the schedule's rolling time window; exhaustion is a
        temporary refusal, automatic callers leave the schedule due for a later retry."""
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
        """Atomically claim one global concurrent-fire slot, mirroring
        _reserve_max_runs_budget(); claim is None when MAX_SCHEDULED_CONCURRENT is unlimited
        (0). Chain children never call this -- only top-level fires consume a slot."""
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
        """Emit a throttled skipped-run record for a capacity-deferred fire: the first
        deferral and every _DEFERRED_RECORD_EVERY-th one after."""
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
        """True if the schedule has exhausted its configured spend budget; a pre-fire
        cumulative gate only, so an in-flight run is never killed and a schedule can
        overshoot by one run's cost."""
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
        """Auto-disable a schedule that exhausted its spend budget, recording why; fire_now()
        refuses instead of disabling."""
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
        """Evaluate schedule["threshold_config"] against live metrics; returns a breach dict
        rendering into action-prompt templates, or None when within bounds."""
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
        """Advance next_fire_at without firing the action -- used when a threshold cadence
        tick has no breach (or an in-cooldown one) to spawn for."""
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
            # Suppress refiring within the last alert's own window, so a sustained breach
            # doesn't fire on every tick; the cadence still advances underneath.
            cooldown_sec = breach["window_minutes"] * 60
            sid = schedule["id"]
            last_alert_at = schedule.get("last_alert_at")
            in_cooldown = last_alert_at is not None and now - last_alert_at < cooldown_sec
            # _threshold_pending closes the race last_alert_at alone can't: this check and the
            # reservation below are synchronous (no await between), so no tick can slip in.
            if in_cooldown or sid in self._threshold_pending:
                await self._advance_next_fire_only(schedule, now)
                return
            self._threshold_pending.add(sid)
            threshold_claim = _ThresholdCooldownClaim(self, sid)
            threshold_extra = breach

        # A raise anywhere in this gate must release threshold_claim/claim/slot_claim, or a
        # leaked reservation permanently mutes the alert until an engine restart.
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
                # Leave next_fire_at untouched so the next tick retries; claims are released
                # by the finally below -- deferring, not consuming budget or the cooldown.
                return

            run_id = uuid.uuid4().hex[:12]
            ctx = {
                "scheduled": True,
                "fired_at": now,
                "next_fire_at": schedule.get("next_fire_at"),
            }
            if threshold_extra:
                ctx.update(threshold_extra)
                # last_alert_at is NOT stamped here: _fire_inner() can still fail before a
                # row persists, and stamping early would consume the cooldown with no record.
            self._tracked_fire(
                schedule,
                run_id,
                trigger_context=ctx,
                rate_limit_claim=rate_claim,
                max_runs_claim=claim,
                global_slot_claim=slot_claim,
                threshold_cooldown_claim=threshold_claim,
            )
            # Flipped only after _tracked_fire() returns, so a synchronous launch failure
            # still releases the claims below (release is idempotent, no double-free).
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
        extra_fields: dict | None = None,
    ) -> bool:
        """Write a terminal status without crashing when the row is already terminal (e.g.
        raced by the deadline reaper); *extra_fields* rides the same guard so a lost race
        doesn't overwrite the winner's values."""
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
            extra_fields=extra_fields,
        )
        if not written:
            _log.debug(
                "%s %s already finalized; continuing scheduler side effects",
                entity_type,
                entity_id,
            )
        return written

    async def _dispatch_signal(self, signal: Any) -> None:
        """Emit *signal*; a handler exception here must never look like it undid the
        already-committed row or stop the tick loop, so failures are recorded here instead
        of propagated (except a genuine cancellation)."""
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
        """Auto-disable a schedule once its top-level fired runs hit max_runs; chain children
        (chain_depth>0) never count. Does not release the max_runs claim -- that's _fire()'s
        responsibility."""
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
    ) -> bool:
        """Thin wrapper that idempotently releases every admission claim on all exit paths,
        as the safety net around _fire_inner(). Returns False only for a pre-commit refusal."""
        try:
            return await self._fire_inner(
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
        """Extra update_schedule() fields for a threshold-alert fire; folded into the same
        call as last_fired_at/next_fire_at, only after the run row is durably persisted so a
        pre-persistence failure never consumes the cooldown silently. Only top-level fires
        (chain_depth==0) stamp it."""
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
        """Durably record one occurrence row, atomic with either the cursor advance (ordinary
        fire) or tombstoning the superseded orphan (recovery re-fire). Returns False only
        when a recovery orphan no longer qualified."""
        if supersedes_run_id is not None:
            applied = await self._svc.tombstone_and_replace_schedule_run(
                supersedes_run_id, run, expected_orphan_status="running"
            )
            if applied:
                # The write above sets only status+updated_at; layer reason/history now -- a
                # same-status append, not a CAS, since the orphan is already durably terminal.
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
        """Clean up a recovery re-fire's own invocation after its occurrence write was
        refused (the orphan it targeted no longer qualified); no schedule_run row was created."""
        _log.info(
            "Abandoning recovery re-fire for invocation %s: orphan %s was "
            "already resolved by something else",
            inv_id,
            orphan_id,
        )
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
            extra_fields={"ended_at": time.time()},
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
    ) -> bool:
        """Fire one occurrence of *schedule*. Returns False only when refused before anything
        was durably committed (caller may re-offer the trigger, since pre-dispatch refusals
        do not consume it); True once the occurrence commits, whether or not a process
        ultimately ran.

        DELIVERY CONTRACT: at-least-once up to confirmed launch, at-most-once past it. A
        crash before the occurrence commits fires fresh on restart; between commit and
        on_launched confirming dispatched_at, _recover_undispatched_fires() re-fires via
        supersedes_run_id (its CAS requires dispatched_at IS NULL, so a race-won launch makes
        the tombstone a no-op); once dispatched_at is confirmed the process is never re-fired
        -- a duplicate real-world side effect is worse than one unretried outcome.
        """
        sid = schedule["id"]
        now = time.time()
        dispatched = False  # flipped by on_launched: distinguishes "never started" from "started then failed"
        # Flipped once the occurrence commits; between it and *dispatched* a failure must
        # leave the row for startup recovery instead of finalizing it.
        occurrence_committed = False
        _tmp_path: str | None = None

        inv_id = uuid.uuid4().hex[:12]
        # Unregistered on every exit path below so a matching registration never outlives this fire.
        notify_scope = _register_schedule_notify(
            inv_id, schedule.get("notify_on"), schedule.get("notify_command")
        )
        try:
            # Record what was actually sent, not the raw {{var}} template.
            rendered_prompt = _subprocess.render_action_prompt(schedule, trigger_context)
            await self._svc.create_invocation(
                {
                    "id": inv_id,
                    "skill": f"scheduled:{schedule['name']}",
                    "plugin": schedule["trigger_type"],
                    # `is not None`, not `or`: a template can render to "", which must persist
                    # as-is rather than falling back to action_playbook.
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
            # No invocation row exists yet, so drop the registration before propagating.
            _unregister_schedule_notify(notify_scope)
            raise

        try:
            # kind='command' spawns an allow-listed executable directly, never through `li`.
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
            # Resolved ahead of the occurrence transaction because it can refuse; resolving it
            # after would durably advance the trigger past an event that never got a process.
            action_cwd = await _resolve_action_cwd(schedule)
        except Exception as exc:
            if isinstance(exc, SchedulerCwdInheritRefusedError):
                # A deliberate fail-closed refusal, not an internal error: log plainly, no stack trace.
                _setup_reason = RunReasons.FAILED_CWD_INHERIT_REFUSED
                _log.warning("Schedule fire %s (run %s): %s", schedule.get("name"), run_id, exc)
            else:
                _setup_reason = RunReasons.FAILED_EXCEPTION
                _log.exception(
                    "Invalid schedule action for %s (run %s)", schedule.get("name"), run_id
                )
            # This handler's own finally drops the notify registration after any terminal write.
            try:
                _end_time = time.time()
                next_at = self._compute_next_fire(schedule, now)
                failed_schedule_fields: dict[str, Any] = {"last_fired_at": now}
                failed_schedule_fields.update(self._next_fire_field(schedule, next_at))
                failed_schedule_fields.update(
                    self._threshold_alert_update_fields(schedule, chain_depth, now)
                )
                failed_schedule_fields.update(self._effective_timezone_fields(schedule))
                # *extra_schedule_fields* (github_poll's cursor advance) is NOT folded in here:
                # no process saw the event, so advancing past it would spend the trigger for nothing.
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
                    await self._abandon_superseded_recovery_fire(
                        inv_id, orphan_id=supersedes_run_id
                    )
                    return False
                if rate_limit_claim is not None:
                    rate_limit_claim.release()  # durable row now accounts for this fire
                if max_runs_claim is not None:
                    max_runs_claim.release()  # durable row now carries this fire's budget unit
                written = await self._svc.update_status(
                    "schedule_run",
                    run_id,
                    new_status="failed",
                    reason_code=_setup_reason,
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
                            reason_code=_setup_reason,
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
                    extra_fields={"ended_at": _end_time},
                )
                if inv_written:
                    await flush_run_telemetry(
                        self._svc, self._signal_bus, run_id=run_id, invocation_id=inv_id
                    )
                else:
                    # Another finalizer already wrote this terminal status; drop the
                    # stranded signal-bus counters instead of leaving them forever.
                    self._signal_bus.pop_run_counters(run_id)
                await self._check_max_runs(schedule, chain_depth)
                return False
            finally:
                _unregister_schedule_notify(notify_scope)
                self._discard_tmp_argv_file(_tmp_path)
        except BaseException:
            # Cancellation during action setup propagates untouched; nothing durable yet,
            # so the trigger is untouched too, but the registration must still be dropped.
            _unregister_schedule_notify(notify_scope)
            self._discard_tmp_argv_file(_tmp_path)
            raise

        try:
            next_at = self._compute_next_fire(schedule, now)
            update_fields: dict[str, Any] = {"last_fired_at": now}
            update_fields.update(self._next_fire_field(schedule, next_at))
            update_fields.update(self._threshold_alert_update_fields(schedule, chain_depth, now))
            update_fields.update(self._effective_timezone_fields(schedule))
            if extra_schedule_fields:
                update_fields.update(extra_schedule_fields)

            # Occurrence-insert + cursor-advance MUST land atomically, or a restart could
            # re-derive "still due" for an occurrence already durably recorded (double-fire).
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
                return False
            occurrence_committed = True
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

            async def _on_launched() -> None:
                # Stamps dispatched_at the instant the process is confirmed to exist -- the
                # signal that distinguishes "never launched" (safe to re-fire) from "launched".
                nonlocal dispatched
                dispatched = True
                await self._mark_dispatched(run_id)

            exit_code, stderr_tail = await _subprocess.spawn_and_wait(
                argv,
                inv_id,
                tmp_path=_tmp_path,
                cwd=action_cwd,
                action_kind=schedule.get("action_kind"),
                on_launched=_on_launched,
            )
            # on_launched already flipped this; kept here so a cancellation mid-run classifies the same way.
            dispatched = True
            end_time = time.time()
            status = "completed" if exit_code == 0 else "failed"
            if exit_code == 0:
                reason_code = RunReasons.COMPLETED_OK
                reason_summary = "Scheduled process completed successfully."
            else:
                reason_code = RunReasons.FAILED_EXIT_NONZERO
                reason_summary = f"Scheduled process exited non-zero: {exit_code}."

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
                extra_fields={
                    "exit_code": exit_code,
                    "ended_at": end_time,
                    "error_detail": stderr_tail if exit_code != 0 else None,
                },
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
            inv_written = await self._guarded_terminal_status(
                "invocation",
                inv_id,
                new_status=inv_status,
                reason_code=inv_rc,
                reason_summary=inv_rs,
                evidence_refs=inv_ev,
                source="executor",
                actor=inv_id,
                extra_fields={"ended_at": end_time},
                metadata=inv_meta,
            )
            if inv_written:
                await flush_run_telemetry(
                    self._svc, self._signal_bus, run_id=run_id, invocation_id=inv_id
                )
            else:
                # Another finalizer already wrote this terminal status; drop the signal
                # bus's per-run_id counters now instead of leaving them stranded forever.
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
            return dispatched

        except asyncio.CancelledError:
            _log.info("Schedule fire cancelled %s (run %s)", schedule.get("name"), run_id)
            if not dispatched:
                # Byte-for-byte the state a crash here leaves; writing "cancelled" would take
                # the row out of the undispatched-recovery lane while the trigger is already spent.
                _log.info(
                    "Leaving run %s undispatched for startup recovery: cancelled "
                    "before its process was launched",
                    run_id,
                )
                raise
            _end_time = time.time()
            try:
                written = await self._guarded_terminal_status(
                    "schedule_run",
                    run_id,
                    new_status="cancelled",
                    reason_code=RunReasons.CANCELLED_SYSTEM,
                    reason_summary="Schedule run cancelled by scheduler shutdown.",
                    evidence_refs=[{"kind": "schedule", "id": sid}],
                    source="executor",
                    actor=run_id,
                    extra_fields={
                        "ended_at": _end_time,
                        "error_detail": "Scheduler shutdown",
                    },
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
                    extra_fields={"ended_at": _end_time},
                )
                if inv_written:
                    await flush_run_telemetry(
                        self._svc, self._signal_bus, run_id=run_id, invocation_id=inv_id
                    )
                else:
                    # Another finalizer already wrote this terminal status; drop the
                    # stranded signal-bus counters instead of leaving them forever.
                    self._signal_bus.pop_run_counters(run_id)
                await self._check_max_runs(schedule, chain_depth)
            except Exception:
                _log.exception("Failed to record cancellation for run %s during shutdown", run_id)
            raise
        except Exception as exc:
            if occurrence_committed and not dispatched:
                # Same window the cancellation branch leaves alone; True is returned since the
                # cursor advanced and the work is queued for recovery, not lost.
                _log.exception(
                    "Schedule fire %s (run %s) failed after its occurrence "
                    "committed but before its process was launched; leaving the "
                    "run undispatched for startup recovery",
                    schedule.get("name"),
                    run_id,
                )
                return True
            if isinstance(exc, SchedulerCwdInheritRefusedError):
                # A deliberate fail-closed refusal, not an internal error: log plainly, no stack trace.
                _fire_exc_reason = RunReasons.FAILED_CWD_INHERIT_REFUSED
                _log.warning("Schedule fire %s (run %s): %s", schedule.get("name"), run_id, exc)
            else:
                _fire_exc_reason = RunReasons.FAILED_EXCEPTION
                _log.exception("Error in schedule fire %s (run %s)", schedule.get("name"), run_id)
            _end_time = time.time()
            written = await self._guarded_terminal_status(
                "schedule_run",
                run_id,
                new_status="failed",
                reason_code=_fire_exc_reason,
                reason_summary=f"{type(exc).__name__}: {exc}",
                evidence_refs=[{"kind": "schedule", "id": sid}],
                source="executor",
                actor=run_id,
                metadata={"exception_class": type(exc).__name__},
                extra_fields={
                    "ended_at": _end_time,
                    "error_detail": f"{type(exc).__name__}: {exc}",
                },
            )
            if written:
                await self._dispatch_signal(
                    build_schedule_run_signal(
                        entity_id=run_id,
                        new_status="failed",
                        reason_code=_fire_exc_reason,
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
                extra_fields={"ended_at": _end_time},
            )
            if inv_written:
                await flush_run_telemetry(
                    self._svc, self._signal_bus, run_id=run_id, invocation_id=inv_id
                )
            else:
                # Another finalizer already wrote this terminal status; drop the
                # stranded signal-bus counters instead of leaving them forever.
                self._signal_bus.pop_run_counters(run_id)
            await self._check_max_runs(schedule, chain_depth)
            return dispatched
        finally:
            _unregister_schedule_notify(notify_scope)
            if chain_depth == 0:
                self._running.pop(sid, None)
            self._discard_tmp_argv_file(_tmp_path)

    @staticmethod
    def _discard_tmp_argv_file(tmp_path: str | None) -> None:
        """Remove the flow_yaml tmp file build_argv may have written; suppress(OSError) makes
        a double-unlink (spawn_and_wait already cleaned up) safe."""
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    def _next_fire_field(self, schedule: dict, next_at: float | None) -> dict[str, float | None]:
        """Field(s) to merge into update_schedule() for *next_at*. None normally means leave
        next_fire_at untouched; for an 'at' trigger, None is the terminal answer and must be
        persisted, not omitted."""
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

                # Resolve wall-clock fields in the schedule's own declared timezone when set,
                # else the process default; get_next(float) still returns an absolute UTC epoch.
                start = datetime.fromtimestamp(
                    ref_time, tz=resolve_schedule_timezone(schedule).tzinfo
                )
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
            return None  # fires exactly once; _next_fire_field() turns this into a persisted None
        return None


scheduler = SchedulerEngine()
register_default_handlers(scheduler._signal_bus)
