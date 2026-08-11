# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""SQLAlchemy transaction implementation of the guarded lifecycle transition
algorithm: one atomic read-check-write-history sequence shared by every
managed entity type's status transitions. See docs/internals/runtime.md for
the ``_transition()`` adapter-only kwargs contract.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol, get_args

from sqlalchemy import JSON, bindparam, text

from ..reasons import VALID_REASON_CODES
from . import LifecycleNotFoundError, LifecycleValidationError
from .callbacks import (
    DEFAULT_TERMINAL_CALLBACKS,
    EXECUTION_ENTITY_KINDS,
    Correlation,
    EntityRef,
    RunTerminalEnvelope,
    TerminalCallbackRegistry,
)
from .models import ActorType, InitialStateCommand, TransitionCommand, TransitionOutcome
from .policy import DEFAULT_REGISTRY, PolicyRegistry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection


class _TxProvider(Protocol):
    """Structural requirement satisfied by ``lionagi.state.db.StateDB``
    without importing it here (would be circular)."""

    dialect: str

    def _tx(self) -> Any: ...


def _json_list(evidence_refs: tuple) -> list:
    return [dict(e) for e in evidence_refs]


def _correlation_for(entity_type: str, entity_id: str) -> Correlation:
    if entity_type == "session":
        return Correlation(session_id=entity_id)
    if entity_type == "invocation":
        return Correlation(invocation_id=entity_id)
    if entity_type == "schedule_run":
        return Correlation(schedule_run_id=entity_id)
    return Correlation()


def _build_terminal_envelope(
    command: TransitionCommand,
    *,
    previous_status: str | None,
    transition_id: str,
    occurred_at: float,
) -> RunTerminalEnvelope:
    return RunTerminalEnvelope(
        event_id=transition_id,
        entity=EntityRef(kind=command.entity_type, id=command.entity_id),
        previous_status=previous_status,
        terminal_status=command.to_status,
        reason_code=command.reason.code,
        occurred_at=occurred_at,
        correlation=_correlation_for(command.entity_type, command.entity_id),
    )


