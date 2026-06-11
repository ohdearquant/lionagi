# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Engine definition management API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..services import engine_defs as ed_svc

router = APIRouter(prefix="/engine-defs", tags=["engine-defs"])


class CreateEngineDefRequest(BaseModel):
    name: str
    kind: str
    model: str | None = None
    max_depth: int | None = None
    max_agents: int | None = None
    options: dict[str, str] | None = None
    description: str | None = None


class UpdateEngineDefRequest(BaseModel):
    name: str | None = None
    kind: str | None = None
    model: str | None = None
    max_depth: int | None = None
    max_agents: int | None = None
    options: dict[str, str] | None = None
    description: str | None = None


@router.get("/")
async def list_engine_defs(
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    return await ed_svc.list_engine_defs(kind=kind, limit=limit, offset=offset)


@router.post("/", status_code=201)
async def create_engine_def(body: CreateEngineDefRequest) -> dict[str, Any]:
    try:
        return await ed_svc.create_engine_def(body.model_dump(exclude_none=True))
    except ed_svc.NameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{def_id}")
async def get_engine_def(def_id: str) -> dict[str, Any]:
    data = await ed_svc.get_engine_def(def_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Engine definition '{def_id}' not found")
    return data


@router.put("/{def_id}")
async def update_engine_def(def_id: str, body: UpdateEngineDefRequest) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    try:
        ok = await ed_svc.update_engine_def(def_id, fields)
    except ed_svc.NameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"Engine definition '{def_id}' not found")
    return {"ok": True}


@router.delete("/{def_id}")
async def delete_engine_def(def_id: str) -> dict[str, Any]:
    ok = await ed_svc.delete_engine_def(def_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Engine definition '{def_id}' not found")
    return {"ok": True}
