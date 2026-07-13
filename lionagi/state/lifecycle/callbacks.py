# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Post-commit terminal-event envelope and the process-wide handler registry
that delivers it. Best-effort fan-out under one shared deadline; see
docs/internals/runtime.md.
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

# Closed set of entity kinds the lifecycle service emits terminal events for.
EXECUTION_ENTITY_KINDS: frozenset[str] = frozenset(
    {"session", "invocation", "schedule_run", "play"}
)

# Shared deadline for the whole fan-out, not per-handler.
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
    """Stable but nullable correlation keys, populated only from the
    transitioning entity's own id (no surface-specific join)."""

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
    """The minimal versioned terminal-event fact; see docs/internals/runtime.md
    for the schema-version stability contract."""

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
    Registration is idempotent by name (replaces in place)."""

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
        registration in place). ``override`` semantics: see
        docs/internals/runtime.md.
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
        swallowed; cancellation of the emitting task itself still propagates.
        """
        targets = [r for r in self._registrations.values() if r.matches(envelope)]
        if not targets:
            return
        overrides = [r for r in targets if r.override]
        if overrides:
            # A per-run override wins this envelope outright, replacing any
            # non-override match for this run's scope only.
            targets = overrides

        async def _run_one(reg: _Registration) -> None:
            try:
                if is_coro_func(reg.handler):
                    await maybe_await(reg.handler(envelope))
                else:
                    # Offload to a worker thread (never run sync handler body
                    # on the loop); abandon_on_cancel=True so a slow handler
                    # can't re-block the shared deadline — see runtime.md.
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
