# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0071 D1: the task-application submit surface. ``TaskApplication`` is the frozen submit shape every binding (in-process, CLI, HTTP) shares.

ADR-0071 D3/PR2 adds a synchronous admission pre-check to ``submit_task()``
for the two conditions that are cheaply checkable at submission time -- the
duration guard (D6) and the waiter cap (D-Cap), when a holder is already
running for the derived ``concurrency_key``. A violation raises
``AdmissionRejectedError`` immediately (D-Reject's "typed error to the caller"),
so a submitter gets fast, observable feedback instead of a silent later
vanish. This is a best-effort early rejection only: the authoritative gate is
``lionagi.studio.scheduler.admit.admit()``, run again inside the worker claim
loop with whatever concurrency configuration the worker actually uses, which
is why the sign-off binding condition additionally requires claim-time
rejections to surface observably (see ``worker._reject_claim``).
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
from ..scheduler.admit import (
    DEFAULT_KEY_CONCURRENCY,
    DEFAULT_WAITER_CAP_MULTIPLIER,
    allows_deferred_over_cap,
    declared_max_duration_seconds,
    holder_is_running,
    waiter_ahead_count,
)
from ..scheduler.subprocess import _ALIAS_ACTION_KINDS, _VALID_ACTION_KINDS
from ..scheduler.worker import DEFAULT_LEASE_TTL_SECONDS

__all__ = ("AdmissionRejectedError", "TaskApplication", "cancel_task", "submit_task")


class AdmissionRejectedError(ValueError):
    """Typed rejection for a ``TaskApplication`` that admission policy would
    reject (waiter cap or duration guard) -- raised at submit time so the
    caller gets an immediate typed error rather than a silent later vanish
    at claim time. Mirrors the terminal branch of
    ``admit.AdmissionDecision`` (ADR-0071 D3/D-Reject). Subclasses
    ``ValueError`` so existing ``pytest.raises(ValueError, ...)``-shaped
    callers are unaffected."""

    def __init__(self, reason_code: str, reason_summary: str) -> None:
        self.reason_code = reason_code
        self.reason_summary = reason_summary
        super().__init__(reason_summary)


# ADR-0071 D1: adds "workflow" (ADR-0073) to the launcher vocabulary as a CHECK widen,
# not a rename -- reuses the launcher's own closed set + "playbook" alias.
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
    # Part of the ADR-0072 dedup contract, but not built yet -- submit_task rejects a
    # non-None value rather than silently double-enqueueing a retried application.
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
    """D4's host-scoped rule: only serialization-class tokens fold into a host-scoped concurrency_key; eligibility/affinity-only tasks get none."""
    return capabilities.host_scoped_concurrency_key(socket.gethostname(), required_capabilities)


async def submit_task(db: StateDB, app: TaskApplication) -> str:
    """Validate *app* and write a durable ``queued`` row. Returns the new
    ``schedule_runs.id``.

    Raises ``AdmissionRejectedError`` (D-Reject) when the submission's declared
    ``args["admission"]`` opts already violate the duration guard, or the
    waiter cap for an already-running holder of the derived
    ``concurrency_key`` -- see the module docstring for why this is a
    best-effort pre-check, not the authoritative gate.
    """
    normalized_kind = _validate(app)
    now = time.time()
    concurrency_key = _derive_concurrency_key(app.required_capabilities)

    max_duration = declared_max_duration_seconds(app.args)
    if max_duration is not None and max_duration >= DEFAULT_LEASE_TTL_SECONDS:
        raise AdmissionRejectedError(
            RunReasons.SKIPPED_DURATION_EXCEEDS_LEASE,
            f"declared max_duration_seconds={max_duration} >= lease TTL "
            f"({DEFAULT_LEASE_TTL_SECONDS}s); lease renewal is not yet "
            "shipped (ADR-0071 delta #5)",
        )

    if (
        concurrency_key is not None
        and not allows_deferred_over_cap(app.args)
        and await holder_is_running(db, concurrency_key)
    ):
        cap = DEFAULT_KEY_CONCURRENCY * DEFAULT_WAITER_CAP_MULTIPLIER
        ahead = await waiter_ahead_count(db, concurrency_key, before_queued_at=now)
        if ahead >= cap:
            raise AdmissionRejectedError(
                RunReasons.SKIPPED_WAITER_CAP_EXCEEDED,
                f"waiter cap ({cap}) exceeded for concurrency_key={concurrency_key!r}: "
                f"{ahead} job(s) already waiting behind the running holder",
            )

    run_id = str(uuid.uuid4())

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
    """Cancel a still-``queued`` task application via the CAS transition store. Only ``queued -> cancelled`` is permitted in this slice."""
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
