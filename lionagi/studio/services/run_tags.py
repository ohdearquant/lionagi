# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from lionagi.state.db import DEFAULT_DB_PATH

from ..registry import studio_route
from ._db import open_db as _open_db

_DB = str(DEFAULT_DB_PATH)

_ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS run_tags (
    session_id TEXT NOT NULL,
    tag        TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_run_tags_tag ON run_tags(tag);
"""


async def _ensure_table(db) -> None:
    await db.executescript(_ENSURE_TABLE_SQL)


async def add_tag(session_id: str, tag: str) -> None:
    """Attach a free-form tag to a run (session). Idempotent (INSERT OR IGNORE)."""
    clean = (tag or "").strip()
    if not clean:
        raise HTTPException(status_code=422, detail="tag must not be empty")

    now = time.time()
    async with _open_db(_DB) as db:
        await _ensure_table(db)
        await db.execute(
            "INSERT OR IGNORE INTO run_tags (session_id, tag, created_at) VALUES (?, ?, ?)",
            (session_id, clean, now),
        )
        await db.commit()


async def remove_tag(session_id: str, tag: str) -> None:
    """Detach a tag from a run (session)."""
    async with _open_db(_DB) as db:
        await _ensure_table(db)
        await db.execute(
            "DELETE FROM run_tags WHERE session_id = ? AND tag = ?",
            (session_id, tag),
        )
        await db.commit()


async def tags_for_sessions(session_ids: list[str]) -> dict[str, list[str]]:
    """Batch-fetch tags for many sessions in ONE query (no N+1).

    Returns {session_id: [tags...]} for sessions that carry at least one tag;
    sessions with no tags are simply absent from the result (caller defaults
    to []).
    """
    if not session_ids:
        return {}

    placeholders = ",".join("?" for _ in session_ids)
    async with _open_db(_DB) as db:
        await _ensure_table(db)
        cur = await db.execute(
            f"SELECT session_id, tag FROM run_tags "  # noqa: S608
            f"WHERE session_id IN ({placeholders}) ORDER BY tag",
            session_ids,
        )
        rows = await cur.fetchall()

    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["session_id"], []).append(r["tag"])
    return out


async def session_ids_with_tags(tags: list[str]) -> set[str] | None:
    """The F8 SQL pre-filter: session_ids carrying ALL of `tags` (AND-composed).

    Contract: an empty/None `tags` list returns None, meaning "no tag filter
    requested" — callers must treat None as pass-through, not as "no matches".
    A non-empty list with no matching sessions returns an empty set.
    """
    if not tags:
        return None

    unique_tags = list(dict.fromkeys(tags))
    placeholders = ",".join("?" for _ in unique_tags)
    async with _open_db(_DB) as db:
        await _ensure_table(db)
        cur = await db.execute(
            f"SELECT session_id FROM run_tags "  # noqa: S608
            f"WHERE tag IN ({placeholders}) "
            f"GROUP BY session_id HAVING COUNT(DISTINCT tag) = ?",
            [*unique_tags, len(unique_tags)],
        )
        rows = await cur.fetchall()

    return {r["session_id"] for r in rows}


class TagBody(BaseModel):
    tag: str


@studio_route("/sessions/{session_id}/tags", method="POST", area="sessions", name="add_run_tag")
async def add_run_tag(session_id: str, body: TagBody) -> dict[str, Any]:
    await add_tag(session_id, body.tag)
    current = await tags_for_sessions([session_id])
    return {"session_id": session_id, "tags": current.get(session_id, [])}


@studio_route(
    "/sessions/{session_id}/tags/{tag}",
    method="DELETE",
    area="sessions",
    name="remove_run_tag",
)
async def remove_run_tag(session_id: str, tag: str) -> dict[str, Any]:
    await remove_tag(session_id, tag)
    current = await tags_for_sessions([session_id])
    return {"session_id": session_id, "tags": current.get(session_id, [])}
