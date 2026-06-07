from __future__ import annotations

from functools import partial
from typing import Any

import anyio
from fastapi import APIRouter, HTTPException

from ..services import skills as skills_svc

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("/")
async def list_skills() -> dict[str, Any]:
    skills = await anyio.to_thread.run_sync(skills_svc.list_skills)
    return {"skills": skills}


@router.get("/{name}")
async def get_skill(name: str) -> dict[str, Any]:
    skill = await anyio.to_thread.run_sync(partial(skills_svc.get_skill, name))
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    return skill
