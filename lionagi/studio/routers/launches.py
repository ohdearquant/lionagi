# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Launch endpoints — fire agent/flow/fanout/play/engine runs on demand."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import launches as launch_svc

router = APIRouter(prefix="/launches", tags=["launches"])


class LaunchRequest(BaseModel):
    action_kind: str
    action_model: str | None = None
    action_prompt: str | None = None
    action_agent: str | None = None
    action_playbook: str | None = None
    action_project: str | None = None
    action_flow_yaml: str | None = None
    action_engine_def: str | None = None
    action_extra_args: list[str] | None = None


@router.post("/", status_code=202)
async def launch_run(body: LaunchRequest) -> dict[str, Any]:
    """Fire an orchestration run immediately; process runs detached.

    Returns invocation_id; monitor via GET /api/invocations/{id} and GET /api/sessions/{id}/signals.
    """
    try:
        return await launch_svc.launch(body.model_dump(exclude_none=True))
    except launch_svc.TooManyLaunchesError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
