# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""SQLAlchemy transaction implementation of the D4 guarded transition algorithm.

See docs/adr/ADR-0058-unified-lifecycle-transition-service.md D3/D4 for the
normative 13-step algorithm and creation-history contract implemented here.

``transition()`` is the public D1 Protocol method, which enforces the
policy's declared-edge graph: an undeclared move is a "rejected" outcome
with a rejection audit row, and a valid override is the audited escape
hatch. ``_transition()`` accepts
additional keyword-only parameters not part of the public
``TransitionCommand`` shape, used only by ``lionagi.state.lifecycle.adapters``
to keep the two legacy compatibility wrappers behaviorally identical to their
pre-ADR-0058 selves:

- ``extra_guard``: an arbitrary per-column WHERE-clause guard (e.g.
  dispatch's ``delivering -> delivering`` crash-recovery claim guarding on
  ``attempt``), which D1's typed command has no generic field for.
- ``enforce_edges``: ``StateDB.update_status()`` never enforced a
  declared-edge graph — only terminal-exit-requires-override and vocabulary
  membership — so it calls with ``enforce_edges=False`` (the default).
  ``lionagi.state.transitions.transition()`` did enforce one (for
  schedule_run), so it calls with ``enforce_edges=True``.
- ``raise_on_unguarded_conflict``: an unguarded zero-row UPDATE is a storage
  anomaly ``StateDB.update_status()`` has always raised ``RuntimeError`` on,
  from inside the transaction so a same-transaction rollback still occurs.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import JSON, bindparam, text

from ..reasons import VALID_REASON_CODES
from . import LifecycleNotFoundError, LifecycleValidationError
from .models import InitialStateCommand, TransitionCommand, TransitionOutcome
from .policy import DEFAULT_REGISTRY, PolicyRegistry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection


class _TxProvider(Protocol):
    """Structural requirement on the object handed to SQLAlchemyLifecycleService.

    Satisfied by ``lionagi.state.db.StateDB`` without importing it here (that
    module imports this package's adapters, so importing StateDB at module
    scope would be circular).
    """

    dialect: str

    def _tx(self) -> Any: ...


def _json_list(evidence_refs: tuple) -> list:
    return [dict(e) for e in evidence_refs]


