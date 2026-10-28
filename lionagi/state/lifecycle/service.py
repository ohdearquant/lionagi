# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""SQLAlchemy transaction implementation of the guarded lifecycle transition
algorithm: one atomic read-check-write-history sequence shared by every
managed entity type's status transitions. See docs/internals/runtime.md for
the ``_transition()`` adapter-only kwargs contract.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol, get_args

from sqlalchemy import JSON, bindparam, text

from lionagi.ln import short_id

from ..compat import _quote
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
from .models import (
    ActorType,
    InitialStateCommand,
    ReasonRecord,
    TransitionCommand,
    TransitionOutcome,
)
from .policy import DEFAULT_REGISTRY, PolicyRegistry
from .schedules import ScheduleLifecycleAdapter

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection


# Only these fields supplied the dead-runner evidence. An omitted key means
# the snapshot lacked the key; unrelated metadata is intentionally not guarded.
_RUNNER_IDENTITY_FIELDS = frozenset(
    {"pid", "pid_create_time", "pid_host", "pid_boot_time", "process_identity_mode"}
)


def _same_runner_value(expected: Any, actual: Any) -> bool:
    """Compare JSON values without treating booleans as numeric identities."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is bool and type(actual) is bool and expected is actual
    if type(expected) in (int, float) and type(actual) in (int, float):
        return expected == actual
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            _same_runner_value(left, right) for left, right in zip(expected, actual, strict=True)
        )
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(
            _same_runner_value(expected[key], actual[key]) for key in expected
        )
    return expected == actual


def _runner_identity_matches(captured: Mapping[str, Any], stored: Any, *, dialect: str) -> bool:
    if isinstance(stored, str):
        # SQLite returns raw JSON text; asyncpg has already decoded JSON once.
        # A PostgreSQL string is a JSON scalar, even when it looks like an object.
        if dialect != "sqlite":
            return False
        try:
            stored = json.loads(stored)
        except (ValueError, RecursionError):
            return False
    if not isinstance(stored, dict):
        return False
    for key in _RUNNER_IDENTITY_FIELDS:
        if (key in captured) != (key in stored):
            return False
        if key in captured and not _same_runner_value(captured[key], stored[key]):
            return False
    return True


class _TxProvider(Protocol):
    """Structural requirement satisfied by ``lionagi.state.db.StateDB``
    without importing it here (would be circular)."""

    dialect: str

    def _tx(self) -> Any: ...

    def _owner_values(self, table: str) -> dict[str, str]: ...

    async def _require_owned(self, conn: Any, table: str, row_id: Any, what: str) -> None: ...

    async def _prepare_status_patch(
        self,
        conn: Any,
        entity_type: str,
        entity_id: str,
        patch: Mapping[str, Any],
        *,
        fill_only_if_absent: frozenset[str] = ...,
    ) -> Mapping[str, Any]: ...


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
        self._schedule_adapter: ScheduleLifecycleAdapter | None = None

    @property
    def schedule_adapter(self) -> ScheduleLifecycleAdapter:
        if self._schedule_adapter is None:
            self._schedule_adapter = ScheduleLifecycleAdapter(self)
        return self._schedule_adapter

    async def initialize_in_transaction(
        self,
        connection: AsyncConnection,
        command: InitialStateCommand,
    ) -> str:
        if command.entity_type == "schedule":
            return await self.schedule_adapter.initialize_in_transaction(connection, command)
        if command.reason.code.startswith("schedule_definition."):
            raise LifecycleValidationError("schedule_definition reasons belong only to definitions")
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

        return await self.append_history_in_transaction(
            connection,
            entity_type=command.entity_type,
            entity_id=command.entity_id,
            previous_status=None,
            new_status=command.status,
            reason=command.reason,
            source=command.actor.type,
            actor=command.actor.id,
        )

    async def append_history_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        entity_type: str,
        entity_id: str,
        previous_status: str | None,
        new_status: str,
        reason: ReasonRecord,
        source: str,
        actor: str | None,
        created_at: float | None = None,
    ) -> str:
        """Append one validated transition on its owning write connection."""
        transition_id = uuid.uuid4().hex
        now = time.time() if created_at is None else created_at
        owner_columns, owner_params, owner_values = self._owner_insert("status_transitions")
        await connection.execute(
            text(
                "INSERT INTO status_transitions "  # noqa: S608 -- the owner column is quoted
                "(id, entity_type, entity_id, previous_status, status, "
                " reason_code, reason_summary, evidence_refs, "
                f" source, actor, created_at, metadata{owner_columns}) "
                "VALUES (:id, :entity_type, :entity_id, :previous_status, :status, "
                " :reason_code, :reason_summary, :evidence_refs, "
                f" :source, :actor, :created_at, :metadata{owner_params})"
            ).bindparams(
                bindparam("evidence_refs", type_=JSON),
                bindparam("metadata", type_=JSON),
            ),
            {
                "id": transition_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "previous_status": previous_status,
                "status": new_status,
                "reason_code": reason.code,
                "reason_summary": reason.summary,
                "evidence_refs": _json_list(reason.evidence_refs),
                "source": source,
                "actor": actor,
                "created_at": now,
                "metadata": dict(reason.metadata),
                **owner_values,
            },
        )
        return transition_id

    def _owner_insert(self, table: str) -> tuple[str, str, dict[str, str]]:
        """The owner columns, placeholders and values an insert into *table* adds.

        A store whose rows carry an owner gets it on every row lifecycle writes (ADR-0128 D5);
        natively there is none.
        """
        owned = self._db._owner_values(table)
        columns = "".join(f", {_quote(name)}" for name in owned)
        placeholders = "".join(f", :_owner{i}" for i in range(len(owned)))
        return columns, placeholders, {f"_owner{i}": v for i, v in enumerate(owned.values())}

    async def transition(
        self,
        command: TransitionCommand,
        *,
        fill_only_if_absent: frozenset[str] = frozenset(),
    ) -> TransitionOutcome:
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
            fill_only_if_absent=fill_only_if_absent,
        )

    async def _emit_terminal_event(
        self,
        command: TransitionCommand,
        *,
        previous_status: str | None,
        transition_id: str | None,
        occurred_at: float,
    ) -> None:
        if transition_id is None or previous_status == command.to_status:
            return
        policy = self._registry.get(command.entity_type)
        if (
            command.entity_type in EXECUTION_ENTITY_KINDS
            and command.to_status in policy.terminal_statuses
        ):
            envelope = _build_terminal_envelope(
                command,
                previous_status=previous_status,
                transition_id=transition_id,
                occurred_at=occurred_at,
            )
            await self._terminal_callbacks.emit(envelope)

    async def _transition(
        self,
        command: TransitionCommand,
        *,
        extra_guard: Mapping[str, Any] | None = None,
        expected_runner_identity: Mapping[str, Any] | None = None,
        enforce_edges: bool = False,
        undeclared_edge_mode: str = "raise",
        raise_on_unguarded_conflict: bool = False,
        write_reason_columns: bool = True,
        fill_only_if_absent: frozenset[str] = frozenset(),
        owned_links: tuple[tuple[str, str, str], ...] = (),
    ) -> TransitionOutcome:
        if expected_runner_identity is not None:
            if command.entity_type != "session":
                raise LifecycleValidationError("runner identity guard requires a session")
            if not isinstance(expected_runner_identity, Mapping):
                raise LifecycleValidationError("runner identity guard requires a mapping")
            if set(expected_runner_identity) - _RUNNER_IDENTITY_FIELDS:
                raise LifecycleValidationError("runner identity guard has unsupported fields")
            expected_runner_identity = dict(expected_runner_identity)
        if command.entity_type == "schedule":
            if fill_only_if_absent or owned_links:
                raise LifecycleValidationError("unsupported schedule transition companion fields")
            return await self.schedule_adapter.transition(command, extra_guard=extra_guard)
        if command.reason.code.startswith("schedule_definition."):
            raise LifecycleValidationError("schedule_definition reasons belong only to definitions")
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
            # In compatibility mode a row that no longer carries the owner is refused here,
            # before any read decides the transition; natively this does nothing.
            await self._db._require_owned(
                conn, policy.table, command.entity_id, command.entity_type
            )
            # A session update names links (a message it references, an invocation it adopts);
            # each is checked here, in the same transaction and before the status is read, so a
            # link that changed hands is refused before the status the update asked for is
            # written, rather than after it has committed (ADR-0128 D5).
            for table, row_id, what in owned_links:
                await self._db._require_owned(conn, table, row_id, what)
            # A status change carries its node_metadata into this same transaction, where the
            # store merges it by key rather than letting the patch below write it whole
            # (ADR-0128 D6); natively the patch is unchanged.
            prepared = await self._db._prepare_status_patch(
                conn,
                command.entity_type,
                command.entity_id,
                command.patch,
                fill_only_if_absent=fill_only_if_absent,
            )
            if prepared is not command.patch:
                command = replace(command, patch=prepared)
            guard_cols = list(extra_guard)
            # A caller may guard one of the duration-basis columns. Keep the
            # SELECT unique, but never let that guard disable the terminal-end
            # invariant itself.
            fetch_cols = list(dict.fromkeys(["status", "updated_at", *guard_cols]))
            if expected_runner_identity is not None and "node_metadata" not in fetch_cols:
                fetch_cols.append("node_metadata")
            if needs_duration_basis:
                fetch_cols.extend(
                    col
                    for col in ("started_at", "ended_at", "ended_at_is_approximate")
                    if col not in fetch_cols
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
            if expected_runner_identity is not None and not _runner_identity_matches(
                expected_runner_identity, row["node_metadata"], dialect=self._db.dialect
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
                owner_columns, owner_params, owner_values = self._owner_insert("admin_events")
                await conn.execute(
                    text(
                        "INSERT INTO admin_events "  # noqa: S608 -- the owner column is quoted
                        f"(id, created_at, action, target_id, details, actor{owner_columns}) "
                        "VALUES (:id, :created_at, :action, :target_id, :details, "
                        f":actor{owner_params})"
                    ).bindparams(bindparam("details", type_=JSON)),
                    {
                        **owner_values,
                        "id": short_id(12),
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
                # A repaired row carries a guessed end until something measures
                # one, and this transition is that measurement. Reusing the
                # guess would compute a real-looking duration from a number
                # nobody observed.
                measured_here = ended_at_value is None or (
                    "ended_at" not in command.patch and row["ended_at_is_approximate"]
                )
                if measured_here:
                    ended_at_value = now
                patch = dict(command.patch)
                # setdefault cannot displace a key the caller already sent, so
                # an explicit ended_at=None would survive while the duration
                # below is computed from the end measured here -- a row with no
                # end, marked measured, carrying a duration. Where this
                # transition is the measurement, its value wins over a key
                # whose value is the absence of one.
                if measured_here:
                    patch["ended_at"] = ended_at_value
                else:
                    patch.setdefault("ended_at", ended_at_value)
                # The flag and the duration are one fact and move together.
                # Leaving the bit set beside a measured duration produces a row
                # whose own two fields disagree, and readers believe the bit:
                # they discard the duration this write just measured.
                #
                # Where this transition is what measured the end, that holds
                # against a caller who sent the bit too: the value they sent
                # described an end they did not supply, so it cannot describe
                # the one taken here. A caller supplying both an end and the
                # bit is recording a reconstructed end deliberately, and keeps
                # it -- which is why this is not an unconditional clear.
                if measured_here:
                    patch["ended_at_is_approximate"] = 0
                else:
                    patch.setdefault("ended_at_is_approximate", 0)
                # Derived only from an end that was measured. Subtracting a
                # reconstructed end from a start yields a real-looking number
                # for a length nobody observed, which is the thing the flag
                # exists to prevent -- and once stored, the duration outlives
                # every reader's chance to notice. An approximate end leaves
                # duration unknown instead.
                #
                # This reads the flag settled just above rather than
                # measured_here, because a caller who supplies both an end and
                # the bit is recording a reconstructed end deliberately, and
                # that end is no more subtractable than a repaired one.
                if patch["ended_at_is_approximate"]:
                    # Cleared, not merely left uncomputed. Both readers already
                    # discard a stored duration when this bit is set, so one
                    # persisted beside it is a value nothing can ever surface,
                    # and the row asserts a measured length for an end nobody
                    # measured. The schema-version repair clears it in the same
                    # write that sets the bit for exactly this reason; a
                    # transition that sets the bit owes the same.
                    patch["duration_ms"] = None
                elif "duration_ms" not in patch:
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

            # An override event describes a transition that actually landed.
            # Keep it after every guard and the guarded UPDATE so a conflict
            # cannot commit a false-positive audit record.
            if override_admin_event:
                assert command.override is not None
                owner_columns, owner_params, owner_values = self._owner_insert("admin_events")
                await conn.execute(
                    text(
                        "INSERT INTO admin_events "  # noqa: S608 -- the owner column is quoted
                        f"(id, created_at, action, target_id, details, actor{owner_columns}) "
                        "VALUES (:id, :created_at, :action, :target_id, :details, "
                        f":actor{owner_params})"
                    ).bindparams(bindparam("details", type_=JSON)),
                    {
                        **owner_values,
                        "id": short_id(12),
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

        await self._emit_terminal_event(
            command,
            previous_status=previous_status,
            transition_id=transition_id,
            occurred_at=now,
        )
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
        # An invocation's cancellation records an observation: cleanup cannot
        # revise an end time or a reason another writer already recorded for the
        # same terminal fact, including one recorded after this transition read
        # the row. A first cancellation (previous status not yet terminal) writes
        # both; a later one keeps what is stored and only appends its history.
        observed_cancel = (
            command.entity_type == "invocation"
            and command.to_status == "cancelled"
            and command.override is None
        )
        revise_recorded_cancel = observed_cancel and previous_status == command.to_status
        set_clauses = ["status = :status", "updated_at = :now"]
        if write_reason_columns:
            if revise_recorded_cancel:
                set_clauses[1:1] = [
                    "status_reason_code = COALESCE(status_reason_code, :reason_code)",
                    "status_reason_summary = COALESCE(status_reason_summary, :reason_summary)",
                    "status_evidence_refs = COALESCE(status_evidence_refs, :evidence_refs)",
                ]
            else:
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
            if expected is None:
                where_clauses.append(f"{col} IS NULL")
            else:
                key = f"guard_{i}"
                where_clauses.append(f"{col} = :{key}")
                params[key] = expected
        for col, value in command.patch.items():
            key = f"patch_{col}"
            if observed_cancel and col == "ended_at":
                set_clauses.append(f"{col} = COALESCE({col}, :{key})")
            else:
                set_clauses.append(f"{col} = :{key}")
            params[key] = value
        # In compatibility mode the update reaches only the owner's row (ADR-0128 D5).
        for i, (col, owner) in enumerate(self._db._owner_values(table).items()):
            where_clauses.append(f"{_quote(col)} = :_owner_guard{i}")
            params[f"_owner_guard{i}"] = owner

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

        return await self.append_history_in_transaction(
            conn,
            entity_type=command.entity_type,
            entity_id=command.entity_id,
            previous_status=previous_status,
            new_status=command.to_status,
            reason=command.reason,
            source=command.actor.type,
            actor=command.actor.id,
            created_at=now,
        )
