# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

# Dialect differences, concurrency arguments and migration order are documented in
# docs/internals/state-db.md rather than inline here.

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import sqlite3
import struct
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, MetaData, bindparam, event, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.schema import CreateTable

from lionagi._paths import LIONAGI_HOME, ensure_lionagi_dir
from lionagi.config import settings
from lionagi.libs.path_safety import check_path_safe as _check_path_safe
from lionagi.ln import json_dumps as _json_dumps
from lionagi.ln.concurrency import CancelScope, Lock, get_cancelled_exc_class, shield
from lionagi.state.engine import (
    dialect_of,
    make_engine,
    make_readonly_engine,
    mask_credentials,
    mask_db_url,
    normalize_state_db_url,
)
from lionagi.state.lifecycle import LifecycleNotFoundError as _LifecycleNotFoundError
from lionagi.state.lifecycle import adapters as _lifecycle_adapters
from lionagi.state.lifecycle import policy as _lifecycle_policy
from lionagi.state.lifecycle.models import (
    ActorRecord as _ActorRecord,
)
from lionagi.state.lifecycle.models import (
    InitialStateCommand as _InitialStateCommand,
)
from lionagi.state.lifecycle.models import (
    ReasonRecord as _ReasonRecord,
)
from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService as _LifecycleService
from lionagi.state.reasons import LEGACY_IMPORTED as _LEGACY_IMPORTED
from lionagi.state.reasons import (
    PlayReasons as _PlayReasons,
)
from lionagi.state.reasons import (
    RunReasons as _RunReasons,
)
from lionagi.state.reasons import (
    ScheduleReasons as _ScheduleReasons,
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
from lionagi.state.schema_meta import definitions as _definitions_table
from lionagi.state.schema_meta import metadata
from lionagi.state.schema_meta import schedules as _schedules_table
from lionagi.state.schema_migrations import MIGRATION_COLUMNS as _MIGRATION_COLUMNS
from lionagi.state.schema_migrations import MIGRATION_CONSTRAINTS as _MIGRATION_CONSTRAINTS
from lionagi.state.schema_migrations import MIGRATION_INDEXES as _MIGRATION_INDEXES

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

_INITIAL_REASON_CODES: dict[tuple[str, str], str] = {
    ("session", "running"): _RunReasons.STARTED_OK,
    ("invocation", "running"): _RunReasons.STARTED_OK,
    ("show", "active"): _ShowReasons.ACTIVE_CREATED,
    ("show", "imported"): _LEGACY_IMPORTED,
    ("play", "pending"): _PlayReasons.PENDING_CREATED,
    ("schedule_run", "queued"): _ScheduleReasons.QUEUED_CREATED,
    ("schedule_run", "running"): _RunReasons.STARTED_OK,
    ("schedule_run", "failed"): _RunReasons.FAILED_EXCEPTION,
    ("schedule_run", "skipped"): _ScheduleReasons.SKIPPED_PRECONDITION,
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


_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB_PATH = LIONAGI_HOME / "state.db"

# Distinguishes "this write claims no due instant" from "it claims one whose value is NULL".
# Public because callers several layers up have to state which of the two they mean.
NO_CURSOR_CLAIM = object()

# Shape of the schema this code applies; ``_apply_schema`` stamps it into ``schema_meta.version`` on
# every open, so the recorded version describes the database after migrations. Bump it whenever a
# migration changes the shape a reader would see.
SCHEMA_VERSION = "4"
_SCHEMA_MIGRATION_LOCK_KEY = "lionagi.state.schema.migration"
_DISPATCHED_AT_BACKFILL_KEY = "migration.dispatched_at_backfill"
_ATTENTION_DISPOSITIONS_BACKFILL_KEY = "migration.attention_dispositions_backfill"
_IMPORTED_ROLE_LABEL_BACKFILL_KEY = "migration.imported_role_label_backfill"
_SESSION_ENDED_AT_BACKFILL_KEY = "migration.session_ended_at_backfill"
_SESSION_ENDED_AT_BACKFILL_BATCH_SIZE = 500


class SchemaTooNewError(RuntimeError):
    """``schema_meta.version`` is higher than ``SCHEMA_VERSION``; only a writable open raises it."""


class BackupNotTrustworthyError(RuntimeError):
    """A pre-rebuild backup was not trustworthy, so the rebuild aborts."""


def state_db_file() -> Path | None:
    """The local file a default ``StateDB()`` would open, or None if the store is not a file."""
    raw = settings.LIONAGI_STATE_DB_URL
    if raw is None:
        raw = DEFAULT_DB_PATH
    url = normalize_state_db_url(raw)
    if dialect_of(url) != "sqlite":
        return None
    from sqlalchemy.engine import make_url

    database = make_url(url).database
    if not database or database == ":memory:":
        return None
    return Path(database)


def read_only_open_supported() -> bool:
    """Whether ``StateDB(readonly=True)`` can open the store; an optimisation check."""
    return state_db_file() is not None


def state_db_known_absent() -> bool:
    """Whether the store a default ``StateDB()`` would open is known absent."""
    path = state_db_file()
    return path is not None and not path.exists()


# Schedule-run statuses counted as "fired and resolved" for budget bookkeeping (max_runs, one-shot
# auto-disable); the scheduler service layer's defaults must match. 'timed_out' counts, since a
# reaped run consumed real work; 'skipped' and 'running' do not.
TERMINAL_RUN_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled", "timed_out")

# Statuses counted as "a run actually executed", for health reads that must not mistake a pending
# row for proof. 'running' counts even though it is not terminal; queued, waiting and skipped are
# pending, not evidence.
EXECUTED_RUN_STATUSES: tuple[str, ...] = ("running", *TERMINAL_RUN_STATUSES)

_VALID_STATUS_SOURCES: frozenset[str] = frozenset({"executor", "agent", "admin", "system"})

_SESSION_COLUMNS = frozenset(
    {
        "cc_session_id",
        "run_id",
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
        # artifact_contract_json is fixed at session creation for the single-agent case, but a DAG's
        # roles are only known once planning finishes, after create_session. Two writer classes are
        # allowlisted, both append-only and both frozen before the work they describe runs:
        # _build_dag folds each planned leg's role defaults in at DAG-build time, and _execute_dag
        # folds a reactively spawned node's own entries in after it completes, though what was
        # expected of it was frozen before it was queued. No other writer may touch this column.
        "artifact_contract_json",
    }
)

_INVOCATION_STATUSES = frozenset(
    {
        "running",
        "completed",
        # Completion-trust gate: aggregation settles an invocation here when a child session
        # produced no commits ahead of base, no artifacts and no assistant output.
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
        # Completion-trust gate: the loop exited clean but produced no commits ahead of base and no
        # artifacts, so "ran and produced nothing" stays distinct from a verified completion.
        "completed_empty",
        "failed",
        "timed_out",
        "aborted",
        "cancelled",
    }
)
# Admin cannot mark completed/completed_empty/timed_out — those are system-determined.
ADMIN_TRANSITION_TARGETS = frozenset({"failed", "aborted", "cancelled"})

_SESSION_STATUSES = VALID_SESSION_STATUSES

# Terminal-status vocabulary, sourced from the lifecycle policy registry so it cannot drift from the
# registry's own terminal_statuses. It lives with the record schema rather than in any one CLI
# surface, since update_status() enforces it uniformly at the single write path.
TERMINAL_STATUSES_BY_ENTITY_TYPE: dict[str, frozenset[str]] = {
    entity_type: _lifecycle_policy.DEFAULT_REGISTRY.get(entity_type).terminal_statuses
    for entity_type in ("session", "invocation", "schedule_run", "show", "play", "team")
}
SESSION_TERMINAL_STATUSES = TERMINAL_STATUSES_BY_ENTITY_TYPE["session"]

# Terminal branches.status values. branches has no lifecycle-policy entry of its own, but every
# status finalize_branch() receives is a session final_status passed straight through by teardown,
# so this IS SESSION_TERMINAL_STATUSES rather than a second list that could drift.
_BRANCH_TERMINAL_STATUSES = SESSION_TERMINAL_STATUSES

INVOCATION_TERMINAL_STATUSES = TERMINAL_STATUSES_BY_ENTITY_TYPE[
    "invocation"
]  # invocations share the session terminal-status vocabulary
SCHEDULE_RUN_TERMINAL_STATUSES = TERMINAL_STATUSES_BY_ENTITY_TYPE["schedule_run"]
SHOW_TERMINAL_STATUSES = TERMINAL_STATUSES_BY_ENTITY_TYPE["show"]
# Still-in-flight play statuses; the schema layer owns this vocabulary (kill.py imports it rather
# than defining a copy).
PLAY_ACTIVE_STATUSES = frozenset(
    {"pending", "prepared", "running", "running_complete", "gated", "redoing"}
)
PLAY_TERMINAL_STATUSES = TERMINAL_STATUSES_BY_ENTITY_TYPE["play"]
TEAM_TERMINAL_STATUSES = TERMINAL_STATUSES_BY_ENTITY_TYPE["team"]

# Same-row columns update_status() may set in the same transaction as the status write, so a crash
# cannot leave a status change and a dependent column split across two. node_metadata is here
# because it carries the process markers the liveness sweeps read.
EXTRA_STATUS_WRITE_FIELDS_BY_ENTITY_TYPE: dict[str, frozenset[str]] = {
    # duration_ms is derived from ended_at and ended_at_is_approximate describes it, so both carry
    # the same requirement: they must land in the status write, never a separate earlier one.
    "session": frozenset({"ended_at", "duration_ms", "ended_at_is_approximate", "node_metadata"}),
    "invocation": frozenset({"ended_at"}),
    "schedule_run": frozenset({"ended_at", "error_detail", "exit_code"}),
    "play": frozenset({"ended_at"}),
}

# Status vocabulary, valid rather than just terminal: update_status() rejects any new_status outside
# its entity_type's set. Sourced from the lifecycle policy registry so it cannot drift from the
# statuses the schema CHECK constraints declare.
VALID_STATUSES_BY_ENTITY_TYPE: dict[str, frozenset[str]] = {
    entity_type: _lifecycle_policy.DEFAULT_REGISTRY.get(entity_type).statuses
    for entity_type in ("session", "invocation", "schedule_run", "show", "play", "team")
}


# Re-exported rather than redefined, so existing imports are unchanged; the lifecycle adapter module
# raises this same class object.
TransitionRejectedError = _lifecycle_adapters.TransitionRejectedError


_INVOCATION_KINDS = frozenset({"agent", "play", "flow", "fanout", "show-play", "engine"})
_SOURCE_KINDS = frozenset({"live", "imported_fs", "imported_codex"})

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

_DEFINITION_KINDS = frozenset({"agent", "playbook", "skill"})


def _validate_columns(fields: dict[str, Any], allowed: frozenset[str]) -> None:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Invalid column(s): {bad}")


def _to_json_column(value: Any) -> Any:
    """Serialize value to a JSON string for a TEXT column, rejecting inf, -inf and nan."""
    if value is None or isinstance(value, bytes | bytearray | memoryview):
        return value
    return _json_dumps(value, check_non_finite=True)


def _unpack_embedding(value: Any) -> list[float] | None:
    """Decode the messages.embedding little-endian float32 storage format."""
    if value is None:
        return None
    if isinstance(value, list):
        values = [float(item) for item in value]
    elif isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        if len(raw) % 4:
            raise ValueError("messages.embedding BLOB length must be a multiple of 4 bytes")
        values = list(struct.unpack(f"<{len(raw) // 4}f", raw))
    else:
        raise ValueError("messages.embedding must be a float list or packed float32 bytes")
    if not all(math.isfinite(item) for item in values):
        raise ValueError("messages.embedding values must be finite")
    return values


def _pack_embedding(value: Any) -> bytes | None:
    """Encode an embedding as packed little-endian float32 bytes."""
    if value is None:
        return None
    values = _unpack_embedding(value)
    try:
        return struct.pack(f"<{len(values)}f", *values)
    except (OverflowError, struct.error) as exc:
        raise ValueError("messages.embedding values must fit finite float32") from exc


def _validate_session_status(status: Any) -> None:
    if status is None:
        return
    if status not in VALID_SESSION_STATUSES:
        raise ValueError(
            f"Invalid session status {status!r}; "
            f"ADR-0057 vocabulary is {sorted(VALID_SESSION_STATUSES)}"
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
        # AUTOCOMMIT reads reach this listener too: the driver already runs in autocommit, but Core
        # still autobegins a logical transaction. Only a real transaction should reserve the writer
        # slot.
        if conn.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
            return
        conn.exec_driver_sql("BEGIN IMMEDIATE")


async def _restore_foreign_keys(conn, driver) -> None:
    """Turn foreign-key enforcement back on after a legacy rebuild, reading it back to confirm."""
    cancelled_exc = get_cancelled_exc_class()

    try:
        # No-op when the caller already ended its own transaction.
        await driver.rollback()
    except cancelled_exc:
        await shield(conn.invalidate)
        raise
    except Exception:  # noqa: S110 -- not fatal on its own; the read-back below is
        # what decides whether this connection is safe to hand out again.
        pass

    confirmed = False
    try:
        await driver.execute("PRAGMA foreign_keys = ON")
        await driver.commit()
        row = await (await driver.execute("PRAGMA foreign_keys")).fetchone()
        confirmed = bool(row) and row[0] == 1
    except cancelled_exc:
        await shield(conn.invalidate)
        raise
    except Exception:
        confirmed = False

    if not confirmed:
        await shield(conn.invalidate)


_log = logging.getLogger(__name__)


class StateDB:
    """Async SQLAlchemy state layer for sessions, branches, messages, and progressions."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        url: str | None = None,
        readonly: bool = False,
    ):
        raw = url if url is not None else path
        if raw is None:
            raw = settings.LIONAGI_STATE_DB_URL  # may be None
        if raw is None:
            raw = DEFAULT_DB_PATH  # module-level; tests can monkeypatch db_mod.DEFAULT_DB_PATH
        self.url = normalize_state_db_url(raw)
        self.dialect = dialect_of(self.url)  # "sqlite" | "postgresql"
        # Read-only mode skips schema application, the BEGIN IMMEDIATE write-lock event and every
        # mutating PRAGMA, opening via SQLite's own read-only URI. A missing file is a loud error,
        # never a silent create.
        self.readonly = readonly
        self._engine = None
        # Per-(kind, name) lock to serialize version increment for save_definition.
        self._definition_locks: dict[tuple[str, str], Lock] = {}
        # Connection-wide write lock: every mutating method that can share the live connection holds
        # it during its write window. On SQLite it stops coroutines racing BEGIN IMMEDIATE on the
        # shared connection; PostgreSQL paths skip it in _tx(), but it still serialises the Python-
        # side CAS in update_status.
        self._write_lock: Lock = Lock()
        # Lazily constructed so StateDB.__init__ never depends on import order inside
        # lionagi.state.lifecycle.
        self.__lifecycle_service: _LifecycleService | None = None

    def _lifecycle_service(self) -> _LifecycleService:
        if self.__lifecycle_service is None:
            self.__lifecycle_service = _LifecycleService(self)
        return self.__lifecycle_service

    async def _initialize_managed_entity_in_tx(
        self,
        conn,
        *,
        entity_type: str,
        entity_id: str,
        status: str,
        actor_id: str,
    ) -> None:
        reason_code = _INITIAL_REASON_CODES.get((entity_type, status))
        if reason_code is None:
            # Compatibility repositories may insert historical terminal rows directly; those are not
            # declared lifecycle initial states and must not get a fabricated creation event.
            return
        policy = _lifecycle_policy.DEFAULT_REGISTRY.get(entity_type)
        await conn.execute(
            text(
                f"UPDATE {policy.table} SET status_reason_code = :reason_code, "  # noqa: S608
                "status_reason_summary = :reason_summary, "
                "status_evidence_refs = :evidence_refs WHERE id = :entity_id"
            ).bindparams(bindparam("evidence_refs", type_=JSON)),
            {
                "reason_code": reason_code,
                "reason_summary": "",
                "evidence_refs": [],
                "entity_id": entity_id,
            },
        )
        await self._lifecycle_service().initialize_in_transaction(
            conn,
            _InitialStateCommand(
                entity_type=entity_type,
                entity_id=entity_id,
                status=status,
                reason=_ReasonRecord(code=reason_code),
                actor=_ActorRecord(type="system", id=actor_id),
            ),
        )

    # backward-compat path property

    @property
    def path(self) -> Path | None:
        if self.dialect == "sqlite":
            # sqlite+aiosqlite:///abs/path  or  sqlite+aiosqlite:///:memory:
            suffix = self.url.split(":///", 1)[1] if ":///" in self.url else None
            if suffix and suffix != ":memory:":
                return Path(suffix)
            return Path(":memory:") if suffix == ":memory:" else None
        return None

    # Connection lifecycle

    async def open(self) -> None:
        if self._engine is not None:
            return
        if self.dialect == "sqlite":
            p = self.path
            if p is not None and str(p) != ":memory:":
                if self.readonly:
                    if not p.exists():
                        # A store URL with no scheme is read as a filesystem path, so a mis-set
                        # credentialed URL ends up here verbatim. Masked at the raise rather than
                        # only where it is printed, since the exception travels to readers this
                        # module does not know about.
                        raise FileNotFoundError(
                            f"state.db not found at {mask_credentials(str(p))} — read-only "
                            "open requires an existing database file (it will never be "
                            "created)"
                        )
                else:
                    ensure_lionagi_dir(p.parent)
        if self.readonly:
            # No make_engine(), no _install_begin_immediate(), no _apply_schema(): every one of
            # those mutates the file.
            self._engine = make_readonly_engine(self.url)
            return
        self._engine = make_engine(self.url)
        if self.dialect == "sqlite":
            _install_begin_immediate(self._engine.sync_engine)
        await self._apply_schema()

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def __aenter__(self) -> StateDB:
        try:
            await self.open()
        except BaseException:
            # __aexit__ is not entered when __aenter__ fails, so dispose the partially opened engine
            # here rather than letting its driver worker outlive the failure.
            with CancelScope(shield=True):
                await self.close()
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # Internal connection helpers

    @asynccontextmanager
    async def _read(self):
        async with self._engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            yield conn

    @asynccontextmanager
    async def read_snapshot(self):
        """Yield one non-blocking, repeatable read snapshot.

        SQLite's legacy driver mode does not start a transaction for SELECT,
        so ``conn.begin()`` alone would still let successive reads observe
        different WAL commits. An explicit deferred ``BEGIN`` pins the WAL
        snapshot without reserving the writer slot. PostgreSQL needs
        REPEATABLE READ because its default READ COMMITTED isolation takes a
        new snapshot for every statement.
        """
        async with self._engine.connect() as conn:
            if self.dialect == "sqlite":
                # AUTOCOMMIT suppresses the writable engine's BEGIN IMMEDIATE
                # event. The explicit deferred BEGIN below is read-only and
                # therefore remains compatible with a concurrent WAL writer.
                conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.exec_driver_sql("BEGIN")
                try:
                    yield conn
                finally:
                    if conn.in_transaction():
                        await conn.exec_driver_sql("ROLLBACK")
                        await conn.rollback()
            else:
                conn = await conn.execution_options(isolation_level="REPEATABLE READ")
                async with conn.begin():
                    await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    yield conn

    @asynccontextmanager
    async def _read_connection(self, connection: AsyncConnection | None = None):
        """Use a caller-owned snapshot connection or an ordinary one-shot read."""
        if connection is not None:
            yield connection
            return
        async with self._read() as conn:
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

    # Public query surface, portable across both dialects. Accepts the legacy qmark (?) form with a
    # sequence, or named (:name) SQL with a dict. Rows come back as plain dicts with JSON columns
    # left as stored, so callers keep their own decode. For multi-statement atomic work use
    # transaction().

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
        # Maintenance SQL runs on sqlite's raw driver connection for true autocommit: SQLAlchemy's
        # AUTOCOMMIT option does not clear the aiosqlite adapter's implicit transaction, which
        # blocks VACUUM and wal_checkpoint.
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
        # WAL checkpoint is sqlite-only maintenance and, like VACUUM, must bypass the adapter's
        # implicit transaction. Returns (busy, log_pages, checkpointed), or None off sqlite.
        if self.dialect != "sqlite":
            return None
        mode = mode.upper()
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError(f"invalid wal_checkpoint mode: {mode!r}")
        row = await self._raw_sqlite_exec(f"PRAGMA wal_checkpoint({mode})", fetch=True)
        return tuple(row) if row is not None else None

    # Schema management

    def _raise_if_schema_too_new(self, recorded: str | None) -> None:
        if recorded is None:
            return
        try:
            recorded_n = int(recorded)
        except (TypeError, ValueError):
            # Not a version this code can order against its own, so there is no
            # downgrade to detect. Leave it to the stamp below.
            return
        if recorded_n <= int(SCHEMA_VERSION):
            return
        # Named for an operator, so masked: on a server-backed store this is the connection URL, and
        # this refusal fires on a routine upgrade-order mistake.
        where = (
            mask_credentials(str(self.path))
            if self.dialect == "sqlite" and self.path is not None
            else mask_db_url(self.url)
        )
        raise SchemaTooNewError(
            f"{where} records schema version {recorded} but this version of lionagi "
            f"applies schema version {SCHEMA_VERSION}. It was written by a later "
            "release whose shape this code cannot verify, and opening it for writing "
            "would migrate it against that shape and record the lower version. "
            "Upgrade lionagi to open it, or open it read-only to inspect it."
        )

    async def _refuse_newer_schema(self, conn) -> None:
        """Refuse to open a database stamped with a version above SCHEMA_VERSION."""
        if self.dialect == "postgresql":
            # The version table may not exist yet, so a row lock cannot serialize first-time schema
            # creation; this database-local lock predates any schema object.
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": _SCHEMA_MIGRATION_LOCK_KEY},
            )
        if not await conn.run_sync(lambda c: inspect(c).has_table("schema_meta")):
            return
        query = "SELECT value FROM schema_meta WHERE key = 'version'"
        if self.dialect == "postgresql":
            query += " FOR UPDATE"
        row = (await conn.execute(text(query))).mappings().first()
        self._raise_if_schema_too_new(row["value"] if row else None)

    async def _refuse_newer_sqlite_schema(self, driver) -> None:
        """Check the version through a raw driver holding BEGIN IMMEDIATE."""
        table = await (
            await driver.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
            )
        ).fetchone()
        if table is None:
            return
        row = await (
            await driver.execute("SELECT value FROM schema_meta WHERE key = 'version'")
        ).fetchone()
        self._raise_if_schema_too_new(row[0] if row else None)

    async def _apply_schema(self) -> None:
        await self._reconcile_columns()
        if self.dialect == "sqlite":
            await self._rebuild_legacy_sessions_table()
            # existing DBs created before flow_yaml was added carry a
            # 4-value CHECK on schedules.action_kind that omits 'flow_yaml'.
            await self._drop_legacy_action_kind_check()
            # Existing DBs, including ones already rebuilt to admit 'flow_yaml', carry a CHECK on
            # schedules.action_kind that omits 'command'.
            await self._drop_legacy_schedules_command_check()
            # Existing DBs carry a CHECK on schedules.trigger_type that
            # omits 'at' (the declarative ScheduleSet absolute-time trigger).
            await self._drop_legacy_schedules_trigger_type_check()
            # existing DBs created before the completion-trust gate carry a
            # 6-value CHECK on invocations.status that omits 'completed_empty'.
            await self._drop_legacy_invocations_status_check()
            # Existing DBs carry a 5-value CHECK on schedule_runs.status and a NOT NULL schedule_id,
            # from before schedule_runs was generalized into the task-application entity.
            await self._drop_legacy_schedule_runs_check()
            # Existing DBs carry a 2-value CHECK on definitions.kind that
            # omits 'skill', from before the skill editor.
            await self._drop_legacy_definitions_kind_check()
        async with self._engine.begin() as conn:
            await self._refuse_newer_schema(conn)
            await conn.run_sync(metadata.create_all)
            # Before _reconcile_indexes, which recreates the unique index on
            # attention_disposition_history.sequence: that would fail against the DEFAULT 0
            # placeholder every pre-existing row shares until this backfill gives each a distinct
            # value.
            await self._backfill_attention_dispositions_once(conn)
            await self._reconcile_indexes(conn)
            # After create_all, which guarantees sessions exists for a store that never had it, and
            # before any write carrying a value the pre-existing CHECK does not name.
            await self._reconcile_constraints(conn)
            await conn.execute(
                text(
                    "UPDATE engine_runs SET parent_session_id = session_id "
                    "WHERE parent_session_id IS NULL AND session_id IS NOT NULL "
                    "AND EXISTS (SELECT 1 FROM sessions WHERE sessions.id = engine_runs.session_id)"
                )
            )
            await self._backfill_dispatched_at_once(conn)
            await self._backfill_imported_role_label_once(conn)
            # Seed immutable reference rows; ON CONFLICT DO NOTHING is safe to re-run on every open
            # because the rows are identity-stable. The version row is the exception and takes DO
            # UPDATE, or a migrated database keeps reporting the version it was created at. The
            # update only ever raises the recorded version, since a database stamped higher is
            # refused at open.
            await conn.execute(
                text(
                    "INSERT INTO schema_meta (key, value) VALUES ('version', :version) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
                ),
                {"version": SCHEMA_VERSION},
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

        # Historical terminal sessions can be numerous, so the repair does not hold the schema
        # transaction or SQLite's writer lock. Each batch commits independently, and a crash leaves
        # the completion marker absent so the next open resumes.
        await self._backfill_session_ended_at_once()

    _MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = _MIGRATION_COLUMNS
    _MIGRATION_INDEXES: dict[str, tuple[str, ...]] = _MIGRATION_INDEXES
    _MIGRATION_CONSTRAINTS: dict[str, tuple[str, ...]] = _MIGRATION_CONSTRAINTS

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
            except Exception as exc:  # noqa: BLE001
                _log.error(
                    "failed to inspect migration columns for table %r: %r",
                    table,
                    exc,
                    exc_info=True,
                )
                raise
            for name, defn in columns:
                if name not in existing:
                    add_column = f"ALTER TABLE {table} ADD COLUMN {name} {defn}"
                    if self.dialect == "postgresql":
                        add_column = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {defn}"
                    try:
                        async with self._engine.begin() as conn:
                            await self._refuse_newer_schema(conn)
                            await conn.execute(text(add_column))
                    except OperationalError:
                        # SQLite has no ADD COLUMN IF NOT EXISTS, and another process may commit the
                        # same migration between our inspection and our ALTER. Only suppress the
                        # error when a fresh inspection proves the column now exists.
                        if self.dialect != "sqlite":
                            raise
                        async with self._engine.connect() as conn:
                            reconciled = await conn.run_sync(
                                lambda c, t=table, n=name: (
                                    n in {col["name"] for col in inspect(c).get_columns(t)}
                                )
                            )
                        if not reconciled:
                            raise

    async def _reconcile_indexes(self, conn) -> None:
        """Create indexes that ``metadata.create_all`` cannot add to existing tables."""
        for statement in self._MIGRATION_INDEXES.get(self.dialect, ()):
            await conn.execute(text(statement))

    async def _reconcile_constraints(self, conn) -> None:
        """Widen CHECK constraints that ``metadata.create_all`` cannot alter; PostgreSQL only."""
        for statement in self._MIGRATION_CONSTRAINTS.get(self.dialect, ()):
            await conn.execute(text(statement))

    async def _backfill_dispatched_at_once(self, conn) -> None:
        """Backfill legacy rows exactly once, even if the column predates this release."""
        claimed = await conn.execute(
            text(
                "INSERT INTO schema_meta (key, value) VALUES (:key, '1') "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"key": _DISPATCHED_AT_BACKFILL_KEY},
        )
        if claimed.rowcount:
            await self._backfill_dispatched_at(conn)

    async def _backfill_session_ended_at_once(self) -> None:
        """Repair historical terminal sessions in bounded, resumable batches."""
        # Completed databases take the ordinary read connection and return: opening StateDB must not
        # consume an unrelated write transaction merely to discover a durable marker already exists.
        async with self._read() as conn:
            marker = (
                await conn.execute(
                    text("SELECT 1 FROM schema_meta WHERE key = :key"),
                    {"key": _SESSION_ENDED_AT_BACKFILL_KEY},
                )
            ).first()
        if marker is not None:
            return

        while True:
            async with self._tx() as conn:
                await self._refuse_newer_schema(conn)
                marker = (
                    await conn.execute(
                        text("SELECT 1 FROM schema_meta WHERE key = :key"),
                        {"key": _SESSION_ENDED_AT_BACKFILL_KEY},
                    )
                ).first()
                if marker is not None:
                    return

                repaired = await self._backfill_session_ended_at_batch(conn)
                if repaired:
                    continue

                await conn.execute(
                    text(
                        "INSERT INTO schema_meta (key, value) VALUES (:key, '1') "
                        "ON CONFLICT (key) DO NOTHING"
                    ),
                    {"key": _SESSION_ENDED_AT_BACKFILL_KEY},
                )
                return

    async def _backfill_session_ended_at_batch(self, conn) -> int:
        """Approximate at most one batch of missing historical end times."""
        if self.dialect == "sqlite":
            query = (
                "SELECT id, created_at, updated_at, started_at, last_message_at "
                "FROM sessions INDEXED BY idx_sessions_terminal_missing_end "
                "WHERE ended_at IS NULL AND status IN "
                "('completed','completed_empty','failed','timed_out','aborted','cancelled') "
                "ORDER BY id LIMIT :limit"
            )
        else:
            query = (
                "SELECT id, created_at, updated_at, started_at, last_message_at "
                "FROM sessions WHERE ended_at IS NULL AND status IN "
                "('completed','completed_empty','failed','timed_out','aborted','cancelled') "
                "ORDER BY id LIMIT :limit FOR UPDATE"
            )
        rows = (
            (
                await conn.execute(
                    text(query),
                    {"limit": _SESSION_ENDED_AT_BACKFILL_BATCH_SIZE},
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return 0

        repairs = []
        for row in rows:
            evidence = [
                value
                for value in (
                    row["updated_at"],
                    row["last_message_at"],
                    row["started_at"],
                    row["created_at"],
                )
                if isinstance(value, int | float)
            ]
            # created_at is NOT NULL in every supported schema, but keep a
            # defensive fallback for malformed hand-built legacy stores.
            repairs.append(
                {
                    "id": row["id"],
                    "ended_at": max(evidence) if evidence else time.time(),
                }
            )

        await conn.execute(
            text(
                # duration_ms is cleared by the same write that sets the bit. A legacy row can carry
                # a duration from an older writer, and keeping it beside an approximate end asserts
                # a measured length for an end nobody measured.
                "UPDATE sessions SET ended_at = :ended_at, "
                "ended_at_is_approximate = 1, duration_ms = NULL "
                "WHERE id = :id AND ended_at IS NULL AND status IN "
                "('completed','completed_empty','failed','timed_out','aborted','cancelled')"
            ),
            repairs,
        )
        return len(rows)

    async def _backfill_dispatched_at(self, conn) -> None:
        """Stamp ``dispatched_at`` on pre-existing running rows once, so recovery skips them."""
        await conn.execute(
            text(
                "UPDATE schedule_runs SET dispatched_at = fired_at "
                "WHERE status = 'running' AND dispatched_at IS NULL "
                "AND schedule_id IS NOT NULL"
            )
        )

    async def _backfill_imported_role_label_once(self, conn) -> None:
        """Clear the engine label off imported rows' role field, exactly once."""
        claimed = await conn.execute(
            text(
                "INSERT INTO schema_meta (key, value) VALUES (:key, '1') "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"key": _IMPORTED_ROLE_LABEL_BACKFILL_KEY},
        )
        if claimed.rowcount:
            await self._backfill_imported_role_label(conn)

    async def _backfill_imported_role_label(self, conn) -> None:
        """Null out ``agent_name`` on rows imported from a desktop transcript."""
        await conn.execute(
            text(
                "UPDATE branches SET agent_name = NULL WHERE session_id IN "
                "(SELECT id FROM sessions WHERE source_kind = 'imported_codex')"
            )
        )
        await conn.execute(
            text("UPDATE sessions SET agent_name = NULL WHERE source_kind = 'imported_codex'")
        )

    async def _backfill_attention_dispositions_once(self, conn) -> None:
        """Backfill the attention-disposition revision and sequence columns exactly once."""
        claimed = await conn.execute(
            text(
                "INSERT INTO schema_meta (key, value) VALUES (:key, '1') "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"key": _ATTENTION_DISPOSITIONS_BACKFILL_KEY},
        )
        if claimed.rowcount:
            await self._backfill_attention_dispositions(conn)

    async def _backfill_attention_dispositions(self, conn) -> None:
        """Fill in the revision/sequence values ``metadata.create_all`` left as inert defaults."""
        hist_rows = (
            (
                await conn.execute(
                    text(
                        "SELECT id, item_id, new_state FROM attention_disposition_history "
                        "ORDER BY created_at ASC, id ASC"
                    )
                )
            )
            .mappings()
            .all()
        )
        counts: dict[str, int] = {}
        latest_state: dict[str, str] = {}
        for offset, row in enumerate(hist_rows, start=1):
            counts[row["item_id"]] = counts.get(row["item_id"], 0) + 1
            latest_state[row["item_id"]] = row["new_state"]
            await conn.execute(
                text("UPDATE attention_disposition_history SET sequence = :seq WHERE id = :id"),
                {"seq": offset, "id": row["id"]},
            )

        active_rows = (
            (await conn.execute(text("SELECT item_id FROM attention_dispositions")))
            .mappings()
            .all()
        )
        for row in active_rows:
            item_id = row["item_id"]
            revision = counts.get(item_id) or 1
            await conn.execute(
                text("UPDATE attention_dispositions SET revision = :rev WHERE item_id = :id"),
                {"rev": revision, "id": item_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO attention_disposition_revisions (item_id, revision) "
                    "VALUES (:id, :rev) ON CONFLICT (item_id) DO NOTHING"
                ),
                {"id": item_id, "rev": revision},
            )

        active_ids = {row["item_id"] for row in active_rows}
        for item_id, state in latest_state.items():
            if item_id in active_ids or state != "open":
                continue
            await conn.execute(
                text(
                    "INSERT INTO attention_disposition_revisions (item_id, revision) "
                    "VALUES (:id, :rev) ON CONFLICT (item_id) DO NOTHING"
                ),
                {"id": item_id, "rev": counts[item_id]},
            )

    async def _rebuild_check_constraint(self, table: str, already_rebuilt, rebuild) -> None:
        """Run a legacy CHECK-constraint table rebuild, tolerant of a concurrent winner."""
        try:
            await rebuild()
        # Five of the six rebuilds execute through the raw aiosqlite driver, which raises
        # sqlite3.OperationalError rather than SQLAlchemy's wrapper, so both types are caught here.
        except (OperationalError, sqlite3.OperationalError) as original_error:
            if self.dialect != "sqlite":
                raise
            # The reinspection read is itself a fresh connection contending for the same schema
            # lock, so under deep enough contention it can raise too. That must not surface as a
            # different, less informative crash than the write failure being handled.
            try:
                async with self._engine.connect() as conn:
                    row = (
                        (
                            await conn.execute(
                                text(
                                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"
                                ),
                                {"t": table},
                            )
                        )
                        .mappings()
                        .first()
                    )
            except (OperationalError, sqlite3.OperationalError):
                raise original_error from None
            if not already_rebuilt(row["sql"] if row is not None else None):
                raise

    _LEGACY_SESSION_STATUS_CHECK_MARKER = "'running', 'completed', 'failed', 'aborted'"
    # Present only in a sessions CREATE SQL predating transcript-import provenance; its absence
    # beside a source_kind CHECK marks a DB that would reject 'imported_codex'.
    _NARROW_SOURCE_KIND_MARKER = "'live', 'imported_fs')"
    _NARROW_INVOCATION_KIND_MARKER = "'show-play')"

    @classmethod
    def _sessions_rebuild_needed(cls, create_sql: str) -> bool:
        """Whether a sessions CREATE SQL still carries either legacy CHECK."""
        if cls._LEGACY_SESSION_STATUS_CHECK_MARKER in create_sql:
            return True
        return (
            cls._NARROW_SOURCE_KIND_MARKER in create_sql
            or cls._NARROW_INVOCATION_KIND_MARKER in create_sql
        )

    async def _rebuild_legacy_sessions_table(self) -> None:
        """Rebuild sessions if it carries either legacy CHECK: status vocabulary, or source_kind."""
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
        if not self._sessions_rebuild_needed(create_sql):
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

        async def _rebuild() -> None:
            async with self._engine.connect() as conn:
                driver = (await conn.get_raw_connection()).driver_connection
                try:
                    # The OFF pragma is inside the try, not above it: it takes effect at its own
                    # await, so anything interrupting after it, cancellation included, must still
                    # reach the finally.
                    await driver.execute("PRAGMA foreign_keys = OFF")
                    # The commit closes any open transaction rather than making the pragma
                    # effective. The pragma must run on the raw driver: open() installs BEGIN
                    # IMMEDIATE on every sqlite engine, so setting it through an engine-level
                    # begin() block leaves it a no-op.
                    await driver.commit()
                    await driver.execute("BEGIN IMMEDIATE")
                    try:
                        await self._refuse_newer_sqlite_schema(driver)
                        # These are the foreign keys the declared schema carries, so a rebuilt table
                        # ends up with the same constraints a freshly created one gets. They are
                        # what makes the OFF pragma load-bearing: sqlite accepts a forward reference
                        # to a missing table, but with enforcement on every statement against the
                        # child fails while the parent is absent, including the copy below and
                        # including rows whose FK column is NULL.
                        await driver.execute(
                            """
                            CREATE TABLE sessions_new (
                              id              TEXT    PRIMARY KEY,
                              cc_session_id   TEXT,
                              run_id          TEXT,
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
                                                  ('agent', 'play', 'flow', 'fanout', 'show-play', 'engine')
                                              ),
                              show_topic      TEXT,
                              show_play_name  TEXT,
                              artifacts_path  TEXT,
                              source_kind     TEXT    DEFAULT 'live' CHECK(
                                                source_kind IS NULL
                                                OR source_kind IN
                                                  ('live', 'imported_fs', 'imported_codex')
                                              ),
                              status          TEXT,
                              started_at      REAL,
                              ended_at        REAL,
                              ended_at_is_approximate INTEGER NOT NULL DEFAULT 0,
                              last_message_at REAL,
                              current_phase   TEXT,
                              invocation_id   TEXT    REFERENCES invocations(id),
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
                        await driver.execute(insert_sql)
                        await driver.execute("DROP TABLE sessions")
                        await driver.execute("ALTER TABLE sessions_new RENAME TO sessions")
                        for idx_sql in index_sqls:
                            await driver.execute(idx_sql)
                        await driver.commit()
                    except BaseException:
                        await driver.rollback()
                        raise
                finally:
                    await _restore_foreign_keys(conn, driver)

        await self._rebuild_check_constraint(
            "sessions",
            lambda sql: sql is None or not self._sessions_rebuild_needed(sql),
            _rebuild,
        )

    # Substring present only in the current schedules CREATE SQL;
    # its absence indicates a legacy DB whose action_kind CHECK needs rebuilding.
    _LEGACY_SCHEDULES_FLOW_YAML_MARKER = "'flow_yaml'"

    async def _drop_legacy_action_kind_check(self) -> None:
        """Rebuild ``schedules`` if it still carries the legacy action_kind CHECK."""
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

        # Derive the rebuild DDL from the canonical schema_meta Table, so this migration path cannot
        # drift from the live schema.
        rebuild_table = _schedules_table.to_metadata(MetaData(), name="schedules_new")
        create_stmt = str(CreateTable(rebuild_table).compile(dialect=self._engine.dialect))

        # ``schedules`` is an FK target with ON DELETE CASCADE, so dropping it under enforced
        # foreign keys cascades away schedule_runs rows even after they are copied across. `PRAGMA
        # foreign_keys` is a no-op inside a pending transaction, so it is toggled on the raw driver
        # connection; see docs/internals/state-db.md.
        async def _rebuild() -> None:
            async with self._engine.connect() as conn:
                driver = (await conn.get_raw_connection()).driver_connection
                try:
                    # The OFF pragma is inside the try, not above it: it takes effect at its own
                    # await, so anything interrupting after it, cancellation included, must still
                    # reach the finally.
                    await driver.execute("PRAGMA foreign_keys = OFF")
                    await driver.commit()
                    await driver.execute("BEGIN IMMEDIATE")
                    try:
                        await self._refuse_newer_sqlite_schema(driver)
                        await driver.execute(create_stmt)
                        insert_sql = f"INSERT INTO schedules_new ({col_list}) SELECT {col_list} FROM schedules"  # noqa: S608
                        await driver.execute(insert_sql)
                        await driver.execute("DROP TABLE schedules")
                        await driver.execute("ALTER TABLE schedules_new RENAME TO schedules")
                        for idx_sql in index_sqls:
                            await driver.execute(idx_sql)
                        await driver.commit()
                    except BaseException:
                        await driver.rollback()
                        raise
                finally:
                    await _restore_foreign_keys(conn, driver)

        await self._rebuild_check_constraint(
            "schedules",
            lambda sql: sql is not None and self._LEGACY_SCHEDULES_FLOW_YAML_MARKER in sql,
            _rebuild,
        )

    # Substring present only in the widened schedules CREATE SQL. Distinct from the flow_yaml marker
    # above: a DB already rebuilt to admit 'flow_yaml' carries that one and would otherwise never
    # re-run to pick up 'command' too.
    _LEGACY_SCHEDULES_COMMAND_MARKER = "'command'"

    async def _drop_legacy_schedules_command_check(self) -> None:
        """Rebuild ``schedules`` if its action_kind CHECK still omits 'command'."""
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
        if self._LEGACY_SCHEDULES_COMMAND_MARKER in create_sql:
            # Table was already created / rebuilt with 'command' in the CHECK.
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

        # Derive the rebuild DDL from the canonical schema_meta Table, so this migration path cannot
        # drift from the live schema.
        rebuild_table = _schedules_table.to_metadata(MetaData(), name="schedules_new")
        create_stmt = str(CreateTable(rebuild_table).compile(dialect=self._engine.dialect))

        # ``schedules`` is an FK target with ON DELETE CASCADE, so dropping it under enforced
        # foreign keys cascades away schedule_runs rows even after they are copied across. `PRAGMA
        # foreign_keys` is a no-op inside a pending transaction, so it is toggled on the raw driver
        # connection; see docs/internals/state-db.md.
        async def _rebuild() -> None:
            async with self._engine.connect() as conn:
                driver = (await conn.get_raw_connection()).driver_connection
                try:
                    # The OFF pragma is inside the try, not above it: it takes effect at its own
                    # await, so anything interrupting after it, cancellation included, must still
                    # reach the finally.
                    await driver.execute("PRAGMA foreign_keys = OFF")
                    await driver.commit()
                    await driver.execute("BEGIN IMMEDIATE")
                    try:
                        await self._refuse_newer_sqlite_schema(driver)
                        await driver.execute(create_stmt)
                        insert_sql = f"INSERT INTO schedules_new ({col_list}) SELECT {col_list} FROM schedules"  # noqa: S608
                        await driver.execute(insert_sql)
                        await driver.execute("DROP TABLE schedules")
                        await driver.execute("ALTER TABLE schedules_new RENAME TO schedules")
                        for idx_sql in index_sqls:
                            await driver.execute(idx_sql)
                        await driver.commit()
                    except BaseException:
                        await driver.rollback()
                        raise
                finally:
                    await _restore_foreign_keys(conn, driver)

        await self._rebuild_check_constraint(
            "schedules",
            lambda sql: sql is not None and self._LEGACY_SCHEDULES_COMMAND_MARKER in sql,
            _rebuild,
        )

    # Substring present only in the widened schedules CREATE SQL; its absence indicates a legacy DB
    # whose trigger_type CHECK still omits 'at'.
    _LEGACY_SCHEDULES_TRIGGER_TYPE_MARKER = "'at'"

    async def _drop_legacy_schedules_trigger_type_check(self) -> None:
        """Rebuild ``schedules`` if its trigger_type CHECK still omits 'at'."""
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
        if self._LEGACY_SCHEDULES_TRIGGER_TYPE_MARKER in create_sql:
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

        rebuild_table = _schedules_table.to_metadata(MetaData(), name="schedules_new")
        create_stmt = str(CreateTable(rebuild_table).compile(dialect=self._engine.dialect))

        async def _rebuild() -> None:
            async with self._engine.connect() as conn:
                driver = (await conn.get_raw_connection()).driver_connection
                try:
                    # The OFF pragma is inside the try, not above it: it takes effect at its own
                    # await, so anything interrupting after it, cancellation included, must still
                    # reach the finally.
                    await driver.execute("PRAGMA foreign_keys = OFF")
                    await driver.commit()
                    await driver.execute("BEGIN IMMEDIATE")
                    try:
                        await self._refuse_newer_sqlite_schema(driver)
                        await driver.execute(create_stmt)
                        insert_sql = f"INSERT INTO schedules_new ({col_list}) SELECT {col_list} FROM schedules"  # noqa: S608
                        await driver.execute(insert_sql)
                        await driver.execute("DROP TABLE schedules")
                        await driver.execute("ALTER TABLE schedules_new RENAME TO schedules")
                        for idx_sql in index_sqls:
                            await driver.execute(idx_sql)
                        await driver.commit()
                    except BaseException:
                        await driver.rollback()
                        raise
                finally:
                    await _restore_foreign_keys(conn, driver)

        await self._rebuild_check_constraint(
            "schedules",
            lambda sql: sql is not None and self._LEGACY_SCHEDULES_TRIGGER_TYPE_MARKER in sql,
            _rebuild,
        )

    # Substring present only in the post-completion-trust-gate invocations CREATE SQL; its absence
    # indicates a status CHECK that needs rebuilding to admit 'completed_empty'.
    _LEGACY_INVOCATIONS_STATUS_MARKER = "'completed_empty'"

    async def _drop_legacy_invocations_status_check(self) -> None:
        """Rebuild ``invocations`` if its status CHECK still omits 'completed_empty'."""
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

        # ``invocations`` is an FK target, so dropping it while `PRAGMA foreign_keys` is enforced
        # raises a constraint failure even with rows safely copied into the new table.
        # engine.begin() opens its transaction before the first statement and SQLite treats the
        # pragma as a no-op inside one, so the toggle goes through the raw driver connection
        # instead.
        async def _rebuild() -> None:
            async with self._engine.connect() as conn:
                driver = (await conn.get_raw_connection()).driver_connection
                try:
                    # The OFF pragma is inside the try, not above it: it takes effect at its own
                    # await, so anything interrupting after it, cancellation included, must still
                    # reach the finally.
                    await driver.execute("PRAGMA foreign_keys = OFF")
                    # The commit closes any open transaction, then BEGIN IMMEDIATE makes the whole
                    # rebuild one atomic transaction: DDL autocommits per statement otherwise, so a
                    # crash between DROP and RENAME would strand the data in invocations_new.
                    await driver.commit()
                    await driver.execute("BEGIN IMMEDIATE")
                    await self._refuse_newer_sqlite_schema(driver)
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
                except BaseException:
                    await driver.rollback()
                    raise
                finally:
                    await _restore_foreign_keys(conn, driver)

        await self._rebuild_check_constraint(
            "invocations",
            lambda sql: sql is not None and self._LEGACY_INVOCATIONS_STATUS_MARKER in sql,
            _rebuild,
        )

    async def _backup_before_rebuild(self, label: str) -> None:
        """Copy the on-disk state.db aside before an in-place table rebuild; a no-op off SQLite."""
        if self.dialect != "sqlite":
            return
        p = self.path
        if p is None or str(p) == ":memory:" or not p.exists():
            return
        # In WAL mode, recently committed transactions can live only in the -wal sidecar until a
        # checkpoint, so a raw file copy taken without checkpointing can silently omit them and
        # defeat the rollback guarantee. TRUNCATE asks for a full checkpoint but does not promise
        # one: when it cannot get the locks it falls back and reports busy, so the result is read
        # rather than discarded and a partial checkpoint refuses.
        result = await self.checkpoint("TRUNCATE")
        if result is None:
            # checkpoint() returns None for a non-sqlite dialect or for a pragma read that produced
            # no row. The dialect is ruled out above, so only the failed read is left, and it says
            # nothing about whether the WAL was folded in.
            raise BackupNotTrustworthyError(
                f"cannot back up {mask_credentials(str(p))} before the {label} rebuild: "
                "wal_checkpoint(TRUNCATE) returned no row, so whether the write-ahead "
                "log was folded into the database file is unknown. A file copy would "
                "not be a verifiable backup. Retry, and check the database is readable."
            )
        busy, log_pages, checkpointed = result
        if busy or log_pages != checkpointed:
            raise BackupNotTrustworthyError(
                f"cannot back up {mask_credentials(str(p))} before the {label} rebuild: "
                f"wal_checkpoint(TRUNCATE) reported busy={busy}, "
                f"{checkpointed} of {log_pages} WAL pages checkpointed. Committed data "
                "may still be in the write-ahead log, so a file copy would not be a "
                "complete backup. Stop other writers to this database and retry."
            )
        backup_path = p.with_name(f"{p.name}.pre-{label}.{int(time.time())}.bak")
        shutil.copy2(p, backup_path)
        # Not a snapshot: a writer committing between the checkpoint above and this copy lands in
        # the WAL again, and those frames are not in the file just written. The check above bounds
        # what was missed to writes concurrent with the backup itself, which is a narrower window
        # rather than no window.

    # Substring present only in the current schedule_runs CREATE SQL; its absence indicates a legacy
    # DB still carrying the 5-value CHECK and a NOT NULL schedule_id.
    _LEGACY_SCHEDULE_RUNS_QUEUE_MARKER = "'waiting_dependency'"

    async def _drop_legacy_schedule_runs_check(self) -> None:
        """Rebuild ``schedule_runs`` if it still carries the legacy status CHECK."""
        if self.dialect != "sqlite":
            return
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='table' AND name='schedule_runs'"
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["sql"] is None:
            return
        create_sql: str = row["sql"]
        if self._LEGACY_SCHEDULE_RUNS_QUEUE_MARKER in create_sql:
            # Table was already created / rebuilt with the widened CHECK.
            return

        await self._backup_before_rebuild("schedule_runs")

        async with self._engine.connect() as conn:
            index_rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='index' AND tbl_name='schedule_runs' AND sql IS NOT NULL"
                        )
                    )
                )
                .mappings()
                .all()
            )
            index_sqls = [r["sql"] for r in index_rows]

            # DROP TABLE also drops the table's triggers; capture them for
            # replay alongside the indexes.
            trigger_rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='trigger' AND tbl_name='schedule_runs' AND sql IS NOT NULL"
                        )
                    )
                )
                .mappings()
                .all()
            )
            trigger_sqls = [r["sql"] for r in trigger_rows]

            cols_rows = (
                (await conn.execute(text("PRAGMA table_info(schedule_runs)"))).mappings().all()
            )
            cols = [r["name"] for r in cols_rows]
        col_list = ", ".join(cols)

        # ``schedule_runs`` is both an FK source and an FK target, the same complication as the
        # invocations rebuild, so it uses the same hand-kept-literal and raw-driver-pragma technique
        # rather than to_metadata(), which cannot resolve cross-table foreign keys from a single-
        # table MetaData. The literal mirrors schema.sql, and parity is test-enforced.
        async def _rebuild() -> None:
            async with self._engine.connect() as conn:
                driver = (await conn.get_raw_connection()).driver_connection
                try:
                    # The OFF pragma is inside the try, not above it: it takes effect at its own
                    # await, so anything interrupting after it, cancellation included, must still
                    # reach the finally.
                    await driver.execute("PRAGMA foreign_keys = OFF")
                    # The commit closes any open transaction, then one BEGIN IMMEDIATE wraps the
                    # whole rebuild so a crash mid-sequence cannot strand the data in
                    # schedule_runs_new.
                    await driver.commit()
                    await driver.execute("BEGIN IMMEDIATE")
                    await self._refuse_newer_sqlite_schema(driver)
                    await driver.execute(
                        """
                        CREATE TABLE schedule_runs_new (
                          id                  TEXT    PRIMARY KEY,
                          schedule_id         TEXT    REFERENCES schedules(id) ON DELETE CASCADE,
                          invocation_id       TEXT    REFERENCES invocations(id),
                          trigger_context     JSON    NOT NULL,
                          action_kind         TEXT    NOT NULL,
                          action_args         JSON    NOT NULL,
                          status              TEXT    NOT NULL DEFAULT 'running'
                                              CHECK(status IN ('queued', 'waiting_dependency',
                                                    'running', 'retry_wait', 'completed',
                                                    'failed', 'timed_out', 'skipped',
                                                    'cancelled')),
                          exit_code           INTEGER,
                          chain_parent_id     TEXT    REFERENCES schedule_runs(id),
                          chain_depth         INTEGER NOT NULL DEFAULT 0,
                          fired_at            REAL    NOT NULL,
                          ended_at            REAL,
                          error_detail        TEXT,
                          created_at          REAL    NOT NULL,
                          updated_at          REAL,
                          status_reason_code     TEXT,
                          status_reason_summary  TEXT,
                          status_evidence_refs   JSON,
                          queued_at           REAL,
                          leased_by           TEXT,
                          lease_expires_at    REAL,
                          concurrency_key     TEXT,
                          lease_attempts      INTEGER NOT NULL DEFAULT 0,
                          required_capabilities  JSON,
                          execution_target       TEXT,
                          library_ref             TEXT,
                          library_content_hash    TEXT,
                          dispatched_at           REAL,
                          resume_packet           JSON
                        )
                        """
                    )
                    insert_sql = (
                        f"INSERT INTO schedule_runs_new ({col_list}) "  # noqa: S608
                        f"SELECT {col_list} FROM schedule_runs"
                    )
                    await driver.execute(insert_sql)
                    await driver.execute("DROP TABLE schedule_runs")
                    await driver.execute("ALTER TABLE schedule_runs_new RENAME TO schedule_runs")
                    for idx_sql in index_sqls:
                        await driver.execute(idx_sql)
                    for trig_sql in trigger_sqls:
                        await driver.execute(trig_sql)
                    # New queue indexes are not part of the pre-rebuild index set, so replaying
                    # index_sqls never creates them. Create them explicitly and idempotently, so a
                    # migrated DB matches a freshly-created one.
                    await driver.execute(
                        "CREATE INDEX IF NOT EXISTS idx_schedule_runs_queue "
                        "ON schedule_runs(status, queued_at) "
                        "WHERE status IN ('queued', 'retry_wait')"
                    )
                    await driver.execute(
                        "CREATE INDEX IF NOT EXISTS idx_schedule_runs_concurrency "
                        "ON schedule_runs(concurrency_key, status) "
                        "WHERE status IN ('queued', 'running', 'retry_wait')"
                    )
                    await driver.commit()
                except BaseException:
                    await driver.rollback()
                    raise
                finally:
                    await _restore_foreign_keys(conn, driver)

        await self._rebuild_check_constraint(
            "schedule_runs",
            lambda sql: sql is not None and self._LEGACY_SCHEDULE_RUNS_QUEUE_MARKER in sql,
            _rebuild,
        )

    # The pre-skill-editor definitions.kind CHECK admitted exactly these two values. A substring
    # search for "'skill'" over the whole CREATE TABLE SQL false-positives on unrelated columns, so
    # detection parses the kind CHECK's own value set instead.
    _LEGACY_DEFINITIONS_KIND_VALUES = frozenset({"agent", "playbook"})

    @staticmethod
    def _definitions_kind_check_values(create_sql: str) -> frozenset[str] | None:
        """The allowed ``kind`` values in a ``definitions`` CHECK, or None if it carries none."""
        match = re.search(r"\bkind\b\s+IN\s*\(([^)]*)\)", create_sql, re.IGNORECASE)
        if match is None:
            return None
        return frozenset(re.findall(r"'([^']*)'", match.group(1)))

    async def _drop_legacy_definitions_kind_check(self) -> None:
        """Rebuild ``definitions`` if it still carries the pre-skill-editor kind CHECK."""
        if self.dialect != "sqlite":
            return
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name='definitions'"
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["sql"] is None:
            return
        create_sql: str = row["sql"]
        if self._definitions_kind_check_values(create_sql) != self._LEGACY_DEFINITIONS_KIND_VALUES:
            # Not the known legacy 2-value CHECK (already migrated, or an
            # unrecognized shape) -- leave it alone.
            return

        # definitions holds every version of every agent, playbook and skill ever saved through
        # Studio: same stakes as schedule_runs, same pre-rebuild backup.
        await self._backup_before_rebuild("definitions")

        async with self._engine.connect() as conn:
            index_rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='index' AND tbl_name='definitions' AND sql IS NOT NULL"
                        )
                    )
                )
                .mappings()
                .all()
            )
            index_sqls = [r["sql"] for r in index_rows]

            cols_rows = (
                (await conn.execute(text("PRAGMA table_info(definitions)"))).mappings().all()
            )
            cols = [r["name"] for r in cols_rows]
        col_list = ", ".join(cols)

        # Derive the rebuild DDL from the canonical schema_meta Table, so this migration path cannot
        # drift from the live schema.
        rebuild_table = _definitions_table.to_metadata(MetaData(), name="definitions_new")
        create_stmt = str(CreateTable(rebuild_table).compile(dialect=self._engine.dialect))

        async def _rebuild() -> None:
            async with self._engine.connect() as conn:
                driver = (await conn.get_raw_connection()).driver_connection
                try:
                    await driver.execute("PRAGMA foreign_keys = OFF")
                    await driver.commit()
                    await driver.execute("BEGIN IMMEDIATE")
                    try:
                        await self._refuse_newer_sqlite_schema(driver)
                        await driver.execute(create_stmt)
                        insert_sql = (
                            f"INSERT INTO definitions_new ({col_list}) "  # noqa: S608
                            f"SELECT {col_list} FROM definitions"
                        )
                        await driver.execute(insert_sql)
                        await driver.execute("DROP TABLE definitions")
                        await driver.execute("ALTER TABLE definitions_new RENAME TO definitions")
                        for idx_sql in index_sqls:
                            await driver.execute(idx_sql)
                        await driver.commit()
                    except BaseException:
                        await driver.rollback()
                        raise
                finally:
                    await _restore_foreign_keys(conn, driver)

        await self._rebuild_check_constraint(
            "definitions",
            lambda sql: (
                sql is not None
                and self._definitions_kind_check_values(sql) != self._LEGACY_DEFINITIONS_KIND_VALUES
            ),
            _rebuild,
        )

    # Schema version

    async def schema_version(self) -> str | None:
        async with self._read() as conn:
            row = (
                (await conn.execute(text("SELECT value FROM schema_meta WHERE key = 'version'")))
                .mappings()
                .first()
            )
        return row["value"] if row else None

    # Messages

    _UNKNOWN_TYPE_ID = 0

    @staticmethod
    def _validate_message(msg: dict[str, Any]) -> None:
        if msg.get("content") is None:
            raise ValueError("messages.content is NOT NULL")
        role = msg.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"messages.role must be a non-empty string; got {role!r}")

    async def _insert_message_in_tx(self, conn, msg: dict[str, Any]) -> None:
        lion_class_str = (msg.get("node_metadata") or {}).get("lion_class", "")
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
                "embedding": _pack_embedding(msg.get("embedding")),
                "sender": msg.get("sender"),
                "recipient": msg.get("recipient"),
                "channel": msg.get("channel"),
                "role": msg["role"],
                "lion_class": type_id,
            },
        )

    async def insert_message(self, msg: dict[str, Any]) -> None:
        self._validate_message(msg)

        # Serialise the full message write behind _write_lock so it cannot interleave with
        # insert_session_signal's or update_status's _tx() on SQLite.
        async with self._tx() as conn:
            await self._insert_message_in_tx(conn, msg)

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

    # Progressions

    async def create_progression(
        self, progression_id: str, collection: list[str] | None = None
    ) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO progressions (id, created_at, collection) VALUES (:id, :ca, :col) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": progression_id,
                    "ca": time.time(),
                    "col": _to_json_column(collection or []),
                },
            )

    async def set_progression(self, progression_id: str, collection: list[str]) -> None:
        """Replace a progression's collection wholesale, serializing the JSON here."""
        async with self._tx() as conn:
            await conn.execute(
                text("UPDATE progressions SET collection = :col WHERE id = :id"),
                {"col": _to_json_column(collection or []), "id": progression_id},
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
        # collection is a TEXT column, cast to jsonb at use-site to append. CAST(:v AS text) not
        # :v::text, since text() does not bind a param immediately followed by '::'.
        return (
            "UPDATE progressions "
            "SET collection = (collection::jsonb || to_jsonb(CAST(:v AS text)))::text "
            "WHERE id=:id AND NOT EXISTS "
            "(SELECT 1 FROM jsonb_array_elements_text(collection::jsonb) WHERE value=:v)"
        )

    async def append_to_progression(self, progression_id: str, message_id: str) -> None:
        """Idempotent append of message_id to the progression JSON array."""
        async with self._tx() as conn:
            await self._append_to_progression_in_tx(conn, progression_id, message_id)

    async def _append_to_progression_in_tx(
        self, conn, progression_id: str, message_id: str
    ) -> None:
        await conn.execute(
            text(self._progression_append_sql(self.dialect)),
            {"v": message_id, "id": progression_id},
        )

    # Sessions

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
        created_at = session.get("created_at", now)
        updated_at = session.get("updated_at", now)
        started_at = session.get("started_at")
        last_message_at = session.get("last_message_at", session.get("started_at", now))
        ended_at = session.get("ended_at")
        ended_at_is_approximate = bool(session.get("ended_at_is_approximate", False))
        if session.get("status") in SESSION_TERMINAL_STATUSES and ended_at is None:
            evidence = [
                value
                for value in (updated_at, last_message_at, started_at, created_at)
                if isinstance(value, int | float)
            ]
            ended_at = max(evidence) if evidence else now
            ended_at_is_approximate = True

        # A row can be born terminal, as when importing a completed run, without passing through
        # _transition() or the admin CAS, both of which derive duration_ms. Derive it here too when
        # the end was measured; an approximate historical marker leaves duration unknown.
        duration_ms = None if ended_at_is_approximate else session.get("duration_ms")
        if (
            duration_ms is None
            and session.get("status") in SESSION_TERMINAL_STATUSES
            and not ended_at_is_approximate
            and isinstance(started_at, int | float)
            and isinstance(ended_at, int | float)
        ):
            duration_ms = max(0.0, (ended_at - started_at) * 1000)
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """INSERT INTO sessions (id, cc_session_id, run_id, created_at, node_metadata, name, "user",
                       progression_id, first_msg_id, last_msg_id, updated_at,
                       playbook_name, agent_name, invocation_kind, show_topic,
                       show_play_name, artifacts_path, artifact_contract_json,
                       artifact_verification_json, source_kind,
                       status, started_at, ended_at, ended_at_is_approximate,
                       last_message_at, invocation_id,
                       model, provider, effort, agent_hash,
                       project, project_source, duration_ms)
                       VALUES (:id, :cc_session_id, :run_id, :created_at, :node_metadata, :name, :user,
                               :progression_id, :first_msg_id, :last_msg_id, :updated_at,
                               :playbook_name, :agent_name, :invocation_kind, :show_topic,
                               :show_play_name, :artifacts_path, :artifact_contract_json,
                               :artifact_verification_json, :source_kind,
                               :status, :started_at, :ended_at, :ended_at_is_approximate,
                               :last_message_at, :invocation_id,
                               :model, :provider, :effort, :agent_hash,
                               :project, :project_source, :duration_ms)
                       ON CONFLICT (id) DO NOTHING"""
                ).bindparams(
                    bindparam("node_metadata", type_=JSON),
                    bindparam("artifact_contract_json", type_=JSON),
                    bindparam("artifact_verification_json", type_=JSON),
                ),
                {
                    "id": session["id"],
                    "cc_session_id": session.get("cc_session_id"),
                    "run_id": session.get("run_id"),
                    "created_at": created_at,
                    "node_metadata": session.get("node_metadata"),
                    "name": session.get("name"),
                    "user": session.get("user"),
                    "progression_id": session["progression_id"],
                    "first_msg_id": session.get("first_msg_id"),
                    "last_msg_id": session.get("last_msg_id"),
                    "updated_at": updated_at,
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
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "ended_at_is_approximate": int(ended_at_is_approximate),
                    "last_message_at": last_message_at,
                    "invocation_id": session.get("invocation_id"),
                    "model": session.get("model"),
                    "provider": session.get("provider"),
                    "effort": session.get("effort"),
                    "agent_hash": session.get("agent_hash"),
                    "project": session.get("project"),
                    "project_source": session.get("project_source"),
                    "duration_ms": duration_ms,
                },
            )
            # Only increment session_count when INSERT actually created a row.
            if result.rowcount and session.get("status") is not None:
                await self._initialize_managed_entity_in_tx(
                    conn,
                    entity_type="session",
                    entity_id=session["id"],
                    status=session["status"],
                    actor_id="create_session",
                )
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

    @staticmethod
    def _decrement_invocation_session_count_sql(dialect: str) -> str:
        # SQLite MAX(a,b) is a scalar greatest, Postgres MAX() an aggregate, so use GREATEST()
        # there. Floor at zero so an out-of-order decrement never reports negative.
        if dialect == "sqlite":
            return (
                "UPDATE invocations SET session_count = MAX(session_count - 1, 0), "
                "updated_at = :now WHERE id = :inv_id"
            )
        return (
            "UPDATE invocations SET session_count = GREATEST(session_count - 1, 0), "
            "updated_at = :now WHERE id = :inv_id"
        )

    async def attach_session_invocation(self, session_id: str, invocation_id: str) -> None:
        """Point an existing session row at *invocation_id*, keeping ``session_count`` in step."""
        now = time.time()
        async with self._tx() as conn:
            prev_query = "SELECT invocation_id FROM sessions WHERE id = :sid"
            if self.dialect == "postgresql":
                prev_query += " FOR UPDATE"
            prev_row = (await conn.execute(text(prev_query), {"sid": session_id})).first()
            prev_invocation_id = prev_row[0] if prev_row else None

            result = await conn.execute(
                text(
                    "UPDATE sessions SET invocation_id = :inv_id, updated_at = :now "
                    "WHERE id = :sid AND (invocation_id IS NULL OR invocation_id != :inv_id)"
                ),
                {"inv_id": invocation_id, "now": now, "sid": session_id},
            )
            if result.rowcount:
                if prev_invocation_id and prev_invocation_id != invocation_id:
                    await conn.execute(
                        text(self._decrement_invocation_session_count_sql(self.dialect)),
                        {"now": now, "inv_id": prev_invocation_id},
                    )
                await conn.execute(
                    text(
                        "UPDATE invocations SET session_count = session_count + 1, "
                        "updated_at = :now WHERE id = :inv_id"
                    ),
                    {"now": now, "inv_id": invocation_id},
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

    async def get_sessions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Every session recorded against CLI run *run_id*, oldest first."""
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM sessions WHERE run_id = :run_id "
                            "ORDER BY created_at ASC, id ASC"
                        ),
                        {"run_id": run_id},
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_dict(row) for row in rows]

    async def get_session_by_cc_id(self, cc_session_id: str) -> dict[str, Any] | None:
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM sessions WHERE cc_session_id = :cc_session_id LIMIT 1"),
                        {"cc_session_id": cc_session_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row else None

    async def sessions_by_source_kind(self, source_kind: str) -> list[dict[str, Any]]:
        """Minimal rows (id, node_metadata) for one source_kind, for importer sweeps."""
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, node_metadata FROM sessions "
                            "WHERE source_kind = :source_kind"
                        ),
                        {"source_kind": source_kind},
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_dict(row) for row in rows]

    async def delete_imported_session(self, session_id: str, *, require_source_kind: str) -> bool:
        """Delete a mirror-imported session and all the mirror wrote for it."""
        if not require_source_kind.startswith("imported_"):
            return False
        async with self._tx() as conn:
            if self.dialect != "sqlite":
                # Table lock taken before the first read, so the rest of the transaction cannot
                # observe a survivor's reference changing underneath it. NOWAIT under a bounded
                # lock_timeout: LOCK TABLE takes its three targets one at a time, and a blocking
                # wait could close a cycle with prune_old_data's reverse lock order. Full reasoning
                # in docs/internals/state-db.md.
                await conn.execute(text("SET LOCAL lock_timeout = '250ms'"))
                await conn.execute(
                    text("LOCK TABLE branches, progressions, sessions IN EXCLUSIVE MODE NOWAIT")
                )
            row = (
                (
                    await conn.execute(
                        text("SELECT source_kind, progression_id FROM sessions WHERE id = :id"),
                        {"id": session_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            if str(row["source_kind"] or "") != require_source_kind:
                return False

            prog_ids: list[str] = [row["progression_id"]] if row["progression_id"] else []
            branch_rows = (
                (
                    await conn.execute(
                        text("SELECT progression_id FROM branches WHERE session_id = :id"),
                        {"id": session_id},
                    )
                )
                .mappings()
                .all()
            )
            prog_ids.extend(b["progression_id"] for b in branch_rows if b["progression_id"])

            # Chunked IN-lists keep each statement under SQLite's bound-variable cap.
            def _chunks(values: list[str], size: int = 400):
                for i in range(0, len(values), size):
                    yield values[i : i + size]

            # A survivor session or branch may point its progression_id FK at one of these
            # progressions; such a progression, and the messages it holds, are not ours to delete.
            kept_progs: set[str] = set()
            for chunk in _chunks(prog_ids):
                params = {f"p{i}": pid for i, pid in enumerate(chunk)}
                ph = ", ".join(f":{k}" for k in params)
                rows = (
                    (
                        await conn.execute(
                            text(
                                f"SELECT progression_id AS pid FROM sessions WHERE id != :sid AND progression_id IN ({ph}) "  # noqa: S608, E501
                                f"UNION SELECT progression_id FROM branches WHERE session_id != :sid AND progression_id IN ({ph})"  # noqa: S608, E501
                            ),
                            {"sid": session_id, **params},
                        )
                    )
                    .mappings()
                    .all()
                )
                kept_progs.update(str(r["pid"]) for r in rows)
            deletable_progs = [p for p in prog_ids if p not in kept_progs]

            msg_ids: set[str] = set()
            for pid in deletable_progs:
                prow = (
                    (
                        await conn.execute(
                            text("SELECT collection FROM progressions WHERE id = :id"),
                            {"id": pid},
                        )
                    )
                    .mappings()
                    .first()
                )
                if prow is None:
                    continue
                collection = prow["collection"]
                if isinstance(collection, str):
                    try:
                        collection = json.loads(collection)
                    except (TypeError, ValueError):
                        collection = []
                if isinstance(collection, list):
                    msg_ids.update(str(m) for m in collection)

            # A message referenced by any progression outside the deletable set is not ours to
            # delete, and a retained target progression counts as an outside holder here.
            prog_params = {f"pp{i}": pid for i, pid in enumerate(deletable_progs)}
            prog_ph = ", ".join(f":{k}" for k in prog_params) or "''"
            unnest = (
                "json_each(p.collection) je"
                if self.dialect == "sqlite"
                else "LATERAL json_array_elements_text(p.collection::json) je(value)"
            )
            # A malformed collection on an unrelated progression must not sink the whole absorb:
            # SQLite filters those rows out, and on other dialects the savepoint below fails toward
            # retention for that chunk.
            valid_guard = " AND json_valid(p.collection)" if self.dialect == "sqlite" else ""
            # Interpolations are bound-param placeholder names, never values.
            shared_sql = f"SELECT DISTINCT je.value AS mid FROM progressions p, {unnest} WHERE p.id NOT IN ({prog_ph}){valid_guard} AND je.value IN ({{msg_ph}})"  # noqa: S608, E501
            shared: set[str] = set()
            for chunk in _chunks(sorted(msg_ids)):
                params = {f"m{i}": mid for i, mid in enumerate(chunk)}
                msg_ph = ", ".join(f":{k}" for k in params)
                try:
                    async with conn.begin_nested():
                        rows = (
                            (
                                await conn.execute(
                                    text(shared_sql.format(msg_ph=msg_ph)),  # noqa: S608
                                    {**prog_params, **params},
                                )
                            )
                            .mappings()
                            .all()
                        )
                except SQLAlchemyError:
                    # Retaining is the safe direction: an over-retained message is a stray row a
                    # later pass can collect, an over-deleted one breaks a live session.
                    _log.warning(
                        "delete_imported_session: shared-reference check failed for a "
                        "chunk of %d message(s); retaining them",
                        len(chunk),
                        exc_info=True,
                    )
                    shared.update(chunk)
                    continue
                shared.update(str(r["mid"]) for r in rows)

            # Detaching an artifact moves it into another partial unique-index domain, where its
            # (kind, name) slot may already be taken. A colliding row keeps its data and takes a
            # suffixed name recording the session it came from, while non-colliding rows keep
            # theirs. The suffixed name is allocated against both the destination domain and this
            # session's own detaching rows, since a fixed suffix can itself be occupied and a UNIQUE
            # failure rolls back the whole absorption.
            srows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, kind, name, invocation_id FROM artifacts "
                            "WHERE session_id = :id ORDER BY id"
                        ),
                        {"id": session_id},
                    )
                )
                .mappings()
                .all()
            )
            if srows:
                kinds = sorted({str(r["kind"]) for r in srows})
                external: dict[tuple[Any, str], set[str]] = {}
                for chunk in _chunks(kinds):
                    ph = ", ".join(f":k{i}" for i in range(len(chunk)))
                    rows = (
                        (
                            await conn.execute(
                                text(
                                    f"SELECT kind, name, invocation_id FROM artifacts "  # noqa: S608
                                    f"WHERE session_id IS NULL AND kind IN ({ph})"
                                ),
                                {f"k{i}": k for i, k in enumerate(chunk)},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    for r in rows:
                        external.setdefault((r["invocation_id"], str(r["kind"])), set()).add(
                            str(r["name"])
                        )
                taken = {dom: set(names) for dom, names in external.items()}
                for r in srows:
                    dom = (r["invocation_id"], str(r["kind"]))
                    taken.setdefault(dom, set()).add(str(r["name"]))
                for r in srows:
                    dom = (r["invocation_id"], str(r["kind"]))
                    if str(r["name"]) not in external.get(dom, set()):
                        continue
                    final = f"{r['name']} (detached {session_id})"
                    n = 2
                    while final in taken[dom]:
                        final = f"{r['name']} (detached {session_id} {n})"
                        n += 1
                    taken[dom].add(final)
                    await conn.execute(
                        text("UPDATE artifacts SET name = :nm WHERE id = :aid"),
                        {"nm": final, "aid": r["id"]},
                    )
            # Soft session FKs without CASCADE are someone else's rows pointing at this one: nullify
            # the pointer, keep the row. Only artifacts carry unique indexes over session_id,
            # handled above.
            for table in ("artifacts", "plays", "team_messages", "dispatch_outbox", "approvals"):
                await conn.execute(
                    text(
                        f"UPDATE {table} SET session_id = NULL WHERE session_id = :id"  # noqa: S608
                    ),
                    {"id": session_id},
                )

            # Referencing rows go before what they reference: branches, then the session, then the
            # now-unreferenced messages, then the progressions.
            await conn.execute(
                text("DELETE FROM branches WHERE session_id = :id"), {"id": session_id}
            )
            await conn.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_id})
            # This session's own rows are already gone, so any first/last/system pointer the
            # subquery still sees belongs to a survivor, and such a message is retained.
            retain_refs = (
                " AND id NOT IN ("
                "SELECT first_msg_id FROM sessions WHERE first_msg_id IS NOT NULL"
                " UNION SELECT last_msg_id FROM sessions WHERE last_msg_id IS NOT NULL"
                " UNION SELECT system_msg_id FROM branches WHERE system_msg_id IS NOT NULL)"
            )
            for chunk in _chunks(sorted(msg_ids - shared)):
                params = {f"m{i}": mid for i, mid in enumerate(chunk)}
                placeholders = ", ".join(f":{k}" for k in params)
                await conn.execute(
                    text(
                        f"DELETE FROM messages WHERE id IN ({placeholders}){retain_refs}"  # noqa: S608, E501
                    ),
                    params,
                )
            for chunk in _chunks(deletable_progs):
                params = {f"p{i}": pid for i, pid in enumerate(chunk)}
                placeholders = ", ".join(f":{k}" for k in params)
                await conn.execute(
                    text(f"DELETE FROM progressions WHERE id IN ({placeholders})"),  # noqa: S608
                    params,
                )
            # terminal_deliveries holds an FK into status_transitions: children first.
            await conn.execute(
                text(
                    "DELETE FROM terminal_deliveries WHERE transition_id IN ("
                    "SELECT id FROM status_transitions "
                    "WHERE entity_type = 'session' AND entity_id = :id)"
                ),
                {"id": session_id},
            )
            await conn.execute(
                text(
                    "DELETE FROM status_transitions "
                    "WHERE entity_type = 'session' AND entity_id = :id"
                ),
                {"id": session_id},
            )
        return True

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
        async with self._tx() as conn:
            await self._touch_session_activity_in_tx(conn, session_id, at=at)

    async def _touch_session_activity_in_tx(
        self,
        conn,
        session_id: str,
        *,
        at: float | None = None,
    ) -> None:
        ts = at if at is not None else time.time()
        await conn.execute(
            text(self._touch_activity_sql(self.dialect)),
            {"ts": ts, "id": session_id},
        )

    async def _persist_live_message(
        self,
        msg: dict[str, Any],
        *,
        session_id: str,
        branch_progression_id: str | None = None,
        session_progression_id: str | None = None,
        system_branch_id: str | None = None,
        system_branch_update_before_activity: bool = False,
        activity_at: float | None = None,
    ) -> None:
        """Atomically persist one live message and its immediate bookkeeping."""
        self._validate_message(msg)
        async with self._tx() as conn:
            await self._insert_message_in_tx(conn, msg)
            if branch_progression_id is not None:
                await self._append_to_progression_in_tx(conn, branch_progression_id, msg["id"])
            if session_progression_id is not None:
                await self._append_to_progression_in_tx(conn, session_progression_id, msg["id"])
            if system_branch_id is not None and system_branch_update_before_activity:
                await self._update_branch_in_tx(
                    conn,
                    system_branch_id,
                    system_msg_id=msg["id"],
                )
            await self._touch_session_activity_in_tx(conn, session_id, at=activity_at)
            if system_branch_id is not None and not system_branch_update_before_activity:
                await self._update_branch_in_tx(
                    conn,
                    system_branch_id,
                    system_msg_id=msg["id"],
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
        set_if_null: frozenset[str] = frozenset(),
        **fields: Any,
    ) -> None:
        """Update session fields; status changes route through update_status()."""
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
            sets = ", ".join(
                f'"{k}" = COALESCE("{k}", :{k})' if k in set_if_null else f'"{k}" = :{k}'
                for k in fields
            )
            params = dict(fields)
            params["_id"] = session_id
            async with self._tx() as conn:
                await conn.execute(
                    text(f"UPDATE sessions SET {sets} WHERE id = :_id"),  # noqa: S608
                    params,
                )

    @staticmethod
    def _merge_node_metadata_sql(dialect: str, table: str = "sessions") -> str:
        """One dialect-specific UPDATE that reads, merges and writes node_metadata in-database."""
        if dialect == "sqlite":
            return (
                f"UPDATE {table} SET "  # noqa: S608
                "node_metadata = json_patch("
                "  CASE"
                "    WHEN node_metadata IS NULL THEN '{}'"
                "    WHEN NOT json_valid(node_metadata) THEN"
                "      json_object('_discarded_node_metadata', node_metadata,"
                "                  '_discarded_at', :now)"
                "    WHEN json_type(node_metadata) = 'object' THEN node_metadata"
                "    WHEN json_type(node_metadata) = 'null' THEN '{}'"
                "    ELSE json_object('_discarded_node_metadata', json(node_metadata),"
                "                     '_discarded_at', :now)"
                "  END,"
                "  :patch"
                "), "
                "updated_at = :now "
                "WHERE id = :id"
            )
        return (
            f"UPDATE {table} SET "  # noqa: S608
            # jsonb `||` keeps an explicit null, unlike sqlite's json_patch which deletes the key
            # per RFC 7396. Reproduce RFC 7396 by subtracting exactly the keys :patch itself set to
            # null, rather than jsonb_strip_nulls which would also strip pre-existing ones.
            "node_metadata = (("
            "  CASE"
            "    WHEN node_metadata IS NULL THEN '{}'::jsonb"
            "    WHEN jsonb_typeof(node_metadata::jsonb) = 'object' THEN node_metadata::jsonb"
            "    WHEN jsonb_typeof(node_metadata::jsonb) = 'null' THEN '{}'::jsonb"
            "    ELSE jsonb_build_object('_discarded_node_metadata', node_metadata::jsonb,"
            "                            '_discarded_at', to_jsonb(CAST(:now AS double precision)))"
            "  END || CAST(:patch AS jsonb)"
            ") - COALESCE("
            "  (SELECT array_agg(kv.key) FROM jsonb_each(CAST(:patch AS jsonb)) AS kv(key, value)"
            "   WHERE kv.value = 'null'::jsonb),"
            "  ARRAY[]::text[]"
            "))::json, "
            "updated_at = :now "
            "WHERE id = :id"
        )

    async def _merge_node_metadata(self, table: str, entity_id: str, patch: dict[str, Any]) -> None:
        """Shared body for the two node_metadata merges: validate the patch, then UPDATE *table*."""
        for key, value in patch.items():
            if isinstance(value, dict):
                raise ValueError(
                    f"merge_{table[:-1]}_node_metadata does not support a nested "
                    f"object patch value (key {key!r}): sqlite and postgres "
                    "merge nested objects differently, so this would persist "
                    "different state per backend. Flatten the patch or merge "
                    "the nested object yourself before calling this."
                )
        now = time.time()
        async with self._tx() as conn:
            stmt = text(self._merge_node_metadata_sql(self.dialect, table=table)).bindparams(
                bindparam("patch", type_=JSON)
            )
            await conn.execute(stmt, {"patch": patch, "now": now, "id": entity_id})

    async def merge_session_node_metadata(self, session_id: str, patch: dict[str, Any]) -> None:
        """Atomically merge *patch* into the session's node_metadata column."""
        await self._merge_node_metadata("sessions", session_id, patch)

    async def merge_invocation_node_metadata(
        self, invocation_id: str, patch: dict[str, Any]
    ) -> None:
        """Atomically merge *patch* into the invocation's node_metadata column."""
        await self._merge_node_metadata("invocations", invocation_id, patch)

    async def update_artifact_verification(
        self,
        session_id: str,
        verification: dict[str, Any] | None,
    ) -> None:
        # Must hold _write_lock: teardown calls this while signal persistence is still bound, so a
        # late signal emit's _tx() can race this UPDATE on SQLite.
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
        cc_session_id: str | None = None,
        artifacts_path: str | None = None,
    ) -> None:
        """Write attribution and provenance fields without touching updated_at."""
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
        if cc_session_id is not None:
            sets.append("cc_session_id = :cc_session_id")
            params["cc_session_id"] = cc_session_id
        if artifacts_path is not None:
            sets.append('artifacts_path = COALESCE("artifacts_path", :artifacts_path)')
            params["artifacts_path"] = artifacts_path
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

    # Status reason model

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
        allowed_extra = EXTRA_STATUS_WRITE_FIELDS_BY_ENTITY_TYPE.get(entity_type, frozenset())
        extra_fields = {name: fields.pop(name) for name in allowed_extra if name in fields}
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
                "(this fallback is deprecated).",
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
            extra_fields=extra_fields or None,
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
        expected_updated_at: float | None = None,
        extra_fields: dict[str, Any] | None = None,
        override: bool = False,
        override_actor: str | None = None,
        override_justification: str | None = None,
    ) -> bool:
        """Atomically transition a status and record the reason, under a terminal-status floor."""
        if source not in _VALID_STATUS_SOURCES:
            raise ValueError(
                f"update_status() called with source={source!r}; "
                f"must be one of {sorted(_VALID_STATUS_SOURCES)}."
            )
        if override and (not override_actor or not override_justification):
            raise ValueError(
                "override=True requires both override_actor and "
                "override_justification (ADR-0035 D5 operational-repair trail)."
            )
        canonical_type = _validate_entity_type_for_reason(entity_type)
        _validate_reason_code(reason_code)
        valid_statuses = VALID_STATUSES_BY_ENTITY_TYPE.get(canonical_type)
        if valid_statuses is not None and new_status not in valid_statuses:
            raise ValueError(
                f"update_status() called with new_status={new_status!r} for "
                f"entity_type={canonical_type!r}; vocabulary is {sorted(valid_statuses)}."
            )
        # `table` kept for message-format parity with the earlier error text;
        # the lifecycle policy for `canonical_type` resolves to the same table.
        table = _reason_entity_table(canonical_type)
        if extra_fields:
            allowed_extra = EXTRA_STATUS_WRITE_FIELDS_BY_ENTITY_TYPE.get(
                canonical_type, frozenset()
            )
            unknown = set(extra_fields) - allowed_extra
            if unknown:
                raise ValueError(
                    f"update_status() called with extra_fields keys {sorted(unknown)} for "
                    f"entity_type={canonical_type!r}; allowed keys are {sorted(allowed_extra)}."
                )

        # The guarded read/CAS/edge-validation/history algorithm lives in LifecycleService; this
        # method keeps only its legacy-specific validation and the outcome-to-bool mapping.
        try:
            return await _lifecycle_adapters.run_update_status(
                self._lifecycle_service(),
                entity_type=canonical_type,
                entity_id=entity_id,
                new_status=new_status,
                reason_code=reason_code,
                reason_summary=reason_summary,
                evidence_refs=evidence_refs,
                source=source,
                actor=actor,
                metadata=metadata,
                expected_statuses=expected_statuses,
                expected_updated_at=expected_updated_at,
                extra_fields=extra_fields,
                override=override,
                override_actor=override_actor,
                override_justification=override_justification,
            )
        except _LifecycleNotFoundError as exc:
            raise LookupError(f"{canonical_type} {entity_id!r} not found (table={table})") from exc

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
        """Per-bucket (bucket_start, status, count) rows for the activity window."""
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

    async def spend_stats(self, *, window_start: float) -> dict[str, Any]:
        """Reported-spend aggregate; unreported sessions are counted, never summed as zero."""
        query = """
            SELECT
                SUM(CASE WHEN total_cost_usd IS NOT NULL THEN total_cost_usd END) AS reported_usd,
                SUM(CASE WHEN total_cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS reported_count,
                SUM(CASE WHEN total_cost_usd IS NULL THEN 1 ELSE 0 END) AS unreported_count
            FROM sessions
            WHERE COALESCE(ended_at, started_at, created_at) >= :window_start
        """  # noqa: S608
        async with self._read() as conn:
            row = (
                (await conn.execute(text(query), {"window_start": window_start})).mappings().first()
            )
        reported_usd = row["reported_usd"] if row else None
        return {
            "reported_usd": float(reported_usd) if reported_usd is not None else None,
            "reported_count": int(row["reported_count"] or 0) if row else 0,
            "unreported_count": int(row["unreported_count"] or 0) if row else 0,
        }

    # Session-grain spend dimensions only. Branch-level attribution needs a coverage check against
    # branch-level total_cost_usd before it can be trusted; these three columns are single-valued
    # per session, so grouping by them carries no such risk.
    _SPEND_ROLLUP_COLUMNS: dict[str, str] = {
        "project": "project",
        "agent": "agent_name",
        "playbook": "playbook_name",
    }
    _SPEND_ROLLUP_MAX_LIMIT = 50

    async def spend_rollup(
        self, *, window_start: float, dimension: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Session-grain spend rollup, one row per dimension value, highest reported spend first."""
        column = self._SPEND_ROLLUP_COLUMNS.get(dimension)
        if column is None:
            raise ValueError(f"dimension must be one of: {', '.join(self._SPEND_ROLLUP_COLUMNS)}")
        bounded_limit = max(1, min(int(limit), self._SPEND_ROLLUP_MAX_LIMIT))
        query = f"""
            SELECT
                {column} AS rollup_key,
                SUM(CASE WHEN total_cost_usd IS NOT NULL THEN total_cost_usd END) AS reported_usd,
                SUM(CASE WHEN total_cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS reported_count,
                SUM(CASE WHEN total_cost_usd IS NULL THEN 1 ELSE 0 END) AS unreported_count
            FROM sessions
            WHERE COALESCE(ended_at, started_at, created_at) >= :window_start
            GROUP BY {column}
            ORDER BY reported_usd IS NULL, reported_usd DESC
            LIMIT :limit
        """  # noqa: S608
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(query), {"window_start": window_start, "limit": bounded_limit}
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "key": r["rollup_key"],
                "reported_usd": float(r["reported_usd"]) if r["reported_usd"] is not None else None,
                "reported_count": int(r["reported_count"] or 0),
                "unreported_count": int(r["unreported_count"] or 0),
            }
            for r in rows
        ]

    # Projects

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

    # Schedules

    async def create_schedule(self, schedule: dict[str, Any]) -> None:
        stmt, params = self._build_schedule_insert_stmt(schedule)
        async with self._tx() as conn:
            await conn.execute(stmt, params)

    @staticmethod
    def _build_schedule_insert_stmt(schedule: dict[str, Any]):
        """Build the ``INSERT INTO schedules`` statement and params, opening no transaction."""
        now = time.time()
        stmt = text(
            """INSERT INTO schedules
               (id, name, description, enabled, trigger_type,
                cron_expr, interval_sec, github_repo, github_filter,
                github_cursor, poll_interval_sec,
                action_kind, action_model, action_prompt, action_agent,
                action_playbook, action_flow_yaml, action_project, action_cwd,
                action_extra_args, action_command, action_command_args,
                on_success, on_fail, last_fired_at, next_fire_at,
                missed_fire_policy, overlap_policy, max_runs, budget_usd, budget_tokens,
                rate_limit,
                project, threshold_config, last_alert_at,
                spec_version, managed_by, owner_key, authored_spec,
                resolved_target, resolved_digest, resolved_timezone,
                effective_timezone, effective_timezone_source,
                notify_on, notify_command,
                created_at, updated_at)
               VALUES (:id, :name, :description, :enabled, :trigger_type,
                       :cron_expr, :interval_sec, :github_repo, :github_filter,
                       :github_cursor, :poll_interval_sec,
                       :action_kind, :action_model, :action_prompt, :action_agent,
                       :action_playbook, :action_flow_yaml, :action_project, :action_cwd,
                       :action_extra_args, :action_command, :action_command_args,
                       :on_success, :on_fail, :last_fired_at, :next_fire_at,
                       :missed_fire_policy, :overlap_policy, :max_runs, :budget_usd, :budget_tokens,
                       :rate_limit,
                       :project, :threshold_config, :last_alert_at,
                       :spec_version, :managed_by, :owner_key, :authored_spec,
                       :resolved_target, :resolved_digest, :resolved_timezone,
                       :effective_timezone, :effective_timezone_source,
                       :notify_on, :notify_command,
                       :created_at, :updated_at)"""
        ).bindparams(
            bindparam("github_filter", type_=JSON),
            bindparam("action_extra_args", type_=JSON),
            bindparam("action_command_args", type_=JSON),
            bindparam("on_success", type_=JSON),
            bindparam("on_fail", type_=JSON),
            bindparam("rate_limit", type_=JSON),
            bindparam("threshold_config", type_=JSON),
            bindparam("authored_spec", type_=JSON),
            bindparam("resolved_target", type_=JSON),
            bindparam("notify_on", type_=JSON),
        )
        params = {
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
            "action_cwd": schedule.get("action_cwd"),
            "action_extra_args": schedule.get("action_extra_args", []),
            "action_command": schedule.get("action_command"),
            "action_command_args": schedule.get("action_command_args", []),
            "on_success": schedule.get("on_success"),
            "on_fail": schedule.get("on_fail"),
            "last_fired_at": schedule.get("last_fired_at"),
            "next_fire_at": schedule.get("next_fire_at"),
            "missed_fire_policy": schedule.get("missed_fire_policy", "skip"),
            "overlap_policy": schedule.get("overlap_policy", "skip"),
            "max_runs": schedule.get("max_runs"),
            "budget_usd": schedule.get("budget_usd"),
            "budget_tokens": schedule.get("budget_tokens"),
            "rate_limit": schedule.get("rate_limit"),
            "project": schedule.get("project"),
            "threshold_config": schedule.get("threshold_config"),
            "last_alert_at": schedule.get("last_alert_at"),
            "spec_version": schedule.get("spec_version"),
            "managed_by": schedule.get("managed_by"),
            "owner_key": schedule.get("owner_key"),
            "authored_spec": schedule.get("authored_spec"),
            "resolved_target": schedule.get("resolved_target"),
            "resolved_digest": schedule.get("resolved_digest"),
            "resolved_timezone": schedule.get("resolved_timezone"),
            "effective_timezone": schedule.get("effective_timezone"),
            "effective_timezone_source": schedule.get("effective_timezone_source"),
            "notify_on": schedule.get("notify_on"),
            "notify_command": schedule.get("notify_command"),
            "created_at": schedule.get("created_at", now),
            "updated_at": schedule.get("updated_at", now),
        }
        return stmt, params

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

    async def list_schedules_by_owner_key(self, owner_key: str) -> list[dict[str, Any]]:
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text("SELECT * FROM schedules WHERE owner_key = :owner_key"),
                        {"owner_key": owner_key},
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_dict(r) for r in rows]

    async def apply_schedule_set(
        self,
        *,
        creates: list[dict[str, Any]],
        updates: list[tuple[str, dict[str, Any]]],
        disables: list[str],
    ) -> None:
        """Atomically commit a ScheduleSet plan; all members must be valid before reaching here."""
        async with self._tx() as conn:
            for schedule in creates:
                stmt, params = self._build_schedule_insert_stmt(schedule)
                await conn.execute(stmt, params)
            for schedule_id, fields in updates:
                stmt, params = self._build_update_schedule_stmt(schedule_id, fields)
                await conn.execute(stmt, params)
            for schedule_id in disables:
                stmt, params = self._build_update_schedule_stmt(schedule_id, {"enabled": 0})
                await conn.execute(stmt, params)

    async def list_schedules(
        self,
        *,
        enabled: bool | None = None,
        trigger_type: str | None = None,
        project: str | None = None,
        limit: int | None = 100,
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
        query += " ORDER BY updated_at DESC"
        if limit is not None:
            query += " LIMIT :limit OFFSET :offset"
            params["limit"] = limit
            params["offset"] = offset
        async with self._read() as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
        return [self._row_to_dict(r) for r in rows]

    # Fields update_schedule(), and create_schedule_run_and_advance()'s folded-in schedule update,
    # may write. A single choke point so the two write paths cannot drift on what is allowed.
    _SCHEDULE_UPDATE_ALLOWED_FIELDS = frozenset(
        {
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
            "action_cwd",
            "action_extra_args",
            "action_command",
            "action_command_args",
            "on_success",
            "on_fail",
            "last_fired_at",
            "next_fire_at",
            "missed_fire_policy",
            "overlap_policy",
            "max_runs",
            "budget_usd",
            "budget_tokens",
            "rate_limit",
            "project",
            "threshold_config",
            "last_alert_at",
            "last_evaluated_at",
            "last_healthy_poll_at",
            "poller_consecutive_401",
            "predispatch_refusal_event",
            "predispatch_refusal_count",
            "spec_version",
            "managed_by",
            "owner_key",
            "authored_spec",
            "resolved_target",
            "resolved_digest",
            "resolved_timezone",
            "effective_timezone",
            "effective_timezone_source",
            "notify_on",
            "notify_command",
        }
    )

    async def update_schedule(
        self,
        schedule_id: str,
        *,
        guard_cursor_forward: bool = False,
        expect_next_fire_at: Any = NO_CURSOR_CLAIM,
        **fields: Any,
    ) -> bool:
        """Update one schedule; returns whether a row matched.

        With *expect_next_fire_at* the update also claims that cursor value, so a caller that
        reserves a due instant ahead of dispatching it learns whether it won the reservation.
        """
        stmt, params = self._build_update_schedule_stmt(
            schedule_id,
            fields,
            guard_cursor_forward=guard_cursor_forward,
            expect_next_fire_at=expect_next_fire_at,
        )
        async with self._tx() as conn:
            result = await conn.execute(stmt, params)
        return result.rowcount > 0

    @classmethod
    def _build_update_schedule_stmt(
        cls,
        schedule_id: str,
        fields: dict[str, Any],
        *,
        guard_cursor_forward: bool = False,
        expect_next_fire_at: Any = NO_CURSOR_CLAIM,
    ):
        """Validate and build the ``UPDATE schedules`` statement and params, no transaction."""
        bad = set(fields) - cls._SCHEDULE_UPDATE_ALLOWED_FIELDS
        if bad:
            raise ValueError(f"Invalid schedule field(s): {bad}")
        json_fields = {
            "github_filter",
            "action_extra_args",
            "action_command_args",
            "on_success",
            "on_fail",
            "rate_limit",
            "threshold_config",
            "authored_spec",
            "resolved_target",
            "notify_on",
        }
        fields = dict(fields)
        fields["updated_at"] = time.time()
        guarded_cursor = (
            guard_cursor_forward
            and "github_cursor" in fields
            and fields["github_cursor"] is not None
        )
        sets_parts = []
        bind_params = []
        for k in fields:
            if k == "github_cursor" and guarded_cursor:
                # Cursors are compared as strings everywhere, since the poller compares them against
                # GitHub's own timestamps, so plain lexical order IS the ordering.
                sets_parts.append(
                    '"github_cursor" = CASE WHEN github_cursor IS NULL '
                    "OR github_cursor < :github_cursor "
                    "THEN :github_cursor ELSE github_cursor END"
                )
                continue
            sets_parts.append(f'"{k}" = :{k}')
            if k in json_fields:
                bind_params.append(bindparam(k, type_=JSON))
        params = dict(fields)
        params["_id"] = schedule_id
        where = "id = :_id"
        if expect_next_fire_at is not NO_CURSOR_CLAIM:
            # Branching on the Python value rather than emitting a NULL-safe operator: sqlite
            # spells one `IS`, postgres rejects `IS` with a bound parameter outright, and their
            # shared spelling (`IS NOT DISTINCT FROM`) is too new to rely on. A schedule with no
            # cursor is still guarded on the same terms as one that has it.
            if expect_next_fire_at is None:
                where += " AND next_fire_at IS NULL"
            else:
                where += " AND next_fire_at = :_expect_nfa"
                params["_expect_nfa"] = expect_next_fire_at
        stmt = text(f"UPDATE schedules SET {', '.join(sets_parts)} WHERE {where}")  # noqa: S608
        if bind_params:
            stmt = stmt.bindparams(*bind_params)
        return stmt, params

    async def delete_schedule(self, schedule_id: str) -> bool:
        async with self._tx() as conn:
            result = await conn.execute(
                text("DELETE FROM schedules WHERE id = :id"),
                {"id": schedule_id},
            )
        return result.rowcount > 0

    # Schedule Runs

    async def create_schedule_run(self, run: dict[str, Any]) -> None:
        stmt, params = self._build_schedule_run_insert_stmt(run)
        async with self._tx() as conn:
            await conn.execute(stmt, params)
            await self._initialize_managed_entity_in_tx(
                conn,
                entity_type="schedule_run",
                entity_id=params["id"],
                status=params["status"],
                actor_id="create_schedule_run",
            )

    @staticmethod
    def _build_schedule_run_insert_stmt(run: dict[str, Any]):
        """Build the ``INSERT INTO schedule_runs`` statement and params, opening no transaction."""
        now = time.time()
        stmt = text(
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
        )
        params = {
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
        }
        return stmt, params

    async def create_schedule_run_and_advance(
        self,
        run: dict[str, Any],
        *,
        schedule_id: str,
        schedule_fields: dict[str, Any],
        expect_next_fire_at: Any,
    ) -> bool:
        """Insert one occurrence row and advance the schedule's cursor in a single transaction.

        Returns False when *expect_next_fire_at* no longer matches, having written nothing.
        Pass ``NO_CURSOR_CLAIM`` for a fire that does not stand for a due instant, such as a
        chain child or a replacement for an occurrence already recorded.
        """
        run_stmt, run_params = self._build_schedule_run_insert_stmt(run)
        # Engine-only path, so the monotonic cursor guard always applies: this value came from a
        # poll snapshot and must never walk an operator's move backwards.
        sched_stmt, sched_params = self._build_update_schedule_stmt(
            schedule_id,
            schedule_fields,
            guard_cursor_forward=True,
            expect_next_fire_at=expect_next_fire_at,
        )
        async with self._tx() as conn:
            # The cursor claim goes FIRST so a lost race writes no occurrence at all. Selecting a
            # due schedule and firing it are separate statements, so without this predicate two
            # schedulers reading one due row both commit, each with its own run id.
            result = await conn.execute(sched_stmt, sched_params)
            if result.rowcount == 0:
                return False
            await conn.execute(run_stmt, run_params)
            await self._initialize_managed_entity_in_tx(
                conn,
                entity_type="schedule_run",
                entity_id=run_params["id"],
                status=run_params["status"],
                actor_id="create_schedule_run_and_advance",
            )
        return True

    async def tombstone_and_replace_schedule_run(
        self,
        orphan_id: str,
        replacement_run: dict[str, Any],
        *,
        expected_orphan_status: str = "running",
    ) -> bool:
        """Tombstone an undispatched orphan and insert its replacement in one transaction."""
        run_stmt, run_params = self._build_schedule_run_insert_stmt(replacement_run)
        now = time.time()
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "UPDATE schedule_runs SET status = 'failed', updated_at = :now "
                    "WHERE id = :orphan_id AND status = :expected_status "
                    "AND dispatched_at IS NULL"
                ),
                {"now": now, "orphan_id": orphan_id, "expected_status": expected_orphan_status},
            )
            if result.rowcount == 0:
                return False
            await conn.execute(run_stmt, run_params)
            await self._initialize_managed_entity_in_tx(
                conn,
                entity_type="schedule_run",
                entity_id=run_params["id"],
                status=run_params["status"],
                actor_id="tombstone_and_replace_schedule_run",
            )
        return True

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
        allowed = {
            "status",
            "exit_code",
            "ended_at",
            "error_detail",
            "invocation_id",
            "dispatched_at",
            "resume_packet",
        }
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
            # updated_at must move on every write to this row, not only on a status transition. A
            # field-only call, such as the exit_code/ended_at write that lands before the terminal
            # transition, would otherwise leave it stale, and a stale updated_at is exactly the
            # snapshot value reap_stale_schedule_runs()'s expected_updated_at guard would still
            # match, letting the reaper race a legitimately-finishing run to "timed_out".
            fields = dict(fields)
            fields["updated_at"] = time.time()
            sets = ", ".join(f'"{k}" = :{k}' for k in fields)
            params = dict(fields)
            params["_id"] = run_id
            stmt = text(f"UPDATE schedule_runs SET {sets} WHERE id = :_id")  # noqa: S608
            if "resume_packet" in fields:
                stmt = stmt.bindparams(bindparam("resume_packet", type_=JSON(none_as_null=True)))
            async with self._tx() as conn:
                await conn.execute(stmt, params)

    async def list_schedule_runs(
        self,
        schedule_id: str,
        *,
        status: str | list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conds: list[str] = ["schedule_id = :schedule_id"]
        params: dict[str, Any] = {"schedule_id": schedule_id}
        statuses = [status] if isinstance(status, str) else (status or [])
        if statuses:
            placeholders = ", ".join(f":status{i}" for i in range(len(statuses)))
            conds.append(f"status IN ({placeholders})")
            params.update({f"status{i}": s for i, s in enumerate(statuses)})
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
        statuses: tuple[str, ...] = TERMINAL_RUN_STATUSES,
        fired_after: float | None = None,
    ) -> int:
        """Count runs that fired and reached a terminal status, for max_runs bookkeeping."""
        placeholders = ", ".join(f":status{i}" for i in range(len(statuses)))
        params: dict[str, Any] = {"schedule_id": schedule_id, "chain_depth": chain_depth}
        params.update({f"status{i}": s for i, s in enumerate(statuses)})
        query = f"SELECT COUNT(*) AS n FROM schedule_runs WHERE schedule_id = :schedule_id AND chain_depth = :chain_depth AND status IN ({placeholders})"  # noqa: S608
        if fired_after is not None:
            query += " AND fired_at >= :fired_after"
            params["fired_after"] = fired_after
        async with self._read() as conn:
            row = (await conn.execute(text(query), params)).mappings().first()
        return int(row["n"]) if row else 0

    async def count_schedule_runs_batch(
        self,
        schedule_ids: list[str],
        *,
        chain_depth: int = 0,
        statuses: tuple[str, ...] = TERMINAL_RUN_STATUSES,
    ) -> dict[str, int]:
        """Batched form of count_schedule_runs — one query for many schedule_ids."""
        if not schedule_ids:
            return {}
        id_placeholders = ", ".join(f":id{i}" for i in range(len(schedule_ids)))
        status_placeholders = ", ".join(f":status{i}" for i in range(len(statuses)))
        params: dict[str, Any] = {f"id{i}": sid for i, sid in enumerate(schedule_ids)}
        params["chain_depth"] = chain_depth
        params.update({f"status{i}": s for i, s in enumerate(statuses)})
        query = (
            f"SELECT schedule_id, COUNT(*) AS n FROM schedule_runs "  # noqa: S608
            f"WHERE schedule_id IN ({id_placeholders}) AND chain_depth = :chain_depth "
            f"AND status IN ({status_placeholders}) GROUP BY schedule_id"
        )
        async with self._read() as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
        counts = {r["schedule_id"]: int(r["n"]) for r in rows}
        return {sid: counts.get(sid, 0) for sid in schedule_ids}

    async def schedule_run_streaks(
        self, schedule_ids: list[str]
    ) -> dict[str, tuple[int, str | None]]:
        """Batched form of schedule_run_streak — one query for many schedule_ids."""
        if not schedule_ids:
            return {}
        id_placeholders = ", ".join(f":id{i}" for i in range(len(schedule_ids)))
        params = {f"id{i}": sid for i, sid in enumerate(schedule_ids)}
        query = (
            "SELECT schedule_id, status FROM ("  # noqa: S608
            "  SELECT schedule_id, status, fired_at,"
            "         ROW_NUMBER() OVER ("
            "           PARTITION BY schedule_id ORDER BY fired_at DESC, id DESC"
            "         ) AS rn"
            f"  FROM schedule_runs WHERE schedule_id IN ({id_placeholders}) AND chain_depth = 0"
            ") ranked WHERE rn <= 50 ORDER BY schedule_id, rn"
        )
        async with self._read() as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["schedule_id"], []).append(row["status"])
        result: dict[str, tuple[int, str | None]] = {}
        for sid in schedule_ids:
            statuses = grouped.get(sid)
            if not statuses:
                result[sid] = (0, None)
                continue
            last_status = statuses[0]
            streak = 0
            for status in statuses:
                if status in ("completed", "cancelled"):
                    break
                if status == "failed":
                    streak += 1
            result[sid] = (streak, last_status)
        return result

    async def schedule_health_evidence(self, schedule_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Batched read of the evidence a health verdict needs, per schedule."""
        if not schedule_ids:
            return {}
        id_placeholders = ", ".join(f":id{i}" for i in range(len(schedule_ids)))
        params: dict[str, Any] = {f"id{i}": sid for i, sid in enumerate(schedule_ids)}

        def _latest_per_schedule(extra_where: str) -> str:
            return (
                "SELECT schedule_id, status, fired_at FROM ("  # noqa: S608
                "  SELECT schedule_id, status, fired_at,"
                "         ROW_NUMBER() OVER ("
                "           PARTITION BY schedule_id ORDER BY fired_at DESC, id DESC"
                "         ) AS rn"
                f"  FROM schedule_runs WHERE schedule_id IN ({id_placeholders})"
                f"    AND chain_depth = 0{extra_where}"
                ") ranked WHERE rn = 1"
            )

        executed_placeholders = ", ".join(f":exec{i}" for i in range(len(EXECUTED_RUN_STATUSES)))
        executed_params = dict(params)
        executed_params.update({f"exec{i}": s for i, s in enumerate(EXECUTED_RUN_STATUSES)})

        recorded_query = _latest_per_schedule("")
        executed_query = _latest_per_schedule(f" AND status IN ({executed_placeholders})")

        async with self._read() as conn:
            recorded_rows = (await conn.execute(text(recorded_query), params)).mappings().all()
            executed_rows = (
                (await conn.execute(text(executed_query), executed_params)).mappings().all()
            )
        last_recorded = {r["schedule_id"]: r["fired_at"] for r in recorded_rows}
        last_executed = {r["schedule_id"]: r for r in executed_rows}
        result: dict[str, dict[str, Any]] = {}
        for sid in schedule_ids:
            executed = last_executed.get(sid)
            result[sid] = {
                "last_recorded_run_at": last_recorded.get(sid),
                "last_executed_status": executed["status"] if executed else None,
                "last_executed_run_at": executed["fired_at"] if executed else None,
            }
        return result

    async def sum_schedule_spend(self, schedule_id: str) -> dict[str, Any]:
        """Sum cost and tokens across every session a schedule spawned, for the budget gate."""
        status_placeholders = ", ".join(
            f":status{i}" for i in range(len(SESSION_TERMINAL_STATUSES))
        )
        params: dict[str, Any] = {"schedule_id": schedule_id}
        params.update({f"status{i}": s for i, s in enumerate(SESSION_TERMINAL_STATUSES)})
        query = (
            "SELECT COALESCE(SUM(s.total_cost_usd), 0) AS cost_usd, "  # noqa: S608
            "COALESCE(SUM(s.input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(s.output_tokens), 0) AS output_tokens, "
            "SUM(CASE WHEN s.total_cost_usd IS NULL "
            f"AND s.status IN ({status_placeholders}) THEN 1 ELSE 0 END) AS unreported_sessions "
            "FROM schedule_runs sr JOIN sessions s ON s.invocation_id = sr.invocation_id "
            "WHERE sr.schedule_id = :schedule_id"
        )
        async with self._read() as conn:
            row = (await conn.execute(text(query), params)).mappings().first()
        if not row:
            return {"cost_usd": 0.0, "tokens": 0, "unreported_sessions": 0}
        return {
            "cost_usd": float(row["cost_usd"] or 0),
            "tokens": int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0),
            "unreported_sessions": int(row["unreported_sessions"] or 0),
        }

    # Threshold-alert metrics, studio-wide rather than scoped to a single schedule's runs. These are
    # the VALID_METRICS members one aggregate query answers, which is not all of them:
    # p95_latency_ms needs a sorted sample and github_poll_healthy_age_minutes is a point-in-time
    # gauge, so both get their own branches in metric_value. The invariant is that every member is
    # answered somewhere in metric_value, not that it is answered here.
    _THRESHOLD_METRIC_QUERIES: dict[str, str] = {
        # Counts distinct CAUSES, not rows: a fan-out spawns one session per worker, so a single
        # wall lands as many rows carrying one cause, and a fan-out wider than the threshold would
        # breach it by construction. Both columns fall back to a per-row value rather than grouping
        # on NULL, and that is the whole correctness of this query: NULL is not a shared value, and
        # most failed sessions carry no invocation id, so the naive form collapses nearly the
        # population to one row per reason and the alarm stops being able to fire. The fallback is
        # namespace-tagged rather than bare, so no value from one side can equal a value from the
        # other; an untagged collision would count two distinct causes once, which is the direction
        # that suppresses an alert.
        "failed_sessions": (
            "SELECT COUNT(*) AS n FROM ("
            "SELECT DISTINCT CASE WHEN invocation_id IS NULL "
            "THEN 'session:' || id ELSE 'invocation:' || invocation_id END AS cause_group, "
            "CASE WHEN status_reason_code IS NULL "
            "THEN 'session:' || id ELSE 'reason:' || status_reason_code END AS cause_class "
            "FROM sessions "
            "WHERE status IN ('failed', 'timed_out') "
            "AND COALESCE(ended_at, started_at, created_at) >= :window_start"
            # PostgreSQL requires a name for a subquery in FROM and SQLite does not, so an unaliased
            # form runs here and fails only on the other dialect.
            ") AS causes"
        ),
        "total_cost_usd": (
            "SELECT COALESCE(SUM(total_cost_usd), 0) AS n FROM sessions "
            "WHERE COALESCE(ended_at, started_at, created_at) >= :window_start"
        ),
        # Attribution metric rather than an alarm on its own: longest 401 streak across enabled
        # github_poll schedules, so a payload can tell a token problem from a network blip. Point-in-
        # time like the age gauge below, so :window_start is unused.
        "github_poll_consecutive_401": (
            "SELECT COALESCE(MAX(poller_consecutive_401), 0) AS n FROM schedules "
            "WHERE trigger_type = 'github_poll' AND enabled = 1"
        ),
    }

    async def metric_value(self, metric: str, window_start: float) -> float:
        """Aggregate a threshold-alert metric over [window_start, now)."""
        if metric == "github_poll_healthy_age_minutes":
            async with self._read() as conn:
                row = (
                    (
                        await conn.execute(
                            text(
                                "SELECT MAX(last_healthy_poll_at) AS n FROM schedules "
                                "WHERE trigger_type = 'github_poll' AND enabled = 1"
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
            last_healthy = row["n"] if row else None
            if last_healthy is None:
                # No enabled github_poll schedule has ever recorded a healthy poll, including the
                # case where none exists at all. There is nothing to be blind about, so report a
                # fresh age rather than a stale one that would falsely alarm on a DB with no
                # observed repo yet.
                return 0.0
            return (time.time() - float(last_healthy)) / 60.0

        if metric == "p95_latency_ms":
            async with self._read() as conn:
                rows = (
                    (
                        await conn.execute(
                            text(
                                "SELECT (ended_at - started_at) AS latency_sec FROM invocations "
                                "WHERE started_at >= :window_start AND ended_at IS NOT NULL "
                                "AND ended_at >= started_at"
                            ),
                            {"window_start": window_start},
                        )
                    )
                    .mappings()
                    .all()
                )
            if not rows:
                return 0.0
            latencies = sorted(float(r["latency_sec"]) * 1000.0 for r in rows)
            idx = min(len(latencies) - 1, max(0, -(-95 * len(latencies) // 100) - 1))
            return latencies[idx]

        query = self._THRESHOLD_METRIC_QUERIES.get(metric)
        if query is None:
            raise ValueError(f"Unknown threshold metric: {metric!r}")
        async with self._read() as conn:
            row = (
                (await conn.execute(text(query), {"window_start": window_start})).mappings().first()
            )
        return float(row["n"]) if row and row["n"] is not None else 0.0

    async def metric_unreported_sessions(self, metric: str, window_start: float) -> int:
        """Count terminal sessions in the metric's window with a NULL total_cost_usd."""
        if metric != "total_cost_usd":
            return 0
        status_placeholders = ", ".join(
            f":status{i}" for i in range(len(SESSION_TERMINAL_STATUSES))
        )
        params: dict[str, Any] = {"window_start": window_start}
        params.update({f"status{i}": s for i, s in enumerate(SESSION_TERMINAL_STATUSES)})
        query = (
            "SELECT COUNT(*) AS n FROM sessions "  # noqa: S608
            "WHERE COALESCE(ended_at, started_at, created_at) >= :window_start "
            "AND total_cost_usd IS NULL "
            f"AND status IN ({status_placeholders})"
        )
        async with self._read() as conn:
            row = (await conn.execute(text(query), params)).mappings().first()
        return int(row["n"]) if row and row["n"] is not None else 0

    async def schedule_run_streak(self, schedule_id: str) -> tuple[int, str | None]:
        """Consecutive terminal 'failed' streak and most recent status, newest-first, capped at 50 rows."""
        query = """SELECT status FROM schedule_runs
                   WHERE schedule_id = :schedule_id AND chain_depth = 0
                   ORDER BY fired_at DESC, id DESC LIMIT 50"""  # noqa: S608
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

    async def schedule_run_exists_since(self, schedule_id: str, since: float) -> bool:
        """True if *schedule_id* has a genuinely-fired top-level occurrence row since *since*."""
        async with self._read() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM schedule_runs "
                        "WHERE schedule_id = :schedule_id AND fired_at >= :since "
                        "AND status != 'skipped' AND chain_depth = 0 LIMIT 1"
                    ),
                    {"schedule_id": schedule_id, "since": since},
                )
            ).first()
        return row is not None

    async def list_undispatched_schedule_runs(self) -> list[dict[str, Any]]:
        """Occurrence rows that committed but whose external process launch was never confirmed."""
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM schedule_runs WHERE status = 'running' "
                            "AND dispatched_at IS NULL AND schedule_id IS NOT NULL"
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_dict(r) for r in rows]

    async def list_dispatched_running_schedule_runs(self) -> list[dict[str, Any]]:
        """Occurrence rows dispatched but never terminal; candidates, not a liveness verdict."""
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM schedule_runs WHERE status = 'running' "
                            "AND dispatched_at IS NOT NULL AND schedule_id IS NOT NULL "
                            "AND invocation_id IS NOT NULL"
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_dict(r) for r in rows]

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
        """Look up the schedule_run that fired a given invocation."""
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

    # Invocations

    async def create_invocation(self, invocation: dict[str, Any]) -> None:
        status = invocation.get("status", "running")
        _validate_enum(
            "status",
            status,
            _INVOCATION_STATUSES,
            adr="ADR-0057",
            nullable=False,
        )
        now = time.time()
        async with self._tx() as conn:
            result = await conn.execute(
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
            if result.rowcount:
                await self._initialize_managed_entity_in_tx(
                    conn,
                    entity_type="invocation",
                    entity_id=invocation["id"],
                    status=status,
                    actor_id="create_invocation",
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
                adr="ADR-0057",
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
        plugin: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        connection: AsyncConnection | None = None,
    ) -> list[dict[str, Any]]:
        # Per invocation, take project/project_source from its latest-updated session; ROW_NUMBER()
        # is portable where the old SQLite idiom is rejected by PostgreSQL. The schedule_run that
        # fired the invocation is surfaced alongside so the UI can show why a scheduled run failed
        # without a second round-trip, via correlated scalar subqueries rather than a ranked derived
        # table: ORDER BY with LIMIT narrows to the emitted page first, then each subquery runs once
        # per emitted row against the partial index on schedule_runs(invocation_id).
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
            "  ) AS schedule_run_error_detail, "
            "  ( SELECT sr.action_kind FROM schedule_runs sr "
            "    WHERE sr.invocation_id = inv.id "
            "    ORDER BY COALESCE(sr.created_at, 0) DESC, sr.id DESC LIMIT 1 "
            "  ) AS action_kind "
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
        if plugin:
            conds.append("inv.plugin = :plugin")
            params["plugin"] = plugin
        if status:
            conds.append("inv.status = :status")
            params["status"] = status
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY inv.updated_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset
        async with self._read_connection(connection) as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
        return [self._row_to_dict(r) for r in rows]

    async def list_running_invocations_for_reaping(
        self,
        *,
        limit: int = 500,
        after_started_at: float | None = None,
        after_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one oldest-first keyset page for the lifecycle reaper.

        This deliberately avoids ``list_invocations``'s UI projection and
        offset pagination. Reaping changes rows from running to terminal while
        it scans, so offsets would skip rows as the result set shrank; the
        immutable ``(started_at, id)`` cursor remains stable across those
        transitions. Only the fields the reaper evaluates are projected.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if (after_started_at is None) != (after_id is None):
            raise ValueError("after_started_at and after_id must be provided together")

        query = (
            "SELECT inv.id, inv.started_at, inv.updated_at, inv.session_count, inv.ended_at, "
            "  ( SELECT sr.action_kind FROM schedule_runs sr "
            "    WHERE sr.invocation_id = inv.id "
            "    ORDER BY COALESCE(sr.created_at, 0) DESC, sr.id DESC LIMIT 1 "
            "  ) AS action_kind "
            "FROM invocations inv "
            "WHERE inv.status = 'running'"
        )
        params: dict[str, Any] = {"limit": limit}
        if after_started_at is not None:
            query += " AND (inv.started_at, inv.id) > (:after_started_at, :after_id)"
            params["after_started_at"] = after_started_at
            params["after_id"] = after_id
        query += " ORDER BY inv.started_at ASC, inv.id ASC LIMIT :limit"

        async with self._read() as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
        return [self._row_to_dict(r) for r in rows]

    async def count_invocations(
        self,
        *,
        skill: str | None = None,
        plugin: str | None = None,
        status: str | None = None,
        connection: AsyncConnection | None = None,
    ) -> int:
        """Real total matching the same filters ``list_invocations`` accepts, which is paginated."""
        conds: list[str] = []
        params: dict[str, Any] = {}
        if skill:
            conds.append("skill = :skill")
            params["skill"] = skill
        if plugin:
            conds.append("plugin = :plugin")
            params["plugin"] = plugin
        if status:
            conds.append("status = :status")
            params["status"] = status
        query = "SELECT COUNT(*) AS n FROM invocations"
        if conds:
            query += " WHERE " + " AND ".join(conds)
        async with self._read_connection(connection) as conn:
            row = (await conn.execute(text(query), params)).mappings().first()
        return row["n"]

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

    async def list_sessions_for_invocations(
        self,
        invocation_ids: list[str],
        *,
        connection: AsyncConnection | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return child sessions grouped by invocation in one bounded query."""
        ids = list(dict.fromkeys(invocation_ids))
        grouped: dict[str, list[dict[str, Any]]] = {invocation_id: [] for invocation_id in ids}
        if not ids:
            return grouped

        params = {
            f"invocation_id_{index}": invocation_id for index, invocation_id in enumerate(ids)
        }
        placeholders = ", ".join(f":invocation_id_{index}" for index in range(len(ids)))
        query = (
            "SELECT * FROM sessions "  # noqa: S608 -- generated names; values stay bound
            f"WHERE invocation_id IN ({placeholders}) "
            "ORDER BY invocation_id ASC, created_at ASC"
        )
        async with self._read_connection(connection) as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
        for row in rows:
            data = self._row_to_dict(row)
            grouped[data["invocation_id"]].append(data)
        return grouped

    # Artifacts

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
        """Upsert one structured artifact in a single statement; return its stable id."""
        if not kind:
            raise ValueError("artifact kind is required")
        if not name:
            raise ValueError("artifact name is required")
        if file_path is not None:
            # Studio artifact file references must stay relative and non-traversing before they can
            # be served, so absolute paths and `..` are rejected at write time rather than trusted.
            _check_path_safe(file_path, "file_path")
        now = time.time()
        art_id = uuid.uuid4().hex[:12]
        if invocation_id is not None and session_id is not None:
            conflict = (
                "(invocation_id, session_id, kind, name) "
                "WHERE invocation_id IS NOT NULL AND session_id IS NOT NULL"
            )
        elif invocation_id is not None:
            conflict = (
                "(invocation_id, kind, name) WHERE invocation_id IS NOT NULL AND session_id IS NULL"
            )
        elif session_id is not None:
            conflict = (
                "(session_id, kind, name) WHERE session_id IS NOT NULL AND invocation_id IS NULL"
            )
        else:
            conflict = "(kind, name) WHERE invocation_id IS NULL AND session_id IS NULL"
        async with self._tx() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "INSERT INTO artifacts "  # noqa: S608
                            "(id, invocation_id, session_id, created_at, updated_at, kind, name, content, file_path) "
                            "VALUES (:id, :inv_id, :ses_id, :now, :now2, :kind, :name, :content, :fp) "
                            f"ON CONFLICT {conflict} DO UPDATE SET "
                            "content = excluded.content, file_path = excluded.file_path, "
                            "updated_at = excluded.updated_at "
                            "RETURNING id"
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
                )
                .mappings()
                .first()
            )
        return row["id"]

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

    # Admin events

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
            # Batch actions affect many rows but record a single event with target_id=NULL and the
            # affected ids inside `details`, so an exact target_id match alone would return nothing
            # for them. `details` is JSON stored as TEXT, so a substring match over its serialized
            # form catches those ids.
            conds.append("(target_id = :target_id OR details LIKE :target_like)")
            params["target_id"] = target_id
            params["target_like"] = f'%"{target_id}"%'
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY created_at DESC LIMIT :limit"
        params["limit"] = limit
        async with self._read() as conn:
            rows = (await conn.execute(text(query), params)).mappings().all()
        return [self._row_to_dict(r) for r in rows]

    # Branches

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
        async with self._tx() as conn:
            await self._update_branch_in_tx(conn, branch_id, **fields)

    async def _update_branch_in_tx(self, conn, branch_id: str, **fields: Any) -> None:
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
        await conn.execute(stmt, params)

    async def finalize_branch(
        self, branch_id: str, *, status: str, ended_at: float | None = None
    ) -> bool:
        """Guarded terminal write for a branch row: *status* terminal, row still pre-terminal."""
        if status not in _BRANCH_TERMINAL_STATUSES:
            return False
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "UPDATE branches SET status = :status, ended_at = :ended_at "
                    "WHERE id = :id AND (status IS NULL OR status = 'running')"
                ),
                {
                    "status": status,
                    "ended_at": ended_at if ended_at is not None else time.time(),
                    "id": branch_id,
                },
            )
        return result.rowcount > 0

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

    # Shows

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
            result = await conn.execute(
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
            if result.rowcount:
                await self._initialize_managed_entity_in_tx(
                    conn,
                    entity_type="show",
                    entity_id=show["id"],
                    status=show.get("status", "active"),
                    actor_id="create_show",
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

    # Plays

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
            result = await conn.execute(
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
            if result.rowcount:
                await self._initialize_managed_entity_in_tx(
                    conn,
                    entity_type="play",
                    entity_id=play["id"],
                    status=play.get("status", "pending"),
                    actor_id="create_play",
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

    # Definitions

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

    async def list_latest_definition_versions(self) -> list[dict[str, Any]]:
        """Latest version and timestamp for every definition, in one read."""
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT kind, name, MAX(version) AS version,"
                            " MAX(created_at) AS created_at FROM definitions"
                            " GROUP BY kind, name"
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    # Session signals

    async def insert_session_signal(
        self,
        *,
        session_id: str,
        kind: str,
        op_id: str = "",
        ts: float,
        payload: dict[str, Any],
    ) -> int:
        """Append one lifecycle signal row; returns the assigned seq number."""
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

    # Engine runs

    async def insert_engine_run(
        self,
        *,
        run_id: str,
        kind: str,
        spec_json: dict[str, Any],
        started_at: float,
        session_id: str | None = None,
    ) -> None:
        """Insert a new engine run row with status='running'."""
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
        """Update an engine run's mutable fields; *status* must be a terminal outcome."""
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

    async def set_engine_run_lineage(
        self,
        run_id: str,
        *,
        invocation_id: str | None,
        signal_session_id: str | None,
        parent_session_id: str | None,
    ) -> None:
        """Attach the three non-interchangeable identities for one engine execution."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE engine_runs SET invocation_id = :invocation_id, "
                    "signal_session_id = :signal_session_id, "
                    "parent_session_id = :parent_session_id WHERE id = :id"
                ),
                {
                    "id": run_id,
                    "invocation_id": invocation_id,
                    "signal_session_id": signal_session_id,
                    "parent_session_id": parent_session_id,
                },
            )

    async def record_engine_run_outcome(self, run_id: str, outcome_json: dict[str, Any]) -> None:
        """Persist the bounded terminal envelope separately from failure text."""
        async with self._tx() as conn:
            await conn.execute(
                text("UPDATE engine_runs SET outcome_json = :outcome WHERE id = :id").bindparams(
                    bindparam("outcome", type_=JSON)
                ),
                {"id": run_id, "outcome": outcome_json},
            )

    async def get_engine_run(self, run_id: str) -> dict[str, Any] | None:
        """Return a single engine run row as a dict, or None if not found."""
        async with self._read() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, kind, spec_json, status, started_at, ended_at, "
                            "session_id, invocation_id, signal_session_id, parent_session_id, "
                            "outcome_json, export_dir, error "
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
        if isinstance(d.get("outcome_json"), str):
            try:
                d["outcome_json"] = json.loads(d["outcome_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    async def list_engine_run_summaries(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        before_started_at: float | None = None,
        before_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return a deterministic keyset page without selecting stored input."""
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        if kind is not None:
            conditions.append("kind = :kind")
            params["kind"] = kind
        if status is not None:
            conditions.append("status = :status")
            params["status"] = status
        if session_id is not None:
            conditions.append(
                "(signal_session_id = :session_id OR parent_session_id = :session_id "
                "OR (signal_session_id IS NULL AND parent_session_id IS NULL "
                "AND session_id = :session_id))"
            )
            params["session_id"] = session_id
        if before_started_at is not None or before_id is not None:
            if before_started_at is None or before_id is None:
                raise ValueError("engine run cursor requires both started_at and id")
            conditions.append("(started_at, id) < (:before_started_at, :before_id)")
            params["before_started_at"] = before_started_at
            params["before_id"] = before_id
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = (
            "SELECT id, kind, status, started_at, ended_at, session_id, "  # noqa: S608
            "invocation_id, signal_session_id, parent_session_id, outcome_json, "
            "CASE WHEN export_dir IS NULL THEN 0 ELSE 1 END AS has_output, "
            "CASE WHEN error IS NULL THEN 0 ELSE 1 END AS has_error FROM engine_runs "
            f"{where} ORDER BY started_at DESC, id DESC LIMIT :limit"
        )
        async with self._read() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("outcome_json"), str):
                try:
                    item["outcome_json"] = json.loads(item["outcome_json"])
                except (json.JSONDecodeError, TypeError):
                    item["outcome_json"] = None
            result.append(item)
        return result

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

    # Engine definitions

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

    # Workflow definitions

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

    # Session controls, the live-control transport: rows written by `li o ctl pause|resume|msg` and
    # consumed by the control poller in _execute_dag. Apply ordering is verb-classed by the poller,
    # not by these methods: pause and resume are idempotent and finalize directly, while message
    # claims the row first, so a crash between claim and apply leaves a visible 'applying:<owner>'
    # row instead of a silent double-injection risk. A claimed row is never resolved by anything but
    # its own claimant; see docs/internals/state-db.md.

    async def insert_session_control(
        self,
        *,
        session_id: str,
        verb: str,
        payload: dict[str, Any] | None = None,
        created_at: float | None = None,
        project: str | None = None,
    ) -> str | None:
        """Queue a control verb for *session_id*; returns its id, or None if it was not admitted."""
        control_id = uuid.uuid4().hex
        # Four complete literals, selected rather than assembled: the statement text never depends
        # on a runtime value, and the ownership predicate is visible in the SQL that actually runs.
        # :project stays a bound parameter.
        if self.dialect == "sqlite":
            admit_source = (
                "WHERE EXISTS (SELECT 1 FROM sessions WHERE id = :sid "
                "AND status = 'running' AND project = :project)"
                if project is not None
                else "WHERE EXISTS (SELECT 1 FROM sessions WHERE id = :sid AND status = 'running')"
            )
        else:
            admit_source = (
                "FROM (SELECT 1 FROM sessions WHERE id = :sid "
                "AND status = 'running' AND project = :project FOR UPDATE) _admitted"
                if project is not None
                else "FROM (SELECT 1 FROM sessions WHERE id = :sid "
                "AND status = 'running' FOR UPDATE) _admitted"
            )
        params = {
            "id": control_id,
            "sid": session_id,
            "verb": verb,
            "payload": payload,
            "created_at": created_at if created_at is not None else time.time(),
        }
        if project is not None:
            params["project"] = project
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "INSERT INTO session_controls "
                    "(id, session_id, verb, payload, created_at, applied_at, "
                    "claimed_at, result) "
                    "SELECT :id, :sid, :verb, :payload, :created_at, NULL, NULL, NULL "
                    f"{admit_source}"  # noqa: S608 — dialect-selected literal, no caller input
                ).bindparams(bindparam("payload", type_=JSON)),
                params,
            )
            if not result.rowcount:
                return None
        return control_id

    async def list_pending_session_controls(self, session_id: str) -> list[dict[str, Any]]:
        """Unapplied controls for *session_id*, oldest first, including rows mid-apply."""
        async with self._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, session_id, verb, payload, created_at, applied_at, "
                            "claimed_at, result "
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

    async def mark_session_control_applying(
        self, control_id: str, *, owner: str | None = None
    ) -> str | None:
        """Claim a non-idempotent control as mid-apply; returns the claim, or None if taken."""
        claim = f"applying:{owner}" if owner else "applying"
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "UPDATE session_controls SET result = :claim, claimed_at = :now "
                    "WHERE id = :id AND result IS NULL"
                ),
                {
                    "claim": claim,
                    "now": time.time(),
                    "id": control_id,
                },
            )
            return claim if result.rowcount else None

    async def finalize_session_control(
        self,
        control_id: str,
        *,
        result: str,
        expect_claim: str | None = None,
        only_if_unclaimed: bool = False,
    ) -> bool:
        """Stamp applied_at and a terminal *result*, optionally guarded by claim or pending."""
        if expect_claim is not None and only_if_unclaimed:
            raise ValueError(
                "finalize_session_control takes expect_claim or only_if_unclaimed, "
                "not both: a row cannot be simultaneously claimed by a given "
                "consumer and unclaimed"
            )
        params: dict[str, Any] = {
            "applied_at": time.time(),
            "result": result,
            "id": control_id,
        }
        sql = (
            "UPDATE session_controls SET applied_at = :applied_at, result = :result WHERE id = :id"
        )
        if expect_claim is not None:
            sql += " AND result = :expect_claim"
            params["expect_claim"] = expect_claim
        elif only_if_unclaimed:
            sql += " AND result IS NULL"
        async with self._tx() as conn:
            written = await conn.execute(text(sql), params)
            return bool(written.rowcount)

    async def resolve_claimed_session_control(
        self, control_id: str, *, outcome: str, actor: str | None = None
    ) -> str | None:
        """Close a control whose claimant never reported back; None if it is not claimed."""
        # The read decides and the write checks that the decision still holds. On PostgreSQL the
        # claimant can commit its own outcome between the two statements, so the compare-and-set
        # refuses to overwrite it, and the row count is what stops this returning a receipt for a
        # write that never happened.
        async with self._tx() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT result FROM session_controls WHERE id = :id"),
                        {"id": control_id},
                    )
                )
                .mappings()
                .first()
            )
            prior = (row or {}).get("result")
            if not str(prior or "").startswith("applying"):
                return None
            stored = (
                f"{outcome}: resolved by {actor or 'an unnamed operator'} after "
                f"the claim {prior!r} was taken and never reported back"
            )
            written = await conn.execute(
                text(
                    "UPDATE session_controls SET applied_at = :applied_at, result = :result "
                    "WHERE id = :id AND result = :prior"
                ),
                {
                    "applied_at": time.time(),
                    "result": stored,
                    "id": control_id,
                    "prior": prior,
                },
            )
            if not written.rowcount:
                return None
        return stored

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

    # Helpers

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        d = dict(row)
        if "embedding" in d:
            d["embedding"] = _unpack_embedding(d["embedding"])
        if "ended_at_is_approximate" in d:
            d["ended_at_is_approximate"] = bool(d["ended_at_is_approximate"])
        for key in (
            "node_metadata",
            "content",
            "depends_on",
            "on_success",
            "on_fail",
            "github_filter",
            "action_extra_args",
            "action_command_args",
            "trigger_context",
            "action_args",
            "threshold_config",
            "rate_limit",
            "notify_on",
            "authored_spec",
            "resolved_target",
            "artifact_contract_json",
            "artifact_verification_json",
            "status_evidence_refs",
            "payload",
            "resume_packet",
        ):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d


# Shared singleton accessor: one open StateDB per DB URL, reused across hook firings, which avoids
# the per-firing connect and schema-check cost. Keyed by normalized URL so tests redirecting
# DEFAULT_DB_PATH get their own isolated instance.

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
    # Resolve the key through StateDB's own cascade so a monkeypatched DEFAULT_DB_PATH is honored;
    # normalize_state_db_url(None) would bypass it to the real home db.
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
    # Hold the open lock so an in-flight get_shared_db()/register_shared_db() cannot repopulate
    # _SHARED after the sweep; bump the generation and null the lock last, so a waiter that raced
    # this close refuses to resurrect it.
    async with lock:
        instances = list(_SHARED.values())
        _SHARED.clear()
        for db in instances:
            with contextlib.suppress(Exception):
                await db.close()
        _SHARED_CLOSE_GEN += 1
        _SHARED_OPEN_LOCK = None
