from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException  # HTTPException used for 404 guards
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
    # ADR-0006: pre-flight 404 guard before opening the stream.
    # Without this, a non-existent session silently returns no messages and
    # then waits 60s before emitting done — client hangs with no indication.
    # The shows router already does this at shows.py:34-35; we mirror that pattern.
    if not await sessions_svc.session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

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

            state = await sessions_svc.get_session_stream_state(session_id)
            if sessions_svc.is_session_stream_done(state, now=time.time()):
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
