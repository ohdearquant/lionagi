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
from lionagi.state.db import SESSION_TERMINAL_STATUSES, TERMINAL_RUN_STATUSES
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
    _DBSchedulerStateService,
    create_skipped_run,
    default_scheduler_state,
    flush_run_telemetry,
    resolve_invocation_terminal,
)

_log = logging.getLogger(__name__)

_MAX_CHAIN_DEPTH = 10
_TICK_INTERVAL = 30  # seconds
# Backoff between tick-loop restarts, holding at the last value. A loop that dies every
# time must not spin, and one that died once must come back before the next schedule is due.
_TICK_RESTART_BACKOFF = (1.0, 2.0, 5.0, 15.0, 30.0)
# Throttles deferred-capacity skipped-run records to one per schedule per this
# many deferrals, so sustained saturation doesn't spam schedule_runs.
_DEFERRED_RECORD_EVERY = 10
# Consecutive pre-dispatch refusals allowed for one github_poll event before it is recorded terminal
# and the cursor moves past it, so one poison event cannot block the queue.
_MAX_PREDISPATCH_REFUSALS = 3

# schedule_runs has no 'completed_empty' or 'aborted' status; _reconcile_dispatched_orphans() maps
# the invocation-vocabulary result onto the nearest schedule_run status, and the finer distinction
# survives in the written reason_code. completed_empty maps to 'failed', not 'completed': a clean
# leader exit with no completion evidence from the child is not success, and the mapped status also
# selects the signal class, so mapping it to 'completed' would mint a success signal for a run
# nothing confirms finished.
_SCHEDULE_RUN_STATUS_FROM_INVOCATION: dict[str, str] = {
    "completed": "completed",
    "completed_empty": "failed",
    "failed": "failed",
    "timed_out": "timed_out",
    "cancelled": "cancelled",
    "aborted": "cancelled",
}


def _register_schedule_notify(
    inv_id: str, notify_on: list[str] | None, notify_command: str | None
) -> str | None:
    """Register the schedule's declared ``notify`` on this fire's invocation, or None."""
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
    """One-shot handle for an in-process max_runs reservation, released once in a finally."""

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
    """One-shot handle for an in-process global concurrent-fire slot."""

    __slots__ = ("_engine", "_released")

    def __init__(self, engine: SchedulerEngine) -> None:
        self._engine = engine
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._engine._release_global_slot()


class _AdhocSlotClaim:
    """One-shot handle for an ad-hoc worker slot, kept a separate pool so neither lane starves."""

    __slots__ = ("_engine", "_released")

    def __init__(self, engine: SchedulerEngine) -> None:
        self._engine = engine
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._engine._release_adhoc_slot()


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
    """One-shot handle for an in-process threshold-alert cooldown reservation."""

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
    """The zone one schedule's cron fields are interpreted in, plus its provenance."""

    name: str
    source: str
    tzinfo: ZoneInfo


def resolve_schedule_timezone(schedule: dict) -> ScheduleTimezone:
    """Resolve the zone *schedule*'s cron expression is interpreted in; a pure read."""
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


def resolve_schedule_cadence_seconds(schedule: dict) -> float | None:
    """Fixed-period cadence *schedule* fires on, or None if it has none."""
    trigger_type = schedule.get("trigger_type")
    if trigger_type == "interval":
        return schedule.get("interval_sec")
    if trigger_type == "github_poll":
        return schedule.get("poll_interval_sec") or schedule.get("interval_sec") or 300
    return None


class SchedulerCwdInheritRefusedError(RuntimeError):
    """A schedule with an explicit execution root resolved none of its directories."""

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
    """A usable execution root is an existing absolute directory."""
    if not root:
        return False
    path = Path(root)
    return path.is_absolute() and path.is_dir()


