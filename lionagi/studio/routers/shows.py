from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..services import shows as shows_svc
from ._sse import sse_response

router = APIRouter(prefix="/shows", tags=["shows"])


@router.get("/")
async def list_shows() -> list[dict[str, Any]]:
    return await shows_svc.list_shows()


# ADR-0011 §"Migration": import_shows is a state-mutating operation
# (INSERT OR IGNORE into shows + plays); it must use POST, not GET.
# ADR-0011 specifies this as a CLI maintenance command (`li state import-shows`).
# The POST endpoint is retained as a Studio convenience trigger.
@router.post("/import", tags=["shows"])
async def import_shows() -> dict[str, int]:
    return await shows_svc.import_shows()


@router.get("/{topic}")
async def get_show(topic: str) -> dict[str, Any]:
    show = await shows_svc.get_show(topic)
    if show is None:
        raise HTTPException(status_code=404, detail=f"Show '{topic}' not found")
    return show


@router.get("/{topic}/stream")
async def stream_show(topic: str):
    """SSE stream of file changes under one show directory."""
    if await shows_svc.get_show(topic) is None:
        raise HTTPException(status_code=404, detail=f"Show '{topic}' not found")
    return sse_response(shows_svc.watch_show(topic))
