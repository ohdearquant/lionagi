# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 scheduler engine — in-process asyncio tick loop."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from lionagi.state.db import StateDB

_log = logging.getLogger(__name__)

_MAX_CHAIN_DEPTH = 10
_TICK_INTERVAL = 30  # seconds


class SchedulerEngine:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running: dict[str, str] = {}  # schedule_id -> run_id
        self._stopping = False

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

    async def fire_now(self, schedule_id: str) -> str | None:
        """Manual trigger — fire a schedule immediately. Returns run_id."""
        async with StateDB() as db:
            schedule = await db.get_schedule(schedule_id)
        if not schedule:
            return None
        run_id = uuid.uuid4().hex[:12]
        asyncio.create_task(
            self._fire(schedule, run_id, trigger_context={"manual": True, "fired_at": time.time()})
        )
        return run_id

    async def _tick_loop(self) -> None:
        # On startup, check for missed fires
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
                if s.get("missed_fire_policy") == "run_once" and s.get("next_fire_at"):
                    if s["next_fire_at"] < now:
                        run_id = uuid.uuid4().hex[:12]
                        _log.info("Missed fire recovery for schedule %s (%s)", s["name"], s["id"])
                        asyncio.create_task(
                            self._fire(s, run_id, trigger_context={"missed_recovery": True, "fired_at": now})
                        )
        except Exception:
            _log.exception("Missed fire check error")

    async def _tick(self) -> None:
        now = time.time()
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
                        # First run — compute next_fire_at
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
            ctx = {"github_events": new_events, "repo": schedule.get("github_repo"), "fired_at": now}
            run_id = uuid.uuid4().hex[:12]
            await self._fire(schedule, run_id, trigger_context=ctx)

    async def _maybe_fire(self, schedule: dict, now: float) -> None:
        # Overlap check
        if schedule.get("overlap_policy") == "skip" and schedule["id"] in self._running:
            _log.debug("Skipping overlapping fire for %s", schedule["name"])
            async with StateDB() as db:
                await db.create_schedule_run({
                    "id": uuid.uuid4().hex[:12],
                    "schedule_id": schedule["id"],
                    "trigger_context": {"skipped_overlap": True, "fired_at": now},
                    "action_kind": schedule["action_kind"],
                    "action_args": [],
                    "status": "skipped",
                    "fired_at": now,
                })
            # Still advance next_fire_at
            next_at = self._compute_next_fire(schedule, now)
            if next_at:
                async with StateDB() as db:
                    await db.update_schedule(schedule["id"], next_fire_at=next_at)
            return

        run_id = uuid.uuid4().hex[:12]
        ctx = {"scheduled": True, "fired_at": now, "next_fire_at": schedule.get("next_fire_at")}
        asyncio.create_task(self._fire(schedule, run_id, trigger_context=ctx))

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

        # Create invocation
        inv_id = uuid.uuid4().hex[:12]
        async with StateDB() as db:
            await db.create_invocation({
                "id": inv_id,
                "skill": f"scheduled:{schedule['name']}",
                "plugin": schedule["trigger_type"],
                "prompt": schedule.get("action_prompt") or schedule.get("action_playbook"),
                "started_at": now,
                "status": "running",
            })

        # Build argv
        argv = build_argv(schedule, trigger_context)

        # Create schedule_run
        async with StateDB() as db:
            await db.create_schedule_run({
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
            })

        # Track as running
        if chain_depth == 0:
            self._running[sid] = run_id

        # Update last_fired_at and compute next
        next_at = self._compute_next_fire(schedule, now)
        update_fields: dict[str, Any] = {"last_fired_at": now}
        if next_at:
            update_fields["next_fire_at"] = next_at
        async with StateDB() as db:
            await db.update_schedule(sid, **update_fields)

        _log.info("Firing schedule %s (run %s, chain_depth=%d)", schedule["name"], run_id, chain_depth)

        # Spawn and wait
        try:
            exit_code, stderr_tail = await spawn_and_wait(argv, inv_id)
            end_time = time.time()
            status = "completed" if exit_code == 0 else "failed"

            async with StateDB() as db:
                await db.update_schedule_run(
                    run_id,
                    status=status,
                    exit_code=exit_code,
                    ended_at=end_time,
                    error_detail=stderr_tail if exit_code != 0 else None,
                )
                await db.update_invocation(
                    inv_id,
                    status=status,
                    ended_at=end_time,
                )

            # Evaluate chain
            if chain_depth < _MAX_CHAIN_DEPTH:
                chain_action = None
                if exit_code == 0 and schedule.get("on_success"):
                    chain_action = schedule["on_success"]
                elif exit_code != 0 and schedule.get("on_fail"):
                    chain_action = schedule["on_fail"]

                if chain_action:
                    chain_schedule = {**schedule, **chain_action}
                    chain_schedule["action_kind"] = chain_action.get("kind", chain_action.get("action_kind", schedule["action_kind"]))
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

        except Exception:
            _log.exception("Error in schedule fire %s (run %s)", schedule.get("name"), run_id)
            async with StateDB() as db:
                await db.update_schedule_run(
                    run_id,
                    status="failed",
                    ended_at=time.time(),
                    error_detail="Internal scheduler error",
                )
                await db.update_invocation(inv_id, status="failed", ended_at=time.time())
        finally:
            if chain_depth == 0:
                self._running.pop(sid, None)

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


# Module-level singleton — imported by app.py lifespan and by the trigger endpoint
scheduler = SchedulerEngine()
