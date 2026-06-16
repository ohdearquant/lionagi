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
from typing import Any

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons, ScheduleReasons

_log = logging.getLogger(__name__)


async def _resolve_invocation_terminal(
    db: StateDB,
    invocation_id: str,
    *,
    fallback_status: str,
    exit_code: int | None = None,
    exception: BaseException | None = None,
) -> tuple[str, str, str, list[dict], dict]:
    sessions = await db.list_sessions_for_invocation(invocation_id)
    child_statuses = [str(s.get("status") or "") for s in sessions]
    evidence_refs = [{"kind": "session", "id": s["id"]} for s in sessions if s.get("id")]
    metadata: dict = {"child_statuses": child_statuses}
    if exit_code is not None:
        metadata["exit_code"] = exit_code
    if exception is not None:
        metadata["exception_class"] = type(exception).__name__

    # Precedence: timed_out > failed > aborted > cancelled > completed.
    if child_statuses:
        if any(s == "timed_out" for s in child_statuses):
            return (
                "timed_out",
                RunReasons.TIMED_OUT_DEADLINE,
                "Invocation timed out because at least one child session timed out.",
                evidence_refs,
                metadata,
            )
        if any(s == "failed" for s in child_statuses):
            return (
                "failed",
                RunReasons.FAILED_EXCEPTION,
                "Invocation failed because at least one child session failed.",
                evidence_refs,
                metadata,
            )
        if any(s == "aborted" for s in child_statuses):
            aborted_reasons = {
                str(sess.get("status_reason_code") or "")
                for sess in sessions
                if sess.get("status") == "aborted"
            }
            if RunReasons.CANCELLED_SIGINT in aborted_reasons:
                reason_code = RunReasons.CANCELLED_SIGINT
                reason_summary = "Invocation was interrupted (SIGINT) because a child session was."
            else:
                reason_code = RunReasons.ABORTED_USER
                reason_summary = (
                    "Invocation was aborted because at least one child session was aborted."
                )
            return ("aborted", reason_code, reason_summary, evidence_refs, metadata)
        if any(s == "cancelled" for s in child_statuses):
            return (
                "cancelled",
                RunReasons.CANCELLED_SYSTEM,
                "Invocation was cancelled because at least one child session was cancelled.",
                evidence_refs,
                metadata,
            )
        if all(s == "completed" for s in child_statuses):
            return (
                "completed",
                RunReasons.COMPLETED_OK,
                "All child sessions completed successfully.",
                evidence_refs,
                metadata,
            )

    if fallback_status == "completed":
        return (
            "completed",
            RunReasons.COMPLETED_OK,
            "Invocation process completed successfully.",
            evidence_refs,
            metadata,
        )
    if fallback_status == "timed_out":
        return (
            "timed_out",
            RunReasons.TIMED_OUT_DEADLINE,
            "Invocation process exceeded its deadline.",
            evidence_refs,
            metadata,
        )
    if fallback_status == "aborted":
        # Process-level abort with no child reason to inspect — keep the neutral
        # user/admin abort reason rather than assume SIGINT.
        return (
            "aborted",
            RunReasons.ABORTED_USER,
            "Invocation process was aborted.",
            evidence_refs,
            metadata,
        )
    if fallback_status == "cancelled":
        return (
            "cancelled",
            RunReasons.CANCELLED_SYSTEM,
            "Invocation process was cancelled by the runtime.",
            evidence_refs,
            metadata,
        )
    if exception is not None:
        return (
            "failed",
            RunReasons.FAILED_EXCEPTION,
            f"{type(exception).__name__}: {exception}",
            evidence_refs,
            metadata,
        )
    if exit_code is not None and exit_code != 0:
        return (
            "failed",
            RunReasons.FAILED_EXIT_NONZERO,
            f"Invocation process failed with exit code {exit_code}.",
            evidence_refs,
            metadata,
        )
    return (
        "failed",
        RunReasons.FAILED_EXCEPTION,
        "Invocation process failed.",
        evidence_refs,
        metadata,
    )