class SQLAlchemyLifecycleService:
    """The D1 LifecycleService implementation, bound to one StateDB backend."""

    def __init__(self, db: _TxProvider, registry: PolicyRegistry = DEFAULT_REGISTRY) -> None:
        self._db = db
        self._registry = registry

    # ── D3: creation writes initial history in the caller's transaction ────

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

    # ── D1 public entry point ───────────────────────────────────────────

    async def transition(self, command: TransitionCommand) -> TransitionOutcome:
        # The public entry point enforces the policy's declared-edge graph.
        # An undeclared move is a "rejected" outcome (with rejection audit),
        # not a raise, so callers get the same D4 outcome shape for both
        # terminal-exit and undeclared-edge refusals; a valid override is the
        # audited escape hatch for either.
        # The legacy wrappers validate the reason code before calling in; the
        # public API must do the same or it becomes the one path that writes
        # uncontrolled reason_code values into status_transitions.
        if command.reason.code not in VALID_REASON_CODES:
            raise LifecycleValidationError(
                f"invalid reason_code: {command.reason.code!r}; must be one of "
                "the codes registered in lionagi.state.reasons.VALID_REASON_CODES"
            )
        # A globally registered code from another entity's domain (e.g. a
        # dispatch.* code on a schedule_run row) would still corrupt audit
        # semantics; the policy declares which prefixes belong to this entity.
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
        return await self._transition(
            command, extra_guard=None, enforce_edges=True, undeclared_edge_mode="reject"
        )

    # ── D4: the one guarded transition algorithm ────────────────────────

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
        # Step 1: resolve policy.
        policy = self._registry.get(command.entity_type)

        # Step 2: static validation (before BEGIN).
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

        async with self._db._tx() as conn:
            # Step 4: SELECT (FOR UPDATE on PostgreSQL).
            guard_cols = list(extra_guard)
            select_cols = ", ".join(["status", "updated_at", *guard_cols])
            sel = f"SELECT {select_cols} FROM {policy.table} WHERE id = :id"  # noqa: S608
            if self._db.dialect != "sqlite":
                sel += " FOR UPDATE"
            row = (await conn.execute(text(sel), {"id": command.entity_id})).mappings().first()

            # Step 5: missing row.
            if row is None:
                raise LifecycleNotFoundError(
                    f"{command.entity_type} {command.entity_id!r} not found (table={policy.table})"
                )
            previous_status = row["status"]

            # Step 6: expected_statuses / expected_version guards.
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

            same_status = previous_status == command.to_status
            declared_edges = policy.edges.get(previous_status, ()) if enforce_edges else ()
            self_edge = next((e for e in declared_edges if e.to_status == command.to_status), None)

            override_admin_event = False
            rejected = False

            if self_edge is not None:
                # Step 7: an ordinary declared edge (covers both regular
                # moves and a declared same-status edge like dispatch's
                # delivering -> delivering crash-recovery claim).
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
            elif same_status:
                # Step 7 (same-status, no declared self-edge): policy's
                # same_status rule governs — "append" is the only rule any
                # built-in policy currently uses.
                if policy.same_status == "noop":
                    return TransitionOutcome(
                        result="applied",
                        previous_status=previous_status,
                        current_status=command.to_status,
                        transition_id=None,
                    )
                if policy.same_status == "reject":
                    rejected = True
                # "append": falls through to the guarded write below.
            elif enforce_edges:
                # Undeclared edge (terminal or not), with the declared-edge
                # graph enforced. A valid override is the audited escape
                # hatch. Otherwise `undeclared_edge_mode` selects the
                # refusal shape: the legacy
                # `lionagi.state.transitions.transition()` surface never had
                # an override/rejection-audit concept — an undeclared move
                # there was and remains a plain vocabulary-violation
                # ValueError ("raise") — while the public `transition()`
                # entry point reports the same D4 "rejected" outcome (with
                # rejection audit) it uses for terminal exits ("reject").
                # This must be checked before the terminal_statuses branch
                # below (which is only reachable when enforce_edges=False).
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
                # Step 7/8/9: undeclared edge exiting a terminal status —
                # rejected unless a valid override is supplied.
                # `StateDB.update_status()` never enforced a declared-edge
                # graph at all (enforce_edges=False), only this
                # terminal-exit-requires-override check and vocabulary
                # membership.
                if command.override is not None:
                    override_admin_event = True
                else:
                    rejected = True
            # else: enforce_edges=False, nonterminal, no self-edge, not
            # same-status — an ordinary unrestricted move. Falls through to
            # the guarded write, matching StateDB.update_status()'s legacy
            # permissiveness (it never had a declared-edge graph).

            if rejected:
                # Step 8: rejection audit commits; ordinary history is not
                # appended (current status did not change).
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
                        # admin_events.actor is NOT NULL; legacy
                        # update_status() wrote `actor or source` here (the
                        # id is often None — a system-initiated write), unlike
                        # status_transitions.actor below, which stores the
                        # raw (possibly-None) id as given.
                        "actor": command.actor.id or command.actor.type,
                    },
                )
                return TransitionOutcome(
                    result="rejected",
                    previous_status=previous_status,
                    current_status=previous_status,
                    transition_id=None,
                )

            if override_admin_event:
                # Step 9: override audit, same transaction as the write.
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

            # extra_guard columns re-checked here (post edge/terminal
            # validation, matching the legacy transitions.transition()
            # ordering: CAS -> vocabulary -> guard columns -> UPDATE).
            for col, expected in extra_guard.items():
                if row[col] != expected:
                    return TransitionOutcome(
                        result="conflict",
                        previous_status=previous_status,
                        current_status=previous_status,
                        transition_id=None,
                    )

            # Steps 10-12: guarded UPDATE + status_transitions append.
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
                # Step 11: zero rows -> conflict, append nothing.
                guarded = (
                    command.expected_statuses is not None
                    or command.expected_version is not None
                    or bool(extra_guard)
                )
                if raise_on_unguarded_conflict and not guarded:
                    # No guard of any kind was supplied, yet the row changed
                    # between the SELECT and this UPDATE — a storage-level
                    # anomaly, not a legitimate lost race. Raising here
                    # (inside the still-open transaction) rolls back
                    # whatever else happened in this transaction, matching
                    # the pre-ADR-0058 `_apply_status_write` behavior of
                    # raising before the `async with` block could commit.
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

        # Step 13: commit (via _tx() context exit) -> applied.
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
        """Steps 10-12: the guarded UPDATE and its status_transitions append,
        factored out so both the ordinary and override paths share it (and so
        tests can inject a concurrent write immediately before this runs, the
        same seam the pre-ADR-0058 `StateDB._apply_status_write` provided).

        *write_reason_columns* mirrors legacy per-surface behavior: the
        `StateDB.update_status()` surface always denormalized the reason onto
        the entity row's own `status_reason_*` columns (True, the default);
        the legacy `lionagi.state.transitions.transition()` surface never
        did (False) — and `dispatch_outbox` (only reachable through that
        surface) does not even have those columns.

        Returns the new transition id, or ``None`` if the guarded UPDATE
        matched zero rows (conflict — caller appends nothing further).
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
