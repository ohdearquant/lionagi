# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0047: unified hook bus, point vocabulary, and declarative agent-YAML loader."""

from __future__ import annotations

from .bus import DORMANT_POINTS, HookBus, HookPoint, HookSignal, StopHook, hook
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
    "DORMANT_POINTS",
    "DEFAULT_HOOKS",
    "build_session_bus",
    "load_hooks_for_agent",
    "register_handler",
    "resolve_handler",
    "route_message_persistence",
    "unroute_message_persistence",
]
