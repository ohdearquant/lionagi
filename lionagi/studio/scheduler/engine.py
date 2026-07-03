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
from lionagi.studio.services.scheduler_state import (
    SchedulerStateService,
    create_skipped_run,
    default_scheduler_state,
    resolve_invocation_terminal,
)

_log = logging.getLogger(__name__)

_MAX_CHAIN_DEPTH = 10
_TICK_INTERVAL = 30  # seconds


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
        run_id = uuid.uuid4().hex[:12]
        self._tracked_fire(
            schedule, run_id, trigger_context={"manual": True, "fired_at": time.time()}
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

    async def _tick_github(self, schedule: dict, now: float) -> None:
        poll_interval = schedule.get("poll_interval_sec") or schedule.get("interval_sec") or 300
        last = schedule.get("last_fired_at") or 0
        if now - last < poll_interval:
            return
        from .github import github_poll

        new_events = await github_poll(schedule)
        if new_events:
            ctx = {
                "github_events": new_events,
                "repo": schedule.get("github_repo"),
                "fired_at": now,
            }
            run_id = uuid.uuid4().hex[:12]
            await self._fire(schedule, run_id, trigger_context=ctx)

    async def _maybe_fire(self, schedule: dict, now: float) -> None:
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

        run_id = uuid.uuid4().hex[:12]
        ctx = {"scheduled": True, "fired_at": now, "next_fire_at": schedule.get("next_fire_at")}
        self._tracked_fire(schedule, run_id, trigger_context=ctx)

    async def _fire(
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
        await self._svc.create_invocation(
            {
                "id": inv_id,
                "skill": f"scheduled:{schedule['name']}",
                "plugin": schedule["trigger_type"],
                "prompt": schedule.get("action_prompt") or schedule.get("action_playbook"),
                "started_at": now,
                "status": "running",
            }
        )

        try:
            argv, _tmp_path = _subprocess.build_argv(schedule, trigger_context)
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
            await self._svc.update_status(
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
            await self._svc.update_status(
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
            await self._svc.update_status(
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
                    reason_code=RunReasons.CANCELLED_SYSTEM,
                    status="cancelled",
                    reason_summary="Schedule run cancelled by scheduler shutdown.",
                    evidence_refs=[{"kind": "schedule", "id": sid}],
                    reason_actor=run_id,
                )
                inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await resolve_invocation_terminal(
                    self._svc, inv_id, fallback_status="cancelled"
                )
                await self._svc.update_invocation(inv_id, ended_at=_end_time)
                await self._svc.update_status(
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
            await self._svc.update_status(
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
