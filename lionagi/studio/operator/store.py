# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""SQLite-backed durable store for the ADR-0083 Operator protocol.

These defensive ``CREATE TABLE IF NOT EXISTS`` statements make the protocol
usable by direct Studio service callers as well as through the normal
StateDB-opening daemon lifespan. The tables use the canonical StateDB file
and never fall back to process memory.

Streamed frames carry an explicit retention contract enforced on the write
path -- see ``MAX_FRAME_PAYLOAD_BYTES``, ``MAX_FRAMES_PER_TURN`` and
``MAX_TURN_PAYLOAD_BYTES``. Nothing is dropped silently: an oversized
payload is stored truncated and says so, and frames refused past a turn's
budget are folded into one ``truncation`` summary frame naming what was
elided. Terminal ``done`` frames are exempt so a turn can always close.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from lionagi.state import db as state_db_mod

from ..services._db import open_db, require_file_store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_operator_conversations (
  id                 TEXT PRIMARY KEY,
  project            TEXT,
  title              TEXT,
  status             TEXT NOT NULL DEFAULT 'active'
                     CHECK(status IN ('active', 'archived', 'deleted')),
  next_sequence      INTEGER NOT NULL DEFAULT 1,
  active_request_id  TEXT,
  -- The provider-side session this conversation resumes, so a second turn
  -- continues the first instead of starting a stranger.
  provider_session_id TEXT,
  provider_model      TEXT,
  -- Provider the conversation is currently pinned to; NULL means "use the
  -- env-var default" (see build_operator_branch), same as provider_model.
  provider            TEXT,
  -- What the last turn actually ran on, which is not the same fact as the pin
  -- above: an unpinned conversation has no pin and still ran on whatever the
  -- environment resolved to. The provider session belongs to this pair, so
  -- this is the pair the resume path compares against. NULL means no turn has
  -- recorded a resolution yet, which is not the same as "it changed".
  resolved_provider   TEXT,
  resolved_model      TEXT,
  -- The identity every turn's Branch is constructed with, so N turns of one
  -- conversation persist as one branch/session instead of N unrelated ones.
  -- NULL until the first turn claims it (see claim_branch_id) -- a
  -- conversation created before this column existed adopts one lazily on its
  -- next turn rather than through a history-rewriting migration.
  branch_id          TEXT,
  pinned             INTEGER NOT NULL DEFAULT 0,
  created_at         REAL NOT NULL,
  updated_at         REAL NOT NULL,
  archived_at        REAL,
  deleted_at         REAL
);

-- Where each page reporting into a conversation was last seen. A turn's own
-- context is frozen at submit, so without this the Operator answers "where am
-- I" with wherever the human was when they hit send, which is wrong exactly
-- when they have moved.
--
-- Keyed by the OBSERVER as well as the conversation, because ``observation_seq``
-- counts the views one page has seen and means nothing outside it. Two tabs on
-- one conversation count independently; keeping a single row per conversation
-- would make each tab's report erase the other's high-water mark, and a delayed
-- older report from the asking tab could then be re-admitted as its latest.
CREATE TABLE IF NOT EXISTS studio_operator_views (
  conversation_id   TEXT NOT NULL REFERENCES studio_operator_conversations(id),
  observer_id       TEXT NOT NULL,
  view_json         TEXT NOT NULL,
  observation_seq   INTEGER NOT NULL,
  updated_at        REAL NOT NULL,
  PRIMARY KEY(conversation_id, observer_id)
);

CREATE TABLE IF NOT EXISTS studio_operator_turns (
  request_id          TEXT PRIMARY KEY,
  conversation_id    TEXT NOT NULL REFERENCES studio_operator_conversations(id),
  instruction        TEXT NOT NULL,
  context_json        TEXT NOT NULL,
  context_hash        TEXT NOT NULL,
  status              TEXT NOT NULL
                      CHECK(status IN ('queued', 'running', 'awaiting_confirmation',
                                       'completed', 'failed', 'cancelled')),
  -- Effort is per-turn (unlike provider/model, it never invalidates a
  -- resumed provider session), so it lives on the turn, not the conversation.
  effort              TEXT,
  error_code          TEXT,
  created_at          REAL NOT NULL,
  started_at          REAL,
  ended_at            REAL,
  cancel_requested_at REAL
);

