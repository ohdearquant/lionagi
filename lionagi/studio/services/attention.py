# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Durable discharge lifecycle for Studio's needs-attention queue.

The attention queue itself stays client-derived (boardReducer.buildAttentionItems):
this module only persists what an operator decided about seeing one derived
item — acknowledged / resolved / expected / snoozed — keyed by the item id the
reducer already builds (``run:<id>`` | ``inv:<id>`` | ``sched:<id>``). It never
writes to a run, invocation, or schedule's own status; that stays the honest
record of what actually happened. Every write also appends to an append-only
history ledger so a discharged item can explain who discharged it, when, and
why.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from lionagi.state.db import StateDB

from ..registry import studio_route
from ._db import open_db as _open_db
from ._db import require_file_store, store_exists, store_path

_VALID_STATES = frozenset({"acknowledged", "resolved", "expected", "snoozed"})

# Mirrors attention_dispositions / attention_disposition_history in
# schema_meta.py -- a defensive fallback so a direct caller of this module
# (outside the studio app's lifespan, which applies the full StateDB schema
# on startup) never hits "no such table".
_ENSURE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS attention_dispositions (
  item_id        TEXT    PRIMARY KEY,
  state          TEXT    NOT NULL
                 CHECK(state IN ('acknowledged', 'resolved', 'expected', 'snoozed')),
  note           TEXT,
  created_at     REAL    NOT NULL,
  updated_at     REAL    NOT NULL,
  expires_at     REAL,
  actor          TEXT    NOT NULL,
  source_status  TEXT    NOT NULL,
  revision       INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS attention_disposition_revisions (
  item_id        TEXT    PRIMARY KEY,
  revision       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attention_disposition_history (
  id             TEXT    PRIMARY KEY,
  item_id        TEXT    NOT NULL,
  sequence       INTEGER NOT NULL,
  prior_state    TEXT,
  new_state      TEXT    NOT NULL,
  note           TEXT,
  actor          TEXT    NOT NULL,
  source_status  TEXT,
  created_at     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attention_disposition_history_item
  ON attention_disposition_history(item_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attention_disposition_history_sequence
  ON attention_disposition_history(sequence);
"""


async def _ensure_tables(db: Any) -> None:
    await db.executescript(_ENSURE_TABLES_SQL)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "item_id": row["item_id"],
        "state": row["state"],
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
        "actor": row["actor"],
        "source_status": row["source_status"],
        "revision": row["revision"],
    }


async def _next_history_sequence(db: Any) -> int:
    """Caller MUST already hold the write lock on *db* (a preceding
    ``BEGIN IMMEDIATE``) so the tail read is race-free. Mirrors the
    approval_evidence.sequence pattern -- a global append-order counter, not
    a per-item_id one, so equal-timestamp rows never tie on read."""
    cur = await db.execute(
        "SELECT sequence FROM attention_disposition_history ORDER BY sequence DESC LIMIT 1"
    )
    tail = await cur.fetchone()
    return (tail["sequence"] + 1) if tail is not None else 1


async def _last_known_revision(db: Any, item_id: str) -> int:
    """The revision of the last operation (PUT or DELETE) ever recorded for
    *item_id*, or 0 if none. Persists across DELETE so a PUT recreating the
    item afterward can still be fenced against it."""
    cur = await db.execute(
        "SELECT revision FROM attention_disposition_revisions WHERE item_id = ?", (item_id,)
    )
    row = await cur.fetchone()
    return row["revision"] if row is not None else 0


async def _record_revision(db: Any, item_id: str, revision: int) -> None:
    await db.execute(
        "INSERT INTO attention_disposition_revisions (item_id, revision) VALUES (?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET revision = excluded.revision",
        (item_id, revision),
    )


async def _ensure_store() -> None:
    require_file_store()
    if not store_exists():
        # Apply the full schema first so a disposition write never leaves a
        # partial db behind (only the attention tables, no sessions/etc).
        db = StateDB()
        await db.open()
        await db.close()


async def upsert_disposition(
    item_id: str,
    *,
    state: str,
    source_status: str,
    note: str | None = None,
    expires_at: float | None = None,
    actor: str = "operator",
    revision: int | None = None,
) -> dict[str, Any]:
    """Create-or-replace *item_id*'s disposition. Idempotent under retry:
    the same (item_id, state, ...) PUT replayed while the row is still
    active produces one current row plus one appended history entry per
    call, never a duplicate terminal state for a caller that only retries
    after a confirmed failure.

    *revision* fences the one case that isn't safe to leave unconditional:
    recreating a row that a DELETE has already removed. A PUT that finds no
    active row for item_id but a last-operation revision recorded for it
    (the item was created and then deleted) must carry a revision at least
    that high, or it is rejected (409) rather than resurrecting a stale
    disposition -- e.g. a delayed retry of the pre-delete PUT arriving after
    the undo, replaying an old expires_at. Updating an already-active row
    never fences: nothing was lost to resurrect."""
    if not item_id.strip():
        raise HTTPException(status_code=422, detail="item_id must not be empty")
    if state not in _VALID_STATES:
        raise HTTPException(status_code=422, detail=f"invalid disposition state: {state!r}")
    if state == "expected" and (not note or not note.strip()):
        raise HTTPException(status_code=422, detail="'expected' requires a non-blank note")
    if state in ("expected", "snoozed") and expires_at is None:
        raise HTTPException(status_code=422, detail=f"{state!r} requires expires_at")

    await _ensure_store()
    async with _open_db(store_path()) as db:
        await _ensure_tables(db)
        await db.execute("BEGIN IMMEDIATE")
        # Captured only after the write lock is held: reading the clock
        # earlier would let two racing writers commit history rows whose
        # created_at order disagrees with the true (lock-serialized) commit
        # order, corrupting the ORDER BY created_at ledger read.
        now = time.time()
        cur = await db.execute(
            "SELECT state, created_at FROM attention_dispositions WHERE item_id = ?",
            (item_id,),
        )
        existing = await cur.fetchone()
        last_revision = await _last_known_revision(db, item_id)
        if existing is None and last_revision > 0:
            if revision is None or revision < last_revision:
                await db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"stale revision for item_id {item_id!r}: "
                        f"disposition has moved on to revision {last_revision}"
                    ),
                )
        prior_state = existing["state"] if existing is not None else None
        created_at = existing["created_at"] if existing is not None else now
        new_revision = last_revision + 1
        await db.execute(
            "INSERT INTO attention_dispositions "
            "(item_id, state, note, created_at, updated_at, expires_at, actor, source_status, revision) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET "
            "state = excluded.state, note = excluded.note, updated_at = excluded.updated_at, "
            "expires_at = excluded.expires_at, actor = excluded.actor, "
            "source_status = excluded.source_status, revision = excluded.revision",
            (item_id, state, note, created_at, now, expires_at, actor, source_status, new_revision),
        )
        await _record_revision(db, item_id, new_revision)
        sequence = await _next_history_sequence(db)
        await db.execute(
            "INSERT INTO attention_disposition_history "
            "(id, item_id, sequence, prior_state, new_state, note, actor, source_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                item_id,
                sequence,
                prior_state,
                state,
                note,
                actor,
                source_status,
                now,
            ),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT item_id, state, note, created_at, updated_at, expires_at, actor, source_status, revision "
            "FROM attention_dispositions WHERE item_id = ?",
            (item_id,),
        )
        row = await cur.fetchone()
    assert row is not None  # just written, same transaction
    return _row_to_dict(row)


async def delete_disposition(item_id: str, *, actor: str = "operator") -> dict[str, Any]:
    """Remove *item_id*'s disposition (undo -> back to 'open'). A no-op
    when nothing is discharged, so a retried/duplicate undo never appends a
    second 'open' history row."""
    require_file_store()
    if not store_exists():
        return {"item_id": item_id, "deleted": False}

    async with _open_db(store_path()) as db:
        await _ensure_tables(db)
        # Cheap pre-check outside the lock so a stray retry against an
        # already-undone item never pays for BEGIN IMMEDIATE.
        cur = await db.execute("SELECT 1 FROM attention_dispositions WHERE item_id = ?", (item_id,))
        if await cur.fetchone() is None:
            return {"item_id": item_id, "deleted": False}

        await db.execute("BEGIN IMMEDIATE")
        # Re-read inside the lock: the pre-check above is racy against a
        # concurrent writer, so prior_state must come from the row this
        # transaction actually deletes, not the one it glimpsed earlier.
        cur = await db.execute(
            "SELECT state, source_status, revision FROM attention_dispositions WHERE item_id = ?",
            (item_id,),
        )
        existing = await cur.fetchone()
        if existing is None:
            await db.rollback()
            return {"item_id": item_id, "deleted": False}
        now = time.time()
        # Bump and persist the revision beyond the row's own deletion, so a
        # PUT that later tries to recreate item_id must beat this operation
        # too -- see upsert_disposition's fencing check.
        new_revision = existing["revision"] + 1
        await _record_revision(db, item_id, new_revision)
        await db.execute("DELETE FROM attention_dispositions WHERE item_id = ?", (item_id,))
        sequence = await _next_history_sequence(db)
        await db.execute(
            "INSERT INTO attention_disposition_history "
            "(id, item_id, sequence, prior_state, new_state, note, actor, source_status, created_at) "
            "VALUES (?, ?, ?, ?, 'open', NULL, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                item_id,
                sequence,
                existing["state"],
                actor,
                existing["source_status"],
                now,
            ),
        )
        await db.commit()
    return {"item_id": item_id, "deleted": True}


async def list_dispositions() -> dict[str, dict[str, Any]]:
    """Current, non-lapsed dispositions keyed by item_id -- a 'snoozed' or
    'expected' row past its expires_at lapses back to open here (no state
    mutation needed: the row simply stops being returned)."""
    require_file_store()
    if not store_exists():
        return {}

    now = time.time()
    async with _open_db(store_path()) as db:
        await _ensure_tables(db)
        cur = await db.execute(
            "SELECT item_id, state, note, created_at, updated_at, expires_at, actor, source_status, revision "
            "FROM attention_dispositions "
            "WHERE expires_at IS NULL OR expires_at > ?",
            (now,),
        )
        rows = await cur.fetchall()
    return {row["item_id"]: _row_to_dict(row) for row in rows}


async def disposition_history(item_id: str) -> list[dict[str, Any]]:
    require_file_store()
    if not store_exists():
        return []

    async with _open_db(store_path()) as db:
        await _ensure_tables(db)
        cur = await db.execute(
            "SELECT id, item_id, prior_state, new_state, note, actor, source_status, created_at "
            "FROM attention_disposition_history WHERE item_id = ? "
            "ORDER BY sequence ASC",
            (item_id,),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": row["id"],
            "item_id": row["item_id"],
            "prior_state": row["prior_state"],
            "new_state": row["new_state"],
            "note": row["note"],
            "actor": row["actor"],
            "source_status": row["source_status"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Route handlers — attention area
# ---------------------------------------------------------------------------


class _DispositionBody(BaseModel):
    state: Literal["acknowledged", "resolved", "expected", "snoozed"]
    note: str | None = None
    expires_at: float | None = None
    source_status: str = Field(..., min_length=1)
    actor: str | None = None
    # The revision GET/list last returned for this item_id, if the caller
    # has one. Required to recreate an item a DELETE has removed -- see
    # upsert_disposition.
    revision: int | None = None

    @field_validator("note")
    @classmethod
    def _blank_note_is_none(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            return None
        return v


@studio_route(
    "/attention/dispositions/",
    method="GET",
    area="attention",
    name="list_attention_dispositions",
)
async def list_attention_dispositions_route() -> dict[str, Any]:
    return {"dispositions": await list_dispositions()}


@studio_route(
    "/attention/dispositions/{item_id}",
    method="PUT",
    area="attention",
    name="put_attention_disposition",
)
async def put_attention_disposition_route(item_id: str, body: _DispositionBody) -> dict[str, Any]:
    actor = (body.actor or "").strip() or "operator"
    return await upsert_disposition(
        item_id,
        state=body.state,
        note=body.note,
        expires_at=body.expires_at,
        source_status=body.source_status,
        actor=actor,
        revision=body.revision,
    )


@studio_route(
    "/attention/dispositions/{item_id}",
    method="DELETE",
    area="attention",
    name="delete_attention_disposition",
)
async def delete_attention_disposition_route(item_id: str, request: Request) -> dict[str, Any]:
    actor = (request.headers.get("x-lionagi-actor") or "").strip() or "operator"
    return await delete_disposition(item_id, actor=actor)


@studio_route(
    "/attention/dispositions/{item_id}/history",
    method="GET",
    area="attention",
    name="get_attention_disposition_history",
)
async def get_attention_disposition_history_route(item_id: str) -> dict[str, Any]:
    return {"item_id": item_id, "history": await disposition_history(item_id)}
