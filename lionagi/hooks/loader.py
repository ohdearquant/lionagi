# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0047 hook registry + agent-YAML loader; profile overrides replace defaults per point."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from . import builtins as _builtins
from .bus import HookBus, HookHandler, HookPoint

logger = logging.getLogger("lionagi.hooks.loader")

__all__ = (
    "DEFAULT_HOOKS",
    "register_handler",
    "resolve_handler",
    "load_hooks_for_agent",
    "build_session_bus",
)

# A Session bus starts with no handlers, so a session used through the SDK writes nothing to the
# state store. A run started through the hub installs the persistence handlers on its own session's
# bus (ADR-0047, amendment of 2026-11-25). Every built-in stays addressable by name below.
DEFAULT_HOOKS: dict[HookPoint, list[HookHandler]] = {}


_REGISTRY: dict[str, HookHandler] = {
    "log_api_metrics": _builtins.log_api_metrics,
    "log_tool_call": _builtins.log_tool_call,
    "log_tool_use": _builtins.log_tool_use,
}

# The state-store persistence handlers are registered under these names by the hub module that
# defines them, when it is imported; the core names them only to say so when one is missing.
_PERSISTENCE_HANDLER_NAMES = frozenset(
    {
        "persist_session_start",
        "persist_session_end",
        "persist_branch_provenance",
        "persist_branch_end",
        "persist_message",
    }
)


def register_handler(name: str, handler: Callable[..., Awaitable[Any]]) -> None:
    """Register a callable under ``name`` for agent-YAML lookup; last writer wins."""
    _REGISTRY[name] = handler


def resolve_handler(name: str) -> HookHandler:
    """Look up ``name`` in the registry; raises KeyError if missing."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        if name in _PERSISTENCE_HANDLER_NAMES:
            raise KeyError(
                f"Hook handler {name!r} is not registered: the state-store persistence handlers "
                "are registered by the module lionhub.orchestration.run_persistence, which must "
                "be imported first."
            ) from exc
        raise KeyError(f"Unknown hook handler {name!r}. Registered: {sorted(_REGISTRY)}") from exc


def _registered_handler(name: str) -> HookHandler | None:
    """The handler registered under ``name``, or None when nothing is."""
    return _REGISTRY.get(name)


def load_hooks_for_agent(
    agent_hooks: dict[str, list[str]] | None,
) -> dict[HookPoint, list[HookHandler]]:
    """Resolve agent profile ``hooks`` section to override map; does NOT merge with defaults."""
    if not agent_hooks:
        return {}
    resolved: dict[HookPoint, list[HookHandler]] = {}
    for point_str, handler_names in agent_hooks.items():
        try:
            point = HookPoint(point_str)
        except ValueError as exc:
            raise ValueError(
                f"Unknown hook point {point_str!r}; valid: {sorted(HookPoint.allowed())}"
            ) from exc
        if not isinstance(handler_names, list):
            raise ValueError(
                f"hooks.{point_str} must be a list of handler names; "
                f"got {type(handler_names).__name__}"
            )
        resolved[point] = [resolve_handler(n) for n in handler_names]
    return resolved


def build_session_bus(
    agent_hooks: dict[str, list[str]] | None = None,
    *,
    observer: Any = None,
) -> HookBus:
    """Construct a per-session HookBus with defaults merged with agent-hook overrides (ADR-0047 D3)."""
    bus = HookBus(observer=observer)
    overrides = load_hooks_for_agent(agent_hooks)

    for point, handlers in DEFAULT_HOOKS.items():
        if point in overrides:
            for handler in overrides[point]:
                bus.on(point, handler)
        else:
            for handler in handlers:
                bus.on(point, handler)

    for point, handlers in overrides.items():
        if point in DEFAULT_HOOKS:
            continue
        for handler in handlers:
            bus.on(point, handler)

    return bus
