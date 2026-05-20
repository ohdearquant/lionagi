from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.responses import StreamingResponse

from ..services import runs as runs_svc

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("/")
async def list_runs() -> dict[str, Any]:
    return {"runs": runs_svc.list_runs()}


@router.get("/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    run = runs_svc.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


@router.get("/{run_id}/events")
async def run_events(run_id: str):
    """SSE stream of execution events for a live run."""
    gen = runs_svc.stream_run_events(run_id)
    if gen is None:
        raise HTTPException(
            status_code=404,
            detail=f"Run '{run_id}' has no live event stream",
        )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{run_id}/rerun")
async def rerun_run(run_id: str) -> dict[str, Any]:
    # TODO(lift-backend-writes)
    raise HTTPException(status_code=501, detail="Not implemented")


@router.delete("/{run_id}")
async def delete_run(run_id: str) -> dict[str, Any]:
    # TODO(lift-backend-writes)
    raise HTTPException(status_code=501, detail="Not implemented")
