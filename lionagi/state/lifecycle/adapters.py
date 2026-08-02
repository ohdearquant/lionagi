# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""StateDB and legacy-transition compatibility mapping: both existing
transition surfaces (``StateDB.update_status()``, ``state.transitions.
transition()``) delegate their guarded write through
``SQLAlchemyLifecycleService`` via this module's command construction and
outcome-to-legacy-return mapping, so neither wrapper carries independent
policy. ``TransitionRejectedError`` lives here (not in ``state/db.py``, which
imports this package) to avoid a circular import; ``state/db.py`` re-exports
the same class object. See docs/internals/runtime.md."""

from __future__ import annotations

from typing import Any

from .models import ActorRecord, OverrideRecord, ReasonRecord, TransitionCommand
from .service import SQLAlchemyLifecycleService

__all__ = (
    "TransitionRejectedError",
    "run_legacy_transition",
    "run_update_status",
)


class TransitionRejectedError(RuntimeError):
    """Raised by update_status() when a write would move an entity out of a
    terminal status without an explicit, justified override."""

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
            "without override=True"
        )


async def run_update_status(
    service: SQLAlchemyLifecycleService,
    *,
    entity_type: str,
    entity_id: str,
    new_status: str,
    reason_code: str,
    reason_summary: str = "",
    evidence_refs: list[dict[str, Any]] | None = None,
    source: str,
    actor: str | None,
    metadata: dict[str, Any] | None,
    expected_statuses: set[str | None] | frozenset[str | None] | None,
    expected_updated_at: float | None,
    extra_fields: dict[str, Any] | None,
    override: bool,
    override_actor: str | None,
    override_justification: str | None,
) -> bool:
    """Route StateDB.update_status()'s kwargs through the lifecycle service
    and map the TransitionOutcome onto its legacy bool/raise contract."""
    command = TransitionCommand(
        entity_type=entity_type,
        entity_id=entity_id,
        to_status=new_status,
        reason=ReasonRecord(
            code=reason_code,
            summary=reason_summary,
            evidence_refs=tuple(evidence_refs or []),
            metadata=metadata or {},
        ),
        # actor id passed through as-is, including None (legacy behavior)
        actor=ActorRecord(type=source, id=actor),  # type: ignore[arg-type]
        expected_statuses=(frozenset(expected_statuses) if expected_statuses is not None else None),
        expected_version=expected_updated_at,
        patch=extra_fields or {},
        override=(
            OverrideRecord(actor=override_actor or "", justification=override_justification or "")
            if override
            else None
        ),
    )
    outcome = await service._transition(  # noqa: SLF001 -- same-package internal API
        command, raise_on_unguarded_conflict=True
    )
    if outcome.result == "applied":
        return True
    if outcome.result == "rejected":
        raise TransitionRejectedError(entity_type, entity_id, outcome.previous_status, new_status)
    # conflict: reachable here only for a guarded lost race; an unguarded
    # zero-row write raises RuntimeError instead (raise_on_unguarded_conflict)
    return False


async def run_legacy_transition(
    service: SQLAlchemyLifecycleService,
    *,
    entity_type: str,
    entity_id: str,
    from_state: str | None,
    to_state: str,
    reason_code: str,
    reason_summary: str,
    evidence_refs: list[dict[str, Any]],
    metadata: dict[str, Any],
    actor_type: str,
    actor_id: str,
    guard: dict[str, Any] | None,
    patch: dict[str, Any] | None,
):
    """Route `lionagi.state.transitions.transition()`'s request through the
    lifecycle service, preserving its ``guard``/``patch`` per-column CAS via
    the service's non-public ``extra_guard`` parameter."""
    command = TransitionCommand(
        entity_type=entity_type,
        entity_id=entity_id,
        to_status=to_state,
        reason=ReasonRecord(
            code=reason_code,
            summary=reason_summary,
            evidence_refs=tuple(evidence_refs or []),
            metadata=metadata or {},
        ),
        actor=ActorRecord(type=actor_type, id=actor_id),
        expected_statuses=frozenset({from_state}) if from_state is not None else None,
        patch=patch or {},
    )
    return await service._transition(  # noqa: SLF001
        command, extra_guard=guard, enforce_edges=True, write_reason_columns=False
    )
