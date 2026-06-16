# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""GET /api/sessions/{id}/signals — SSE stream of lifecycle-signal events."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.responses import StreamingResponse

from ..services import sessions as sessions_svc
from ..services import signals as signals_svc

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("/{session_id}/signals")
async def stream_signals(session_id: str) -> Any:
    # Pre-flight 404 guard before opening the stream — mirrors the pattern
    # at sessions.py:35-36 (ADR-0006).
    if not await sessions_svc.session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    async def generate():
        after_seq: int = 0
        last_heartbeat = time.monotonic()

        while True:
            rows = await signals_svc.get_signals_after(session_id, after_seq)

            if rows:
                for row in rows:
                    # _PAYLOAD_BYTE_CAP (16 KiB, defined in session/observer.py) caps the
                    # payload column only, not the full SSE frame; the row envelope adds
                    # ~176 bytes of fixed metadata overhead, so frames can exceed that cap.
                    yield f"data: {json.dumps(row)}\n\n"
                    if row["seq"] > after_seq:
                        after_seq = row["seq"]
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
