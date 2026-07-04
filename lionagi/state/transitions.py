# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Guarded compare-and-swap state transitions (ADR-0092 slice 1, spec-gate ruling 1).

ADR-0062's ``transition()`` API (entity-agnostic, idempotency-key-deduplicated)
is proposed and unbuilt. This module ships a minimal fallback carrying the same
request/result shape and reason-code discipline so 0062 can absorb it later as
a refactor, not a migration. Scoped to ``entity_type='dispatch'``
(``dispatch_outbox``) only — it is not a general TransitionStore.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.types import JSON

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
_ENTITY_TABLES: dict[str, str] = {
    "dispatch": "dispatch_outbox",
}


async def transition(db: Any, request: TransitionRequest) -> TransitionResult:
    """Guarded compare-and-swap transition: ``UPDATE ... WHERE id=:id AND status=:from``.

    Writes the row status and an atomic ``status_transitions`` append inside
    one ``db._tx()``. A mismatched current state reports a conflict rather
    than raising or silently overwriting (CAS guard).
    """
    table = _ENTITY_TABLES.get(request.entity_type)
    if table is None:
        raise ValueError(f"transition(): unsupported entity_type {request.entity_type!r}")
    validate_reason_code(request.reason.code)

    now = time.time()
    transition_id = uuid.uuid4().hex

    async with db._tx() as conn:
        sel = f"SELECT status FROM {table} WHERE id = :id"  # noqa: S608
        row = (await conn.execute(text(sel), {"id": request.entity_id})).mappings().first()
        if row is None:
            raise LookupError(
                f"{request.entity_type} {request.entity_id!r} not found (table={table})"
            )
        previous_status = row["status"]

        if request.from_state is not None and previous_status != request.from_state:
            return TransitionResult(
                applied=False,
                conflict=True,
                previous_state=previous_status,
                current_state=previous_status,
                transition_id=transition_id,
            )

        result = await conn.execute(
            text(
                f"UPDATE {table} SET status = :to_state, updated_at = :now "  # noqa: S608
                "WHERE id = :id AND status = :from_state"
            ),
            {
                "to_state": request.to_state,
                "now": now,
                "id": request.entity_id,
                "from_state": previous_status,
            },
        )
        if result.rowcount == 0:
            # Lost the race between the SELECT and the guarded UPDATE.
            return TransitionResult(
                applied=False,
                conflict=True,
                previous_state=previous_status,
                current_state=previous_status,
                transition_id=transition_id,
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
                "id": transition_id,
                "entity_type": request.entity_type,
                "entity_id": request.entity_id,
                "previous_status": previous_status,
                "status": request.to_state,
                "reason_code": request.reason.code,
                "reason_summary": request.reason.summary,
                "evidence_refs": request.reason.evidence_refs,
                "source": request.actor.type,
                "actor": request.actor.id,
                "created_at": now,
                "metadata": request.reason.metadata,
            },
        )

    return TransitionResult(
        applied=True,
        conflict=False,
        previous_state=previous_status,
        current_state=request.to_state,
        transition_id=transition_id,
        event_id=None,
    )
