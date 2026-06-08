# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Reactive, typed event dispatch over a session's Flow."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from lionagi.ln.concurrency import maybe_await
from lionagi.ln.types import Filter, RoleFilter, TypeFilter, all_of
from lionagi.protocols._concepts import Observable, Observer

from ..protocols.generic.flow import Flow
from ..protocols.generic.progression import Progression
from .signal import Signal

__all__ = ("SessionObserver", "RoleFilter")

Handler = Callable[[Any, "SessionObserver"], Any]
Predicate = Callable[[Any], bool]
Gate = Callable[[Any], Any]


class _KeyAndRoleFilter(Filter):
    """Conjunction of a payload filter (key) and a role filter (event envelope)."""

    __slots__ = ("_key", "_role")

    def __init__(self, key: Filter, role: RoleFilter) -> None:
        self._key = key
        self._role = role

    def matches(self, payload: Any) -> list[Any]:
        return list(self._key.matches(payload))

    def __repr__(self) -> str:
        return f"({self._key!r} & {self._role!r})"


def _payload(obj: Any) -> Any:
    return obj.data if isinstance(obj, Signal) else obj


def _is_condition(x: Any) -> bool:
    return isinstance(x, type | Filter) or hasattr(x, "__as_filter__")


def _looks_like_handler(x: Any) -> bool:
    return callable(x) and not _is_condition(x)


class SessionObserver(Observer):
    """Typed, reactive event dispatch over a session-scoped Flow."""

    def __init__(self, session: Any = None) -> None:
        self.session = session
        self.flow: Flow = Flow(name="session-events")
        self._subs: list[tuple[Filter, Handler]] = []
        self._routes: list[tuple[Predicate, str]] = []
        self._gate: Gate | None = None

    def observe(
        self,
        *keys: type | Filter | Predicate | Any,
        handler: Handler | None = None,
        role: str | None = None,
    ) -> Any:
        """Subscribe a handler to AND-composed conditions. Usable as a decorator."""
        keys_list = list(keys)
        if handler is None and len(keys_list) >= 2 and _looks_like_handler(keys_list[-1]):
            handler = keys_list.pop()

        key_flt: Filter | None = all_of(*keys_list) if keys_list else None
        if role is not None:
            role_flt = RoleFilter(role)
            flt: Filter = role_flt if key_flt is None else _KeyAndRoleFilter(key_flt, role_flt)
        elif key_flt is not None:
            flt = key_flt
        else:
            raise TypeError("observe() requires at least one condition or 'role'")

        def _register(fn: Handler) -> Handler:
            self._subs.append((flt, fn))
            return fn

        return _register if handler is None else _register(handler)

    def unobserve(self, handler: Handler) -> int:
        """Remove all subscriptions for handler. Returns count removed."""
        before = len(self._subs)
        self._subs = [(f, h) for (f, h) in self._subs if h is not handler]
        return before - len(self._subs)

    def route(self, condition: Predicate, *, into: str) -> SessionObserver:
        self._routes.append((condition, into))
        return self

    def gate(self, check: Gate) -> SessionObserver:
        """Set the governance gate for event dispatch and pre-invoke authorization."""
        self._gate = check
        return self

    async def authorize(self, action: Any) -> bool:
        """Pre-invoke gate. Returns True when no gate set. Denials recorded as GateDenied."""
        if self._gate is None:
            return True
        try:
            allowed = bool(await maybe_await(self._gate(action)))
        except Exception:
            allowed = False
        if not allowed:
            from .signal import GateDenied

            self.flow.add_item(GateDenied(data=action))
        return allowed

    async def emit(self, event: Any) -> list[Any]:
        """Gate -> store -> route -> dispatch. Returns handler results."""
        if not isinstance(event, Observable):
            event = Signal(data=event)
        payload = _payload(event)

        allowed = True
        if self._gate is not None:
            try:
                allowed = bool(await maybe_await(self._gate(payload)))
            except Exception:
                allowed = False

        self.flow.add_item(event)
        if not allowed:
            return []

        for condition, name in self._routes:
            if condition(payload):
                self._ensure_stream(name).append(event)

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
        """Match filter against event. Handles RoleFilter and TypeFilter specially."""
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

    def stream(self, name: str) -> list[Any]:
        try:
            prog = self.flow.get_progression(name)
        except Exception:
            return []
        return [self.flow.items[uid] for uid in prog]

    def by_type(self, event_type: type) -> list[Any]:
        """Stored items whose payload matches event_type (unwraps Signals)."""
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
