from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..services import definitions as defs_svc

router = APIRouter(prefix="/definitions", tags=["definitions"])


@router.get("/")
async def list_definitions(
    kind: str | None = Query(default=None, description="Filter by kind: agent, playbook, skill"),
) -> dict[str, Any]:
    return {"definitions": await defs_svc.list_definitions(kind)}


@router.get("/{kind}/{name}")
async def get_definition(kind: str, name: str) -> dict[str, Any]:
    defn = await defs_svc.get_definition(kind, name)
    if defn is None:
        raise HTTPException(status_code=404, detail=f"Definition '{kind}/{name}' not found")
    return defn


@router.get("/{kind}/{name}/versions/{version}")
async def get_version(kind: str, name: str, version: int) -> dict[str, Any]:
    v = await defs_svc.get_version(kind, name, version)
    if v is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found for {kind}/{name}")
    return v


class SaveBody(BaseModel):
    content: str
    message: str | None = None


@router.put("/{kind}/{name}")
async def save_definition(kind: str, name: str, body: SaveBody) -> dict[str, Any]:
    return await defs_svc.save_definition(kind, name, body.content, body.message)


@router.post("/{kind}/{name}/rollback/{version}")
async def rollback_definition(kind: str, name: str, version: int) -> dict[str, Any]:
    result = await defs_svc.rollback_definition(kind, name, version)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found for {kind}/{name}")
    return result


@router.post("/snapshot")
async def snapshot_current(
    kind: str | None = Query(default=None),
) -> dict[str, Any]:
    count = await defs_svc.snapshot_current(kind)
    return {"snapshots_created": count}
