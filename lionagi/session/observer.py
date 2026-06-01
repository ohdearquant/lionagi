# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""SessionObserver — reactive, typed event dispatch over a session's Flow.

The complement to ``Exchange``: where Exchange is *addressed pull* messaging
("send this Message to branch B"), SessionObserver is *typed push* reaction
("when any DepthRequested appears, run this handler").

Usage::

    obs = SessionObserver(session)

    @obs.observe(DepthRequested)            # register a reaction (setup)
    async def on_depth(event, obs):
        branch = obs.session.new_branch()
        return await branch.operate(instruction=event.question)

    obs.route(lambda e: e.novelty > 0.7, into="high_novelty")   # condition stream
    obs.gate(my_permission_check)           # governance seam (request_permission)

    await obs.emit(DepthRequested(question="..."))   # a tool/branch emits

``emit`` runs the chain: gate (govern) → store in Flow → route to streams →
dispatch to Filter-subscribed observers. The gate is where charter/gate
mediation plugs in; an observer firing is the honored capability.

Subscriptions are :class:`Filter`s. ``observe(MyModel)`` is sugar for a
``TypeFilter`` — it fires when the payload *is* a ``MyModel`` or carries a
``MyModel``-typed field, handing the matched instance to the handler.
``observe(spec.q == "rose")`` is a ``SpecFilter`` — it fires on a named field's
value, handing the payload. When the emitted object is a :class:`Signal`, the
filter is applied to ``signal.data``; the full envelope is stored in the Flow.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from lionagi.ln.types import Filter, RoleFilter, TypeFilter, as_filter
from lionagi.protocols._concepts import Observable, Observer

from ..protocols.generic.flow import Flow
from ..protocols.generic.progression import Progression
from .signal import Signal

__all__ = ("SessionObserver", "RoleFilter")

Handler = Callable[[Any, "SessionObserver"], Any]
Predicate = Callable[[Any], bool]
Gate = Callable[[Any], Any]


class _KeyAndRoleFilter(Filter):
    """Conjunction of a payload filter (key) and a role filter (event envelope).

    Used when ``observe(SomeType, role="researcher")`` is called — the type is
    checked against the *payload* and the role against the *event* envelope.
    Plain ``Filter.__and__`` composition would pass the payload to both, which
    silently drops role matches because the payload carries no ``emitter_role``.
    """

    __slots__ = ("_key", "_role")

    def __init__(self, key: Filter, role: RoleFilter) -> None:
        self._key = key
        self._role = role

    def matches(self, payload: Any) -> list[Any]:
        # This is only called directly (not via _match) when used in non-Session
        # contexts; return key matches since role context is unavailable.
        return list(self._key.matches(payload))

    def __repr__(self) -> str:
        return f"({self._key!r} & {self._role!r})"


def _payload(obj: Any) -> Any:
    """The value handlers/filters see: a Signal's data, else the object."""
    return obj.data if isinstance(obj, Signal) else obj


