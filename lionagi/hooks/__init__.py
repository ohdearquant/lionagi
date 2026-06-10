# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0023: unified hook system.

The :class:`HookBus` consolidates three previously-disjoint hook systems
(iModel ``HookRegistry``, agent ``hook_handlers``, CLI ``_on_message``
closures) into one event bus. Migration of those systems is staged —
this package ships the bus, the hook-point vocabulary, and the
declarative agent-YAML loader. The CLI / service / agent wiring lands
in follow-up PRs (ADR-0023b/c) so the existing hot paths aren't
disturbed mid-flight.

Public surface:

* :class:`HookPoint` — the enumerated event vocabulary.
* :class:`HookBus` — per-session pub/sub registry.
* :class:`HookSignal` — typed envelope recording an emission onto the observer.
* :func:`hook` — decorator for registering custom handlers.
* :class:`StopHook` — handler may raise to abort siblings on the same point.
* :func:`load_hooks_for_agent` — build a HookBus from an agent profile.

Per ADR-0076 the bus is re-based onto the session observer: it is the
ordered/blocking dispatch *discipline* over the observer's single event
*transport*, not a parallel bus.
"""

from __future__ import annotations

from .bus import HookBus, HookPoint, HookSignal, StopHook, hook
from .loader import (
    DEFAULT_HOOKS,
    build_session_bus,
    load_hooks_for_agent,
    register_handler,
    resolve_handler,
)
from .persist import route_message_persistence, unroute_message_persistence

__all__ = [
    "HookBus",
    "HookPoint",
    "HookSignal",
    "StopHook",
    "hook",
    "DEFAULT_HOOKS",
    "build_session_bus",
    "load_hooks_for_agent",
    "register_handler",
    "resolve_handler",
    "route_message_persistence",
    "unroute_message_persistence",
]
