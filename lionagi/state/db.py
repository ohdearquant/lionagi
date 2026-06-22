# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from lionagi._paths import LIONAGI_HOME
from lionagi.ln import json_dumps as _json_dumps
from lionagi.ln.concurrency import Lock
from lionagi.state.reasons import (
    PlayReasons as _PlayReasons,
)
from lionagi.state.reasons import (
    RunReasons as _RunReasons,
)
from lionagi.state.reasons import (
    ShowReasons as _ShowReasons,
)
from lionagi.state.reasons import (
    entity_table as _reason_entity_table,
)
from lionagi.state.reasons import (
    validate_entity_type as _validate_entity_type_for_reason,
)
from lionagi.state.reasons import (
    validate_reason_code as _validate_reason_code,
)
from lionagi.state.schema_migrations import MIGRATION_COLUMNS as _MIGRATION_COLUMNS

_RUN_DEFAULTS: dict[str, str] = {
    "running": _RunReasons.STARTED_OK,
    "completed": _RunReasons.COMPLETED_OK,
    "failed": _RunReasons.FAILED_EXCEPTION,
    "timed_out": _RunReasons.TIMED_OUT_DEADLINE,
    "aborted": _RunReasons.ABORTED_USER,
    "cancelled": _RunReasons.CANCELLED_SYSTEM,
}

_SHOW_DEFAULTS: dict[str, str] = {
    "completed": _ShowReasons.COMPLETED_FINAL_GATE,
    "aborted": _ShowReasons.ABORTED_OPERATOR,
}

_PLAY_DEFAULTS: dict[str, str] = {
    "merged": _PlayReasons.MERGED_OK,
    "escalated": _PlayReasons.ESCALATED_GATE_TWICE,
    "gate_failed": _PlayReasons.GATE_FAILED_VERDICT,
}


def _default_reason_code_for_entity_status(entity_type: str, status: str) -> str | None:
    """Map (entity_type, status) to canonical reason_code, or None."""
    if entity_type in ("session", "invocation", "schedule_run"):
        return _RUN_DEFAULTS.get(status)
    if entity_type == "show":
        return _SHOW_DEFAULTS.get(status)
    if entity_type == "play":
        return _PLAY_DEFAULTS.get(status)
    return None


def _default_reason_code_for_status(status: str) -> str:
    """Legacy run-only resolver; prefer _default_reason_code_for_entity_status."""
    return _RUN_DEFAULTS.get(status, _RunReasons.FAILED_EXCEPTION)


_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB_PATH = LIONAGI_HOME / "state.db"

_VALID_STATUS_SOURCES: frozenset[str] = frozenset({"executor", "agent", "admin", "system"})

_SESSION_COLUMNS = frozenset(
    {
        "name",
        "user",
        "node_metadata",
        "first_msg_id",
        "last_msg_id",
        "updated_at",
        "playbook_name",
        "agent_name",
        "invocation_kind",
        "show_topic",
        "show_play_name",
        "artifacts_path",
        "source_kind",
        "status",
        "started_at",
        "ended_at",
        "last_message_at",
        "current_phase",
        "invocation_id",
        "model",
        "provider",
        "effort",
        "agent_hash",
        "project",
        "project_source",
        "input_tokens",
        "output_tokens",
        "total_cost_usd",
        "num_turns",
        "duration_ms",
    }
)

_INVOCATION_STATUSES = frozenset(
    {"running", "completed", "failed", "timed_out", "aborted", "cancelled"}
)
_INVOCATION_COLUMNS = frozenset(
    {
        "skill",
        "plugin",
        "prompt",
        "started_at",
        "ended_at",
        "status",
        "session_count",
        "updated_at",
        "node_metadata",
    }
)

_SHOW_COLUMNS = frozenset(
    {
        "topic",
        "goal",
        "repo",
        "base_branch",
        "integration_branch",
        "status",
        "show_dir",
        "status_source",
        "updated_at",
    }
)

_PLAY_COLUMNS = frozenset(
    {
        "name",
        "playbook",
        "effort",
        "status",
        "attempt",
        "session_id",
        "started_at",
        "ended_at",
        "exit_code",
        "worktree",
        "branch",
        "merge_sha",
        "merged_at",
        "gate_passed",
        "gate_feedback",
        "depends_on",
        "sort_order",
        "updated_at",
    }
)

_BRANCH_COLUMNS = frozenset(
    {
        "name",
        "user",
        "node_metadata",
        "system_msg_id",
        "model",
        "provider",
        "agent_name",
        "status",
        "started_at",
        "ended_at",
    }
)

VALID_SESSION_STATUSES = frozenset(
    {"running", "completed", "failed", "timed_out", "aborted", "cancelled"}
)
SESSION_TERMINAL_STATUSES = frozenset({"completed", "failed", "timed_out", "aborted", "cancelled"})
# Admin cannot mark completed/timed_out — those are system-determined.
ADMIN_TRANSITION_TARGETS = frozenset({"failed", "aborted", "cancelled"})

_SESSION_STATUSES = VALID_SESSION_STATUSES


def can_transition(current: str | None, target: str) -> bool:
    """Return True iff a session may move from *current* to *target*."""
    if current != "running":
        return False
    return target in SESSION_TERMINAL_STATUSES


_INVOCATION_KINDS = frozenset({"agent", "play", "flow", "fanout", "show-play"})
_SOURCE_KINDS = frozenset({"live", "imported_fs"})

_SHOW_STATUSES = frozenset({"active", "completed", "aborted", "imported"})
_PLAY_STATUSES = frozenset(
    {
        "pending",
        "prepared",
        "running",
        "running_complete",
        "gated",
        "gate_failed",
        "redoing",
        "merged",
        "escalated",
        "blocked",
        "aborted_after_finish",
    }
)

_DEFINITION_KINDS = frozenset({"agent", "playbook"})


def _validate_columns(fields: dict[str, Any], allowed: frozenset[str]) -> None:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Invalid column(s): {bad}")


def _to_json_column(value: Any) -> Any:
    """Serialize value to JSON string for round-trippable storage."""
    if value is None or isinstance(value, bytes | bytearray | memoryview):
        return value
    return _json_dumps(value)


def _validate_session_status(status: Any) -> None:
    if status is None:
        return
    if status not in VALID_SESSION_STATUSES:
        raise ValueError(
            f"Invalid session status {status!r}; "
            f"ADR-0025 vocabulary is {sorted(VALID_SESSION_STATUSES)}"
        )


def _validate_enum(
    name: str,
    value: Any,
    allowed: frozenset[str],
    *,
    adr: str,
    nullable: bool = True,
) -> None:
    if value is None:
        if nullable:
            return
        raise ValueError(f"{name} is required")
    if value not in allowed:
        raise ValueError(f"Invalid {name} {value!r}; {adr} vocabulary is {sorted(allowed)}")


