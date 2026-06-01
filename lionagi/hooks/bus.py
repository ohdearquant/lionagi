# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0023: HookBus + HookPoint vocabulary.

The bus is intentionally minimal. Handlers register against a
:class:`HookPoint`, the bus dispatches sequentially, and a handler
exception is logged but never aborts the user-facing operation. The one
exception is :class:`StopHook`, which the handler may raise to short-
circuit subsequent handlers on the same point (but still doesn't fail
the surrounding operation).

Isolation invariants (enforced by convention, not code):

* Handlers MUST NOT mutate the kwargs they receive — pass-by-reference
  observers only.
* DB-writing handlers MUST be tolerant of in-flight failure (the bus
  keeps going).
* ``TOOL_PRE`` is the one hook point where a handler may legitimately
  raise to block the underlying operation (e.g., destructive-command
  guard). The bus enforces this via :meth:`HookBus.blocking_emit`, which
  propagates handler exceptions to the caller instead of swallowing them.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import Field

from lionagi.session.signal import Signal

if TYPE_CHECKING:
    from lionagi.session.observer import SessionObserver

logger = logging.getLogger("lionagi.hooks")


class HookPoint(str, Enum):
    """ADR-0023 §"Event payloads" — closed vocabulary of hook points."""

    # Session lifecycle
    SESSION_START = "session.start"
    SESSION_END = "session.end"

    # Branch lifecycle
    BRANCH_CREATE = "branch.create"

    # iModel (API calls)
    API_PRE_CALL = "api.pre_call"
    API_POST_CALL = "api.post_call"
    API_STREAM_CHUNK = "api.stream_chunk"

    # Tool execution
    TOOL_PRE = "tool.pre"
    TOOL_POST = "tool.post"
    TOOL_ERROR = "tool.error"

    # Message lifecycle
    MESSAGE_ADD = "message.add"

    # Artifact production (ADR-0021)
    ARTIFACT_CREATED = "artifact.created"


HookHandler = Callable[..., Awaitable[Any]]


class StopHook(Exception):  # noqa: N818 — control-flow signal, not an error
    """Raised by a handler to skip remaining handlers on this point.

    Does NOT abort the underlying operation — only stops the bus from
    invoking later handlers for the same emit call. Named without the
    ``Error`` suffix on purpose: this is the StopIteration-style control
    flow signal of the hook bus, not a failure indication.
    """


class HookSignal(Signal):
    """A HookBus emission recorded on the observer transport (ADR-0076).

    Carries the named :class:`HookPoint` and the loose ``kwargs`` the
    handlers receive. When a :class:`HookBus` is bound to a session's
    observer, every ``emit`` / ``blocking_emit`` records one of these onto
    the observer's ``Flow`` — so hook activity lives on the single event
    bus and reactive observers may subscribe with ``observe(HookSignal)``.
    The ordered, short-circuiting handler chain is still dispatched by the
    bus itself; this envelope is the *record*, not the dispatcher.
    """

    point: HookPoint | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)


def _normalize_point(point: HookPoint | str) -> HookPoint:
    """Coerce ``point`` to a :class:`HookPoint`, raising ``ValueError`` for unknowns."""
    if isinstance(point, HookPoint):
        return point
    return HookPoint(point)  # raises ValueError for unknown values


