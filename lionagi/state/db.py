# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from lionagi.cli._runs import LIONAGI_HOME

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB_PATH = LIONAGI_HOME / "state.db"


class StateDB:
    """Async SQLite state layer for lionagi's core data model.

    Mirrors the runtime Session / Branch / Message / Progression objects.
    Uses WAL mode for concurrent read + single writer.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self._db: aiosqlite.Connection | None = None

    # ── Connection lifecycle ───────────────────────────────────────────

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.path))
        self._db.row_factory = aiosqlite.Row
        await self._apply_pragmas()
        await self._apply_schema()
        await self._migrate()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> StateDB:
        await self.open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("StateDB not open — call open() or use async with")
        return self._db

    async def _apply_pragmas(self) -> None:
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.execute("PRAGMA synchronous = NORMAL")
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.execute("PRAGMA busy_timeout = 5000")
        await self.db.execute("PRAGMA cache_size = -64000")

    async def _apply_schema(self) -> None:
        schema = _SCHEMA_PATH.read_text()
        # Strip PRAGMA lines (already applied above) — executescript
        # doesn't mix well with PRAGMA inside a transaction.
        lines = [
            ln for ln in schema.splitlines()
            if not ln.strip().upper().startswith("PRAGMA")
        ]
        await self.db.executescript("\n".join(lines))

    async def _migrate(self) -> None:
        """Run forward migrations based on schema version."""
        cur = await self.db.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        )
        row = await cur.fetchone()
        version = int(row["value"]) if row else 1

        if version < 2:
            # ADR-0012: session provenance columns
            for col, coldef in [
                ("playbook_name", "TEXT"),
                ("agent_name", "TEXT"),
                ("invocation_kind", "TEXT"),
                ("show_topic", "TEXT"),
                ("show_play_name", "TEXT"),
                ("artifacts_path", "TEXT"),
                ("source_kind", "TEXT DEFAULT 'live'"),
            ]:
                try:
                    await self.db.execute(
                        f"ALTER TABLE sessions ADD COLUMN {col} {coldef}"
                    )
                except Exception:
                    pass  # column already exists
            await self.db.execute(
                "UPDATE schema_meta SET value = '2' WHERE key = 'version'"
            )
            await self.db.commit()
            version = 2

        if version < 3:
            # Rename worker_name → agent_name
            try:
                await self.db.execute(
                    "ALTER TABLE sessions RENAME COLUMN worker_name TO agent_name"
                )
            except Exception:
                # Column may already be agent_name (fresh db) or rename unsupported
                try:
                    await self.db.execute(
                        "ALTER TABLE sessions ADD COLUMN agent_name TEXT"
                    )
                except Exception:
                    pass
            await self.db.execute(
                "UPDATE schema_meta SET value = '3' WHERE key = 'version'"
            )
            await self.db.commit()

    # ── Schema version ─────────────────────────────────────────────────

    async def schema_version(self) -> str | None:
        cur = await self.db.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        )
        row = await cur.fetchone()
        return row["value"] if row else None

    # ── Messages ───────────────────────────────────────────────────────

    async def insert_message(self, msg: dict[str, Any]) -> None:
        lion_class_str = (msg.get("node_metadata") or {}).get("lion_class", "")
        if isinstance(msg.get("node_metadata"), dict):
            node_metadata = json.dumps(msg["node_metadata"])
        else:
            node_metadata = msg.get("node_metadata")

        type_id = await self._resolve_lion_class(lion_class_str)

        await self.db.execute(
            """INSERT OR IGNORE INTO messages (id, created_at, node_metadata, content,
               embedding, sender, recipient, channel, role, lion_class)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg["id"],
                msg["created_at"],
                node_metadata,
                json.dumps(msg["content"]) if isinstance(msg["content"], dict) else msg["content"],
                msg.get("embedding"),
                msg.get("sender"),
                msg.get("recipient"),
                msg.get("channel"),
                msg["role"],
                type_id,
            ),
        )
        await self.db.commit()

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            """SELECT m.*, mt.lion_class AS lion_class_str
               FROM messages m
               LEFT JOIN message_types mt ON m.lion_class = mt.type_id
               WHERE m.id = ?""",
            (message_id,),
        )
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def _resolve_lion_class(self, lion_class_str: str) -> int:
        cur = await self.db.execute(
            "SELECT type_id FROM message_types WHERE lion_class = ?",
            (lion_class_str,),
        )
        row = await cur.fetchone()
        if row:
            return row["type_id"]
        # Auto-register new types
        cur = await self.db.execute(
            "INSERT INTO message_types (lion_class) VALUES (?) RETURNING type_id",
            (lion_class_str,),
        )
        row = await cur.fetchone()
        await self.db.commit()
        return row["type_id"]

    # ── Progressions ───────────────────────────────────────────────────

    async def create_progression(self, progression_id: str, collection: list[str] | None = None) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO progressions (id, created_at, collection) VALUES (?, ?, ?)",
            (progression_id, time.time(), json.dumps(collection or [])),
        )
        await self.db.commit()

    async def get_progression(self, progression_id: str) -> list[str]:
        cur = await self.db.execute(
            "SELECT collection FROM progressions WHERE id = ?",
            (progression_id,),
        )
        row = await cur.fetchone()
        if not row:
            return []
        return json.loads(row["collection"])

    async def append_to_progression(self, progression_id: str, message_id: str) -> None:
        collection = await self.get_progression(progression_id)
        collection.append(message_id)
        await self.db.execute(
            "UPDATE progressions SET collection = ? WHERE id = ?",
            (json.dumps(collection), progression_id),
        )
        await self.db.commit()

    # ── Sessions ───────────────────────────────────────────────────────

    async def create_session(self, session: dict[str, Any]) -> None:
        now = time.time()
        await self.db.execute(
            """INSERT OR IGNORE INTO sessions (id, created_at, node_metadata, name, user,
               progression_id, first_msg_id, last_msg_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session["id"],
                session.get("created_at", now),
                json.dumps(session.get("node_metadata")) if isinstance(session.get("node_metadata"), dict) else session.get("node_metadata"),
                session.get("name"),
                session.get("user"),
                session["progression_id"],
                session.get("first_msg_id"),
                session.get("last_msg_id"),
                now,
            ),
        )
        await self.db.commit()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def update_session(self, session_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [session_id]
        await self.db.execute(
            f"UPDATE sessions SET {sets} WHERE id = ?", vals
        )
        await self.db.commit()

    # ── Branches ───────────────────────────────────────────────────────

    async def create_branch(self, branch: dict[str, Any]) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO branches (id, created_at, node_metadata, user, name,
               session_id, progression_id, system_msg_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                branch["id"],
                branch.get("created_at", time.time()),
                json.dumps(branch.get("node_metadata")) if isinstance(branch.get("node_metadata"), dict) else branch.get("node_metadata"),
                branch.get("user"),
                branch.get("name"),
                branch["session_id"],
                branch["progression_id"],
                branch.get("system_msg_id"),
            ),
        )
        await self.db.commit()

    async def get_branch(self, branch_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM branches WHERE id = ?", (branch_id,)
        )
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_branches(self, session_id: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM branches WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_branch_messages(self, branch_id: str) -> list[dict[str, Any]]:
        """Get all messages in a branch's progression, in order."""
        branch = await self.get_branch(branch_id)
        if not branch:
            return []
        message_ids = await self.get_progression(branch["progression_id"])
        if not message_ids:
            return []
        placeholders = ",".join("?" for _ in message_ids)
        cur = await self.db.execute(
            f"""SELECT m.*, mt.lion_class AS lion_class_str
                FROM messages m
                LEFT JOIN message_types mt ON m.lion_class = mt.type_id
                WHERE m.id IN ({placeholders})""",
            message_ids,
        )
        rows = await cur.fetchall()
        by_id = {r["id"]: self._row_to_dict(r) for r in rows}
        return [by_id[mid] for mid in message_ids if mid in by_id]

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        for key in ("node_metadata", "content"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d