class SessionObserver(Observer):
    """Typed, reactive event dispatch over a session-scoped Flow."""

    def __init__(self, session: Any = None) -> None:
        self.session = session
        self.flow: Flow = Flow(name="session-events")
        self._subs: list[tuple[Filter, Handler]] = []
        self._routes: list[tuple[Predicate, str]] = []
        self._gate: Gate | None = None
        self._pending_tasks: list[asyncio.Task] = []

    # -- Registration ---------------------------------------------------------

    def observe(
        self,
        key: type | Filter | Predicate | None = None,
        handler: Handler | None = None,
        *,
        role: str | None = None,
    ) -> Any:
        """Subscribe a handler. Usable as a decorator.

        ``key`` is a type (→ ``TypeFilter``), a ``Filter`` (e.g.
        ``spec.q == value``), or a plain predicate. Handlers receive
        ``(matched, ctx)`` — for a type, the matched instance (the payload or a
        matching field); for a value/predicate filter, the payload.

        ``role`` subscribes by the emitting agent's role name (a ``RoleFilter``).
        When both ``key`` and ``role`` are provided the filter is their conjunction.
        """
        if role is not None:
            role_flt = RoleFilter(role)
            flt: Filter = role_flt if key is None else _KeyAndRoleFilter(as_filter(key), role_flt)
        elif key is not None:
            flt = as_filter(key)
        else:
            raise TypeError("observe() requires at least one of 'key' or 'role'")

        def _register(fn: Handler) -> Handler:
            self._subs.append((flt, fn))
            return fn

        return _register if handler is None else _register(handler)

    def unobserve(self, handler: Handler) -> int:
        """Remove every subscription whose handler is *handler*.

        Returns the number removed. Used to bound a subscription's lifetime
        (e.g. a flow that subscribes for its duration then detaches).
        """
        before = len(self._subs)
        self._subs = [(f, h) for (f, h) in self._subs if h is not handler]
        return before - len(self._subs)

    def route(self, condition: Predicate, *, into: str) -> SessionObserver:
        """Auto-append events matching ``condition`` to a named stream."""
        self._routes.append((condition, into))
        return self

    def gate(self, check: Gate) -> SessionObserver:
        """Set the permission gate run before an emitted event is honored.

        This is the governance seam — charter/gate mediation lives here.
        Return falsy (or raise) to deny; the event is recorded but no
        observers fire.
        """
        self._gate = check
        return self

    # -- Emission / dispatch --------------------------------------------------

    async def emit(self, event: Observable) -> list[Any]:
        """Run the chain: gate → store → route → dispatch. Returns handler results.

        ``event`` is any Observable — a :class:`Signal` (whose ``data`` is the
        payload) or a bare element. Gate, route-conditions, and handlers all
        operate on the *payload*; the full envelope is stored in the Flow.
        """
        payload = _payload(event)

        # The gate may deny by returning falsy OR by raising — both deny while
        # the event is still recorded below (documented audit contract).
        allowed = True
        if self._gate is not None:
            try:
                verdict = self._gate(payload)
                if inspect.isawaitable(verdict):
                    verdict = await verdict
                allowed = bool(verdict)
            except Exception:
                allowed = False

        self.flow.add_item(event)  # always recorded (audit trail)
        if not allowed:
            return []

        for condition, name in self._routes:
            if condition(payload):
                self._ensure_stream(name).append(event)

        # Each subscription is a Filter; it yields the matched value(s) handed
        # to the handler. ctx is the bound Session when attached, else self.
        # Async handlers run concurrently via asyncio.gather so a slow handler
        # does not serialise subsequent emissions on the same event.
        ctx = self.session if self.session is not None else self
        sync_results: list[Any] = []
        coros: list[Any] = []
        for flt, handler in self._subs:
            for matched in self._match(flt, event, payload):
                out = handler(matched, ctx)
                if inspect.isawaitable(out):
                    coros.append(out)
                else:
                    sync_results.append(out)
        if coros:
            from lionagi.ln.concurrency import gather as _gather

            async_results: list[Any] = list(await _gather(*coros))
        else:
            async_results = []
        return sync_results + async_results

    @staticmethod
    def _match(flt: Filter, event: Any, payload: Any) -> list[Any]:
        """Matched values for a filter against an emitted event.

        Filters normally run on the *payload* (a Signal's ``data``). Two
        exceptions:
        - A ``TypeFilter`` for a Signal subtype matches the envelope itself so
          lifecycle signals (``RunEnd``, etc.) are observable by their own type.
        - A ``RoleFilter`` matches the envelope to access ``emitter_role``; the
          handler receives the payload (Signal.data), not the envelope.
        """
        if isinstance(flt, RoleFilter):
            return flt.matches(event)
        if isinstance(flt, _KeyAndRoleFilter):
            if not flt._role.matches(event):
                return []
            return list(flt._key.matches(payload))
        matched = list(flt.matches(payload))
        if event is not payload and isinstance(flt, TypeFilter) and isinstance(event, flt.type_):
            matched.append(event)
        return matched

    # -- Reads ----------------------------------------------------------------

    def stream(self, name: str) -> list[Any]:
        """Events in a named condition-stream, in arrival order."""
        try:
            prog = self.flow.get_progression(name)
        except Exception:
            return []
        return [self.flow.items[uid] for uid in prog]

    def by_type(self, event_type: type) -> list[Any]:
        """Stored items whose payload is — or carries a field of — ``event_type``.

        Also matches the envelope by exact type, so lifecycle signals
        (``by_type(RunEnd)``) are retrievable like dispatch-time subscriptions.

        Distinct from ``pile[type]`` (which matches at the item level): this
        UNWRAPS a Signal to its ``data`` payload first, so a capability *bundle*
        whose ``data`` carries an ``event_type`` field still matches. Pile stays
        Signal-ignorant by design; this Signal-aware query layers on top.
        """
        flt = TypeFilter(event_type)
        return [e for e in self.flow.items if self._match(flt, e, _payload(e))]

    def _ensure_stream(self, name: str) -> Progression:
        try:
            return self.flow.get_progression(name)
        except Exception:
            prog = Progression(name=name)
            self.flow.add_progression(prog)
            return prog

    def __repr__(self) -> str:
        return f"SessionObserver(events={len(self.flow.items)}, subscriptions={len(self._subs)})"
