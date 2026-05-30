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
dispatch to type-subscribed observers. The gate is where charter/gate
mediation plugs in; an observer firing is the honored capability.

Dispatch keys off the *payload* type. When the emitted object is a
:class:`Signal`, the key is ``type(signal.data)`` and handlers/conditions
receive ``signal.data``; the full envelope is still stored in the Flow for
audit. Anything else is dispatched by its own type. So
``session.observe(MyModel)`` fires for ``branch.emit(StructuredOutput(data=MyModel(...)))``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from lionagi.protocols._concepts import Observable, Observer

from ..protocols.generic.flow import Flow
from ..protocols.generic.progression import Progression
from .signal import Signal

__all__ = ("SessionObserver",)

Handler = Callable[[Any, "SessionObserver"], Any]
Condition = Callable[[Any], bool]
Gate = Callable[[Any], Any]


def _payload(obj: Any) -> Any:
    """The value handlers/conditions see: a Signal's data, else the object."""
    return obj.data if isinstance(obj, Signal) else obj


class SessionObserver(Observer):
    """Typed, reactive event dispatch over a session-scoped Flow."""

    def __init__(self, session: Any = None) -> None:
        self.session = session
        self.flow: Flow = Flow(name="session-events")
        self._handlers: dict[type, list[Handler]] = {}
        self._routes: list[tuple[Condition, str]] = []
        self._gate: Gate | None = None

    # -- Registration ---------------------------------------------------------

    def observe(self, event_type: type, handler: Handler | None = None) -> Any:
        """Subscribe a handler to a payload type. Usable as a decorator.

        ``event_type`` is the *payload* type — the type carried by an emitted
        ``Signal.data``, or the type of a directly-emitted object. Handlers
        receive ``(payload, ctx)``.
        """

        def _register(fn: Handler) -> Handler:
            self._handlers.setdefault(event_type, []).append(fn)
            return fn

        return _register if handler is None else _register(handler)

    def route(self, condition: Condition, *, into: str) -> SessionObserver:
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
        key = type(payload)

        allowed = True
        if self._gate is not None:
            verdict = self._gate(payload)
            if inspect.isawaitable(verdict):
                verdict = await verdict
            allowed = bool(verdict)

        self.flow.add_item(event)  # always recorded (audit trail)
        if not allowed:
            return []

        for condition, name in self._routes:
            if condition(payload):
                self._ensure_stream(name).append(event)

        # Handlers receive (payload, ctx) where ctx is the bound Session when
        # one is attached, else the observer itself.
        ctx = self.session if self.session is not None else self
        results: list[Any] = []
        for handler in self._handlers.get(key, []):
            out = handler(payload, ctx)
            if inspect.isawaitable(out):
                out = await out
            results.append(out)
        return results

    # -- Reads ----------------------------------------------------------------

    def stream(self, name: str) -> list[Any]:
        """Events in a named condition-stream, in arrival order."""
        try:
            prog = self.flow.get_progression(name)
        except Exception:
            return []
        return [self.flow.items[uid] for uid in prog]

    def by_type(self, event_type: type) -> list[Any]:
        """Stored items whose payload is an instance of ``event_type``."""
        return [e for e in self.flow.items if isinstance(_payload(e), event_type)]

    def _ensure_stream(self, name: str) -> Progression:
        try:
            return self.flow.get_progression(name)
        except Exception:
            prog = Progression(name=name)
            self.flow.add_progression(prog)
            return prog

    def __repr__(self) -> str:
        subs = {t.__name__: len(h) for t, h in self._handlers.items()}
        return f"SessionObserver(events={len(self.flow.items)}, handlers={subs})"
