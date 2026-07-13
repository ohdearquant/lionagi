# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Guarded compare-and-swap state transitions (ADR-0059) — minimal counterpart to ADR-0058's
entity-agnostic API, scoped to dispatch/schedule_run entities. See docs/internals/runtime.md."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from .lifecycle import LifecycleNotFoundError
from .lifecycle.adapters import run_legacy_transition
from .reasons import validate_reason_code

__all__ = (
    "Actor",
    "StateReason",
    "TransitionRequest",
    "TransitionResult",
    "transition",
)


class Actor(BaseModel):
    type: Literal["scheduler", "operator", "system", "webhook", "agent"]
    id: str


class StateReason(BaseModel):
    code: str
    summary: str = ""
    evidence_refs: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class TransitionRequest(BaseModel):
    entity_type: str
    entity_id: str
    from_state: str | None
    to_state: str
    reason: StateReason
    actor: Actor
    idempotency_key: str


class TransitionResult(BaseModel):
    applied: bool
    conflict: bool = False
    previous_state: str | None = None
    current_state: str
    transition_id: str
    event_id: str | None = None


# Entities the minimal fallback knows how to CAS-transition — see docs/internals/runtime.md.
_ENTITY_TABLES: dict[str, str] = {
    "dispatch": "dispatch_outbox",
    "schedule_run": "schedule_runs",
}

# guard/patch column names are interpolated into SQL text (values stay bound
# params); allowlist closes the latent injection surface — see docs/internals/runtime.md.
_GUARD_PATCH_COLUMNS: dict[str, frozenset[str]] = {
    "dispatch": frozenset({"attempt", "next_attempt_at", "last_error"}),
    "schedule_run": frozenset({"leased_by", "lease_expires_at", "lease_attempts"}),
}


async def transition(
    db: Any,
    request: TransitionRequest,
    *,
    guard: dict[str, Any] | None = None,
    patch: dict[str, Any] | None = None,
) -> TransitionResult:
    """Guarded compare-and-swap transition: ``UPDATE ... WHERE id=:id AND status=:from``.
    ``guard``/``patch`` extend the WHERE/SET clauses — see docs/internals/runtime.md."""
    table = _ENTITY_TABLES.get(request.entity_type)
    if table is None:
        raise ValueError(f"transition(): unsupported entity_type {request.entity_type!r}")
    validate_reason_code(request.reason.code)

    guard = guard or {}
    patch = patch or {}

    allowed_columns = _GUARD_PATCH_COLUMNS.get(request.entity_type, frozenset())
    for label, cols in (("guard", guard), ("patch", patch)):
        unknown = sorted(set(cols) - allowed_columns)
        if unknown:
            raise ValueError(
                f"transition(): {label} column(s) {unknown} are not in the declared "
                f"guard/patch allowlist for entity_type {request.entity_type!r} "
                f"(allowed: {sorted(allowed_columns)})"
            )

    try:
        outcome = await run_legacy_transition(
            db._lifecycle_service(),
            entity_type=request.entity_type,
            entity_id=request.entity_id,
            from_state=request.from_state,
            to_state=request.to_state,
            reason_code=request.reason.code,
            reason_summary=request.reason.summary,
            evidence_refs=request.reason.evidence_refs,
            metadata=request.reason.metadata,
            actor_type=request.actor.type,
            actor_id=request.actor.id,
            guard=guard,
            patch=patch,
        )
    except LifecycleNotFoundError as exc:
        raise LookupError(
            f"{request.entity_type} {request.entity_id!r} not found (table={table})"
        ) from exc

    if outcome.result == "conflict":
        return TransitionResult(
            applied=False,
            conflict=True,
            previous_state=outcome.previous_status,
            current_state=outcome.current_status,
            transition_id=uuid.uuid4().hex,
        )

    # "rejected" is unreachable here (raise_on_undeclared_edge=True) — see docs/internals/runtime.md.
    return TransitionResult(
        applied=True,
        conflict=False,
        previous_state=outcome.previous_status,
        current_state=outcome.current_status,
        transition_id=outcome.transition_id,
        event_id=None,
    )