class StateDB:
    """Async SQLite state layer for sessions, branches, messages, and progressions."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self._db: aiosqlite.Connection | None = None
        # Per-(kind, name) lock to serialize version increment for save_definition.
        self._definition_locks: dict[tuple[str, str], Lock] = {}
        # Connection-wide write lock: every mutating method that can share the
        # live-persistence connection must hold this lock during its
        # execute + commit (or BEGIN IMMEDIATE … commit/rollback) window.
        #
        # aiosqlite routes commands through a single background thread, so two
        # coroutines issuing "BEGIN IMMEDIATE" concurrently on the same
        # connection see "cannot start a transaction within a transaction".
        # Even implicit-write paths (execute + commit) interleave unsafely when
        # an explicit BEGIN is active: another coroutine's commit() can
        # prematurely close the outer transaction.
        #
        # Methods covered: insert_session_signal, update_status,
        # insert_message, append_to_progression, touch_session_activity,
        # update_session (field-update section), update_branch,
        # update_artifact_verification.
        # Methods NOT covered: read-only queries; save_definition (uses its
        # own per-(kind,name) _definition_locks and is not on the signal path).
        self._write_lock: Lock = Lock()

    # ── Connection lifecycle ───────────────────────────────────────────

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.path))
        self._db.row_factory = aiosqlite.Row
        await self._apply_pragmas()
        await self._apply_schema()

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
        # busy_timeout MUST be first: the journal_mode=WAL switch below takes a
        # momentary exclusive lock, so when a second connection initialises the
        # same file concurrently (the mirror is "just another writer") it must
        # wait out the timeout rather than fail instantly with "database is locked".
        await self.db.execute("PRAGMA busy_timeout = 5000")
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.execute("PRAGMA synchronous = NORMAL")
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.execute("PRAGMA cache_size = -64000")
        await self.db.execute("PRAGMA wal_autocheckpoint = 1000")

    async def _apply_schema(self) -> None:
        await self._reconcile_columns()
        await self._drop_legacy_session_status_check()
        # #1174: existing DBs created before flow_yaml was added carry a
        # 4-value CHECK on schedules.action_kind that omits 'flow_yaml'.
        await self._drop_legacy_action_kind_check()
        schema = _SCHEMA_PATH.read_text()
        lines = [ln for ln in schema.splitlines() if not ln.strip().upper().startswith("PRAGMA")]
        await self.db.executescript("\n".join(lines))

    _MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = _MIGRATION_COLUMNS

    async def _reconcile_columns(self) -> None:
        for table, columns in self._MIGRATION_COLUMNS.items():
            cur = await self.db.execute(f"PRAGMA table_info({table})")
            rows = await cur.fetchall()
            if not rows:
                continue
            existing = {row["name"] for row in rows}
            for name, defn in columns:
                if name not in existing:
                    await self.db.execute(  # noqa: S608
                        f"ALTER TABLE {table} ADD COLUMN {name} {defn}"
                    )
        await self.db.commit()

    _LEGACY_SESSION_STATUS_CHECK_MARKER = "'running', 'completed', 'failed', 'aborted'"

    async def _drop_legacy_session_status_check(self) -> None:
        """Rebuild sessions table if it carries the legacy 4-value CHECK constraint."""
        cur = await self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
        row = await cur.fetchone()
        if row is None or row["sql"] is None:
            return
        create_sql: str = row["sql"]
        if self._LEGACY_SESSION_STATUS_CHECK_MARKER not in create_sql:
            return

        idx_cur = await self.db.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='sessions' AND sql IS NOT NULL"
        )
        index_sqls = [r["sql"] for r in await idx_cur.fetchall()]

        info_cur = await self.db.execute("PRAGMA table_info(sessions)")
        cols = [r["name"] for r in await info_cur.fetchall()]
        col_list = ", ".join(cols)

        await self.db.execute("PRAGMA foreign_keys = OFF")
        try:
            await self.db.execute(
                """
                CREATE TABLE sessions_new (
                  id              TEXT    PRIMARY KEY,
                  created_at      REAL    NOT NULL,
                  node_metadata   JSON,
                  name            TEXT,
                  user            TEXT,
                  progression_id  TEXT    NOT NULL REFERENCES progressions(id),
                  first_msg_id    TEXT    REFERENCES messages(id),
                  last_msg_id     TEXT    REFERENCES messages(id),
                  updated_at      REAL    NOT NULL,
                  playbook_name   TEXT,
                  agent_name      TEXT,
                  invocation_kind TEXT CHECK(
                                    invocation_kind IS NULL
                                    OR invocation_kind IN
                                      ('agent', 'play', 'flow', 'fanout', 'show-play')
                                  ),
                  show_topic      TEXT,
                  show_play_name  TEXT,
                  artifacts_path  TEXT,
                  source_kind     TEXT    DEFAULT 'live' CHECK(
                                    source_kind IS NULL
                                    OR source_kind IN ('live', 'imported_fs')
                                  ),
                  status          TEXT,
                  started_at      REAL,
                  ended_at        REAL,
                  last_message_at REAL,
                  current_phase   TEXT,
                  invocation_id   TEXT,
                  model           TEXT,
                  provider        TEXT,
                  effort          TEXT,
                  agent_hash      TEXT,
                  project         TEXT,
                  project_source  TEXT,
                  status_reason_code     TEXT,
                  status_reason_summary  TEXT,
                  status_evidence_refs   JSON,
                  artifact_contract_json      JSON,
                  artifact_verification_json  JSON,
                  input_tokens    INTEGER,
                  output_tokens   INTEGER,
                  total_cost_usd  REAL,
                  num_turns       INTEGER,
                  duration_ms     REAL
                )
                """
            )
            select_cols = []
            for c in cols:
                if c == "updated_at":
                    select_cols.append(
                        "COALESCE(updated_at, created_at, strftime('%s','now')) AS updated_at"
                    )
                else:
                    select_cols.append(c)
            select_list = ", ".join(select_cols)
            insert_sql = f"INSERT INTO sessions_new ({col_list}) SELECT {select_list} FROM sessions"  # noqa: S608
            await self.db.execute(insert_sql)
            await self.db.execute("DROP TABLE sessions")
            await self.db.execute("ALTER TABLE sessions_new RENAME TO sessions")
            for idx_sql in index_sqls:
                await self.db.execute(idx_sql)
            await self.db.commit()
        finally:
            await self.db.execute("PRAGMA foreign_keys = ON")

    # Substring present only in the post-#1174 schedules CREATE SQL;
    # its absence indicates a legacy DB whose action_kind CHECK needs rebuilding.
    _LEGACY_SCHEDULES_FLOW_YAML_MARKER = "'flow_yaml'"

    async def _drop_legacy_action_kind_check(self) -> None:
        """Rebuild ``schedules`` if it still carries the pre-#1174 action_kind CHECK.

        The old CHECK omits ``'flow_yaml'``; SQLite cannot drop a constraint via
        ALTER TABLE, so we use the rename → CREATE new → INSERT SELECT → DROP
        old pattern (same as ``_drop_legacy_session_status_check``).
        """
        cur = await self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedules'"
        )
        row = await cur.fetchone()
        if row is None or row["sql"] is None:
            return
        create_sql: str = row["sql"]
        if self._LEGACY_SCHEDULES_FLOW_YAML_MARKER in create_sql:
            # Table was already created / rebuilt with flow_yaml in the CHECK.
            return

        idx_cur = await self.db.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='schedules' AND sql IS NOT NULL"
        )
        index_sqls = [r["sql"] for r in await idx_cur.fetchall()]

        info_cur = await self.db.execute("PRAGMA table_info(schedules)")
        cols = [r["name"] for r in await info_cur.fetchall()]
        col_list = ", ".join(cols)

        await self.db.execute("PRAGMA foreign_keys = OFF")
        try:
            await self.db.execute(
                """
                CREATE TABLE schedules_new (
                  id                  TEXT    PRIMARY KEY,
                  name                TEXT    NOT NULL UNIQUE,
                  description         TEXT,
                  enabled             INTEGER NOT NULL DEFAULT 1
                                      CHECK(enabled IN (0, 1)),
                  trigger_type        TEXT    NOT NULL
                                      CHECK(trigger_type IN ('cron', 'interval', 'github_poll')),
                  cron_expr           TEXT,
                  interval_sec        INTEGER,
                  github_repo         TEXT,
                  github_filter       JSON,
                  github_cursor       TEXT,
                  poll_interval_sec   INTEGER,
                  action_kind         TEXT    NOT NULL
                                      CHECK(action_kind IN ('agent', 'flow', 'fanout', 'play', 'flow_yaml')),
                  action_model        TEXT,
                  action_prompt       TEXT,
                  action_agent        TEXT,
                  action_playbook     TEXT,
                  action_flow_yaml    TEXT,
                  action_project      TEXT,
                  action_extra_args   JSON    DEFAULT '[]',
                  on_success          JSON,
                  on_fail             JSON,
                  last_fired_at       REAL,
                  next_fire_at        REAL,
                  missed_fire_policy  TEXT    NOT NULL DEFAULT 'skip'
                                      CHECK(missed_fire_policy IN ('skip', 'run_once')),
                  overlap_policy      TEXT    NOT NULL DEFAULT 'skip'
                                      CHECK(overlap_policy IN ('skip', 'allow')),
                  project             TEXT,
                  created_at          REAL    NOT NULL,
                  updated_at          REAL    NOT NULL
                )
                """
            )
            insert_sql = f"INSERT INTO schedules_new ({col_list}) SELECT {col_list} FROM schedules"  # noqa: S608
            await self.db.execute(insert_sql)
            await self.db.execute("DROP TABLE schedules")
            await self.db.execute("ALTER TABLE schedules_new RENAME TO schedules")
            for idx_sql in index_sqls:
                await self.db.execute(idx_sql)
            await self.db.commit()
        finally:
            await self.db.execute("PRAGMA foreign_keys = ON")

    # ── Schema version ─────────────────────────────────────────────────

    async def schema_version(self) -> str | None:
        cur = await self.db.execute("SELECT value FROM schema_meta WHERE key = 'version'")
        row = await cur.fetchone()
        return row["value"] if row else None

    # ── Messages ───────────────────────────────────────────────────────

    _UNKNOWN_TYPE_ID = 0

    async def insert_message(self, msg: dict[str, Any]) -> None:
        if msg.get("content") is None:
            raise ValueError("messages.content is NOT NULL (ADR-0009)")
        role = msg.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"messages.role must be a non-empty string; got {role!r}")

        lion_class_str = (msg.get("node_metadata") or {}).get("lion_class", "")
        node_metadata = _to_json_column(msg.get("node_metadata"))
        content = _to_json_column(msg["content"])

        # Serialise the full message write (including the message_types upsert
        # sub-commit in _resolve_lion_class) behind _write_lock so this path
        # cannot interleave with insert_session_signal's or update_status's
        # BEGIN IMMEDIATE on the same aiosqlite connection.
        async with self._write_lock:
            type_id = await self._resolve_lion_class(lion_class_str)

            # ON CONFLICT(id) DO UPDATE so re-emitted hooks overwrite stale content.
            await self.db.execute(
                """INSERT INTO messages (id, created_at, node_metadata, content,
                   embedding, sender, recipient, channel, role, lion_class)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     node_metadata = excluded.node_metadata,
                     content       = excluded.content,
                     embedding     = excluded.embedding,
                     sender        = excluded.sender,
                     recipient     = excluded.recipient,
                     channel       = excluded.channel,
                     role          = excluded.role,
                     lion_class    = excluded.lion_class""",
                (
                    msg["id"],
                    msg["created_at"],
                    node_metadata,
                    content,
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
        """Get or create a message_types row; race-safe via INSERT OR IGNORE."""
        if not lion_class_str:
            return self._UNKNOWN_TYPE_ID
        await self.db.execute(
            "INSERT OR IGNORE INTO message_types (lion_class) VALUES (?)",
            (lion_class_str,),
        )
        cur = await self.db.execute(
            "SELECT type_id FROM message_types WHERE lion_class = ?",
            (lion_class_str,),
        )
        row = await cur.fetchone()
        await self.db.commit()
        return row["type_id"]

    # ── Progressions ───────────────────────────────────────────────────

    async def create_progression(
        self, progression_id: str, collection: list[str] | None = None
    ) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO progressions (id, created_at, collection) VALUES (?, ?, ?)",
            (progression_id, time.time(), _json_dumps(collection or [])),
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
        """Idempotent append of message_id to the progression JSON array."""
        async with self._write_lock:
            await self.db.execute(
                """UPDATE progressions
                   SET collection = json_insert(collection, '$[#]', ?)
                   WHERE id = ?
                     AND NOT EXISTS (
                       SELECT 1 FROM json_each(progressions.collection)
                       WHERE value = ?
                     )""",
                (message_id, progression_id, message_id),
            )
            await self.db.commit()

    # ── Sessions ───────────────────────────────────────────────────────

    async def create_session(self, session: dict[str, Any]) -> None:
        _validate_session_status(session.get("status"))
        _validate_enum(
            "invocation_kind",
            session.get("invocation_kind"),
            _INVOCATION_KINDS,
            adr="ADR-0012",
        )
        _validate_enum(
            "source_kind",
            session.get("source_kind"),
            _SOURCE_KINDS,
            adr="ADR-0012",
        )
        now = time.time()
        cur = await self.db.execute(
            """INSERT OR IGNORE INTO sessions (id, created_at, node_metadata, name, user,
               progression_id, first_msg_id, last_msg_id, updated_at,
               playbook_name, agent_name, invocation_kind, show_topic,
               show_play_name, artifacts_path, artifact_contract_json,
               artifact_verification_json, source_kind,
               status, started_at, ended_at, last_message_at, invocation_id,
               model, provider, effort, agent_hash,
               project, project_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session["id"],
                session.get("created_at", now),
                _to_json_column(session.get("node_metadata")),
                session.get("name"),
                session.get("user"),
                session["progression_id"],
                session.get("first_msg_id"),
                session.get("last_msg_id"),
                session.get("updated_at", now),
                session.get("playbook_name"),
                session.get("agent_name"),
                session.get("invocation_kind"),
                session.get("show_topic"),
                session.get("show_play_name"),
                session.get("artifacts_path"),
                _to_json_column(session.get("artifact_contract_json")),
                _to_json_column(session.get("artifact_verification_json")),
                session.get("source_kind", "live"),
                session.get("status"),
                session.get("started_at"),
                session.get("ended_at"),
                session.get("last_message_at", session.get("started_at", now)),
                session.get("invocation_id"),
                session.get("model"),
                session.get("provider"),
                session.get("effort"),
                session.get("agent_hash"),
                session.get("project"),
                session.get("project_source"),
            ),
        )
        # Only increment session_count when INSERT actually created a row.
        if session.get("invocation_id") and cur.rowcount:
            await self.db.execute(
                "UPDATE invocations SET session_count = session_count + 1, "
                "updated_at = ? WHERE id = ?",
                (now, session["invocation_id"]),
            )
        await self.db.commit()
        project_name = session.get("project")
        if project_name:
            await self.register_project(
                project_name,
                session.get("project_source") or "git_remote",
            )

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def touch_session_activity(self, session_id: str, *, at: float | None = None) -> None:
        """Bump last_message_at and updated_at for staleness detection."""
        ts = at if at is not None else time.time()
        async with self._write_lock:
            await self.db.execute(
                "UPDATE sessions "
                "SET last_message_at = MAX(COALESCE(last_message_at, 0), ?), "
                "    updated_at      = MAX(COALESCE(updated_at, 0), ?) "
                "WHERE id = ?",
                (ts, ts, session_id),
            )
            await self.db.commit()

    async def update_session(
        self,
        session_id: str,
        *,
        reason_code: str | None = None,
        reason_summary: str = "",
        evidence_refs: list[dict[str, Any]] | None = None,
        reason_source: str = "executor",
        reason_actor: str | None = None,
        **fields: Any,
    ) -> None:
        """Update session fields; route status changes through update_status()."""
        _validate_columns(fields, _SESSION_COLUMNS)
        if "invocation_kind" in fields:
            _validate_enum(
                "invocation_kind",
                fields["invocation_kind"],
                _INVOCATION_KINDS,
                adr="ADR-0012",
            )
        if "source_kind" in fields:
            _validate_enum(
                "source_kind",
                fields["source_kind"],
                _SOURCE_KINDS,
                adr="ADR-0012",
            )

        if "status" in fields:
            _validate_session_status(fields["status"])
        await self._route_status_change(
            "session",
            session_id,
            "update_session",
            fields,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=evidence_refs,
            reason_source=reason_source,
            reason_actor=reason_actor,
        )

        if fields:
            fields["updated_at"] = time.time()
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [session_id]
            async with self._write_lock:
                await self.db.execute(
                    f"UPDATE sessions SET {sets} WHERE id = ?",  # noqa: S608
                    vals,
                )
                await self.db.commit()

    async def update_artifact_verification(
        self,
        session_id: str,
        verification: dict[str, Any] | None,
    ) -> None:
        # Must hold _write_lock: teardown calls this while signal persistence is
        # still bound (unbind happens after _teardown_common returns), so a late
        # signal emit's BEGIN IMMEDIATE can race this implicit UPDATE+commit.
        async with self._write_lock:
            await self.db.execute(
                "UPDATE sessions SET artifact_verification_json = ?, updated_at = ? WHERE id = ?",
                (_to_json_column(verification), time.time(), session_id),
            )
            await self.db.commit()

    async def set_session_provenance(
        self,
        session_id: str,
        *,
        node_metadata: dict[str, Any] | None = None,
        project: str | None = None,
        project_source: str | None = None,
    ) -> None:
        """Write attribution/provenance fields without touching updated_at.

        Project bucketing and conversation lineage describe where a session came
        from, not whether it is live, so they must never move the liveness clock
        (which reconcile_session_status and the phantom reaper read). project and
        project_source are written together (the source is meaningless alone).
        The session update and the projects-registry upsert run as one locked
        write so neither can commit without the other.
        """
        sets: list[str] = []
        vals: list[Any] = []
        if node_metadata is not None:
            sets.append("node_metadata = ?")
            vals.append(_to_json_column(node_metadata))
        if project is not None:
            sets.append("project = ?")
            vals.append(project)
            sets.append("project_source = ?")
            vals.append(project_source)
        if not sets:
            return
        vals.append(session_id)
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?",  # noqa: S608
                    vals,
                )
                if project:
                    await self._upsert_project_stmt(project, project_source or "cwd_dir")
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    # ── Status reason model ───────────────────────────────────────────

    async def _route_status_change(
        self,
        entity_type: str,
        entity_id: str,
        caller_name: str,
        fields: dict[str, Any],
        *,
        reason_code: str | None,
        reason_summary: str,
        evidence_refs: list[dict[str, Any]] | None,
        reason_source: str,
        reason_actor: str | None,
    ) -> None:
        status_value = fields.pop("status", None)
        if status_value is None:
            return
        if reason_code is None:
            from warnings import warn

            resolved = _default_reason_code_for_entity_status(entity_type, status_value)
            if resolved is None:
                raise ValueError(
                    f"{caller_name}() called with status={status_value!r} but "
                    f"no canonical default reason_code exists for "
                    f"({entity_type}, {status_value!r}). Pass reason_code "
                    f"explicitly from lionagi/state/reasons.py."
                )
            reason_code = resolved
            warn(
                f"{caller_name}({entity_id!r}, status={status_value!r}) "
                "called without reason_code; defaulting to "
                f"{reason_code!r}. Pass reason_code explicitly "
                "(ADR-0028 Phase 2 deprecation).",
                DeprecationWarning,
                stacklevel=3,
            )
        await self.update_status(
            entity_type,
            entity_id,
            new_status=status_value,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=evidence_refs,
            source=reason_source,
            actor=reason_actor,
        )

    async def update_status(
        self,
        entity_type: str,
        entity_id: str,
        *,
        new_status: str,
        reason_code: str,
        reason_summary: str = "",
        evidence_refs: list[dict[str, Any]] | None = None,
        source: str = "executor",
        actor: str | None = None,
        metadata: dict[str, Any] | None = None,
        expected_statuses: set[str | None] | frozenset[str | None] | None = None,
    ) -> bool:
        """Atomically transition an entity's status and record the reason.

        When *expected_statuses* is provided, the transition is only performed
        if the current status is a member of that set.  Pass ``None`` inside
        the set to match a SQL NULL status (e.g. ``{None}`` for null-status
        sessions, ``{"running", None}`` to accept either).

        Returns ``True`` when the transition was applied, ``False`` when it was
        skipped because the current status was not in *expected_statuses*.  All
        existing callers that ignore the return value are unaffected.
        """
        if source not in _VALID_STATUS_SOURCES:
            raise ValueError(
                f"update_status() called with source={source!r}; "
                f"must be one of {sorted(_VALID_STATUS_SOURCES)}."
            )
        canonical_type = _validate_entity_type_for_reason(entity_type)
        _validate_reason_code(reason_code)
        table = _reason_entity_table(canonical_type)
        evidence_json = _json_dumps(evidence_refs or [])
        metadata_json = _json_dumps(metadata) if metadata is not None else None
        now = time.time()

        # Serialise the BEGIN IMMEDIATE window against all other write paths on
        # this connection so no concurrent coroutine can issue its own BEGIN or
        # implicit-write commit while this transaction is open.
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = await self.db.execute(
                    f"SELECT status FROM {table} WHERE id = ?",  # noqa: S608
                    (entity_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise LookupError(f"{canonical_type} {entity_id!r} not found (table={table})")
                previous_status = row["status"] if "status" in row.keys() else None

                if expected_statuses is not None and previous_status not in expected_statuses:
                    # CAS guard: current status is not in the expected set — skip.
                    await self.db.rollback()
                    return False

                await self.db.execute(
                    f"UPDATE {table} SET "  # noqa: S608
                    "  status = ?, "
                    "  status_reason_code = ?, "
                    "  status_reason_summary = ?, "
                    "  status_evidence_refs = ?, "
                    "  updated_at = ? "
                    "WHERE id = ?",
                    (
                        new_status,
                        reason_code,
                        reason_summary,
                        evidence_json,
                        now,
                        entity_id,
                    ),
                )

                await self.db.execute(
                    "INSERT INTO status_transitions "
                    "(id, entity_type, entity_id, previous_status, status, "
                    " reason_code, reason_summary, evidence_refs, "
                    " source, actor, created_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        canonical_type,
                        entity_id,
                        previous_status,
                        new_status,
                        reason_code,
                        reason_summary,
                        evidence_json,
                        source,
                        actor,
                        now,
                        metadata_json,
                    ),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        return True

    async def list_sessions(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM sessions"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur = await self.db.execute(query, params)
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def count_sessions(self, *, status: str | None = None) -> int:
        if status:
            cur = await self.db.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE status = ?",
                (status,),
            )
        else:
            cur = await self.db.execute("SELECT COUNT(*) AS n FROM sessions")
        row = await cur.fetchone()
        return row["n"]

    # ── Projects ──────────────────────────────────────────────────────

    async def _upsert_project_stmt(
        self,
        name: str,
        source: str,
        *,
        path: str | None = None,
        github: str | None = None,
    ) -> None:
        """Projects-registry upsert statement only; caller owns the lock and commit."""
        now = time.time()
        await self.db.execute(
            """INSERT INTO projects
                   (name, source, path, github, created_at, updated_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   last_seen_at = excluded.last_seen_at,
                   updated_at   = excluded.updated_at,
                   source       = COALESCE(
                       CASE WHEN excluded.source IN ('config_toml', 'global_override')
                            THEN excluded.source ELSE NULL END,
                       projects.source
                   ),
                   path   = COALESCE(excluded.path, projects.path),
                   github = COALESCE(excluded.github, projects.github)""",
            (name, source, path, github, now, now, now),
        )

    async def register_project(
        self,
        name: str,
        source: str,
        *,
        path: str | None = None,
        github: str | None = None,
    ) -> None:
        """Upsert a project entry; bumps last_seen_at on conflict."""
        await self._upsert_project_stmt(name, source, path=path, github=github)
        await self.db.commit()

    async def create_project(
        self,
        name: str,
        *,
        github: str | None = None,
        description: str | None = None,
        path: str | None = None,
    ) -> None:
        """Insert a Studio-managed project (source='studio')."""
        now = time.time()
        await self.db.execute(
            """INSERT INTO projects
                   (name, source, path, github, description,
                    created_at, updated_at, last_seen_at)
               VALUES (?, 'studio', ?, ?, ?, ?, ?, ?)""",
            (name, path, github, description, now, now, now),
        )
        await self.db.commit()

    async def list_projects(self) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            """SELECT p.*,
                      COUNT(s.id) AS session_count,
                      SUM(CASE WHEN s.status = 'running' THEN 1 ELSE 0 END) AS running_count
               FROM projects p
               LEFT JOIN sessions s ON s.project = p.name
               GROUP BY p.name
               ORDER BY COALESCE(p.last_seen_at, p.updated_at) DESC"""
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_project(self, name: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            """SELECT p.*,
                      COUNT(s.id) AS session_count,
                      SUM(CASE WHEN s.status = 'running' THEN 1 ELSE 0 END) AS running_count
               FROM projects p
               LEFT JOIN sessions s ON s.project = p.name
               WHERE p.name = ?
               GROUP BY p.name""",
            (name,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_project(self, name: str, **fields: Any) -> bool:
        allowed = {"description", "github", "path"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Invalid project field(s): {bad}")
        if not fields:
            return False
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [name]
        cur = await self.db.execute(
            f"UPDATE projects SET {sets} WHERE name = ?",  # noqa: S608
            vals,
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def delete_project(self, name: str) -> bool:
        """Delete a Studio-managed project; auto-detected ones are immutable."""
        cur = await self.db.execute(
            "DELETE FROM projects WHERE name = ? AND source = 'studio'",
            (name,),
        )
        await self.db.commit()
        return cur.rowcount > 0

    # ── Schedules (ADR-0027) ──────────────────────────────────────────

    async def create_schedule(self, schedule: dict[str, Any]) -> None:
        now = time.time()
        await self.db.execute(
            """INSERT INTO schedules
               (id, name, description, enabled, trigger_type,
                cron_expr, interval_sec, github_repo, github_filter,
                github_cursor, poll_interval_sec,
                action_kind, action_model, action_prompt, action_agent,
                action_playbook, action_flow_yaml, action_project, action_extra_args,
                on_success, on_fail, last_fired_at, next_fire_at,
                missed_fire_policy, overlap_policy, project,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                schedule["id"],
                schedule["name"],
                schedule.get("description"),
                schedule.get("enabled", 1),
                schedule["trigger_type"],
                schedule.get("cron_expr"),
                schedule.get("interval_sec"),
                schedule.get("github_repo"),
                _to_json_column(schedule.get("github_filter")),
                schedule.get("github_cursor"),
                schedule.get("poll_interval_sec"),
                schedule["action_kind"],
                schedule.get("action_model"),
                schedule.get("action_prompt"),
                schedule.get("action_agent"),
                schedule.get("action_playbook"),
                schedule.get("action_flow_yaml"),
                schedule.get("action_project"),
                _to_json_column(schedule.get("action_extra_args", [])),
                _to_json_column(schedule.get("on_success")),
                _to_json_column(schedule.get("on_fail")),
                schedule.get("last_fired_at"),
                schedule.get("next_fire_at"),
                schedule.get("missed_fire_policy", "skip"),
                schedule.get("overlap_policy", "skip"),
                schedule.get("project"),
                schedule.get("created_at", now),
                schedule.get("updated_at", now),
            ),
        )
        await self.db.commit()

    async def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def get_schedule_by_name(self, name: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM schedules WHERE name = ?", (name,))
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_schedules(
        self,
        *,
        enabled: bool | None = None,
        trigger_type: str | None = None,
        project: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM schedules"
        conds: list[str] = []
        params: list[Any] = []
        if enabled is not None:
            conds.append("enabled = ?")
            params.append(1 if enabled else 0)
        if trigger_type:
            conds.append("trigger_type = ?")
            params.append(trigger_type)
        if project:
            conds.append("project = ?")
            params.append(project)
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur = await self.db.execute(query, params)
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def update_schedule(self, schedule_id: str, **fields: Any) -> None:
        allowed = {
            "name",
            "description",
            "enabled",
            "trigger_type",
            "cron_expr",
            "interval_sec",
            "github_repo",
            "github_filter",
            "github_cursor",
            "poll_interval_sec",
            "action_kind",
            "action_model",
            "action_prompt",
            "action_agent",
            "action_playbook",
            "action_flow_yaml",
            "action_project",
            "action_extra_args",
            "on_success",
            "on_fail",
            "last_fired_at",
            "next_fire_at",
            "missed_fire_policy",
            "overlap_policy",
            "project",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Invalid schedule field(s): {bad}")
        for k in ("github_filter", "action_extra_args", "on_success", "on_fail"):
            if k in fields:
                fields[k] = _to_json_column(fields[k])
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [schedule_id]
        await self.db.execute(
            f"UPDATE schedules SET {sets} WHERE id = ?",  # noqa: S608
            vals,
        )
        await self.db.commit()

    async def delete_schedule(self, schedule_id: str) -> bool:
        cur = await self.db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        await self.db.commit()
        return cur.rowcount > 0

    # ── Schedule Runs (ADR-0027) ──────────────────────────────────────

    async def create_schedule_run(self, run: dict[str, Any]) -> None:
        now = time.time()
        await self.db.execute(
            """INSERT INTO schedule_runs
               (id, schedule_id, invocation_id, trigger_context,
                action_kind, action_args, status, exit_code,
                chain_parent_id, chain_depth, fired_at, ended_at,
                error_detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run["id"],
                run["schedule_id"],
                run.get("invocation_id"),
                _to_json_column(run["trigger_context"]),
                run["action_kind"],
                _to_json_column(run["action_args"]),
                run.get("status", "running"),
                run.get("exit_code"),
                run.get("chain_parent_id"),
                run.get("chain_depth", 0),
                run["fired_at"],
                run.get("ended_at"),
                run.get("error_detail"),
                run.get("created_at", now),
            ),
        )
        await self.db.commit()

    async def update_schedule_run(
        self,
        run_id: str,
        *,
        reason_code: str | None = None,
        reason_summary: str = "",
        evidence_refs: list[dict[str, Any]] | None = None,
        reason_source: str = "executor",
        reason_actor: str | None = None,
        **fields: Any,
    ) -> None:
        """Update schedule_run fields; route status through update_status()."""
        allowed = {"status", "exit_code", "ended_at", "error_detail", "invocation_id"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Invalid schedule_run field(s): {bad}")

        await self._route_status_change(
            "schedule_run",
            run_id,
            "update_schedule_run",
            fields,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=evidence_refs,
            reason_source=reason_source,
            reason_actor=reason_actor,
        )

        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [run_id]
            await self.db.execute(
                f"UPDATE schedule_runs SET {sets} WHERE id = ?",  # noqa: S608
                vals,
            )
            await self.db.commit()

    async def list_schedule_runs(
        self,
        schedule_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM schedule_runs WHERE schedule_id = ?"
        params: list[Any] = [schedule_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY fired_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur = await self.db.execute(query, params)
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_schedule_run(self, run_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM schedule_runs WHERE id = ?", (run_id,))
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_running_schedule_runs(self, schedule_id: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM schedule_runs WHERE schedule_id = ? AND status = 'running'",
            (schedule_id,),
        )
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Invocations (ADR-0020) ──────────────────────────────────────────

    async def create_invocation(self, invocation: dict[str, Any]) -> None:
        status = invocation.get("status", "running")
        _validate_enum(
            "status",
            status,
            _INVOCATION_STATUSES,
            adr="ADR-0020",
            nullable=False,
        )
        now = time.time()
        await self.db.execute(
            """INSERT OR IGNORE INTO invocations
               (id, skill, plugin, prompt, started_at, ended_at, status,
                session_count, created_at, updated_at, node_metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                invocation["id"],
                invocation["skill"],
                invocation.get("plugin"),
                invocation.get("prompt"),
                invocation["started_at"],
                invocation.get("ended_at"),
                status,
                invocation.get("session_count", 0),
                invocation.get("created_at", now),
                invocation.get("updated_at", now),
                _to_json_column(invocation.get("node_metadata")),
            ),
        )
        await self.db.commit()

    async def update_invocation(
        self,
        invocation_id: str,
        *,
        reason_code: str | None = None,
        reason_summary: str = "",
        evidence_refs: list[dict[str, Any]] | None = None,
        reason_source: str = "executor",
        reason_actor: str | None = None,
        **fields: Any,
    ) -> None:
        """Update invocation fields; route status changes through update_status()."""
        _validate_columns(fields, _INVOCATION_COLUMNS)
        if "status" in fields:
            _validate_enum(
                "status",
                fields["status"],
                _INVOCATION_STATUSES,
                adr="ADR-0020",
                nullable=False,
            )
        if "node_metadata" in fields:
            fields["node_metadata"] = _to_json_column(fields["node_metadata"])

        await self._route_status_change(
            "invocation",
            invocation_id,
            "update_invocation",
            fields,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=evidence_refs,
            reason_source=reason_source,
            reason_actor=reason_actor,
        )

        if fields:
            fields["updated_at"] = time.time()
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [invocation_id]
            update_sql = f"UPDATE invocations SET {sets} WHERE id = ?"  # noqa: S608
            await self.db.execute(update_sql, vals)
            await self.db.commit()

    async def get_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM invocations WHERE id = ?", (invocation_id,))
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_invocations(
        self,
        *,
        skill: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT inv.*, "
            "  sq.project        AS project, "
            "  sq.project_source AS project_source "
            "FROM invocations inv "
            "LEFT JOIN ( "
            "  SELECT invocation_id, project, project_source "
            "  FROM sessions "
            "  WHERE invocation_id IS NOT NULL "
            "  GROUP BY invocation_id "
            "  HAVING MAX(updated_at) "
            ") sq ON sq.invocation_id = inv.id"
        )
        conds: list[str] = []
        params: list[Any] = []
        if skill:
            conds.append("inv.skill = ?")
            params.append(skill)
        if status:
            conds.append("inv.status = ?")
            params.append(status)
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY inv.updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur = await self.db.execute(query, params)
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def list_sessions_for_invocation(self, invocation_id: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM sessions WHERE invocation_id = ? ORDER BY created_at ASC",
            (invocation_id,),
        )
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Artifacts (ADR-0021) ─────────────────────────────────────────────

    async def _find_artifact_id(
        self,
        *,
        kind: str,
        name: str,
        invocation_id: str | None,
        session_id: str | None,
    ) -> str | None:
        """Return the artifact id matching the natural key, or None."""
        if invocation_id is not None and session_id is not None:
            cur = await self.db.execute(
                "SELECT id FROM artifacts "
                "WHERE invocation_id = ? AND session_id = ? AND kind = ? AND name = ?",
                (invocation_id, session_id, kind, name),
            )
        elif invocation_id is not None:
            cur = await self.db.execute(
                "SELECT id FROM artifacts "
                "WHERE invocation_id = ? AND session_id IS NULL AND kind = ? AND name = ?",
                (invocation_id, kind, name),
            )
        elif session_id is not None:
            cur = await self.db.execute(
                "SELECT id FROM artifacts "
                "WHERE session_id = ? AND invocation_id IS NULL AND kind = ? AND name = ?",
                (session_id, kind, name),
            )
        else:
            cur = await self.db.execute(
                "SELECT id FROM artifacts "
                "WHERE invocation_id IS NULL AND session_id IS NULL AND kind = ? AND name = ?",
                (kind, name),
            )
        row = await cur.fetchone()
        return row["id"] if row else None

    async def insert_artifact(
        self,
        *,
        kind: str,
        name: str,
        content: dict[str, Any],
        invocation_id: str | None = None,
        session_id: str | None = None,
        file_path: str | None = None,
    ) -> str:
        """Upsert one structured artifact; return its stable id."""
        if not kind:
            raise ValueError("artifact kind is required")
        if not name:
            raise ValueError("artifact name is required")
        now = time.time()
        content_json = _to_json_column(content)
        existing_id = await self._find_artifact_id(
            kind=kind, name=name, invocation_id=invocation_id, session_id=session_id
        )
        if existing_id:
            await self.db.execute(
                "UPDATE artifacts SET content = ?, file_path = ?, updated_at = ? WHERE id = ?",
                (content_json, file_path, now, existing_id),
            )
            await self.db.commit()
            return existing_id
        art_id = uuid.uuid4().hex[:12]
        await self.db.execute(
            "INSERT INTO artifacts "
            "(id, invocation_id, session_id, created_at, updated_at, kind, name, content, file_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (art_id, invocation_id, session_id, now, now, kind, name, content_json, file_path),
        )
        await self.db.commit()
        return art_id

    async def list_artifacts_for_invocation(self, invocation_id: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM artifacts WHERE invocation_id = ? ORDER BY created_at ASC",
            (invocation_id,),
        )
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def list_artifacts_for_session(self, session_id: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM artifacts WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    # ── Admin events (ADR-0024) ─────────────────────────────────────────

    async def insert_admin_event(
        self,
        *,
        action: str,
        details: dict[str, Any],
        target_id: str | None = None,
        actor: str = "admin",
    ) -> str:
        """Append one row to the admin event log; returns the event id."""
        event_id = uuid.uuid4().hex[:12]
        await self.db.execute(
            "INSERT INTO admin_events (id, created_at, action, target_id, "
            "details, actor) VALUES (?, ?, ?, ?, ?, ?)",
            (
                event_id,
                time.time(),
                action,
                target_id,
                _to_json_column(details),
                actor,
            ),
        )
        await self.db.commit()
        return event_id

    async def list_admin_events(
        self,
        *,
        action: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM admin_events"
        conds: list[str] = []
        params: list[Any] = []
        if action:
            conds.append("action = ?")
            params.append(action)
        if target_id:
            conds.append("target_id = ?")
            params.append(target_id)
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cur = await self.db.execute(query, params)
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Branches ───────────────────────────────────────────────────────

    async def create_branch(self, branch: dict[str, Any]) -> None:
        await self.db.execute(
            """INSERT OR IGNORE INTO branches (id, created_at, node_metadata, user, name,
               session_id, progression_id, system_msg_id, model, provider, agent_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                branch["id"],
                branch.get("created_at", time.time()),
                _to_json_column(branch.get("node_metadata")),
                branch.get("user"),
                branch.get("name"),
                branch["session_id"],
                branch["progression_id"],
                branch.get("system_msg_id"),
                branch.get("model"),
                branch.get("provider"),
                branch.get("agent_name"),
            ),
        )
        await self.db.commit()

    async def get_branch(self, branch_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM branches WHERE id = ?", (branch_id,))
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def update_branch(self, branch_id: str, **fields: Any) -> None:
        _validate_columns(fields, _BRANCH_COLUMNS)
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [branch_id]
        async with self._write_lock:
            await self.db.execute(
                f"UPDATE branches SET {sets} WHERE id = ?",  # noqa: S608
                vals,
            )
            await self.db.commit()

    async def repair_branch_progression(
        self,
        branch_id: str,
        new_progression_id: str,
    ) -> str | None:
        """Backfill NULL progression_id; returns the effective id or None."""
        await self.db.execute(
            "UPDATE branches SET progression_id = ? WHERE id = ? AND progression_id IS NULL",
            (new_progression_id, branch_id),
        )
        cur = await self.db.execute(
            "SELECT progression_id FROM branches WHERE id = ?",
            (branch_id,),
        )
        row = await cur.fetchone()
        await self.db.commit()
        if row is None:
            return None
        return row["progression_id"]

    async def repair_session_progression(
        self,
        session_id: str,
        new_progression_id: str,
    ) -> str | None:
        """Backfill NULL progression_id; returns the effective id or None."""
        await self.db.execute(
            "UPDATE sessions SET progression_id = ? WHERE id = ? AND progression_id IS NULL",
            (new_progression_id, session_id),
        )
        cur = await self.db.execute(
            "SELECT progression_id FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        await self.db.commit()
        if row is None:
            return None
        return row["progression_id"]

    async def list_branches(self, session_id: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM branches WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_branch_messages(self, branch_id: str) -> list[dict[str, Any]]:
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
                WHERE m.id IN ({placeholders})""",  # noqa: S608
            message_ids,
        )
        rows = await cur.fetchall()
        by_id = {r["id"]: self._row_to_dict(r) for r in rows}
        return [by_id[mid] for mid in message_ids if mid in by_id]

    # ── Shows ─────────────────────────────────────────────────────────

    async def create_show(self, show: dict[str, Any]) -> None:
        _validate_enum(
            "show status",
            show.get("status", "active"),
            _SHOW_STATUSES,
            adr="ADR-0011",
            nullable=False,
        )
        now = time.time()
        await self.db.execute(
            """INSERT OR IGNORE INTO shows (id, topic, goal, repo, base_branch,
               integration_branch, status, show_dir, status_source,
               created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                show["id"],
                show["topic"],
                show.get("goal"),
                show.get("repo"),
                show.get("base_branch"),
                show.get("integration_branch"),
                show.get("status", "active"),
                show["show_dir"],
                show.get("status_source", "unknown"),
                show.get("created_at", now),
                now,
            ),
        )
        await self.db.commit()

    async def get_show(self, show_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM shows WHERE id = ?", (show_id,))
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def get_show_by_topic(self, topic: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM shows WHERE topic = ?", (topic,))
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_shows(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            cur = await self.db.execute(
                "SELECT * FROM shows WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            )
        else:
            cur = await self.db.execute("SELECT * FROM shows ORDER BY updated_at DESC")
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def update_show(
        self,
        show_id: str,
        *,
        reason_code: str | None = None,
        reason_summary: str = "",
        evidence_refs: list[dict[str, Any]] | None = None,
        reason_source: str = "executor",
        reason_actor: str | None = None,
        **fields: Any,
    ) -> None:
        """Update show fields; route status changes through update_status()."""
        _validate_columns(fields, _SHOW_COLUMNS)
        if "status" in fields:
            _validate_enum(
                "show status",
                fields["status"],
                _SHOW_STATUSES,
                adr="ADR-0011",
                nullable=False,
            )

        await self._route_status_change(
            "show",
            show_id,
            "update_show",
            fields,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=evidence_refs,
            reason_source=reason_source,
            reason_actor=reason_actor,
        )

        if fields:
            fields["updated_at"] = time.time()
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [show_id]
            await self.db.execute(
                f"UPDATE shows SET {sets} WHERE id = ?",  # noqa: S608
                vals,
            )
            await self.db.commit()

    # ── Plays ─────────────────────────────────────────────────────────

    async def create_play(self, play: dict[str, Any]) -> None:
        _validate_enum(
            "play status",
            play.get("status", "pending"),
            _PLAY_STATUSES,
            adr="ADR-0011",
            nullable=False,
        )
        now = time.time()
        await self.db.execute(
            """INSERT OR IGNORE INTO plays (id, show_id, name, playbook, effort,
               status, attempt, session_id, started_at, ended_at, exit_code,
               worktree, branch, merge_sha, merged_at, gate_passed, gate_feedback,
               depends_on, sort_order, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                play["id"],
                play["show_id"],
                play["name"],
                play.get("playbook"),
                play.get("effort"),
                play.get("status", "pending"),
                play.get("attempt", 1),
                play.get("session_id"),
                play.get("started_at"),
                play.get("ended_at"),
                play.get("exit_code"),
                play.get("worktree"),
                play.get("branch"),
                play.get("merge_sha"),
                play.get("merged_at"),
                play.get("gate_passed"),
                play.get("gate_feedback"),
                _json_dumps(play.get("depends_on", [])),
                play.get("sort_order", 0),
                play.get("created_at", now),
                now,
            ),
        )
        await self.db.commit()

    async def get_play(self, play_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM plays WHERE id = ?", (play_id,))
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_plays(self, show_id: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM plays WHERE show_id = ? ORDER BY sort_order, created_at",
            (show_id,),
        )
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def update_play(
        self,
        play_id: str,
        *,
        reason_code: str | None = None,
        reason_summary: str = "",
        evidence_refs: list[dict[str, Any]] | None = None,
        reason_source: str = "executor",
        reason_actor: str | None = None,
        **fields: Any,
    ) -> None:
        """Update play fields; route status changes through update_status()."""
        _validate_columns(fields, _PLAY_COLUMNS)
        if "status" in fields:
            _validate_enum(
                "play status",
                fields["status"],
                _PLAY_STATUSES,
                adr="ADR-0011",
                nullable=False,
            )

        await self._route_status_change(
            "play",
            play_id,
            "update_play",
            fields,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=evidence_refs,
            reason_source=reason_source,
            reason_actor=reason_actor,
        )

        if fields:
            fields["updated_at"] = time.time()
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [play_id]
            await self.db.execute(
                f"UPDATE plays SET {sets} WHERE id = ?",  # noqa: S608
                vals,
            )
            await self.db.commit()

    # ── Definitions ───────────────────────────────────────────────────

    async def save_definition(
        self,
        *,
        kind: str,
        name: str,
        path: str,
        content: str,
        message: str | None = None,
    ) -> int:
        if kind not in _DEFINITION_KINDS:
            raise ValueError(
                f"Invalid definition kind {kind!r}; "
                f"ADR-0016 editable set is {sorted(_DEFINITION_KINDS)}"
            )

        lock_key = (kind, name)
        lock = self._definition_locks.setdefault(lock_key, Lock())
        async with lock:
            last_exc: Exception | None = None
            for _ in range(5):
                try:
                    cur = await self.db.execute(
                        "SELECT MAX(version) AS v FROM definitions WHERE kind = ? AND name = ?",
                        (kind, name),
                    )
                    row = await cur.fetchone()
                    next_version = (row["v"] or 0) + 1
                    await self.db.execute(
                        """INSERT INTO definitions
                           (id, kind, name, path, content, version,
                            created_at, message)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(uuid.uuid4()),
                            kind,
                            name,
                            path,
                            content,
                            next_version,
                            time.time(),
                            message,
                        ),
                    )
                    await self.db.commit()
                    return next_version
                except aiosqlite.IntegrityError as exc:
                    last_exc = exc
                    continue
            raise RuntimeError(
                f"save_definition failed to acquire a unique version after "
                f"5 retries (kind={kind!r}, name={name!r}): {last_exc}"
            )

    async def get_definition(
        self, kind: str, name: str, *, version: int | None = None
    ) -> dict[str, Any] | None:
        if version is not None:
            cur = await self.db.execute(
                "SELECT * FROM definitions WHERE kind = ? AND name = ? AND version = ?",
                (kind, name, version),
            )
        else:
            cur = await self.db.execute(
                "SELECT * FROM definitions WHERE kind = ? AND name = ? ORDER BY version DESC LIMIT 1",
                (kind, name),
            )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_definition_versions(self, kind: str, name: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT id, kind, name, version, created_at, message FROM definitions WHERE kind = ? AND name = ? ORDER BY version DESC",
            (kind, name),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Session signals (Phase C Move 1) ─────────────────────────────

    async def insert_session_signal(
        self,
        *,
        session_id: str,
        kind: str,
        op_id: str = "",
        ts: float,
        payload: dict[str, Any],
    ) -> int:
        """Append one lifecycle signal row; returns the assigned seq number.

        seq is MAX(seq)+1 for the session, assigned in the same write so
        concurrent inserts from different processes (WAL mode) do not
        collide — SQLite serialises IMMEDIATE transactions.

        Concurrent coroutines sharing this StateDB instance are serialised
        through ``self._write_lock`` so that no two coroutines can enter
        ``BEGIN IMMEDIATE`` simultaneously on the same aiosqlite connection
        (which would raise "cannot start a transaction within a transaction"
        since aiosqlite uses a single background thread with no transaction
        nesting support).
        """
        sig_id = uuid.uuid4().hex
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = await self.db.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM session_signals WHERE session_id = ?",
                    (session_id,),
                )
                row = await cur.fetchone()
                seq: int = (row[0] if row else 0) + 1
                await self.db.execute(
                    "INSERT INTO session_signals (id, session_id, seq, kind, op_id, ts, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sig_id, session_id, seq, kind, op_id, ts, _to_json_column(payload)),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        return seq

    async def get_session_signals_after(
        self,
        session_id: str,
        after_seq: int,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return signals for *session_id* with seq > *after_seq*, ordered by seq."""
        cur = await self.db.execute(
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
                    "op_id": r["op_id"],
                    "ts": r["ts"],
                    "payload": payload,
                }
            )
        return result

    # ── Engine runs (Phase C Move 2) ──────────────────────────────────

    async def insert_engine_run(
        self,
        *,
        run_id: str,
        kind: str,
        spec_json: dict[str, Any],
        started_at: float,
        session_id: str | None = None,
    ) -> None:
        """Insert a new engine run row with status='running'.

        Serialised through ``self._write_lock`` (same pattern as
        ``insert_session_signal``) to prevent concurrent INSERT conflicts on
        the shared connection.
        """
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    "INSERT INTO engine_runs "
                    "(id, kind, spec_json, status, started_at, session_id) "
                    "VALUES (?, ?, ?, 'running', ?, ?)",
                    (
                        run_id,
                        kind,
                        _to_json_column(spec_json),
                        started_at,
                        session_id,
                    ),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def update_engine_run(
        self,
        run_id: str,
        *,
        status: str,
        ended_at: float | None = None,
        export_dir: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update the mutable fields of an engine run row.

        *status* must be one of ``completed``, ``failed``, or ``cancelled``.
        Serialised through ``self._write_lock``.
        """
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    "UPDATE engine_runs "
                    "SET status = ?, ended_at = ?, export_dir = ?, error = ? "
                    "WHERE id = ?",
                    (status, ended_at, export_dir, error, run_id),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def get_engine_run(self, run_id: str) -> dict[str, Any] | None:
        """Return a single engine run row as a dict, or None if not found."""
        cur = await self.db.execute(
            "SELECT id, kind, spec_json, status, started_at, ended_at, "
            "session_id, export_dir, error "
            "FROM engine_runs WHERE id = ?",
            (run_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("spec_json"), str):
            try:
                d["spec_json"] = json.loads(d["spec_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    async def list_engine_runs(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return engine run rows, newest-first, with optional filters."""
        conditions: list[str] = []
        params: list[Any] = []
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        sql = (
            f"SELECT id, kind, spec_json, status, started_at, ended_at, "  # noqa: S608
            f"session_id, export_dir, error "
            f"FROM engine_runs {where} "
            f"ORDER BY started_at DESC "
            f"LIMIT ? OFFSET ?"
        )
        cur = await self.db.execute(sql, params)
        rows = await cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("spec_json"), str):
                try:
                    d["spec_json"] = json.loads(d["spec_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result

    # ── Engine definitions ─────────────────────────────────────────────

    async def create_engine_def(self, defn: dict[str, Any]) -> None:
        now = time.time()
        await self.db.execute(
            "INSERT INTO engine_defs "
            "(id, name, kind, model, max_depth, max_agents, options, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                defn["id"],
                defn["name"],
                defn["kind"],
                defn.get("model"),
                defn.get("max_depth"),
                defn.get("max_agents"),
                _to_json_column(defn.get("options")),
                defn.get("description"),
                defn.get("created_at", now),
                defn.get("updated_at", now),
            ),
        )
        await self.db.commit()

    async def get_engine_def(self, def_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM engine_defs WHERE id = ?", (def_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("options"), str):
            try:
                d["options"] = json.loads(d["options"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    async def get_engine_def_by_name(self, name: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM engine_defs WHERE name = ?", (name,))
        row = await cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("options"), str):
            try:
                d["options"] = json.loads(d["options"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    async def list_engine_defs(
        self,
        *,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM engine_defs"
        params: list[Any] = []
        if kind is not None:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur = await self.db.execute(query, params)
        rows = await cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("options"), str):
                try:
                    d["options"] = json.loads(d["options"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result

    async def update_engine_def(self, def_id: str, **fields: Any) -> None:
        allowed = {"name", "kind", "model", "max_depth", "max_agents", "options", "description"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Invalid engine_def field(s): {bad}")
        if "options" in fields:
            fields["options"] = _to_json_column(fields["options"])
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [def_id]
        await self.db.execute(
            f"UPDATE engine_defs SET {sets} WHERE id = ?",  # noqa: S608
            vals,
        )
        await self.db.commit()

    async def delete_engine_def(self, def_id: str) -> bool:
        cur = await self.db.execute("DELETE FROM engine_defs WHERE id = ?", (def_id,))
        await self.db.commit()
        return cur.rowcount > 0

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        for key in (
            "node_metadata",
            "content",
            "depends_on",
            "on_success",
            "on_fail",
            "github_filter",
            "action_extra_args",
            "trigger_context",
            "action_args",
            "artifact_contract_json",
            "artifact_verification_json",
            "status_evidence_refs",
        ):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d


# ── Shared singleton accessor ─────────────────────────────────────────────────
# One open StateDB connection is reused across all hook firings for a given
# DB path.  This avoids the per-firing connect + pragma + schema-check cost
# and is the prerequisite for session-lifecycle hook wiring.
#
# Async-safety argument:
#   aiosqlite routes every command through a single background thread, so
#   SQLite serialises all operations at the C layer.  Concurrent coroutines
#   sharing the same StateDB instance are further serialised by the instance's
#   own _write_lock (anyio.Lock) for every mutating path that uses BEGIN
#   IMMEDIATE or needs atomic execute+commit.  A shared instance therefore
#   cannot produce "cannot start a transaction within a transaction" races —
#   that was already the guarantee provided by _write_lock for the CLI paths
#   that pass an open StateDB to bind_db_persistence().
#
# Key by resolved Path so tests that redirect DEFAULT_DB_PATH to a tmp_path
# get their own isolated instance.

_SHARED: dict[Path, StateDB] = {}
# Guards the lazy-open window; created on first async call (anyio.Lock
# must be instantiated inside an active backend task context).
_SHARED_OPEN_LOCK: Lock | None = None
# Bumped by every close_shared_db() sweep so a get_shared_db()/register_shared_db()
# that waited on a now-abandoned lock can detect it raced a teardown.
_SHARED_CLOSE_GEN: int = 0
_SHARED_TEARDOWN_RACE = (
    "shared StateDB was torn down while this call was pending; quiesce "
    "get_shared_db()/register_shared_db() callers before close_shared_db()"
)


async def get_shared_db(path: str | Path | None = None) -> StateDB:
    """Return the process-wide open StateDB for *path* (default: DEFAULT_DB_PATH)."""
    global _SHARED_OPEN_LOCK  # noqa: PLW0603
    resolved = Path(path) if path else DEFAULT_DB_PATH
    if resolved in _SHARED:
        return _SHARED[resolved]
    if _SHARED_OPEN_LOCK is None:
        _SHARED_OPEN_LOCK = Lock()
    lock = _SHARED_OPEN_LOCK
    gen = _SHARED_CLOSE_GEN
    async with lock:
        # A close_shared_db() swept the registry while we waited on this lock;
        # refuse to resurrect the singleton rather than leak a fresh worker.
        if _SHARED_CLOSE_GEN != gen:
            raise RuntimeError(_SHARED_TEARDOWN_RACE)
        # Double-checked: another coroutine may have opened it while we waited.
        if resolved not in _SHARED:
            db = StateDB(resolved)
            await db.open()
            _SHARED[resolved] = db
    return _SHARED[resolved]


async def register_shared_db(db: StateDB) -> None:
    """Adopt a caller-owned StateDB as the shared instance, closing any prior one for its path."""
    global _SHARED_OPEN_LOCK  # noqa: PLW0603
    import contextlib

    if _SHARED_OPEN_LOCK is None:
        _SHARED_OPEN_LOCK = Lock()
    lock = _SHARED_OPEN_LOCK
    gen = _SHARED_CLOSE_GEN
    async with lock:
        if _SHARED_CLOSE_GEN != gen:
            raise RuntimeError(_SHARED_TEARDOWN_RACE)
        existing = _SHARED.get(db.path)
        if existing is not None and existing is not db:
            with contextlib.suppress(Exception):
                await existing.close()
        _SHARED[db.path] = db


def unregister_shared_db(db: StateDB) -> None:
    """Drop *db* from the shared registry iff it is the registered instance."""
    if _SHARED.get(db.path) is db:
        del _SHARED[db.path]


async def close_shared_db() -> None:
    """Close and forget every shared StateDB; callers must quiesce get_shared_db() first."""
    global _SHARED_OPEN_LOCK, _SHARED_CLOSE_GEN  # noqa: PLW0603
    import contextlib

    lock = _SHARED_OPEN_LOCK
    if lock is None:
        # No open ever happened in this loop (opens create the lock first).
        instances = list(_SHARED.values())
        _SHARED.clear()
        _SHARED_CLOSE_GEN += 1
        for db in instances:
            with contextlib.suppress(Exception):
                await db.close()
        return
    # Hold the open lock so an in-flight get_shared_db()/register_shared_db()
    # cannot repopulate _SHARED after the sweep; bump the generation and null
    # the lock last so a waiter that raced this close refuses to resurrect it
    # and a later event loop lazily recreates a fresh lock.
    async with lock:
        instances = list(_SHARED.values())
        _SHARED.clear()
        for db in instances:
            with contextlib.suppress(Exception):
                await db.close()
        _SHARED_CLOSE_GEN += 1
        _SHARED_OPEN_LOCK = None
