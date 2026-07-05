# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, bindparam, event, inspect, text
from sqlalchemy.exc import IntegrityError

from lionagi._paths import LIONAGI_HOME
from lionagi.config import settings
from lionagi.ln import json_dumps as _json_dumps
from lionagi.ln.concurrency import Lock
from lionagi.state.engine import dialect_of, make_engine, normalize_state_db_url
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
from lionagi.state.schema_meta import metadata
from lionagi.state.schema_migrations import MIGRATION_COLUMNS as _MIGRATION_COLUMNS

_RUN_DEFAULTS: dict[str, str] = {
    "running": _RunReasons.STARTED_OK,
    "completed": _RunReasons.COMPLETED_OK,
    "completed_empty": _RunReasons.COMPLETED_EMPTY_NO_EVIDENCE,
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
        # ADR-0029 documents artifact_contract_json as fixed at session
        # creation for the single-agent case, where the full contract
        # (playbook + agent profile) is already known at create_session time.
        # DAG flows break that assumption: which role runs which leg is only
        # known once planning finishes, which happens after create_session
        # (see _build_dag in cli/orchestrate/flow.py). This column is
        # allowlisted here so that ONE extension write is possible — folding
        # resolved per-leg role artifact_defaults into the flow-wide
        # contract, done once at DAG-build time, strictly before any leg
        # starts executing. No writer may touch it after that point; the
        # anti-drift intent of ADR-0029 (no changes once work is underway)
        # still holds, it is just anchored at "DAG built" instead of
        # "session created" for this call path.
        "artifact_contract_json",
    }
)

_INVOCATION_STATUSES = frozenset(
    {
        "running",
        "completed",
        # Completion-trust gate: flow/scheduler aggregation can settle an
        # invocation on this status when a child session produced no commits
        # ahead of base, no artifacts, and no assistant output.
        "completed_empty",
        "failed",
        "timed_out",
        "aborted",
        "cancelled",
    }
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
    {
        "running",
        "completed",
        # Completion-trust gate: loop exited clean but no commits ahead of base
        # and no artifacts were produced — distinct from "completed" so
        # operators/monitors can tell "ran and produced nothing" apart from a
        # verified completion.
        "completed_empty",
        "failed",
        "timed_out",
        "aborted",
        "cancelled",
    }
)
SESSION_TERMINAL_STATUSES = frozenset(
    {"completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"}
)
# Admin cannot mark completed/completed_empty/timed_out — those are system-determined.
ADMIN_TRANSITION_TARGETS = frozenset({"failed", "aborted", "cancelled"})

_SESSION_STATUSES = VALID_SESSION_STATUSES

# ── ADR-0094 terminal-status vocabulary ────────────────────────────────
# Terminal-state definitions live here, with the record schema, rather than
# in any one CLI surface — update_status() enforces them uniformly for
# every entity_type at the single write path; `li wait` reads the same
# tables to build its per-kind terminal predicate.
INVOCATION_TERMINAL_STATUSES = SESSION_TERMINAL_STATUSES  # ADR-0025 vocabulary is shared
SCHEDULE_RUN_TERMINAL_STATUSES = frozenset({"completed", "failed", "skipped", "cancelled"})
SHOW_TERMINAL_STATUSES = frozenset({"completed", "aborted"})
# Still-in-flight play statuses — the schema layer owns this vocabulary
# (kill.py imports it rather than defining its own copy); everything else
# in PLAY_TERMINAL_STATUSES below is terminal.
PLAY_ACTIVE_STATUSES = frozenset(
    {"pending", "prepared", "running", "running_complete", "gated", "redoing"}
)
PLAY_TERMINAL_STATUSES = frozenset(
    {"merged", "escalated", "gate_failed", "blocked", "aborted_after_finish"}
)
TEAM_TERMINAL_STATUSES = frozenset({"archived"})

TERMINAL_STATUSES_BY_ENTITY_TYPE: dict[str, frozenset[str]] = {
    "session": SESSION_TERMINAL_STATUSES,
    "invocation": INVOCATION_TERMINAL_STATUSES,
    "schedule_run": SCHEDULE_RUN_TERMINAL_STATUSES,
    "show": SHOW_TERMINAL_STATUSES,
    "play": PLAY_TERMINAL_STATUSES,
    "team": TEAM_TERMINAL_STATUSES,
}

# ── ADR-0094 status vocabulary (valid, not just terminal) ──────────────
# update_status() rejects any new_status outside its entity_type's set here —
# the terminal-overwrite floor above stops a terminal record from moving;
# this stops ANY record (terminal or not) from being written to a status
# that was never declared for its entity type.
VALID_STATUSES_BY_ENTITY_TYPE: dict[str, frozenset[str]] = {
    "session": VALID_SESSION_STATUSES,
    "invocation": VALID_SESSION_STATUSES,  # shared vocabulary
    "schedule_run": SCHEDULE_RUN_TERMINAL_STATUSES | frozenset({"pending", "running"}),
    "show": SHOW_TERMINAL_STATUSES | frozenset({"pending", "running", "active"}),
    "play": PLAY_ACTIVE_STATUSES | PLAY_TERMINAL_STATUSES,
    "team": TEAM_TERMINAL_STATUSES | frozenset({"active"}),
}


def can_transition(current: str | None, target: str) -> bool:
    """Return True iff a session may move from *current* to *target*."""
    if current != "running":
        return False
    return target in SESSION_TERMINAL_STATUSES


class TransitionRejectedError(RuntimeError):
    """Raised by update_status() when a write would move an entity out of a
    terminal status without an explicit, justified override (ADR-0094)."""

    def __init__(
        self,
        entity_type: str,
        entity_id: str,
        previous_status: str | None,
        attempted_status: str,
    ) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.previous_status = previous_status
        self.attempted_status = attempted_status
        super().__init__(
            f"transition rejected: {entity_type} {entity_id!r} is terminal "
            f"at {previous_status!r}; refusing to write {attempted_status!r} "
            "without override=True (ADR-0094)"
        )


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


