from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.responses import StreamingResponse

from ..services import shows as shows_svc

router = APIRouter(prefix="/shows", tags=["shows"])


@router.get("/")
async def list_shows() -> list[dict[str, Any]]:
    return shows_svc.list_shows()


@router.get("/{topic}")
async def get_show(topic: str) -> dict[str, Any]:
    show = shows_svc.get_show(topic)
    if show is None:
        raise HTTPException(status_code=404, detail=f"Show '{topic}' not found")
    return show


@router.get("/{topic}/stream")
async def stream_show(topic: str):
    """SSE stream of file changes under one show directory."""
    if shows_svc.get_show(topic) is None:
        raise HTTPException(status_code=404, detail=f"Show '{topic}' not found")
    return StreamingResponse(
        shows_svc.watch_show(topic),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
