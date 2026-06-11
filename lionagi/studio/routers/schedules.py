# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 schedule management API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..services import schedules as sched_svc

router = APIRouter(prefix="/schedules", tags=["schedules"])


class CreateScheduleRequest(BaseModel):
    name: str
    description: str | None = None
    trigger_type: str
    cron_expr: str | None = None
    interval_sec: int | None = None
    github_repo: str | None = None
    github_filter: dict | None = None
    poll_interval_sec: int | None = None
    action_kind: str
    action_model: str | None = None
    action_prompt: str | None = None
    action_agent: str | None = None
    action_playbook: str | None = None
    action_flow_yaml: str | None = None
    action_project: str | None = None
    action_extra_args: list[str] | None = None
    on_success: dict | None = None
    on_fail: dict | None = None
    missed_fire_policy: str = "skip"
    overlap_policy: str = "skip"
    project: str | None = None


class UpdateScheduleRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    trigger_type: str | None = None
    cron_expr: str | None = None
    interval_sec: int | None = None
    github_repo: str | None = None
    github_filter: dict | None = None
    poll_interval_sec: int | None = None
    action_kind: str | None = None
    action_model: str | None = None
    action_prompt: str | None = None
    action_agent: str | None = None
    action_playbook: str | None = None
    action_flow_yaml: str | None = None
    action_project: str | None = None
    action_extra_args: list[str] | None = None
    on_success: dict | None = None
    on_fail: dict | None = None
    missed_fire_policy: str | None = None
    overlap_policy: str | None = None
    project: str | None = None


@router.get("/")
async def list_schedules(
    enabled: bool | None = Query(default=None),
    trigger_type: str | None = Query(default=None),
    project: str | None = Query(default=None),
) -> dict[str, Any]:
    rows = await sched_svc.list_schedules(
        enabled=enabled, trigger_type=trigger_type, project=project
    )
    return {"schedules": rows}


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str) -> dict[str, Any]:
    data = await sched_svc.get_schedule(schedule_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return data


@router.post("/", status_code=201)
async def create_schedule(body: CreateScheduleRequest) -> dict[str, Any]:
    try:
        return await sched_svc.create_schedule(body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/{schedule_id}")
async def update_schedule(schedule_id: str, body: UpdateScheduleRequest) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    try:
        ok = await sched_svc.update_schedule(schedule_id, fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return {"ok": True}


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str) -> dict[str, Any]:
    ok = await sched_svc.delete_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return {"ok": True}


@router.post("/{schedule_id}/enable")
async def enable_schedule(schedule_id: str) -> dict[str, Any]:
    ok = await sched_svc.enable_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return {"ok": True, "enabled": True}


@router.post("/{schedule_id}/disable")
async def disable_schedule(schedule_id: str) -> dict[str, Any]:
    ok = await sched_svc.disable_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return {"ok": True, "enabled": False}


@router.post("/{schedule_id}/trigger")
async def trigger_schedule(schedule_id: str) -> dict[str, Any]:
    from ..scheduler.engine import scheduler

    run_id = await scheduler.fire_now(schedule_id)
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return {"ok": True, "run_id": run_id}


@router.get("/{schedule_id}/runs")
async def list_schedule_runs(
    schedule_id: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows = await sched_svc.list_schedule_runs(
        schedule_id, status=status, limit=limit, offset=offset
    )
    return {"runs": rows, "limit": limit, "offset": offset, "has_next": len(rows) == limit}


# Top-level schedule-runs endpoint for looking up a single run by ID
@router.get("/runs/{run_id}", tags=["schedule-runs"])
async def get_schedule_run(run_id: str) -> dict[str, Any]:
    data = await sched_svc.get_schedule_run(run_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Schedule run '{run_id}' not found")
    return data
