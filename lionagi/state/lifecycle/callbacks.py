# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Post-commit terminal-event envelope and the process-wide handler registry
that delivers it.

A ``RunTerminalEnvelope`` is constructed by the lifecycle service only after
a guarded transition has committed and landed on a terminal status for an
execution entity (session, invocation, schedule_run, play). The registry
then pushes that envelope to every matching registered handler, concurrently,
under one shared deadline. The push is best-effort: a handler failure,
timeout, or cancellation is logged and swallowed, and can never affect the
already-committed transition or delay the caller past the shared budget.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio.to_thread

from lionagi.ln.concurrency import (
    create_task_group,
    get_cancelled_exc_class,
    is_coro_func,
    maybe_await,
    move_on_after,
)

logger = logging.getLogger(__name__)

__all__ = (
    "Correlation",
    "EntityRef",
    "EXECUTION_ENTITY_KINDS",
    "HANDLER_BUDGET_SECONDS",
    "RunTerminalEnvelope",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "TerminalCallbackHandler",
    "TerminalCallbackRegistry",
    "DEFAULT_TERMINAL_CALLBACKS",
)

SCHEMA_NAME = "lionagi.run-terminal"
SCHEMA_VERSION = 1

# The closed set of entity kinds the lifecycle service constructs terminal
# events for — everything else (dispatch, team, show, ...) never reaches
# the registry.
EXECUTION_ENTITY_KINDS: frozenset[str] = frozenset(
    {"session", "invocation", "schedule_run", "play"}
)

# One shared deadline for the whole handler fan-out, not a per-handler
# timeout: a hanging handler is cancelled at this point, but
# never starves the others, and never delays the transition caller past it.
HANDLER_BUDGET_SECONDS = 10.0

# A handler may be sync or async; either return value is discarded.
TerminalCallbackHandler = Callable[["RunTerminalEnvelope"], Any]


@dataclass(frozen=True)
class EntityRef:
    kind: str
    id: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


@dataclass(frozen=True)
class Correlation:
    """Stable but nullable correlation keys.

    The lifecycle service never performs a surface-specific join to
    populate these beyond the transitioning entity's own id — a play's
    envelope, for instance, does not carry its underlying invocation id.
    """

    invocation_id: str | None = None
    session_id: str | None = None
    schedule_run_id: str | None = None
    run_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "invocation_id": self.invocation_id,
            "session_id": self.session_id,
            "schedule_run_id": self.schedule_run_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class RunTerminalEnvelope:
    """The minimal versioned terminal-event fact.

    Within ``schema_version == 1`` the guaranteed fields below never change
    name, type, semantics, or requiredness; new optional fields may be added
    without a version bump. ``event_id`` is the committed
    ``status_transitions.id`` for a durable event, or a synthetic id for the
    sole non-durable exception (``--no-persist`` engine runs).
    """

    event_id: str
    entity: EntityRef
    previous_status: str | None
    terminal_status: str
    reason_code: str
    occurred_at: float
    correlation: Correlation = field(default_factory=Correlation)
    artifacts: tuple[Mapping[str, Any], ...] = ()
    durable: bool = True
    schema: str = SCHEMA_NAME
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "durable": self.durable,
            "entity": self.entity.to_dict(),
            "previous_status": self.previous_status,
            "terminal_status": self.terminal_status,
            "reason_code": self.reason_code,
            "occurred_at": self.occurred_at,
            "correlation": self.correlation.to_dict(),
            "artifacts": [dict(a) for a in self.artifacts],
        }


@dataclass(frozen=True)
class _Registration:
    name: str
    handler: TerminalCallbackHandler
    kinds: frozenset[str] | None
    ids: frozenset[str] | None
    override: bool = False

    def matches(self, envelope: RunTerminalEnvelope) -> bool:
        if self.kinds is not None and envelope.entity.kind not in self.kinds:
            return False
        if self.ids is not None and envelope.entity.id not in self.ids:
            return False
        return True


