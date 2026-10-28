# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Studio read paths for persisted session signals."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from ._db import open_db as _open_db
from ._db import require_file_store, store_exists, store_path

__all__ = ("get_signals_after",)


async def _read_signals(
    db: aiosqlite.Connection,
    session_id: str,
    after_seq: int,
    *,
    limit: int = 500,
    through_seq: int | None = None,
) -> list[dict[str, Any]]:
    if through_seq is None:
        query = (
            "SELECT id, session_id, seq, kind, op_id, ts, payload FROM session_signals "
            "WHERE session_id = ? AND seq > ? ORDER BY seq LIMIT ?"
        )
        params = (session_id, after_seq, limit)
    else:
        query = (
            "SELECT id, session_id, seq, kind, op_id, ts, payload FROM session_signals "
            "WHERE session_id = ? AND seq > ? AND seq <= ? ORDER BY seq LIMIT ?"
        )
        params = (session_id, after_seq, through_seq, limit)
    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()
    result = []
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        result.append(
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "seq": row["seq"],
                "kind": row["kind"],
                "op_id": row["op_id"] or "",
                "ts": row["ts"],
                "payload": payload or {},
            }
        )
    return result


async def get_signals_after(
    session_id: str,
    after_seq: int,
    *,
    limit: int = 500,
    through_seq: int | None = None,
) -> list[dict[str, Any]]:
    """Return ordered signals after a sequence, optionally through a fixed prefix cut."""
    require_file_store()
    if not store_exists():
        return []
    async with _open_db(store_path()) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_signals'"
        ) as cursor:
            if not await cursor.fetchone():
                return []
        return await _read_signals(db, session_id, after_seq, limit=limit, through_seq=through_seq)