class SQLAlchemyLifecycleService:
    """The LifecycleService implementation, bound to one StateDB backend."""

    def __init__(
        self,
        db: _TxProvider,
        registry: PolicyRegistry = DEFAULT_REGISTRY,
        *,
        terminal_callbacks: TerminalCallbackRegistry | None = None,
    ) -> None:
        self._db = db
        self._registry = registry
        self._terminal_callbacks = (
            terminal_callbacks if terminal_callbacks is not None else DEFAULT_TERMINAL_CALLBACKS
        )

    async def initialize_in_transaction(
        self,
        connection: AsyncConnection,
        command: InitialStateCommand,
    ) -> str:
        policy = self._registry.get(command.entity_type)
        if not command.entity_id:
            raise LifecycleValidationError("InitialStateCommand.entity_id must be non-empty")
        if not command.actor.id:
            raise LifecycleValidationError("InitialStateCommand.actor.id must be non-empty")
        if not command.reason.code:
            raise LifecycleValidationError("InitialStateCommand.reason.code must be non-empty")
        if command.status not in policy.initial_statuses:
            raise LifecycleValidationError(
                f"initialize_in_transaction(): status {command.status!r} is not a declared "
                f"initial status for entity_type {command.entity_type!r}; allowed: "
                f"{sorted(policy.initial_statuses)}"
            )

        row = (
            (
                await connection.execute(
                    text(f"SELECT id FROM {policy.table} WHERE id = :id"),  # noqa: S608
                    {"id": command.entity_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise LifecycleNotFoundError(
                f"{command.entity_type} {command.entity_id!r} not found (table={policy.table}); "
                "initialize_in_transaction() requires the entity row to already exist in the "
                "same transaction"
            )

        existing = (
            (
                await connection.execute(
                    text(
                        "SELECT 1 FROM status_transitions WHERE entity_type = :et "
                        "AND entity_id = :eid AND previous_status IS NULL LIMIT 1"
                    ),
                    {"et": command.entity_type, "eid": command.entity_id},
                )
            )
            .mappings()
            .first()
        )
        if existing is not None:
            raise LifecycleValidationError(
                f"initialize_in_transaction(): {command.entity_type} {command.entity_id!r} "
                "already has an initial (previous_status=NULL) history row"
            )

        transition_id = uuid.uuid4().hex
        now = time.time()
        await connection.execute(
            text(
                "INSERT INTO status_transitions "
                "(id, entity_type, entity_id, previous_status, status, "
                " reason_code, reason_summary, evidence_refs, "
                " source, actor, created_at, metadata) "
                "VALUES (:id, :entity_type, :entity_id, NULL, :status, "
                " :reason_code, :reason_summary, :evidence_refs, "
                " :source, :actor, :created_at, :metadata)"
            ).bindparams(
                bindparam("evidence_refs", type_=JSON),
                bindparam("metadata", type_=JSON),
            ),
            {
                "id": transition_id,
                "entity_type": command.entity_type,
                "entity_id": command.entity_id,
                "status": command.status,
                "reason_code": command.reason.code,
                "reason_summary": command.reason.summary,
                "evidence_refs": _json_list(command.reason.evidence_refs),
                "source": command.actor.type,
                "actor": command.actor.id,
                "created_at": now,
                "metadata": dict(command.reason.metadata),
            },
        )
        return transition_id

    async def transition(self, command: TransitionCommand) -> TransitionOutcome:
        if command.reason.code not in VALID_REASON_CODES:
            raise LifecycleValidationError(
                f"invalid reason_code: {command.reason.code!r}; must be one of "
                "the codes registered in lionagi.state.reasons.VALID_REASON_CODES"
            )
        policy = self._registry.get(command.entity_type)
        prefix = command.reason.code.split(".", 1)[0]
        if prefix not in policy.reason_prefixes:
            raise LifecycleValidationError(
                f"reason_code {command.reason.code!r} does not belong to entity_type "
                f"{command.entity_type!r}; expected a prefix in "
                f"{sorted(policy.reason_prefixes)}"
            )
        if not command.actor.id:
            raise LifecycleValidationError("TransitionCommand.actor.id must be non-empty")
        if command.actor.type not in get_args(ActorType):
            raise LifecycleValidationError(
                f"invalid actor type: {command.actor.type!r}; must be one of "
                f"{sorted(get_args(ActorType))}"
            )
        return await self._transition(
            command,
            extra_guard=None,
            enforce_edges=True,
            undeclared_edge_mode="reject",
            write_reason_columns=policy.reason_columns,
        )

    async def _transition(
        self,
        command: TransitionCommand,
        *,
        extra_guard: Mapping[str, Any] | None = None,
        enforce_edges: bool = False,
        undeclared_edge_mode: str = "raise",
        raise_on_unguarded_conflict: bool = False,
        write_reason_columns: bool = True,
    ) -> TransitionOutcome:
        policy = self._registry.get(command.entity_type)

        if not command.entity_id:
            raise LifecycleValidationError("TransitionCommand.entity_id must be non-empty")
        if not command.actor.type:
            raise LifecycleValidationError("TransitionCommand.actor.type must be non-empty")
        if not command.reason.code:
            raise LifecycleValidationError("TransitionCommand.reason.code must be non-empty")
        if not command.to_status:
            raise LifecycleValidationError("TransitionCommand.to_status must be non-empty")
        if command.to_status not in policy.statuses:
            raise LifecycleValidationError(
                f"transition(): to_status {command.to_status!r} is not in the declared "
                f"vocabulary for entity_type {command.entity_type!r}: {sorted(policy.statuses)}"
            )
        unknown_patch = set(command.patch) - policy.patch_fields
        if unknown_patch:
            raise LifecycleValidationError(
                f"transition(): patch field(s) {sorted(unknown_patch)} are not in the "
                f"declared patch_fields allowlist for entity_type {command.entity_type!r} "
                f"(allowed: {sorted(policy.patch_fields)})"
            )
        if command.override is not None:
            if not command.override.actor or not command.override.justification:
                raise LifecycleValidationError(
                    "transition(): OverrideRecord requires both a non-empty actor and justification"
                )
        extra_guard = extra_guard or {}

        now = time.time()

        # Every session's terminal write (any caller, any patch) derives
        # ended_at/duration_ms from the same row read here, instead of each
        # writer computing it independently (or not at all).
        needs_duration_basis = (
            command.entity_type == "session" and command.to_status in policy.terminal_statuses
        )

        async with self._db._tx() as conn:
            guard_cols = list(extra_guard)
            # A caller may guard one of the duration-basis columns. Keep the
            # SELECT unique, but never let that guard disable the terminal-end
            # invariant itself.
            fetch_cols = list(dict.fromkeys(["status", "updated_at", *guard_cols]))
            if needs_duration_basis:
                fetch_cols.extend(
                    col for col in ("started_at", "ended_at") if col not in fetch_cols
                )
            select_cols = ", ".join(fetch_cols)
            sel = f"SELECT {select_cols} FROM {policy.table} WHERE id = :id"  # noqa: S608
            if self._db.dialect != "sqlite":
                sel += " FOR UPDATE"
            row = (await conn.execute(text(sel), {"id": command.entity_id})).mappings().first()

            if row is None:
                raise LifecycleNotFoundError(
                    f"{command.entity_type} {command.entity_id!r} not found (table={policy.table})"
                )
            previous_status = row["status"]

            if (
                command.expected_statuses is not None
                and previous_status not in command.expected_statuses
            ):
                return TransitionOutcome(
                    result="conflict",
                    previous_status=previous_status,
                    current_status=previous_status,
                    transition_id=None,
                )
            same_status = previous_status == command.to_status
            declared_edges = policy.edges.get(previous_status, ()) if enforce_edges else ()
            self_edge = next((e for e in declared_edges if e.to_status == command.to_status), None)

            override_admin_event = False
            rejected = False

            if self_edge is not None:
                if (
                    self_edge.actor_types is not None
                    and command.actor.type not in self_edge.actor_types
                ):
                    raise LifecycleValidationError(
                        f"transition(): actor type {command.actor.type!r} is not permitted for "
                        f"{command.entity_type!r} edge {previous_status!r} -> "
                        f"{command.to_status!r} (allowed: {sorted(self_edge.actor_types)})"
                    )
                missing_patch = self_edge.required_patch_fields - set(command.patch)
                if missing_patch:
                    raise LifecycleValidationError(
                        f"transition(): edge {previous_status!r} -> {command.to_status!r} for "
                        f"entity_type {command.entity_type!r} requires patch field(s) "
                        f"{sorted(missing_patch)}"
                    )
                if self_edge.required_guard_fields:
                    guarded_by_extra = self_edge.required_guard_fields <= set(extra_guard)
                    guarded_by_version = command.expected_version is not None
                    if not (guarded_by_extra or guarded_by_version):
                        raise LifecycleValidationError(
                            f"transition(): edge {previous_status!r} -> {command.to_status!r} for "
                            f"entity_type {command.entity_type!r} requires a race guard covering "
                            f"{sorted(self_edge.required_guard_fields)} (via expected_version or "
                            "an equivalent guard); none was supplied"
                        )
            elif same_status:
                if policy.same_status == "noop":
                    return TransitionOutcome(
                        result="applied",
                        previous_status=previous_status,
                        current_status=command.to_status,
                        transition_id=None,
                    )
                if policy.same_status == "reject":
                    rejected = True
            elif enforce_edges:
                if command.override is not None:
                    override_admin_event = True
                elif undeclared_edge_mode == "reject":
                    rejected = True
                else:
                    raise LifecycleValidationError(
                        f"transition(): {command.entity_type} {previous_status!r} -> "
                        f"{command.to_status!r} is not in the declared transition "
                        f"vocabulary (allowed targets: "
                        f"{sorted(e.to_status for e in declared_edges)})"
                    )
            elif previous_status in policy.terminal_statuses:
                if command.override is not None:
                    override_admin_event = True
                else:
                    rejected = True

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
                        "target_id": command.entity_id,
                        "details": {
                            "entity_type": command.entity_type,
                            "previous_status": previous_status,
                            "attempted_status": command.to_status,
                            "reason_code": command.reason.code,
                            "source": command.actor.type,
                        },
                        "actor": command.actor.id or command.actor.type,
                    },
                )
                return TransitionOutcome(
                    result="rejected",
                    previous_status=previous_status,
                    current_status=previous_status,
                    transition_id=None,
                )

            if (
                command.expected_version is not None
                and row["updated_at"] != command.expected_version
            ):
                return TransitionOutcome(
                    result="conflict",
                    previous_status=previous_status,
                    current_status=previous_status,
                    transition_id=None,
                )

            if override_admin_event:
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
                        "target_id": command.entity_id,
                        "details": {
                            "entity_type": command.entity_type,
                            "previous_status": previous_status,
                            "new_status": command.to_status,
                            "reason_code": command.reason.code,
                            "justification": command.override.justification,
                        },
                        "actor": command.override.actor,
                    },
                )

            for col, expected in extra_guard.items():
                if row[col] != expected:
                    return TransitionOutcome(
                        result="conflict",
                        previous_status=previous_status,
                        current_status=previous_status,
                        transition_id=None,
                    )

            if needs_duration_basis and not same_status:
                ended_at_value = command.patch.get("ended_at", row["ended_at"])
                if ended_at_value is None:
                    ended_at_value = now
                patch = dict(command.patch)
                patch.setdefault("ended_at", ended_at_value)
                if "duration_ms" not in patch:
                    started_at = row["started_at"]
                    if isinstance(started_at, int | float):
                        patch["duration_ms"] = max(0.0, (ended_at_value - started_at) * 1000)
                if patch != dict(command.patch):
                    command = replace(command, patch=patch)

            transition_id = await self._write(
                conn,
                policy.table,
                command,
                previous_status=previous_status,
                now=now,
                extra_guard=extra_guard,
                write_reason_columns=write_reason_columns,
            )
            if transition_id is None:
                guarded = (
                    command.expected_statuses is not None
                    or command.expected_version is not None
                    or bool(extra_guard)
                )
                if raise_on_unguarded_conflict and not guarded:
                    raise RuntimeError(
                        f"status CAS lost for {command.entity_type} "
                        f"{command.entity_id!r}: row changed under update_status"
                    )
                return TransitionOutcome(
                    result="conflict",
                    previous_status=previous_status,
                    current_status=previous_status,
                    transition_id=None,
                )

        if (
            transition_id is not None
            and previous_status != command.to_status
            and command.entity_type in EXECUTION_ENTITY_KINDS
            and command.to_status in policy.terminal_statuses
        ):
            envelope = _build_terminal_envelope(
                command,
                previous_status=previous_status,
                transition_id=transition_id,
                occurred_at=now,
            )
            await self._terminal_callbacks.emit(envelope)
        return TransitionOutcome(
            result="applied",
            previous_status=previous_status,
            current_status=command.to_status,
            transition_id=transition_id,
        )

    async def _write(
        self,
        conn: Any,
        table: str,
        command: TransitionCommand,
        *,
        previous_status: str | None,
        now: float,
        extra_guard: Mapping[str, Any],
        write_reason_columns: bool = True,
    ) -> str | None:
        """The guarded UPDATE and its status_transitions append, factored out
        so both the ordinary and override paths share it. Returns the new
        transition id, or ``None`` on a zero-row UPDATE (conflict).
        """
        set_clauses = ["status = :status", "updated_at = :now"]
        if write_reason_columns:
            set_clauses[1:1] = [
                "status_reason_code = :reason_code",
                "status_reason_summary = :reason_summary",
                "status_evidence_refs = :evidence_refs",
            ]
        where_clauses = [
            "id = :id",
            "(status = :previous_status OR (status IS NULL AND :previous_status IS NULL))",
        ]
        params: dict[str, Any] = {
            "status": command.to_status,
            "now": now,
            "id": command.entity_id,
            "previous_status": previous_status,
        }
        if write_reason_columns:
            params["reason_code"] = command.reason.code
            params["reason_summary"] = command.reason.summary
            params["evidence_refs"] = _json_list(command.reason.evidence_refs)
        if command.expected_version is not None:
            where_clauses.append("updated_at = :expected_version")
            params["expected_version"] = command.expected_version
        for i, (col, expected) in enumerate(extra_guard.items()):
            key = f"guard_{i}"
            where_clauses.append(f"{col} = :{key}")
            params[key] = expected
        for col, value in command.patch.items():
            key = f"patch_{col}"
            set_clauses.append(f"{col} = :{key}")
            params[key] = value

        update_sql = (
            f"UPDATE {table} SET {', '.join(set_clauses)} "  # noqa: S608
            f"WHERE {' AND '.join(where_clauses)}"
        )
        update_stmt = text(update_sql)
        if write_reason_columns:
            update_stmt = update_stmt.bindparams(bindparam("evidence_refs", type_=JSON))
        result = await conn.execute(update_stmt, params)
        if result.rowcount == 0:
            return None

        transition_id = uuid.uuid4().hex
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
                "id": transition_id,
                "entity_type": command.entity_type,
                "entity_id": command.entity_id,
                "previous_status": previous_status,
                "status": command.to_status,
                "reason_code": command.reason.code,
                "reason_summary": command.reason.summary,
                "evidence_refs": _json_list(command.reason.evidence_refs),
                "source": command.actor.type,
                "actor": command.actor.id,
                "created_at": now,
                "metadata": dict(command.reason.metadata),
            },
        )
        return transition_id
