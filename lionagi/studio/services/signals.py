# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Studio service: read path for session_signals — replay rows then poll for new ones."""

from __future__ import annotations

import json
from typing import Any
from weakref import WeakKeyDictionary

from ._db import open_db as _open_db
from ._db import store_exists, store_path

# A live-tail broker retains one connection for its lifetime.  Remember the
# schema capability on that connection so each 500 ms poll does not hit
# sqlite_master again.  Weak keys make direct/test connections self-cleaning.
_SIGNALS_TABLE_CAPABILITY: WeakKeyDictionary[Any, bool] = WeakKeyDictionary()


async def _has_signals_table(db: Any) -> bool:
    known = _SIGNALS_TABLE_CAPABILITY.get(db)
    if known is not None:
        return known
    cur = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_signals'"
    )
    available = await cur.fetchone() is not None
    _SIGNALS_TABLE_CAPABILITY[db] = available
    return available


async def _get_signals_after_db(
    db: Any,
    session_id: str,
    after_seq: int,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    if not await _has_signals_table(db):
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


async def get_signals_after(
    session_id: str,
    after_seq: int,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return signal rows for *session_id* with seq > *after_seq*, ordered by seq."""
    if not store_exists():
        return []

    async with _open_db(store_path()) as db:
        return await _get_signals_after_db(db, session_id, after_seq, limit=limit)
