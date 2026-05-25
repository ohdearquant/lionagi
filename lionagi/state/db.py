# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from lionagi.cli._runs import LIONAGI_HOME
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

# Per-entity default-reason maps used by the Phase 2 deprecation shim
# in update_invocation / update_show / update_play / update_schedule_run.
# Picking a wrong-domain code (e.g. ``run.completed.ok`` for a successful
# show) pollutes the reason-grouping primitive ADR-0028 introduces for
# Phase 3/4 + ADR-0030. The resolver is conservative — it returns None
# for (entity, status) pairs without a canonical default, forcing the
# caller to surface a clear error instead of synthesizing nonsense.
_RUN_DEFAULTS: dict[str, str] = {
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
    """Map (entity_type, status) → canonical reason_code, or None.

    Returns None for ambiguous (entity, status) pairs — sessions/
    invocations/schedule_runs share the run.* codes (they're process
    runs); shows + plays have entity-specific codes for the subset
    of statuses with canonical defaults. Statuses outside the per-
    entity map return None and the deprecation shim raises so
    callers supply reason_code explicitly.
    """
    if entity_type in ("session", "invocation", "schedule_run"):
        return _RUN_DEFAULTS.get(status)
    if entity_type == "show":
        return _SHOW_DEFAULTS.get(status)
    if entity_type == "play":
        return _PLAY_DEFAULTS.get(status)
    return None


def _default_reason_code_for_status(status: str) -> str:
    """Legacy run-only resolver. Kept for one release to avoid
    breaking external callers that imported the private name.
    New code MUST use :func:`_default_reason_code_for_entity_status`.
    """
    return _RUN_DEFAULTS.get(status, _RunReasons.FAILED_EXCEPTION)


_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB_PATH = LIONAGI_HOME / "state.db"

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
        # ADR-0019: activity marker for staleness detection. Bumped on
        # every message INSERT so a session that hasn't produced output
        # in N hours is detectably stale without scanning ``messages``.
        "last_message_at",
        # ADR-0020: optional FK to the skill orchestration that spawned
        # this session (e.g. a /show invocation grouping its plays).
        "invocation_id",
        # ADR-0022: provenance disclosure — resolved model spec, provider,
        # effort, and the agent profile content hash at invocation time.
        "model",
        "provider",
        "effort",
        "agent_hash",
        # ADR-0026: project detection for session organization.
        "project",
        "project_source",
    }
)

# ADR-0020: invocation lifecycle vocabulary — narrower than session
# status (no 'aborted_after_finish') but otherwise shares the ADR-0025
# terminal set. The schema CHECK is in schema.sql; this set lets the
# Python helpers validate updates symmetrically.
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
        # ADR-0022: per-branch provenance for multi-model flows.
        "model",
        "provider",
        "agent_name",
        "status",
        "started_at",
        "ended_at",
    }
)

# ADR-0025: expanded closed status vocabulary for sessions. The SQLite
# CHECK constraint is removed (see schema.sql); Python is the source of
# truth so we get a clear ``ValueError`` instead of an opaque sqlite
# IntegrityError, and the vocabulary can evolve without table rebuilds.
#
# The six values distinguish operational follow-up actions:
#   - timed_out: deliberate bound hit ("retry with more time")
#   - failed:    unexpected error ("investigate")
#   - aborted:   user pressed Ctrl-C (no follow-up needed)
#   - cancelled: system/orchestrator cancelled (cascade or admin decision)
VALID_SESSION_STATUSES = frozenset(
    {"running", "completed", "failed", "timed_out", "aborted", "cancelled"}
)
SESSION_TERMINAL_STATUSES = frozenset({"completed", "failed", "timed_out", "aborted", "cancelled"})
# Admin/operator transitions cannot mark a session "completed" or
# "timed_out" — those are system-determined outcomes.
ADMIN_TRANSITION_TARGETS = frozenset({"failed", "aborted", "cancelled"})

# Legacy alias retained for any internal call sites that still import the
# private name. New code should use ``VALID_SESSION_STATUSES``.
_SESSION_STATUSES = VALID_SESSION_STATUSES


def can_transition(current: str | None, target: str) -> bool:
    """Return True iff a session may move from ``current`` to ``target``.

    Mirrors khive's GTD ``can_transition`` (Lean4-proven FSM properties):
    transitions only originate from ``running``; terminal states have no
    outgoing transitions.
    """
    if current != "running":
        return False
    return target in SESSION_TERMINAL_STATUSES


