# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 schedules service — backs /api/schedules endpoints."""

from __future__ import annotations

import time
import uuid
from typing import Any

from lionagi.state.db import DEFAULT_DB_PATH, StateDB

_ENSURE_SCHEDULES_SQL = """
CREATE TABLE IF NOT EXISTS schedules (
    id                  TEXT    PRIMARY KEY,
    name                TEXT    NOT NULL UNIQUE,
    description         TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1,
    trigger_type        TEXT    NOT NULL,
    cron_expr           TEXT,
    interval_sec        INTEGER,
    github_repo         TEXT,
    github_filter       JSON,
    github_cursor       TEXT,
    poll_interval_sec   INTEGER,
    action_kind         TEXT    NOT NULL,
    action_model        TEXT,
    action_prompt       TEXT,
    action_agent        TEXT,
    action_playbook     TEXT,
    action_project      TEXT,
    action_extra_args   JSON    DEFAULT '[]',
    on_success          JSON,
    on_fail             JSON,
    last_fired_at       REAL,
    next_fire_at        REAL,
    missed_fire_policy  TEXT    NOT NULL DEFAULT 'skip',
    overlap_policy      TEXT    NOT NULL DEFAULT 'skip',
    project             TEXT,
    created_at          REAL    NOT NULL,
    updated_at          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_enabled
    ON schedules(enabled, next_fire_at) WHERE enabled = 1;
CREATE INDEX IF NOT EXISTS idx_schedules_name
    ON schedules(name);

CREATE TABLE IF NOT EXISTS schedule_runs (
    id                  TEXT    PRIMARY KEY,
    schedule_id         TEXT    NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    invocation_id       TEXT,
    trigger_context     JSON    NOT NULL,
    action_kind         TEXT    NOT NULL,
    action_args         JSON    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'running',
    exit_code           INTEGER,
    chain_parent_id     TEXT,
    chain_depth         INTEGER NOT NULL DEFAULT 0,
    fired_at            REAL    NOT NULL,
    ended_at            REAL,
    error_detail        TEXT,
    created_at          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_runs_schedule
    ON schedule_runs(schedule_id, fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_sched_runs_status
    ON schedule_runs(status) WHERE status = 'running';
"""


async def _ensure_table(db) -> None:
    await db.executescript(_ENSURE_SCHEDULES_SQL)


async def list_schedules(
    *,
    enabled: bool | None = None,
    trigger_type: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []
    async with StateDB() as db:
        rows = await db.list_schedules(enabled=enabled, trigger_type=trigger_type, project=project)
    return rows


async def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None
    async with StateDB() as db:
        row = await db.get_schedule(schedule_id)
        if not row:
            return None
        runs = await db.list_schedule_runs(schedule_id, limit=10)
    row["recent_runs"] = runs
    return row


async def get_schedule_by_name(name: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None
    async with StateDB() as db:
        return await db.get_schedule_by_name(name)


async def create_schedule(data: dict[str, Any]) -> dict[str, Any]:
    if not data.get("name"):
        raise ValueError("Schedule name is required")
    if not data.get("trigger_type"):
        raise ValueError("trigger_type is required")
    if not data.get("action_kind"):
        raise ValueError("action_kind is required")

    schedule_id = uuid.uuid4().hex[:12]
    now = time.time()
    schedule = {
        "id": schedule_id,
        "created_at": now,
        "updated_at": now,
        **data,
    }
    async with StateDB() as db:
        await db.create_schedule(schedule)
    return {"id": schedule_id, "name": data["name"], "created_at": now}


async def update_schedule(schedule_id: str, fields: dict[str, Any]) -> bool:
    if not fields:
        return False
    async with StateDB() as db:
        schedule = await db.get_schedule(schedule_id)
        if not schedule:
            return False
        await db.update_schedule(schedule_id, **fields)
    return True


async def delete_schedule(schedule_id: str) -> bool:
    async with StateDB() as db:
        return await db.delete_schedule(schedule_id)


async def enable_schedule(schedule_id: str) -> bool:
    async with StateDB() as db:
        schedule = await db.get_schedule(schedule_id)
        if not schedule:
            return False
        await db.update_schedule(schedule_id, enabled=1)
    return True


async def disable_schedule(schedule_id: str) -> bool:
    async with StateDB() as db:
        schedule = await db.get_schedule(schedule_id)
        if not schedule:
            return False
        await db.update_schedule(schedule_id, enabled=0)
    return True


async def list_schedule_runs(
    schedule_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []
    async with StateDB() as db:
        return await db.list_schedule_runs(schedule_id, status=status, limit=limit, offset=offset)


async def get_schedule_run(run_id: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None
    async with StateDB() as db:
        run = await db.get_schedule_run(run_id)
        if not run:
            return None
        # Include chain children
        if run.get("chain_depth", 0) == 0:
            cur = await db.db.execute(
                "SELECT * FROM schedule_runs WHERE chain_parent_id = ? ORDER BY chain_depth, fired_at",
                (run_id,),
            )
            rows = await cur.fetchall()
            run["chain_children"] = [db._row_to_dict(r) for r in rows]
    return run
