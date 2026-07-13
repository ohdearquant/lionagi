# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unified lifecycle transition service: one guarded status-transition
algorithm shared by every managed entity type, replacing per-surface
transition logic.

Public surface: immutable command/result records (`models`), the policy
registry (`policy`), the SQLAlchemy transaction implementation (`service`),
and the StateDB/legacy-transition compatibility mapping (`adapters`).
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from .callbacks import (
    DEFAULT_TERMINAL_CALLBACKS,
    EXECUTION_ENTITY_KINDS,
    Correlation,
    EntityRef,
    RunTerminalEnvelope,
    TerminalCallbackHandler,
    TerminalCallbackRegistry,
)
from .models import (
    ActorRecord,
    ActorType,
    EdgePolicy,
    InitialStateCommand,
    JsonValue,
    LifecyclePolicy,
    OverrideRecord,
    ReasonRecord,
    SameStatusRule,
    TransitionCommand,
    TransitionOutcome,
    TransitionResultKind,
)
from .policy import DEFAULT_REGISTRY, PolicyRegistry, build_default_registry

__all__ = (
    "ActorRecord",
    "ActorType",
    "Correlation",
    "DEFAULT_REGISTRY",
    "DEFAULT_TERMINAL_CALLBACKS",
    "EdgePolicy",
    "EntityRef",
    "EXECUTION_ENTITY_KINDS",
    "InitialStateCommand",
    "JsonValue",
    "LifecycleError",
    "LifecycleNotFoundError",
    "LifecyclePolicy",
    "LifecycleService",
    "LifecycleStorageError",
    "LifecycleValidationError",
    "OverrideRecord",
    "PolicyRegistry",
    "ReasonRecord",
    "RunTerminalEnvelope",
    "SameStatusRule",
    "TerminalCallbackHandler",
    "TerminalCallbackRegistry",
    "TransitionCommand",
    "TransitionOutcome",
    "TransitionResultKind",
    "build_default_registry",
)


class LifecycleError(RuntimeError):
    """Base class for lifecycle service errors."""


class LifecycleValidationError(LifecycleError, ValueError):
    """An invalid command: unknown entity/status/edge/reason/patch field, or
    a malformed override, before any database mutation."""


class LifecycleNotFoundError(LifecycleError, LookupError):
    """The targeted entity row does not exist."""


class LifecycleStorageError(LifecycleError):
    """A database failure propagated after rollback; never converted to a
    ``conflict`` outcome."""


class LifecycleService(Protocol):
    """The typed service boundary: create initial state, then transition it."""

    async def initialize_in_transaction(
        self,
        connection: AsyncConnection,
        command: InitialStateCommand,
    ) -> str: ...

    async def transition(self, command: TransitionCommand) -> TransitionOutcome: ...