def _install_begin_immediate(sync_engine) -> None:
    @event.listens_for(sync_engine, "connect")
    def _on_connect(dbapi_conn, _rec):
        dbapi_conn.isolation_level = None  # driver autocommit; SA "begin" emits ours

    @event.listens_for(sync_engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


class StateDB:
    """Async SQLAlchemy state layer for sessions, branches, messages, and progressions."""

    def __init__(self, path: str | Path | None = None, *, url: str | None = None):
        raw = url if url is not None else path
        if raw is None:
            raw = settings.LIONAGI_STATE_DB_URL  # may be None
        if raw is None:
            raw = DEFAULT_DB_PATH  # module-level; tests can monkeypatch db_mod.DEFAULT_DB_PATH
        self.url = normalize_state_db_url(raw)
        self.dialect = dialect_of(self.url)  # "sqlite" | "postgresql"
        self._engine = None
        # Per-(kind, name) lock to serialize version increment for save_definition.
        self._definition_locks: dict[tuple[str, str], Lock] = {}
        # Connection-wide write lock: every mutating method that can share the
        # live-persistence connection must hold this lock during its write window.
        #
        # For SQLite: prevents concurrent coroutines from racing BEGIN IMMEDIATE
        # on the same AsyncEngine (which shares a single connection in the pool).
        # For PostgreSQL: _tx() uses engine.begin() which handles isolation
        # natively, so the lock is a no-op for PG paths (they skip it via dialect
        # check in _tx()), but it still serializes Python-side CAS in update_status.
        self._write_lock: Lock = Lock()

    # ── backward-compat path property ─────────────────────────────────

    @property
    def path(self) -> Path | None:
        if self.dialect == "sqlite":
            # sqlite+aiosqlite:///abs/path  or  sqlite+aiosqlite:///:memory:
            suffix = self.url.split(":///", 1)[1] if ":///" in self.url else None
            if suffix and suffix != ":memory:":
                return Path(suffix)
            return Path(":memory:") if suffix == ":memory:" else None
        return None

    # ── Connection lifecycle ───────────────────────────────────────────

    async def open(self) -> None:
        if self._engine is not None:
            return
        if self.dialect == "sqlite":
            p = self.path
            if p is not None and str(p) != ":memory:":
                p.parent.mkdir(parents=True, exist_ok=True)
        self._engine = make_engine(self.url)
        if self.dialect == "sqlite":
            _install_begin_immediate(self._engine.sync_engine)
        await self._apply_schema()

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def __aenter__(self) -> StateDB:
        await self.open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ── Internal connection helpers ────────────────────────────────────

    @asynccontextmanager
    async def _read(self):
        async with self._engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            yield conn

    @asynccontextmanager
    async def _tx(self):
        if self.dialect == "sqlite":
            async with self._write_lock:
                async with self._engine.begin() as conn:
                    yield conn
        else:
            async with self._engine.begin() as conn:
                yield conn

    # ── Public query surface (portable across both dialects) ───────────
    # Replaces direct `db.db.execute(...)` access from CLI/studio consumers.
    # Accepts the legacy qmark (?) form with a sequence of params, or named
    # (:name) SQL with a dict; SQLAlchemy translates the paramstyle per dialect.
    # Rows are returned as plain dicts (JSON columns left as stored — str on
    # sqlite, native on pg — so callers keep their own decode, guarded by
    # isinstance(str) for pg). For multi-statement atomic work use transaction().

    @staticmethod
    def _to_named(sql: str, params: Any) -> tuple[str, dict[str, Any]]:
        if params is None:
            return sql, {}
        if isinstance(params, dict):
            return sql, params
        seq = list(params)
        out: list[str] = []
        i = 0
        in_str = False  # inside a '...' SQL string literal — leave ? untranslated
        k = 0
        n = len(sql)
        while k < n:
            ch = sql[k]
            if in_str:
                out.append(ch)
                if ch == "'":
                    if k + 1 < n and sql[k + 1] == "'":  # '' escape — stays in literal
                        out.append("'")
                        k += 2
                        continue
                    in_str = False
            elif ch == "'":
                in_str = True
                out.append(ch)
            elif ch == "?":
                out.append(f":p{i}")
                i += 1
            else:
                out.append(ch)
            k += 1
        if i != len(seq):
            raise ValueError(f"param count mismatch: {i} placeholders, {len(seq)} params")
        return "".join(out), {f"p{j}": v for j, v in enumerate(seq)}

    async def fetch_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        sql, p = self._to_named(sql, params)
        async with self._read() as conn:
            result = await conn.execute(text(sql), p)
            return [dict(r) for r in result.mappings().all()]

    async def fetch_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        sql, p = self._to_named(sql, params)
        async with self._read() as conn:
            result = await conn.execute(text(sql), p)
            row = result.mappings().first()
            return dict(row) if row is not None else None

    async def execute(self, sql: str, params: Any = None) -> None:
        sql, p = self._to_named(sql, params)
        async with self._tx() as conn:
            await conn.execute(text(sql), p)

    def transaction(self):
        return self._tx()

    async def _raw_sqlite_exec(self, sql: str, *, fetch: bool = False):
        # Run maintenance SQL on sqlite's raw driver connection for true
        # autocommit. SQLAlchemy's AUTOCOMMIT option does not clear the aiosqlite
        # adapter's implicit transaction, which blocks VACUUM and wal_checkpoint
        # ("cannot VACUUM"/"database table is locked").
        async with self._engine.connect() as conn:
            driver = (await conn.get_raw_connection()).driver_connection
            cur = await driver.execute(sql)
            row = await cur.fetchone() if fetch else None
            await driver.commit()
            return row

    async def vacuum(self) -> None:
        if self.dialect == "sqlite":
            await self._raw_sqlite_exec("VACUUM")
        else:
            async with self._read() as conn:
                await conn.execute(text("VACUUM"))

    async def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int] | None:
        # WAL checkpoint is sqlite-only maintenance; like VACUUM it must bypass
        # the adapter's implicit transaction. Returns (busy, log_pages,
        # checkpointed); None on postgresql (no WAL checkpoint concept).
        if self.dialect != "sqlite":
            return None
        mode = mode.upper()
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError(f"invalid wal_checkpoint mode: {mode!r}")
        row = await self._raw_sqlite_exec(f"PRAGMA wal_checkpoint({mode})", fetch=True)
        return tuple(row) if row is not None else None

    # ── Schema management ──────────────────────────────────────────────

    async def _apply_schema(self) -> None:
        await self._reconcile_columns()
        if self.dialect == "sqlite":
            await self._drop_legacy_session_status_check()
            # existing DBs created before flow_yaml was added carry a
            # 4-value CHECK on schedules.action_kind that omits 'flow_yaml'.
            await self._drop_legacy_action_kind_check()
            # existing DBs created before the completion-trust gate carry a
            # 6-value CHECK on invocations.status that omits 'completed_empty'.
            await self._drop_legacy_invocations_status_check()
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            # Seed immutable reference rows; ON CONFLICT DO NOTHING is safe to
            # re-run on every open() because the rows are identity-stable.
            await conn.execute(
                text(
                    "INSERT INTO schema_meta (key, value) VALUES ('version', '1') "
                    "ON CONFLICT (key) DO NOTHING"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO schema_meta (key, value) VALUES ('created_at', :created_at) "
                    "ON CONFLICT (key) DO NOTHING"
                ),
                {"created_at": str(int(time.time()))},
            )
            await conn.execute(
                text(
                    "INSERT INTO message_types (type_id, lion_class) VALUES "
                    "(0, '__unknown__'), "
                    "(1, 'lionagi.protocols.messages.system.System'), "
                    "(2, 'lionagi.protocols.messages.instruction.Instruction'), "
                    "(3, 'lionagi.protocols.messages.assistant_response.AssistantResponse'), "
                    "(4, 'lionagi.protocols.messages.action_request.ActionRequest'), "
                    "(5, 'lionagi.protocols.messages.action_response.ActionResponse') "
                    "ON CONFLICT (type_id) DO NOTHING"
                )
            )

    _MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = _MIGRATION_COLUMNS

    async def _reconcile_columns(self) -> None:
        for table, columns in self._MIGRATION_COLUMNS.items():
            try:
                async with self._engine.connect() as conn:
                    has_it = await conn.run_sync(lambda c, t=table: inspect(c).has_table(t))
                    if not has_it:
                        continue
                    existing = await conn.run_sync(
                        lambda c, t=table: [col["name"] for col in inspect(c).get_columns(t)]
                    )
            except Exception:  # noqa: BLE001, S112
                continue
            for name, defn in columns:
                if name not in existing:
                    async with self._engine.begin() as conn:
                        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {defn}"))

    _LEGACY_SESSION_STATUS_CHECK_MARKER = "'running', 'completed', 'failed', 'aborted'"

    async def _drop_legacy_session_status_check(self) -> None:
        """Rebuild sessions table if it carries the legacy 4-value CHECK constraint."""
        if self.dialect != "sqlite":
            return
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'")
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["sql"] is None:
            return
        create_sql: str = row["sql"]
        if self._LEGACY_SESSION_STATUS_CHECK_MARKER not in create_sql:
            return

        async with self._engine.connect() as conn:
            index_rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='index' AND tbl_name='sessions' AND sql IS NOT NULL"
                        )
                    )
                )
                .mappings()
                .all()
            )
            index_sqls = [r["sql"] for r in index_rows]

            cols_rows = (await conn.execute(text("PRAGMA table_info(sessions)"))).mappings().all()
            cols = [r["name"] for r in cols_rows]
        col_list = ", ".join(cols)

        async with self._engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys = OFF"))
            try:
                # Create without FK references: the referenced tables (messages,
                # invocations) may not exist yet in a minimal legacy DB.
                # metadata.create_all() runs AFTER this rebuild and will not
                # re-create sessions (table already exists after rename).
                # FK enforcement relies on the PRAGMA which is already set up
                # by make_engine and applies to all DML after schema init.
                await conn.execute(
                    text(
                        """
                        CREATE TABLE sessions_new (
                          id              TEXT    PRIMARY KEY,
                          created_at      REAL    NOT NULL,
                          node_metadata   JSON,
                          name            TEXT,
                          user            TEXT,
                          progression_id  TEXT    NOT NULL,
                          first_msg_id    TEXT,
                          last_msg_id     TEXT,
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
                insert_sql = (
                    f"INSERT INTO sessions_new ({col_list}) SELECT {select_list} FROM sessions"  # noqa: S608
                )
                await conn.execute(text(insert_sql))
                await conn.execute(text("DROP TABLE sessions"))
                await conn.execute(text("ALTER TABLE sessions_new RENAME TO sessions"))
                for idx_sql in index_sqls:
                    await conn.execute(text(idx_sql))
            finally:
                await conn.execute(text("PRAGMA foreign_keys = ON"))

    # Substring present only in the post-#1174 schedules CREATE SQL;
    # its absence indicates a legacy DB whose action_kind CHECK needs rebuilding.
    _LEGACY_SCHEDULES_FLOW_YAML_MARKER = "'flow_yaml'"

    async def _drop_legacy_action_kind_check(self) -> None:
        """Rebuild ``schedules`` if it still carries the pre-#1174 action_kind CHECK.

        The old CHECK omits ``'flow_yaml'``; SQLite cannot drop a constraint via
        ALTER TABLE, so we use the rename → CREATE new → INSERT SELECT → DROP
        old pattern (same as ``_drop_legacy_session_status_check``).
        """
        if self.dialect != "sqlite":
            return
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedules'"
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["sql"] is None:
            return
        create_sql: str = row["sql"]
        if self._LEGACY_SCHEDULES_FLOW_YAML_MARKER in create_sql:
            # Table was already created / rebuilt with flow_yaml in the CHECK.
            return

        async with self._engine.connect() as conn:
            index_rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='index' AND tbl_name='schedules' AND sql IS NOT NULL"
                        )
                    )
                )
                .mappings()
                .all()
            )
            index_sqls = [r["sql"] for r in index_rows]

            cols_rows = (await conn.execute(text("PRAGMA table_info(schedules)"))).mappings().all()
            cols = [r["name"] for r in cols_rows]
        col_list = ", ".join(cols)

        async with self._engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys = OFF"))
            try:
                await conn.execute(
                    text(
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
                          max_runs            INTEGER,
                          project             TEXT,
                          created_at          REAL    NOT NULL,
                          updated_at          REAL    NOT NULL
                        )
                        """
                    )
                )
                insert_sql = (
                    f"INSERT INTO schedules_new ({col_list}) SELECT {col_list} FROM schedules"  # noqa: S608
                )
                await conn.execute(text(insert_sql))
                await conn.execute(text("DROP TABLE schedules"))
                await conn.execute(text("ALTER TABLE schedules_new RENAME TO schedules"))
                for idx_sql in index_sqls:
                    await conn.execute(text(idx_sql))
            finally:
                await conn.execute(text("PRAGMA foreign_keys = ON"))

    # Substring present only in the post-completion-trust-gate invocations
    # CREATE SQL; its absence indicates a legacy DB whose status CHECK needs
    # rebuilding to admit 'completed_empty'.
    _LEGACY_INVOCATIONS_STATUS_MARKER = "'completed_empty'"

    async def _drop_legacy_invocations_status_check(self) -> None:
        """Rebuild ``invocations`` if its status CHECK still omits 'completed_empty'.

        SQLite cannot drop a constraint via ALTER TABLE, so we use the same
        rename → CREATE new → INSERT SELECT → DROP old pattern as
        ``_drop_legacy_session_status_check`` / ``_drop_legacy_action_kind_check``.
        """
        if self.dialect != "sqlite":
            return
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name='invocations'"
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["sql"] is None:
            return
        create_sql: str = row["sql"]
        if self._LEGACY_INVOCATIONS_STATUS_MARKER in create_sql:
            return

        async with self._engine.connect() as conn:
            index_rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='index' AND tbl_name='invocations' AND sql IS NOT NULL"
                        )
                    )
                )
                .mappings()
                .all()
            )
            index_sqls = [r["sql"] for r in index_rows]

            cols_rows = (
                (await conn.execute(text("PRAGMA table_info(invocations)"))).mappings().all()
            )
            cols = [r["name"] for r in cols_rows]
        col_list = ", ".join(cols)

        # `invocations` is an FK target (sessions.invocation_id,
        # schedule_runs.invocation_id, artifacts.invocation_id): dropping it
        # while `PRAGMA foreign_keys` is enforced raises a FOREIGN KEY
        # constraint failure even with real rows safely copied into the new
        # table first. `engine.begin()` opens its transaction before our first
        # statement runs, and SQLite treats `PRAGMA foreign_keys` as a no-op
        # inside a pending transaction — so toggling it through a normal
        # SQLAlchemy connection never actually takes effect. Go through the
        # raw driver connection instead (same technique as `_raw_sqlite_exec`)
        # so the pragma flip is real autocommit, not swallowed by an open txn.
        async with self._engine.connect() as conn:
            driver = (await conn.get_raw_connection()).driver_connection
            await driver.execute("PRAGMA foreign_keys = OFF")
            try:
                await driver.execute(
                    """
                    CREATE TABLE invocations_new (
                      id              TEXT    PRIMARY KEY,
                      skill           TEXT    NOT NULL,
                      plugin          TEXT,
                      prompt          TEXT,
                      started_at      REAL    NOT NULL,
                      ended_at        REAL,
                      status          TEXT    NOT NULL DEFAULT 'running'
                                      CHECK(status IN ('running', 'completed',
                                            'completed_empty', 'failed',
                                            'timed_out', 'aborted', 'cancelled')),
                      session_count   INTEGER NOT NULL DEFAULT 0,
                      created_at      REAL    NOT NULL,
                      updated_at      REAL    NOT NULL,
                      node_metadata   JSON,
                      status_reason_code     TEXT,
                      status_reason_summary  TEXT,
                      status_evidence_refs   JSON
                    )
                    """
                )
                insert_sql = (
                    f"INSERT INTO invocations_new ({col_list}) "  # noqa: S608
                    f"SELECT {col_list} FROM invocations"
                )
                await driver.execute(insert_sql)
                await driver.execute("DROP TABLE invocations")
                await driver.execute("ALTER TABLE invocations_new RENAME TO invocations")
                for idx_sql in index_sqls:
                    await driver.execute(idx_sql)
                await driver.commit()
            finally:
                await driver.execute("PRAGMA foreign_keys = ON")
                await driver.commit()

    # ── Schema version ─────────────────────────────────────────────────

    async def schema_version(self) -> str | None:
        async with self._read() as conn:
            row = (
                (await conn.execute(text("SELECT value FROM schema_meta WHERE key = 'version'")))
                .mappings()
                .first()
            )
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

        # Serialise the full message write (including the message_types upsert
        # in _resolve_lion_class) behind _write_lock so this path cannot
        # interleave with insert_session_signal's or update_status's _tx() on SQLite.
        async with self._tx() as conn:
            type_id = await self._resolve_lion_class_in_tx(conn, lion_class_str)

            # ON CONFLICT(id) DO UPDATE so re-emitted hooks overwrite stale content.
            await conn.execute(
                text(
                    """INSERT INTO messages (id, created_at, node_metadata, content,
                       embedding, sender, recipient, channel, role, lion_class)
                       VALUES (:id, :created_at, :node_metadata, :content,
                               :embedding, :sender, :recipient, :channel, :role, :lion_class)
                       ON CONFLICT(id) DO UPDATE SET
                         node_metadata = excluded.node_metadata,
                         content       = excluded.content,
                         embedding     = excluded.embedding,
                         sender        = excluded.sender,
                         recipient     = excluded.recipient,
                         channel       = excluded.channel,
                         role          = excluded.role,
                         lion_class    = excluded.lion_class"""
                ).bindparams(
                    bindparam("node_metadata", type_=JSON),
                    bindparam("content", type_=JSON),
                ),
                {
                    "id": msg["id"],
                    "created_at": msg["created_at"],
                    "node_metadata": msg.get("node_metadata"),
                    "content": msg["content"],
                    "embedding": msg.get("embedding"),
                    "sender": msg.get("sender"),
                    "recipient": msg.get("recipient"),
                    "channel": msg.get("channel"),
                    "role": msg["role"],
                    "lion_class": type_id,
                },
            )

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            """SELECT m.*, mt.lion_class AS lion_class_str
                           FROM messages m
                           LEFT JOIN message_types mt ON m.lion_class = mt.type_id
                           WHERE m.id = :id"""
                        ),
                        {"id": message_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row else None

    async def _resolve_lion_class(self, lion_class_str: str) -> int:
        """Get or create a message_types row; race-safe via ON CONFLICT DO NOTHING."""
        if not lion_class_str:
            return self._UNKNOWN_TYPE_ID
        async with self._tx() as conn:
            return await self._resolve_lion_class_in_tx(conn, lion_class_str)

    async def _resolve_lion_class_in_tx(self, conn, lion_class_str: str) -> int:
        """Get or create a message_types row within an existing transaction."""
        if not lion_class_str:
            return self._UNKNOWN_TYPE_ID
        await conn.execute(
            text(
                "INSERT INTO message_types (lion_class) VALUES (:lc) "
                "ON CONFLICT (lion_class) DO NOTHING"
            ),
            {"lc": lion_class_str},
        )
        row = (
            (
                await conn.execute(
                    text("SELECT type_id FROM message_types WHERE lion_class = :lc"),
                    {"lc": lion_class_str},
                )
            )
            .mappings()
            .first()
        )
        return row["type_id"]

    # ── Progressions ───────────────────────────────────────────────────

    async def create_progression(
        self, progression_id: str, collection: list[str] | None = None
    ) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO progressions (id, created_at, collection) VALUES (:id, :ca, :col) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": progression_id, "ca": time.time(), "col": json.dumps(collection or [])},
            )

    async def get_progression(self, progression_id: str) -> list[str]:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT collection FROM progressions WHERE id = :id"),
                        {"id": progression_id},
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            return []
        val = row["collection"]
        # collection is a TEXT column holding a JSON array string; both drivers
        # return it as str, so decode here.
        if isinstance(val, str):
            val = json.loads(val)
        return val

    @staticmethod
    def _progression_append_sql(dialect: str) -> str:
        if dialect == "sqlite":
            return (
                "UPDATE progressions "
                "SET collection = json_insert(collection,'$[#]',:v) "
                "WHERE id=:id AND NOT EXISTS "
                "(SELECT 1 FROM json_each(progressions.collection) WHERE value=:v)"
            )
        # collection is a TEXT column; cast to jsonb at use-site to append.
        # CAST(:v AS text) not :v::text — text() does not bind a param immediately
        # followed by '::', so the postgres-cast form would leave :v unbound.
        return (
            "UPDATE progressions "
            "SET collection = (collection::jsonb || to_jsonb(CAST(:v AS text)))::text "
            "WHERE id=:id AND NOT EXISTS "
            "(SELECT 1 FROM jsonb_array_elements_text(collection::jsonb) WHERE value=:v)"
        )

    async def append_to_progression(self, progression_id: str, message_id: str) -> None:
        """Idempotent append of message_id to the progression JSON array."""
        async with self._tx() as conn:
            await conn.execute(
                text(self._progression_append_sql(self.dialect)),
                {"v": message_id, "id": progression_id},
            )

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
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """INSERT INTO sessions (id, created_at, node_metadata, name, "user",
                       progression_id, first_msg_id, last_msg_id, updated_at,
                       playbook_name, agent_name, invocation_kind, show_topic,
                       show_play_name, artifacts_path, artifact_contract_json,
                       artifact_verification_json, source_kind,
                       status, started_at, ended_at, last_message_at, invocation_id,
                       model, provider, effort, agent_hash,
                       project, project_source)
                       VALUES (:id, :created_at, :node_metadata, :name, :user,
                               :progression_id, :first_msg_id, :last_msg_id, :updated_at,
                               :playbook_name, :agent_name, :invocation_kind, :show_topic,
                               :show_play_name, :artifacts_path, :artifact_contract_json,
                               :artifact_verification_json, :source_kind,
                               :status, :started_at, :ended_at, :last_message_at, :invocation_id,
                               :model, :provider, :effort, :agent_hash,
                               :project, :project_source)
                       ON CONFLICT (id) DO NOTHING"""
                ).bindparams(
                    bindparam("node_metadata", type_=JSON),
                    bindparam("artifact_contract_json", type_=JSON),
                    bindparam("artifact_verification_json", type_=JSON),
                ),
                {
                    "id": session["id"],
                    "created_at": session.get("created_at", now),
                    "node_metadata": session.get("node_metadata"),
                    "name": session.get("name"),
                    "user": session.get("user"),
                    "progression_id": session["progression_id"],
                    "first_msg_id": session.get("first_msg_id"),
                    "last_msg_id": session.get("last_msg_id"),
                    "updated_at": session.get("updated_at", now),
                    "playbook_name": session.get("playbook_name"),
                    "agent_name": session.get("agent_name"),
                    "invocation_kind": session.get("invocation_kind"),
                    "show_topic": session.get("show_topic"),
                    "show_play_name": session.get("show_play_name"),
                    "artifacts_path": session.get("artifacts_path"),
                    "artifact_contract_json": session.get("artifact_contract_json"),
                    "artifact_verification_json": session.get("artifact_verification_json"),
                    "source_kind": session.get("source_kind", "live"),
                    "status": session.get("status"),
                    "started_at": session.get("started_at"),
                    "ended_at": session.get("ended_at"),
                    "last_message_at": session.get(
                        "last_message_at", session.get("started_at", now)
                    ),
                    "invocation_id": session.get("invocation_id"),
                    "model": session.get("model"),
                    "provider": session.get("provider"),
                    "effort": session.get("effort"),
                    "agent_hash": session.get("agent_hash"),
                    "project": session.get("project"),
                    "project_source": session.get("project_source"),
                },
            )
            # Only increment session_count when INSERT actually created a row.
            if session.get("invocation_id") and result.rowcount:
                await conn.execute(
                    text(
                        "UPDATE invocations SET session_count = session_count + 1, "
                        "updated_at = :now WHERE id = :inv_id"
                    ),
                    {"now": now, "inv_id": session["invocation_id"]},
                )

        project_name = session.get("project")
        if project_name:
            await self.register_project(
                project_name,
                session.get("project_source") or "git_remote",
            )

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM sessions WHERE id = :id"),
                        {"id": session_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _touch_activity_sql(dialect: str) -> str:
        # SQLite MAX(a,b) is a scalar greatest; Postgres MAX() is an aggregate,
        # so the 2-arg scalar form must be GREATEST() there.
        if dialect == "sqlite":
            return (
                "UPDATE sessions "
                "SET last_message_at = MAX(COALESCE(last_message_at, 0), :ts), "
                "    updated_at      = MAX(COALESCE(updated_at, 0), :ts) "
                "WHERE id = :id"
            )
        return (
            "UPDATE sessions "
            "SET last_message_at = GREATEST(COALESCE(last_message_at, 0), :ts), "
            "    updated_at      = GREATEST(COALESCE(updated_at, 0), :ts) "
            "WHERE id = :id"
        )

    async def touch_session_activity(self, session_id: str, *, at: float | None = None) -> None:
        """Bump last_message_at and updated_at for staleness detection."""
        ts = at if at is not None else time.time()
        async with self._tx() as conn:
            await conn.execute(
                text(self._touch_activity_sql(self.dialect)),
                {"ts": ts, "id": session_id},
            )

    async def update_session(
        self,
        session_id: str,
        *,
        reason_code: str | None = None,
        reason_summary: str = "",
        evidence_refs: list[dict[str, Any]] | None = None,
        reason_source: str = "executor",
        reason_actor: str | None = None,
        override: bool = False,
        override_actor: str | None = None,
        override_justification: str | None = None,
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
            override=override,
            override_actor=override_actor,
            override_justification=override_justification,
        )

        if fields:
            fields["updated_at"] = time.time()
            sets = ", ".join(f'"{k}" = :{k}' for k in fields)
            params = dict(fields)
            params["_id"] = session_id
            async with self._tx() as conn:
                await conn.execute(
                    text(f"UPDATE sessions SET {sets} WHERE id = :_id"),  # noqa: S608
                    params,
                )

    async def update_artifact_verification(
        self,
        session_id: str,
        verification: dict[str, Any] | None,
    ) -> None:
        # Must hold _write_lock: teardown calls this while signal persistence is
        # still bound (unbind happens after _teardown_common returns), so a late
        # signal emit's _tx() can race this UPDATE on SQLite.
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE sessions SET artifact_verification_json = :v, updated_at = :now WHERE id = :id"
                ).bindparams(bindparam("v", type_=JSON)),
                {"v": verification, "now": time.time(), "id": session_id},
            )

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
        params: dict[str, Any] = {}
        if node_metadata is not None:
            sets.append("node_metadata = :node_metadata")
            params["node_metadata"] = node_metadata
        if project is not None:
            sets.append("project = :project")
            params["project"] = project
            sets.append("project_source = :project_source")
            params["project_source"] = project_source
        if not sets:
            return
        params["_id"] = session_id

        async with self._tx() as conn:
            node_meta_bind = (
                text(f"UPDATE sessions SET {', '.join(sets)} WHERE id = :_id").bindparams(  # noqa: S608
                    bindparam("node_metadata", type_=JSON)
                )
                if "node_metadata" in params
                else text(f"UPDATE sessions SET {', '.join(sets)} WHERE id = :_id")  # noqa: S608
            )
            await conn.execute(node_meta_bind, params)
            if project:
                await self._upsert_project_stmt(conn, project, project_source or "cwd_dir")

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
        override: bool = False,
        override_actor: str | None = None,
        override_justification: str | None = None,
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
            override=override,
            override_actor=override_actor,
            override_justification=override_justification,
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
        override: bool = False,
        override_actor: str | None = None,
        override_justification: str | None = None,
    ) -> bool:
        """Atomically transition an entity's status and record the reason.

        When *expected_statuses* is provided, the transition is only performed
        if the current status is a member of that set.  Pass ``None`` inside
        the set to match a SQL NULL status (e.g. ``{None}`` for null-status
        sessions, ``{"running", None}`` to accept either).

        Returns ``True`` when the transition was applied, ``False`` when it was
        skipped because the current status was not in *expected_statuses*.  All
        existing callers that ignore the return value are unaffected.

        ADR-0094 integrity floor: once an entity's status is terminal (per
        TERMINAL_STATUSES_BY_ENTITY_TYPE), any write that would CHANGE it is
        rejected and recorded in admin_events — a terminal record must not
        silently move back to running or oscillate to a different terminal
        value. A same-status write (new_status == previous_status) is not a
        transition — it is allowed through untouched, since callers already
        rely on it to attach/refresh a reason code on an already-terminal row
        without that counting as leaving terminal. Pass override=True with
        override_actor and override_justification for a deliberate
        operational repair that does change the value; the repair is itself
        recorded in admin_events, distinctly from an ordinary transition.
        """
        if source not in _VALID_STATUS_SOURCES:
            raise ValueError(
                f"update_status() called with source={source!r}; "
                f"must be one of {sorted(_VALID_STATUS_SOURCES)}."
            )
        if override and (not override_actor or not override_justification):
            raise ValueError(
                "override=True requires both override_actor and "
                "override_justification (ADR-0094 operational-repair trail)."
            )
        canonical_type = _validate_entity_type_for_reason(entity_type)
        _validate_reason_code(reason_code)
        valid_statuses = VALID_STATUSES_BY_ENTITY_TYPE.get(canonical_type)
        if valid_statuses is not None and new_status not in valid_statuses:
            raise ValueError(
                f"update_status() called with new_status={new_status!r} for "
                f"entity_type={canonical_type!r}; vocabulary is {sorted(valid_statuses)}."
            )
        table = _reason_entity_table(canonical_type)
        now = time.time()
        terminal_statuses = TERMINAL_STATUSES_BY_ENTITY_TYPE.get(canonical_type, frozenset())

        rejected = False
        overridden = False
        async with self._tx() as conn:
            # FOR UPDATE on PG to prevent read-modify-write races under MVCC.
            sel = f"SELECT status FROM {table} WHERE id = :id"  # noqa: S608
            if self.dialect != "sqlite":
                sel += " FOR UPDATE"
            row = (await conn.execute(text(sel), {"id": entity_id})).mappings().first()
            if row is None:
                raise LookupError(f"{canonical_type} {entity_id!r} not found (table={table})")
            previous_status = row["status"]

            if expected_statuses is not None and previous_status not in expected_statuses:
                # CAS guard: current status is not in the expected set — skip.
                return False

            if previous_status in terminal_statuses and new_status != previous_status:
                if not override:
                    rejected = True
                else:
                    overridden = True

            if rejected:
                await conn.execute(
                    text(
                        "INSERT INTO admin_events "
                        "(id, created_at, action, target_id, details, actor) "
                        "VALUES (:id, :created_at, :action, :target_id, :details, :actor)"
                    ).bindparams(bindparam("details", type_=JSON)),
                    {
                        "id": uuid.uuid4().hex[:12],
                        "created_at": now,
                        "action": "status_transition_rejected",
                        "target_id": entity_id,
                        "details": {
                            "entity_type": canonical_type,
                            "previous_status": previous_status,
                            "attempted_status": new_status,
                            "reason_code": reason_code,
                            "source": source,
                        },
                        "actor": actor or source,
                    },
                )
                # Do not raise inside the transaction — the rejection audit
                # row above must commit even though the status write itself
                # does not; raise once the `async with` block below exits.
            else:
                if overridden:
                    await conn.execute(
                        text(
                            "INSERT INTO admin_events "
                            "(id, created_at, action, target_id, details, actor) "
                            "VALUES (:id, :created_at, :action, :target_id, :details, :actor)"
                        ).bindparams(bindparam("details", type_=JSON)),
                        {
                            "id": uuid.uuid4().hex[:12],
                            "created_at": now,
                            "action": "status_transition_override",
                            "target_id": entity_id,
                            "details": {
                                "entity_type": canonical_type,
                                "previous_status": previous_status,
                                "new_status": new_status,
                                "reason_code": reason_code,
                                "justification": override_justification,
                            },
                            "actor": override_actor,
                        },
                    )
                await self._apply_status_write(
                    conn,
                    table,
                    canonical_type,
                    entity_id,
                    previous_status=previous_status,
                    new_status=new_status,
                    reason_code=reason_code,
                    reason_summary=reason_summary,
                    evidence_refs=evidence_refs,
                    source=source,
                    actor=actor,
                    metadata=metadata,
                    now=now,
                )

        if rejected:
            raise TransitionRejectedError(canonical_type, entity_id, previous_status, new_status)
        return True

    async def _apply_status_write(
        self,
        conn: Any,
        table: str,
        canonical_type: str,
        entity_id: str,
        *,
        previous_status: str | None,
        new_status: str,
        reason_code: str,
        reason_summary: str,
        evidence_refs: list[dict[str, Any]] | None,
        source: str,
        actor: str | None,
        metadata: dict[str, Any] | None,
        now: float,
    ) -> None:
        """The actual status + status_transitions write, factored out of
        update_status() so both the ordinary and override paths share it.

        The UPDATE's WHERE clause re-asserts `previous_status` (the value read
        under the row lock in update_status()) so the compare-and-set is
        enforced by storage itself, not only by the Python read-then-write
        gap — a concurrent writer that changes the row between the SELECT and
        this UPDATE loses the race at the database level. The
        `status = :previous_status OR (status IS NULL AND :previous_status IS
        NULL)` form is the portable NULL-safe equality: it matches a NULL
        previous_status (sessions may have no status yet) on both SQLite and
        PostgreSQL — unlike SQLite's `IS` extension (a NULL-safe `=` for any
        operand pair), PostgreSQL's `IS` only accepts the NULL/TRUE/FALSE
        keywords, not a bound parameter.
        """
        result = await conn.execute(
            text(
                f"UPDATE {table} SET "  # noqa: S608
                "  status = :status, "
                "  status_reason_code = :reason_code, "
                "  status_reason_summary = :reason_summary, "
                "  status_evidence_refs = :evidence_refs, "
                "  updated_at = :now "
                "WHERE id = :id "
                "  AND (status = :previous_status "
                "       OR (status IS NULL AND :previous_status IS NULL))"
            ).bindparams(bindparam("evidence_refs", type_=JSON)),
            {
                "status": new_status,
                "reason_code": reason_code,
                "reason_summary": reason_summary,
                "evidence_refs": evidence_refs or [],
                "now": now,
                "id": entity_id,
                "previous_status": previous_status,
            },
        )
        if result.rowcount == 0:
            raise RuntimeError(
                f"status CAS lost for {canonical_type} {entity_id!r}: "
                "row changed under update_status"
            )

        await conn.execute(
            text(
                "INSERT INTO status_transitions "
                "(id, entity_type, entity_id, previous_status, status, "
                " reason_code, reason_summary, evidence_refs, "
                " source, actor, created_at, metadata) "
                "VALUES (:id, :entity_type, :entity_id, :previous_status, :status, "
                " :reason_code, :reason_summary, :evidence_refs, "
                " :source, :actor, :created_at, :metadata)"
            ).bindparams(
                bindparam("evidence_refs", type_=JSON),
                bindparam("metadata", type_=JSON),
            ),
            {
                "id": uuid.uuid4().hex,
                "entity_type": canonical_type,
                "entity_id": entity_id,
                "previous_status": previous_status,
                "status": new_status,
                "reason_code": reason_code,
                "reason_summary": reason_summary,
                "evidence_refs": evidence_refs or [],
                "source": source,
                "actor": actor,
                "created_at": now,
                "metadata": metadata,
            },
        )

    async def list_sessions(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        query = "SELECT * FROM sessions"
        if status:
            conds.append("status = :status")
            params["status"] = status
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset
        async with self._read() as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
        return [self._row_to_dict(r) for r in rows]

    async def count_sessions(self, *, status: str | None = None) -> int:
        if status:
            async with self._read() as conn:
                row = (
                    (
                        await conn.execute(
                            text("SELECT COUNT(*) AS n FROM sessions WHERE status = :status"),
                            {"status": status},
                        )
                    )
                    .mappings()
                    .first()
                )
        else:
            async with self._read() as conn:
                row = (
                    (await conn.execute(text("SELECT COUNT(*) AS n FROM sessions")))
                    .mappings()
                    .first()
                )
        return row["n"]

    async def activity_stats(
        self, *, window_start: float, bucket_seconds: int
    ) -> list[dict[str, Any]]:
        """Per-bucket (bucket_start, status, count) rows for the activity window.

        Bucketed by the raw epoch-seconds anchor timestamp (ended_at for
        terminal sessions, started_at/created_at while running) with a single
        GROUP BY — no per-bucket queries and no row-by-row counting in Python.
        ``window_start`` is expected to already be bucket-aligned (the caller
        owns bucket-boundary math) so every returned row lands in a bucket the
        caller asked for.
        """
        query = """
            SELECT
                CAST(
                    COALESCE(ended_at, started_at, created_at) / :bucket_seconds
                    AS INTEGER
                ) * :bucket_seconds AS bucket_start,
                status,
                COUNT(*) AS n
            FROM sessions
            WHERE COALESCE(ended_at, started_at, created_at) >= :window_start
            GROUP BY bucket_start, status
        """  # noqa: S608
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(query),
                        {"bucket_seconds": bucket_seconds, "window_start": window_start},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    # ── Projects ──────────────────────────────────────────────────────

    async def _upsert_project_stmt(
        self,
        conn,
        name: str,
        source: str,
        *,
        path: str | None = None,
        github: str | None = None,
    ) -> None:
        """Projects-registry upsert statement only; caller owns the transaction."""
        now = time.time()
        await conn.execute(
            text(
                """INSERT INTO projects
                       (name, source, path, github, created_at, updated_at, last_seen_at)
                   VALUES (:name, :source, :path, :github, :now, :now2, :now3)
                   ON CONFLICT(name) DO UPDATE SET
                       last_seen_at = excluded.last_seen_at,
                       updated_at   = excluded.updated_at,
                       source       = COALESCE(
                           CASE WHEN excluded.source IN ('config_toml', 'global_override')
                                THEN excluded.source ELSE NULL END,
                           projects.source
                       ),
                       path   = COALESCE(excluded.path, projects.path),
                       github = COALESCE(excluded.github, projects.github)"""
            ),
            {
                "name": name,
                "source": source,
                "path": path,
                "github": github,
                "now": now,
                "now2": now,
                "now3": now,
            },
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
        async with self._tx() as conn:
            await self._upsert_project_stmt(conn, name, source, path=path, github=github)

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
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO projects
                           (name, source, path, github, description,
                            created_at, updated_at, last_seen_at)
                       VALUES (:name, 'studio', :path, :github, :description, :now, :now2, :now3)"""
                ),
                {
                    "name": name,
                    "path": path,
                    "github": github,
                    "description": description,
                    "now": now,
                    "now2": now,
                    "now3": now,
                },
            )

    async def list_projects(self) -> list[dict[str, Any]]:
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            """SELECT p.*,
                                  COUNT(s.id) AS session_count,
                                  SUM(CASE WHEN s.status = 'running' THEN 1 ELSE 0 END) AS running_count
                           FROM projects p
                           LEFT JOIN sessions s ON s.project = p.name
                           GROUP BY p.name
                           ORDER BY COALESCE(p.last_seen_at, p.updated_at) DESC"""
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    async def get_project(self, name: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            """SELECT p.*,
                                  COUNT(s.id) AS session_count,
                                  SUM(CASE WHEN s.status = 'running' THEN 1 ELSE 0 END) AS running_count
                           FROM projects p
                           LEFT JOIN sessions s ON s.project = p.name
                           WHERE p.name = :name
                           GROUP BY p.name"""
                        ),
                        {"name": name},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def update_project(self, name: str, **fields: Any) -> bool:
        allowed = {"description", "github", "path"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Invalid project field(s): {bad}")
        if not fields:
            return False
        fields["updated_at"] = time.time()
        sets = ", ".join(f'"{k}" = :{k}' for k in fields)
        params = dict(fields)
        params["_name"] = name
        async with self._tx() as conn:
            result = await conn.execute(
                text(f"UPDATE projects SET {sets} WHERE name = :_name"),  # noqa: S608
                params,
            )
        return result.rowcount > 0

    async def delete_project(self, name: str) -> bool:
        """Delete a Studio-managed project; auto-detected ones are immutable."""
        async with self._tx() as conn:
            result = await conn.execute(
                text("DELETE FROM projects WHERE name = :name AND source = 'studio'"),
                {"name": name},
            )
        return result.rowcount > 0

    # ── Schedules (ADR-0027) ──────────────────────────────────────────

    async def create_schedule(self, schedule: dict[str, Any]) -> None:
        now = time.time()
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO schedules
                       (id, name, description, enabled, trigger_type,
                        cron_expr, interval_sec, github_repo, github_filter,
                        github_cursor, poll_interval_sec,
                        action_kind, action_model, action_prompt, action_agent,
                        action_playbook, action_flow_yaml, action_project, action_extra_args,
                        on_success, on_fail, last_fired_at, next_fire_at,
                        missed_fire_policy, overlap_policy, max_runs, project,
                        created_at, updated_at)
                       VALUES (:id, :name, :description, :enabled, :trigger_type,
                               :cron_expr, :interval_sec, :github_repo, :github_filter,
                               :github_cursor, :poll_interval_sec,
                               :action_kind, :action_model, :action_prompt, :action_agent,
                               :action_playbook, :action_flow_yaml, :action_project, :action_extra_args,
                               :on_success, :on_fail, :last_fired_at, :next_fire_at,
                               :missed_fire_policy, :overlap_policy, :max_runs, :project,
                               :created_at, :updated_at)"""
                ).bindparams(
                    bindparam("github_filter", type_=JSON),
                    bindparam("action_extra_args", type_=JSON),
                    bindparam("on_success", type_=JSON),
                    bindparam("on_fail", type_=JSON),
                ),
                {
                    "id": schedule["id"],
                    "name": schedule["name"],
                    "description": schedule.get("description"),
                    "enabled": schedule.get("enabled", 1),
                    "trigger_type": schedule["trigger_type"],
                    "cron_expr": schedule.get("cron_expr"),
                    "interval_sec": schedule.get("interval_sec"),
                    "github_repo": schedule.get("github_repo"),
                    "github_filter": schedule.get("github_filter"),
                    "github_cursor": schedule.get("github_cursor"),
                    "poll_interval_sec": schedule.get("poll_interval_sec"),
                    "action_kind": schedule["action_kind"],
                    "action_model": schedule.get("action_model"),
                    "action_prompt": schedule.get("action_prompt"),
                    "action_agent": schedule.get("action_agent"),
                    "action_playbook": schedule.get("action_playbook"),
                    "action_flow_yaml": schedule.get("action_flow_yaml"),
                    "action_project": schedule.get("action_project"),
                    "action_extra_args": schedule.get("action_extra_args", []),
                    "on_success": schedule.get("on_success"),
                    "on_fail": schedule.get("on_fail"),
                    "last_fired_at": schedule.get("last_fired_at"),
                    "next_fire_at": schedule.get("next_fire_at"),
                    "missed_fire_policy": schedule.get("missed_fire_policy", "skip"),
                    "overlap_policy": schedule.get("overlap_policy", "skip"),
                    "max_runs": schedule.get("max_runs"),
                    "project": schedule.get("project"),
                    "created_at": schedule.get("created_at", now),
                    "updated_at": schedule.get("updated_at", now),
                },
            )

    async def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM schedules WHERE id = :id"),
                        {"id": schedule_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row else None

    async def get_schedule_by_name(self, name: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM schedules WHERE name = :name"),
                        {"name": name},
                    )
                )
                .mappings()
                .first()
            )
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
        params: dict[str, Any] = {}
        if enabled is not None:
            conds.append("enabled = :enabled")
            params["enabled"] = 1 if enabled else 0
        if trigger_type:
            conds.append("trigger_type = :trigger_type")
            params["trigger_type"] = trigger_type
        if project:
            conds.append("project = :project")
            params["project"] = project
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset
        async with self._read() as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
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
            "max_runs",
            "project",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Invalid schedule field(s): {bad}")
        json_fields = {"github_filter", "action_extra_args", "on_success", "on_fail"}
        fields["updated_at"] = time.time()
        sets_parts = []
        bind_params = []
        for k in fields:
            sets_parts.append(f'"{k}" = :{k}')
            if k in json_fields:
                bind_params.append(bindparam(k, type_=JSON))
        params = dict(fields)
        params["_id"] = schedule_id
        stmt = text(f"UPDATE schedules SET {', '.join(sets_parts)} WHERE id = :_id")  # noqa: S608
        if bind_params:
            stmt = stmt.bindparams(*bind_params)
        async with self._tx() as conn:
            await conn.execute(stmt, params)

    async def delete_schedule(self, schedule_id: str) -> bool:
        async with self._tx() as conn:
            result = await conn.execute(
                text("DELETE FROM schedules WHERE id = :id"),
                {"id": schedule_id},
            )
        return result.rowcount > 0

    # ── Schedule Runs (ADR-0027) ──────────────────────────────────────

    async def create_schedule_run(self, run: dict[str, Any]) -> None:
        now = time.time()
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO schedule_runs
                       (id, schedule_id, invocation_id, trigger_context,
                        action_kind, action_args, status, exit_code,
                        chain_parent_id, chain_depth, fired_at, ended_at,
                        error_detail, created_at)
                       VALUES (:id, :schedule_id, :invocation_id, :trigger_context,
                               :action_kind, :action_args, :status, :exit_code,
                               :chain_parent_id, :chain_depth, :fired_at, :ended_at,
                               :error_detail, :created_at)"""
                ).bindparams(
                    bindparam("trigger_context", type_=JSON),
                    bindparam("action_args", type_=JSON),
                ),
                {
                    "id": run["id"],
                    "schedule_id": run["schedule_id"],
                    "invocation_id": run.get("invocation_id"),
                    "trigger_context": run["trigger_context"],
                    "action_kind": run["action_kind"],
                    "action_args": run["action_args"],
                    "status": run.get("status", "running"),
                    "exit_code": run.get("exit_code"),
                    "chain_parent_id": run.get("chain_parent_id"),
                    "chain_depth": run.get("chain_depth", 0),
                    "fired_at": run["fired_at"],
                    "ended_at": run.get("ended_at"),
                    "error_detail": run.get("error_detail"),
                    "created_at": run.get("created_at", now),
                },
            )

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
            sets = ", ".join(f'"{k}" = :{k}' for k in fields)
            params = dict(fields)
            params["_id"] = run_id
            async with self._tx() as conn:
                await conn.execute(
                    text(f"UPDATE schedule_runs SET {sets} WHERE id = :_id"),  # noqa: S608
                    params,
                )

    async def list_schedule_runs(
        self,
        schedule_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conds: list[str] = ["schedule_id = :schedule_id"]
        params: dict[str, Any] = {"schedule_id": schedule_id}
        if status:
            conds.append("status = :status")
            params["status"] = status
        query = "SELECT * FROM schedule_runs WHERE " + " AND ".join(conds)  # noqa: S608
        query += " ORDER BY fired_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset
        async with self._read() as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
        return [self._row_to_dict(r) for r in rows]

    async def count_schedule_runs(
        self,
        schedule_id: str,
        *,
        chain_depth: int = 0,
        statuses: tuple[str, ...] = ("completed", "failed", "cancelled"),
    ) -> int:
        """Count runs that actually fired and reached a terminal status.

        Used for max_runs bookkeeping: chain_depth=0 excludes on_success/
        on_fail chain children (they don't consume the parent's budget), and
        the default status set excludes 'skipped' (missed-fire/overlap skips
        never ran) and 'running' (not yet terminal).
        """
        placeholders = ", ".join(f":status{i}" for i in range(len(statuses)))
        params: dict[str, Any] = {"schedule_id": schedule_id, "chain_depth": chain_depth}
        params.update({f"status{i}": s for i, s in enumerate(statuses)})
        query = f"SELECT COUNT(*) AS n FROM schedule_runs WHERE schedule_id = :schedule_id AND chain_depth = :chain_depth AND status IN ({placeholders})"  # noqa: S608
        async with self._read() as conn:
            row = (await conn.execute(text(query), params)).mappings().first()
        return int(row["n"]) if row else 0

    async def schedule_run_streak(self, schedule_id: str) -> tuple[int, str | None]:
        """Consecutive terminal 'failed' streak and most recent status, newest-first, capped at 50 rows."""
        query = """SELECT status FROM schedule_runs
                   WHERE schedule_id = :schedule_id AND chain_depth = 0
                   ORDER BY fired_at DESC LIMIT 50"""  # noqa: S608
        async with self._read() as conn:
            rows = (await conn.execute(text(query), {"schedule_id": schedule_id})).mappings().all()
        if not rows:
            return 0, None
        last_status = rows[0]["status"]
        streak = 0
        for row in rows:
            status = row["status"]
            if status in ("completed", "cancelled"):
                break
            if status == "failed":
                streak += 1
        return streak, last_status

    async def get_schedule_run(self, run_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM schedule_runs WHERE id = :id"),
                        {"id": run_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row else None

    async def get_schedule_run_by_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        """Look up the schedule_run that fired a given invocation (ADR-0027).

        invocation_id is 1:1 with schedule_runs in practice (each fire mints a
        fresh invocation), but the ORDER BY + LIMIT keeps this defensively
        correct if that ever isn't true.
        """
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM schedule_runs WHERE invocation_id = :invocation_id "
                            "ORDER BY COALESCE(created_at, 0) DESC, id DESC LIMIT 1"
                        ),
                        {"invocation_id": invocation_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row else None

    async def list_running_schedule_runs(self, schedule_id: str) -> list[dict[str, Any]]:
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM schedule_runs WHERE schedule_id = :sid AND status = 'running'"
                        ),
                        {"sid": schedule_id},
                    )
                )
                .mappings()
                .all()
            )
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
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO invocations
                       (id, skill, plugin, prompt, started_at, ended_at, status,
                        session_count, created_at, updated_at, node_metadata)
                       VALUES (:id, :skill, :plugin, :prompt, :started_at, :ended_at, :status,
                               :session_count, :created_at, :updated_at, :node_metadata)
                       ON CONFLICT (id) DO NOTHING"""
                ).bindparams(bindparam("node_metadata", type_=JSON)),
                {
                    "id": invocation["id"],
                    "skill": invocation["skill"],
                    "plugin": invocation.get("plugin"),
                    "prompt": invocation.get("prompt"),
                    "started_at": invocation["started_at"],
                    "ended_at": invocation.get("ended_at"),
                    "status": status,
                    "session_count": invocation.get("session_count", 0),
                    "created_at": invocation.get("created_at", now),
                    "updated_at": invocation.get("updated_at", now),
                    "node_metadata": invocation.get("node_metadata"),
                },
            )

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
            json_fields = {"node_metadata"}
            sets_parts = []
            bind_params = []
            for k in fields:
                sets_parts.append(f'"{k}" = :{k}')
                if k in json_fields:
                    bind_params.append(bindparam(k, type_=JSON))
            params = dict(fields)
            params["_id"] = invocation_id
            stmt = text(f"UPDATE invocations SET {', '.join(sets_parts)} WHERE id = :_id")  # noqa: S608
            if bind_params:
                stmt = stmt.bindparams(*bind_params)
            async with self._tx() as conn:
                await conn.execute(stmt, params)

    async def get_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM invocations WHERE id = :id"),
                        {"id": invocation_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row else None

    async def list_invocations(
        self,
        *,
        skill: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        # Per invocation, take project/project_source from its latest-updated
        # session. ROW_NUMBER() is portable; the old SQLite idiom (bare columns
        # under HAVING MAX(updated_at)) is rejected by PostgreSQL.
        #
        # Also surface the schedule_run that fired this invocation (exit_code,
        # error_detail) so the UI can show why a scheduled run failed without
        # a second round-trip. Unlike the sessions join above, this uses
        # correlated scalar subqueries rather than a ranked derived table:
        # ORDER BY + LIMIT/OFFSET on inv.updated_at narrows to the emitted
        # page first (sorting only invocations, not schedule_runs), and each
        # subquery then runs once per emitted row against the partial index
        # on schedule_runs(invocation_id) — instead of ROW_NUMBER()ing the
        # entire schedule_runs table before pagination even applies.
        query = (
            "SELECT inv.*, "
            "  sq.project        AS project, "
            "  sq.project_source AS project_source, "
            "  ( SELECT sr.exit_code FROM schedule_runs sr "
            "    WHERE sr.invocation_id = inv.id "
            "    ORDER BY COALESCE(sr.created_at, 0) DESC, sr.id DESC LIMIT 1 "
            "  ) AS schedule_run_exit_code, "
            "  ( SELECT sr.error_detail FROM schedule_runs sr "
            "    WHERE sr.invocation_id = inv.id "
            "    ORDER BY COALESCE(sr.created_at, 0) DESC, sr.id DESC LIMIT 1 "
            "  ) AS schedule_run_error_detail "
            "FROM invocations inv "
            "LEFT JOIN ( "
            "  SELECT invocation_id, project, project_source FROM ( "
            "    SELECT invocation_id, project, project_source, "
            "           ROW_NUMBER() OVER ( "
            "             PARTITION BY invocation_id "
            "             ORDER BY COALESCE(updated_at, 0) DESC, "
            "                      COALESCE(created_at, 0) DESC, id DESC "
            "           ) AS rn "
            "    FROM sessions "
            "    WHERE invocation_id IS NOT NULL "
            "  ) ranked "
            "  WHERE rn = 1 "
            ") sq ON sq.invocation_id = inv.id"
        )
        conds: list[str] = []
        params: dict[str, Any] = {}
        if skill:
            conds.append("inv.skill = :skill")
            params["skill"] = skill
        if status:
            conds.append("inv.status = :status")
            params["status"] = status
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY inv.updated_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset
        async with self._read() as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
        return [self._row_to_dict(r) for r in rows]

    async def list_sessions_for_invocation(self, invocation_id: str) -> list[dict[str, Any]]:
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM sessions WHERE invocation_id = :id ORDER BY created_at ASC"
                        ),
                        {"id": invocation_id},
                    )
                )
                .mappings()
                .all()
            )
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
            sql = (
                "SELECT id FROM artifacts "
                "WHERE invocation_id = :inv_id AND session_id = :ses_id AND kind = :kind AND name = :name"
            )
            params = {"inv_id": invocation_id, "ses_id": session_id, "kind": kind, "name": name}
        elif invocation_id is not None:
            sql = (
                "SELECT id FROM artifacts "
                "WHERE invocation_id = :inv_id AND session_id IS NULL AND kind = :kind AND name = :name"
            )
            params = {"inv_id": invocation_id, "kind": kind, "name": name}
        elif session_id is not None:
            sql = (
                "SELECT id FROM artifacts "
                "WHERE session_id = :ses_id AND invocation_id IS NULL AND kind = :kind AND name = :name"
            )
            params = {"ses_id": session_id, "kind": kind, "name": name}
        else:
            sql = (
                "SELECT id FROM artifacts "
                "WHERE invocation_id IS NULL AND session_id IS NULL AND kind = :kind AND name = :name"
            )
            params = {"kind": kind, "name": name}
        async with self._read() as conn:
            row = (await conn.execute(text(sql), params)).mappings().first()
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
        existing_id = await self._find_artifact_id(
            kind=kind, name=name, invocation_id=invocation_id, session_id=session_id
        )
        if existing_id:
            async with self._tx() as conn:
                await conn.execute(
                    text(
                        "UPDATE artifacts SET content = :content, file_path = :fp, updated_at = :now WHERE id = :id"
                    ).bindparams(bindparam("content", type_=JSON)),
                    {"content": content, "fp": file_path, "now": now, "id": existing_id},
                )
            return existing_id
        art_id = uuid.uuid4().hex[:12]
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO artifacts "
                    "(id, invocation_id, session_id, created_at, updated_at, kind, name, content, file_path) "
                    "VALUES (:id, :inv_id, :ses_id, :now, :now2, :kind, :name, :content, :fp)"
                ).bindparams(bindparam("content", type_=JSON)),
                {
                    "id": art_id,
                    "inv_id": invocation_id,
                    "ses_id": session_id,
                    "now": now,
                    "now2": now,
                    "kind": kind,
                    "name": name,
                    "content": content,
                    "fp": file_path,
                },
            )
        return art_id

    async def list_artifacts_for_invocation(self, invocation_id: str) -> list[dict[str, Any]]:
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM artifacts WHERE invocation_id = :id ORDER BY created_at ASC"
                        ),
                        {"id": invocation_id},
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_dict(r) for r in rows]

    async def list_artifacts_for_session(self, session_id: str) -> list[dict[str, Any]]:
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM artifacts WHERE session_id = :id ORDER BY created_at ASC"
                        ),
                        {"id": session_id},
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_dict(r) for r in rows]

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM artifacts WHERE id = :id"),
                        {"id": artifact_id},
                    )
                )
                .mappings()
                .first()
            )
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
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_events (id, created_at, action, target_id, "
                    "details, actor) VALUES (:id, :created_at, :action, :target_id, :details, :actor)"
                ).bindparams(bindparam("details", type_=JSON)),
                {
                    "id": event_id,
                    "created_at": time.time(),
                    "action": action,
                    "target_id": target_id,
                    "details": details,
                    "actor": actor,
                },
            )
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
        params: dict[str, Any] = {}
        if action:
            conds.append("action = :action")
            params["action"] = action
        if target_id:
            conds.append("target_id = :target_id")
            params["target_id"] = target_id
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY created_at DESC LIMIT :limit"
        params["limit"] = limit
        async with self._read() as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
        return [self._row_to_dict(r) for r in rows]

    # ── Branches ───────────────────────────────────────────────────────

    async def create_branch(self, branch: dict[str, Any]) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO branches (id, created_at, node_metadata, "user", name,
                       session_id, progression_id, system_msg_id, model, provider, agent_name)
                       VALUES (:id, :created_at, :node_metadata, :user, :name,
                               :session_id, :progression_id, :system_msg_id, :model, :provider, :agent_name)
                       ON CONFLICT (id) DO NOTHING"""
                ).bindparams(bindparam("node_metadata", type_=JSON)),
                {
                    "id": branch["id"],
                    "created_at": branch.get("created_at", time.time()),
                    "node_metadata": branch.get("node_metadata"),
                    "user": branch.get("user"),
                    "name": branch.get("name"),
                    "session_id": branch["session_id"],
                    "progression_id": branch["progression_id"],
                    "system_msg_id": branch.get("system_msg_id"),
                    "model": branch.get("model"),
                    "provider": branch.get("provider"),
                    "agent_name": branch.get("agent_name"),
                },
            )

    async def get_branch(self, branch_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM branches WHERE id = :id"),
                        {"id": branch_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row else None

    async def update_branch(self, branch_id: str, **fields: Any) -> None:
        _validate_columns(fields, _BRANCH_COLUMNS)
        if not fields:
            return
        json_fields = {"node_metadata"}
        sets_parts = []
        bind_params = []
        for k in fields:
            sets_parts.append(f'"{k}" = :{k}')
            if k in json_fields:
                bind_params.append(bindparam(k, type_=JSON))
        params = dict(fields)
        params["_id"] = branch_id
        stmt = text(f"UPDATE branches SET {', '.join(sets_parts)} WHERE id = :_id")  # noqa: S608
        if bind_params:
            stmt = stmt.bindparams(*bind_params)
        async with self._tx() as conn:
            await conn.execute(stmt, params)

    async def repair_branch_progression(
        self,
        branch_id: str,
        new_progression_id: str,
    ) -> str | None:
        """Backfill NULL progression_id; returns the effective id or None."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE branches SET progression_id = :new_id WHERE id = :id AND progression_id IS NULL"
                ),
                {"new_id": new_progression_id, "id": branch_id},
            )
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT progression_id FROM branches WHERE id = :id"),
                        {"id": branch_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return row["progression_id"]

    async def repair_session_progression(
        self,
        session_id: str,
        new_progression_id: str,
    ) -> str | None:
        """Backfill NULL progression_id; returns the effective id or None."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE sessions SET progression_id = :new_id WHERE id = :id AND progression_id IS NULL"
                ),
                {"new_id": new_progression_id, "id": session_id},
            )
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT progression_id FROM sessions WHERE id = :id"),
                        {"id": session_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return row["progression_id"]

    async def list_branches(self, session_id: str) -> list[dict[str, Any]]:
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text("SELECT * FROM branches WHERE session_id = :id ORDER BY created_at"),
                        {"id": session_id},
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_dict(r) for r in rows]

    async def get_branch_messages(self, branch_id: str) -> list[dict[str, Any]]:
        branch = await self.get_branch(branch_id)
        if not branch:
            return []
        message_ids = await self.get_progression(branch["progression_id"])
        if not message_ids:
            return []
        placeholders = ",".join(f":id{i}" for i in range(len(message_ids)))
        sql = (
            f"SELECT m.*, mt.lion_class AS lion_class_str "  # noqa: S608
            f"FROM messages m LEFT JOIN message_types mt ON m.lion_class = mt.type_id "
            f"WHERE m.id IN ({placeholders})"
        )
        params = {f"id{i}": mid for i, mid in enumerate(message_ids)}
        async with self._read() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
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
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO shows (id, topic, goal, repo, base_branch,
                       integration_branch, status, show_dir, status_source,
                       created_at, updated_at)
                       VALUES (:id, :topic, :goal, :repo, :base_branch,
                               :integration_branch, :status, :show_dir, :status_source,
                               :created_at, :updated_at)
                       ON CONFLICT (id) DO NOTHING"""
                ),
                {
                    "id": show["id"],
                    "topic": show["topic"],
                    "goal": show.get("goal"),
                    "repo": show.get("repo"),
                    "base_branch": show.get("base_branch"),
                    "integration_branch": show.get("integration_branch"),
                    "status": show.get("status", "active"),
                    "show_dir": show["show_dir"],
                    "status_source": show.get("status_source", "unknown"),
                    "created_at": show.get("created_at", now),
                    "updated_at": now,
                },
            )

    async def get_show(self, show_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM shows WHERE id = :id"),
                        {"id": show_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row else None

    async def get_show_by_topic(self, topic: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM shows WHERE topic = :topic"),
                        {"topic": topic},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row else None

    async def list_shows(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            async with self._read() as conn:
                rows = (
                    (
                        await conn.execute(
                            text(
                                "SELECT * FROM shows WHERE status = :status ORDER BY updated_at DESC"
                            ),
                            {"status": status},
                        )
                    )
                    .mappings()
                    .all()
                )
        else:
            async with self._read() as conn:
                rows = (
                    (await conn.execute(text("SELECT * FROM shows ORDER BY updated_at DESC")))
                    .mappings()
                    .all()
                )
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
            sets = ", ".join(f'"{k}" = :{k}' for k in fields)
            params = dict(fields)
            params["_id"] = show_id
            async with self._tx() as conn:
                await conn.execute(
                    text(f"UPDATE shows SET {sets} WHERE id = :_id"),  # noqa: S608
                    params,
                )

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
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO plays (id, show_id, name, playbook, effort,
                       status, attempt, session_id, started_at, ended_at, exit_code,
                       worktree, branch, merge_sha, merged_at, gate_passed, gate_feedback,
                       depends_on, sort_order, created_at, updated_at)
                       VALUES (:id, :show_id, :name, :playbook, :effort,
                               :status, :attempt, :session_id, :started_at, :ended_at, :exit_code,
                               :worktree, :branch, :merge_sha, :merged_at, :gate_passed, :gate_feedback,
                               :depends_on, :sort_order, :created_at, :updated_at)
                       ON CONFLICT (id) DO NOTHING"""
                ).bindparams(bindparam("depends_on", type_=JSON)),
                {
                    "id": play["id"],
                    "show_id": play["show_id"],
                    "name": play["name"],
                    "playbook": play.get("playbook"),
                    "effort": play.get("effort"),
                    "status": play.get("status", "pending"),
                    "attempt": play.get("attempt", 1),
                    "session_id": play.get("session_id"),
                    "started_at": play.get("started_at"),
                    "ended_at": play.get("ended_at"),
                    "exit_code": play.get("exit_code"),
                    "worktree": play.get("worktree"),
                    "branch": play.get("branch"),
                    "merge_sha": play.get("merge_sha"),
                    "merged_at": play.get("merged_at"),
                    "gate_passed": play.get("gate_passed"),
                    "gate_feedback": play.get("gate_feedback"),
                    "depends_on": play.get("depends_on", []),
                    "sort_order": play.get("sort_order", 0),
                    "created_at": play.get("created_at", now),
                    "updated_at": now,
                },
            )

    async def get_play(self, play_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM plays WHERE id = :id"),
                        {"id": play_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row else None

    async def list_plays(self, show_id: str) -> list[dict[str, Any]]:
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM plays WHERE show_id = :id ORDER BY sort_order, created_at"
                        ),
                        {"id": show_id},
                    )
                )
                .mappings()
                .all()
            )
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
            json_fields = {"depends_on", "status_evidence_refs"}
            sets_parts = []
            bind_params = []
            for k in fields:
                sets_parts.append(f'"{k}" = :{k}')
                if k in json_fields:
                    bind_params.append(bindparam(k, type_=JSON))
            params = dict(fields)
            params["_id"] = play_id
            stmt = text(f"UPDATE plays SET {', '.join(sets_parts)} WHERE id = :_id")  # noqa: S608
            if bind_params:
                stmt = stmt.bindparams(*bind_params)
            async with self._tx() as conn:
                await conn.execute(stmt, params)

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
                    async with self._tx() as conn:
                        row = (
                            (
                                await conn.execute(
                                    text(
                                        "SELECT MAX(version) AS v FROM definitions WHERE kind = :kind AND name = :name"
                                    ),
                                    {"kind": kind, "name": name},
                                )
                            )
                            .mappings()
                            .first()
                        )
                        next_version = (row["v"] or 0) + 1
                        await conn.execute(
                            text(
                                """INSERT INTO definitions
                                   (id, kind, name, path, content, version,
                                    created_at, message)
                                   VALUES (:id, :kind, :name, :path, :content, :version,
                                           :created_at, :message)"""
                            ),
                            {
                                "id": str(uuid.uuid4()),
                                "kind": kind,
                                "name": name,
                                "path": path,
                                "content": content,
                                "version": next_version,
                                "created_at": time.time(),
                                "message": message,
                            },
                        )
                    return next_version
                except IntegrityError as exc:
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
            async with self._read() as conn:
                row = (
                    (
                        await conn.execute(
                            text(
                                "SELECT * FROM definitions WHERE kind = :kind AND name = :name AND version = :version"
                            ),
                            {"kind": kind, "name": name, "version": version},
                        )
                    )
                    .mappings()
                    .first()
                )
        else:
            async with self._read() as conn:
                row = (
                    (
                        await conn.execute(
                            text(
                                "SELECT * FROM definitions WHERE kind = :kind AND name = :name ORDER BY version DESC LIMIT 1"
                            ),
                            {"kind": kind, "name": name},
                        )
                    )
                    .mappings()
                    .first()
                )
        return dict(row) if row else None

    async def list_definition_versions(self, kind: str, name: str) -> list[dict[str, Any]]:
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, kind, name, version, created_at, message FROM definitions WHERE kind = :kind AND name = :name ORDER BY version DESC"
                        ),
                        {"kind": kind, "name": name},
                    )
                )
                .mappings()
                .all()
            )
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
        through ``self._write_lock`` (via _tx()) so that no two coroutines can
        enter BEGIN IMMEDIATE simultaneously on the same AsyncEngine on SQLite.
        PostgreSQL uses an advisory transaction lock per session_id.
        """
        sig_id = uuid.uuid4().hex
        async with self._tx() as conn:
            if self.dialect != "sqlite":
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:k,0))"),
                    {"k": session_id},
                )
            row = (
                await conn.execute(
                    text(
                        "SELECT COALESCE(MAX(seq), 0) FROM session_signals WHERE session_id = :sid"
                    ),
                    {"sid": session_id},
                )
            ).scalar()
            seq: int = (row or 0) + 1
            await conn.execute(
                text(
                    "INSERT INTO session_signals (id, session_id, seq, kind, op_id, ts, payload) "
                    "VALUES (:id, :sid, :seq, :kind, :op_id, :ts, :payload)"
                ).bindparams(bindparam("payload", type_=JSON)),
                {
                    "id": sig_id,
                    "sid": session_id,
                    "seq": seq,
                    "kind": kind,
                    "op_id": op_id,
                    "ts": ts,
                    "payload": payload,
                },
            )
        return seq

    async def get_session_signals_after(
        self,
        session_id: str,
        after_seq: int,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return signals for *session_id* with seq > *after_seq*, ordered by seq."""
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, session_id, seq, kind, op_id, ts, payload "
                            "FROM session_signals "
                            "WHERE session_id = :sid AND seq > :after_seq "
                            "ORDER BY seq "
                            "LIMIT :limit"
                        ),
                        {"sid": session_id, "after_seq": after_seq, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
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

        Serialised through _tx() to prevent concurrent INSERT conflicts on
        the shared connection on SQLite.
        """
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO engine_runs "
                    "(id, kind, spec_json, status, started_at, session_id) "
                    "VALUES (:id, :kind, :spec_json, 'running', :started_at, :session_id)"
                ).bindparams(bindparam("spec_json", type_=JSON)),
                {
                    "id": run_id,
                    "kind": kind,
                    "spec_json": spec_json,
                    "started_at": started_at,
                    "session_id": session_id,
                },
            )

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
        Serialised through _tx().
        """
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE engine_runs "
                    "SET status = :status, ended_at = :ended_at, export_dir = :export_dir, error = :error "
                    "WHERE id = :id"
                ),
                {
                    "status": status,
                    "ended_at": ended_at,
                    "export_dir": export_dir,
                    "error": error,
                    "id": run_id,
                },
            )

    async def get_engine_run(self, run_id: str) -> dict[str, Any] | None:
        """Return a single engine run row as a dict, or None if not found."""
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, kind, spec_json, status, started_at, ended_at, "
                            "session_id, export_dir, error "
                            "FROM engine_runs WHERE id = :id"
                        ),
                        {"id": run_id},
                    )
                )
                .mappings()
                .first()
            )
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
        params: dict[str, Any] = {}
        if kind is not None:
            conditions.append("kind = :kind")
            params["kind"] = kind
        if status is not None:
            conditions.append("status = :status")
            params["status"] = status
        if session_id is not None:
            conditions.append("session_id = :session_id")
            params["session_id"] = session_id
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params["limit"] = limit
        params["offset"] = offset
        sql = (
            f"SELECT id, kind, spec_json, status, started_at, ended_at, "  # noqa: S608
            f"session_id, export_dir, error "
            f"FROM engine_runs {where} "
            f"ORDER BY started_at DESC "
            f"LIMIT :limit OFFSET :offset"
        )
        async with self._read() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
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
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO engine_defs "
                    "(id, name, kind, model, max_depth, max_agents, options, description, created_at, updated_at) "
                    "VALUES (:id, :name, :kind, :model, :max_depth, :max_agents, :options, :description, :created_at, :updated_at)"
                ).bindparams(bindparam("options", type_=JSON)),
                {
                    "id": defn["id"],
                    "name": defn["name"],
                    "kind": defn["kind"],
                    "model": defn.get("model"),
                    "max_depth": defn.get("max_depth"),
                    "max_agents": defn.get("max_agents"),
                    "options": defn.get("options"),
                    "description": defn.get("description"),
                    "created_at": defn.get("created_at", now),
                    "updated_at": defn.get("updated_at", now),
                },
            )

    async def get_engine_def(self, def_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM engine_defs WHERE id = :id"),
                        {"id": def_id},
                    )
                )
                .mappings()
                .first()
            )
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
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM engine_defs WHERE name = :name"),
                        {"name": name},
                    )
                )
                .mappings()
                .first()
            )
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
        params: dict[str, Any] = {}
        if kind is not None:
            query += " WHERE kind = :kind"
            params["kind"] = kind
        query += " ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset
        async with self._read() as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
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
        json_fields = {"options"}
        fields["updated_at"] = time.time()
        sets_parts = []
        bind_params = []
        for k in fields:
            sets_parts.append(f'"{k}" = :{k}')
            if k in json_fields:
                bind_params.append(bindparam(k, type_=JSON))
        params = dict(fields)
        params["_id"] = def_id
        stmt = text(f"UPDATE engine_defs SET {', '.join(sets_parts)} WHERE id = :_id")  # noqa: S608
        if bind_params:
            stmt = stmt.bindparams(*bind_params)
        async with self._tx() as conn:
            await conn.execute(stmt, params)

    async def delete_engine_def(self, def_id: str) -> bool:
        async with self._tx() as conn:
            result = await conn.execute(
                text("DELETE FROM engine_defs WHERE id = :id"),
                {"id": def_id},
            )
        return result.rowcount > 0

    # ── Workflow definitions ───────────────────────────────────────────

    @staticmethod
    def _decode_workflow_def(row: Any) -> dict[str, Any]:
        d = dict(row)
        if isinstance(d.get("spec_json"), str):
            try:
                d["spec_json"] = json.loads(d["spec_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    async def create_workflow_def(self, defn: dict[str, Any]) -> None:
        now = time.time()
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workflow_defs "
                    "(id, name, description, spec_json, created_at, updated_at) "
                    "VALUES (:id, :name, :description, :spec_json, :created_at, :updated_at)"
                ).bindparams(bindparam("spec_json", type_=JSON)),
                {
                    "id": defn["id"],
                    "name": defn["name"],
                    "description": defn.get("description"),
                    "spec_json": defn.get("spec_json"),
                    "created_at": defn.get("created_at", now),
                    "updated_at": defn.get("updated_at", now),
                },
            )

    async def get_workflow_def(self, def_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM workflow_defs WHERE id = :id"),
                        {"id": def_id},
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else self._decode_workflow_def(row)

    async def get_workflow_def_by_name(self, name: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM workflow_defs WHERE name = :name"),
                        {"name": name},
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else self._decode_workflow_def(row)

    async def list_workflow_defs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM workflow_defs "
                            "ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
                        ),
                        {"limit": limit, "offset": offset},
                    )
                )
                .mappings()
                .all()
            )
        return [self._decode_workflow_def(r) for r in rows]

    async def update_workflow_def(self, def_id: str, **fields: Any) -> None:
        allowed = {"name", "description", "spec_json"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Invalid workflow_def field(s): {bad}")
        json_fields = {"spec_json"}
        fields["updated_at"] = time.time()
        sets_parts = []
        bind_params = []
        for k in fields:
            sets_parts.append(f'"{k}" = :{k}')
            if k in json_fields:
                bind_params.append(bindparam(k, type_=JSON))
        params = dict(fields)
        params["_id"] = def_id
        stmt = text(f"UPDATE workflow_defs SET {', '.join(sets_parts)} WHERE id = :_id")  # noqa: S608
        if bind_params:
            stmt = stmt.bindparams(*bind_params)
        async with self._tx() as conn:
            await conn.execute(stmt, params)

    async def delete_workflow_def(self, def_id: str) -> bool:
        async with self._tx() as conn:
            result = await conn.execute(
                text("DELETE FROM workflow_defs WHERE id = :id"),
                {"id": def_id},
            )
        return result.rowcount > 0

    # ── Session controls (ADR-0085 part 1: run control plane transport) ────
    # session_controls rows are written by `li o ctl pause|resume|msg` and
    # consumed by the control poller task in cli/orchestrate/flow.py's
    # _execute_dag (same lifecycle as the heartbeat loop). Apply/stamp
    # ordering is verb-classed by the poller, not by these methods: pause/
    # resume call insert_session_control() then, once applied against the
    # executor, finalize_session_control() directly (idempotent — safe to
    # re-apply on a poller crash). message calls mark_session_control_applying()
    # before attempting the (non-idempotent) apply, then finalize_session_control()
    # (a crash between the two leaves a visible 'applying' row instead of a
    # silent double-injection risk).

    async def insert_session_control(
        self,
        *,
        session_id: str,
        verb: str,
        payload: dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> str:
        """Queue a control verb for *session_id*; returns the new control id.

        Serialised through _tx() like the other append-only session logs.
        """
        control_id = uuid.uuid4().hex
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO session_controls "
                    "(id, session_id, verb, payload, created_at, applied_at, result) "
                    "VALUES (:id, :sid, :verb, :payload, :created_at, NULL, NULL)"
                ).bindparams(bindparam("payload", type_=JSON)),
                {
                    "id": control_id,
                    "sid": session_id,
                    "verb": verb,
                    "payload": payload,
                    "created_at": created_at if created_at is not None else time.time(),
                },
            )
        return control_id

    async def list_pending_session_controls(self, session_id: str) -> list[dict[str, Any]]:
        """Unapplied controls (applied_at IS NULL) for *session_id*, oldest first.

        Includes rows mid-apply (result='applying') — the poller/status surface
        distinguish "never touched" (result IS NULL) from "a prior poller crashed
        mid-apply" (result='applying') by inspecting that field.
        """
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, session_id, verb, payload, created_at, applied_at, result "
                            "FROM session_controls "
                            "WHERE session_id = :sid AND applied_at IS NULL "
                            # id tiebreak: identical created_at floats (rapid
                            # enqueues) must not flip apply order between ticks
                            "ORDER BY created_at, id"
                        ),
                        {"sid": session_id},
                    )
                )
                .mappings()
                .all()
            )
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("payload"), str):
                try:
                    d["payload"] = json.loads(d["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result

    async def mark_session_control_applying(self, control_id: str) -> None:
        """Stamp a non-idempotent (message) control as mid-apply, before attempting it.

        applied_at stays NULL — the row remains "pending" until finalize_session_control()
        runs, so a poller crash right after this stamp is visible, not silently lost.
        """
        async with self._tx() as conn:
            await conn.execute(
                text("UPDATE session_controls SET result = 'applying' WHERE id = :id"),
                {"id": control_id},
            )

    async def finalize_session_control(self, control_id: str, *, result: str) -> None:
        """Stamp applied_at + a terminal *result* ('applied' or 'rejected:<reason>')."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE session_controls SET applied_at = :applied_at, result = :result "
                    "WHERE id = :id"
                ),
                {"applied_at": time.time(), "result": result, "id": control_id},
            )

    async def get_session_control(self, control_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM session_controls WHERE id = :id"),
                        {"id": control_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("payload"), str):
            try:
                d["payload"] = json.loads(d["payload"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
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
            "payload",
        ):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d


# ── Shared singleton accessor ─────────────────────────────────────────────────
# One open StateDB connection is reused across all hook firings for a given
# DB URL.  This avoids the per-firing connect + schema-check cost
# and is the prerequisite for session-lifecycle hook wiring.
#
# Key by normalized URL string so tests that redirect DEFAULT_DB_PATH to a
# tmp_path get their own isolated instance, and so that "sqlite:///..." and
# "postgresql+asyncpg://..." both work.

_SHARED: dict[str, StateDB] = {}
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
    # Resolve the key through StateDB's own cascade (None → LIONAGI_STATE_DB_URL
    # → DEFAULT_DB_PATH) so a monkeypatched DEFAULT_DB_PATH is honored; calling
    # normalize_state_db_url(None) directly would bypass it to the real home db.
    key = StateDB(path).url
    if key in _SHARED:
        return _SHARED[key]
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
        if key not in _SHARED:
            db = StateDB(url=key)
            await db.open()
            _SHARED[key] = db
    return _SHARED[key]


async def register_shared_db(db: StateDB) -> None:
    """Adopt a caller-owned StateDB as the shared instance, closing any prior one for its url."""
    global _SHARED_OPEN_LOCK  # noqa: PLW0603
    import contextlib

    if _SHARED_OPEN_LOCK is None:
        _SHARED_OPEN_LOCK = Lock()
    lock = _SHARED_OPEN_LOCK
    gen = _SHARED_CLOSE_GEN
    async with lock:
        if _SHARED_CLOSE_GEN != gen:
            raise RuntimeError(_SHARED_TEARDOWN_RACE)
        existing = _SHARED.get(db.url)
        if existing is not None and existing is not db:
            with contextlib.suppress(Exception):
                await existing.close()
        _SHARED[db.url] = db


def unregister_shared_db(db: StateDB) -> None:
    """Drop *db* from the shared registry iff it is the registered instance."""
    if _SHARED.get(db.url) is db:
        del _SHARED[db.url]


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
