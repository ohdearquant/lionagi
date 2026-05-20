from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.responses import StreamingResponse

from ..services import sessions as sessions_svc

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("/")
async def list_sessions() -> dict[str, Any]:
    return {"sessions": await sessions_svc.list_sessions()}


@router.get("/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    session = await sessions_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return session


@router.get("/{session_id}/stream")
async def stream_session(session_id: str):
    async def generate():
        after_ts: float = 0.0
        last_heartbeat = time.monotonic()

        while True:
            messages = await sessions_svc.get_session_messages_after(session_id, after_ts)

            if messages:
                for msg in messages:
                    yield f"data: {json.dumps(msg)}\n\n"
                    ts = msg.get("timestamp") or msg.get("created_at")
                    if ts and ts > after_ts:
                        after_ts = ts
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= 5.0:
                yield 'data: {"type":"heartbeat"}\n\n'
                last_heartbeat = time.monotonic()

            session = await sessions_svc.get_session(session_id)
            if session is not None:
                updated_at = session.get("updated_at") or 0.0
                if time.time() - updated_at > 60.0:
                    yield 'data: {"type":"done"}\n\n'
                    return

            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
