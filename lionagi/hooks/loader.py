# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0023 hook registry + agent-YAML loader; profile overrides replace defaults per point."""

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

# Default wiring per ADR-0023 §"Default hooks (no configuration needed)".
DEFAULT_HOOKS: dict[HookPoint, list[HookHandler]] = {
    HookPoint.SESSION_START: [_builtins.persist_session_start],
    HookPoint.SESSION_END: [_builtins.persist_session_end],
    HookPoint.MESSAGE_ADD: [_builtins.persist_message],
    HookPoint.BRANCH_CREATE: [_builtins.persist_branch_provenance],
}


_REGISTRY: dict[str, HookHandler] = {
    "persist_session_start": _builtins.persist_session_start,
    "persist_session_end": _builtins.persist_session_end,
    "persist_branch_provenance": _builtins.persist_branch_provenance,
    "persist_message": _builtins.persist_message,
    "log_api_metrics": _builtins.log_api_metrics,
    "log_tool_use": _builtins.log_tool_use,
}


def register_handler(name: str, handler: Callable[..., Awaitable[Any]]) -> None:
    """Register a callable under ``name`` for agent-YAML lookup; last writer wins."""
    _REGISTRY[name] = handler


def resolve_handler(name: str) -> HookHandler:
    """Look up ``name`` in the registry; raises KeyError if missing."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown hook handler {name!r}. Registered: {sorted(_REGISTRY)}") from exc


def registered_handlers() -> list[str]:
    """Sorted list of registered handler names (for diagnostics)."""
    return sorted(_REGISTRY)


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
                f"Unknown hook point {point_str!r}; valid: {sorted(p.value for p in HookPoint)}"
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
    """Construct a per-session HookBus with defaults merged with profile overrides (ADR-0023)."""
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
