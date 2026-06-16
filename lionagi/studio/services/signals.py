# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Studio service: read path for session_signals — replay rows then poll for new ones."""

from __future__ import annotations

from typing import Any

from lionagi.state.db import DEFAULT_DB_PATH

from ._db import open_db as _open_db

_DB = str(DEFAULT_DB_PATH)


async def get_signals_after(
    session_id: str,
    after_seq: int,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return signal rows for *session_id* with seq > *after_seq*, ordered by seq."""
    if not DEFAULT_DB_PATH.exists():
        return []

    async with _open_db(_DB) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_signals'"
        )
        if not await cur.fetchone():
            return []

        cur = await db.execute(
            "SELECT id, session_id, seq, kind, op_id, ts, payload "
            "FROM session_signals "
            "WHERE session_id = ? AND seq > ? "
            "ORDER BY seq "
            "LIMIT ?",
            (session_id, after_seq, limit),
        )
        rows = await cur.fetchall()

    import json

    result = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        result.append(
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "seq": r["seq"],
                "kind": r["kind"],
                "op_id": r["op_id"] or "",
                "ts": r["ts"],
                "payload": payload or {},
            }
        )
    return result
