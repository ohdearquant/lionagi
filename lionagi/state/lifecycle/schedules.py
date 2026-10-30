# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Schedule definition storage adapter for the shared lifecycle policy and ledger."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

from sqlalchemy import text

from ..compat import _quote
from ..reasons import VALID_REASON_CODES, ScheduleDefinitionReasons
from . import LifecycleNotFoundError, LifecycleValidationError
from .models import (
    ActorType,
    InitialStateCommand,
    ProvenanceStatus,
    ReasonRecord,
    ScheduleAssurance,
    ScheduleAuditContext,
    ScheduleFailureCategory,
    ScheduleOrigin,
    TransitionCommand,
    TransitionOutcome,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from .service import SQLAlchemyLifecycleService

__all__ = ("ScheduleLifecycleAdapter",)


class ScheduleLifecycleAdapter:
    """Connection-owned definition writes; callers retain their transaction and claims."""

    def __init__(self, service: SQLAlchemyLifecycleService) -> None:
        self._service = service
        self._db = service._db
        self._policy = service._registry.get("schedule")

    @staticmethod
    def validate_context(audit_context: ScheduleAuditContext | None) -> ScheduleAuditContext:
        context = ScheduleAuditContext() if audit_context is None else audit_context
        if not isinstance(context, ScheduleAuditContext):
            raise LifecycleValidationError("audit_context must be a ScheduleAuditContext")
        for field, allowed in (
            ("origin", get_args(ScheduleOrigin)),
            ("assurance", get_args(ScheduleAssurance)),
            ("actor_status", get_args(ProvenanceStatus)),
            ("cwd_status", get_args(ProvenanceStatus)),
            ("failure_category", (*get_args(ScheduleFailureCategory), None)),
        ):
            if getattr(context, field) not in allowed:
                raise LifecycleValidationError(f"invalid schedule provenance {field}")
        if context.source not in (*get_args(ScheduleOrigin), *get_args(ActorType)):
            raise LifecycleValidationError("invalid schedule provenance source")
        if context.actor is not None and (
            not isinstance(context.actor, str) or not context.actor.strip()
        ):
            raise LifecycleValidationError("a supplied schedule actor must be nonblank")
        if (context.actor is not None) != (context.actor_status == "available"):
            raise LifecycleValidationError("schedule actor availability is inconsistent")
        if context.submitting_cwd is not None and (
            not isinstance(context.submitting_cwd, str)
            or not Path(context.submitting_cwd).is_absolute()
        ):
            raise LifecycleValidationError("a supplied submitting cwd must be absolute")
        if (context.submitting_cwd is not None) != (context.cwd_status == "available"):
            raise LifecycleValidationError("schedule cwd availability is inconsistent")
        if (
            context.origin == "http"
            and (context.actor is not None or context.submitting_cwd is not None)
            and context.assurance != "client_reported"
        ):
            raise LifecycleValidationError("HTTP schedule provenance is client-reported")
        if context.origin == "browser" and (
            context.actor is not None
            or context.submitting_cwd is not None
            or context.assurance != "unavailable"
        ):
            raise LifecycleValidationError("browser schedule provenance must be unavailable")
        return context

    @staticmethod
    def _validate_id(schedule_id: str) -> None:
        if not isinstance(schedule_id, str) or not schedule_id.strip():
            raise LifecycleValidationError("schedule id must be nonblank")

    def _status(self, row: Mapping[str, Any]) -> str:
        enabled = row.get("enabled", 1)
        if not isinstance(enabled, (int, bool)) or enabled not in (0, 1):
            raise LifecycleValidationError("schedule enabled must be 0 or 1")
        status = "enabled" if enabled else "disabled"
        if status not in self._policy.statuses:
            raise LifecycleValidationError(f"undeclared schedule state {status!r}")
        return status

    def _reason(
        self, target: str, reason: ReasonRecord | None, *, creation: bool = False
    ) -> ReasonRecord:
        defaults = {
            "enabled": ScheduleDefinitionReasons.ENABLED_OBSERVED,
            "disabled": ScheduleDefinitionReasons.DISABLED_UNSPECIFIED,
            "deleted": ScheduleDefinitionReasons.DELETED_OBSERVED,
        }
        code = ScheduleDefinitionReasons.CREATED_OBSERVED if creation else defaults[target]
        reason = ReasonRecord(code=code) if reason is None else reason
        if not isinstance(reason, ReasonRecord) or reason.code not in VALID_REASON_CODES:
            raise LifecycleValidationError("invalid schedule definition reason")
        expected = "created" if creation else target
        prefix = reason.code.split(".", 1)[0]
        if prefix not in self._policy.reason_prefixes or not reason.code.startswith(
            f"schedule_definition.{expected}."
        ):
            raise LifecycleValidationError("schedule definition reason does not match its target")
        if reason.code == ScheduleDefinitionReasons.DISABLED_MANUAL and (
            not isinstance(reason.summary, str) or not reason.summary.strip()
        ):
            raise LifecycleValidationError("a manual disable reason must include an explanation")
        return reason

    def validate_create(
        self,
        schedule: Mapping[str, Any],
        *,
        audit_context: ScheduleAuditContext | None = None,
        reason: ReasonRecord | None = None,
    ) -> None:
        self.validate_context(audit_context)
        self._validate_id(schedule["id"])
        status = self._status(schedule)
        if status not in self._policy.initial_statuses:
            raise LifecycleValidationError("undeclared initial schedule state")
        self._reason(status, reason, creation=True)

    def validate_change(
        self,
        before: Mapping[str, Any],
        after_or_none: Mapping[str, Any] | None,
        *,
        audit_context: ScheduleAuditContext | None = None,
        reason: ReasonRecord | None = None,
    ) -> None:
        context = self.validate_context(audit_context)
        self._validate_id(before["id"])
        previous = self._status(before)
        target = "deleted" if after_or_none is None else self._status(after_or_none)
        if after_or_none is not None and after_or_none["id"] != before["id"]:
            raise LifecycleValidationError("a schedule transition cannot change its id")
        if target not in self._policy.statuses:
            raise LifecycleValidationError("undeclared schedule state")
        self._reason(target, reason)
        if previous == target:
            if self._policy.same_status == "reject":
                raise LifecycleValidationError("same-state schedule transition is not allowed")
            return
        edge = next(
            (edge for edge in self._policy.edges.get(previous, ()) if edge.to_status == target),
            None,
        )
        if edge is None:
            raise LifecycleValidationError(f"undeclared schedule edge {previous!r} -> {target!r}")
        if edge.actor_types is not None and context.source not in edge.actor_types:
            raise LifecycleValidationError("schedule source is not permitted for this edge")

    async def _lock_id(self, conn: AsyncConnection, schedule_id: str) -> None:
        self._validate_id(schedule_id)
        if self._db.dialect != "sqlite":
            # The lock covers absent rows too, preventing create from racing a deletion's audit.
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"lionagi:schedule-definition:{schedule_id}"},
            )

    async def read_for_update(
        self, conn: AsyncConnection, schedule_id: str
    ) -> dict[str, Any] | None:
        await self._lock_id(conn, schedule_id)
        await self._db._require_owned(conn, "schedules", schedule_id, "schedule")
        sql = "SELECT * FROM schedules WHERE id = :id"
        if self._db.dialect != "sqlite":
            sql += " FOR UPDATE"
        row = (await conn.execute(text(sql), {"id": schedule_id})).mappings().first()
        return dict(row) if row is not None else None

    async def assert_id_available(self, conn: AsyncConnection, schedule_id: str) -> None:
        await self._lock_id(conn, schedule_id)
        used = await conn.execute(
            text(
                "SELECT 1 FROM status_transitions WHERE entity_type = 'schedule' "
                "AND entity_id = :id LIMIT 1"
            ),
            {"id": schedule_id},
        )
        if used.first() is not None:
            raise LifecycleValidationError("schedule definition ids cannot be reused")

    def _with_metadata(
        self,
        reason: ReasonRecord,
        row: Mapping[str, Any],
        context: ScheduleAuditContext,
    ) -> ReasonRecord:
        metadata = {
            **reason.metadata,
            "schedule_name": row.get("name"),
            "provenance": {
                "origin": context.origin,
                "submitting_cwd": context.submitting_cwd,
                "actor_status": context.actor_status,
                "cwd_status": context.cwd_status,
                "assurance": context.assurance,
                "failure_category": context.failure_category,
            },
        }
        return replace(reason, metadata=metadata)

    async def record_created(
        self,
        conn: AsyncConnection,
        row: Mapping[str, Any],
        *,
        audit_context: ScheduleAuditContext | None = None,
        reason: ReasonRecord | None = None,
    ) -> str:
        self.validate_create(row, audit_context=audit_context, reason=reason)
        await self.assert_id_available(conn, row["id"])
        context = self.validate_context(audit_context)
        status = self._status(row)
        reason = self._with_metadata(self._reason(status, reason, creation=True), row, context)
        return await self._service.append_history_in_transaction(
            conn,
            entity_type="schedule",
            entity_id=row["id"],
            previous_status=None,
            new_status=status,
            write_kind="initialization",
            reason=reason,
            source=context.source,
            actor=context.actor,
        )

    async def record_change(
        self,
        conn: AsyncConnection,
        before: Mapping[str, Any],
        after_or_none: Mapping[str, Any] | None,
        *,
        audit_context: ScheduleAuditContext | None = None,
        reason: ReasonRecord | None = None,
    ) -> str | None:
        self.validate_change(before, after_or_none, audit_context=audit_context, reason=reason)
        previous = self._status(before)
        target = "deleted" if after_or_none is None else self._status(after_or_none)
        if previous == target and self._policy.same_status == "noop":
            return None
        context = self.validate_context(audit_context)
        reason = self._with_metadata(
            self._reason(target, reason),
            before if after_or_none is None else after_or_none,
            context,
        )
        return await self._service.append_history_in_transaction(
            conn,
            entity_type="schedule",
            entity_id=before["id"],
            previous_status=previous,
            new_status=target,
            write_kind="transition",
            reason=reason,
            source=context.source,
            actor=context.actor,
        )

    @staticmethod
    def _command_context(command: InitialStateCommand | TransitionCommand) -> ScheduleAuditContext:
        if not command.actor.id or command.actor.type not in get_args(ActorType):
            raise LifecycleValidationError("typed schedule commands require a valid ActorRecord")
        return ScheduleAuditContext(
            actor=command.actor.id,
            source=command.actor.type,
            origin="programmatic",
            assurance="client_reported",
            actor_status="available",
            cwd_status="unavailable",
            failure_category="cwd_unavailable",
        )

    async def initialize_in_transaction(
        self, conn: AsyncConnection, command: InitialStateCommand
    ) -> str:
        context = self._command_context(command)
        row = await self.read_for_update(conn, command.entity_id)
        if row is None:
            raise LifecycleNotFoundError(f"schedule {command.entity_id!r} not found")
        if command.status != self._status(row):
            raise LifecycleValidationError(
                "initial schedule state must match the stored definition"
            )
        return await self.record_created(conn, row, audit_context=context, reason=command.reason)

    async def transition(
        self, command: TransitionCommand, *, extra_guard: Mapping[str, Any] | None = None
    ) -> TransitionOutcome:
        context = self._command_context(command)
        self._validate_id(command.entity_id)
        if command.to_status not in self._policy.statuses:
            raise LifecycleValidationError("undeclared schedule state")
        self._reason(command.to_status, command.reason)
        if command.patch or command.override is not None:
            raise LifecycleValidationError(
                "typed schedule transitions do not admit patches or overrides"
            )
        extra_guard = {} if extra_guard is None else extra_guard
        if set(extra_guard) - {"next_fire_at", "github_cursor", "owner_key", "updated_at"}:
            raise LifecycleValidationError("unsupported schedule claim field")
        async with self._db._tx() as conn:
            before = await self.read_for_update(conn, command.entity_id)
            if before is None:
                raise LifecycleNotFoundError(f"schedule {command.entity_id!r} not found")
            previous = self._status(before)

            def outcome(result, transition_id=None):
                return TransitionOutcome(
                    result=result,
                    previous_status=previous,
                    current_status=command.to_status if result == "applied" else previous,
                    transition_id=transition_id,
                )

            if (
                (
                    command.expected_statuses is not None
                    and previous not in command.expected_statuses
                )
                or (
                    command.expected_version is not None
                    and before["updated_at"] != command.expected_version
                )
                or any(before[key] != value for key, value in extra_guard.items())
            ):
                return outcome("conflict")
            same = previous == command.to_status
            if same and self._policy.same_status == "noop":
                return outcome("applied")
            if (same and self._policy.same_status == "reject") or (
                not same
                and not any(
                    edge.to_status == command.to_status
                    for edge in self._policy.edges.get(previous, ())
                )
            ):
                return outcome("rejected")
            after = (
                None
                if command.to_status == "deleted"
                else {
                    **before,
                    "enabled": int(command.to_status == "enabled"),
                    "updated_at": time.time(),
                }
            )
            self.validate_change(before, after, audit_context=context, reason=command.reason)
            params = {"id": command.entity_id, "previous": before["enabled"]}
            where = "id = :id AND enabled = :previous"
            claims = dict(extra_guard)
            if command.expected_version is not None:
                claims["updated_at"] = command.expected_version
            claims.update(self._db._owner_values("schedules"))
            for index, (column, value) in enumerate(claims.items()):
                if value is None:
                    where += f" AND {_quote(column)} IS NULL"
                else:
                    where += f" AND {_quote(column)} = :claim{index}"
                    params[f"claim{index}"] = value
            if after is None:
                sql = f"DELETE FROM schedules WHERE {where}"  # noqa: S608
            else:
                sql = f"UPDATE schedules SET enabled = :enabled, updated_at = :now WHERE {where}"  # noqa: S608
                params.update(enabled=after["enabled"], now=after["updated_at"])
            result = await conn.execute(text(sql), params)
            if result.rowcount == 0:
                return outcome("conflict")
            transition_id = await self.record_change(
                conn,
                before,
                after,
                audit_context=context,
                reason=command.reason,
            )
            return outcome("applied", transition_id)