CREATE TABLE IF NOT EXISTS studio_operator_frames (
  conversation_id   TEXT NOT NULL REFERENCES studio_operator_conversations(id),
  sequence          INTEGER NOT NULL,
  request_id        TEXT NOT NULL REFERENCES studio_operator_turns(request_id),
  frame_type        TEXT NOT NULL,
  payload_json      TEXT NOT NULL,
  created_at        REAL NOT NULL,
  PRIMARY KEY(conversation_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_operator_frames_request
  ON studio_operator_frames(request_id, sequence);

CREATE TABLE IF NOT EXISTS studio_operator_proposals (
  id                 TEXT PRIMARY KEY,
  conversation_id   TEXT NOT NULL REFERENCES studio_operator_conversations(id),
  request_id         TEXT NOT NULL REFERENCES studio_operator_turns(request_id),
  command_type       TEXT NOT NULL,
  command_json       TEXT NOT NULL,
  command_hash       TEXT NOT NULL,
  target_version     TEXT,
  risk               TEXT NOT NULL CHECK(risk IN ('mutate', 'execute', 'admin')),
  summary            TEXT NOT NULL,
  idempotency_key    TEXT NOT NULL UNIQUE,
  status             TEXT NOT NULL
                     CHECK(status IN ('pending', 'confirmed', 'executing', 'succeeded',
                                      'failed', 'expired', 'cancelled', 'conflict')),
  expires_at         REAL NOT NULL,
  confirmed_at       REAL,
  completed_at       REAL,
  result_json        TEXT,
  error_code         TEXT,
  created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operator_proposals_request
  ON studio_operator_proposals(request_id, created_at);

CREATE TABLE IF NOT EXISTS studio_operator_effects (
  id                 TEXT PRIMARY KEY,
  conversation_id   TEXT NOT NULL REFERENCES studio_operator_conversations(id),
  request_id         TEXT NOT NULL REFERENCES studio_operator_turns(request_id),
  effect_type        TEXT NOT NULL,
  effect_json        TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending', 'applied', 'rejected', 'expired')),
  emitted_at         REAL NOT NULL,
  acknowledged_at    REAL,
  rejection_code     TEXT
);
"""

# Durable retention contract for streamed frames, enforced in append_frame so
# direct store callers cannot bypass it.
MAX_FRAME_PAYLOAD_BYTES = 64 * 1024
MAX_FRAMES_PER_TURN = 2000
MAX_TURN_PAYLOAD_BYTES = 8 * 1024 * 1024
# Reporting pages retained per conversation. Each page load is its own reporter,
# so this bounds what a long-lived conversation accumulates.
MAX_REPORTING_VIEWS_PER_CONVERSATION = 8
TRUNCATION_FRAME_TYPE = "truncation"

# Distinguishes "field omitted from a partial update" from "field explicitly
# set to None/False" -- title in particular is legitimately nullable.
_UNSET: Any = object()


class OperatorStoreError(RuntimeError):
    code = "service_failure"


class OperatorNotFoundError(OperatorStoreError):
    code = "not_found"


class OperatorAuditUnavailableError(OperatorStoreError):
    code = "audit_unavailable"


class OperatorConflictError(OperatorStoreError):
    code = "conflict"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class OperatorValidationError(OperatorStoreError):
    code = "validation"


class OperatorStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else None
        self._schema_ready: tuple[Path, int, int] | None = None
        self._schema_lock = asyncio.Lock()

    def path(self) -> Path:
        """The StateDB file these tables live in.

        A store with no file at all is refused as
        :class:`StoreNotAddressableError` rather than as an
        ``OperatorStoreError``, because those two say different things to a
        caller. An Operator error is about this subsystem and the routes map it
        to 503, which invites a retry; a server-backed or in-memory store is
        not a condition that resolves by waiting, and the app answers 501 for
        it everywhere else. Reusing the existing refusal also keeps one
        definition of which stores this SQLite-direct layer can open, instead of
        a second one here that could drift from it.
        """
        if self._db_path is not None:
            return self._db_path
        require_file_store()
        path = state_db_mod.state_db_file()
        if path is None:  # pragma: no cover — require_file_store already refused
            raise OperatorStoreError(
                "Studio Operator currently requires a local StateDB file; "
                "no ephemeral fallback is available"
            )
        return path

    async def ensure_schema(self) -> None:
        path = self.path().resolve()
        try:
            stat = path.stat()
            identity = (path, stat.st_dev, stat.st_ino)
        except FileNotFoundError:
            identity = None
        if self._schema_ready == identity and identity is not None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        async with self._schema_lock:
            try:
                stat = path.stat()
                identity = (path, stat.st_dev, stat.st_ino)
            except FileNotFoundError:
                identity = None
            if self._schema_ready == identity and identity is not None:
                return
            async with open_db(str(path)) as db:
                await db.executescript(_SCHEMA)
                # CREATE TABLE IF NOT EXISTS is a no-op on a database created
                # before a column was added, so additive columns need their own
                # migration or an existing conversation store silently lacks them.
                await self._add_missing_columns(
                    db,
                    "studio_operator_conversations",
                    {
                        "provider_session_id": "TEXT",
                        "provider_model": "TEXT",
                        "provider": "TEXT",
                        "resolved_provider": "TEXT",
                        "resolved_model": "TEXT",
                        "pinned": "INTEGER NOT NULL DEFAULT 0",
                        "branch_id": "TEXT",
                    },
                )
                await self._add_missing_columns(
                    db,
                    "studio_operator_turns",
                    {"effort": "TEXT"},
                )
                await db.commit()
            stat = path.stat()
            self._schema_ready = (path, stat.st_dev, stat.st_ino)

    @staticmethod
    async def _add_missing_columns(db: Any, table: str, columns: dict[str, str]) -> None:
        """Add additive columns a pre-existing database was created without."""
        rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
        present = {row[1] for row in rows}
        for name, decl in columns.items():
            if name not in present:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _byte_len(text: str) -> int:
        return len(text.encode("utf-8"))

    @classmethod
    def _truncate_strings(cls, value: Any, budget: int) -> Any:
        if isinstance(value, str):
            raw = value.encode("utf-8")
            if len(raw) <= budget:
                return value
            kept = raw[:budget].decode("utf-8", "ignore")
            elided = len(raw) - len(kept.encode("utf-8"))
            return f"{kept}…[{elided} bytes elided]"
        if isinstance(value, dict):
            return {key: cls._truncate_strings(item, budget) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._truncate_strings(item, budget) for item in value]
        return value

    @classmethod
    def _cap_frame_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a payload within ``MAX_FRAME_PAYLOAD_BYTES`` that states any elision."""
        original_bytes = cls._byte_len(cls._json(payload))
        if original_bytes <= MAX_FRAME_PAYLOAD_BYTES:
            return payload
        capped = cls._truncate_strings(payload, MAX_FRAME_PAYLOAD_BYTES // 2)
        note = {
            "reason": "frame_payload_bytes",
            "limitBytes": MAX_FRAME_PAYLOAD_BYTES,
            "originalBytes": original_bytes,
        }
        capped = {**capped, "truncation": note}
        # Leave headroom for the storedBytes field added below.
        if cls._byte_len(cls._json(capped)) > MAX_FRAME_PAYLOAD_BYTES - 64:
            # The payload's own structure, not one long string, is oversized.
            fields = sorted(map(str, payload))
            capped = {
                "truncation": {
                    **note,
                    "elidedFieldCount": len(fields),
                    "elidedFields": [field[:64] for field in fields[:32]],
                }
            }
        capped["truncation"]["storedBytes"] = cls._byte_len(cls._json(capped))
        return capped

    @staticmethod
    def canonical_hash(value: Any) -> str:
        return hashlib.sha256(OperatorStore._json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _conversation(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project": row["project"],
            "title": row["title"],
            "status": row["status"],
            "pinned": bool(row["pinned"]),
            "nextSequence": row["next_sequence"],
            "activeRequestId": row["active_request_id"],
            "providerSessionId": row["provider_session_id"],
            "providerModel": row["provider_model"],
            "provider": row["provider"],
            "branchId": row["branch_id"],
            # Served beside the pin because a session that resets has to be
            # explainable: without these, "my conversation started over" has no
            # visible cause anywhere in the UI or the API.
            "resolvedProvider": row["resolved_provider"],
            "resolvedModel": row["resolved_model"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _frame(row: Any) -> dict[str, Any]:
        return {
            "version": 1,
            "conversationId": row["conversation_id"],
            "requestId": row["request_id"],
            "sequence": row["sequence"],
            "type": row["frame_type"],
            "payload": json.loads(row["payload_json"]),
            "createdAt": row["created_at"],
        }

    @staticmethod
    def _proposal(row: Any) -> dict[str, Any]:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return {
            "id": row["id"],
            "conversationId": row["conversation_id"],
            "requestId": row["request_id"],
            "commandType": row["command_type"],
            "command": json.loads(row["command_json"]),
            "commandHash": row["command_hash"],
            "targetVersion": row["target_version"],
            "risk": row["risk"],
            "summary": row["summary"],
            "idempotencyKey": row["idempotency_key"],
            "status": row["status"],
            "expiresAt": row["expires_at"],
            "confirmedAt": row["confirmed_at"],
            "completedAt": row["completed_at"],
            "result": result,
            "errorCode": row["error_code"],
            "createdAt": row["created_at"],
        }

    @staticmethod
    def _proposal_target(
        command_type: str,
        command: dict[str, Any],
        target_version: str | None,
    ) -> dict[str, str] | None:
        if target_version is None:
            return None
        if (
            command_type == "launch"
            and command.get("action_kind") == "play"
            and isinstance(command.get("action_playbook"), str)
        ):
            return {
                "kind": "playbook",
                "id": command["action_playbook"],
                "version": target_version,
            }
        return {
            "kind": command_type,
            "id": command_type,
            "version": target_version,
        }

    @classmethod
    def _proposal_audit_details(
        cls,
        row: Any,
        *,
        decision: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        confirmed_at: float | None = None,
        completed_at: float | None = None,
    ) -> dict[str, Any]:
        """Build the exact ADR-0083 audit envelope from a proposal row."""
        target = cls._proposal_target(
            row["command_type"],
            json.loads(row["command_json"]),
            row["target_version"],
        )
        return {
            "conversation_id": row["conversation_id"],
            "request_id": row["request_id"],
            "proposal_id": row["id"],
            "command_type": row["command_type"],
            "command_hash": row["command_hash"],
            "target": target,
            "risk": row["risk"],
            "idempotency_key": row["idempotency_key"],
            "decision": decision,
            "result": result or {},
            "error_code": error_code,
            "confirmed_at": confirmed_at,
            "completed_at": completed_at,
        }

    async def create_conversation(
        self, *, project: str | None = None, title: str | None = None
    ) -> dict[str, Any]:
        await self.ensure_schema()
        now = time.time()
        conversation_id = str(uuid.uuid4())
        async with open_db(str(self.path())) as db:
            await db.execute(
                "INSERT INTO studio_operator_conversations "
                "(id, project, title, status, next_sequence, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', 1, ?, ?)",
                (conversation_id, project, title, now, now),
            )
            await db.commit()
        return await self.get_conversation(conversation_id)

    async def list_conversations(
        self, *, limit: int = 100, status: str = "active"
    ) -> list[dict[str, Any]]:
        await self.ensure_schema()
        if status not in ("active", "archived", "all"):
            raise OperatorConflictError(f"Unsupported conversation status filter '{status}'")
        async with open_db(str(self.path())) as db:
            if status == "all":
                query = (
                    "SELECT * FROM studio_operator_conversations WHERE status != 'deleted' "
                    "ORDER BY pinned DESC, updated_at DESC LIMIT ?"
                )
                params: tuple[Any, ...] = (limit,)
            else:
                query = (
                    "SELECT * FROM studio_operator_conversations WHERE status = ? "
                    "ORDER BY pinned DESC, updated_at DESC LIMIT ?"
                )
                params = (status, limit)
            rows = await (await db.execute(query, params)).fetchall()
        return [self._conversation(row) for row in rows]

    async def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None | object = _UNSET,
        pinned: bool | object = _UNSET,
        status: str | object = _UNSET,
    ) -> dict[str, Any]:
        """Apply a partial update: rename, pin/unpin, and/or archive/reactivate.

        Only the fields the caller actually names (not left at ``_UNSET``) are
        touched, so a rename never disturbs the pin state and vice versa.
        Archiving is refused while a turn is in flight, matching the delete
        restriction: it removes the conversation from the active list rather
        than interrupting work in progress.
        """
        await self.ensure_schema()
        if status is not _UNSET and status not in ("active", "archived"):
            raise OperatorConflictError(f"Unsupported conversation status '{status}'")
        now = time.time()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT status, active_request_id FROM studio_operator_conversations "
                    "WHERE id = ?",
                    (conversation_id,),
                )
            ).fetchone()
            if row is None or row["status"] == "deleted":
                await db.rollback()
                raise OperatorNotFoundError(f"Operator conversation '{conversation_id}' not found")
            if status == "archived" and row["active_request_id"]:
                await db.rollback()
                raise OperatorConflictError("Cannot archive a conversation with an active turn")
            sets: list[str] = []
            params: list[Any] = []
            if title is not _UNSET:
                sets.append("title=?")
                params.append(title)
            if pinned is not _UNSET:
                sets.append("pinned=?")
                params.append(1 if pinned else 0)
            if status is not _UNSET:
                sets.append("status=?")
                params.append(status)
                sets.append("archived_at=?")
                params.append(now if status == "archived" else None)
            if sets:
                sets.append("updated_at=?")
                params.append(now)
                params.append(conversation_id)
                await db.execute(
                    f"UPDATE studio_operator_conversations SET {', '.join(sets)} "  # noqa: S608
                    "WHERE id=?",
                    params,
                )
            await db.commit()
        return await self.get_conversation(conversation_id)

    async def fork_conversation(
        self,
        conversation_id: str,
        *,
        up_to_sequence: int | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Copy a conversation's completed turns into a new, independent conversation.

        Only turns that reached a terminal status (completed/failed/cancelled)
        are copied whole -- forking a conversation with a turn actively
        streaming therefore ends the fork at the last completed turn instead
        of copying a half-written one; the original conversation keeps
        streaming untouched. ``up_to_sequence``, when given, additionally caps
        the fork at the turn ending at or before that point in the source
        conversation's history, so a user can branch from any earlier turn
        rather than always from the tip. The new conversation starts with no
        provider session of its own, so it does not resume the source's
        provider-side session.
        """
        await self.ensure_schema()
        source = await self.get_conversation(conversation_id)
        now = time.time()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            query = (
                "SELECT t.request_id AS request_id, MIN(f.sequence) AS first_sequence "
                "FROM studio_operator_turns t "
                "JOIN studio_operator_frames f ON f.request_id = t.request_id "
                "WHERE t.conversation_id=? AND t.status IN ('completed','failed','cancelled') "
            )
            params: list[Any] = [conversation_id]
            if up_to_sequence is not None:
                query += "GROUP BY t.request_id HAVING MAX(f.sequence) <= ? "
                params.append(up_to_sequence)
            else:
                query += "GROUP BY t.request_id "
            query += "ORDER BY first_sequence ASC"
            turn_rows = await (await db.execute(query, params)).fetchall()
            ordered_request_ids = [row["request_id"] for row in turn_rows]

            new_id = str(uuid.uuid4())
            if title is not None:
                new_title = title
            elif source["title"]:
                new_title = f"{source['title']} (fork)"
            else:
                new_title = f"Fork of {conversation_id[:8]}"

            await db.execute(
                "INSERT INTO studio_operator_conversations "
                "(id, project, title, status, next_sequence, provider, provider_model, "
                "created_at, updated_at) VALUES (?, ?, ?, 'active', 1, ?, ?, ?, ?)",
                (
                    new_id,
                    source["project"],
                    new_title,
                    # A pin is the provider and the model together. Copying the
                    # model alone would leave the fork resolving its provider
                    # from the environment, so a conversation pinned to one
                    # provider's model would fork into a conversation that runs
                    # that model name against whatever provider the environment
                    # names.
                    source["provider"],
                    source["providerModel"],
                    now,
                    now,
                ),
            )
            next_sequence = 1
            if ordered_request_ids:
                placeholders = ",".join("?" for _ in ordered_request_ids)
                turns = await (
                    await db.execute(
                        "SELECT * FROM studio_operator_turns "  # noqa: S608
                        f"WHERE request_id IN ({placeholders})",
                        tuple(ordered_request_ids),
                    )
                ).fetchall()
                turns_by_id = {row["request_id"]: row for row in turns}
                request_id_map = {rid: str(uuid.uuid4()) for rid in ordered_request_ids}
                for original_request_id in ordered_request_ids:
                    turn = turns_by_id[original_request_id]
                    new_request_id = request_id_map[original_request_id]
                    await db.execute(
                        "INSERT INTO studio_operator_turns "
                        "(request_id, conversation_id, instruction, context_json, "
                        "context_hash, status, error_code, created_at, started_at, ended_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_request_id,
                            new_id,
                            turn["instruction"],
                            turn["context_json"],
                            turn["context_hash"],
                            turn["status"],
                            turn["error_code"],
                            turn["created_at"],
                            turn["started_at"],
                            turn["ended_at"],
                        ),
                    )
                    frames = await (
                        await db.execute(
                            "SELECT * FROM studio_operator_frames "
                            "WHERE request_id=? ORDER BY sequence ASC",
                            (original_request_id,),
                        )
                    ).fetchall()
                    for frame in frames:
                        await db.execute(
                            "INSERT INTO studio_operator_frames "
                            "(conversation_id, sequence, request_id, frame_type, "
                            "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                new_id,
                                next_sequence,
                                new_request_id,
                                frame["frame_type"],
                                frame["payload_json"],
                                frame["created_at"],
                            ),
                        )
                        next_sequence += 1
                await db.execute(
                    "UPDATE studio_operator_conversations SET next_sequence=? WHERE id=?",
                    (next_sequence, new_id),
                )
            await db.commit()
        return await self.get_conversation(new_id)

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            row = await (
                await db.execute(
                    "SELECT * FROM studio_operator_conversations WHERE id = ?",
                    (conversation_id,),
                )
            ).fetchone()
        if row is None or row["status"] == "deleted":
            raise OperatorNotFoundError(f"Operator conversation '{conversation_id}' not found")
        return self._conversation(row)

    async def set_provider_session_id(self, conversation_id: str, session_id: str) -> None:
        """Remember the provider session so the next turn resumes this one.

        Written every turn rather than only the first: the provider is free to
        hand back a new session id on resume, and pinning the first one forever
        would silently stop resuming the moment it does.
        """
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            await db.execute(
                "UPDATE studio_operator_conversations "
                "SET provider_session_id = ?, updated_at = ? WHERE id = ?",
                (session_id, time.time(), conversation_id),
            )
            await db.commit()

    async def record_view(
        self, conversation_id: str, view: dict[str, Any], seq: int, observer: str
    ) -> bool:
        """Record where the page *observer* is now, independently of any turn.

        *seq* is how many views that page had seen when it saw this one. Each
        report is its own request, so two navigations by one page can arrive
        reversed; a report that does not count higher than that page's own
        stored count is DISCARDED, because otherwise the stale view wins and is
        still labelled current, which is the exact failure this whole mechanism
        exists to fix. Each page gets its own row -- see the schema comment on
        ``studio_operator_views`` for why a shared row would be wrong.

        Returns whether the report was applied.
        """
        await self.ensure_schema()
        now = time.time()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            conversation = await (
                await db.execute(
                    "SELECT id FROM studio_operator_conversations WHERE id = ?",
                    (conversation_id,),
                )
            ).fetchone()
            if conversation is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator conversation '{conversation_id}' not found")
            row = await (
                await db.execute(
                    "SELECT observation_seq FROM studio_operator_views "
                    "WHERE conversation_id = ? AND observer_id = ?",
                    (conversation_id, observer),
                )
            ).fetchone()
            if row is not None and seq <= row["observation_seq"]:
                await db.rollback()
                return False
            await db.execute(
                "INSERT INTO studio_operator_views "
                "(conversation_id, observer_id, view_json, observation_seq, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(conversation_id, observer_id) DO UPDATE SET "
                "view_json = excluded.view_json, observation_seq = excluded.observation_seq, "
                "updated_at = excluded.updated_at",
                (conversation_id, observer, self._json(view), seq, now),
            )
            # Every reload is a new page, so these rows would otherwise
            # accumulate for the life of the conversation. Evicting the quietest
            # costs that page its freshness and nothing else: a turn from an
            # evicted page falls back to its own frozen snapshot, which is the
            # honest answer rather than a wrong one.
            await db.execute(
                "DELETE FROM studio_operator_views WHERE conversation_id = ? AND observer_id NOT IN "
                "(SELECT observer_id FROM studio_operator_views WHERE conversation_id = ? "
                "ORDER BY updated_at DESC LIMIT ?)",
                (conversation_id, conversation_id, MAX_REPORTING_VIEWS_PER_CONVERSATION),
            )
            await db.commit()
        return True

    async def get_view(
        self, conversation_id: str, observer: str
    ) -> tuple[dict[str, Any] | None, int | None]:
        """Return where *observer* was last seen, and its count when it saw it.

        Scoped to one page on purpose. The count means nothing outside the page
        that made it, so answering with another page's view would be answering
        for a window the human may not even be looking at.
        """
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            row = await (
                await db.execute(
                    "SELECT view_json, observation_seq FROM studio_operator_views "
                    "WHERE conversation_id = ? AND observer_id = ?",
                    (conversation_id, observer),
                )
            ).fetchone()
        if row is None:
            return None, None
        return json.loads(row["view_json"]), row["observation_seq"]

    async def claim_resolved_pair(
        self,
        conversation_id: str,
        *,
        provider: str,
        model: str,
    ) -> str | None:
        """Record what this turn will run on and return the session it may resume.

        A provider session belongs to the (provider, model) pair that created it.
        An unpinned conversation still runs on whatever the environment resolves
        to, re-read every turn, so this compares the pair about to run against
        the pair that last ran rather than relying on the pin alone.

        Returns the session id to resume with, or ``None`` when the pair moved and
        the stored session no longer belongs to this turn.

        A stored pair of ``NULL`` means no turn has recorded a resolution yet, not
        that anything changed -- treating it as a mismatch would drop every
        existing session's resume on its first use after this column was added.
        """
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT provider_session_id, resolved_provider, resolved_model "
                    "FROM studio_operator_conversations WHERE id = ?",
                    (conversation_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator conversation '{conversation_id}' not found")
            session_id = row["provider_session_id"]
            known = row["resolved_provider"] is not None
            moved = known and (row["resolved_provider"], row["resolved_model"]) != (
                provider,
                model,
            )
            if moved:
                await db.execute(
                    "UPDATE studio_operator_conversations "
                    "SET resolved_provider = ?, resolved_model = ?, "
                    "provider_session_id = NULL, updated_at = ? WHERE id = ?",
                    (provider, model, time.time(), conversation_id),
                )
            else:
                await db.execute(
                    "UPDATE studio_operator_conversations "
                    "SET resolved_provider = ?, resolved_model = ?, updated_at = ? "
                    "WHERE id = ?",
                    (provider, model, time.time(), conversation_id),
                )
            await db.commit()
        return None if moved else session_id

    async def claim_branch_id(self, conversation_id: str) -> str:
        """Return the identity every turn of this conversation builds its Branch
        with, minting and persisting one on the first call.

        Stores an IDENTITY (a UUID), not a live Branch -- turns arrive as separate
        requests and the daemon restarts between them, so no Python object is
        shared across turns. Idempotent and race-safe under the store's usual
        ``BEGIN IMMEDIATE`` transaction: a losing racer blocks until the winner
        commits, then reads back and returns the id it wrote instead of minting
        its own.

        A conversation created before this column existed adopts an id lazily on
        its first post-upgrade turn instead of through a migration; already-
        recorded turns keep their original grouping. See docs/internals/store.md
        for the full rationale.
        """
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT branch_id FROM studio_operator_conversations WHERE id = ?",
                    (conversation_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator conversation '{conversation_id}' not found")
            existing = row["branch_id"]
            if isinstance(existing, str) and existing:
                await db.rollback()
                return existing
            new_branch_id = str(uuid.uuid4())
            await db.execute(
                "UPDATE studio_operator_conversations SET branch_id = ?, updated_at = ? "
                "WHERE id = ?",
                (new_branch_id, time.time(), conversation_id),
            )
            await db.commit()
        return new_branch_id

    @staticmethod
    async def _write_selection(
        db: Any,
        conversation_id: str,
        *,
        row_provider: str | None,
        row_model: str | None,
        provider: str | None,
        model: str | None,
    ) -> None:
        """Apply a provider/model pin inside the caller's open transaction.

        Takes the row's current values rather than re-reading them, so the
        decision to drop the session is made against the same snapshot the
        caller validated. Both ``provider`` and ``model`` as ``None`` means
        clear the pin; otherwise a ``None`` leaves that column untouched.
        """
        clearing = provider is None and model is None
        if clearing:
            if row_provider is None and row_model is None:
                return
            next_provider: str | None = None
            next_model: str | None = None
            changed = True
        else:
            next_provider = provider if provider is not None else row_provider
            next_model = model if model is not None else row_model
            # A provider session belongs to the pair that created it, so any
            # explicitly supplied value that differs from the stored one
            # invalidates it. A stored NULL counts as a difference rather than
            # as "nothing to invalidate": an unpinned conversation still ran on
            # whatever the environment resolved to, and the session it holds
            # belongs to that pair, so the first explicit pin is a change of
            # pair like any other. Re-sending the value already stored is not a
            # change, which is what keeps a session alive across the turns of a
            # conversation whose composer submits its pin every time.
            changed = (provider is not None and row_provider != provider) or (
                model is not None and row_model != model
            )
        if changed:
            await db.execute(
                "UPDATE studio_operator_conversations "
                "SET provider = ?, provider_model = ?, provider_session_id = NULL, "
                "updated_at = ? WHERE id = ?",
                (next_provider, next_model, time.time(), conversation_id),
            )
        else:
            await db.execute(
                "UPDATE studio_operator_conversations "
                "SET provider = ?, provider_model = ?, updated_at = ? WHERE id = ?",
                (next_provider, next_model, time.time(), conversation_id),
            )

    async def select_provider_model(
        self,
        conversation_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Record the provider and/or model for this conversation, dropping a stale session.

        A provider session belongs to the (provider, model) pair that created
        it, so resuming one under a different provider or model is undefined.
        Changing either therefore starts a fresh session on purpose rather
        than resuming into a mismatch. A ``None`` argument leaves that column
        untouched -- e.g. selecting only a model on a conversation that
        already pinned a provider does not clear the provider pin.
        """
        if provider is None and model is None:
            return
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT provider, provider_model FROM studio_operator_conversations "
                    "WHERE id = ?",
                    (conversation_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator conversation '{conversation_id}' not found")
            await self._write_selection(
                db,
                conversation_id,
                row_provider=row["provider"],
                row_model=row["provider_model"],
                provider=provider,
                model=model,
            )
            await db.commit()

    async def clear_provider_model(self, conversation_id: str) -> None:
        """Drop this conversation's provider/model pin so it runs on the default again.

        Omitting a model on a turn means "leave the pin alone", which is what
        an unchanged composer sends, so it cannot also mean "remove the pin" --
        without this there is no way back to the daemon's own default once a
        conversation has been pinned. The provider session goes with it: a
        session belongs to the pair that created it, and the next turn resolves
        its provider from the environment rather than from that pair.

        Clearing a conversation that has no pin does nothing at all. Dropping
        the session is a consequence of the pin changing, so a repeated clear
        must not keep discarding sessions that no pin is invalidating.
        """
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT provider, provider_model FROM studio_operator_conversations "
                    "WHERE id = ?",
                    (conversation_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator conversation '{conversation_id}' not found")
            if row["provider"] is None and row["provider_model"] is None:
                await db.rollback()
                return
            await self._write_selection(
                db,
                conversation_id,
                row_provider=row["provider"],
                row_model=row["provider_model"],
                provider=None,
                model=None,
            )
            await db.commit()

    async def archive_or_delete(self, conversation_id: str) -> None:
        await self.ensure_schema()
        now = time.time()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT status, active_request_id FROM studio_operator_conversations "
                    "WHERE id = ?",
                    (conversation_id,),
                )
            ).fetchone()
            if row is None or row["status"] == "deleted":
                await db.rollback()
                raise OperatorNotFoundError(f"Operator conversation '{conversation_id}' not found")
            if row["active_request_id"]:
                await db.rollback()
                raise OperatorConflictError("Cannot delete a conversation with an active turn")
            await db.execute(
                "UPDATE studio_operator_conversations SET status='deleted', deleted_at=?, "
                "updated_at=? WHERE id=?",
                (now, now, conversation_id),
            )
            await db.commit()

    async def list_frames(
        self, conversation_id: str, *, after_sequence: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        await self.get_conversation(conversation_id)
        async with open_db(str(self.path())) as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM studio_operator_frames "
                    "WHERE conversation_id=? AND sequence>? "
                    "ORDER BY sequence ASC LIMIT ?",
                    (conversation_id, after_sequence, limit),
                )
            ).fetchall()
        return [self._frame(row) for row in rows]

    async def list_recent_frames(
        self, conversation_id: str, *, limit: int = 64
    ) -> list[dict[str, Any]]:
        """Return the newest ``limit`` frames, ordered chronologically."""
        await self.get_conversation(conversation_id)
        async with open_db(str(self.path())) as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM (SELECT * FROM studio_operator_frames "
                    "WHERE conversation_id=? ORDER BY sequence DESC LIMIT ?) "
                    "ORDER BY sequence ASC",
                    (conversation_id, limit),
                )
            ).fetchall()
        return [self._frame(row) for row in rows]

    async def list_complete_turn_frame_groups(
        self,
        conversation_id: str,
        *,
        exclude_request_id: str,
        limit: int = 64,
    ) -> list[tuple[dict[str, Any], ...]]:
        """Return recent durable turn frames, grouped newest-complete-turn first."""
        await self.get_conversation(conversation_id)
        bounded_limit = max(1, min(limit, 64))
        async with open_db(str(self.path())) as db:
            requests = await (
                await db.execute(
                    "SELECT t.request_id, MAX(f.sequence) AS terminal_sequence "
                    "FROM studio_operator_turns AS t "
                    "JOIN studio_operator_frames AS f "
                    "ON f.request_id=t.request_id AND f.frame_type='done' "
                    "WHERE t.conversation_id=? AND t.request_id!=? "
                    "AND t.status IN ('completed','failed','cancelled') "
                    "GROUP BY t.request_id "
                    "ORDER BY terminal_sequence DESC LIMIT ?",
                    (conversation_id, exclude_request_id, bounded_limit),
                )
            ).fetchall()
            request_ids = [str(row["request_id"]) for row in requests]
            if not request_ids:
                return []
            placeholders = ",".join("?" for _ in request_ids)
            query = f"""\
SELECT * FROM studio_operator_frames
WHERE request_id IN ({placeholders}) ORDER BY sequence ASC
"""  # noqa: S608
            rows = await (await db.execute(query, tuple(request_ids))).fetchall()

        grouped: dict[str, list[dict[str, Any]]] = {request_id: [] for request_id in request_ids}
        for row in rows:
            grouped[str(row["request_id"])].append(self._frame(row))
        return [tuple(grouped[request_id]) for request_id in request_ids]

    async def record_context_compilation(
        self, request_id: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist the exact durable-frame compilation receipt for one turn."""
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT context_json FROM studio_operator_turns WHERE request_id=?",
                    (request_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator request '{request_id}' not found")
            context = json.loads(row["context_json"])
            context["operatorCompilation"] = dict(metadata)
            context_json = self._json(context)
            await db.execute(
                "UPDATE studio_operator_turns SET context_json=?, context_hash=? "
                "WHERE request_id=?",
                (context_json, self.canonical_hash(context), request_id),
            )
            await db.commit()
        return context

    async def submit_turn(
        self,
        conversation_id: str,
        *,
        instruction: str,
        context: dict[str, Any],
        expected_last_sequence: int,
        effort: str | None = None,
        select_provider: str | None = None,
        select_model: str | None = None,
        clear_selection: bool = False,
    ) -> dict[str, Any]:
        """Accept a turn, applying any provider/model change in the same transaction.

        The selection rides here rather than being written before the call
        because a turn that is refused must not leave the conversation
        changed. Reserving the turn and moving the pin are decided against one
        snapshot of the row: either both land or neither does. The pin still
        applies to this turn, since it is committed before the turn is
        readable.
        """
        await self.ensure_schema()
        request_id = str(uuid.uuid4())
        now = time.time()
        context_json = self._json(context)
        context_hash = self.canonical_hash(context)
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT status, next_sequence, active_request_id, provider, provider_model "
                    "FROM studio_operator_conversations WHERE id=?",
                    (conversation_id,),
                )
            ).fetchone()
            if row is None or row["status"] == "deleted":
                await db.rollback()
                raise OperatorNotFoundError(f"Operator conversation '{conversation_id}' not found")
            if row["status"] != "active":
                await db.rollback()
                raise OperatorConflictError("Archived Operator conversations are read-only")
            if row["active_request_id"] is not None:
                await db.rollback()
                raise OperatorConflictError(
                    "This conversation already has an active turn",
                    details={"activeRequestId": row["active_request_id"]},
                )
            actual_last = int(row["next_sequence"]) - 1
            if expected_last_sequence != actual_last:
                await db.rollback()
                raise OperatorConflictError(
                    "Conversation cursor is stale",
                    details={
                        "code": "stale_context",
                        "expectedLastSequence": expected_last_sequence,
                        "actualLastSequence": actual_last,
                    },
                )
            # Past every rejection, so nothing below can refuse the turn after
            # the pin has moved.
            if clear_selection or select_provider is not None or select_model is not None:
                await self._write_selection(
                    db,
                    conversation_id,
                    row_provider=row["provider"],
                    row_model=row["provider_model"],
                    provider=None if clear_selection else select_provider,
                    model=None if clear_selection else select_model,
                )
            sequence = int(row["next_sequence"])
            await db.execute(
                "INSERT INTO studio_operator_turns "
                "(request_id, conversation_id, instruction, context_json, context_hash, "
                "status, effort, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
                (
                    request_id,
                    conversation_id,
                    instruction,
                    context_json,
                    context_hash,
                    effort,
                    now,
                ),
            )
            await db.execute(
                "UPDATE studio_operator_conversations SET active_request_id=?, "
                "next_sequence=?, updated_at=? WHERE id=? AND active_request_id IS NULL",
                (request_id, sequence + 1, now, conversation_id),
            )
            await db.execute(
                "INSERT INTO studio_operator_frames "
                "(conversation_id, sequence, request_id, frame_type, payload_json, created_at) "
                "VALUES (?, ?, ?, 'text', ?, ?)",
                (
                    conversation_id,
                    sequence,
                    request_id,
                    self._json(
                        {
                            "content": instruction,
                            "format": "plain",
                            # Additive discriminator used by the chat renderer.
                            "role": "user",
                        }
                    ),
                    now,
                ),
            )
            await db.commit()
        return {
            "conversationId": conversation_id,
            "requestId": request_id,
            "acceptedSequence": sequence,
        }

    async def mark_running(self, request_id: str) -> bool:
        await self.ensure_schema()
        now = time.time()
        async with open_db(str(self.path())) as db:
            cur = await db.execute(
                "UPDATE studio_operator_turns SET status='running', started_at=? "
                "WHERE request_id=? AND status='queued'",
                (now, request_id),
            )
            await db.commit()
        return cur.rowcount == 1

    async def get_turn(self, request_id: str) -> dict[str, Any]:
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            row = await (
                await db.execute(
                    "SELECT * FROM studio_operator_turns WHERE request_id=?", (request_id,)
                )
            ).fetchone()
        if row is None:
            raise OperatorNotFoundError(f"Operator request '{request_id}' not found")
        return {
            "requestId": row["request_id"],
            "conversationId": row["conversation_id"],
            "instruction": row["instruction"],
            "context": json.loads(row["context_json"]),
            "contextHash": row["context_hash"],
            "status": row["status"],
            "effort": row["effort"],
            "errorCode": row["error_code"],
            "cancelRequestedAt": row["cancel_requested_at"],
        }

    @staticmethod
    async def _turn_budget_exceeded(db: Any, request_id: str, payload_bytes: int) -> str | None:
        """Name the retention limit this frame would breach, or None if it fits."""
        usage = await (
            await db.execute(
                "SELECT COUNT(*) AS frame_count, "
                "COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))), 0) AS payload_bytes "
                "FROM studio_operator_frames WHERE request_id=?",
                (request_id,),
            )
        ).fetchone()
        if int(usage["frame_count"]) >= MAX_FRAMES_PER_TURN:
            return "frames_per_turn"
        if int(usage["payload_bytes"]) + payload_bytes > MAX_TURN_PAYLOAD_BYTES:
            return "turn_payload_bytes"
        return None

    async def _record_elided_frame(
        self,
        db: Any,
        *,
        conversation_id: str,
        request_id: str,
        frame_type: str,
        payload_bytes: int,
        sequence: int,
        reason: str,
        now: float,
    ) -> dict[str, Any] | None:
        """Fold one refused frame into the turn's single truncation summary frame."""
        existing = await (
            await db.execute(
                "SELECT sequence, payload_json FROM studio_operator_frames "
                "WHERE request_id=? AND frame_type=? LIMIT 1",
                (request_id, TRUNCATION_FRAME_TYPE),
            )
        ).fetchone()
        if existing is not None:
            summary = json.loads(existing["payload_json"])
            summary["elidedFrames"] = int(summary.get("elidedFrames", 0)) + 1
            summary["elidedBytes"] = int(summary.get("elidedBytes", 0)) + payload_bytes
            by_type = dict(summary.get("elidedFrameTypes") or {})
            by_type[frame_type] = int(by_type.get(frame_type, 0)) + 1
            summary["elidedFrameTypes"] = by_type
            summary["lastElidedAt"] = now
            await db.execute(
                "UPDATE studio_operator_frames SET payload_json=? "
                "WHERE request_id=? AND sequence=?",
                (self._json(summary), request_id, existing["sequence"]),
            )
            await db.commit()
            return None
        summary = {
            "reason": reason,
            "limits": {
                "maxFramesPerTurn": MAX_FRAMES_PER_TURN,
                "maxTurnPayloadBytes": MAX_TURN_PAYLOAD_BYTES,
                "maxFramePayloadBytes": MAX_FRAME_PAYLOAD_BYTES,
            },
            "elidedFrames": 1,
            "elidedBytes": payload_bytes,
            "elidedFrameTypes": {frame_type: 1},
            "firstElidedAt": now,
            "lastElidedAt": now,
            "message": (
                "This turn reached its durable retention limit; frames after this "
                "point were not stored and are counted here."
            ),
        }
        await db.execute(
            "UPDATE studio_operator_conversations SET next_sequence=?, updated_at=? WHERE id=?",
            (sequence + 1, now, conversation_id),
        )
        await db.execute(
            "INSERT INTO studio_operator_frames "
            "(conversation_id, sequence, request_id, frame_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                sequence,
                request_id,
                TRUNCATION_FRAME_TYPE,
                self._json(summary),
                now,
            ),
        )
        await db.commit()
        return {
            "version": 1,
            "conversationId": conversation_id,
            "requestId": request_id,
            "sequence": sequence,
            "type": TRUNCATION_FRAME_TYPE,
            "payload": summary,
            "createdAt": now,
        }

    async def append_frame(
        self,
        conversation_id: str,
        request_id: str,
        frame_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Insert before returning; returned frames are therefore safe to yield.

        Enforces the per-turn retention contract: an oversized payload is stored
        truncated, and once the turn's frame or byte budget is spent the frame is
        folded into a single durable ``truncation`` summary instead of a new row.
        """
        await self.ensure_schema()
        now = time.time()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            terminal = await (
                await db.execute(
                    "SELECT 1 FROM studio_operator_frames "
                    "WHERE request_id=? AND frame_type='done' LIMIT 1",
                    (request_id,),
                )
            ).fetchone()
            if terminal is not None:
                await db.rollback()
                return None
            row = await (
                await db.execute(
                    "SELECT next_sequence FROM studio_operator_conversations "
                    "WHERE id=? AND active_request_id=?",
                    (conversation_id, request_id),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                return None
            sequence = int(row["next_sequence"])
            stored_payload = self._cap_frame_payload(dict(payload))
            if frame_type == "done":
                stored_payload["lastSequence"] = sequence
            else:
                over_budget = await self._turn_budget_exceeded(
                    db,
                    request_id,
                    self._byte_len(self._json(stored_payload)),
                )
                if over_budget is not None:
                    return await self._record_elided_frame(
                        db,
                        conversation_id=conversation_id,
                        request_id=request_id,
                        frame_type=frame_type,
                        payload_bytes=self._byte_len(self._json(stored_payload)),
                        sequence=sequence,
                        reason=over_budget,
                        now=now,
                    )
            await db.execute(
                "UPDATE studio_operator_conversations SET next_sequence=?, updated_at=? WHERE id=?",
                (sequence + 1, now, conversation_id),
            )
            await db.execute(
                "INSERT INTO studio_operator_frames "
                "(conversation_id, sequence, request_id, frame_type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    sequence,
                    request_id,
                    frame_type,
                    self._json(stored_payload),
                    now,
                ),
            )
            await db.commit()
        return {
            "version": 1,
            "conversationId": conversation_id,
            "requestId": request_id,
            "sequence": sequence,
            "type": frame_type,
            "payload": stored_payload,
            "createdAt": now,
        }

    async def append_effect(
        self,
        conversation_id: str,
        request_id: str,
        effect: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Persist one pending UI effect and its frame in the same transaction."""
        await self.ensure_schema()
        kind = effect.get("kind")
        if kind not in {"navigate", "select", "prefill", "theme"}:
            raise OperatorConflictError("Operator emitted an unsupported UI effect")
        effect_id = str(uuid.uuid4())
        stored_effect = {**effect, "id": effect_id}
        payload = {"effect": stored_effect}
        now = time.time()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            terminal = await (
                await db.execute(
                    "SELECT 1 FROM studio_operator_frames "
                    "WHERE request_id=? AND frame_type='done' LIMIT 1",
                    (request_id,),
                )
            ).fetchone()
            row = await (
                await db.execute(
                    "SELECT next_sequence FROM studio_operator_conversations "
                    "WHERE id=? AND active_request_id=?",
                    (conversation_id, request_id),
                )
            ).fetchone()
            if terminal is not None or row is None:
                await db.rollback()
                return None
            sequence = int(row["next_sequence"])
            await db.execute(
                "INSERT INTO studio_operator_effects "
                "(id, conversation_id, request_id, effect_type, effect_json, "
                "status, emitted_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (
                    effect_id,
                    conversation_id,
                    request_id,
                    str(kind),
                    self._json(stored_effect),
                    now,
                ),
            )
            await db.execute(
                "UPDATE studio_operator_conversations SET next_sequence=?, updated_at=? WHERE id=?",
                (sequence + 1, now, conversation_id),
            )
            await db.execute(
                "INSERT INTO studio_operator_frames "
                "(conversation_id, sequence, request_id, frame_type, payload_json, created_at) "
                "VALUES (?, ?, ?, 'ui_command', ?, ?)",
                (conversation_id, sequence, request_id, self._json(payload), now),
            )
            await db.commit()
        return {
            "version": 1,
            "conversationId": conversation_id,
            "requestId": request_id,
            "sequence": sequence,
            "type": "ui_command",
            "payload": payload,
            "createdAt": now,
        }

    async def finish_turn(
        self,
        request_id: str,
        *,
        outcome: str,
        error: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Append optional error + exactly one done and clear the active CAS."""
        await self.ensure_schema()
        now = time.time()
        inserted: list[dict[str, Any]] = []
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            turn = await (
                await db.execute(
                    "SELECT conversation_id FROM studio_operator_turns WHERE request_id=?",
                    (request_id,),
                )
            ).fetchone()
            if turn is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator request '{request_id}' not found")
            conversation_id = turn["conversation_id"]
            existing = await (
                await db.execute(
                    "SELECT 1 FROM studio_operator_frames "
                    "WHERE request_id=? AND frame_type='done' LIMIT 1",
                    (request_id,),
                )
            ).fetchone()
            if existing is not None:
                await db.rollback()
                return []
            conv = await (
                await db.execute(
                    "SELECT next_sequence FROM studio_operator_conversations WHERE id=?",
                    (conversation_id,),
                )
            ).fetchone()
            sequence = int(conv["next_sequence"])
            unfinished_proposals = await (
                await db.execute(
                    "SELECT * FROM studio_operator_proposals "
                    "WHERE request_id=? AND status IN ('pending','confirmed')",
                    (request_id,),
                )
            ).fetchall()
            confirmed_without_result = [
                proposal
                for proposal in unfinished_proposals
                if proposal["status"] == "confirmed" and outcome != "cancelled"
            ]
            terminal_error = error
            if confirmed_without_result and outcome == "completed":
                outcome = "failed"
                terminal_error = {
                    "code": "service_failure",
                    "message": (
                        "The provider ended without returning a terminal result "
                        "for an approved tool"
                    ),
                    "retryable": False,
                }
            status = {
                "completed": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
            }[outcome]
            missing_result_error = {
                "code": "service_failure",
                "message": (
                    "The provider ended without returning a terminal result for this approved tool"
                ),
                "retryable": False,
            }
            for proposal in confirmed_without_result:
                command = json.loads(proposal["command_json"])
                call_id = command.get("toolUseId") or proposal["id"]
                payload = {
                    "callId": call_id,
                    "ok": False,
                    "error": missing_result_error,
                }
                await db.execute(
                    "INSERT INTO studio_operator_frames "
                    "(conversation_id, sequence, request_id, frame_type, "
                    "payload_json, created_at) "
                    "VALUES (?, ?, ?, 'tool_result', ?, ?)",
                    (
                        conversation_id,
                        sequence,
                        request_id,
                        self._json(payload),
                        now,
                    ),
                )
                inserted.append(
                    {
                        "version": 1,
                        "conversationId": conversation_id,
                        "requestId": request_id,
                        "sequence": sequence,
                        "type": "tool_result",
                        "payload": payload,
                        "createdAt": now,
                    }
                )
                sequence += 1
            if terminal_error is not None:
                await db.execute(
                    "INSERT INTO studio_operator_frames "
                    "(conversation_id, sequence, request_id, frame_type, payload_json, created_at) "
                    "VALUES (?, ?, ?, 'error', ?, ?)",
                    (
                        conversation_id,
                        sequence,
                        request_id,
                        self._json({"error": terminal_error}),
                        now,
                    ),
                )
                inserted.append(
                    {
                        "version": 1,
                        "conversationId": conversation_id,
                        "requestId": request_id,
                        "sequence": sequence,
                        "type": "error",
                        "payload": {"error": terminal_error},
                        "createdAt": now,
                    }
                )
                sequence += 1
            done_payload = {"outcome": outcome, "lastSequence": sequence}
            await db.execute(
                "INSERT INTO studio_operator_frames "
                "(conversation_id, sequence, request_id, frame_type, payload_json, created_at) "
                "VALUES (?, ?, ?, 'done', ?, ?)",
                (conversation_id, sequence, request_id, self._json(done_payload), now),
            )
            inserted.append(
                {
                    "version": 1,
                    "conversationId": conversation_id,
                    "requestId": request_id,
                    "sequence": sequence,
                    "type": "done",
                    "payload": done_payload,
                    "createdAt": now,
                }
            )
            await db.execute(
                "UPDATE studio_operator_turns SET status=?, error_code=?, ended_at=? "
                "WHERE request_id=?",
                (
                    status,
                    terminal_error["code"] if terminal_error else None,
                    now,
                    request_id,
                ),
            )
            for proposal in unfinished_proposals:
                was_pending = proposal["status"] == "pending"
                proposal_status = "cancelled" if was_pending else "failed"
                proposal_error = (
                    "cancelled"
                    if was_pending or outcome == "cancelled"
                    else "provider_result_missing"
                )
                decision = "denied" if was_pending else "indeterminate"
                details = self._proposal_audit_details(
                    proposal,
                    decision=decision,
                    error_code=proposal_error,
                    confirmed_at=proposal["confirmed_at"],
                    completed_at=now,
                )
                await db.execute(
                    "INSERT INTO admin_events "
                    "(id, created_at, action, target_id, details, actor) "
                    "VALUES (?, ?, 'studio.operator.command', ?, ?, 'studio_operator')",
                    (
                        uuid.uuid4().hex[:12],
                        now,
                        proposal["id"],
                        self._json(details),
                    ),
                )
                await db.execute(
                    "UPDATE studio_operator_proposals SET status=?, completed_at=?, "
                    "error_code=? WHERE id=? AND status=?",
                    (
                        proposal_status,
                        now,
                        proposal_error,
                        proposal["id"],
                        proposal["status"],
                    ),
                )
            await db.execute(
                "UPDATE studio_operator_conversations SET active_request_id=NULL, "
                "next_sequence=?, updated_at=? "
                "WHERE id=? AND active_request_id=?",
                (sequence + 1, now, conversation_id, request_id),
            )
            await db.commit()
        return inserted

    async def request_cancel(self, conversation_id: str, request_id: str) -> dict[str, Any]:
        await self.ensure_schema()
        now = time.time()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT status FROM studio_operator_turns "
                    "WHERE request_id=? AND conversation_id=?",
                    (request_id, conversation_id),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator request '{request_id}' not found")
            terminal = row["status"] in {"completed", "failed", "cancelled"}
            if not terminal:
                await db.execute(
                    "UPDATE studio_operator_turns SET cancel_requested_at=COALESCE("
                    "cancel_requested_at, ?) WHERE request_id=?",
                    (now, request_id),
                )
            await db.commit()
        return {
            "conversationId": conversation_id,
            "requestId": request_id,
            "status": row["status"],
            "cancelRequested": not terminal,
        }

    async def create_proposal(
        self,
        conversation_id: str,
        request_id: str,
        *,
        command_type: str,
        command: dict[str, Any],
        risk: str,
        summary: str,
        idempotency_key: str | None = None,
        target_version: str | None = None,
    ) -> dict[str, Any]:
        await self.ensure_schema()
        proposal_id = str(uuid.uuid4())
        idempotency_key = idempotency_key or str(uuid.uuid4())
        command_hash = self.canonical_hash(command)
        now = time.time()
        expires_at = now + 600
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT * FROM studio_operator_proposals WHERE idempotency_key=?",
                    (idempotency_key,),
                )
            ).fetchone()
            if existing is not None:
                await db.rollback()
                same_proposal = (
                    existing["conversation_id"] == conversation_id
                    and existing["request_id"] == request_id
                    and existing["command_type"] == command_type
                    and existing["command_json"] == self._json(command)
                    and existing["command_hash"] == command_hash
                    and existing["target_version"] == target_version
                    and existing["risk"] == risk
                )
                if not same_proposal:
                    raise OperatorConflictError(
                        "Idempotency key was reused for a different Operator proposal",
                        details={
                            "idempotencyKey": idempotency_key,
                            "existingProposalId": existing["id"],
                        },
                    )
                return self._proposal(existing)
            turn = await (
                await db.execute(
                    "SELECT status FROM studio_operator_turns "
                    "WHERE request_id=? AND conversation_id=?",
                    (request_id, conversation_id),
                )
            ).fetchone()
            if turn is None or turn["status"] not in {"running", "awaiting_confirmation"}:
                await db.rollback()
                raise OperatorConflictError("Permission request is not attached to a running turn")
            await db.execute(
                "INSERT INTO studio_operator_proposals "
                "(id, conversation_id, request_id, command_type, command_json, command_hash, "
                "target_version, risk, summary, idempotency_key, status, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    proposal_id,
                    conversation_id,
                    request_id,
                    command_type,
                    self._json(command),
                    command_hash,
                    target_version,
                    risk,
                    summary,
                    idempotency_key,
                    expires_at,
                    now,
                ),
            )
            await db.execute(
                "UPDATE studio_operator_turns SET status='awaiting_confirmation' "
                "WHERE request_id=? AND status='running'",
                (request_id,),
            )
            await db.commit()
        proposal = await self.get_proposal(proposal_id)
        await self.append_frame(
            conversation_id,
            request_id,
            "proposal",
            {
                "proposal": {
                    "id": proposal_id,
                    # The frame is the only view of a proposal a stream client
                    # ever sees, so it has to carry what that client needs to
                    # check the proposal against the request it made. Without
                    # the type, a caller can confirm a proposal by id and hash
                    # while having no way to tell what it would actually do.
                    # Same key and value as the full row serialization.
                    "commandType": command_type,
                    "command": command,
                    "commandHash": command_hash,
                    "risk": risk,
                    "summary": summary,
                    "target": self._proposal_target(
                        command_type,
                        command,
                        target_version,
                    ),
                    "idempotencyKey": idempotency_key,
                    "expiresAt": expires_at,
                }
            },
        )
        await self.append_frame(
            conversation_id,
            request_id,
            "confirmation",
            {"proposalId": proposal_id, "state": "required"},
        )
        return proposal

    async def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            row = await (
                await db.execute(
                    "SELECT * FROM studio_operator_proposals WHERE id=?", (proposal_id,)
                )
            ).fetchone()
        if row is None:
            raise OperatorNotFoundError(f"Operator proposal '{proposal_id}' not found")
        return self._proposal(row)

    async def find_proposal_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            row = await (
                await db.execute(
                    "SELECT * FROM studio_operator_proposals WHERE idempotency_key=?",
                    (idempotency_key,),
                )
            ).fetchone()
        return self._proposal(row) if row is not None else None

    async def list_proposals_for_request(self, request_id: str) -> list[dict[str, Any]]:
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM studio_operator_proposals WHERE request_id=? "
                    "ORDER BY created_at ASC",
                    (request_id,),
                )
            ).fetchall()
        return [self._proposal(row) for row in rows]

    async def decide_proposal(
        self,
        conversation_id: str,
        proposal_id: str,
        *,
        allow: bool,
        expected_command_hash: str | None,
        expected_target_version: str | None,
        audit: bool = False,
        claim_execution: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_schema()
        now = time.time()
        transitioned = False
        claimed_execution = False
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT * FROM studio_operator_proposals WHERE id=? AND conversation_id=?",
                    (proposal_id, conversation_id),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator proposal '{proposal_id}' not found")
            conversation = await (
                await db.execute(
                    "SELECT status FROM studio_operator_conversations WHERE id=?",
                    (conversation_id,),
                )
            ).fetchone()
            if conversation is None or conversation["status"] != "active":
                await db.rollback()
                raise OperatorConflictError("Operator conversation is no longer active")
            if allow and expected_command_hash is None:
                await db.rollback()
                raise OperatorConflictError("Proposal command hash is required before execution")
            if expected_command_hash and expected_command_hash != row["command_hash"]:
                await db.rollback()
                raise OperatorConflictError("Proposal command hash does not match")
            if expected_target_version != row["target_version"]:
                await db.rollback()
                raise OperatorConflictError("Proposal target version changed")
            if row["expires_at"] <= now and row["status"] == "pending":
                if audit:
                    details = self._proposal_audit_details(
                        row,
                        decision="expired",
                        error_code="stale_context",
                        confirmed_at=None,
                        completed_at=now,
                    )
                    await db.execute(
                        "INSERT INTO admin_events "
                        "(id, created_at, action, target_id, details, actor) "
                        "VALUES (?, ?, 'studio.operator.command', ?, ?, 'studio_operator')",
                        (
                            uuid.uuid4().hex[:12],
                            now,
                            proposal_id,
                            self._json(details),
                        ),
                    )
                await db.execute(
                    "UPDATE studio_operator_proposals SET status='expired', "
                    "completed_at=?, error_code='stale_context' WHERE id=?",
                    (now, proposal_id),
                )
                await db.execute(
                    "UPDATE studio_operator_turns SET status='running' "
                    "WHERE request_id=? AND status='awaiting_confirmation'",
                    (row["request_id"],),
                )
                await db.commit()
                transitioned = True
                expired = True
            else:
                expired = False
            if expired:
                pass
            elif row["status"] != "pending":
                await db.rollback()
                existing = self._proposal(row)
                existing["_claimedExecution"] = False
                return existing
            else:
                status = (
                    (
                        "executing"
                        if claim_execution and row["command_type"] != "provider_permission"
                        else "confirmed"
                    )
                    if allow
                    else "cancelled"
                )
                if audit:
                    audit_details = self._proposal_audit_details(
                        row,
                        decision="confirmed" if allow else "denied",
                        error_code=None if allow else "denied",
                        confirmed_at=now,
                        completed_at=now if not allow else None,
                    )
                    await db.execute(
                        "INSERT INTO admin_events "
                        "(id, created_at, action, target_id, details, actor) "
                        "VALUES (?, ?, 'studio.operator.command', ?, ?, 'studio_operator')",
                        (
                            uuid.uuid4().hex[:12],
                            now,
                            proposal_id,
                            self._json(audit_details),
                        ),
                    )
                await db.execute(
                    "UPDATE studio_operator_proposals SET status=?, confirmed_at=?, "
                    "completed_at=CASE WHEN ?='cancelled' THEN ? ELSE completed_at END, "
                    "error_code=CASE WHEN ?='cancelled' THEN 'denied' ELSE error_code END "
                    "WHERE id=? AND status='pending'",
                    (status, now, status, now, status, proposal_id),
                )
                await db.execute(
                    "UPDATE studio_operator_turns SET status='running' "
                    "WHERE request_id=? AND status='awaiting_confirmation'",
                    (row["request_id"],),
                )
                await db.commit()
                transitioned = True
                claimed_execution = status == "executing"
        if expired:
            await self.append_frame(
                conversation_id,
                row["request_id"],
                "confirmation",
                {"proposalId": proposal_id, "state": "expired"},
            )
        elif transitioned and allow:
            await self.append_frame(
                conversation_id,
                row["request_id"],
                "confirmation",
                {"proposalId": proposal_id, "state": "confirmed"},
            )
        elif transitioned:
            await self.append_frame(
                conversation_id,
                row["request_id"],
                "confirmation",
                {"proposalId": proposal_id, "state": "cancelled"},
            )
            await self.append_frame(
                conversation_id,
                row["request_id"],
                "error",
                {
                    "error": {
                        "code": "denied",
                        "message": "The operator denied this permission request",
                        "retryable": False,
                    }
                },
            )
        proposal = await self.get_proposal(proposal_id)
        proposal["_claimedExecution"] = claimed_execution
        return proposal

    async def complete_proposal(
        self,
        proposal_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        audit: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_schema()
        now = time.time()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT * FROM studio_operator_proposals WHERE id=?",
                    (proposal_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator proposal '{proposal_id}' not found")
            cur = await db.execute(
                "UPDATE studio_operator_proposals SET status=?, result_json=?, "
                "error_code=?, completed_at=? WHERE id=? "
                "AND status IN ('confirmed','executing')",
                (
                    status,
                    self._json(result) if result is not None else None,
                    error_code,
                    now,
                    proposal_id,
                ),
            )
            if cur.rowcount and audit:
                details = self._proposal_audit_details(
                    row,
                    decision="executed" if status == "succeeded" else "failed",
                    result=result,
                    error_code=error_code,
                    confirmed_at=row["confirmed_at"],
                    completed_at=now,
                )
                await db.execute(
                    "INSERT INTO admin_events "
                    "(id, created_at, action, target_id, details, actor) "
                    "VALUES (?, ?, 'studio.operator.command', ?, ?, 'studio_operator')",
                    (
                        uuid.uuid4().hex[:12],
                        now,
                        proposal_id,
                        self._json(details),
                    ),
                )
            await db.commit()
        return await self.get_proposal(proposal_id)

    async def complete_provider_permission(
        self,
        request_id: str,
        tool_use_id: str,
        *,
        ok: bool,
    ) -> dict[str, Any] | None:
        """Terminalize the confirmed provider proposal for one native tool result."""
        if not tool_use_id:
            return None
        await self.ensure_schema()
        now = time.time()
        matched_id: str | None = None
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await (
                await db.execute(
                    "SELECT * FROM studio_operator_proposals "
                    "WHERE request_id=? AND command_type='provider_permission' "
                    "AND status='confirmed'",
                    (request_id,),
                )
            ).fetchall()
            row = next(
                (
                    candidate
                    for candidate in rows
                    if json.loads(candidate["command_json"]).get("toolUseId") == tool_use_id
                ),
                None,
            )
            if row is None:
                await db.rollback()
                return None
            matched_id = row["id"]
            status = "succeeded" if ok else "failed"
            result = {"nativeToolCompleted": ok}
            error_code = None if ok else "provider_tool_failed"
            cur = await db.execute(
                "UPDATE studio_operator_proposals SET status=?, result_json=?, "
                "error_code=?, completed_at=? WHERE id=? AND status='confirmed'",
                (
                    status,
                    self._json(result),
                    error_code,
                    now,
                    matched_id,
                ),
            )
            if not cur.rowcount:
                await db.rollback()
                return None
            details = self._proposal_audit_details(
                row,
                decision="executed" if ok else "failed",
                result=result,
                error_code=error_code,
                confirmed_at=row["confirmed_at"],
                completed_at=now,
            )
            await db.execute(
                "INSERT INTO admin_events "
                "(id, created_at, action, target_id, details, actor) "
                "VALUES (?, ?, 'studio.operator.command', ?, ?, 'studio_operator')",
                (
                    uuid.uuid4().hex[:12],
                    now,
                    matched_id,
                    self._json(details),
                ),
            )
            await db.commit()
        return await self.get_proposal(matched_id)

    async def expire_proposal(self, proposal_id: str) -> dict[str, Any]:
        """Expire one still-pending proposal and release its waiting turn."""
        await self.ensure_schema()
        now = time.time()
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT * FROM studio_operator_proposals WHERE id=?",
                    (proposal_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator proposal '{proposal_id}' not found")
            cur = await db.execute(
                "UPDATE studio_operator_proposals SET status='expired', "
                "completed_at=?, error_code='stale_context' "
                "WHERE id=? AND status='pending' AND expires_at<=?",
                (now, proposal_id, now),
            )
            if cur.rowcount:
                details = self._proposal_audit_details(
                    row,
                    decision="expired",
                    error_code="stale_context",
                    confirmed_at=None,
                    completed_at=now,
                )
                await db.execute(
                    "INSERT INTO admin_events "
                    "(id, created_at, action, target_id, details, actor) "
                    "VALUES (?, ?, 'studio.operator.command', ?, ?, 'studio_operator')",
                    (
                        uuid.uuid4().hex[:12],
                        now,
                        proposal_id,
                        self._json(details),
                    ),
                )
                await db.execute(
                    "UPDATE studio_operator_turns SET status='running' "
                    "WHERE request_id=? AND status='awaiting_confirmation'",
                    (row["request_id"],),
                )
            await db.commit()
        if cur.rowcount:
            await self.append_frame(
                row["conversation_id"],
                row["request_id"],
                "confirmation",
                {"proposalId": proposal_id, "state": "expired"},
            )
        return await self.get_proposal(proposal_id)

    async def recover_interrupted_turns(self) -> list[str]:
        await self.ensure_schema()
        async with open_db(str(self.path())) as db:
            rows = await (
                await db.execute(
                    "SELECT request_id FROM studio_operator_turns "
                    "WHERE status IN ('queued','running','awaiting_confirmation')"
                )
            ).fetchall()
        recovered: list[str] = []
        for row in rows:
            request_id = row["request_id"]
            await self.finish_turn(
                request_id,
                outcome="failed",
                error={
                    "code": "service_restarted",
                    "message": "The Operator daemon restarted during this turn",
                    "retryable": True,
                },
            )
            recovered.append(request_id)
        return recovered

    async def acknowledge_effect(
        self,
        conversation_id: str,
        effect_id: str,
        *,
        status: str,
        rejection_code: str | None,
    ) -> dict[str, Any]:
        await self.ensure_schema()
        now = time.time()
        desired = "applied" if status == "applied" else "rejected"
        if desired == "applied" and rejection_code is not None:
            raise OperatorConflictError("Applied effects cannot carry a rejection code")
        if desired == "rejected" and rejection_code is None:
            raise OperatorConflictError("Rejected effects require a rejection code")
        async with open_db(str(self.path())) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT * FROM studio_operator_effects WHERE id=? AND conversation_id=?",
                    (effect_id, conversation_id),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise OperatorNotFoundError(f"Operator effect '{effect_id}' not found")
            if row["status"] != "pending":
                same_rejection = desired != "rejected" or row["rejection_code"] == rejection_code
                if row["status"] != desired or not same_rejection:
                    await db.rollback()
                    raise OperatorConflictError("Effect was already acknowledged differently")
            if row["status"] == "pending":
                await db.execute(
                    "UPDATE studio_operator_effects SET status=?, acknowledged_at=?, "
                    "rejection_code=? WHERE id=?",
                    (desired, now, rejection_code, effect_id),
                )
            await db.commit()
        return {"effectId": effect_id, "status": desired}
