# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

# Dialect differences, concurrency arguments and migration order are documented in
# docs/internals/state-db.md rather than inline here.

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal, NamedTuple
from urllib.parse import parse_qsl, urlencode

from sqlalchemy import JSON, MetaData, bindparam, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.schema import CreateTable

from lionagi._paths import LIONAGI_HOME, ensure_lionagi_dir
from lionagi.config import settings
from lionagi.libs.path_safety import check_path_safe as _check_path_safe
from lionagi.ln import json_dumps as _json_dumps
from lionagi.ln import short_id
from lionagi.ln.concurrency import (
    CancelScope,
    Lock,
    fail_after,
    get_cancelled_exc_class,
    run_sync,
    shield,
)
from lionagi.ln.types import SingletonType
from lionagi.state.compat import (
    _LINEAGE_KEY,
    _NATIVE_LINEAGE,
    CompatibilityProfile,
    StateCompatibilityRefused,
    _names_another_application,
    _operation,
    _sql_tokens,
    _statement_kind,
)
from lionagi.state.compat import _Guard as _CompatibilityGuard
from lionagi.state.compat import _quote as _quote_identifier
from lionagi.state.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    dialect_of,
    make_compatibility_engine,
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
from lionagi.state.lifecycle.models import (
    ScheduleAuditContext as _ScheduleAuditContext,
)
from lionagi.state.lifecycle.models import (
    TransitionCommand as _TransitionCommand,
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
    ScheduleDefinitionReasons as _ScheduleDefinitionReasons,
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


class NoCursorClaim(SingletonType):
    """The type of :data:`NO_CURSOR_CLAIM`, so a claim is not typed as ``Any`` end to end."""

    __slots__ = ()

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return "NO_CURSOR_CLAIM"

    def __reduce__(self):
        return "NO_CURSOR_CLAIM"


class ScheduleOwnershipConflictError(RuntimeError):
    """A ScheduleSet update no longer matches the row state it planned against."""

    def __init__(self, schedule_ids: list[str]) -> None:
        self.schedule_ids = schedule_ids
        super().__init__("schedule row changed under this apply: " + ", ".join(schedule_ids))


# Distinguishes "this write claims no due instant" from "it claims one whose value is NULL".
# Public because callers several layers up have to state which of the two they mean.
NO_CURSOR_CLAIM = NoCursorClaim()

# What a caller may claim a cursor column still holds: the value it read, including NULL, or
# the sentinel for a write that claims nothing. Spelled out so a reader of any of the three
# layers that forward it can tell a cursor value from a missing claim.
CursorClaim = float | str | None | NoCursorClaim

# Shape of the schema this code applies; ``_apply_schema`` stamps it into ``schema_meta.version`` on
# every open, so the recorded version describes the database after migrations. Bump it whenever a
# migration changes the shape a reader would see.
SCHEMA_VERSION = "6"
_SCHEMA_MIGRATION_LOCK_KEY = "lionagi.state.schema.migration"
_DISPATCHED_AT_BACKFILL_KEY = "migration.dispatched_at_backfill"
_ATTENTION_DISPOSITIONS_BACKFILL_KEY = "migration.attention_dispositions_backfill"
_IMPORTED_ROLE_LABEL_BACKFILL_KEY = "migration.imported_role_label_backfill"
_SESSION_ENDED_AT_BACKFILL_KEY = "migration.session_ended_at_backfill"
_TERMINAL_ELIGIBILITY_BACKFILL_KEY = "migration.terminal_delivery_eligibility"
_SESSION_ENDED_AT_BACKFILL_BATCH_SIZE = 500
# The ``schema_meta.version`` values a lionagi release writes: a whole number, no sign or padding.
_RECORDED_VERSION = re.compile(r"[1-9][0-9]*")
# The Studio Operator store keeps its tables in the StateDB file and creates them itself, so they can
# be there before any StateDB open has written schema_meta (lionagi/studio/operator/store.py).
_OPERATOR_TABLES = frozenset(
    {
        "studio_operator_conversations",
        "studio_operator_effects",
        "studio_operator_frames",
        "studio_operator_proposals",
        "studio_operator_turns",
        "studio_operator_views",
    }
)
# Every table a lionagi component creates in a store; a store with no schema_meta table, or one
# with no lineage row and no sessions table, is this package's only when it holds nothing else
# (ADR-0128 D7).
_LIONAGI_TABLES = frozenset(metadata.tables) | _OPERATOR_TABLES


class SchemaTooNewError(RuntimeError):
    """``schema_meta.version`` is higher than ``SCHEMA_VERSION``; only a writable open raises it."""


class ForeignLineageError(RuntimeError):
    """``schema_meta.lineage`` names another application; only a writable native open raises it."""


class UnrecognizedStoreError(RuntimeError):
    """The store is neither new nor one lionagi created; only a writable native open raises it."""


class UnreadableStoreError(RuntimeError):
    """The store's journal or write-ahead log is present and could not be read, so this code cannot
    tell whether it holds another application's ``lineage`` row, a newer version or a table
    lionagi never creates; only a writable native open raises it."""


# What a writable native open refuses a store with (ADR-0128 D1, D7). A refused open leaves no
# engine, and a step of the open that meets one passes it on as it is.
_NATIVE_OPEN_REFUSALS = (
    ForeignLineageError,
    UnrecognizedStoreError,
    UnreadableStoreError,
    SchemaTooNewError,
    StateCompatibilityRefused,
)


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
        "cli_worker_generation",
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
    "session": frozenset(
        {
            "ended_at",
            "duration_ms",
            "ended_at_is_approximate",
            "node_metadata",
            "current_phase",
            "cli_worker_generation",
        }
    ),
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


def _is_utf8_str(value: Any) -> bool:
    """Whether ``value`` is a string that encodes as UTF-8 (no lone surrogate)."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# Session columns naming one row of another table. In compatibility mode each is a link D5 checks
# before the write, and each is checked again inside the write's transaction so a row that changed
# hands between the two is still refused. ``created`` says whether the row must also be one this
# run created: a message link is, while a session's invocation is checked by owner alone, exactly
# as create_session checks it. ``progression_id`` is deliberately absent: it is the session's own
# progression, fixed at creation, and is not an updatable session column.
_SESSION_LINKS: tuple[tuple[str, str, str, bool], ...] = (
    ("invocation_id", "invocations", "invocation", False),
    ("first_msg_id", "messages", "message", True),
    ("last_msg_id", "messages", "message", True),
)


def _set_if_null_expression(column: str, dialect: str) -> str:
    """The SET expression of a ``set_if_null`` column: fill only where the stored value is absent.

    A stored JSON ``null`` is an absence, not a value. ``COALESCE`` alone reads the stored text
    ``null`` as present, so a fill over a store that wrote one leaves the null in place and the
    value handed to the column vanishes without a trace. ``node_metadata`` is compared against
    ``null`` for that reason; every other column keeps the plain ``COALESCE``.
    """
    if column != "node_metadata":
        return f'COALESCE("{column}", :{column})'
    if dialect == "sqlite":
        # json_valid guards json_type(), which raises on the malformed text a sqlite column holds.
        return (
            f'CASE WHEN json_valid("{column}") AND json_type("{column}") = \'null\' '
            f'THEN :{column} ELSE COALESCE("{column}", :{column}) END'
        )
    return (
        f"CASE WHEN (\"{column}\")::text = 'null' "
        f'THEN :{column} ELSE COALESCE("{column}", :{column}) END'
    )


def _decode_node_metadata(value: Any) -> Any:
    """A stored or given node_metadata as a JSON value; text that is not JSON stays text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


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
    if values is None:
        return None
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


_SCHEMA_META_GUARD_ROWS = "SELECT key, value FROM schema_meta WHERE key IN ('lineage', 'version')"
_SQLITE_TABLE_NAMES = (
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite~_%' ESCAPE '~'"
)
_SQLITE_SCHEMA_META_COLUMNS = "SELECT name FROM pragma_table_info('schema_meta')"


_CLI_WORKER_OPERATION_TIMEOUT_SECONDS = 5.0
_CLI_WORKER_PRUNE_CHUNK = 500
_CLI_WORKER_PRODUCER_FIELDS = (
    "spawn_id",
    "session_id",
    "generation_id",
    "run_id",
    "engine_run_id",
    "branch_id",
    "pid",
    "pgid",
    "pid_create_time",
    "pid_host",
    "pid_boot_time",
    "process_identity_mode",
)
_CLI_WORKER_DIAGNOSTIC_FIELDS = frozenset({"run_id", "engine_run_id", "branch_id"})
_CLI_WORKER_STORED_FIELDS = (*_CLI_WORKER_PRODUCER_FIELDS, "recorded_at", "confirmed_empty_at")


def _validate_cli_worker_uuid(value: object, *, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be UUID text")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be UUID text") from exc


def _validate_cli_worker_generation(value: object) -> str | None:
    return _validate_cli_worker_uuid(value, field="cli_worker_generation", nullable=True)


def _validate_cli_worker_timestamp(value: object, *, field: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


def _validate_cli_worker_session_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("session_id must be nonempty text")
    return value


def _validate_cli_worker_spawn(record: Mapping[str, Any]) -> dict[str, Any]:
    """Copy and validate producer fields without accepting store-owned timestamps."""
    if not isinstance(record, Mapping) or any(not isinstance(key, str) for key in record):
        raise ValueError("a worker spawn must be a mapping with text field names")
    unknown = set(record) - set(_CLI_WORKER_PRODUCER_FIELDS)
    missing = set(_CLI_WORKER_PRODUCER_FIELDS) - _CLI_WORKER_DIAGNOSTIC_FIELDS - set(record)
    if unknown or missing:
        raise ValueError(
            f"invalid worker spawn fields: unknown={sorted(unknown)}, missing={sorted(missing)}"
        )
    result = {field: record.get(field) for field in _CLI_WORKER_PRODUCER_FIELDS}
    for field in ("spawn_id", "generation_id"):
        result[field] = _validate_cli_worker_uuid(result[field], field=field)
    result["session_id"] = _validate_cli_worker_session_id(result["session_id"])
    for field in _CLI_WORKER_DIAGNOSTIC_FIELDS:
        value = result[field]
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{field} must be nonempty text or None")
    for field in ("pid", "pgid"):
        if type(result[field]) is not int or result[field] <= 1:
            raise ValueError(f"{field} must be an integer greater than one")
    if result["pid"] != result["pgid"]:
        raise ValueError("a worker spawn must have pid equal to pgid")
    for field in ("pid_create_time", "pid_boot_time"):
        result[field] = _validate_cli_worker_timestamp(result[field], field=field)
    host = result["pid_host"]
    if not isinstance(host, str) or not host.strip():
        raise ValueError("pid_host must be nonempty host text")
    if (
        not isinstance(result["process_identity_mode"], str)
        or result["process_identity_mode"] != "local"
    ):
        raise ValueError("process_identity_mode must be local")
    return result


def _validate_cli_worker_spawn_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a complete stored row separately from the producer payload."""
    if set(row) != set(_CLI_WORKER_STORED_FIELDS):
        raise ValueError("stored worker spawn has incomplete or unknown fields")
    _validate_cli_worker_spawn({field: row[field] for field in _CLI_WORKER_PRODUCER_FIELDS})
    _validate_cli_worker_timestamp(row["recorded_at"], field="recorded_at")
    if row["confirmed_empty_at"] is not None:
        _validate_cli_worker_timestamp(row["confirmed_empty_at"], field="confirmed_empty_at")
    return dict(row)


async def _locked_cli_worker_prune_candidates(
    conn: AsyncConnection, session_ids: Sequence[str], *, dialect: str
) -> list[str]:
    """Lock caller-owned candidates, then exclude every unresolved worker owner.

    The caller keeps this transaction open and rechecks its retention predicate
    before archiving, changing dependents or deleting any returned session.
    SQLite callers enter through their existing writer transaction.
    """
    if dialect not in {"sqlite", "postgresql"}:
        raise ValueError("worker retention requires a SQLite or PostgreSQL transaction")
    if not conn.in_transaction():
        raise ValueError("worker retention requires the caller's active transaction")
    ids = sorted({_validate_cli_worker_session_id(value) for value in session_ids})
    if not ids:
        return []
    if dialect == "postgresql":
        await conn.execute(text("SET LOCAL lock_timeout = '250ms'"))
    eligible: list[str] = []
    for start in range(0, len(ids), _CLI_WORKER_PRUNE_CHUNK):
        chunk = ids[start : start + _CLI_WORKER_PRUNE_CHUNK]
        parameters = {f"sid{i}": value for i, value in enumerate(chunk)}
        placeholders = ", ".join(f":sid{i}" for i in range(len(chunk)))
        lock_sql = f"SELECT id FROM sessions WHERE id IN ({placeholders})"  # noqa: S608
        if dialect == "postgresql":
            # Match Python's key ordering across batches even when the
            # database's default locale orders text differently.
            lock_sql += ' ORDER BY id COLLATE "C" FOR UPDATE'
        else:
            lock_sql += " ORDER BY id"
        locked = (await conn.execute(text(lock_sql), parameters)).scalars().all()
        if not locked:
            continue
        # A previously absent key may be created after the lock query on
        # PostgreSQL. It was not locked, so it cannot become this caller's
        # deletion candidate merely by appearing in the second statement.
        parameters = {f"sid{i}": value for i, value in enumerate(locked)}
        placeholders = ", ".join(f":sid{i}" for i in range(len(locked)))
        rows = (
            await conn.execute(
                text(
                    f"SELECT s.id FROM sessions s WHERE s.id IN ({placeholders}) "  # noqa: S608
                    "AND NOT EXISTS (SELECT 1 FROM cli_worker_spawns w "
                    "WHERE w.session_id = s.id AND w.confirmed_empty_at IS NULL) "
                    "ORDER BY s.id"
                ),
                parameters,
            )
        ).all()
        eligible.extend(row[0] for row in rows)
    return sorted(eligible)


class _StoreRows(NamedTuple):
    """What a writable native open admits a store by (ADR-0128 D7)."""

    # The store's ``lineage`` and ``version`` rows; None when it has no ``schema_meta`` table with
    # ``key`` and ``value`` columns.
    meta: dict[str, Any] | None
    # Every table in a SQLite store; None for a server database, which holds other applications'
    # tables beside lionagi's.
    tables: frozenset[str] | None

    @property
    def lineage(self) -> Any:
        return None if self.meta is None else self.meta.get(_LINEAGE_KEY)


def _sqlite_store_rows(
    execute: Callable[[str], Sequence[Any]], *, tables: bool = True
) -> _StoreRows:
    """A SQLite store's ``_StoreRows``, read through *execute*, which runs one statement and returns
    its rows. ``tables`` false reads the ``schema_meta`` rows alone, without the store's table
    names, for a connection the open makes after it has applied the schema. The async reader
    always reads both."""
    if not tables:
        try:
            return _StoreRows(dict(execute(_SCHEMA_META_GUARD_ROWS)), None)
        except sqlite3.OperationalError:
            return _StoreRows(None, None)
    names = frozenset(row[0] for row in execute(_SQLITE_TABLE_NAMES))
    if "schema_meta" not in names:
        return _StoreRows(None, names)
    if not {"key", "value"} <= {row[0] for row in execute(_SQLITE_SCHEMA_META_COLUMNS)}:
        return _StoreRows(None, names)
    return _StoreRows(dict(execute(_SCHEMA_META_GUARD_ROWS)), names)


async def _sqlite_store_rows_async(driver) -> _StoreRows:
    async def execute(statement: str) -> Sequence[Any]:
        return await (await driver.execute(statement)).fetchall()

    tables = frozenset(row[0] for row in await execute(_SQLITE_TABLE_NAMES))
    if "schema_meta" not in tables:
        return _StoreRows(None, tables)
    if not {"key", "value"} <= {row[0] for row in await execute(_SQLITE_SCHEMA_META_COLUMNS)}:
        return _StoreRows(None, tables)
    return _StoreRows(dict(await execute(_SCHEMA_META_GUARD_ROWS)), tables)


def _some_names(names: Sequence[str]) -> str:
    """The first three of *names*, each cut to 40 characters, for a refusal message."""
    shown = ", ".join(name[:40] for name in names[:3])
    return f"{shown}, ..." if len(names) > 3 else shown


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


def _cancellation_wins(cm_func):
    """Keep a cancelled body cancelled when ending the connection then fails.

    A second cancel can interrupt SQLAlchemy while it discards a connection the
    first cancel broke, before the connection is marked invalid. The rollback or
    close that ends the block then reaches a driver that has already stopped and
    raises, and that error would replace the cancellation: the caller would see a
    failed read or write rather than a cancelled one.
    """

    @functools.wraps(cm_func)
    @asynccontextmanager
    async def wrapper(*args, **kwargs):
        cancelled_exc = get_cancelled_exc_class()
        cancel: BaseException | None = None
        try:
            async with cm_func(*args, **kwargs) as value:
                try:
                    yield value
                except cancelled_exc as exc:
                    cancel = exc
                    raise
        except Exception:
            if cancel is None:
                raise
            raise cancel  # noqa: B904 -- the cleanup error did not cause the cancel; it stays as __context__

    return wrapper


_log = logging.getLogger(__name__)

# The rows a compatibility-mode transaction inserted, recorded as its run's once it commits.
_COMPAT_INSERTED: ContextVar[list[tuple[str, Any]] | None] = ContextVar(
    "lionagi_state_compat_inserted", default=None
)

# The handle a context entry is opening: ``__aenter__`` marks this with the handle, and ``open()``
# reads and clears it before it waits on the open lock. It tells this call whether the failure
# cleanup is its own, since ``__aexit__`` never runs when an entry fails (StateDB.open,
# StateDB.__aenter__). A context variable rather than an attribute, so a second entry waiting behind
# another open keeps its own; there is one variable for the whole task, not one per handle, but it
# is consumed before the first await, so an open made from a task this open starts, or one nested
# inside a schema, never reads another call's mark.
_OPEN_CALL: ContextVar[Any] = ContextVar("lionagi_state_open_call", default=None)


class StateDB:
    """Async SQLAlchemy state layer for sessions, branches, messages, and progressions."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        url: str | None = None,
        readonly: bool = False,
        compatibility: CompatibilityProfile | None = None,
        owner: str | None = None,
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
        # Compatibility mode (ADR-0128) writes into a store another application manages, within
        # the profile it is given: it never migrates or stamps the store, and refuses what the
        # profile does not name.
        self.compatibility = compatibility
        self._compat: _CompatibilityGuard | None = None
        # The rows this handle's compatibility-mode operations created, by table: the first
        # profile writes only rows its run creates (ADR-0128 D4). A handle shared by several
        # runs of one owner counts their rows together.
        self._compat_created: dict[str, set[Any]] = {}
        # The (device, inode) of the file a compatibility handle holds open, so a native open in
        # this process is refused for that file however its URL spells it.
        self._held_file: tuple[int, int] | None = None
        # The (device, inode) of the file a writable native handle has open, so compatibility
        # admission in this process is refused while that handle's connections can still write.
        self._native_file: tuple[int, int] | None = None
        # Whether a connection of this handle's writable engine has read the store (ADR-0128 D7).
        self._connection_read = False
        # Whether this handle's current engine has finished opening, schema included. A plain
        # open that fails after its connection read the store keeps its engine but never reaches
        # its schema, so a later call that finds that engine completes the open instead of
        # returning over a store whose schema never ran.
        self._schema_applied = False
        # Whether this handle's current engine has committed its schema transaction. A plain open
        # that fails after that commit, in the session end-time backfill, keeps its engine with
        # ``_schema_applied`` unset, so a later connection of it reads the store and refuses only
        # a ``lineage`` row naming another application (ADR-0128 D7).
        self._schema_committed = False
        # Whether a close() of this handle is in progress. Set on entry to _close(), before the
        # disposal, and cleared only once the engine is gone: a close a second native cancel
        # interrupts leaves the engine set and this flag set, so the next open or close finishes
        # that disposal rather than opening over it.
        self._closing = False
        # One open at a time: a native open reads the store in a worker thread before its engine
        # exists, so two opens running together would each build an engine and lose one.
        self._open_lock: Lock = Lock()
        # The event loop whose open() or close() holds or waits on _open_lock, and how many do.
        # The lock wakes a waiter only on the loop that holds it, so a call from another loop is
        # refused rather than left waiting.
        self._open_lock_guard = threading.Lock()
        self._open_lock_loop: asyncio.AbstractEventLoop | None = None
        self._open_lock_users = 0
        if compatibility is None and owner is not None:
            raise ValueError("an owner is written in compatibility mode only")
        if compatibility is not None:
            if readonly:
                raise ValueError("readonly and compatibility are separate open modes; pick one")
            self._compat = _CompatibilityGuard(compatibility, owner)
            if self.dialect != "sqlite":
                raise self._compat.refusal(
                    f"compatibility mode relies on SQLite's authorizer; {self.dialect} is refused",
                    operation="open",
                )
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
        # In compatibility mode the row is this run's, so the update is restricted to its owner.
        clause, owner = self._owner_predicate(policy.table)
        await conn.execute(
            text(
                f"UPDATE {policy.table} SET status_reason_code = :reason_code, "  # noqa: S608
                "status_reason_summary = :reason_summary, "
                f"status_evidence_refs = :evidence_refs WHERE id = :entity_id{clause}"
            ).bindparams(bindparam("evidence_refs", type_=JSON)),
            {
                "reason_code": reason_code,
                "reason_summary": "",
                "evidence_refs": [],
                "entity_id": entity_id,
                **owner,
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

    @property
    def mode(self) -> str:
        """``"native"``, ``"readonly"`` or ``"compatibility"``."""
        if self.readonly:
            return "readonly"
        return "native" if self._compat is None else "compatibility"

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
        """Open this handle, one call at a time (see ``_holding_open_lock``).

        A context entry marks ``_OPEN_CALL`` with this handle just before it calls here, and this
        call reads and clears the mark before it waits: an entry whose ``__aenter__`` fails runs no
        ``__aexit__``, and only the call that got past the open lock has an engine to dispose.
        Running that cleanup under the lock means a queued open cannot build an engine that the
        cleanup then disposes. A plain call keeps a later failure's engine, as it always has, and
        the next call that finds that engine completes the open, its schema included.
        """
        context_entry = _OPEN_CALL.get() is self
        try:
            _OPEN_CALL.set(None)
            async with self._holding_open_lock():
                try:
                    await self._open()
                except BaseException:
                    if context_entry:
                        # The shield keeps a cancellation from reaching the disposal, but a
                        # second native cancel delivered while the disposal is in progress still
                        # escapes it and leaves ``_engine`` set; the disposal is entered again by
                        # the next open or close.
                        with CancelScope(shield=True):
                            await self._close()
                    raise
        finally:
            _OPEN_CALL.set(None)

    @asynccontextmanager
    async def _holding_open_lock(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # another async library, where the engine cannot run anyway
            loop = None
        with self._open_lock_guard:
            if self._open_lock_users and self._open_lock_loop is not loop:
                raise RuntimeError(
                    "this StateDB is being opened or closed on another event loop; call open() "
                    "and close() on that loop"
                )
            self._open_lock_loop = loop
            self._open_lock_users += 1
        try:
            async with self._open_lock:
                yield
        finally:
            with self._open_lock_guard:
                self._open_lock_users -= 1
                if not self._open_lock_users:
                    self._open_lock_loop = None

    async def _open(self) -> None:
        if self._closing:
            # A close was interrupted after it had discarded this handle but before it disposed
            # the engine; finish it here, so this open builds over a disposed engine rather than
            # one a live disposal still holds.
            await self._close()
        if self._engine is not None:
            if self._schema_applied:
                return
            # An engine kept by an open that failed after its connection read the store: its
            # schema never ran, so this call completes the open rather than reporting success
            # over a store with no schema. A store this process holds in compatibility mode, or
            # one a native migration must not touch, refuses here as the fresh path does, and that
            # refusal disposes the kept engine rather than leaving it open and registered.
            # This call runs the schema transaction again, so until that transaction commits its
            # connections read the store as an open's do, whatever an earlier attempt on this
            # engine committed before it failed.
            self._schema_committed = False
            try:
                if self._compat is None:
                    held = _open_in_compatibility_mode(self.url)
                    if held is not None:
                        raise held._compat.refusal(
                            "this process holds the store in compatibility mode, and a native "
                            "open would apply this package's schema to it",
                            "open",
                        )
                    if self.dialect == "sqlite":
                        # Read before any connection of the kept engine runs a step of the open:
                        # a store another writer moved to a later version or another table is
                        # refused here, as a fresh open refuses it, rather than inside the schema
                        # transaction, whose disposal would fold the store's log into its file.
                        await run_sync(self._read_lineage_without_writing, _held_files())
                await self._apply_schema()
            except _NATIVE_OPEN_REFUSALS:
                with CancelScope(shield=True):
                    await self._close()
                raise
            self._schema_applied = True
            return
        if self._compat is not None:
            await self._open_compatibility()
            self._schema_applied = True
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
            self._schema_applied = True
            return
        held = _open_in_compatibility_mode(self.url)
        if held is not None:
            # A native open applies this package's schema, and this process holds the store for
            # another application that manages it (ADR-0128 D1).
            raise held._compat.refusal(
                "this process holds the store in compatibility mode, and a native open would "
                "apply this package's schema to it",
                "open",
            )
        if self.dialect == "sqlite":
            # The file and its lineage row are read before any writable connection exists, so a
            # refusal from those reads changes no file of the store, and each new writable
            # connection reads them again before its settings change the file, for a row written
            # in between (ADR-0128 D7).
            await run_sync(self._read_lineage_without_writing, _held_files())
            self._connection_read = False
            self._native_file = None
            self._schema_committed = False
            self._engine = make_engine(self.url, before_settings=self._refuse_a_managed_store)
            _install_begin_immediate(self._engine.sync_engine)
        else:
            self._schema_committed = False
            self._engine = make_engine(self.url)
        try:
            await self._apply_schema()
            self._schema_applied = True
        except BaseException as exc:
            # A refused open, or one whose first connection never read the store (a lock held past
            # the busy timeout, a file that is not a database), leaves no engine, so a caller that
            # keeps the object can open it again. After any other failure the engine stays, as
            # before.
            unread = self.dialect == "sqlite" and not self._connection_read
            if unread or isinstance(exc, _NATIVE_OPEN_REFUSALS):
                with CancelScope(shield=True):
                    await self._close()
            raise

    def _refuse_a_managed_store(self, dbapi_conn) -> None:
        """Refuse a writable native connection to a file this process holds in compatibility
        mode, or, while this handle's open is in progress, to a store a native migration must not
        touch (``_refuse_unless_native``), before its settings change the file. Once the open has
        applied the schema, a later connection refuses only a ``lineage`` row naming another
        application: a version check there would raise ``SchemaTooNewError`` from an ordinary
        write, and only an open raises it, and it reads only the ``schema_meta`` rows there. Only
        reads: a hot rollback journal is rolled back by the first read, as on any writable SQLite
        open."""
        cursor = dbapi_conn.cursor()

        def execute(statement: str) -> Sequence[Any]:
            cursor.execute(statement)
            return cursor.fetchall()

        try:
            opened = next(
                (row[2] for row in execute("PRAGMA database_list") if row[1] == "main"), ""
            )
            self._refuse_a_held_file(opened, _held_files())
            # A connection this open makes after its schema transaction has committed reads only
            # the ``schema_meta`` rows: the tables were read before the schema, and reading them
            # again would refuse a table another writer adds later from an ordinary call.
            store = _sqlite_store_rows(execute, tables=not self._schema_committed)
        finally:
            cursor.close()
        if self._schema_applied or self._schema_committed:
            self._raise_if_foreign_lineage(store.lineage)
        else:
            self._refuse_unless_native(store)
        self._connection_read = True
        # The file the connection opened, however the URL spells it, so compatibility admission
        # in this process is refused for that file while this handle is open. Registered as soon
        # as a connection has read the store: an open that fails after this keeps its engine.
        self._native_file = _file_identity(opened) if opened else None
        if self._native_file is not None:
            _OPEN_NATIVE.add(self)

    def _read_lineage_without_writing(self, held: tuple) -> None:
        """Refuse a file this process holds in compatibility mode, or a store a native migration
        must not touch (``_refuse_unless_native``), or one whose journal or log cannot be read
        (``UnreadableStoreError``), before any writable connection opens it (ADR-0128 D7).

        No file of the store is written. The file is read immutable first: SQLite takes no lock and
        reads no journal, log or index. Beside a rollback journal that begins with a journal header,
        as one an interrupted transaction leaves does, the file can hold part of that transaction, so
        the rows are read instead from a private copy of the file and the journal, which SQLite rolls
        back. When a write-ahead log with frames is beside it, whatever the file's own rows say,
        the rows are read again read-only with ``readonly_shm``, which reads the log through its
        ``-shm`` index without writing to it, and, when there is no index or that read fails, from
        a private copy of the file and the log. A copy the store committed or checkpointed under is
        taken again from the start, a bounded number of times. A store beside a journal or a log
        whose rows could not be read has rows this code cannot know, the file's own ``lineage`` row
        included, since that row can be part of an unfinished transaction or stale beside a
        committed log, so such a store is refused by ``UnreadableStoreError`` before any writable
        connection opens it. A file that cannot be read at all is left to the writable connection,
        which reads the rows again before its settings and changes no file if it refuses then.
        """
        from sqlalchemy.dialects.sqlite.aiosqlite import SQLiteDialect_aiosqlite

        try:
            (filename,), options = SQLiteDialect_aiosqlite().create_connect_args(make_url(self.url))
        except Exception:  # noqa: BLE001 -- make_engine reports a URL it cannot open
            return
        uri = bool(options.get("uri"))
        target = _read_only_uri(filename, uri, immutable=True)
        if target is None:
            return
        sidecar = False
        for _ in range(_COPY_ATTEMPTS):
            read = _read_lineage(target)
            if read is None:
                return
            opened, in_file = read
            self._refuse_a_held_file(opened, held)
            if not opened:
                return
            if _journal_has_a_header(opened):
                sidecar = True
                current = _read_lineage_from_a_copy(opened, "-journal")
            elif _log_has_frames(opened):
                # The log is read whatever the file shows: a row it holds is the store's row.
                sidecar = True
                reread = _read_lineage(_read_only_uri(filename, uri, immutable=False))
                current = _UNREAD if reread is None else reread[1]
                if current is _UNREAD:
                    current = _read_lineage_from_a_copy(opened, "-wal")
            else:
                # No journal and no log: the file holds the whole store.
                sidecar = False
                current = in_file
            if current is not _MOVED:
                break
        else:
            # Every copy saw the store commit or checkpoint under it: no read of the file and its
            # journal or log is of one state.
            current = _UNREAD
        if current is not _UNREAD:
            self._refuse_unless_native(current)
        elif sidecar:
            raise UnreadableStoreError(
                f"{self._store_named_for_an_operator()} has a rollback journal or write-ahead log "
                "that could not be read, so the rows they hold, a lineage row naming the "
                "application that manages the store and a schema version among them, cannot be "
                "checked. Opening it for writing would roll back or fold that journal or log. "
                "Close every process using the store and open it again, or open it read-only to "
                "inspect it."
            )

    @staticmethod
    def _refuse_a_held_file(filename: str, held: tuple) -> None:
        identity = _file_identity(filename) if filename else None
        if identity is None:
            return
        for file, handle in held:
            if file == identity:
                raise handle._compat.refusal(
                    "this process holds the store in compatibility mode, and a native open would "
                    "apply this package's schema to it",
                    "open",
                )

    async def _open_compatibility(self) -> None:
        # Never _apply_schema(), create_all, a rebuild, a backfill or a version stamp: the store's
        # schema belongs to its managing application. A failed admission disposes the engine, so
        # the object stays unopened rather than half-open as a writer.
        guard = self._compat
        p = self.path
        if p is None or str(p) == ":memory:":
            raise guard.refusal("compatibility mode opens an existing store file", "open")
        # SQLAlchemy opens the absolute path of a SQLite URL's database, resolved lexically, so
        # that is the path checked: through a symlink, `..` names another file.
        lexical = os.path.abspath(p)
        if not os.path.exists(lexical):
            raise guard.refusal(
                f"no store at {mask_credentials(str(p))}; compatibility mode never creates one",
                "open",
            )
        identity = _file_identity(lexical)
        for native in list(_OPEN_NATIVE):
            if native._engine is not None and native._native_file == identity:
                # Its connections write without the profile, and those already open would never
                # read the guard (ADR-0128 D7).
                raise guard.refusal(
                    "this process holds the store open natively, and that handle's connections "
                    "write without the profile",
                    "open",
                )
        self._engine = make_compatibility_engine(self.url, guard)
        try:
            async with self.read_snapshot() as conn:
                await conn.run_sync(guard.admit)
        except BaseException:
            with CancelScope(shield=True):
                await self._close()
            raise
        self._held_file = _file_identity(lexical)
        _OPEN_COMPAT.add(self)

    def _refuse_in_compatibility(self, operation: str, reason: str) -> None:
        if self._compat is not None:
            raise self._compat.refusal(reason, operation)

    # Compatibility-mode rows (ADR-0128 D4, D5). The first profile writes only rows its run
    # creates: a row an operation inserts must not exist yet, carries the configured owner, and is
    # recorded as this run's once its transaction commits; a row an operation links to must be one
    # of those.

    def _owner_values(self, table: str) -> dict[str, str]:
        """The owner column and value an insert into *table* writes; empty when it has none."""
        guard = self._compat
        if guard is None or table not in guard.owned_tables:
            return {}
        return {guard.profile.owner.column: guard.owner}

    def _owner_predicate(self, table: str) -> tuple[str, dict[str, str]]:
        """A conjunct restricting a WHERE clause on *table* to the owner's rows, and its parameter."""
        owned = self._owner_values(table)
        if not owned:
            return "", {}
        ((column, owner),) = owned.items()
        return f" AND {_quote_identifier(column)} = :_compat_owner", {"_compat_owner": owner}

    def _require_created(self, table: str, row_id: Any, what: str) -> None:
        if not isinstance(row_id, str):
            # A set lookup is by Python equality, under which 1 and 1.0 are one id.
            raise self._compat.refusal(
                f"{what} {row_id!r} is not a string id; compatibility mode links rows by "
                "their string ids"
            )
        if row_id not in self._compat_created.get(table, ()):
            raise self._compat.refusal(
                f"{what} {row_id!r} was not created by this run; compatibility mode writes only "
                "rows its run creates"
            )

    async def _require_owned(self, conn, table: str, row_id: Any, what: str) -> None:
        """Refuse, inside the writing transaction, when the row no longer carries the owner.

        A table that records no owner column cannot tell one owner's row from another's, and a
        profile that records owners but not this column cannot show the row is the run's, so the
        link is refused rather than checked against an owner the table does not carry -- exactly
        as ``create_session`` refuses an invocation link there.
        """
        guard = self._compat
        clause, owner = self._owner_predicate(table)
        if not clause:
            if (
                guard is not None
                and guard.profile.owner is not None
                and table not in guard.owned_tables
            ):
                raise self._compat.refusal(
                    f"{what} {row_id!r} cannot be shown to belong to this run: the linked table "
                    f"{table} records no owner column"
                )
            return
        found = (
            await conn.execute(
                text(f"SELECT 1 FROM {_quote_identifier(table)} WHERE id = :_id{clause}"),  # noqa: S608
                {"_id": row_id, **owner},
            )
        ).first()
        if found is None:
            raise self._compat.refusal(
                f"{what} {row_id!r} does not belong to {self._compat.owner!r}; an update never "
                "reaches another owner's row"
            )

    async def _insert_new_row(
        self, conn, table: str, values: dict[str, Any], json_columns: tuple[str, ...] = ()
    ) -> None:
        found = (
            await conn.execute(
                text(f"SELECT 1 FROM {table} WHERE id = :id"),  # noqa: S608 -- table is a literal
                {"id": values["id"]},
            )
        ).first()
        if found is not None:
            raise self._compat.refusal(
                f"{table} row {values['id']!r} already exists; compatibility mode writes only "
                "rows its run creates"
            )
        row = {**values, **self._owner_values(table)}
        names = list(row)
        stmt = text(
            f"INSERT INTO {table} ({', '.join(_quote_identifier(n) for n in names)}) "  # noqa: S608
            f"VALUES ({', '.join(f':c{i}' for i in range(len(names)))})"
        )
        json_params = [
            bindparam(f"c{i}", type_=JSON) for i, name in enumerate(names) if name in json_columns
        ]
        if json_params:
            stmt = stmt.bindparams(*json_params)
        await conn.execute(stmt, {f"c{i}": row[name] for i, name in enumerate(names)})
        _COMPAT_INSERTED.get().append((table, values["id"]))

    async def close(self) -> None:
        # An open in progress on this loop finishes first, so close() never returns before an
        # engine it would have disposed exists; one in progress on another loop refuses it.
        async with self._holding_open_lock():
            await self._close()

    async def _close(self) -> None:
        # Marked before anything is discarded, and cleared only once the engine is gone: a second
        # native cancel delivered while the shielded disposal below is in progress escapes it and
        # leaves the engine set, and this flag is how the next open or close knows to enter the
        # disposal again rather than open over the engine it left.
        self._closing = True
        _OPEN_COMPAT.discard(self)
        _OPEN_NATIVE.discard(self)
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        # Cleared only once the engine is gone, so a close interrupted before its disposal leaves
        # both set and the next call enters the disposal again rather than over the engine it left.
        self._schema_applied = False
        self._schema_committed = False
        self._connection_read = False
        self._closing = False

    async def __aenter__(self) -> StateDB:
        # __aexit__ does not run when __aenter__ fails, so the entry marks its own open call:
        # open() disposes what its own call opened under the lock it holds, rather than letting the
        # driver worker outlive the failure or disposing an engine a queued open built.
        _OPEN_CALL.set(self)
        await self.open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # Internal connection helpers

    @_cancellation_wins
    @asynccontextmanager
    async def _read(self):
        async with self._engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            yield conn

    @_cancellation_wins
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

    @_cancellation_wins
    @asynccontextmanager
    async def _tx(self):
        # Compatibility mode refuses here, before the write lock and before any statement, when
        # no operation the profile names is running.
        scope = None if self._compat is None else self._compat.require_scope("transaction")
        inserted: list[tuple[str, Any]] = []
        token = None if scope is None else _COMPAT_INSERTED.set(inserted)
        try:
            if self.dialect == "sqlite":
                async with self._write_lock:
                    async with self._engine.begin() as conn:
                        yield conn
            else:
                async with self._engine.begin() as conn:
                    yield conn
        finally:
            if token is not None:
                _COMPAT_INSERTED.reset(token)
        if scope is not None:
            scope.committed.append(f"{scope.name} transaction {len(scope.committed) + 1}")
            for table, row_id in inserted:
                self._compat_created.setdefault(table, set()).add(row_id)

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

    def _refuse_unless_read(self, operation: str, sql: str) -> None:
        if self._compat is not None and _statement_kind(_sql_tokens(sql)) != "read":
            raise self._compat.refusal(
                "compatibility mode serves the profile's operations only, and this statement is "
                "not a read",
                operation,
            )

    async def fetch_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self._refuse_unless_read("fetch_all", sql)
        sql, p = self._to_named(sql, params)
        async with self._read() as conn:
            result = await conn.execute(text(sql), p)
            return [dict(r) for r in result.mappings().all()]

    async def fetch_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        self._refuse_unless_read("fetch_one", sql)
        sql, p = self._to_named(sql, params)
        async with self._read() as conn:
            result = await conn.execute(text(sql), p)
            row = result.mappings().first()
            return dict(row) if row is not None else None

    async def execute(self, sql: str, params: Any = None) -> None:
        self._refuse_in_compatibility(
            "execute",
            "compatibility mode serves the profile's operations only; read with fetch_all() "
            "or fetch_one()",
        )
        sql, p = self._to_named(sql, params)
        async with self._tx() as conn:
            await conn.execute(text(sql), p)

    def transaction(self):
        self._refuse_in_compatibility(
            "transaction",
            "compatibility mode serves the profile's operations only; read with read_snapshot()",
        )
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
        self._refuse_in_compatibility("vacuum", "maintenance belongs to the managing application")
        if self.dialect == "sqlite":
            await self._raw_sqlite_exec("VACUUM")
        else:
            async with self._read() as conn:
                await conn.execute(text("VACUUM"))

    async def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int] | None:
        # WAL checkpoint is sqlite-only maintenance and, like VACUUM, must bypass the adapter's
        # implicit transaction. Returns (busy, log_pages, checkpointed), or None off sqlite.
        self._refuse_in_compatibility(
            "checkpoint", "manual checkpoints belong to the managing application"
        )
        if self.dialect != "sqlite":
            return None
        mode = mode.upper()
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError(f"invalid wal_checkpoint mode: {mode!r}")
        row = await self._raw_sqlite_exec(f"PRAGMA wal_checkpoint({mode})", fetch=True)
        return tuple(row) if row is not None else None

    # Schema management

    def _store_named_for_an_operator(self) -> str:
        # Masked: on a server-backed store this is the connection URL, and the refusals that name
        # it fire on routine mistakes (upgrade order, a path pointed at another application's store).
        if self.dialect == "sqlite" and self.path is not None:
            return mask_credentials(str(self.path))
        return mask_db_url(self.url)

    def _raise_if_foreign_lineage(self, lineage: Any) -> None:
        if not _names_another_application(lineage):
            return
        raise ForeignLineageError(
            f"{self._store_named_for_an_operator()} names {str(lineage)[:80]!r} as the application "
            "that manages its schema, and opening it for writing would apply this package's "
            "schema to it. Open it read-only to inspect it, or in compatibility mode with a "
            "profile for that application."
        )

    def _refuse_unless_native(self, store: _StoreRows) -> None:
        """Refuse a store a native migration must not touch (ADR-0128 D7).

        A SQLite store with no ``schema_meta`` table is this package's when every table in it is
        one a lionagi component creates (none at all: a new store), since the Studio Operator store
        and Studio's approval ledger can create their tables before any StateDB open writes
        ``schema_meta``; on a server database, which holds other applications' tables, such a
        database is new. A ``schema_meta`` table without ``key`` and ``value`` columns is not one
        lionagi created. A store whose ``lineage`` names lionagi is this package's: only a version
        above ``SCHEMA_VERSION`` refuses it. A store with no ``lineage`` row predates the row, and
        every store lionagi created before it holds ``schema_meta`` with a version from 1 to
        ``SCHEMA_VERSION`` and the ``sessions`` table, created together on its first open; only that
        store is adopted, and ``_apply_schema`` stamps the row into it. Missing, malformed and
        higher versions are each refused by name rather than by a failed comparison. Tables are
        recognized by name, not by their columns.
        """
        where = self._store_named_for_an_operator()
        meta, tables = store
        if meta is None:
            if tables is None:
                return
            if "schema_meta" in tables:
                raise UnrecognizedStoreError(
                    f"{where} has a schema_meta table without the key and value columns of the "
                    "one lionagi creates, so lionagi did not create it, and opening it for writing "
                    "would migrate it. Open it read-only to inspect it."
                )
            foreign = sorted(tables - _LIONAGI_TABLES)
            if not foreign:
                return
            raise UnrecognizedStoreError(
                f"{where} has no schema_meta table and holds tables lionagi never creates "
                f"({_some_names(foreign)}), so lionagi did not create it, and opening it for "
                "writing would add this package's tables to it. Point lionagi at a new store, or "
                "open this one read-only to inspect it."
            )
        lineage = meta.get(_LINEAGE_KEY)
        self._raise_if_foreign_lineage(lineage)
        recorded = meta.get("version")
        if lineage == _NATIVE_LINEAGE:
            self._raise_if_schema_too_new(recorded)
            return
        if recorded is None:
            raise UnrecognizedStoreError(
                f"{where} has a schema_meta table with no lineage or version row, which every "
                "store lionagi creates has, so this code cannot tell what shape it would "
                "migrate. Open it read-only to inspect it."
            )
        if not isinstance(recorded, str) or not _RECORDED_VERSION.fullmatch(recorded):
            raise UnrecognizedStoreError(
                f"{where} records schema version {str(recorded)[:40]!r} and no lineage row; no "
                "lionagi release writes that version, so this code cannot tell what shape it "
                "would migrate. Open it read-only to inspect it."
            )
        self._raise_if_schema_too_new(recorded)
        if tables is None or "sessions" in tables:
            return
        foreign = sorted(tables - _LIONAGI_TABLES)
        if foreign:
            raise UnrecognizedStoreError(
                f"{where} records schema version {recorded} and no lineage row, and holds tables "
                f"lionagi never creates ({_some_names(foreign)}) but no sessions table, which "
                "every lionagi release creates with the version row, so lionagi did not create "
                "it. Open it read-only to inspect it."
            )

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
        where = self._store_named_for_an_operator()
        raise SchemaTooNewError(
            f"{where} records schema version {recorded} but this version of lionagi "
            f"applies schema version {SCHEMA_VERSION}. It was written by a later "
            "release whose shape this code cannot verify, and opening it for writing "
            "would migrate it against that shape and record the lower version. "
            "Upgrade lionagi to open it, or open it read-only to inspect it."
        )

    async def _refuse_newer_schema(self, conn) -> None:
        """Refuse to open a database a native migration must not touch (``_refuse_unless_native``),
        reading its rows under the write lock and the lineage first."""
        if self.dialect == "sqlite":
            # A SQLite file is a store of its own, so every table in it is someone's.
            store = await conn.run_sync(
                lambda c: _sqlite_store_rows(lambda statement: c.exec_driver_sql(statement).all())
            )
            self._refuse_unless_native(store)
            return
        if self.dialect == "postgresql":
            # The version table may not exist yet, so a row lock cannot serialize first-time schema
            # creation; this database-local lock predates any schema object.
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": _SCHEMA_MIGRATION_LOCK_KEY},
            )
        if not await conn.run_sync(lambda c: inspect(c).has_table("schema_meta")):
            # A server database may hold other applications' tables beside the ones lionagi
            # creates there, so one without schema_meta is new.
            return
        query = _SCHEMA_META_GUARD_ROWS
        if self.dialect == "postgresql":
            query += " FOR UPDATE"
        meta = dict((await conn.execute(text(query))).all())
        self._refuse_unless_native(_StoreRows(meta, None))

    async def _refuse_newer_sqlite_schema(self, driver) -> None:
        """Refuse a store a native migration must not touch (``_refuse_unless_native``), reading
        its rows through a raw driver holding BEGIN IMMEDIATE."""
        self._refuse_unless_native(await _sqlite_store_rows_async(driver))

    async def _apply_schema(self) -> None:
        self._refuse_in_compatibility("migration", "the store's schema belongs to its manager")
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
            await self._classify_terminal_delivery_history_once(conn)
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
            # ADR-0128 D7: a store this transaction created, or one _refuse_newer_schema
            # recognized as lionagi's, carries the native lineage; a row naming another
            # application was refused above, so this never overwrites one.
            await conn.execute(
                text(
                    "INSERT INTO schema_meta (key, value) VALUES (:key, :lineage) "
                    "ON CONFLICT (key) DO NOTHING"
                ),
                {"key": _LINEAGE_KEY, "lineage": _NATIVE_LINEAGE},
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
        # The schema transaction has committed, and with it the native lineage row, so a
        # connection this handle opens from here on refuses only a lineage row naming another
        # application, never a version another writer records afterwards (ADR-0128 D7).
        self._schema_committed = True

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
            except _NATIVE_OPEN_REFUSALS:
                # A new connection's own refusal (ADR-0128 D7), not a failed inspection.
                raise
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

    async def _classify_terminal_delivery_history_once(self, conn) -> None:
        """Freeze v5 replay eligibility, the unknown count and marker in one transaction."""
        columns = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("status_transitions")}
        )
        if "delivery_eligible" not in columns:
            await conn.execute(
                text("ALTER TABLE status_transitions ADD COLUMN delivery_eligible BOOLEAN")
            )
        marker = await conn.execute(
            text("SELECT 1 FROM schema_meta WHERE key = :key"),
            {"key": _TERMINAL_ELIGIBILITY_BACKFILL_KEY},
        )
        if marker.first() is not None:
            return
        if self.dialect == "postgresql":
            # Block old writers until the classification and baseline count both commit.
            await conn.execute(text("LOCK TABLE status_transitions IN SHARE ROW EXCLUSIVE MODE"))

        # This is the v5 replay predicate, deliberately frozen here rather than imported
        # from the live registry. A NULL predecessor on a terminal execution row is
        # ambiguous between initial history and a reaper transition.
        eligible_kind_status = (
            "((entity_type IN ('session', 'invocation') AND status IN "
            "('completed', 'completed_empty', 'failed', 'timed_out', 'aborted', 'cancelled')) "
            "OR (entity_type = 'play' AND status IN "
            "('merged', 'escalated', 'gate_failed', 'blocked', 'aborted_after_finish')) "
            "OR (entity_type = 'schedule_run' AND status IN "
            "('completed', 'failed', 'timed_out', 'skipped', 'cancelled')))"
        )
        await conn.execute(
            text(
                "UPDATE status_transitions SET delivery_eligible = CASE "  # noqa: S608
                f"WHEN NOT {eligible_kind_status} THEN FALSE "
                "WHEN previous_status IS NULL THEN NULL "
                "WHEN previous_status = status THEN FALSE ELSE TRUE END"
            )
        )
        count = (
            await conn.execute(
                text("SELECT COUNT(*) FROM status_transitions WHERE delivery_eligible IS NULL")
            )
        ).scalar_one()
        await conn.execute(
            text("INSERT INTO schema_meta (key, value) VALUES (:key, :value)"),
            {
                "key": _TERMINAL_ELIGIBILITY_BACKFILL_KEY,
                "value": json.dumps({"classified_at": time.time(), "initial_null_count": count}),
            },
        )

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
                              cli_worker_generation TEXT,
                              created_at      REAL    NOT NULL,
                              node_metadata   JSON,
                              name            TEXT,
                              user_label      TEXT,
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
        # The checkpoint below writes the store, so a store a native migration must not touch is
        # refused first; the rebuild reads the rows again under its own write lock.
        async with self._engine.connect() as conn:
            await self._refuse_newer_schema(conn)
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
    _MESSAGE_READ_CHUNK = 500

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
        values = {
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
        }
        if self._compat is not None:
            await self._write_message_compat(conn, values)
            return

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
            values,
        )

    async def _write_message_compat(self, conn, values: dict[str, Any]) -> None:
        # A message this run wrote is rewritten in place, as a re-emitted hook does natively, and
        # refuses when the store has deleted it or given it to another owner; any other existing
        # id refuses, and a new one is inserted with its owner.
        if values["id"] not in self._compat_created.get("messages", ()):
            await self._insert_new_row(
                conn, "messages", values, json_columns=("node_metadata", "content")
            )
            return
        clause, owner = self._owner_predicate("messages")
        result = await conn.execute(
            text(
                "UPDATE messages SET node_metadata = :node_metadata, content = :content, "  # noqa: S608 -- the clause is this module's
                "embedding = :embedding, sender = :sender, recipient = :recipient, "
                "channel = :channel, role = :role, lion_class = :lion_class "
                f"WHERE id = :id{clause}"
            ).bindparams(
                bindparam("node_metadata", type_=JSON),
                bindparam("content", type_=JSON),
            ),
            {**values, **owner},
        )
        if result.rowcount == 0:
            raise self._compat.refusal(
                f"message {values['id']!r}, which this run wrote, is gone or no longer "
                f"belongs to {self._compat.owner!r}; compatibility mode rewrites only its own "
                "rows"
            )

    @_operation("insert_message")
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

    async def get_messages(self, message_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Read many messages in one query per 500 ids, keyed by id; absent ids are missing.

        A caller that needs N rows by id (e.g. recovering a name from a stored tool use)
        reads them in bounded chunks rather than one ``get_message`` per row, which reopens
        a connection each time. ``_MESSAGE_READ_CHUNK`` keeps each IN list under SQLite's
        bound-parameter ceiling.
        """
        unique = list(dict.fromkeys(message_ids))
        if not unique:
            return {}
        found: dict[str, dict[str, Any]] = {}
        async with self._read() as conn:
            for start in range(0, len(unique), self._MESSAGE_READ_CHUNK):
                chunk = unique[start : start + self._MESSAGE_READ_CHUNK]
                placeholders = ",".join(f":id{i}" for i in range(len(chunk)))
                sql = (
                    f"SELECT m.*, mt.lion_class AS lion_class_str "  # noqa: S608
                    f"FROM messages m LEFT JOIN message_types mt ON m.lion_class = mt.type_id "
                    f"WHERE m.id IN ({placeholders})"
                )
                params = {f"id{i}": mid for i, mid in enumerate(chunk)}
                rows = (await conn.execute(text(sql), params)).mappings().all()
                for row in rows:
                    found[row["id"]] = self._row_to_dict(row)
        return found

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
        # In compatibility mode an owned lookup table gets its owner like any other insert.
        params = {"lc": lion_class_str}
        owner_column = owner_value = ""
        for name, owner in self._owner_values("message_types").items():
            owner_column, owner_value = f", {_quote_identifier(name)}", ", :_compat_owner"
            params["_compat_owner"] = owner
        await conn.execute(
            text(
                f"INSERT INTO message_types (lion_class{owner_column}) VALUES (:lc{owner_value}) "  # noqa: S608 -- the column is this module's
                "ON CONFLICT (lion_class) DO NOTHING"
            ),
            params,
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

    @staticmethod
    def _validate_progression_message_id(progression_id: str, message_id: Any) -> None:
        if not isinstance(message_id, str) or not message_id:
            raise ValueError(
                f"progression {progression_id!r} requires non-empty string message IDs"
            )

    @staticmethod
    def _validate_progression_collection(
        progression_id: str, collection: Any, *, allow_none: bool = False
    ) -> list[str]:
        if collection is None and allow_none:
            return []
        if not isinstance(collection, (list, tuple)):
            raise ValueError(f"progression {progression_id!r} requires a list or tuple of IDs")
        snapshot = list(collection)
        for message_id in snapshot:
            StateDB._validate_progression_message_id(progression_id, message_id)
        return snapshot

    @_operation("create_progression")
    async def create_progression(
        self, progression_id: str, collection: list[str] | None = None
    ) -> None:
        if self._compat is not None:
            # The messages a progression lists are rows this run created.
            for message_id in collection or ():
                self._require_created("messages", message_id, "message")
            values = {
                "id": progression_id,
                "created_at": time.time(),
                "collection": _to_json_column(collection or []),
            }
            async with self._tx() as conn:
                await self._insert_new_row(conn, "progressions", values)
            return
        collection = self._validate_progression_collection(
            progression_id, collection, allow_none=True
        )
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
        if self._compat is None:
            collection = self._validate_progression_collection(progression_id, collection)
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
        if self._compat is None:
            self._validate_progression_message_id(progression_id, message_id)
        async with self._tx() as conn:
            await self._append_to_progression_in_tx(conn, progression_id, message_id)

    async def _append_to_progression_in_tx(
        self, conn, progression_id: str, message_id: str
    ) -> None:
        clause, owner = self._owner_predicate("progressions")
        await conn.execute(
            text(self._progression_append_sql(self.dialect) + clause),
            {"v": message_id, "id": progression_id, **owner},
        )

    @staticmethod
    def _progression_collection_for_update_sql(dialect: str, clause: str = "") -> str:
        # SQLite's BEGIN IMMEDIATE already holds the writer reservation for the whole transaction;
        # elsewhere the row lock keeps a concurrent append from being lost between read and write.
        if dialect == "sqlite":
            return "SELECT collection FROM progressions WHERE id = :id" + clause  # noqa: S608 -- owner predicate
        return "SELECT collection FROM progressions WHERE id = :id" + clause + " FOR UPDATE"  # noqa: S608 -- owner predicate, before the lock clause

    async def insert_messages(
        self,
        msgs: Sequence[dict[str, Any]],
        *,
        progression_ids: Sequence[str] = (),
        session_id: str | None = None,
        activity_at: float | None = None,
    ) -> None:
        """Write a batch of messages and append their ids to progressions in one transaction.

        Each progression is read once. One that holds an array of string ids, given string
        ids, is rewritten at most once for the whole batch, where ``insert_message`` plus
        ``append_to_progression`` per message rewrites each progression's whole array once
        per message. Ids are appended in batch order and
        skipped when already present, so re-running a batch leaves a string-id progression
        unchanged. That single rewrite covers a stored array of string ids given string ids,
        the ordinary string-ID shape; any other stored value or id goes through the
        legacy internal append statement one id at a time. The public
        ``append_to_progression`` validates new IDs; this batch path retains its own
        existing input contract. ``session_id`` moves that session's activity clock in
        the same transaction.
        """
        for msg in msgs:
            self._validate_message(msg)
        if not msgs:
            return
        message_ids = [msg["id"] for msg in msgs]
        async with self._tx() as conn:
            for msg in msgs:
                await self._insert_message_in_tx(conn, msg)
            for progression_id in progression_ids:
                await self._extend_progression_in_tx(conn, progression_id, message_ids)
            if session_id is not None:
                await self._touch_session_activity_in_tx(conn, session_id, at=activity_at)

    async def _extend_progression_in_tx(
        self, conn, progression_id: str, message_ids: Sequence[str]
    ) -> None:
        # The same owner predicate the per-message append applies, on both the read and the
        # rewrite: a progression another owner's run owns is not this run's to extend, so it is
        # read as absent rather than rewritten. Natively the predicate is empty.
        clause, owner = self._owner_predicate("progressions")
        row = (
            (
                await conn.execute(
                    text(self._progression_collection_for_update_sql(self.dialect, clause)),
                    {"id": progression_id, **owner},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return
        current = row["collection"]
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except ValueError:
                current = None
        if (
            not isinstance(current, list)
            or not all(_is_utf8_str(item) for item in current)
            or not all(_is_utf8_str(message_id) for message_id in message_ids)
        ):
            # Any other shape (not an array, a non-string or non-finite element, a string that
            # cannot be encoded as UTF-8 such as a lone surrogate, a non-string id) compares or
            # encodes differently on each dialect, so it takes the per-message statement. The
            # SQLite tests cannot tell the two paths apart for a non-string id.
            for message_id in message_ids:
                await self._append_to_progression_in_tx(conn, progression_id, message_id)
            return
        seen = set(current)
        added: list[str] = []
        for message_id in message_ids:
            if message_id not in seen:
                seen.add(message_id)
                added.append(message_id)
        if not added:
            return
        await conn.execute(
            text("UPDATE progressions SET collection = :col WHERE id = :id" + clause),  # noqa: S608 -- owner predicate
            {"col": _to_json_column([*current, *added]), "id": progression_id, **owner},
        )

    # Sessions

    @_operation("create_session")
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
        generation = _validate_cli_worker_generation(session.get("cli_worker_generation"))
        if generation is not None:
            self._refuse_in_compatibility(
                "create_session", "cli_worker_generation requires a native state store"
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
        params = {
            "id": session["id"],
            "cc_session_id": session.get("cc_session_id"),
            "run_id": session.get("run_id"),
            "cli_worker_generation": generation,
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
        }
        if self._compat is not None:
            params.pop("cli_worker_generation")
            await self._create_session_compat(session, params, now)
            return
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """INSERT INTO sessions (id, cc_session_id, run_id, cli_worker_generation, created_at, node_metadata, name, "user",
                       progression_id, first_msg_id, last_msg_id, updated_at,
                       playbook_name, agent_name, invocation_kind, show_topic,
                       show_play_name, artifacts_path, artifact_contract_json,
                       artifact_verification_json, source_kind,
                       status, started_at, ended_at, ended_at_is_approximate,
                       last_message_at, invocation_id,
                       model, provider, effort, agent_hash,
                       project, project_source, duration_ms)
                       VALUES (:id, :cc_session_id, :run_id, :cli_worker_generation, :created_at, :node_metadata, :name, :user,
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
                params,
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

    async def _create_session_compat(
        self, session: dict[str, Any], params: dict[str, Any], now: float
    ) -> None:
        # The id must not exist; the owner is written on the session and on its first transition.
        # The progression and messages it links are rows this run created, and an invocation it
        # names must already belong to the owner. Project registration updates a row the run did
        # not create, so it is left to the managing application and the project stays on the
        # session row (ADR-0128 Appendix A).
        if params["progression_id"] is not None:
            self._require_created("progressions", params["progression_id"], "progression")
        for link in ("first_msg_id", "last_msg_id"):
            if params[link] is not None:
                self._require_created("messages", params[link], "message")
        invocation_id = params["invocation_id"]
        if invocation_id is not None and "invocations" not in self._compat.owned_tables:
            raise self._compat.refusal(
                f"the session names invocation {invocation_id!r}, which this run did not create, "
                "and the profile records no owner to admit it by"
            )
        clause, owner = self._owner_predicate("invocations")
        async with self._tx() as conn:
            if invocation_id is not None:
                found = (
                    await conn.execute(
                        text(f"SELECT 1 FROM invocations WHERE id = :inv_id{clause}"),  # noqa: S608
                        {"inv_id": invocation_id, **owner},
                    )
                ).first()
                if found is None:
                    raise self._compat.refusal(
                        f"invocation {invocation_id!r} does not belong to {self._compat.owner!r}"
                    )
            await self._insert_new_row(
                conn,
                "sessions",
                params,
                json_columns=(
                    "node_metadata",
                    "artifact_contract_json",
                    "artifact_verification_json",
                ),
            )
            if session.get("status") is not None:
                await self._initialize_managed_entity_in_tx(
                    conn,
                    entity_type="session",
                    entity_id=session["id"],
                    status=session["status"],
                    actor_id="create_session",
                )
            if invocation_id is not None:
                await conn.execute(
                    text(
                        "UPDATE invocations SET session_count = session_count + 1, "  # noqa: S608 -- the clause is this module's
                        f"updated_at = :now WHERE id = :inv_id{clause}"
                    ),
                    {"now": now, "inv_id": invocation_id, **owner},
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

    @_operation("register_cli_worker_spawn")
    async def register_cli_worker_spawn(self, record: Mapping[str, Any]) -> None:
        """Admit an immutable worker record or accept an identical durable replay.

        New records require a running session and its current generation. A
        replay compares producer fields only, in any session state, and writes
        neither the session nor the worker's store-owned timestamps.
        """
        self._refuse_in_compatibility(
            "register_cli_worker_spawn", "worker spawn records require native storage"
        )
        payload = _validate_cli_worker_spawn(record)

        def compare(existing: Mapping[str, Any]) -> None:
            if any(existing[field] != payload[field] for field in _CLI_WORKER_PRODUCER_FIELDS):
                raise ValueError("spawn_id already names different worker contents")

        with fail_after(_CLI_WORKER_OPERATION_TIMEOUT_SECONDS):
            async with self._read() as conn:
                existing = (
                    (
                        await conn.execute(
                            text("SELECT * FROM cli_worker_spawns WHERE spawn_id = :spawn_id"),
                            {"spawn_id": payload["spawn_id"]},
                        )
                    )
                    .mappings()
                    .first()
                )
            if existing is not None:
                compare(existing)
                return
            async with self._tx() as conn:
                if self.dialect == "postgresql":
                    await conn.execute(text("SET LOCAL lock_timeout = '250ms'"))
                parent_sql = (
                    "SELECT status, cli_worker_generation FROM sessions WHERE id = :session_id"
                )
                if self.dialect == "postgresql":
                    parent_sql += " FOR UPDATE"
                parent = (
                    (await conn.execute(text(parent_sql), {"session_id": payload["session_id"]}))
                    .mappings()
                    .first()
                )
                # Another registration may have committed while this call
                # waited for the parent lock, followed by terminalization or
                # resume. Recheck durable replay before testing NEW admission.
                existing = (
                    (
                        await conn.execute(
                            text("SELECT * FROM cli_worker_spawns WHERE spawn_id = :spawn_id"),
                            {"spawn_id": payload["spawn_id"]},
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    compare(existing)
                    return
                if parent is None or parent["status"] != "running":
                    raise ValueError("a new worker spawn requires a running session")
                if parent["cli_worker_generation"] != payload["generation_id"]:
                    raise ValueError("a new worker spawn requires the current session generation")
                parameters = dict(payload)
                parameters["recorded_at"] = _validate_cli_worker_timestamp(
                    time.time(), field="recorded_at"
                )
                await conn.execute(
                    text(
                        "INSERT INTO cli_worker_spawns "
                        "(spawn_id, session_id, generation_id, run_id, engine_run_id, branch_id, "
                        "pid, pgid, pid_create_time, pid_host, pid_boot_time, process_identity_mode, "
                        "recorded_at, confirmed_empty_at) VALUES "
                        "(:spawn_id, :session_id, :generation_id, :run_id, :engine_run_id, :branch_id, "
                        ":pid, :pgid, :pid_create_time, :pid_host, :pid_boot_time, :process_identity_mode, "
                        ":recorded_at, NULL) ON CONFLICT (spawn_id) DO NOTHING"
                    ),
                    parameters,
                )
                stored = (
                    (
                        await conn.execute(
                            text("SELECT * FROM cli_worker_spawns WHERE spawn_id = :spawn_id"),
                            {"spawn_id": payload["spawn_id"]},
                        )
                    )
                    .mappings()
                    .first()
                )
                if stored is None:
                    raise RuntimeError("worker spawn insert returned without a stored record")
                compare(stored)

    @_operation("snapshot_cli_worker_spawns")
    async def snapshot_cli_worker_spawns(self, session_id: str) -> dict[str, Any] | None:
        """Read one session and all its unresolved generations in one snapshot."""
        self._refuse_in_compatibility(
            "snapshot_cli_worker_spawns", "worker spawn records require native storage"
        )
        session_id = _validate_cli_worker_session_id(session_id)
        with fail_after(_CLI_WORKER_OPERATION_TIMEOUT_SECONDS):
            async with self.read_snapshot() as conn:
                session = (
                    (
                        await conn.execute(
                            text(
                                "SELECT id, status, cli_worker_generation, node_metadata, "
                                "artifacts_path, source_kind FROM sessions WHERE id = :id"
                            ),
                            {"id": session_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                if session is None:
                    return None
                rows = (
                    (
                        await conn.execute(
                            text(
                                "SELECT * FROM cli_worker_spawns WHERE session_id = :id "
                                "AND confirmed_empty_at IS NULL ORDER BY recorded_at, spawn_id"
                            ),
                            {"id": session_id},
                        )
                    )
                    .mappings()
                    .all()
                )
                return {
                    "session": self._row_to_dict(session),
                    "spawns": [_validate_cli_worker_spawn_row(row) for row in rows],
                }

    @_cancellation_wins
    @asynccontextmanager
    async def guard_cli_worker_signal(
        self,
        session_id: str,
        snapshotted_status: str,
        snapshotted_generation: str | None,
        spawn_id: str,
    ) -> AsyncIterator[Literal["unchanged", "changed"]]:
        """Hold the execution and exact unresolved record stable for one signal.

        The caller performs only synchronous identity checks and its signal
        while this context is entered. No grace wait belongs under this lock.
        """
        self._refuse_in_compatibility(
            "guard_cli_worker_signal", "worker cleanup requires native storage"
        )
        if self.readonly:
            raise ValueError("worker signal guards require a writable state handle")
        session_id = _validate_cli_worker_session_id(session_id)
        generation = _validate_cli_worker_generation(snapshotted_generation)
        spawn_id = _validate_cli_worker_uuid(spawn_id, field="spawn_id")
        with fail_after(_CLI_WORKER_OPERATION_TIMEOUT_SECONDS):
            async with self._tx() as conn:
                if self.dialect == "postgresql":
                    await conn.execute(text("SET LOCAL lock_timeout = '250ms'"))
                parent_sql = "SELECT status, cli_worker_generation FROM sessions WHERE id = :id"
                if self.dialect == "postgresql":
                    parent_sql += " FOR UPDATE"
                parent = (
                    (await conn.execute(text(parent_sql), {"id": session_id})).mappings().first()
                )
                if (
                    parent is None
                    or parent["status"] != snapshotted_status
                    or parent["cli_worker_generation"] != generation
                ):
                    yield "changed"
                    return
                record_sql = (
                    "SELECT * FROM cli_worker_spawns WHERE spawn_id = :spawn_id "
                    "AND session_id = :session_id AND confirmed_empty_at IS NULL"
                )
                if self.dialect == "postgresql":
                    record_sql += " FOR UPDATE"
                stored = (
                    (
                        await conn.execute(
                            text(record_sql), {"spawn_id": spawn_id, "session_id": session_id}
                        )
                    )
                    .mappings()
                    .first()
                )
                if stored is None:
                    yield "changed"
                    return
                # Registration makes the identity and association immutable.
                # Older recorded generations remain eligible for this snapshot.
                _validate_cli_worker_spawn_row(stored)
                yield "unchanged"

    @_operation("cancel_session_if_worker_generation")
    async def cancel_session_if_worker_generation(
        self,
        session_id: str,
        generation_id: str | None,
        *,
        expected_runner_identity: Mapping[str, Any],
        reason_code: str,
        reason_summary: str,
        evidence: dict[str, Any],
    ) -> Literal["applied", "already_terminal", "conflict", "missing", "rejected"]:
        """Cancel only the captured running execution, through lifecycle policy."""
        self._refuse_in_compatibility(
            "cancel_session_if_worker_generation", "worker cleanup requires native storage"
        )
        if self.readonly:
            raise ValueError("worker cancellation requires a writable state handle")
        session_id = _validate_cli_worker_session_id(session_id)
        generation_id = _validate_cli_worker_generation(generation_id)
        if not isinstance(expected_runner_identity, Mapping):
            raise ValueError("expected_runner_identity must be a mapping")
        expected_runner_identity = dict(expected_runner_identity)
        _validate_reason_code(reason_code)
        with fail_after(_CLI_WORKER_OPERATION_TIMEOUT_SECONDS):
            return await _lifecycle_adapters.run_cancel_session_if_worker_generation(
                self._lifecycle_service(),
                session_id=session_id,
                generation_id=generation_id,
                expected_runner_identity=expected_runner_identity,
                reason_code=reason_code,
                reason_summary=reason_summary,
                evidence=evidence,
            )

    @_operation("settle_cli_worker_spawn")
    async def settle_cli_worker_spawn(self, spawn_id: str, *, confirmed_empty_at: float) -> bool:
        """Set the exact record's empty marker once, without changing its owner.

        The caller must have established kernel process-group absence under
        the recorded-identity contract. Normal child exit or lost identity
        witnesses do not establish that observation.
        """
        self._refuse_in_compatibility(
            "settle_cli_worker_spawn", "worker spawn records require native storage"
        )
        spawn_id = _validate_cli_worker_uuid(spawn_id, field="spawn_id")
        confirmed_empty_at = _validate_cli_worker_timestamp(
            confirmed_empty_at, field="confirmed_empty_at"
        )
        with fail_after(_CLI_WORKER_OPERATION_TIMEOUT_SECONDS):
            async with self._tx() as conn:
                if self.dialect == "postgresql":
                    await conn.execute(text("SET LOCAL lock_timeout = '250ms'"))
                result = await conn.execute(
                    text(
                        "UPDATE cli_worker_spawns SET confirmed_empty_at = :at "
                        "WHERE spawn_id = :spawn_id AND confirmed_empty_at IS NULL"
                    ),
                    {"spawn_id": spawn_id, "at": confirmed_empty_at},
                )
                return bool(result.rowcount)

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
            if not await _locked_cli_worker_prune_candidates(
                conn, [session_id], dialect=self.dialect
            ):
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
        clause, owner = self._owner_predicate("sessions")
        await conn.execute(
            text(self._touch_activity_sql(self.dialect) + clause),
            {"ts": ts, "id": session_id, **owner},
        )

    @_operation("persist_live_message")
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
        if self._compat is not None:
            # A live message joins only the session, progressions and branch this run
            # created.
            self._require_created("sessions", session_id, "session")
            for progression_id in (branch_progression_id, session_progression_id):
                if progression_id is not None:
                    self._require_created("progressions", progression_id, "progression")
            if system_branch_id is not None:
                self._require_created("branches", system_branch_id, "branch")
        async with self._tx() as conn:
            if self._compat is not None:
                for table, row_id, what in (
                    ("sessions", session_id, "session"),
                    ("progressions", branch_progression_id, "progression"),
                    ("progressions", session_progression_id, "progression"),
                    ("branches", system_branch_id, "branch"),
                ):
                    if row_id is not None:
                        await self._require_owned(conn, table, row_id, what)
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

    @_operation("set_session_user_label")
    async def set_session_user_label(self, session_id: str, user_label: str | None) -> bool:
        """Set or clear a label without changing the session's activity or system name.

        Return False when the session does not exist. Generic session updates
        deliberately exclude this column because they advance updated_at.
        """
        from lionagi.state.session_naming import normalize_session_user_label

        user_label = normalize_session_user_label(user_label)
        if self._compat is not None:
            self._require_created("sessions", session_id, "session")
        clause, owner = self._owner_predicate("sessions")
        async with self._tx() as conn:
            if self._compat is not None:
                await self._require_owned(conn, "sessions", session_id, "session")
            result = await conn.execute(
                text(
                    "UPDATE sessions SET user_label = :user_label "  # noqa: S608
                    "WHERE id = :id" + clause
                ),
                {"user_label": user_label, "id": session_id, **owner},
            )
            return bool(result.rowcount)

    @_operation("update_session")
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
        """Update session fields; status changes route through update_status().

        Natively ``node_metadata`` is written whole, as given. A ``set_if_null`` fill of it treats
        a stored JSON ``null`` as absent, in either mode.

        In compatibility mode the session must be one this run created and still carry its owner;
        a message it links to must be one this run created and still carry that owner, and an
        invocation it links to must carry that owner. ``node_metadata`` is merged by top-level key
        into the stored object inside this write's transaction, and inside the status change's own
        transaction when there is one, so a reader never sees the metadata without the status it
        landed with: a key the payload omits keeps its stored value, a nested object under a key
        the payload sends replaces the stored object there, and a stored or given value that is not
        a JSON object is refused.
        """
        if "cli_worker_generation" in fields:
            fields["cli_worker_generation"] = _validate_cli_worker_generation(
                fields["cli_worker_generation"]
            )
            self._refuse_in_compatibility(
                "update_session", "cli_worker_generation requires a native state store"
            )
        if self._compat is not None:
            self._require_created("sessions", session_id, "session")
            for link, table, what, created in _SESSION_LINKS:
                if fields.get(link) is not None and created:
                    self._require_created(table, fields[link], what)
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
        # A status change takes node_metadata into the lifecycle transition, which writes the
        # fields it is handed whole. The transition merges the value in its own transaction
        # instead (see _prepare_status_patch), and is told here which columns are fills.
        fill_only_if_absent = set_if_null
        # A status change writes the status in the transition's own transaction, before this
        # call's fat-field UPDATE runs its in-transaction link checks. The named links are checked
        # inside that same transaction, before the status is written, so a refused link leaves the
        # stored status as it was rather than a status the store already committed (ADR-0128 D5).
        owned_links = tuple(
            (table, fields[link], what)
            for link, table, what, _created in _SESSION_LINKS
            if fields.get(link) is not None
        )
        await self._route_status_change(
            "session",
            session_id,
            "update_session",
            fields,
            owned_links=owned_links,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=evidence_refs,
            reason_source=reason_source,
            reason_actor=reason_actor,
            override=override,
            override_actor=override_actor,
            override_justification=override_justification,
            fill_only_if_absent=fill_only_if_absent,
            # The operation wrapper adds a frame between this method and its caller.
            warn_stacklevel=4,
        )

        if fields:
            fields["updated_at"] = time.time()
            merging_metadata = self._compat is not None and "node_metadata" in fields
            sets = ", ".join(
                f'"{k}" = {_set_if_null_expression(k, self.dialect)}'
                if k in set_if_null
                else f'"{k}" = :{k}'
                for k in fields
            )
            params = dict(fields)
            params["_id"] = session_id
            clause, owner = self._owner_predicate("sessions")
            async with self._tx() as conn:
                if self._compat is not None:
                    await self._require_owned(conn, "sessions", session_id, "session")
                    for link, table, what, _created in _SESSION_LINKS:
                        if params.get(link) is not None:
                            await self._require_owned(conn, table, params[link], what)
                    if merging_metadata:
                        params["node_metadata"] = await self._merged_node_metadata(
                            conn,
                            "sessions",
                            session_id,
                            params["node_metadata"],
                            fill_only_if_absent="node_metadata" in set_if_null,
                        )
                await conn.execute(
                    text(f"UPDATE sessions SET {sets} WHERE id = :_id{clause}"),  # noqa: S608
                    {**params, **owner},
                )

    @staticmethod
    def _node_metadata_merge_ops(patch: dict[str, Any]) -> list[tuple[str, tuple[str, ...], Any]]:
        """The in-database steps one RFC 7396 patch needs, containers before their contents.

        Each entry is ``("object", path, None)`` to make the value at *path* an object to merge
        into, or ``("value", path, value)`` to set it -- removing it when *value* is ``None``.
        An object-valued node gets a step of its own so a subtree holding only empty objects still
        lands, and every container step sorts ahead of the values under it, so a value is never
        written through a container that is not one yet.
        """
        nodes: list[tuple[tuple[str, ...], Any, int]] = []
        stack: list[tuple[tuple[str, ...], Any, int]] = [((), patch, 0)]
        while stack:
            path, value, depth = stack.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    nodes.append((path + (key,), child, depth + 1))
                    stack.append((path + (key,), child, depth + 1))
        containers = sorted((n for n in nodes if isinstance(n[1], dict)), key=lambda n: n[2])
        return [("object", path, None) for path, _, _ in containers] + [
            ("value", path, value) for path, value, _ in nodes if not isinstance(value, dict)
        ]

    @staticmethod
    def _merge_node_metadata_sql(
        dialect: str,
        *,
        ops: list[tuple[str, tuple[str, ...], Any]],
        table: str = "sessions",
    ) -> str:
        """One dialect-specific UPDATE that reads, merges and writes node_metadata in-database.

        *ops* is the fold :meth:`_node_metadata_merge_ops` derived from the patch; the caller
        binds the patch's own values in the same order, so the two cannot disagree. It is
        required, because a caller that forgot it would build an UPDATE that merges nothing.
        """
        if dialect == "sqlite":
            # json_patch is RFC 7396 already: nested objects merge, a key the patch sets to null is
            # removed, and an array or scalar patch value replaces the stored value outright.
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
        # PostgreSQL's `||` merges top-level keys only, so a nested patch value would replace the
        # stored object instead of merging into it. Reproduce RFC 7396 with one jsonb_set per patch
        # node, containers first, and `#-` for a key the patch sets to null. Each container step
        # reads the document through a one-row subquery, so the steps so far appear once no matter
        # how deep the patch nests.
        expr = (
            "CASE"
            "  WHEN node_metadata IS NULL THEN '{}'::jsonb"
            "  WHEN jsonb_typeof(node_metadata::jsonb) = 'object' THEN node_metadata::jsonb"
            "  WHEN jsonb_typeof(node_metadata::jsonb) = 'null' THEN '{}'::jsonb"
            "  ELSE jsonb_build_object('_discarded_node_metadata', node_metadata::jsonb,"
            "                          '_discarded_at', to_jsonb(CAST(:now AS double precision)))"
            "END"
        )
        for index, (kind, path, value) in enumerate(ops or []):
            # A patch key path, one bound parameter per element. `ARRAY[...]` takes text values,
            # and a key the caller supplied is never spelled into the statement: read from inside
            # a text() statement, a key holding `:name`, a lone `:id`, a backslash or a quote
            # would be read as a bind parameter, substituted, or unescaped, so the path would not
            # be the key the caller named.
            at = "ARRAY[{}]".format(
                ", ".join(f"CAST(:k{index}_{depth} AS text)" for depth in range(len(path)))
            )
            if kind == "object":
                expr = (
                    f"(SELECT jsonb_set(_doc, {at},"  # noqa: S608 -- patch key path, bound
                    f" CASE WHEN jsonb_typeof(_doc #> {at}) = 'object'"
                    f" THEN _doc #> {at} ELSE '{{}}'::jsonb END, true)"
                    f" FROM (SELECT {expr} AS _doc) AS _m{index})"
                )
            elif value is None:
                expr = f"({expr} #- {at})"
            else:
                expr = f"jsonb_set({expr}, {at}, CAST(:v{index} AS jsonb), true)"
        return (
            f"UPDATE {table} SET "  # noqa: S608
            f"node_metadata = ({expr})::json, "
            "updated_at = :now "
            "WHERE id = :id"
        )

    async def _merged_node_metadata(
        self,
        conn,
        table: str,
        entity_id: str,
        given: Any,
        *,
        fill_only_if_absent: bool = False,
    ) -> str:
        """Merge *given* into the stored node_metadata of *table* row *entity_id*, by top-level key.

        The merge runs inside the caller's transaction, so the value read and the value written
        cannot be split by a concurrent writer. A stored JSON ``null`` and a stored SQL NULL both
        hold no keys yet, so either merges as an empty object; a fill leaves an object that is
        already stored untouched.
        """
        stored = (
            await conn.execute(
                text(f"SELECT node_metadata FROM {table} WHERE id = :_id"),  # noqa: S608
                {"_id": entity_id},
            )
        ).scalar()
        stored_value = _decode_node_metadata(stored)
        given_value = _decode_node_metadata(given)
        if not isinstance(given_value, dict):
            raise self._node_metadata_refusal(entity_id, "given")
        if stored_value is not None and not isinstance(stored_value, dict):
            raise self._node_metadata_refusal(entity_id, "stored")
        if fill_only_if_absent and isinstance(stored_value, dict):
            # The stored object is already there, and a fill never displaces one.
            return json.dumps(stored_value)
        merged: dict[str, Any] = {}
        if isinstance(stored_value, dict):
            merged.update(stored_value)
        merged.update(given_value)
        return json.dumps(merged)

    def _node_metadata_refusal(self, entity_id: str, which: str) -> Exception:
        """The one refusal for a node_metadata value the by-key merge cannot take.

        Compatibility mode raises the store's own refusal; natively it is a plain ``ValueError``,
        so the message is the same either way and only the type follows the mode.
        """
        message = (
            f"the {which} node_metadata of session {entity_id!r} is not a JSON object; "
            "node_metadata is merged by key and never written whole"
        )
        if self._compat is not None:
            return self._compat.refusal(message)
        return ValueError(message)

    async def _prepare_status_patch(
        self,
        conn,
        entity_type: str,
        entity_id: str,
        patch: Mapping[str, Any],
        *,
        fill_only_if_absent: frozenset[str] = frozenset(),
    ) -> Mapping[str, Any]:
        """The patch a status write is about to make, with its node_metadata merged by key.

        The lifecycle transition writes the fields it is handed whole, so compatibility mode
        merges node_metadata here, inside the transition's own transaction, and hands the
        transition the merged document. Natively the patch is returned unchanged.
        """
        if self._compat is None or entity_type != "session" or "node_metadata" not in patch:
            return patch
        merged = await self._merged_node_metadata(
            conn,
            "sessions",
            entity_id,
            patch["node_metadata"],
            fill_only_if_absent="node_metadata" in fill_only_if_absent,
        )
        return {**patch, "node_metadata": merged}

    async def _merge_node_metadata(self, table: str, entity_id: str, patch: dict[str, Any]) -> None:
        """Shared body for the two node_metadata merges: build the merge, then UPDATE *table*."""
        ops = self._node_metadata_merge_ops(patch)
        if self._compat is not None:
            self._require_created(table, entity_id, table[:-1])
        now = time.time()
        clause, owner = self._owner_predicate(table)
        params: dict[str, Any] = {"now": now, "id": entity_id, **owner}
        binds = []
        if self.dialect == "sqlite":
            params["patch"] = patch
            binds.append(bindparam("patch", type_=JSON))
        else:
            for index, (kind, path, value) in enumerate(ops):
                # Every path element the statement reads back, bound in that statement's own
                # parameter names, so a key cannot reach the SQL text at all.
                for depth, key in enumerate(path):
                    params[f"k{index}_{depth}"] = key
                    binds.append(bindparam(f"k{index}_{depth}"))
                if kind == "value" and value is not None:
                    params[f"v{index}"] = value
                    binds.append(bindparam(f"v{index}", type_=JSON))
        stmt = text(self._merge_node_metadata_sql(self.dialect, ops=ops, table=table) + clause)
        if binds:
            stmt = stmt.bindparams(*binds)
        async with self._tx() as conn:
            if self._compat is not None:
                await self._require_owned(conn, table, entity_id, table[:-1])
            await conn.execute(stmt, params)

    @_operation("merge_session_node_metadata")
    async def merge_session_node_metadata(self, session_id: str, patch: dict[str, Any]) -> None:
        """Atomically merge *patch* into the session's node_metadata column under RFC 7396.

        Keys merge per RFC 7396: a nested object patch merges into the stored object rather than
        replacing it, a key the patch sets to ``null`` is removed, and an array or scalar patch
        value replaces whatever was stored at that key. A stored value that is not an object -- an
        array, a scalar, or on SQLite even non-JSON text -- is preserved verbatim under
        ``_discarded_node_metadata`` instead of being dropped.
        """
        await self._merge_node_metadata("sessions", session_id, patch)

    async def merge_invocation_node_metadata(
        self, invocation_id: str, patch: dict[str, Any]
    ) -> None:
        """Atomically merge *patch* into the invocation's node_metadata column under RFC 7396.

        Same semantics as :meth:`merge_session_node_metadata`, for the invocations table.
        """
        await self._merge_node_metadata("invocations", invocation_id, patch)

    @_operation("update_artifact_verification")
    async def update_artifact_verification(
        self,
        session_id: str,
        verification: dict[str, Any] | None,
    ) -> None:
        if self._compat is not None:
            self._require_created("sessions", session_id, "session")
        clause, owner = self._owner_predicate("sessions")
        # Must hold _write_lock: teardown calls this while signal persistence is still bound, so a
        # late signal emit's _tx() can race this UPDATE on SQLite.
        async with self._tx() as conn:
            if self._compat is not None:
                await self._require_owned(conn, "sessions", session_id, "session")
            await conn.execute(
                text(
                    "UPDATE sessions SET artifact_verification_json = :v, updated_at = :now "  # noqa: S608
                    "WHERE id = :id" + clause
                ).bindparams(bindparam("v", type_=JSON)),
                {"v": verification, "now": time.time(), "id": session_id, **owner},
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
        fill_only_if_absent: frozenset[str] = frozenset(),
        owned_links: tuple[tuple[str, str, str], ...] = (),
        warn_stacklevel: int = 3,
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
                stacklevel=warn_stacklevel,
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
            fill_only_if_absent=fill_only_if_absent,
            owned_links=owned_links,
        )

    @_operation("update_status")
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
        fill_only_if_absent: frozenset[str] = frozenset(),
        owned_links: tuple[tuple[str, str, str], ...] = (),
    ) -> bool:
        """Atomically transition a status and record the reason, under a terminal-status floor.

        ``fill_only_if_absent`` names ``extra_fields`` entries that are written only where the
        stored value is absent, exactly as ``update_session``'s ``set_if_null`` does for the
        columns it writes itself.
        """
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
        if canonical_type == "session" and extra_fields and "cli_worker_generation" in extra_fields:
            generation = _validate_cli_worker_generation(extra_fields["cli_worker_generation"])
            self._refuse_in_compatibility(
                "update_status", "cli_worker_generation requires a native state store"
            )
            extra_fields = dict(extra_fields, cli_worker_generation=generation)
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
        if self._compat is not None:
            # Compatibility mode moves only its own run's session, and never by override.
            if canonical_type != "session":
                raise self._compat.refusal(
                    f"compatibility mode transitions only its run's session, not a {canonical_type}"
                )
            if override:
                raise self._compat.refusal(
                    "an override transition is refused in compatibility mode"
                )
            self._require_created("sessions", entity_id, "session")

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
                fill_only_if_absent=fill_only_if_absent,
                owned_links=owned_links,
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

    async def create_schedule(
        self,
        schedule: dict[str, Any],
        *,
        audit_context: _ScheduleAuditContext | None = None,
        reason: _ReasonRecord | None = None,
    ) -> None:
        adapter = self._lifecycle_service().schedule_adapter
        adapter.validate_create(schedule, audit_context=audit_context, reason=reason)
        stmt, params = self._build_schedule_insert_stmt(schedule)
        async with self._tx() as conn:
            await adapter.assert_id_available(conn, params["id"])
            await conn.execute(stmt, params)
            await adapter.record_created(conn, params, audit_context=audit_context, reason=reason)

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

    async def get_schedule_history(
        self, schedule_id: str, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """Read newest definition events, including after deletion, without migrating old stores.

        An available empty ledger means no events were recorded. An unavailable ledger
        is reported separately; a read-only legacy store must never acquire a new table.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        async with self._read() as conn:
            if not await conn.run_sync(lambda c: inspect(c).has_table("status_transitions")):
                return {"available": False, "events": []}
            owner_where, owner_params = self._owner_predicate("status_transitions")
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, entity_type, entity_id, previous_status, status, "
                            "reason_code, reason_summary, evidence_refs, source, actor, created_at, metadata "
                            "FROM status_transitions "
                            "WHERE entity_type='schedule' AND entity_id=:id"
                            + owner_where
                            + " ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"
                        ),
                        {"id": schedule_id, "limit": limit, "offset": offset, **owner_params},
                    )
                )
                .mappings()
                .all()
            )
        events = [dict(row) for row in rows]
        for row in events:
            for field in ("metadata", "evidence_refs"):
                if isinstance(row[field], str):
                    row[field] = json.loads(row[field])
        return {"available": True, "events": events}

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
        expect_owner_keys: dict[str, str | None] | None = None,
        expect_updated_at: dict[str, float] | None = None,
        audit_context: _ScheduleAuditContext | None = None,
        reason: _ReasonRecord | None = None,
    ) -> None:
        """Atomically commit a ScheduleSet plan and any enablement edges.

        When supplied, *expect_owner_keys* claims the ownership value each
        update was planned against, and *expect_updated_at* the ``updated_at``
        value, which every schedule write moves. A row that no longer matches
        its claims makes the entire apply fail.
        """
        claims = expect_owner_keys or {}
        stamps = expect_updated_at or {}
        adapter = self._lifecycle_service().schedule_adapter
        context = (
            _ScheduleAuditContext(origin="declaration", source="declaration")
            if audit_context is None
            else audit_context
        )
        adapter.validate_context(context)
        if reason is not None:
            _validate_reason_code(reason.code)
            if not reason.code.startswith("schedule_definition."):
                raise ValueError("ScheduleSet reasons must use the schedule_definition namespace")
        async with self._tx() as conn:
            for schedule in creates:
                created_reason = self._schedule_set_reason(
                    schedule, _ScheduleDefinitionReasons.CREATED_OBSERVED, "created", reason
                )
                adapter.validate_create(schedule, audit_context=context, reason=created_reason)
                stmt, params = self._build_schedule_insert_stmt(schedule)
                await adapter.assert_id_available(conn, params["id"])
                await conn.execute(stmt, params)
                await adapter.record_created(
                    conn, params, audit_context=context, reason=created_reason
                )
            for schedule_id, fields in updates:
                claimed = schedule_id in claims
                stmt, params = self._build_update_schedule_stmt(
                    schedule_id,
                    fields,
                    expect_owner_key=claims[schedule_id] if claimed else NO_CURSOR_CLAIM,
                    expect_updated_at=stamps.get(schedule_id, NO_CURSOR_CLAIM),
                )
                matched = await self._update_schedule_in_tx(
                    conn,
                    schedule_id,
                    fields,
                    stmt,
                    params,
                    audit_context=context,
                    reason=reason,
                    declaration_cause="declared",
                )
                if (claimed or schedule_id in stamps) and not matched:
                    raise ScheduleOwnershipConflictError([schedule_id])
            for schedule_id in disables:
                claimed = schedule_id in claims
                stmt, params = self._build_update_schedule_stmt(
                    schedule_id,
                    {"enabled": 0},
                    expect_owner_key=claims[schedule_id] if claimed else NO_CURSOR_CLAIM,
                    expect_updated_at=stamps.get(schedule_id, NO_CURSOR_CLAIM),
                )
                matched = await self._update_schedule_in_tx(
                    conn,
                    schedule_id,
                    {"enabled": 0},
                    stmt,
                    params,
                    audit_context=context,
                    reason=reason,
                    declaration_cause="omitted",
                )
                if (claimed or schedule_id in stamps) and not matched:
                    raise ScheduleOwnershipConflictError([schedule_id])

    @staticmethod
    def _schedule_set_reason(
        row: Mapping[str, Any], code: str, cause: str, reason: _ReasonRecord | None
    ) -> _ReasonRecord:
        """Keep optional apply text beside the generated member-specific cause."""
        return _ReasonRecord(
            code=code,
            summary=reason.summary if reason else "",
            evidence_refs=reason.evidence_refs if reason else (),
            metadata={
                **(dict(reason.metadata) if reason else {}),
                "declaration": {
                    "owner_key": row.get("owner_key"),
                    "member_id": row["id"],
                    "cause": cause,
                },
            },
        )

    async def _update_schedule_in_tx(
        self,
        conn: AsyncConnection,
        schedule_id: str,
        fields: dict[str, Any],
        stmt: Any,
        params: dict[str, Any],
        *,
        audit_context: _ScheduleAuditContext | None,
        reason: _ReasonRecord | None,
        declaration_cause: str | None = None,
    ) -> bool:
        """Preserve existing claims while coupling an enabled edge to its history."""
        adapter = self._lifecycle_service().schedule_adapter
        adapter.validate_context(audit_context)
        before = None
        after = None
        if "enabled" in fields or reason is not None:
            before = await adapter.read_for_update(conn, schedule_id)
            if before is not None:
                after = {**before, **fields}
                if declaration_cause is not None:
                    if after["enabled"]:
                        code = _ScheduleDefinitionReasons.ENABLED_OBSERVED
                    elif declaration_cause == "omitted":
                        code = _ScheduleDefinitionReasons.DISABLED_OMITTED
                    else:
                        code = _ScheduleDefinitionReasons.DISABLED_DECLARED
                    reason = self._schedule_set_reason(after, code, declaration_cause, reason)
                adapter.validate_change(before, after, audit_context=audit_context, reason=reason)
        result = await conn.execute(stmt, params)
        if result.rowcount > 0 and before is not None:
            await adapter.record_change(
                conn, before, after, audit_context=audit_context, reason=reason
            )
        return result.rowcount > 0

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
        expect_next_fire_at: CursorClaim = NO_CURSOR_CLAIM,
        expect_github_cursor: CursorClaim = NO_CURSOR_CLAIM,
        expect_owner_key: str | None | NoCursorClaim = NO_CURSOR_CLAIM,
        audit_context: _ScheduleAuditContext | None = None,
        reason: _ReasonRecord | None = None,
        **fields: Any,
    ) -> bool:
        """Update one schedule; returns whether a row matched.

        With *expect_next_fire_at* the update also claims that cursor value, so a caller that
        reserves a due instant ahead of dispatching it learns whether it won the reservation.

        *expect_github_cursor* claims the poll cursor the same way, which is what serializes the
        events inside one poll batch. A batch's events all resolve to the same next_fire_at, so a
        claim on that value matches twice and separates nothing; github_cursor advances per event
        and is the only value in the row that distinguishes one event of a batch from the next.
        """
        stmt, params = self._build_update_schedule_stmt(
            schedule_id,
            fields,
            guard_cursor_forward=guard_cursor_forward,
            expect_next_fire_at=expect_next_fire_at,
            expect_github_cursor=expect_github_cursor,
            expect_owner_key=expect_owner_key,
        )
        async with self._tx() as conn:
            return await self._update_schedule_in_tx(
                conn, schedule_id, fields, stmt, params, audit_context=audit_context, reason=reason
            )

    async def record_github_poll_stagnation(self, schedule_id: str, *, reset: bool) -> bool:
        """Account for one settled observation against the current GitHub schedule.

        The transaction's commit orders independent observations. A late increment after
        a reset starts at one instead of restoring a caller's earlier snapshot.
        """
        stmt = text(
            "UPDATE schedules SET poller_consecutive_cap_no_dispatch = "
            "CASE WHEN :reset THEN 0 ELSE poller_consecutive_cap_no_dispatch + 1 END, "
            "updated_at = :updated_at WHERE id = :id AND trigger_type = 'github_poll'"
        )
        params = {"id": schedule_id, "reset": reset, "updated_at": time.time()}
        context = _ScheduleAuditContext(origin="scheduler", source="scheduler", assurance="system")
        async with self._tx() as conn:
            return await self._update_schedule_in_tx(
                conn, schedule_id, {}, stmt, params, audit_context=context, reason=None
            )

    @classmethod
    def _build_update_schedule_stmt(
        cls,
        schedule_id: str,
        fields: dict[str, Any],
        *,
        guard_cursor_forward: bool = False,
        expect_next_fire_at: CursorClaim = NO_CURSOR_CLAIM,
        expect_github_cursor: CursorClaim = NO_CURSOR_CLAIM,
        expect_owner_key: str | None | NoCursorClaim = NO_CURSOR_CLAIM,
        expect_updated_at: float | None | NoCursorClaim = NO_CURSOR_CLAIM,
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
                # Cursors are compared as strings everywhere, since the poller compares them
                # against GitHub's own timestamps, so plain lexical order IS the ordering. The
                # PR number a cursor carries after its timestamp is zero-padded to keep that
                # true within one second.
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
        # One loop rather than a block per column, so a second claim cannot be given subtly
        # different NULL handling from the first. Branching on the Python value rather than
        # emitting a NULL-safe operator: sqlite spells one `IS`, postgres rejects `IS` with a
        # bound parameter outright, and their shared spelling (`IS NOT DISTINCT FROM`) is too
        # new to rely on. A schedule with no cursor is guarded on the same terms as one with it.
        for column, expected, bind in (
            ("next_fire_at", expect_next_fire_at, "_expect_nfa"),
            ("github_cursor", expect_github_cursor, "_expect_ghc"),
            ("owner_key", expect_owner_key, "_expect_owner"),
            ("updated_at", expect_updated_at, "_expect_updated_at"),
        ):
            if expected is NO_CURSOR_CLAIM:
                continue
            if expected is None:
                where += f" AND {column} IS NULL"
            else:
                where += f" AND {column} = :{bind}"
                params[bind] = expected
        stmt = text(f"UPDATE schedules SET {', '.join(sets_parts)} WHERE {where}")  # noqa: S608
        if bind_params:
            stmt = stmt.bindparams(*bind_params)
        return stmt, params

    async def delete_schedule(
        self,
        schedule_id: str,
        *,
        audit_context: _ScheduleAuditContext | None = None,
        reason: _ReasonRecord | None = None,
    ) -> bool:
        adapter = self._lifecycle_service().schedule_adapter
        adapter.validate_context(audit_context)
        async with self._tx() as conn:
            before = await adapter.read_for_update(conn, schedule_id)
            if before is None:
                return False
            adapter.validate_change(before, None, audit_context=audit_context, reason=reason)
            result = await conn.execute(
                text("DELETE FROM schedules WHERE id = :id"),
                {"id": schedule_id},
            )
            if result.rowcount > 0:
                await adapter.record_change(
                    conn, before, None, audit_context=audit_context, reason=reason
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
        expect_next_fire_at: CursorClaim,
        expect_github_cursor: CursorClaim = NO_CURSOR_CLAIM,
        audit_context: _ScheduleAuditContext | None = None,
        reason: _ReasonRecord | None = None,
    ) -> bool:
        """Insert one occurrence row and advance the schedule's cursor in a single transaction.

        Returns False when a claim no longer matches, having written nothing. Pass
        ``NO_CURSOR_CLAIM`` for a fire that does not stand for a due instant, such as a chain
        child or a replacement for an occurrence already recorded. *expect_github_cursor* is the
        per-event claim a poll batch needs, since every event in one batch shares a due instant.
        """
        run_stmt, run_params = self._build_schedule_run_insert_stmt(run)
        # Engine-only path, so the monotonic cursor guard always applies: this value came from a
        # poll snapshot and must never walk an operator's move backwards.
        sched_stmt, sched_params = self._build_update_schedule_stmt(
            schedule_id,
            schedule_fields,
            guard_cursor_forward=True,
            expect_next_fire_at=expect_next_fire_at,
            expect_github_cursor=expect_github_cursor,
        )
        async with self._tx() as conn:
            # The cursor claim goes FIRST so a lost race writes no occurrence at all. Selecting a
            # due schedule and firing it are separate statements, so without this predicate two
            # schedulers reading one due row both commit, each with its own run id.
            matched = await self._update_schedule_in_tx(
                conn,
                schedule_id,
                schedule_fields,
                sched_stmt,
                sched_params,
                audit_context=audit_context,
                reason=reason,
            )
            if not matched:
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
        reason_summary: str = "undispatched schedule run replaced during recovery",
        evidence_refs: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Tombstone an undispatched orphan and insert its replacement in one transaction."""
        run_stmt, run_params = self._build_schedule_run_insert_stmt(replacement_run)
        lifecycle_service = self._lifecycle_service()
        now = time.time()
        command = _TransitionCommand(
            entity_type="schedule_run",
            entity_id=orphan_id,
            to_status="failed",
            reason=_ReasonRecord(
                code=_RunReasons.FAILED_NEVER_DISPATCHED,
                summary=reason_summary,
                evidence_refs=tuple(evidence_refs or ()),
            ),
            actor=_ActorRecord(
                type="scheduler",
                id="tombstone_and_replace_schedule_run",
            ),
        )
        async with self._tx() as conn:
            history_write = await lifecycle_service._write(  # noqa: SLF001
                conn,
                "schedule_runs",
                command,
                previous_status=expected_orphan_status,
                now=now,
                extra_guard={"dispatched_at": None},
            )
            if history_write is None:
                return False
            await conn.execute(run_stmt, run_params)
            await self._initialize_managed_entity_in_tx(
                conn,
                entity_type="schedule_run",
                entity_id=run_params["id"],
                status=run_params["status"],
                actor_id="tombstone_and_replace_schedule_run",
            )
        await lifecycle_service._emit_terminal_event(  # noqa: SLF001
            command,
            previous_status=expected_orphan_status,
            transition_id=history_write.transition_id,
            occurred_at=now,
            eligibility_stored=history_write.eligibility_stored,
            eligibility_at_write=history_write.eligibility_at_write,
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
        query += " ORDER BY fired_at DESC, id DESC LIMIT :limit OFFSET :offset"
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

    @staticmethod
    def _reaper_evidence_exclusion_sql(dialect: str) -> str:
        """A predicate excluding rows whose terminal evidence the reaper wrote.

        The reaper tags its rows with a ``{"kind": "invocation"}`` entry; a run's own terminal
        writers do not. The entry must be an object member of the evidence array, so the walk is
        guarded on both the array and each element: an object held as the whole value, or an array
        of strings, is not the reaper's entry and must not exclude the row. The JSON functions are
        guarded with validity and type checks so a legacy non-JSON value, or a value of the wrong
        shape, cannot abort or misread the whole metric query.
        """
        if dialect == "sqlite":
            # json_each over an object walks its members and over an array its elements, so the
            # column itself has to be an array before any element is read; a string element
            # unquotes into ev.value for json_extract to parse, so ev.type must be 'object'.
            return (
                "NOT EXISTS (SELECT 1 FROM json_each("
                "CASE WHEN json_valid(status_evidence_refs) "
                "AND json_type(status_evidence_refs) = 'array' "
                "THEN status_evidence_refs ELSE '[]' END"
                ") AS ev WHERE ev.type = 'object' "
                "AND json_extract(ev.value, '$.kind') = 'invocation')"
            )
        # PostgreSQL's typed json column cannot hold text that is not JSON (the driver rejects it
        # on write), so the cast below cannot raise; the shape checks carry the same weight as on
        # SQLite, and ``->>`` reads a member -- a string element has no member to read.
        return (
            "NOT EXISTS (SELECT 1 FROM jsonb_array_elements("
            "CASE WHEN status_evidence_refs IS NULL "
            "OR jsonb_typeof(status_evidence_refs::jsonb) <> 'array' THEN '[]'::jsonb "
            "ELSE status_evidence_refs::jsonb END) AS ev "
            "WHERE jsonb_typeof(ev) = 'object' AND ev ->> 'kind' = 'invocation')"
        )

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
            # Only an end the run itself measured counts. A cancellation's ended_at records when
            # the cancellation was observed, so a cancelled row's age can be a delayed cleanup
            # rather than anything that ran -- dropped by status. A row the lifecycle reaper ends
            # is the same kind of outside observation, and its timed_out status and
            # run.timed_out.deadline reason code are shared with a run that hit its own deadline,
            # so nothing on those two columns tells the two apart. What does separate them is the
            # reaper's {"kind": "invocation"} entry in status_evidence_refs, which no run-side
            # invocation terminal writer adds; that column is read here in the same query.
            async with self._read() as conn:
                rows = (
                    (
                        await conn.execute(
                            text(
                                "SELECT (ended_at - started_at) AS latency_sec FROM invocations "  # noqa: S608
                                "WHERE started_at >= :window_start AND ended_at IS NOT NULL "
                                "AND ended_at >= started_at AND status != 'cancelled' "
                                f"AND {self._reaper_evidence_exclusion_sql(self.dialect)}"
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
        art_id = short_id(12)
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
        event_id = short_id(12)
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

    @_operation("create_branch")
    async def create_branch(self, branch: dict[str, Any]) -> None:
        values = {
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
        }
        if self._compat is not None:
            # The session, progression and system message a branch links are rows this run
            # created, so they share its owner.
            self._require_created("sessions", values["session_id"], "session")
            self._require_created("progressions", values["progression_id"], "progression")
            if values["system_msg_id"] is not None:
                self._require_created("messages", values["system_msg_id"], "system message")
            async with self._tx() as conn:
                await self._insert_new_row(
                    conn, "branches", values, json_columns=("node_metadata",)
                )
            return
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO branches (id, created_at, node_metadata, "user", name,
                       session_id, progression_id, system_msg_id, model, provider, agent_name)
                       VALUES (:id, :created_at, :node_metadata, :user, :name,
                               :session_id, :progression_id, :system_msg_id, :model, :provider, :agent_name)
                       ON CONFLICT (id) DO NOTHING"""
                ).bindparams(bindparam("node_metadata", type_=JSON)),
                values,
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

    @_operation("update_branch")
    async def update_branch(self, branch_id: str, **fields: Any) -> None:
        """Update a branch row.

        In compatibility mode the branch must be one this run created and still carry the run's
        owner, and the UPDATE carries the owner predicate, as the other branch writes do
        (ADR-0128 D5), so a run never rewrites a branch another run created.
        """
        _validate_columns(fields, _BRANCH_COLUMNS)
        if not fields:
            return
        if self._compat is not None:
            self._require_created("branches", branch_id, "branch")
        async with self._tx() as conn:
            if self._compat is not None:
                await self._require_owned(conn, "branches", branch_id, "branch")
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
        clause, owner = self._owner_predicate("branches")
        params = {**fields, **owner}
        params["_id"] = branch_id
        stmt = text(f"UPDATE branches SET {', '.join(sets_parts)} WHERE id = :_id{clause}")  # noqa: S608
        if bind_params:
            stmt = stmt.bindparams(*bind_params)
        await conn.execute(stmt, params)

    @_operation("finalize_branch")
    async def finalize_branch(
        self, branch_id: str, *, status: str, ended_at: float | None = None
    ) -> bool:
        """Guarded terminal write for a branch row: *status* terminal, row still pre-terminal.

        In compatibility mode the branch must be one this run created and still carry the run's
        owner, and the UPDATE carries the owner predicate, as the session writes do (ADR-0128 D5).
        """
        if status not in _BRANCH_TERMINAL_STATUSES:
            return False
        if self._compat is not None:
            self._require_created("branches", branch_id, "branch")
        clause, owner = self._owner_predicate("branches")
        async with self._tx() as conn:
            if self._compat is not None:
                await self._require_owned(conn, "branches", branch_id, "branch")
            result = await conn.execute(
                text(
                    "UPDATE branches SET status = :status, ended_at = :ended_at "  # noqa: S608
                    "WHERE id = :id AND (status IS NULL OR status = 'running')" + clause
                ),
                {
                    "status": status,
                    "ended_at": ended_at if ended_at is not None else time.time(),
                    "id": branch_id,
                    **owner,
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

    @_operation("insert_session_signal")
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

        In compatibility mode the session must be one this run created and still carry its owner,
        and the row is written with the owner.
        """
        if self._compat is not None:
            self._require_created("sessions", session_id, "session")
        sig_id = uuid.uuid4().hex
        owned = self._owner_values("session_signals")
        owner_columns = "".join(f", {_quote_identifier(name)}" for name in owned)
        owner_params = "".join(f", :_owner{i}" for i in range(len(owned)))
        async with self._tx() as conn:
            if self._compat is not None:
                await self._require_owned(conn, "sessions", session_id, "session")
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
                    "INSERT INTO session_signals "  # noqa: S608 -- the owner column is quoted
                    f"(id, session_id, seq, kind, op_id, ts, payload{owner_columns}) "
                    f"VALUES (:id, :sid, :seq, :kind, :op_id, :ts, :payload{owner_params})"
                ).bindparams(bindparam("payload", type_=JSON)),
                {
                    "id": sig_id,
                    "sid": session_id,
                    "seq": seq,
                    "kind": kind,
                    "op_id": op_id,
                    "ts": ts,
                    "payload": payload,
                    **{f"_owner{i}": value for i, value in enumerate(owned.values())},
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
        require_live_flow_poller: bool = False,
    ) -> str | None:
        """Queue a control verb for *session_id*; returns its id, or None if it was not admitted.

        With *require_live_flow_poller*, a flow/play session must also declare
        a live DAG poller (``node_metadata.flow_control_poller`` of ``pending``
        or ``active``) in this same statement. The operator admission paths ask
        for it: they read that marker to decide, and the run can lose its
        poller between that read and this write -- the teardown publishes the
        stop before it stops applying controls. Re-checking it here is what
        stops a control being queued against a consumer that is already gone,
        the same way the running condition stops one being queued against a
        terminal run. Left off by default, because a direct caller has already
        decided who its consumer is.
        """
        control_id = uuid.uuid4().hex
        # Every piece of the statement is a fixed literal chosen by the dialect, whether a project
        # is bound and whether the poller is required; no caller value reaches the text, and the
        # ownership and delivery predicates are visible in the SQL that actually runs. :project
        # stays a bound parameter. Only flow and play rows carry the poller marker, so every other
        # kind keeps the status-only condition.
        if not require_live_flow_poller:
            delivery = ""
        elif self.dialect == "sqlite":
            delivery = (
                " AND (COALESCE(invocation_kind, '') NOT IN ('flow', 'play') OR COALESCE("
                "CASE WHEN json_valid(node_metadata) THEN "
                "json_extract(node_metadata, '$.flow_control_poller') END, '') "
                "IN ('pending', 'active'))"
            )
        else:
            delivery = (
                " AND (COALESCE(invocation_kind, '') NOT IN ('flow', 'play') OR COALESCE("
                "node_metadata::jsonb->>'flow_control_poller', '') IN ('pending', 'active'))"
            )
        owner = " AND project = :project" if project is not None else ""
        if self.dialect == "sqlite":
            admit_source = (
                "WHERE EXISTS (SELECT 1 FROM sessions WHERE id = :sid AND status = 'running'"  # noqa: S608
                f"{owner}{delivery})"
            )
        else:
            admit_source = (
                "FROM (SELECT 1 FROM sessions WHERE id = :sid AND status = 'running'"  # noqa: S608
                f"{owner}{delivery} FOR UPDATE) _admitted"
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
                except (ValueError, TypeError, RecursionError) as exc:  # digit limit, depth guard
                    # Text past the parser's depth is kept only in trigger_context, which its
                    # projection reads as text. On Python 3.10 and 3.11 that guard's error also
                    # fires for a caller near the recursion limit, so every other column raises
                    # it, as before; a bare RecursionError is raised for every column.
                    if isinstance(exc, RecursionError) and (
                        key != "trigger_context" or "while decoding a JSON" not in str(exc)
                    ):
                        raise
        return d


# Shared singleton accessor: one open StateDB per DB URL, reused across hook firings, which avoids
# the per-firing connect and schema-check cost. Keyed by normalized URL so tests redirecting
# DEFAULT_DB_PATH get their own isolated instance, and a compatibility handle by its profile
# and owner too, so a native and a compatibility handle on one file are never the same object.

# A native or read-only handle is keyed by its url alone, as it always was; a compatibility
# handle by (url, mode, profile, owner), so it is never the shared handle of the same file.
_SHARED: dict[str | tuple[str, str, CompatibilityProfile | None, str | None], StateDB] = {}
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
# The event loop whose call holds or waits on _SHARED_OPEN_LOCK, and how many do: the lock wakes a
# waiter only on the loop that holds it, so a call from another loop is refused rather than left
# waiting for a wake that never comes.
_SHARED_LOCK_GUARD = threading.Lock()
_SHARED_LOCK_LOOP: asyncio.AbstractEventLoop | None = None
_SHARED_LOCK_USERS = 0
_SHARED_LOCK_OTHER_LOOP = (
    "the shared state registry is being opened, registered or closed on another event loop; call "
    "get_shared_db(), register_shared_db() and close_shared_db() on that loop"
)


@asynccontextmanager
async def _holding_shared_lock():
    """Hold the shared-registry lock, refusing a call made from another event loop.

    The same guard StateDB's own open lock uses: the lock belongs to the loop that holds or waits
    on it, and a call from any other loop would wait for a wake only that loop's release can send,
    so such a call is refused at once with a ``RuntimeError`` naming the other loop rather than
    left to time out with the registry emptied. The user count rises before the wait and falls on
    every way out, cancellation included.
    """
    global _SHARED_LOCK_LOOP, _SHARED_LOCK_USERS, _SHARED_OPEN_LOCK  # noqa: PLW0603

    if _SHARED_OPEN_LOCK is None:
        _SHARED_OPEN_LOCK = Lock()
    lock = _SHARED_OPEN_LOCK
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # another async library, where the engine cannot run anyway
        loop = None
    with _SHARED_LOCK_GUARD:
        if _SHARED_LOCK_USERS and _SHARED_LOCK_LOOP is not loop:
            raise RuntimeError(_SHARED_LOCK_OTHER_LOOP)
        _SHARED_LOCK_LOOP = loop
        _SHARED_LOCK_USERS += 1
    try:
        async with lock:
            yield
    finally:
        with _SHARED_LOCK_GUARD:
            _SHARED_LOCK_USERS -= 1
            if not _SHARED_LOCK_USERS:
                _SHARED_LOCK_LOOP = None


def _shared_key(db: StateDB) -> str | tuple[str, str, CompatibilityProfile | None, str | None]:
    if db.mode != "compatibility":
        return db.url
    owner = None if db._compat is None else db._compat.owner
    return (db.url, db.mode, db.compatibility, owner)


# Every compatibility handle open in this process, registered as the shared handle or not. While
# one is open on a store, a native open of that store refuses, and so does the shared lookup unless
# it can hand out the compatibility handle itself.
_OPEN_COMPAT: weakref.WeakSet[StateDB] = weakref.WeakSet()
# Every writable native handle open on a SQLite file in this process. While one is open on a
# store, compatibility admission of that store refuses.
_OPEN_NATIVE: weakref.WeakSet[StateDB] = weakref.WeakSet()


def _file_identity(path: str) -> tuple[int, int] | None:
    """The (device, inode) of the file at *path*, or None when there is no such file."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def _held_files() -> tuple:
    """The (device, inode) and the handle of every file this process holds in compatibility mode,
    read on the calling thread so a worker thread never iterates the registry."""
    return tuple(
        (held._held_file, held)
        for held in list(_OPEN_COMPAT)
        if held._engine is not None and held._held_file is not None
    )


def _read_only_uri(filename: str, uri: bool, *, immutable: bool) -> str | None:
    """A URI opening the file SQLAlchemy's SQLite dialect opens as *filename*, read-only:
    ``immutable`` reads the file alone, otherwise its log is read with it through the log's
    existing ``-shm`` index, which is never written. None for an in-memory or temporary
    database."""
    if uri:
        # SQLite ends the file name at a `#`, and reads nothing after it.
        location, _, query = filename.partition("#")[0].partition("?")
        parameters = parse_qsl(query, keep_blank_values=True)
        if location in ("file:", "file::memory:") or ("mode", "memory") in parameters:
            return None
        parameters = [
            (k, v)
            for k, v in parameters
            if k not in ("mode", "immutable", "nolock", "readonly_shm")
        ]
    else:
        if filename in ("", ":memory:"):
            return None
        try:
            location = Path(filename).as_uri()
        except ValueError:
            return None
        parameters = []
    if immutable:
        parameters.append(("immutable", "1"))
    else:
        parameters += [("mode", "ro"), ("readonly_shm", "1")]
    return f"{location}?{urlencode(parameters)}"


# A store whose file opened but whose `lineage` row could not be read.
_UNREAD = object()
# A copy of the file and its journal or log that the store committed or checkpointed under, so the
# two may not be of one state; the read is taken again, up to _COPY_ATTEMPTS times.
_MOVED = object()
_COPY_ATTEMPTS = 3
# The first eight bytes of a rollback journal's header. A commit deletes, truncates or zeroes it.
_JOURNAL_HEADER = bytes.fromhex("d9d505f920a163d7")
# The first four bytes of a write-ahead log's header, in its two checksum byte orders.
_LOG_MAGIC = (bytes.fromhex("377f0682"), bytes.fromhex("377f0683"))


def _no_sidecar(exc: OSError) -> bool:
    """Whether *exc* says no journal or log can be beside the store: it is not there, or the
    store's name leaves no room for the suffix, so SQLite cannot create one either."""
    import errno

    return exc.errno in (errno.ENOENT, errno.ENAMETOOLONG)


def _journal_has_a_header(path: str) -> bool:
    """Whether the rollback journal beside *path* begins with a journal header: an interrupted
    transaction's does, and so does a transaction's still in progress. A journal that is there and
    cannot be opened (a directory, or one this user cannot read) may hold one, so it counts as one
    and its copy decides."""
    try:
        with open(f"{path}-journal", "rb") as journal:
            return journal.read(len(_JOURNAL_HEADER)) == _JOURNAL_HEADER
    except OSError as exc:
        return not _no_sidecar(exc)


def _log_has_frames(path: str) -> bool:
    """Whether a write-ahead log beside *path* has anything in it; one that is there and cannot be
    measured counts as having frames, so its copy decides."""
    try:
        return os.path.getsize(f"{path}-wal") > 0
    except OSError as exc:
        return not _no_sidecar(exc)


def _read_lineage_from_a_copy(path: str, suffix: str) -> Any:
    """The ``_StoreRows`` of a private copy of the file at *path* and of its ``-journal`` or
    ``-wal``, read on a writable connection, so SQLite rolls the copy's journal back or reads the
    copy's log and never touches the originals. The copy is made in a new directory under the
    system's temporary directory, which only this user can read, and removed before this returns;
    a process killed while it copies leaves the copy there. ``_MOVED`` when the store committed or
    checkpointed while it was copied, or its journal or log could not be opened or was gone by the
    time it was copied (a commit or a closing checkpoint removes it), so the caller reads the store
    again from the start; ``_UNREAD`` when the copy or its read fails otherwise."""
    try:
        with tempfile.TemporaryDirectory(prefix="lionagi-lineage-") as scratch:
            copy = os.path.join(scratch, "store.db")
            before = _copy_state(path, suffix)
            if before is None:
                return _MOVED
            _copy_store_file(path, copy)
            try:
                shutil.copyfile(f"{path}{suffix}", f"{copy}{suffix}")
            except FileNotFoundError:
                return _MOVED
            if _copy_state(path, suffix) != before:
                return _MOVED
            read = _read_lineage(Path(copy).as_uri())
    except (OSError, sqlite3.Error):
        return _UNREAD
    return _UNREAD if read is None else read[1]


def _copy_state(path: str, suffix: str) -> tuple | None:
    """What changes when the store commits or checkpoints between the copy of its file and the
    copy of its journal or log: the file's inode, the journal's or log's inode, and the log's header
    (a checkpoint that resets or truncates the log rewrites it) or the journal header's magic and
    nonce (a commit removes, truncates or zeroes the journal, and a new transaction writes a new
    header). None when the journal or log cannot be read, and when it does not begin with a journal
    header or a log header: a truncated or zeroed one has no nonce or salt, so two reads of it
    would match across a transaction that began and ended between them."""
    side = f"{path}{suffix}"
    try:
        with open(side, "rb") as handle:
            header = handle.read(32)
            inodes = (os.stat(path).st_ino, os.fstat(handle.fileno()).st_ino)
    except OSError:
        return None
    if suffix == "-wal":
        return (inodes, header) if header[:4] in _LOG_MAGIC else None
    return (inodes, header[:8] + header[12:16]) if header[:8] == _JOURNAL_HEADER else None


# Descriptors this process opened on a store's file to copy it, one per file, never closed: closing
# any descriptor of a file drops every POSIX lock this process holds on that file, which another
# connection here may hold, so each stays open until the process exits, as SQLite keeps its own
# descriptor of a file open while its connections here hold locks on it. Two costs follow from
# that: each distinct store copied keeps one descriptor open for the life of the process, and a
# store deleted after it was copied keeps its disk space allocated until the process exits. The
# opens are serialised by `_COPY_DESCRIPTORS_LOCK`, so an open never races another; a descriptor
# that finds one already filed stays open and unfiled, which needs the file replaced between the
# `stat` and the `open`, so the open costs a second descriptor no entry can find. The descriptors
# count against the process's open-file limit, so a long run that copies very many distinct stores
# spends one of that limit's slots per store, and an `EMFILE` reached there makes
# `_read_lineage_from_a_copy` return `_UNREAD`, so the open is refused with `UnreadableStoreError`
# before any writable connection opens the store.
_COPY_DESCRIPTORS: dict[tuple[int, int], int] = {}
_COPY_DESCRIPTORS_LOCK = threading.Lock()


def _copy_store_file(path: str, copy: str) -> None:
    """Copy the file at *path* to *copy* byte for byte, whatever its pages hold, through a
    descriptor of the file this process keeps open, so the copy closes no descriptor of it. On
    Windows a lock belongs to the handle that took it, and the file is copied as usual."""
    if os.name == "nt":
        shutil.copyfile(path, copy)
        return
    descriptor = _copy_descriptor(path)
    with open(copy, "wb") as target:
        offset = 0
        while chunk := os.pread(descriptor, 1 << 20, offset):
            target.write(chunk)
            offset += len(chunk)


def _copy_descriptor(path: str) -> int:
    """A descriptor this process keeps open on the file at *path*, opening one if none is filed.

    The descriptor is never closed, since closing any descriptor of a file drops every POSIX lock
    this process holds on it. So each distinct store copied keeps one descriptor open for the life
    of the process, and a store deleted after it was copied keeps its disk space allocated until
    the process exits. The opens are serialised by ``_COPY_DESCRIPTORS_LOCK``, so the one that
    stays open and unfiled needs the file replaced between the ``stat`` and the ``open``.
    """
    with _COPY_DESCRIPTORS_LOCK:
        found = os.stat(path)
        descriptor = _COPY_DESCRIPTORS.get((found.st_dev, found.st_ino))
        if descriptor is None:
            descriptor = os.open(path, os.O_RDONLY)
            opened = os.fstat(descriptor)
            # A file replaced between the stat and the open is filed under the file it opened;
            # a descriptor that finds one already filed there stays open, as every one here does,
            # and cannot be closed by this process without dropping the locks it holds on that file.
            descriptor = _COPY_DESCRIPTORS.setdefault((opened.st_dev, opened.st_ino), descriptor)
        return descriptor


def _read_lineage(uri: str) -> tuple[str, Any] | None:
    """The file SQLite opened for *uri* and that file's ``_StoreRows``, its ``lineage`` row among
    them (``_UNREAD`` when the file opened but the rows could not be read), or None when the file
    could not be opened."""
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error:
        return None
    try:
        # A file an interrupted transaction left beside a hot journal can list a table whose pages
        # it does not hold yet, and SQLite refuses to load such a schema; this skips that entry
        # rather than the whole read. On a read-only connection it still writes nothing.
        conn.execute("PRAGMA writable_schema = ON")
        rows = conn.execute("PRAGMA database_list").fetchall()
        opened = next((row[2] for row in rows if row[1] == "main"), "")
    except sqlite3.Error:
        conn.close()
        return None
    try:
        return opened, _sqlite_store_rows(lambda statement: conn.execute(statement).fetchall())
    except sqlite3.Error:
        return opened, _UNREAD
    finally:
        conn.close()


def _open_in_compatibility_mode(url: str) -> StateDB | None:
    """A compatibility handle this process holds open on *url*, registered or not, else None."""
    for db in list(_OPEN_COMPAT):
        if db.url == url and db._engine is not None:
            return db
    return None


def _held_in_compatibility_mode(url: str) -> StateDB | None:
    """The compatibility handle registered for *url*, or None when no such handle is registered.

    Each registered handle differs in its profile or its owner, so it writes under rules or for
    an owner of its own; a caller that names only the store cannot say which it means, and when
    more than one is registered that lookup is refused.
    """
    held = [db for key, db in _SHARED.items() if isinstance(key, tuple) and key[0] == url]
    if len(held) > 1:
        raise held[0]._compat.refusal(
            f"{len(held)} compatibility handles are registered for this store, each with its own "
            "profile or owner, and a caller that names only the store does not say which one "
            "it writes through",
            operation="open",
        )
    return held[0] if held else None


async def get_shared_db(path: str | Path | None = None) -> StateDB:
    """Return the process-wide open StateDB for *path* (default: DEFAULT_DB_PATH).

    A store this process holds in compatibility mode is never handed out natively here: a native
    open applies this package's schema to a store another application manages (ADR-0128 D1). The
    registered compatibility handle is returned instead, before any native handle registered for
    the same store, so what the caller writes meets the profile (Appendix A.2). A store with more
    than one compatibility handle registered is refused, and so is one this process holds open in
    compatibility mode without registering it, since the caller cannot be given that handle.
    """
    # Resolve the key through StateDB's own cascade so a monkeypatched DEFAULT_DB_PATH is honored;
    # normalize_state_db_url(None) would bypass it to the real home db.
    url = StateDB(path).url
    key = url
    held = _held_in_compatibility_mode(url)
    if held is not None:
        return held
    _refuse_native_beside_compatibility(url)
    if key in _SHARED:
        return _SHARED[key]
    gen = _SHARED_CLOSE_GEN
    async with _holding_shared_lock():
        # A close_shared_db() swept the registry while we waited on this lock;
        # refuse to resurrect the singleton rather than leak a fresh worker.
        if _SHARED_CLOSE_GEN != gen:
            raise RuntimeError(_SHARED_TEARDOWN_RACE)
        # Double-checked: another coroutine may have opened it while we waited, or registered a
        # compatibility handle for it.
        held = _held_in_compatibility_mode(url)
        if held is not None:
            return held
        _refuse_native_beside_compatibility(url)
        if key not in _SHARED:
            db = StateDB(url=url)
            await db.open()
            _SHARED[key] = db
    return _SHARED[key]


def _refuse_native_beside_compatibility(url: str) -> None:
    held = _open_in_compatibility_mode(url)
    if held is not None:
        raise held._compat.refusal(
            "this process holds the store open in compatibility mode without sharing that "
            "handle, and a native handle would write to it outside the profile",
            "open",
        )


async def register_shared_db(db: StateDB) -> None:
    """Adopt a caller-owned StateDB as the shared instance, closing any prior one for its url.

    A native writer is refused for a store this process holds open in compatibility mode.

    The handle being displaced is closed before the entry is replaced. Any error that close raises
    reaches the caller, not just a refusal, and the registry keeps the handle that is still open
    rather than filing one with no entry to reach it by. A caller that registers for a run takes
    that error as a setup failure: ``setup_agent_persist`` disables persistence for the run,
    records it degraded and leaves it running, and only a ``StateCompatibilityRefused`` fails the
    run with the refusal as its reason. Orchestration admission writes through its own handle and
    registers none.
    """
    gen = _SHARED_CLOSE_GEN
    async with _holding_shared_lock():
        if _SHARED_CLOSE_GEN != gen:
            raise RuntimeError(_SHARED_TEARDOWN_RACE)
        if db.mode == "native":
            _refuse_native_beside_compatibility(db.url)
        key = _shared_key(db)
        existing = _SHARED.get(key)
        if existing is not None and existing is not db:
            await existing.close()
        _SHARED[key] = db


def unregister_shared_db(db: StateDB) -> None:
    """Drop *db* from the shared registry iff it is the registered instance."""
    key = _shared_key(db)
    if _SHARED.get(key) is db:
        del _SHARED[key]


def _describe_a_shared_handle(db: StateDB) -> str:
    """A shared handle for a message: the store it opens, masked, and the mode it was opened in."""
    return f"{mask_db_url(db.url)} ({db.mode})"


async def close_shared_db() -> None:
    """Close and forget every shared StateDB; callers must quiesce get_shared_db() first.

    Every handle the sweep can close is closed and forgotten, and one whose close raises any error
    is kept in the registry and raised together with the others once the sweep is done: reported as
    closed, it would keep its engine with nothing left to reach it by.
    """
    global _SHARED_OPEN_LOCK, _SHARED_CLOSE_GEN, _SHARED_LOCK_USERS  # noqa: PLW0603

    # Hold the open lock so an in-flight get_shared_db()/register_shared_db() cannot repopulate
    # _SHARED after the sweep; bump the generation and null the lock last, so a waiter that raced
    # this close refuses to resurrect it. Every sweep takes the lock, the one after a refused close
    # included: that close kept its handle registered and left the lock unset, and a sweep that ran
    # without a lock over that entry would let a lookup register a second handle for the store,
    # which the sweep would then return over or the re-file below would orphan. The lock is unset
    # only by the last caller waiting on it: a sweep queued behind this one captured this lock
    # while this one held it, so nulling it here would send that queued sweep's own lookup to a
    # fresh lock, running beside the queued sweep instead of behind it.
    async with _holding_shared_lock():
        refused = await _sweep_shared_handles()
        _SHARED_CLOSE_GEN += 1
        if _SHARED_LOCK_USERS == 1:
            _SHARED_OPEN_LOCK = None
    _raise_refused_closes(refused)


async def _sweep_shared_handles() -> list[tuple[StateDB, Exception]]:
    """Close every registered handle, returning the ones whose close raised, with the error.

    The registry is emptied before the first close, so a get_shared_db() racing the sweep meets the
    lock rather than a handle being closed, and a handle that did not close is filed again under
    its key: one a close from another event loop refuses, or whose close is cancelled, would
    otherwise stay open with no entry a later lookup or sweep could reach it by."""
    remaining = list(_SHARED.items())
    _SHARED.clear()
    refused: list[tuple[StateDB, Exception]] = []
    while remaining:
        key, db = remaining.pop(0)
        try:
            await db.close()
        except Exception as exc:  # noqa: BLE001 -- collected and raised after the sweep
            _SHARED[key] = db
            refused.append((db, exc))
        except BaseException:
            # The close was cancelled: this handle and every one still to close stay open, so they
            # keep their entries rather than being left unreachable.
            _SHARED[key] = db
            for later_key, later in remaining:
                _SHARED[later_key] = later
            raise
    return refused


def _raise_refused_closes(refused: list[tuple[StateDB, Exception]]) -> None:
    """Raise once for every handle a sweep could not close, naming each one and why."""
    if not refused:
        return
    named = "; ".join(f"{_describe_a_shared_handle(db)}: {exc}" for db, exc in refused)
    raise RuntimeError(
        f"{len(refused)} shared StateDB handle(s) could not be closed: {named}"
    ) from (refused[0][1])