_MAX_CHAIN_DEPTH = 10
_TICK_INTERVAL = 30  # seconds


class SchedulerEngine:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running: dict[str, str] = {}  # schedule_id -> run_id
        self._stopping = False
        self._fire_tasks: set[asyncio.Task] = set()
        self._last_reaper_run: float = 0.0  # epoch; 0 means never
        self._last_checkpoint_run: float = 0.0  # epoch; 0 means never

    async def start(self) -> None:
        _log.info("Scheduler engine starting")
        self._stopping = False
        self._task = asyncio.create_task(self._tick_loop())

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
        async with StateDB() as db:
            schedule = await db.get_schedule(schedule_id)
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
            async with StateDB() as db:
                schedules = await db.list_schedules(enabled=True)
            now = time.time()
            for s in schedules:
                next_fire_at = s.get("next_fire_at")
                if next_fire_at is None or next_fire_at >= now:
                    continue
                policy = s.get("missed_fire_policy")
                if policy == "run_once":
                    run_id = uuid.uuid4().hex[:12]
                    _log.info(
                        "Missed fire recovery for schedule %s (%s)",
                        s["name"],
                        s["id"],
                    )
                    self._tracked_fire(
                        s,
                        run_id,
                        trigger_context={"missed_recovery": True, "fired_at": now},
                    )
                else:
                    await self._record_missed_fire_skip(s, now)
        except Exception:
            _log.exception("Missed fire check error")

    async def _record_missed_fire_skip(self, schedule: dict, now: float) -> None:
        """Record missed-fire skip and advance next_fire_at."""
        skipped_run_id = uuid.uuid4().hex[:12]
        try:
            async with StateDB() as db:
                await db.create_schedule_run(
                    {
                        "id": skipped_run_id,
                        "schedule_id": schedule["id"],
                        "trigger_context": {
                            "skipped_missed_fire": True,
                            "missed_fire_at": schedule.get("next_fire_at"),
                            "checked_at": now,
                        },
                        "action_kind": schedule["action_kind"],
                        "action_args": [],
                        "status": "skipped",
                        "fired_at": now,
                    }
                )
                await db.update_status(
                    "schedule_run",
                    skipped_run_id,
                    new_status="skipped",
                    reason_code=ScheduleReasons.SKIPPED_MISSED_FIRE,
                    reason_summary=(
                        "Schedule fire skipped because the scheduled time "
                        "passed while the server was down or the tick was "
                        "delayed (missed_fire_policy=skip)."
                    ),
                    evidence_refs=[{"kind": "schedule", "id": schedule["id"]}],
                    source="system",
                    actor=schedule["id"],
                    metadata={
                        "missed_fire_policy": schedule.get("missed_fire_policy"),
                        "missed_fire_at": schedule.get("next_fire_at"),
                    },
                )
                next_at = self._compute_next_fire(schedule, now)
                if next_at:
                    await db.update_schedule(schedule["id"], next_fire_at=next_at)
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

        async with StateDB() as db:
            schedules = await db.list_schedules(enabled=True)

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
                            async with StateDB() as db:
                                await db.update_schedule(s["id"], next_fire_at=next_at)
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
            async with StateDB() as db:
                await db.create_schedule_run(
                    {
                        "id": skipped_run_id,
                        "schedule_id": schedule["id"],
                        "trigger_context": {"skipped_overlap": True, "fired_at": now},
                        "action_kind": schedule["action_kind"],
                        "action_args": [],
                        "status": "skipped",
                        "fired_at": now,
                    }
                )
                await db.update_status(
                    "schedule_run",
                    skipped_run_id,
                    new_status="skipped",
                    reason_code=ScheduleReasons.SKIPPED_OVERLAP,
                    reason_summary="Schedule fire skipped because overlap_policy=skip and a prior run is still active.",
                    evidence_refs=[{"kind": "schedule", "id": schedule["id"]}],
                    source="system",
                    actor=schedule["id"],
                    metadata={"overlap_policy": schedule.get("overlap_policy")},
                )
            next_at = self._compute_next_fire(schedule, now)
            if next_at:
                async with StateDB() as db:
                    await db.update_schedule(schedule["id"], next_fire_at=next_at)
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
        from .subprocess import build_argv, spawn_and_wait

        sid = schedule["id"]
        now = time.time()

        inv_id = uuid.uuid4().hex[:12]
        async with StateDB() as db:
            await db.create_invocation(
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
            argv, _tmp_path = build_argv(schedule, trigger_context)
        except Exception as exc:
            _log.exception("Invalid schedule action for %s (run %s)", schedule.get("name"), run_id)
            _end_time = time.time()
            next_at = self._compute_next_fire(schedule, now)
            async with StateDB() as db:
                await db.create_schedule_run(
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
                await db.update_status(
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
                inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await _resolve_invocation_terminal(
                    db, inv_id, fallback_status="failed", exception=exc
                )
                await db.update_invocation(inv_id, ended_at=_end_time)
                await db.update_status(
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
                await db.update_schedule(sid, **update_fields)
            return

        # Ensure the flow_yaml tmp file is removed on any exception or
        # cancellation in the DB ops below, before spawn_and_wait() runs.
        # suppress(OSError) makes double-unlink (spawn_and_wait already cleaned up) safe.
        try:
            async with StateDB() as db:
                await db.create_schedule_run(
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
                await db.update_status(
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
            update_fields: dict[str, Any] = {"last_fired_at": now}
            if next_at:
                update_fields["next_fire_at"] = next_at
            async with StateDB() as db:
                await db.update_schedule(sid, **update_fields)

            _log.info(
                "Firing schedule %s (run %s, chain_depth=%d)", schedule["name"], run_id, chain_depth
            )

            exit_code, stderr_tail = await spawn_and_wait(argv, inv_id, tmp_path=_tmp_path)
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

            async with StateDB() as db:
                await db.update_schedule_run(
                    run_id,
                    exit_code=exit_code,
                    ended_at=end_time,
                    error_detail=stderr_tail if exit_code != 0 else None,
                )
                await db.update_status(
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
                inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await _resolve_invocation_terminal(
                    db, inv_id, fallback_status=status, exit_code=exit_code
                )
                await db.update_invocation(inv_id, ended_at=end_time)
                await db.update_status(
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
                async with StateDB() as db:
                    await db.update_schedule_run(
                        run_id,
                        ended_at=_end_time,
                        error_detail="Scheduler shutdown",
                        reason_code=RunReasons.CANCELLED_SYSTEM,
                        status="cancelled",
                        reason_summary="Schedule run cancelled by scheduler shutdown.",
                        evidence_refs=[{"kind": "schedule", "id": sid}],
                        reason_actor=run_id,
                    )
                    (
                        inv_status,
                        inv_rc,
                        inv_rs,
                        inv_ev,
                        inv_meta,
                    ) = await _resolve_invocation_terminal(db, inv_id, fallback_status="cancelled")
                    await db.update_invocation(inv_id, ended_at=_end_time)
                    await db.update_status(
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
            async with StateDB() as db:
                await db.update_schedule_run(
                    run_id,
                    ended_at=_end_time,
                    error_detail="Internal scheduler error",
                )
                await db.update_status(
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
                inv_status, inv_rc, inv_rs, inv_ev, inv_meta = await _resolve_invocation_terminal(
                    db, inv_id, fallback_status="failed", exception=exc
                )
                await db.update_invocation(inv_id, ended_at=_end_time)
                await db.update_status(
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
            # Clean up flow_yaml tmp file if spawn_and_wait never ran;
            # suppress OSError so double-unlink is harmless.
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

                return croniter(expr, start_time=ref_time).get_next(float)
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
