# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Route per-branch live persistence through the session hook bus (ADR-0023b).

The CLI used to register its message-persist callback directly on
``branch.on_message_added`` — a parallel dispatch path beside the observer. This
moves it onto the one transport: the branch emits ``MESSAGE_ADD`` (ordered,
awaited) and a thin per-branch handler on ``session.hooks`` invokes the SAME
persist callback. The existing callback body is unchanged; only its dispatch
route moves, so the migration carries no persistence-logic risk.

The built-in ``persist_message`` default is disabled here because the CLI
callback is richer (resume-dedup, lazy per-branch row creation, dual
progression) — there must be exactly one persistence path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .bus import HookHandler, HookPoint


def route_message_persistence(
    session: Any,
    branch: Any,
    on_message: Callable[[Any], Awaitable[None]],
) -> HookHandler:
    """Route ``on_message`` for ``branch`` through the session hook bus.

    Replaces ``branch.on_message_added.append(on_message)``. Ensures the session
    bus exists, disables the default ``persist_message`` (the CLI owns
    persistence), attaches the bus to the branch so it emits ``MESSAGE_ADD``, and
    registers a handler scoped to this branch. Returns the registered handler so
    the caller can detach it at teardown.
    """
    from .builtins import persist_message

    bus = session.hooks  # lazily builds the bus, bound to session.observer
    # Exactly one persistence path: the CLI callback supersedes the built-in.
    bus.off(HookPoint.MESSAGE_ADD, persist_message)
    branch._hooks = bus  # enable Branch._persist_via_bus to emit MESSAGE_ADD
    # Register the async emit hook here (not at construction): the sync
    # add_message path rejects async callbacks, and the construction-time system
    # message goes through it. By the time we wire persistence we are in an async
    # context and only a_add_message runs, so the hook is safe.
    if branch._persist_via_bus not in branch.on_message_added:
        branch.on_message_added.append(branch._persist_via_bus)
    bid = str(branch.id)

    async def _handler(message: Any = None, *, branch_id: str | None = None, **_: Any) -> None:
        # Per-branch demux on the shared session bus: persist only THIS branch's
        # messages. ``_persist_via_bus`` always supplies ``branch_id``; a direct
        # call without it (e.g. a test invoking the handler with a controlled
        # message) targets this branch unconditionally.
        if branch_id is None or branch_id == bid:
            await on_message(message)

    bus.on(HookPoint.MESSAGE_ADD, _handler)
    return _handler


def unroute_message_persistence(holder: Any, handler: HookHandler) -> None:
    """Detach a handler registered by :func:`route_message_persistence`.

    ``holder`` is the branch whose persistence was routed (it carries the shared
    session bus as ``_hooks`` and the ``_persist_via_bus`` emit hook). Removes the
    handler from the bus and the emit hook from the branch so neither fires after
    teardown.
    """
    bus = getattr(holder, "_hooks", None)
    if bus is not None:
        bus.off(HookPoint.MESSAGE_ADD, handler)
    # Remove the emit hook by its underlying function, not object identity:
    # ``holder._persist_via_bus`` makes a fresh bound-method object on each
    # access, so an ``is`` comparison against a re-fetched method never matches.
    emit_func = getattr(type(holder), "_persist_via_bus", None)
    added = getattr(holder, "on_message_added", None)
    if emit_func is not None and isinstance(added, list):
        added[:] = [h for h in added if getattr(h, "__func__", None) is not emit_func]
