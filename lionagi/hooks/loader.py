# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0023 hook registry + agent-YAML loader.

The loader maintains a name → handler registry so agent profiles can
reference handlers as strings::

    hooks:
      session.start:
        - persist_session_start
      api.post_call:
        - log_api_metrics

The registry is pre-populated with the built-ins from
:mod:`lionagi.hooks.builtins`. User-defined handlers (decorated with
:func:`lionagi.hooks.bus.hook`) register via :func:`register_handler`.

:func:`build_session_bus` implements the override semantics from the
ADR: when a profile mentions a hook point, the profile's list REPLACES
the default for that point (so ``message.add: []`` actually disables
the built-in persistence). Hook points the profile doesn't mention keep
their defaults.
"""

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
    """Register a callable under ``name`` for agent-YAML lookup.

    Re-registration overrides — last writer wins. User-defined handlers
    decorated with :func:`bus.hook` typically come in via
    :func:`load_user_handlers` (not yet wired; that's the auto-load path
    deferred to ADR-0023b).
    """
    _REGISTRY[name] = handler


def resolve_handler(name: str) -> HookHandler:
    """Look up ``name`` in the registry. Raises KeyError if missing.

    The bus catches and logs handler exceptions, but a *missing* handler
    is a configuration error — failing loudly at session start is the
    right time.
    """
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
    """Resolve an agent profile's ``hooks`` section to a (point → handlers) map.

    Returns the *override* set, NOT merged with defaults — pass through
    :func:`build_session_bus` to get the merged result. Unknown hook
    point strings raise ``ValueError`` so typos surface at load time
    instead of at first emit.
    """
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
    """Construct a per-session bus with defaults + profile overrides.

    Override semantics: if ``agent_hooks`` mentions a point, the profile's
    list *replaces* the default for that point. Empty list disables the
    default (e.g., ``message.add: []`` turns off persistence). Points the
    profile doesn't mention keep the default.

    ADR-0023 §"Bus lifecycle" — one bus per session, created at session
    init, passed to all branches. Per ADR-0076, ``observer`` binds the bus
    to the session's :class:`SessionObserver` (the shared event transport)
    so emissions are recorded there; pass ``session.observer`` at wiring.
    """
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
