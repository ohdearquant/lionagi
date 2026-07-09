# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0101 D1: the task-application submit surface.

``TaskApplication`` is the frozen submit shape every binding shares. This
module wires only the in-process binding (``submit_task`` /
``cancel_task``) — ``li task submit`` and ``POST /api/tasks`` are later,
separate bindings that call the same functions.

``submit_task`` validates the application and writes a durable ``queued``
row into ``schedule_runs`` (the ADR-0101 D2 generalized task entity,
``schedule_id`` NULL). That first write is a plain INSERT — there is no
prior CAS state to guard. Every status move after it routes through
``lionagi.state.transitions.transition()``; this module never writes
``schedule_runs.status`` directly again.

No worker/lease loop and no remote execution live here — ``execution_target``
and ``library_ref`` are stored as provenance for a later slice (D3, already
shipped, and ADR-0102). ``required_capabilities`` is used at submit time only
to derive the D4 host-scoped ``concurrency_key`` (``capabilities.py``); the
claim-time eligibility/affinity matching itself lives in ``worker.py``.
"""

from __future__ import annotations

import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.types import JSON

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition

from ..scheduler import capabilities
from ..scheduler.subprocess import _ALIAS_ACTION_KINDS, _VALID_ACTION_KINDS

__all__ = ("TaskApplication", "cancel_task", "submit_task")

# ADR-0101 D1: this ADR pair adds "workflow" (ADR-0102 registry-resolved
# definitions) to the existing launcher vocabulary — a CHECK widen, not a
# rename of any existing kind. Reuses the launcher's own closed set +
# "playbook" alias rather than declaring a second copy.
_TASK_APPLICATION_ACTION_KINDS: frozenset[str] = _VALID_ACTION_KINDS | {"workflow"}

_VALID_EXECUTION_TARGETS: frozenset[str] = frozenset(
    {"host", "local_worktree", "daytona", "remote_agent", "process"}
)


@dataclass(frozen=True)
class TaskApplication:
    """D1's frozen submit shape."""

    action_kind: str
    args: dict[str, Any]
    execution_target: str
    required_capabilities: list[str] = field(default_factory=list)
    library_ref: str | None = None
    library_content_hash: str | None = None
    # Part of the submit contract per ADR-0062 dedup, but submit-level
    # deduplication is not built yet — submit_task rejects a non-None value
    # rather than silently double-enqueueing a retried application.
    idempotency_key: str | None = None


def _validate(app: TaskApplication) -> str:
    """Validate *app*; return the normalized (alias-resolved) action_kind."""
    if app.idempotency_key is not None:
        raise ValueError(
            "idempotency_key is not supported yet: submit-level deduplication "
            "is unimplemented, and accepting the key would silently enqueue a "
            "duplicate task on retry instead of returning the existing one"
        )
    normalized_kind = _ALIAS_ACTION_KINDS.get(app.action_kind, app.action_kind)
    if normalized_kind not in _TASK_APPLICATION_ACTION_KINDS:
        valid = sorted(_TASK_APPLICATION_ACTION_KINDS | set(_ALIAS_ACTION_KINDS))
        raise ValueError(f"unknown action_kind {app.action_kind!r}. Valid kinds: {valid}")
    if app.execution_target not in _VALID_EXECUTION_TARGETS:
        raise ValueError(
            f"unknown execution_target {app.execution_target!r}. "
            f"Valid targets: {sorted(_VALID_EXECUTION_TARGETS)}"
        )
    if not isinstance(app.required_capabilities, list) or not all(
        isinstance(tok, str) and tok for tok in app.required_capabilities
    ):
        raise ValueError(
            "required_capabilities must be a list of non-empty strings, got "
            f"{app.required_capabilities!r}"
        )
    if not isinstance(app.args, dict):
        raise ValueError(f"args must be a dict, got {type(app.args).__name__}")
    return normalized_kind


def _derive_concurrency_key(required_capabilities: list[str]) -> str | None:
    """D4's host-scoped rule: only the serialization-class tokens among
    *required_capabilities* (capabilities.py's declarative token->class map)
    fold into a concurrency_key scoped to this host, so ADR-0061 admission
    serializes same-host claims for that resource. Eligibility and affinity
    tokens never gate concurrency: an eligibility-only or affinity-only task
    gets no concurrency_key at all.
    """
    return capabilities.host_scoped_concurrency_key(socket.gethostname(), required_capabilities)


async def submit_task(db: StateDB, app: TaskApplication) -> str:
    """Validate *app* and write a durable ``queued`` row. Returns the new
    ``schedule_runs.id``."""
    normalized_kind = _validate(app)
    run_id = str(uuid.uuid4())
    now = time.time()
    concurrency_key = _derive_concurrency_key(app.required_capabilities)

    async with db._tx() as conn:
        await conn.execute(
            text(
                """INSERT INTO schedule_runs
                   (id, schedule_id, invocation_id, trigger_context,
                    action_kind, action_args, status, chain_depth,
                    fired_at, created_at, queued_at, concurrency_key,
                    required_capabilities, execution_target,
                    library_ref, library_content_hash)
                   VALUES (:id, NULL, NULL, :trigger_context,
                           :action_kind, :action_args, 'queued', 0,
                           :fired_at, :created_at, :queued_at, :concurrency_key,
                           :required_capabilities, :execution_target,
                           :library_ref, :library_content_hash)"""
            ).bindparams(
                bindparam("trigger_context", type_=JSON),
                bindparam("action_args", type_=JSON),
                bindparam("required_capabilities", type_=JSON),
            ),
            {
                "id": run_id,
                "trigger_context": {},
                "action_kind": normalized_kind,
                "action_args": app.args,
                "fired_at": now,
                "created_at": now,
                "queued_at": now,
                "concurrency_key": concurrency_key,
                "required_capabilities": app.required_capabilities,
                "execution_target": app.execution_target,
                "library_ref": app.library_ref,
                "library_content_hash": app.library_content_hash,
            },
        )
    return run_id


async def cancel_task(db: StateDB, run_id: str, *, actor: Actor) -> bool:
    """Cancel a still-``queued`` task application via the CAS transition
    store. Only ``queued -> cancelled`` is permitted in this slice
    (transitions.py's ADR-0101 vocab gate rejects anything else, e.g. a
    lease/running move, out from a queued row).
    """
    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="queued",
            to_state="cancelled",
            reason=StateReason(
                code=RunReasons.CANCELLED_ORCHESTRATOR,
                summary="task application cancelled before lease",
            ),
            actor=actor,
            idempotency_key=str(uuid.uuid4()),
        ),
    )
    return result.applied