class HookBus:
    """Per-session pub/sub bus for the eleven :class:`HookPoint` events.

    Handlers are dispatched sequentially in registration order. A handler
    raising any exception other than :class:`StopHook` is logged and
    skipped — the user-facing operation is never blocked. This trades
    strict error propagation for the operational invariant that
    persistence side-effects must never break the agent loop.

    ``TOOL_PRE`` is the exception: emit() routes it through
    :meth:`blocking_emit` so that a guard handler raising (e.g.
    ``PermissionError``) actually blocks the tool call.
    """

    def __init__(self, observer: SessionObserver | None = None) -> None:
        self._handlers: dict[HookPoint, list[HookHandler]] = {}
        # The shared event transport (ADR-0076). When set, every emit records
        # a HookSignal here so hook activity lives on the one bus. Optional:
        # an unbound HookBus dispatches exactly as before, recording nothing.
        self._observer = observer

    def bind(self, observer: SessionObserver | None) -> HookBus:
        """Bind (or unbind, with ``None``) this bus to a session's observer.

        Returns ``self`` for chaining. The observer is the shared transport
        onto which emissions are recorded; binding does not change dispatch.
        """
        self._observer = observer
        return self

    async def _record(self, point: HookPoint, kwargs: dict[str, Any]) -> None:
        """Record this emission onto the bound observer transport, if any.

        Best-effort and isolated: a transport failure must never turn a
        successful hook dispatch into a failure. Runs *after* the ordered
        chain so dispatch / blocking semantics are unchanged.
        """
        if self._observer is None:
            return
        try:
            await self._observer.emit(HookSignal(point=point, kwargs=dict(kwargs)))
        except Exception:  # noqa: BLE001 — transport recording is best-effort
            logger.exception("HookSignal record failed: %s", point.value)

    def on(self, point: HookPoint | str, handler: HookHandler) -> None:
        """Register ``handler`` to fire on ``point``. Order preserved.

        Accepts a string hook-point value (e.g. ``"session.start"``) and
        normalises it to a :class:`HookPoint`. Raises ``ValueError`` for
        unrecognised strings.
        """
        point = _normalize_point(point)
        self._handlers.setdefault(point, []).append(handler)

    def off(self, point: HookPoint | str, handler: HookHandler) -> None:
        """Unregister ``handler``. No-op if not registered."""
        point = _normalize_point(point)
        handlers = self._handlers.get(point, [])
        if handler in handlers:
            handlers.remove(handler)

    def handlers_for(self, point: HookPoint | str) -> list[HookHandler]:
        """Public read of the registered handlers for ``point``.

        Useful for testing / introspection. Returns a shallow copy so
        callers can't mutate the bus by mutating the returned list.
        """
        point = _normalize_point(point)
        return list(self._handlers.get(point, []))

    async def blocking_emit(self, point: HookPoint | str, /, **kwargs: Any) -> None:
        """Fire all handlers for ``point``, propagating exceptions.

        Unlike :meth:`emit`, handler exceptions are NOT swallowed —
        they propagate to the caller. Intended for ``TOOL_PRE`` where a
        guard handler raising ``PermissionError`` (or similar) should
        actually abort the tool call.

        :class:`StopHook` still short-circuits remaining handlers without
        propagating as an error.
        """
        point = _normalize_point(point)
        handlers = list(self._handlers.get(point, []))
        for handler in handlers:
            try:
                result = handler(**kwargs)
                if inspect.isawaitable(result):
                    await result
            except StopHook:
                break  # stop remaining handlers, but still record below
            # All other exceptions propagate — no except clause here. A
            # propagated (blocking) exception skips the record; deny-audit
            # arrives with the real pre-invoke gate (ADR-0076 Follow-up 1).
        await self._record(point, kwargs)

    async def emit(self, point: HookPoint | str, /, **kwargs: Any) -> None:
        """Fire all handlers for ``point`` sequentially with ``kwargs``.

        Handler exceptions are logged + swallowed. ``StopHook`` short-
        circuits remaining handlers on this point only.

        ``TOOL_PRE`` is routed through :meth:`blocking_emit` so guard
        handlers can block the operation by raising.
        """
        point = _normalize_point(point)
        if point is HookPoint.TOOL_PRE:
            await self.blocking_emit(point, **kwargs)  # records inside
            return
        # Snapshot handlers before dispatch so a handler registered during
        # this emit cycle does not fire in the current cycle.
        handlers = list(self._handlers.get(point, []))
        for handler in handlers:
            try:
                result = handler(**kwargs)
                # Allow sync handlers too — await only if needed.
                if inspect.isawaitable(result):
                    await result
            except StopHook:
                break  # stop remaining handlers, but still record below
            except Exception:  # noqa: BLE001 — hook isolation invariant
                logger.exception("Hook failed: %s", point.value)
        await self._record(point, kwargs)


# ── Decorator for user-defined handlers ───────────────────────────────────────


def hook(point: HookPoint | str) -> Callable[[HookHandler], HookHandler]:
    """Tag a callable as a handler for ``point`` (registered at load time).

    The decorator only marks the function — registration into a bus is
    the loader's job (see :func:`lionagi.hooks.loader.register_handler`).
    This separation lets a single module define multiple handlers without
    side effects at import time.

    Usage::

        @hook(HookPoint.API_POST_CALL)
        async def notify_on_expensive_call(*, tokens, model, **kw):
            ...
    """
    point_enum = point if isinstance(point, HookPoint) else HookPoint(point)

    def _wrap(fn: HookHandler) -> HookHandler:
        # Attribute is consulted by the loader to bind by HookPoint without
        # requiring the user to import the bus.
        fn.__lionagi_hook_point__ = point_enum  # type: ignore[attr-defined]
        return fn

    return _wrap
