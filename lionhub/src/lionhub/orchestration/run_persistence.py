# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The persistence handlers a hub run installs on its session's hook bus.

A lionagi ``Session`` bus starts with no handlers, so a session used through the SDK writes
nothing to the state store. A run started through the hub (the CLI and the MCP server's agent
runs, by way of :mod:`lionhub.orchestration.runs`) records its session and branches there, so
before it emits a lifecycle point it installs these handlers on its session's bus.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from lionagi.hooks.builtins import (
    persist_branch_end,
    persist_branch_provenance,
    persist_session_end,
    persist_session_start,
)
from lionagi.hooks.bus import HookBus, HookHandler, HookPoint

__all__ = ("RUN_PERSISTENCE_HOOKS", "install_run_persistence")

RUN_PERSISTENCE_HOOKS: Mapping[HookPoint, tuple[HookHandler, ...]] = MappingProxyType(
    {
        HookPoint.SESSION_START: (persist_session_start,),
        HookPoint.SESSION_END: (persist_session_end,),
        HookPoint.BRANCH_CREATE: (persist_branch_provenance,),
        HookPoint.BRANCH_END: (persist_branch_end,),
    }
)


def install_run_persistence(bus: HookBus) -> HookBus:
    """Register each persistence handler on *bus* once, and return *bus*.

    A handler already registered at its point is left where it is, so installing again on the
    same session, as a run's setup and its teardown both do, never registers a handler twice and
    never writes a row twice. Handlers the caller registered stay ahead of these.
    """
    for point, handlers in RUN_PERSISTENCE_HOOKS.items():
        present = bus.handlers_for(point)
        for handler in handlers:
            if handler not in present:
                bus.on(point, handler)
    return bus