class TerminalCallbackRegistry:
    """Process-local registry of post-commit terminal-event handlers.

    Registration is idempotent by name: registering the same name again
    replaces its handler/filters in place rather than adding a second entry,
    so a caller that re-registers on every call site never double-fires.
    """

    def __init__(self, *, budget_seconds: float = HANDLER_BUDGET_SECONDS) -> None:
        self._registrations: dict[str, _Registration] = {}
        self._budget_seconds = budget_seconds

    def register(
        self,
        name: str,
        handler: TerminalCallbackHandler,
        *,
        kinds: Sequence[str] | None = None,
        ids: Sequence[str] | None = None,
        override: bool = False,
    ) -> None:
        """Register *handler* under *name* (replaces an existing same-name
        registration in place).

        *override* marks this registration as a per-run override: for any
        envelope it matches, only override registrations fire -- any
        non-override registration that would otherwise also match (e.g. an
        unscoped settings-level handler) is skipped for that one envelope.
        This is scoped strictly to the envelopes the override itself
        matches (normally via ``ids``); it never disables a non-override
        handler for entities outside the override's own filter.
        """
        self._registrations[name] = _Registration(
            name=name,
            handler=handler,
            kinds=frozenset(kinds) if kinds is not None else None,
            ids=frozenset(ids) if ids is not None else None,
            override=override,
        )

    def unregister(self, name: str) -> None:
        self._registrations.pop(name, None)

    def clear(self) -> None:
        self._registrations.clear()

    def __contains__(self, name: str) -> bool:
        return name in self._registrations

    async def emit(self, envelope: RunTerminalEnvelope) -> None:
        """Invoke every matching handler concurrently under one deadline.

        Never raises: a handler exception or timeout is logged and
        swallowed. Cancellation of the emitting task itself still
        propagates (this must not turn a real shutdown into a silent
        no-op), it is only the *handlers'* own failures that are absorbed.
        """
        targets = [r for r in self._registrations.values() if r.matches(envelope)]
        if not targets:
            return
        overrides = [r for r in targets if r.override]
        if overrides:
            # A per-run override wins this envelope outright -- any
            # non-override handler that would also have matched (typically
            # an unscoped settings-level handler) is skipped, replacing it
            # for this run's scope only. Other envelopes the override does
            # not match are entirely unaffected.
            targets = overrides

        async def _run_one(reg: _Registration) -> None:
            try:
                if is_coro_func(reg.handler):
                    await maybe_await(reg.handler(envelope))
                else:
                    # A plain (non-async-def) handler is invoked in a worker
                    # thread, not on this event loop. Calling it directly
                    # here would run its body -- and any blocking I/O or
                    # time.sleep() inside it -- synchronously on the loop,
                    # during which the shared move_on_after deadline can
                    # never fire (nothing yields back to it) and no other
                    # handler in this fan-out can make progress either.
                    # abandon_on_cancel=True is required, not just the
                    # default offload: with the default (False), anyio
                    # defers delivering cancellation to this awaiting task
                    # until the worker thread finishes on its own, which
                    # would let a slow handler silently re-block the shared
                    # deadline it was just moved off of. With True, the
                    # move_on_after deadline can still cut this await short
                    # -- but the thread itself is only abandoned, not
                    # killed: its body may keep running to completion in
                    # the background after this returns. The budget
                    # guarantees the fan-out and the caller are never
                    # blocked by a slow sync handler, not that the handler
                    # itself stops.
                    await maybe_await(
                        await anyio.to_thread.run_sync(
                            reg.handler, envelope, abandon_on_cancel=True
                        )
                    )
            except get_cancelled_exc_class():
                raise
            except BaseException:  # noqa: BLE001 — a handler failure is swallowed by design
                logger.warning(
                    "terminal callback handler %r raised for event %s (entity %s/%s)",
                    reg.name,
                    envelope.event_id,
                    envelope.entity.kind,
                    envelope.entity.id,
                    exc_info=True,
                )

        with move_on_after(self._budget_seconds):
            async with create_task_group() as tg:
                for reg in targets:
                    tg.start_soon(_run_one, reg)


# Process-wide default registry. SQLAlchemyLifecycleService uses this unless
# a caller injects its own instance (tests, isolated bootstraps).
DEFAULT_TERMINAL_CALLBACKS = TerminalCallbackRegistry()