# ADR-0012: closed provenance vocabularies. Validated alongside status
# so dashboards/filters can't be polluted with arbitrary text.
_INVOCATION_KINDS = frozenset({"agent", "play", "flow", "fanout", "show-play"})
_SOURCE_KINDS = frozenset({"live", "imported_fs"})

# ADR-0011: shows + plays lifecycle vocabularies.
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

# ADR-0016: only agent + playbook definitions are editable via Studio's
# write path. Skills and third-party plugin components are read-only.
_DEFINITION_KINDS = frozenset({"agent", "playbook"})


def _validate_columns(fields: dict[str, Any], allowed: frozenset[str]) -> None:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Invalid column(s): {bad}")


def _to_json_column(value: Any) -> Any:
    """Serialize ``value`` to a JSON-tagged string for round-trippable storage.

    Without this, a string that happens to be valid JSON (e.g. user
    content ``'{"text": "x"}'``) round-trips to a dict because
    ``_row_to_dict`` ``json.loads()`` every string column. Always
    serializing here means ``json.loads`` on the way out is the exact
    inverse — a string stays a string, a dict stays a dict.

    ``None`` is preserved as ``NULL``. ``bytes`` (used for embeddings)
    are passed through unchanged so they go into the SQLite BLOB
    storage class without UTF-8 coercion.
    """
    if value is None or isinstance(value, bytes | bytearray | memoryview):
        return value
    return json.dumps(value)


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
    """Async SQLite state layer for lionagi's core data model.

    Mirrors the runtime Session / Branch / Message / Progression objects.
    Uses WAL mode for concurrent read + single writer.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self._db: aiosqlite.Connection | None = None
        # Per-(kind, name) serialization for save_definition. Concurrent
        # writers for the same definition stream would race on
        # ``SELECT MAX(version) + INSERT`` and most would fail on the
        # UNIQUE(kind, name, version) index. The lock is keyed by
        # ``(kind, name)`` so unrelated definitions can still progress
        # in parallel.
        self._definition_locks: dict[tuple[str, str], asyncio.Lock] = {}

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
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.execute("PRAGMA synchronous = NORMAL")
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.execute("PRAGMA busy_timeout = 5000")
        await self.db.execute("PRAGMA cache_size = -64000")
        # Explicit policy (matches SQLite default) so the intent is
        # visible: auto-checkpoint every 1000 frames. Long-lived
        # readers can still prevent WAL truncation; users should run
        # ``li state checkpoint --mode TRUNCATE`` to force it.
        await self.db.execute("PRAGMA wal_autocheckpoint = 1000")

    async def _apply_schema(self) -> None:
        # Older state.db files from earlier iterations of the schema lack
        # the provenance / lifecycle columns added by ADR-0012 / ADR-0017,
        # and ``CREATE TABLE IF NOT EXISTS`` is a no-op on existing
        # tables. Reconcile column-by-column FIRST so the index/trigger
        # statements in schema.sql that reference these columns can
        # succeed. This is a forward-only migration; all migrated
        # columns are nullable or have an INSERT-time default.
        await self._reconcile_columns()
        # ADR-0025: existing DBs created under ADR-0017 carry a 4-value
        # CHECK on sessions.status. Drop the constraint via table rebuild
        # before the new vocabulary (timed_out / cancelled) is written.
        await self._drop_legacy_session_status_check()
        schema = _SCHEMA_PATH.read_text()
        lines = [ln for ln in schema.splitlines() if not ln.strip().upper().startswith("PRAGMA")]
        await self.db.executescript("\n".join(lines))

    # Columns that may need to be back-added to an existing sessions
    # table — keyed by table name. Each entry is (column_name, column_def).
    # column_def must be valid in an ``ALTER TABLE ... ADD COLUMN`` clause
    # (SQLite only allows CHECK constraints inline here if they reference
    # only the new column; we skip CHECKs in ALTER and rely on the Python
    # validators in this module to enforce them on old databases).
    _MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = {
        "sessions": [
            ("updated_at", "REAL"),
            ("playbook_name", "TEXT"),
            ("agent_name", "TEXT"),
            ("invocation_kind", "TEXT"),
            ("show_topic", "TEXT"),
            ("show_play_name", "TEXT"),
            ("artifacts_path", "TEXT"),
            ("source_kind", "TEXT"),
            ("status", "TEXT"),
            ("started_at", "REAL"),
            ("ended_at", "REAL"),
            # ADR-0019: activity marker for staleness detection.
            ("last_message_at", "REAL"),
            # ADR-0020: optional FK to invocations table.
            ("invocation_id", "TEXT"),
            # ADR-0022: provenance disclosure columns.
            ("model", "TEXT"),
            ("provider", "TEXT"),
            ("effort", "TEXT"),
            ("agent_hash", "TEXT"),
            # ADR-0026: project detection for session organization.
            ("project", "TEXT"),
            ("project_source", "TEXT"),
            # ADR-0028: denormalized current status reason (hot read path).
            ("status_reason_code", "TEXT"),
            ("status_reason_summary", "TEXT"),
            ("status_evidence_refs", "JSON"),
            # ADR-0029: resolved artifact contract and teardown result.
            ("artifact_contract_json", "JSON"),
            ("artifact_verification_json", "JSON"),
        ],
        "branches": [
            ("system_msg_id", "TEXT"),
            # ADR-0022: per-branch provenance.
            ("model", "TEXT"),
            ("provider", "TEXT"),
            ("agent_name", "TEXT"),
            ("status", "TEXT"),
            ("started_at", "REAL"),
            ("ended_at", "REAL"),
        ],
        "shows": [
            ("status_source", "TEXT NOT NULL DEFAULT 'unknown'"),
            # ADR-0028.
            ("status_reason_code", "TEXT"),
            ("status_reason_summary", "TEXT"),
            ("status_evidence_refs", "JSON"),
        ],
        "plays": [
            # ADR-0028.
            ("status_reason_code", "TEXT"),
            ("status_reason_summary", "TEXT"),
            ("status_evidence_refs", "JSON"),
        ],
        "invocations": [
            # ADR-0028.
            ("status_reason_code", "TEXT"),
            ("status_reason_summary", "TEXT"),
            ("status_evidence_refs", "JSON"),
        ],
        "teams": [
            # ADR-0028.
            ("status_reason_code", "TEXT"),
            ("status_reason_summary", "TEXT"),
            ("status_evidence_refs", "JSON"),
        ],
        "artifacts": [
            # Nullable in ALTER TABLE because expressions aren't valid
            # column defaults there; insert_artifact() always sets this.
            ("updated_at", "REAL"),
        ],
        "schedules": [],
        "schedule_runs": [
            # ADR-0028: schedule_runs originally had no updated_at.
            # update_status() writes it, so it must exist.
            ("updated_at", "REAL"),
            ("status_reason_code", "TEXT"),
            ("status_reason_summary", "TEXT"),
            ("status_evidence_refs", "JSON"),
        ],
    }

    async def _reconcile_columns(self) -> None:
        for table, columns in self._MIGRATION_COLUMNS.items():
            cur = await self.db.execute(f"PRAGMA table_info({table})")
            rows = await cur.fetchall()
            if not rows:
                # Table doesn't exist yet — schema.sql will CREATE it
                # with the full column list. Nothing to migrate.
                continue
            existing = {row["name"] for row in rows}
            for name, defn in columns:
                if name not in existing:
                    # Identifier safety: name/defn come from the
                    # _MIGRATION_COLUMNS class constant above, never user
                    # input. Table name same. F-string is safe here.
                    await self.db.execute(  # noqa: S608
                        f"ALTER TABLE {table} ADD COLUMN {name} {defn}"
                    )
        await self.db.commit()

    # Substring that appears in the ADR-0017 CHECK clause but not in the
    # ADR-0025 schema (the new schema has no CHECK on sessions.status).
    _LEGACY_SESSION_STATUS_CHECK_MARKER = "'running', 'completed', 'failed', 'aborted'"

    async def _drop_legacy_session_status_check(self) -> None:
        """Rebuild ``sessions`` if it still carries the ADR-0017 CHECK.

        SQLite cannot ``ALTER TABLE ... DROP CONSTRAINT``; the only path
        is the documented "rename → CREATE new → INSERT SELECT → DROP
        old" pattern. This runs at most once per database — subsequent
        opens find no marker and skip the rebuild.
        """
        cur = await self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
        row = await cur.fetchone()
        if row is None or row["sql"] is None:
            # Table doesn't exist yet — schema.sql will CREATE it without
            # the legacy CHECK. Nothing to migrate.
            return
        create_sql: str = row["sql"]
        if self._LEGACY_SESSION_STATUS_CHECK_MARKER not in create_sql:
            return

        # Capture indexes so we can recreate them after the swap. SQLite
        # auto-drops indexes attached to a dropped table.
        idx_cur = await self.db.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='sessions' AND sql IS NOT NULL"
        )
        index_sqls = [r["sql"] for r in await idx_cur.fetchall()]

        # Discover the actual live columns (may include ALTER-added
        # columns not present in any historical CREATE TABLE statement).
        info_cur = await self.db.execute("PRAGMA table_info(sessions)")
        cols = [r["name"] for r in await info_cur.fetchall()]
        col_list = ", ".join(cols)

        await self.db.execute("PRAGMA foreign_keys = OFF")
        try:
            # Use sessions_new pattern to avoid FK rewrites: SQLite's
            # ALTER TABLE RENAME rewrites child-table FK references
            # (branches, plays) to point at the renamed table. Creating
            # a _new table, copying, then swapping avoids that.
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
                  invocation_id   TEXT,
                  model           TEXT,
                  provider        TEXT,
                  effort          TEXT,
                  agent_hash      TEXT,
                  project         TEXT,
                  project_source  TEXT,
                  -- ADR-0028: keep parity with schema.sql so the rebuild
                  -- preserves these columns when migrating from ADR-0017.
                  status_reason_code     TEXT,
                  status_reason_summary  TEXT,
                  status_evidence_refs   JSON,
                  -- ADR-0029: artifact contract snapshot and verification.
                  artifact_contract_json      JSON,
                  artifact_verification_json  JSON
                )
                """
            )
            # Build SELECT with COALESCE for updated_at — legacy DBs
            # may have NULL values from _reconcile_columns additions.
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

    # ── Schema version ─────────────────────────────────────────────────

    async def schema_version(self) -> str | None:
        cur = await self.db.execute("SELECT value FROM schema_meta WHERE key = 'version'")
        row = await cur.fetchone()
        return row["value"] if row else None

    # ── Messages ───────────────────────────────────────────────────────

    _UNKNOWN_TYPE_ID = 0

    async def insert_message(self, msg: dict[str, Any]) -> None:
        # ADR-0009 invariants: messages.content + messages.role are
        # NOT NULL. SQLite enforces that at the column level, but
        # ``INSERT OR IGNORE`` silently swallows constraint violations
        # — without these explicit checks the live-persist hook would
        # log a NULL row as a successful insert, then progression
        # would reference a missing ID.
        if msg.get("content") is None:
            raise ValueError("messages.content is NOT NULL (ADR-0009)")
        role = msg.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"messages.role must be a non-empty string (ADR-0009); got {role!r}")

        lion_class_str = (msg.get("node_metadata") or {}).get("lion_class", "")
        node_metadata = _to_json_column(msg.get("node_metadata"))
        content = _to_json_column(msg["content"])

        type_id = await self._resolve_lion_class(lion_class_str)

        # ON CONFLICT(id) DO UPDATE so a re-fire of the hook for a
        # mutated existing message (e.g. ActionResponse.update via
        # create_action_response) overwrites the stale row instead of
        # silently keeping the old content. Immutable identity stays
        # ``id`` + ``created_at``; mutable content is replaced.
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
        """Get or create a ``message_types`` row for ``lion_class_str``.

        Concurrent live-persist writes can see the same novel class.
        ``INSERT OR IGNORE`` + ``SELECT`` is atomic w.r.t. the SQLite
        connection lock and avoids the ``UNIQUE constraint failed``
        race that the previous SELECT-then-INSERT pattern produced
        when two tasks raced on the same new class.
        """
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
        """Append ``message_id`` to the progression's ordered collection.

        Idempotent for same-(progression_id, message_id) pairs — a re-fire
        of an on_message_added hook for an existing message (the
        ActionResponse-update path mutates an existing object and re-emits
        the hook) must not duplicate the ID in the progression JSON array.
        Uses ``json_insert`` only when the ID is not already present;
        ``EXISTS (json_each)`` lets SQLite do the check in one statement
        without round-tripping the whole collection to Python.
        """
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
        await self.db.execute(
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
                # ADR-0009: caller may preserve ``updated_at`` for lossless
                # import/backfill. Live sessions omit it and get ``now``.
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
                # ADR-0019: initialize last_message_at to session start so
                # a freshly-created session that has produced no messages
                # yet isn't immediately classified stale.
                session.get("last_message_at", session.get("started_at", now)),
                # ADR-0020: optional FK to invocations(id). NULL when the
                # session was spawned standalone (no `li invoke start`).
                session.get("invocation_id"),
                # ADR-0022: resolved provenance — model/provider/effort
                # are the values the runtime actually used, not the user
                # input. agent_hash is the SHA-256 of the agent profile
                # content at invocation time (drift detection).
                session.get("model"),
                session.get("provider"),
                session.get("effort"),
                session.get("agent_hash"),
                # ADR-0026: project detection for session organization.
                session.get("project"),
                session.get("project_source"),
            ),
        )
        # ADR-0020: keep invocations.session_count in sync so list queries
        # don't need a JOIN.
        if session.get("invocation_id"):
            await self.db.execute(
                "UPDATE invocations SET session_count = session_count + 1, "
                "updated_at = ? WHERE id = ?",
                (now, session["invocation_id"]),
            )
        await self.db.commit()
        # ADR-0026: auto-register the detected project so Studio's project
        # list is populated without any explicit studio action.
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
        """Bump ``last_message_at`` (and ``updated_at``) for staleness signal.

        ADR-0019: stored at write time so the runs list and dashboard can
        compute staleness with one indexed read instead of scanning
        messages. ``at`` lets the caller pass the message timestamp for
        precision; callers without one let it default to ``time.time()``.
        """
        ts = at if at is not None else time.time()
        await self.db.execute(
            "UPDATE sessions "
            "SET last_message_at = MAX(COALESCE(last_message_at, 0), ?), "
            "    updated_at      = MAX(COALESCE(updated_at, 0), ?) "
            "WHERE id = ?",
            (ts, ts, session_id),
        )
        await self.db.commit()

    async def update_session(self, session_id: str, **fields: Any) -> None:
        _validate_columns(fields, _SESSION_COLUMNS)
        if "status" in fields:
            _validate_session_status(fields["status"])
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
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [session_id]
        # noqa: S608 — column names allowlisted via _validate_columns
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
        await self.db.execute(
            "UPDATE sessions SET artifact_verification_json = ?, updated_at = ? WHERE id = ?",
            (_to_json_column(verification), time.time(), session_id),
        )
        await self.db.commit()

    # ── Status reason model (ADR-0028) ───────────────────────────────
    # update_status() is the single sanctioned mutation point for any
    # entity's status. It always writes:
    #   1. the entity row (status + denormalized reason columns)
    #   2. an append-only status_transitions row (history)
    # ...in the same SQLite transaction, so denormalized "current
    # reason" and the transition log never drift.
    #
    # The legacy update_session(..., status=...) path remains for
    # callers that haven't migrated yet; new code MUST use
    # update_status() so reason tracking is enforced.

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
    ) -> None:
        """Atomically transition an entity's status and record the reason.

        Args:
            entity_type: Canonical singular ('session', 'show', ...) or a
                registered alias ('run', 'sessions'). Resolved to the
                canonical form before the table lookup.
            entity_id: Primary key of the row being transitioned.
            new_status: The target status string. Per ADR-0025, sessions
                accept only the six-value vocabulary; other entities have
                their own vocabularies (still validated by their own
                table's CHECK).
            reason_code: Must be in :data:`VALID_REASON_CODES`. The
                ``legacy.imported`` sentinel is permitted as the one
                two-segment code.
            reason_summary: Human-readable message; rendered in the
                StatusPill tooltip. Free text; not validated.
            evidence_refs: Optional list of typed references — see
                ADR-0028 Section 3 for the kind vocabulary.
            source: Who initiated the write. One of 'executor', 'agent',
                'admin', 'system'.
            actor: Optional identifier for the actor (session id, user
                name, 'doctor_auto', ...). Stored on the transition row
                for audit.
            metadata: Optional JSON blob for additional context (timing,
                exit code, exception class).

        Raises:
            ValueError: ``reason_code`` not in
                :data:`VALID_REASON_CODES`, or ``entity_type`` not
                recognized.
            LookupError: Entity row not found.
        """
        canonical_type = _validate_entity_type_for_reason(entity_type)
        _validate_reason_code(reason_code)
        table = _reason_entity_table(canonical_type)
        evidence_json = json.dumps(evidence_refs or [])
        metadata_json = json.dumps(metadata) if metadata is not None else None
        now = time.time()

        # Single transaction: status + denormalized reason + transition row.
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            # Lookup previous status. NOT FOUND raises before any write.
            # noqa: S608 — `table` is allowlisted via _reason_entity_table.
            cur = await self.db.execute(
                f"SELECT status FROM {table} WHERE id = ?",  # noqa: S608
                (entity_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise LookupError(f"{canonical_type} {entity_id!r} not found (table={table})")
            previous_status = row["status"] if "status" in row.keys() else None

            # Update the entity row. updated_at is always touched; every
            # status-bearing table has the column per ADR-0028 migration.
            # noqa: S608 — `table` is allowlisted.
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

            # Append to transition history.
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

    # ── Projects (ADR-0026) ────────────────────────────────────────────

    async def register_project(
        self,
        name: str,
        source: str,
        *,
        path: str | None = None,
        github: str | None = None,
    ) -> None:
        """Register or update a project entry (ADR-0026).

        Called during session creation to auto-register detected projects.
        Uses INSERT ... ON CONFLICT to bump last_seen_at on subsequent
        encounters. Prefers config_toml/global_override over git_remote as
        source; never overwrites existing path/github with NULL.
        """
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
        await self.db.commit()

    async def create_project(
        self,
        name: str,
        *,
        github: str | None = None,
        description: str | None = None,
        path: str | None = None,
    ) -> None:
        """Insert a Studio-managed project (source='studio').

        Raises ``aiosqlite.IntegrityError`` on duplicate name.
        """
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
        """List all projects ordered by last_seen_at DESC, with session counts."""
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
        """Return a single project row with session counts, or None."""
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
        """Patch allowed mutable columns on a project row.

        Returns True when a row was updated, False when the project was not
        found. Raises ``ValueError`` for disallowed column names.
        """
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
        """Delete a Studio-managed project.

        Only projects with source='studio' may be deleted; auto-detected
        projects are immutable from the Studio API.  Returns True when a
        row was deleted.
        """
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
                action_playbook, action_project, action_extra_args,
                on_success, on_fail, last_fired_at, next_fire_at,
                missed_fire_policy, overlap_policy, project,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        """Update schedule_run fields; route status through update_status().

        ADR-0028 Phase 2: status writes go through update_status() so
        reason columns + status_transitions are atomic. See
        update_invocation() for the pattern.
        """
        allowed = {"status", "exit_code", "ended_at", "error_detail", "invocation_id"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Invalid schedule_run field(s): {bad}")

        status_value = fields.pop("status", None)
        if status_value is not None:
            if reason_code is None:
                from warnings import warn

                resolved = _default_reason_code_for_entity_status("schedule_run", status_value)
                if resolved is None:
                    raise ValueError(
                        f"update_schedule_run() called with status={status_value!r} but "
                        f"no canonical default reason_code exists for "
                        f"(schedule_run, {status_value!r}). Pass reason_code "
                        f"explicitly from lionagi/state/reasons.py."
                    )
                reason_code = resolved
                warn(
                    f"update_schedule_run({run_id!r}, status={status_value!r}) "
                    "called without reason_code; defaulting to "
                    f"{reason_code!r}. Pass reason_code explicitly "
                    "(ADR-0028 Phase 2 deprecation).",
                    DeprecationWarning,
                    stacklevel=2,
                )
            await self.update_status(
                "schedule_run",
                run_id,
                new_status=status_value,
                reason_code=reason_code,
                reason_summary=reason_summary,
                evidence_refs=evidence_refs,
                source=reason_source,
                actor=reason_actor,
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
        """Insert an invocation row (skill-level orchestration record).

        Called by ``li invoke start``. Required keys: ``id``, ``skill``,
        ``started_at``. Optional: ``plugin``, ``prompt``, ``status``,
        ``node_metadata``.
        """
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
        """Update invocation fields; route status changes through update_status().

        ADR-0028 Phase 2: when ``status`` is among the updated fields, the
        transition goes through :meth:`update_status` so the denormalized
        reason columns and ``status_transitions`` history row are written
        atomically.

        Pass ``reason_code`` (required when status is in fields) plus
        optional ``reason_summary`` / ``evidence_refs`` / ``reason_source``
        / ``reason_actor`` to control the transition record. The
        non-status fields are written by the legacy UPDATE path below
        (separate transaction).

        Backwards compatibility: callers that pass ``status`` without a
        ``reason_code`` get a deprecation warning + a default code
        derived from the status (matching the executor's resolution
        logic for sessions). Remove the fallback in a future release.
        """
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

        status_value = fields.pop("status", None)
        if status_value is not None:
            if reason_code is None:
                from warnings import warn

                resolved = _default_reason_code_for_entity_status("invocation", status_value)
                if resolved is None:
                    raise ValueError(
                        f"update_invocation() called with status={status_value!r} but "
                        f"no canonical default reason_code exists for "
                        f"(invocation, {status_value!r}). Pass reason_code "
                        f"explicitly from lionagi/state/reasons.py."
                    )
                reason_code = resolved
                warn(
                    f"update_invocation({invocation_id!r}, status={status_value!r}) "
                    "called without reason_code; defaulting to "
                    f"{reason_code!r}. Pass reason_code explicitly "
                    "(ADR-0028 Phase 2 deprecation).",
                    DeprecationWarning,
                    stacklevel=2,
                )
            await self.update_status(
                "invocation",
                invocation_id,
                new_status=status_value,
                reason_code=reason_code,
                reason_summary=reason_summary,
                evidence_refs=evidence_refs,
                source=reason_source,
                actor=reason_actor,
            )

        # Remaining (non-status) fields go through the legacy update path
        # in a separate write — update_status() already touched updated_at.
        if fields:
            fields["updated_at"] = time.time()
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [invocation_id]
            # columns validated above; SQL is identifier-only interpolation.
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
        # ADR-0026: surface project/project_source from the most-recently
        # updated child session so Studio can render project chips on the
        # invocations list page.  The subquery picks one row per invocation
        # by latest updated_at; NULL when the invocation has no sessions yet.
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
        """Upsert one structured skill artifact; return its stable id.

        ``content`` is typically ``SkillOutcome.model_dump()``. Either
        ``invocation_id`` or ``session_id`` (or both) should be set so
        the artifact is reachable from a parent; the schema permits NULL
        on both for unattached blobs (rare; the API doesn't expose this).

        INSERT OR REPLACE must not be used — it deletes then re-inserts,
        generating a new id and silently breaking external FK references.
        We SELECT the existing id first and UPDATE in place; a new id is
        only minted when no matching row exists.
        """
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
        """Append one row to the admin event log (NIST SP 800-92 pattern).

        Insert-only; the cleanup job is the only allowed deleter. Returns
        the generated event id so callers can correlate follow-up writes.
        """
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
                # ADR-0022: per-branch resolved provenance.
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
        """Update mutable columns on a branch row.

        Restricted to the allowlist in ``_BRANCH_COLUMNS`` — system
        prompt pointer (``system_msg_id``), display name, user, and
        ``node_metadata`` are the only fields a long-lived branch
        should change after creation. The branch identity (id,
        session_id, progression_id, created_at) is immutable.
        """
        _validate_columns(fields, _BRANCH_COLUMNS)
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [branch_id]
        # noqa: S608 — column names allowlisted via _validate_columns
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
        """Backfill ``branches.progression_id`` for a legacy row that has NULL.

        Pre-PR DBs may have ``branches.progression_id IS NULL``. The live
        hook then calls ``append_to_progression(None, msg_id)`` which is
        an ``UPDATE progressions ... WHERE id = NULL`` — a silent no-op
        that loses branch history. This helper repairs the row by
        pointing it at a freshly-created progression.

        Returns the EFFECTIVE progression id stored on the row: if we
        won the race, it's ``new_progression_id``; if another concurrent
        repair landed first, it's whatever the row now points to. The
        caller MUST use the returned id rather than its locally-generated
        one, or it will append to an orphan progression while
        ``branches.progression_id`` points elsewhere.

        Returns None only if the branch row itself does not exist.

        Bypasses the ``_BRANCH_COLUMNS`` allowlist on purpose: normal
        runtime must NOT mutate ``progression_id`` (the branch identity
        includes its progression), but a one-shot migration from NULL
        is the explicit exception.
        """
        # Atomic: the conditional UPDATE either lands our id or no-ops
        # if a concurrent writer already filled the column. Then read
        # back the actual stored id so the caller cannot diverge from
        # the row.
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
        """Backfill ``sessions.progression_id`` for a legacy row that has NULL.

        Parallel to ``repair_branch_progression``: returns the effective
        progression id stored on the row after the conditional UPDATE,
        or None if the session row does not exist. Caller MUST adopt
        the returned id.
        """
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
        # noqa: S608 — `placeholders` is a fixed-shape "?,?,?" string built
        # from message_ids length; values flow through parameter binding.
        cur = await self.db.execute(
            f"""SELECT m.*, mt.lion_class AS lion_class_str
                FROM messages m
                LEFT JOIN message_types mt ON m.lion_class = mt.type_id
                WHERE m.id IN ({placeholders})""",  # noqa: S608
            message_ids,
        )
        rows = await cur.fetchall()
        # Restore progression order (SQL IN doesn't guarantee order)
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
        """Update show fields; route status changes through update_status().

        ADR-0028 Phase 2: status writes go through StateDB.update_status()
        so reason columns and status_transitions are written atomically.
        Backwards-compatible deprecation shim mirrors update_invocation().
        """
        _validate_columns(fields, _SHOW_COLUMNS)
        if "status" in fields:
            _validate_enum(
                "show status",
                fields["status"],
                _SHOW_STATUSES,
                adr="ADR-0011",
                nullable=False,
            )

        status_value = fields.pop("status", None)
        if status_value is not None:
            if reason_code is None:
                from warnings import warn

                resolved = _default_reason_code_for_entity_status("show", status_value)
                if resolved is None:
                    raise ValueError(
                        f"update_show() called with status={status_value!r} but "
                        f"no canonical default reason_code exists for "
                        f"(show, {status_value!r}). Pass reason_code "
                        f"explicitly from lionagi/state/reasons.py."
                    )
                reason_code = resolved
                warn(
                    f"update_show({show_id!r}, status={status_value!r}) "
                    "called without reason_code; defaulting to "
                    f"{reason_code!r}. Pass reason_code explicitly "
                    "(ADR-0028 Phase 2 deprecation).",
                    DeprecationWarning,
                    stacklevel=2,
                )
            await self.update_status(
                "show",
                show_id,
                new_status=status_value,
                reason_code=reason_code,
                reason_summary=reason_summary,
                evidence_refs=evidence_refs,
                source=reason_source,
                actor=reason_actor,
            )

        if fields:
            fields["updated_at"] = time.time()
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [show_id]
            # noqa: S608 — column names allowlisted via _validate_columns
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
                json.dumps(play.get("depends_on", [])),
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
        """Update play fields; route status changes through update_status().

        ADR-0028 Phase 2: see update_invocation() / update_show() for
        the same routing pattern.
        """
        _validate_columns(fields, _PLAY_COLUMNS)
        if "status" in fields:
            _validate_enum(
                "play status",
                fields["status"],
                _PLAY_STATUSES,
                adr="ADR-0011",
                nullable=False,
            )

        status_value = fields.pop("status", None)
        if status_value is not None:
            if reason_code is None:
                from warnings import warn

                resolved = _default_reason_code_for_entity_status("play", status_value)
                if resolved is None:
                    raise ValueError(
                        f"update_play() called with status={status_value!r} but "
                        f"no canonical default reason_code exists for "
                        f"(play, {status_value!r}). Pass reason_code "
                        f"explicitly from lionagi/state/reasons.py."
                    )
                reason_code = resolved
                warn(
                    f"update_play({play_id!r}, status={status_value!r}) "
                    "called without reason_code; defaulting to "
                    f"{reason_code!r}. Pass reason_code explicitly "
                    "(ADR-0028 Phase 2 deprecation).",
                    DeprecationWarning,
                    stacklevel=2,
                )
            await self.update_status(
                "play",
                play_id,
                new_status=status_value,
                reason_code=reason_code,
                reason_summary=reason_summary,
                evidence_refs=evidence_refs,
                source=reason_source,
                actor=reason_actor,
            )

        if fields:
            fields["updated_at"] = time.time()
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [play_id]
            # noqa: S608 — column names allowlisted via _validate_columns
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
        # ADR-0016: Studio's write path is limited to agent + playbook
        # definitions. Skills and third-party plugin components are
        # source-controlled, read-only, and must not be versioned through
        # this API.
        if kind not in _DEFINITION_KINDS:
            raise ValueError(
                f"Invalid definition kind {kind!r}; "
                f"ADR-0016 editable set is {sorted(_DEFINITION_KINDS)}"
            )

        # Concurrent saves for the same (kind, name) need a serialization
        # point: ``SELECT MAX(version)`` + ``INSERT`` is not atomic and
        # two writers can pick the same next version, with all but one
        # losing on the ``UNIQUE(kind, name, version)`` index. We
        # serialize at the Python level with a per-(kind, name)
        # asyncio.Lock — explicit BEGIN IMMEDIATE would conflict with
        # aiosqlite's implicit-transaction default. Bounded retry on
        # IntegrityError catches the residual case where a separate
        # ``StateDB`` instance (different connection) races us; the
        # Lock alone handles intra-instance concurrency.
        lock_key = (kind, name)
        lock = self._definition_locks.setdefault(lock_key, asyncio.Lock())
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
        ):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d
