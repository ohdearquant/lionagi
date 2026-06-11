from __future__ import annotations

from functools import partial
from typing import Any

import anyio
from fastapi import APIRouter, HTTPException

from ..services import plugins as plugins_svc

router = APIRouter(tags=["plugins"])


@router.get("/plugins")
async def list_plugins_endpoint() -> dict[str, Any]:
    plugins = await anyio.to_thread.run_sync(plugins_svc.list_plugins)
    return {"plugins": plugins}


@router.get("/plugins/{name}")
async def get_plugin_endpoint(name: str) -> dict[str, Any]:
    plugin = await anyio.to_thread.run_sync(partial(plugins_svc.get_plugin, name))
    if not plugin:
        raise HTTPException(status_code=404, detail=f"Plugin {name} not found")
    return plugin


@router.get("/plugins/{plugin_name}/skills/{skill_name}")
async def get_plugin_skill_endpoint(plugin_name: str, skill_name: str) -> dict[str, Any]:
    skill = await anyio.to_thread.run_sync(
        partial(plugins_svc.get_plugin_skill, plugin_name, skill_name)
    )
    if not skill:
        raise HTTPException(
            status_code=404,
            detail=f"Skill {skill_name} not found in plugin {plugin_name}",
        )
    return skill


@router.get("/plugins/{plugin_name}/agents/{agent_name}")
async def get_plugin_agent_endpoint(plugin_name: str, agent_name: str) -> dict[str, Any]:
    agent = await anyio.to_thread.run_sync(
        partial(plugins_svc.get_plugin_agent, plugin_name, agent_name)
    )
    if not agent:
        raise HTTPException(
            status_code=404,
            detail=f"Agent {agent_name} not found in plugin {plugin_name}",
        )
    return agent
