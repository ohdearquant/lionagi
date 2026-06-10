from __future__ import annotations

from functools import partial
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Body, HTTPException

from ..services import agents as agents_svc

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/")
async def list_agents() -> dict[str, Any]:
    agents = await anyio.to_thread.run_sync(agents_svc.list_agents)
    return {"agents": agents}


@router.get("/{name}")
async def get_agent(name: str) -> dict[str, Any]:
    agent = await anyio.to_thread.run_sync(partial(agents_svc.get_agent, name))
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return agent


@router.post("/{name}")
async def create_agent(name: str) -> dict[str, Any]:
    # TODO(lift-backend-writes)
    raise HTTPException(status_code=501, detail="Not implemented")


@router.put("/{name}")
async def update_agent(name: str, body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
    updated = await anyio.to_thread.run_sync(partial(agents_svc.update_agent, name, body))
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return updated


@router.delete("/{name}")
async def delete_agent(name: str) -> dict[str, Any]:
    # TODO(lift-backend-writes)
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/{name}/validate")
async def validate_agent(name: str, body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
    errors: list[str] = []
    if not (body.get("name") or "").strip():
        errors.append("name is required")
    if not (body.get("provider") or "").strip():
        errors.append("provider is required")
    if not (body.get("model") or "").strip():
        errors.append("model is required")
    return {"ok": not errors, "errors": errors or None}
