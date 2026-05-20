from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ..services import playbooks as playbooks_svc

router = APIRouter(prefix="/playbooks", tags=["playbooks"])


@router.get("/")
async def list_playbooks() -> dict[str, Any]:
    return {"playbooks": playbooks_svc.list_playbooks()}


@router.get("/{name}")
async def get_playbook(name: str) -> dict[str, Any]:
    pb = playbooks_svc.get_playbook(name)
    if pb is None:
        raise HTTPException(status_code=404, detail=f"Playbook '{name}' not found")
    return pb


@router.post("/{name}")
async def create_playbook(name: str) -> dict[str, Any]:
    # TODO(lift-backend-writes)
    raise HTTPException(status_code=501, detail="Not implemented")


@router.put("/{name}")
async def update_playbook(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    updated = playbooks_svc.update_playbook(name, body)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Playbook '{name}' not found")
    return updated


@router.delete("/{name}")
async def delete_playbook(name: str) -> dict[str, Any]:
    # TODO(lift-backend-writes)
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/{name}/validate")
async def validate_playbook(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return playbooks_svc.validate_playbook(name, body)


@router.post("/{name}/run")
async def run_playbook(name: str) -> dict[str, Any]:
    # TODO(lift-backend-writes)
    raise HTTPException(status_code=501, detail="Not implemented")