async def _resolve_action_cwd(schedule: dict) -> str | None:
    """Resolve the working directory for a scheduled subprocess spawn."""
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
        # Present-but-empty root: the truthiness check above is deliberate,
        # since Path("") is Path(".") and would otherwise pass as usable.
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
        # Gate on `is not None`, not truthiness, so a present-but-empty root fails closed here too
        # rather than falling into the ownerless branch below.
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
        # Single-flight tracked task for the ad-hoc worker pass: a slow pass must not block schedule
        # evaluation, and a new tick must never start a second overlapping one.
        self._worker_task: asyncio.Task | None = None
        self._running: dict[str, str] = {}  # schedule_id -> run_id
        self._stopping = False
        self._fire_tasks: set[asyncio.Task] = set()
        self._last_reaper_run: float = 0.0
        self._last_checkpoint_run: float = 0.0
        # Starts unresolved rather than at 0.0, which would make a prune due on the first tick. It
        # resolves from when a prune last committed, so restarting neither triggers a pass nor
        # postpones an overdue one. A cheap gate on the tick, not the decision.
        self._last_retention_run: float | None = None
        # Single-flight tracked task for the retention prune: the sweep's cost scales with whatever
        # has accumulated, so awaiting it on the tick would hold up dispatch delivery and schedule
        # evaluation.
        self._retention_task: asyncio.Task | None = None
        # max_runs budget reservation (single-process; see _reserve_max_runs_budget).
        self._max_runs_lock = asyncio.Lock()
        self._max_runs_inflight: dict[
            str, int
        ] = {}  # schedule_id -> claimed-not-yet-terminal count
        # Rolling-window reservations bridge the admission-read to terminal-row window, so
        # concurrent paths cannot all observe the same count and overshoot max_fires.
        self._rate_limit_lock = asyncio.Lock()
        self._rate_limit_inflight: dict[str, dict[str, float]] = {}
        # Global concurrent-fire cap, scoped to SCHEDULED fires only; the ad-hoc task-worker lane
        # has its own cap below so the two cannot starve each other.
        self._global_slot_lock = asyncio.Lock()
        self._global_inflight = 0
        # Ad-hoc task-worker concurrency cap, independent of MAX_SCHEDULED_CONCURRENT by design.
        self._adhoc_slot_lock = asyncio.Lock()
        self._adhoc_inflight = 0
        self._deferred_log_counts: dict[str, int] = {}  # schedule_id -> deferrals since last record
        # Threshold-alert cooldown reservations. Membership means a fire for this schedule's current
        # breach is in flight or was just reserved, which closes a race a DB-only last_alert_at
        # check cannot.
        self._threshold_pending: set[str] = set()
        # ADR-0071 D4: this daemon process is the one host worker (v1).
        self._task_worker_id = f"host:{uuid.uuid4().hex[:8]}"
        # Tick-loop supervision. The loop advancing is the only thing that makes this a
        # scheduler, and the process staying up says nothing about whether it still is.
        self._tick_loop_restarts = 0
        self._last_tick_loop_failure: tuple[float, str] | None = None

    async def start(self) -> None:
        _log.info("Scheduler engine starting")
        self._stopping = False
        self._log_scheduler_timezone()
        await self._backfill_action_cwd()
        await self._stamp_effective_timezones()
        await self._recompute_armed_cron_schedules()
        self._tick_loop_restarts = 0
        self._task = self._spawn_tick_loop()

    def _spawn_tick_loop(self) -> asyncio.Task:
        task = asyncio.create_task(self._tick_loop())
        task.add_done_callback(self._on_tick_loop_done)
        return task

    def _on_tick_loop_done(self, task: asyncio.Task) -> None:
        """Restart the loop on any exit that is not stop(), including a clean return."""
        if self._stopping or task is not self._task:
            return
        if task.cancelled():
            reason = "cancelled"
        elif (exc := task.exception()) is not None:
            reason = f"{type(exc).__name__}: {exc}"
        else:
            reason = "returned while still running"
        self._tick_loop_restarts += 1
        self._last_tick_loop_failure = (time.time(), reason)
        delay = _TICK_RESTART_BACKOFF[min(self._tick_loop_restarts, len(_TICK_RESTART_BACKOFF)) - 1]
        _log.error(
            "Scheduler tick loop ended (%s); restart %d in %.0fs",
            reason,
            self._tick_loop_restarts,
            delay,
        )
        self._task = asyncio.create_task(self._restart_tick_loop(delay))

    async def _restart_tick_loop(self, delay: float) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(delay)
        if self._stopping:
            return
        self._task = self._spawn_tick_loop()

    def _log_scheduler_timezone(self) -> None:
        """Say the effective cron timezone out loud, once, at startup."""
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
        """Record on every cron row the zone it is interpreted in and how it was resolved."""
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
        """The columns recording how *schedule*'s fire times were resolved; write-only."""
        if schedule.get("trigger_type") != "cron" or not schedule.get("cron_expr"):
            return {}
        resolution = resolve_schedule_timezone(schedule)
        return {
            "effective_timezone": resolution.name,
            "effective_timezone_source": resolution.source,
        }

    async def _backfill_action_cwd(self) -> None:
        """One-shot startup backfill giving pre-migration schedules a persisted execution root."""
        try:
            schedules = await self._svc.list_schedules()
        except Exception:
            _log.exception("Failed to load schedules for startup action_cwd backfill")
            return
        for s in schedules:
            # ``is not None``, not truthiness: a present-but-empty action_cwd is an execution root
            # the schedule supplied, which the resolver fails closed on. Backfilling it would hand
            # that row a different directory by a side door.
            if s.get("action_cwd") is not None or not s.get("action_project"):
                continue
            try:
                from lionagi.studio.services.projects import get_project

                project = await get_project(s["action_project"])
                path = project.get("path") if project else None
                # Same usability rule as the resolver: a relative path would persist a root meaning
                # 'wherever the daemon started', which can never resolve.
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
        """Re-resolve every enabled cron schedule's next_fire_at before the tick loop starts."""
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
        """Recompute and persist a cron schedule's next_fire_at, logging only if it shifts."""
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
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        if self._retention_task is not None:
            # Cancelling mid-prune keeps whichever chunks committed and writes no event, so the next
            # process reads the older prune and is due straight away.
            self._retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._retention_task
            self._retention_task = None
        if self._fire_tasks:
            for ft in list(self._fire_tasks):
                ft.cancel()
            await asyncio.gather(*self._fire_tasks, return_exceptions=True)
            self._fire_tasks.clear()
        if isinstance(self._svc, _DBSchedulerStateService):
            await self._svc.close()

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
        for recovery in (
            self._recover_undispatched_fires,
            self._reconcile_dispatched_orphans,
            self._check_missed_fires,
        ):
            # Startup recovery is best-effort: one failing pass must not cost every later tick.
            try:
                await recovery()
            except Exception:
                _log.exception("Scheduler startup recovery failed in %s", recovery.__name__)
        while not self._stopping:
            try:
                await self._tick()
                await asyncio.sleep(_TICK_INTERVAL)
            except asyncio.CancelledError:
                # stop() cancels this task, so a cancel while stopping IS the shutdown. A cancel
                # at any other time escaped from something the tick awaited, and ending the loop
                # over it is how the scheduler goes quiet while the process keeps answering.
                if self._stopping:
                    raise
                _log.exception("Scheduler tick cancelled without a stop; continuing")
            except Exception:
                _log.exception("Scheduler tick error")
                await asyncio.sleep(_TICK_INTERVAL)

    def _maybe_start_prune(self, now: float) -> None:
        """Start the retention prune as a tracked, single-flight background task."""
        from lionagi.studio.config import RETENTION_INTERVAL_SECONDS

        if RETENTION_INTERVAL_SECONDS <= 0:
            return
        if self._retention_task is not None and not self._retention_task.done():
            return
        if (
            self._last_retention_run is not None
            and now - self._last_retention_run < RETENTION_INTERVAL_SECONDS
        ):
            return
        self._retention_task = asyncio.create_task(self._run_prune_guarded(now))

    async def _run_prune_guarded(self, now: float) -> None:
        try:
            await self._run_prune(now)
        except Exception:
            _log.exception("Periodic retention prune error")

    async def _run_prune(self, now: float) -> None:
        """Prune once, if a full interval has passed since a prune last committed."""
        from lionagi.studio.config import RETENTION_INTERVAL_SECONDS
        from lionagi.studio.services.db_maintenance import get_last_prune_at, prune_old_data

        try:
            recorded = await get_last_prune_at()
        except Exception:
            # Leave the anchor as it was so the next tick tries again. Anchoring on a failed read
            # would either silence the pass or run it early.
            _log.exception("Could not read the last prune time; retrying next tick")
            return

        if recorded is None and self._last_retention_run is None:
            # Nothing has ever been pruned. Start the clock now rather than firing immediately, so
            # adopting this with a large backlog gets a predictable first pass one interval out.
            self._last_retention_run = now
            return

        # Judged against the tick's own reading, the one the gate used. The completion stamp below
        # is the only place a fresh reading belongs, because that is the value the tick cannot know.
        anchor = max(recorded or 0.0, self._last_retention_run or 0.0)
        self._last_retention_run = anchor
        if now - anchor < RETENTION_INTERVAL_SECONDS:
            return

        try:
            await prune_old_data(actor="scheduler_tick")
        finally:
            # Stamped from completion rather than from the tick that started this, and on failure
            # too, matching the reaper and checkpoint passes: a prune that keeps failing must not be
            # retried every tick.
            self._last_retention_run = time.time()

    async def _mark_dispatched(self, run_id: str) -> None:
        """Stamp ``dispatched_at`` the instant spawn_and_wait confirms the process exists."""
        await self._svc.update_schedule_run(run_id, dispatched_at=time.time())

    async def _recover_undispatched_fires(self) -> None:
        """Startup scan for occurrences that committed but whose launch was never confirmed."""
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
        """CAS-tombstone an undispatched orphan that has no replacement to follow."""
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
            # Raced with something else finalizing this row (e.g. the
            # stale-run reaper) between the scan and here; already resolved.
            pass

    async def _reconcile_dispatched_orphans(self) -> None:
        """Startup reconciliation for dispatched rows that never reached a terminal status."""
        try:
            rows = await self._svc.list_dispatched_running_schedule_runs()
        except Exception:
            _log.exception("Failed to scan for dispatched-but-unterminated schedule_runs")
            return

        for row in rows:
            run_id = row["id"]
            sid = row.get("schedule_id")
            inv_id = row.get("invocation_id")
            if not inv_id:
                continue
            try:
                sessions = await self._svc.list_sessions_for_invocation(inv_id)
            except Exception:
                _log.exception(
                    "Failed to list sessions for invocation %s (schedule_run %s)", inv_id, run_id
                )
                continue
            if not sessions:
                continue
            child_statuses = [str(s.get("status") or "") for s in sessions]
            if any(s not in SESSION_TERMINAL_STATUSES for s in child_statuses):
                continue  # at least one child still genuinely non-terminal

            inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                self._svc, inv_id, fallback_status="completed"
            )
            run_status = _SCHEDULE_RUN_STATUS_FROM_INVOCATION.get(inv_status)
            if run_status is None:
                _log.warning(
                    "Unmapped invocation status %r reconciling schedule_run %s; leaving as-is",
                    inv_status,
                    run_id,
                )
                continue

            end_time = time.time()
            chain_depth = row.get("chain_depth") or 0
            trigger_context = row.get("trigger_context") or {}
            action_kind = row.get("action_kind") or ""

            # The guarded CAS below is the idempotency boundary against another finalizer; losing it
            # means someone else already owns this row's follow-on effects, so this pass does
            # nothing further.
            written = await self._guarded_terminal_status(
                "schedule_run",
                run_id,
                new_status=run_status,
                reason_code=inv_rc,
                reason_summary=(
                    f"{inv_rs} (reconciled at scheduler startup from child session "
                    "evidence; the scheduler that dispatched this run did not "
                    "record its outcome)."
                ),
                evidence_refs=inv_ev,
                source="system",
                actor="scheduler_startup_reconciliation",
                metadata={"invocation_id": inv_id, "invocation_status": inv_status},
                extra_fields={"ended_at": end_time},
            )
            if not written:
                continue
            _log.info(
                "Reconciled dispatched-orphan schedule_run %s as %s from child "
                "session evidence (invocation %s)",
                run_id,
                run_status,
                inv_id,
            )
            await self._dispatch_signal(
                build_schedule_run_signal(
                    entity_id=run_id,
                    new_status=run_status,
                    reason_code=inv_rc,
                    schedule_id=sid or "",
                    action_kind=action_kind,
                    chain_depth=chain_depth,
                    trigger_context=trigger_context,
                )
            )

            # Finalize the linked invocation too, or it stays 'running' forever and every normal
            # terminal-invocation side effect never fires for a run this pass just marked terminal.
            inv_written = await self._guarded_terminal_status(
                "invocation",
                inv_id,
                new_status=inv_status,
                reason_code=inv_rc,
                reason_summary=inv_rs,
                evidence_refs=inv_ev,
                source="system",
                actor="scheduler_startup_reconciliation",
                metadata=inv_meta,
                extra_fields={"ended_at": end_time},
            )
            if inv_written:
                await flush_run_telemetry(
                    self._svc, self._signal_bus, run_id=run_id, invocation_id=inv_id
                )
            else:
                self._signal_bus.pop_run_counters(run_id)

            schedule = await self._svc.get_schedule(sid) if sid else None
            if schedule is None:
                continue
            await self._check_max_runs(schedule, chain_depth)
            if chain_depth < _MAX_CHAIN_DEPTH:
                chain_action = None
                if run_status == "completed" and schedule.get("on_success"):
                    chain_action = schedule["on_success"]
                elif run_status != "completed" and schedule.get("on_fail"):
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
                        "parent_status": run_status,
                    }
                    chain_run_id = uuid.uuid4().hex[:12]
                    self._tracked_fire(
                        chain_schedule,
                        chain_run_id,
                        trigger_context=chain_ctx,
                        chain_parent_id=run_id,
                        chain_depth=chain_depth + 1,
                    )

    async def _check_missed_fires(self) -> None:
        try:
            schedules = await self._svc.list_schedules(enabled=True)
            now = time.time()
            for s in schedules:
                if s.get("trigger_type") == "github_poll":
                    # github_poll's cadence is last_fired_at + poll_interval_sec, not next_fire_at,
                    # so a stale or legacy-persisted value here is not a missed scheduled
                    # occurrence.
                    continue
                next_fire_at = s.get("next_fire_at")
                if next_fire_at is None or next_fire_at > now:
                    continue
                # A schedule_run already recorded for this occurrence means the slot was handled;
                # firing again would double-execute the action, so advance the cursor past it
                # instead.
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
        """Queue exactly one recovery fire for a past-due run_once schedule."""
        # Admission claims first, then the next_fire_at reserve: a refusal must leave the row still
        # due, and clearing an 'at' trigger's next_fire_at before a refusal would strand its single
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
            # _next_fire_field, not a bare not-None check: an 'at' trigger's terminal None must be
            # reserved too, or the next tick still sees the past-due instant and queues a duplicate.
            fields = self._next_fire_field(schedule, next_at)
            if fields:
                try:
                    await self._svc.update_schedule(schedule["id"], **fields)
                except Exception:
                    # Reserve didn't land: skip recovery and let the normal
                    # tick own this cycle's fire instead of double-running it.
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

        self._maybe_start_prune(now)

        try:
            await self._deliver_due_dispatches(now)
        except Exception:
            _log.exception("Dispatch outbox delivery scan error")

        self._maybe_start_worker_pass(now)

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
        """Scan due dispatch_outbox rows and attempt delivery; not interval-gated."""
        from lionagi.dispatch import deliver_due_dispatches
        from lionagi.state.db import StateDB

        async with StateDB() as db:
            await deliver_due_dispatches(db, now=now)

    def _maybe_start_worker_pass(self, now: float) -> None:
        """Start the ad-hoc task-worker pass as a tracked, single-flight background task."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(self._run_task_worker_tick_guarded(now))

    async def _run_task_worker_tick_guarded(self, now: float) -> None:
        try:
            await self._run_task_worker_tick(now)
        except Exception:
            _log.exception("Task worker tick error")

    async def _run_task_worker_tick(self, now: float) -> None:
        """Reap lapsed leases and claim/execute eligible host task applications."""
        from lionagi.state.db import StateDB
        from lionagi.studio.scheduler import worker as _worker

        if not _worker.TASK_WORKER_ENABLED:
            return

        def _release(claim: Any) -> None:
            if claim is not None:
                claim.release()

        async with StateDB() as db:
            await _worker.worker_tick(
                db,
                worker_id=self._task_worker_id,
                now=now,
                reserve_slot=self._reserve_adhoc_slot,
                release_slot=_release,
            )

    async def _tick_github(self, schedule: dict, now: float) -> None:
        poll_interval = resolve_schedule_cadence_seconds(schedule)
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

        # Reserve one global slot before polling, so a no-slot poll does not fetch, advance the
        # cursor and discard. This slot goes to whichever event fires first; later ones reserve
        # their own.
        slot_allowed, pre_slot_claim = await self._reserve_global_slot()
        if not slot_allowed:
            if pre_rate_claim is not None:
                pre_rate_claim.release()
            await self._maybe_record_deferred(schedule, now)
            return

        from .github import github_poll

        sid = schedule["id"]
        # Every await between reserving pre_slot_claim and handing it to the first dispatched
        # _fire() must release it on failure, or a transient error mid-poll leaks the slot
        # permanently. It is nulled the moment it is handed off or released inline, so this finally
        # only fires for the untouched case.
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

            # Observer self-health: stamp the health columns from this poll's outcome whether or not
            # it returned items, so a healthy-empty poll resets the blind clock exactly like one
            # that found PRs and a quiet repo never false-alarms. An error leaves both columns
            # untouched, so the age metric climbs on its own.
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
                    # Filtered-out PRs consume no budget, and the cursor can always advance past
                    # them so they are not re-listed forever.
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
                        # Advances github_cursor inside the same atomic transaction as this event's
                        # occurrence insert, durably before the action runs, closing the double-fire
                        # hazard of batching the cursor write until after the loop.
                        extra_schedule_fields={"github_cursor": item.updated_at},
                    )
                    if not fired:
                        # A refusal before a process started means nothing ran, so re-offering the
                        # event is not a re-execution. Bounded, because a refusal can be a property
                        # of this one event rather than the schedule, and holding the cursor forever
                        # would block every later event.
                        refusals = await self._record_predispatch_refusal(schedule, item.updated_at)
                        if refusals < _MAX_PREDISPATCH_REFUSALS:
                            # Stop rather than trying the rest: if the cause is the schedule, later
                            # events refuse identically and each burns a budget unit doing so.
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
                        # The advance rides the trailing batched write below, since the refusing
                        # fire wrote its failed run row without a cursor advance; a crash here just
                        # re-offers the event.
                        continue
                    await self._clear_predispatch_refusals(schedule)
                    # Tracked locally for the batched trailing write below, and idempotent if this
                    # event's own fire already persisted the same cursor value.
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

            # Safety-net batched write: every dispatched event already advanced github_cursor
            # atomically with its own occurrence insert. This only still does work when the loop
            # ends on non-dispatched or filtered items, or when nothing fired at all, both no-
            # occurrence cases with nothing to be atomic with.
            if cursor != schedule.get("github_cursor"):
                # guard_cursor_forward: this value derives from the snapshot read at tick start, so
                # it must not undo a cursor an operator moved forward while the poll was in flight.
                await self._svc.update_schedule(
                    sid, github_cursor=cursor, guard_cursor_forward=True
                )
        finally:
            if pre_rate_claim is not None:
                pre_rate_claim.release()
            if pre_slot_claim is not None:
                pre_slot_claim.release()

    async def _record_predispatch_refusal(self, schedule: dict, event_cursor: str) -> int:
        """Count one pre-dispatch refusal of the event at *event_cursor*; returns the new total."""
        prior = schedule.get("predispatch_refusal_count") or 0
        if schedule.get("predispatch_refusal_event") != event_cursor:
            prior = 0
        count = prior + 1
        await self._svc.update_schedule(
            schedule["id"],
            predispatch_refusal_event=event_cursor,
            predispatch_refusal_count=count,
        )
        # Keep this tick's snapshot in step, so a second refusal within the
        # same poll counts from the value just written.
        schedule["predispatch_refusal_event"] = event_cursor
        schedule["predispatch_refusal_count"] = count
        return count

    async def _clear_predispatch_refusals(self, schedule: dict) -> None:
        """Drop the pre-dispatch refusal streak once the cursor moves past the event."""
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
        """Atomically claim one top-level fire against ``max_runs``; returns (allowed, claim)."""
        max_runs = schedule.get("max_runs")
        if not max_runs:
            return True, None
        sid = schedule["id"]
        async with self._max_runs_lock:
            inflight = self._max_runs_inflight.get(sid, 0)
            # Persisted 'running' rows count budget alongside terminal ones, since a fire spends
            # budget when it fires rather than when it resolves. Claims cover only the pre-commit
            # window, so summing the two counts each fire exactly once.
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
        """Reserve one fire inside the schedule's rolling time window."""
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
        """Atomically claim one global concurrent-fire slot; returns (allowed, claim)."""
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

    async def _reserve_adhoc_slot(self) -> tuple[bool, _AdhocSlotClaim | None]:
        """Atomically claim one ad-hoc worker slot from its own independent counter."""
        from lionagi.studio.config import MAX_ADHOC_CONCURRENT

        if MAX_ADHOC_CONCURRENT <= 0:
            return True, None
        async with self._adhoc_slot_lock:
            if self._adhoc_inflight >= MAX_ADHOC_CONCURRENT:
                return False, None
            self._adhoc_inflight += 1
            return True, _AdhocSlotClaim(self)

    def _release_adhoc_slot(self) -> None:
        self._adhoc_inflight = max(0, self._adhoc_inflight - 1)

    async def _maybe_record_deferred(self, schedule: dict, now: float) -> None:
        """Emit a throttled skipped-run record for a capacity-deferred fire."""
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
        """True if the schedule exhausted its spend budget: a pre-fire gate, not an interrupt."""
        budget_usd = schedule.get("budget_usd")
        budget_tokens = schedule.get("budget_tokens")
        if not budget_usd and not budget_tokens:
            return False
        spend = await self._svc.sum_schedule_spend(schedule["id"])
        if spend.get("unreported_sessions"):
            _log.warning(
                "Schedule %s (%s) budget check: %d spawned session(s) never reported "
                "cost; observed cost_usd=%.4f/tokens=%d may undercount actual spend.",
                schedule.get("name"),
                schedule["id"],
                spend["unreported_sessions"],
                spend["cost_usd"],
                spend["tokens"],
            )
        if budget_usd and spend["cost_usd"] >= budget_usd:
            return True
        if budget_tokens and spend["tokens"] >= budget_tokens:
            return True
        return False

    async def _disable_for_budget_exhausted(self, schedule: dict, now: float) -> None:
        """Auto-disable a schedule that exhausted its spend budget, recording why."""
        try:
            spend = await self._svc.sum_schedule_spend(schedule["id"])
        except Exception:
            _log.warning(
                "Could not re-read the spend rollup for schedule %s while "
                "disabling it for budget exhaustion; recording the "
                "unreported-session count as unknown",
                schedule["id"],
                exc_info=True,
            )
            spend = {}
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
                "unreported_sessions": spend.get("unreported_sessions"),
            },
        )
        await self._svc.update_schedule(schedule["id"], enabled=0)

    async def _evaluate_threshold_breach(self, schedule: dict, now: float) -> dict[str, Any] | None:
        """Evaluate ``threshold_config`` against live metrics; None when in bounds."""
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
        breach: dict[str, Any] = {
            "metric": metric,
            "op": op,
            "value": observed,
            "threshold": threshold_value,
            "window_minutes": window_minutes,
        }
        # total_cost_usd reads an unreported session as $0, so surface how many sessions in this
        # window carried no cost data at all, and a breach or its absence is not mistaken for a
        # complete reading.
        unreported = await self._svc.metric_unreported_sessions(metric, window_start)
        if unreported:
            breach["unreported_sessions"] = unreported
            breach["spend_is_partial"] = True
        return breach

    async def _record_evaluation_without_firing(self, schedule: dict, now: float) -> None:
        """Record a completed evaluation and advance next_fire_at, firing nothing."""
        next_at = self._compute_next_fire(schedule, now)
        fields: dict[str, float] = {"last_evaluated_at": now}
        if next_at:
            fields["next_fire_at"] = next_at
        await self._svc.update_schedule(schedule["id"], **fields)

    async def _mark_threshold_evaluated(self, schedule: dict, now: float) -> None:
        """Stamp the liveness watermark for an evaluation that found a breach."""
        await self._svc.update_schedule(schedule["id"], last_evaluated_at=now)

    async def _maybe_fire(self, schedule: dict, now: float) -> None:
        threshold_extra: dict[str, Any] | None = None
        threshold_claim: _ThresholdCooldownClaim | None = None
        if schedule.get("threshold_config"):
            breach = await self._evaluate_threshold_breach(schedule, now)
            if breach is None:
                await self._record_evaluation_without_firing(schedule, now)
                return
            # Cooldown: suppress refiring while still within the metric's own window of the last
            # alert, so a sustained breach does not fire every tick. The cadence advances
            # underneath, so the next tick re-checks once it lapses.
            cooldown_sec = breach["window_minutes"] * 60
            sid = schedule["id"]
            last_alert_at = schedule.get("last_alert_at")
            in_cooldown = last_alert_at is not None and now - last_alert_at < cooldown_sec
            # in_pending closes the race last_alert_at alone cannot: a fire reserved by an earlier
            # tick whose durable stamp has not landed still reads as out of cooldown. This gate and
            # the reservation below it are both synchronous, so no second tick can slip between
            # them.
            if in_cooldown or sid in self._threshold_pending:
                await self._record_evaluation_without_firing(schedule, now)
                return
            self._threshold_pending.add(sid)
            threshold_claim = _ThresholdCooldownClaim(self, sid)
            threshold_extra = breach

        # Every await from here through _tracked_fire() must release threshold_claim, and the other
        # claims once reserved, on failure; a raise mid-gate would leak the reservation and mute the
        # alert until restart. handed_off flips True only once _tracked_fire() has launched and
        # taken ownership.
        rate_claim: _RateLimitClaim | None = None
        claim: _MaxRunsClaim | None = None
        slot_claim: _GlobalSlotClaim | None = None
        handed_off = False
        try:
            if threshold_extra is not None:
                # Every remaining outcome returns through a path of its own, so the watermark is
                # stamped here once, ahead of all of them. It sits inside the try because the
                # cooldown reservation is already held, and a failure writing the watermark has to
                # give that back.
                await self._mark_threshold_evaluated(schedule, now)

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
                # Leave next_fire_at untouched so the next tick retries this schedule rather than
                # skipping it. The claims are given back by the finally below: this defers the fire,
                # it does not consume a run or abandon the cooldown.
                return

            run_id = uuid.uuid4().hex[:12]
            ctx = {
                "scheduled": True,
                "fired_at": now,
                "next_fire_at": schedule.get("next_fire_at"),
            }
            if threshold_extra:
                ctx.update(threshold_extra)
                # last_alert_at is NOT stamped here. Every gate above has passed, but _fire_inner()
                # can still fail before persisting any schedule_run row, and stamping this early
                # would consume the cooldown with no durable record an alert was ever attempted, the
                # exact silent loss this feature prevents. The in-process threshold_claim closes the
                # duplicate-fire race meanwhile.
            self._tracked_fire(
                schedule,
                run_id,
                trigger_context=ctx,
                rate_limit_claim=rate_claim,
                max_runs_claim=claim,
                global_slot_claim=slot_claim,
                threshold_cooldown_claim=threshold_claim,
            )
            # Flipped only after _tracked_fire() returns, so even a synchronous task-launch failure
            # releases the claims below. Release is idempotent, so there is no double-free against
            # _fire()'s own finally.
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
        """Write a terminal status guarded on the row still being ``running``."""
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
        """Emit *signal* on the scheduler's signal bus, recording handler failures."""
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
        """Auto-disable a schedule once its top-level fired runs reach ``max_runs``."""
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
        """Thin wrapper that releases every admission claim on all exit paths."""
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
        """Extra ``update_schedule()`` fields stamping ``last_alert_at`` for a threshold fire."""
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
        """Durably record one occurrence row: the choke point both write sites take."""
        if supersedes_run_id is not None:
            applied = await self._svc.tombstone_and_replace_schedule_run(
                supersedes_run_id, run, expected_orphan_status="running"
            )
            if applied:
                # The atomic write above sets only status and updated_at, so layer the reason code
                # and history on now, the same pattern create_schedule_run_and_advance()'s callers
                # follow. A same-status append, not a CAS: the orphan is already durably terminal.
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
        """Clean up a recovery re-fire whose occurrence write was refused."""
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
        """Fire one occurrence of *schedule*; False only if it refused before anything committed."""
        sid = schedule["id"]
        now = time.time()
        dispatched = False  # set by on_launched once the OS process is confirmed to exist
        occurrence_committed = False  # set once the occurrence transaction commits
        _tmp_path: str | None = None

        inv_id = uuid.uuid4().hex[:12]
        # Registered before the invocation can reach a terminal status and unregistered on every
        # exit path below, so a matching registration never outlives this fire.
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
                    # An explicit None check, not `or`: a template can render to "", which
                    # build_argv sends to the child as-is, and falling back on an empty-but-rendered
                    # prompt would persist a value differing from what was sent.
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
            # kind='command' spawns an allow-listed executable directly rather than through `li`, so
            # resolving `li` is unnecessary and would wrongly block such a fire on a host where `li`
            # is unresolvable.
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
            # Resolved ahead of the occurrence transaction precisely because it can refuse: a
            # schedule whose configured root no longer exists raises rather than run under a
            # substituted directory, and resolving it after the transaction would durably advance
            # the trigger past an event that never got a process. It is a read, so it is safe before
            # anything commits.
            action_cwd = await _resolve_action_cwd(schedule)
        except Exception as exc:
            if isinstance(exc, SchedulerCwdInheritRefusedError):
                # A deliberate fail-closed refusal, not an internal error: the message already names
                # the configured root and the daemon directory that would have been substituted, so
                # log it without a stack trace.
                _setup_reason = RunReasons.FAILED_CWD_INHERIT_REFUSED
                _log.warning("Schedule fire %s (run %s): %s", schedule.get("name"), run_id, exc)
            else:
                _setup_reason = RunReasons.FAILED_EXCEPTION
                _log.exception(
                    "Invalid schedule action for %s (run %s)", schedule.get("name"), run_id
                )
            # The notify unregister lives in this handler's own finally, so every exit drops the
            # registration and any terminal write that does land happens inside the try, before the
            # unregister.
            try:
                _end_time = time.time()
                next_at = self._compute_next_fire(schedule, now)
                failed_schedule_fields: dict[str, Any] = {"last_fired_at": now}
                failed_schedule_fields.update(self._next_fire_field(schedule, next_at))
                failed_schedule_fields.update(
                    self._threshold_alert_update_fields(schedule, chain_depth, now)
                )
                failed_schedule_fields.update(self._effective_timezone_fields(schedule))
                # *extra_schedule_fields*, the github_poll cursor advance, is deliberately NOT
                # folded in here. This handler runs only for refusals raised before anything was
                # dispatched, so advancing past the event would spend the trigger on a run that did
                # nothing. last_fired_at and next_fire_at still move, so a cron schedule does not
                # spin, and the next poll re-offers the event.
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
                    # Abandon writes the invocation's cancelled terminal status, and the finally
                    # unregisters only after it, so a declared notify still fires.
                    await self._abandon_superseded_recovery_fire(
                        inv_id, orphan_id=supersedes_run_id
                    )
                    return False
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
                    # Another finalizer already wrote this invocation's terminal status, so no flush
                    # happens here, but a schedule_run signal was still minted. Drop its counters
                    # rather than leaving them in the bus's per-run_id map forever.
                    self._signal_bus.pop_run_counters(run_id)
                # last_fired_at/next_fire_at already landed atomically with
                # the occurrence insert above.
                await self._check_max_runs(schedule, chain_depth)
                return False
            finally:
                _unregister_schedule_notify(notify_scope)
                self._discard_tmp_argv_file(_tmp_path)
        except BaseException:
            # Cancellation during action setup is not an invalid action, so propagate it untouched;
            # nothing is durable yet, so the trigger is too. This window sits before the main
            # try/finally, so the registration is dropped here.
            _unregister_schedule_notify(notify_scope)
            self._discard_tmp_argv_file(_tmp_path)
            raise

        # Ensure the flow_yaml tmp file is removed on any exception or
        # cancellation in the DB ops below, before spawn_and_wait() runs.
        try:
            next_at = self._compute_next_fire(schedule, now)
            update_fields: dict[str, Any] = {"last_fired_at": now}
            update_fields.update(self._next_fire_field(schedule, next_at))
            update_fields.update(self._threshold_alert_update_fields(schedule, chain_depth, now))
            update_fields.update(self._effective_timezone_fields(schedule))
            if extra_schedule_fields:
                update_fields.update(extra_schedule_fields)

            # Occurrence insert and cursor advance MUST land atomically: a crash between two
            # independently-committed writes is exactly what let a restart re-derive 'still due' for
            # an occurrence already durably recorded. spawn_and_wait() always runs after this
            # transaction commits, so a crash before it can at worst discard an occurrence that was
            # never recorded; a crash after the commit but before launch is the delivery contract's
            # second window, handled at the next startup.
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
                # Stamps dispatched_at the instant the OS process is confirmed to exist, the signal
                # that tells 'committed but never launched', safe to re-fire, apart from 'launched,
                # outcome merely lost'. The local flag is the same distinction in memory, for exit
                # paths running before a restart could read the column.
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
            # spawn_and_wait returns only once it has an exit code, so a process existed;
            # on_launched flips the flag earlier so a mid-run cancellation classifies the same.
            dispatched = True
            end_time = time.time()

            # Resolved BEFORE the schedule_run write, so that write and the signal, telemetry and
            # chain decisions after it all agree with the invocation's own resolved outcome rather
            # than the leader's raw exit_code. A clean exit whose child never independently reached
            # a terminal status resolves to 'completed_empty', which this scheduler treats as not
            # success, so it cannot pick the status, signal or chain action a genuine completion
            # would.
            exit_status = "completed" if exit_code == 0 else "failed"
            inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                self._svc, inv_id, fallback_status=exit_status, exit_code=exit_code
            )
            success = inv_status == "completed"
            status = _SCHEDULE_RUN_STATUS_FROM_INVOCATION.get(inv_status, exit_status)
            if success:
                reason_code = RunReasons.COMPLETED_OK
                reason_summary = "Scheduled process completed successfully."
            else:
                reason_code = inv_rc
                reason_summary = inv_rs

            written = await self._guarded_terminal_status(
                "schedule_run",
                run_id,
                new_status=status,
                reason_code=reason_code,
                reason_summary=reason_summary,
                evidence_refs=[{"kind": "invocation", "id": inv_id}],
                source="executor",
                actor=run_id,
                metadata={"exit_code": exit_code, "invocation_status": inv_status},
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
                # Another finalizer already wrote this invocation's terminal status, so no flush
                # happens here, but a schedule_run signal was still minted. Drop its counters rather
                # than leaving them in the bus's per-run_id map forever.
                self._signal_bus.pop_run_counters(run_id)
            await self._check_max_runs(schedule, chain_depth)

            if chain_depth < _MAX_CHAIN_DEPTH:
                chain_action = None
                if success and schedule.get("on_success"):
                    chain_action = schedule["on_success"]
                elif not success and schedule.get("on_fail"):
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
                # Cancelled after the occurrence committed but before any process existed, byte-for-
                # byte the state a crash in that window leaves: 'running' with dispatched_at still
                # NULL. Writing a terminal 'cancelled' here would take the row out of the recovery
                # lane while the cursor has already advanced, so the trigger would be spent on a run
                # that never started anything.
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
                    # Another finalizer already wrote this invocation's terminal status, so no flush
                    # happens here, but a schedule_run signal was still minted. Drop its counters
                    # rather than leaving them in the bus's per-run_id map forever.
                    self._signal_bus.pop_run_counters(run_id)
                await self._check_max_runs(schedule, chain_depth)
            except Exception:
                _log.exception("Failed to record cancellation for run %s during shutdown", run_id)
            raise
        except Exception as exc:
            if occurrence_committed and not dispatched:
                # Failed in the same window the cancellation branch above leaves alone, reachable by
                # any awaited call in it including the pre-launch running-status write. Finalizing
                # the row here would take it out of the undispatched-recovery lane while the trigger
                # is already spent, so nothing would ever run for this event. The caller is told the
                # trigger was consumed, because the cursor did advance and the work is queued for
                # recovery, not refused.
                _log.exception(
                    "Schedule fire %s (run %s) failed after its occurrence "
                    "committed but before its process was launched; leaving the "
                    "run undispatched for startup recovery",
                    schedule.get("name"),
                    run_id,
                )
                return True
            if isinstance(exc, _subprocess.SubprocessDeadlineExceededError):
                _fire_terminal_status = "timed_out"
                _fire_exc_reason = RunReasons.TIMED_OUT_DEADLINE
                _fire_reason_summary = str(exc)
                _log.warning(
                    "Schedule fire %s (run %s) exceeded its execution deadline: %s",
                    schedule.get("name"),
                    run_id,
                    exc,
                )
            elif isinstance(exc, SchedulerCwdInheritRefusedError):
                # A deliberate fail-closed refusal, not an internal error: the message already names
                # the configured root and the daemon directory that would have been substituted, so
                # log it without a stack trace.
                _fire_terminal_status = "failed"
                _fire_exc_reason = RunReasons.FAILED_CWD_INHERIT_REFUSED
                _fire_reason_summary = f"{type(exc).__name__}: {exc}"
                _log.warning("Schedule fire %s (run %s): %s", schedule.get("name"), run_id, exc)
            else:
                _fire_terminal_status = "failed"
                _fire_exc_reason = RunReasons.FAILED_EXCEPTION
                _fire_reason_summary = f"{type(exc).__name__}: {exc}"
                _log.exception("Error in schedule fire %s (run %s)", schedule.get("name"), run_id)
            _end_time = time.time()
            written = await self._guarded_terminal_status(
                "schedule_run",
                run_id,
                new_status=_fire_terminal_status,
                reason_code=_fire_exc_reason,
                reason_summary=_fire_reason_summary,
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
                        new_status=_fire_terminal_status,
                        reason_code=_fire_exc_reason,
                        schedule_id=sid,
                        action_kind=schedule.get("action_kind", ""),
                        chain_depth=chain_depth,
                        trigger_context=trigger_context,
                        error_detail=f"{type(exc).__name__}: {exc}",
                    )
                )
            inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                self._svc,
                inv_id,
                fallback_status=_fire_terminal_status,
                exception=exc,
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
                # Another finalizer already wrote this invocation's terminal status, so no flush
                # happens here, but a schedule_run signal was still minted. Drop its counters rather
                # than leaving them in the bus's per-run_id map forever.
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
        """Remove the flow_yaml tmp file build_argv may have written."""
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    def _next_fire_field(self, schedule: dict, next_at: float | None) -> dict[str, float | None]:
        """Field(s) to merge into an ``update_schedule()`` call for *next_at*."""
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

                # Resolve the cron expression's wall-clock fields in the schedule's own declared
                # timezone when it has one; legacy rows with no resolved_timezone keep resolving
                # against the process-wide default. croniter honors DST transitions given a tz-aware
                # start_time, and get_next(float) still returns an absolute UTC epoch.
                start = datetime.fromtimestamp(
                    ref_time, tz=resolve_schedule_timezone(schedule).tzinfo
                )
                return croniter(expr, start_time=start).get_next(float)
            except Exception:
                _log.exception("Invalid cron expression: %s", expr)
                return None
        elif schedule["trigger_type"] in ("interval", "github_poll"):
            cadence = resolve_schedule_cadence_seconds(schedule)
            if not cadence:
                return None
            return ref_time + cadence
        elif schedule["trigger_type"] == "at":
            # A point-in-time trigger fires exactly once, so there is no next occurrence to compute.
            # Callers use _next_fire_field() to turn this None into an explicit persisted None.
            return None
        return None


scheduler = SchedulerEngine(svc=_DBSchedulerStateService(persistent=True))
register_default_handlers(scheduler._signal_bus)
