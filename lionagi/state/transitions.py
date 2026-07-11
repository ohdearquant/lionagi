# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Guarded compare-and-swap state transitions (ADR-0092 slice 1, spec-gate ruling 1).

ADR-0058's ``transition()`` API (entity-agnostic, idempotency-key-deduplicated)
is proposed and unbuilt. This module ships a minimal fallback carrying the same
request/result shape and reason-code discipline so 0062 can absorb it later as
a refactor, not a migration. Scoped to ``entity_type='dispatch'``
(``dispatch_outbox``) and ``entity_type='schedule_run'`` (``schedule_runs``)
only — it is not a general TransitionStore.

The guarded read/CAS/vocabulary/write algorithm itself now lives in
``lionagi.state.lifecycle`` (shared with ``StateDB.update_status()``); this
module keeps its own narrower entity-type boundary, its ``guard``/``patch``
column allowlist, and the legacy ``TransitionResult`` return shape.
"""

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


# Entities the minimal fallback knows how to CAS-transition. ADR-0062's full
# backend generalizes this; slice 1 needs only dispatch_outbox.
# "schedule_run" is ADR-0101 D2's generalized task-application entity
# (schedule_runs table, schedule_id now nullable) — registered here so ALL
# status movement on it can route through this guarded CAS store rather than
# a second, parallel implementation.
_ENTITY_TABLES: dict[str, str] = {
    "dispatch": "dispatch_outbox",
    "schedule_run": "schedule_runs",
}

# guard/patch column names are interpolated directly into SQL text (values are
# still bound params) inside the lifecycle service. Every production call site
# today passes literal dicts, but this module is a generic surface that a
# future full transition backend will absorb, so a per-entity allowlist closes the
# latent injection surface for future callers instead of trusting the
# caller's dict keys outright.
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

    Writes the row status and an atomic ``status_transitions`` append inside
    one transaction, via the shared ``lionagi.state.lifecycle`` service.
    A mismatched current state reports a conflict rather than
    raising or silently overwriting (CAS guard). An undeclared status move
    (per the shared policy registry's edge graph) raises ``ValueError`` —
    this surface has no override mechanism, unlike ``StateDB.update_status()``.

    ``guard`` adds extra ``column = :expected`` equality constraints to the
    WHERE clause beyond ``status`` — required whenever a transition can be a
    same-state no-op (e.g. ``delivering -> delivering`` recovery claims),
    where the status guard alone would match trivially and let two
    concurrent callers both believe they won the claim. Callers pass the
    value they read *before* the transition as the expected guard value;
    only the caller whose guard value still matches at UPDATE time wins.

    ``patch`` adds extra ``column = :value`` assignments to the SET clause,
    applied atomically with the status change and the ``status_transitions``
    append — for callers (e.g. an operator-forced retry resetting attempt
    counters) that would otherwise need a second, non-atomic write.
    """
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

    # "rejected" is unreachable through this surface: run_legacy_transition()
    # passes raise_on_undeclared_edge=True, so an undeclared edge (terminal
    # or not) raises ValueError above rather than resolving to "rejected",
    # and TransitionRequest carries no override field to trigger the
    # override path either.
    return TransitionResult(
        applied=True,
        conflict=False,
        previous_state=outcome.previous_status,
        current_state=outcome.current_status,
        transition_id=outcome.transition_id,
        event_id=None,
    )
