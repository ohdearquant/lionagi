# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0047: route per-branch live persistence through the session hook bus."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .bus import HookHandler, HookPoint

__all__ = (
    "route_message_persistence",
    "unroute_message_persistence",
)


def route_message_persistence(
    session: Any,
    branch: Any,
    on_message: Callable[[Any], Awaitable[None]],
) -> HookHandler:
    """Wire ``on_message`` for ``branch`` through the session bus; returns handler for teardown."""
    from .builtins import persist_message

    bus = session.hooks  # lazily builds the bus, bound to session.observer
    # Exactly one persistence path: the CLI callback supersedes the built-in.
    bus.off(HookPoint.MESSAGE_ADD, persist_message)
    branch._hooks = bus  # enable Branch._persist_via_bus to emit MESSAGE_ADD
    # Register the async emit hook here (not at construction): the sync
    # add_message path used at construction can't accept async callbacks.
    if branch._persist_via_bus not in branch.on_message_added:
        branch.on_message_added.append(branch._persist_via_bus)
    bid = str(branch.id)

    async def _handler(message: Any = None, *, branch_id: str | None = None, **_: Any) -> None:
        # Per-branch demux on the shared session bus: persist only this
        # branch's messages (a direct call w/o branch_id targets it unconditionally).
        if branch_id is None or branch_id == bid:
            await on_message(message)

    bus.on(HookPoint.MESSAGE_ADD, _handler)
    return _handler


def unroute_message_persistence(holder: Any, handler: HookHandler) -> None:
    """Detach handler and emit hook registered by :func:`route_message_persistence`."""
    bus = getattr(holder, "_hooks", None)
    if bus is not None:
        bus.off(HookPoint.MESSAGE_ADD, handler)
    # Remove by underlying function, not identity: a bound-method object is
    # freshly created on each access, so `is` against a re-fetched one never matches.
    emit_func = getattr(type(holder), "_persist_via_bus", None)
    added = getattr(holder, "on_message_added", None)
    if emit_func is not None and isinstance(added, list):
        added[:] = [h for h in added if getattr(h, "__func__", None) is not emit_func]
