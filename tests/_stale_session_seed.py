# SPDX-License-Identifier: Apache-2.0

"""Shared helper for tests that need a long-idle running session in the state store."""

from __future__ import annotations

import time
import uuid


async def _seed_stale_session(
    db_path,
    session_id: str,
    last_message_at: float | None = None,
    updated_at: float | None = None,
) -> None:
    from lionagi.state.db import StateDB

    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        old_time = last_message_at or (time.time() - 7 * 3600)  # 7h old → stale
        await db.create_session(
            {
                "id": session_id,
                "progression_id": pid,
                "name": "stale-session",
                "status": "running",
                "started_at": old_time,
            }
        )
        # Set last_message_at and updated_at explicitly
        _up = updated_at or old_time
        await db.execute(
            "UPDATE sessions SET last_message_at=?, updated_at=? WHERE id=?",
            (old_time, _up, session_id),
        )
