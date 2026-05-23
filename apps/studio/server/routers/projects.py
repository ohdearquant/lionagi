# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import projects as projects_svc

router = APIRouter(prefix="/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    name: str
    github: str | None = None
    description: str | None = None
    path: str | None = None


class UpdateProjectRequest(BaseModel):
    github: str | None = None
    description: str | None = None
    path: str | None = None


class AssignProjectRequest(BaseModel):
    session_ids: list[str] | None = None
    all_unassigned: bool = False


@router.get("/")
async def list_projects() -> dict[str, Any]:
    return await projects_svc.list_projects()


@router.get("/{name}")
async def get_project(name: str) -> dict[str, Any]:
    project = await projects_svc.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
    return project


@router.post("/", status_code=201)
async def create_project(body: CreateProjectRequest) -> dict[str, Any]:
    try:
        return await projects_svc.create_project(
            body.name,
            github=body.github,
            description=body.description,
            path=body.path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/{name}")
async def update_project(name: str, body: UpdateProjectRequest) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    ok = await projects_svc.update_project(name, fields)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{name}' not found or no changes",
        )
    return {"ok": True}


@router.post("/{name}/assign")
async def assign_project(name: str, body: AssignProjectRequest) -> dict[str, Any]:
    """Assign sessions to a project. Use session_ids for specific ones, or all_unassigned=true."""
    count = await projects_svc.assign_sessions_to_project(
        name,
        session_ids=body.session_ids,
        all_unassigned=body.all_unassigned,
    )
    return {"assigned": count, "project": name}


@router.delete("/{name}")
async def delete_project(name: str) -> dict[str, Any]:
    ok = await projects_svc.delete_project(name)
    if not ok:
        raise HTTPException(
            status_code=403,
            detail="Only Studio-managed projects can be deleted",
        )
    return {"ok": True}
