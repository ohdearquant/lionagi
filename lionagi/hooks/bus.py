# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""HookBus and HookPoint vocabulary for session lifecycle hooks."""

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

__all__ = (
    "HookPoint",
    "HookBus",
    "HookSignal",
    "HookHandler",
    "StopHook",
    "hook",
)


class HookPoint(str, Enum):
    """Closed vocabulary of hook points."""

    SESSION_START = "session.start"
    SESSION_END = "session.end"

    BRANCH_CREATE = "branch.create"
    API_PRE_CALL = "api.pre_call"
    API_POST_CALL = "api.post_call"
    API_STREAM_CHUNK = "api.stream_chunk"
    TOOL_PRE = "tool.pre"
    TOOL_POST = "tool.post"
    TOOL_ERROR = "tool.error"
    MESSAGE_ADD = "message.add"
    ARTIFACT_CREATED = "artifact.created"


HookHandler = Callable[..., Awaitable[Any] | Any]


class StopHook(Exception):  # noqa: N818 — control-flow signal, not an error
    """Raised by a handler to skip remaining handlers on this point."""


class HookSignal(Signal):
    """A HookBus emission recorded on the observer transport."""

    point: HookPoint | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)


def _normalize_point(point: HookPoint | str) -> HookPoint:
    if isinstance(point, HookPoint):
        return point
    return HookPoint(point)  # raises ValueError for unknown values


class HookBus:
    """Per-session pub/sub bus for HookPoint events."""

    def __init__(self, observer: SessionObserver | None = None) -> None:
        self._handlers: dict[HookPoint, list[HookHandler]] = {}
        self._observer = observer

    def bind(self, observer: SessionObserver | None) -> HookBus:
        """Bind (or unbind) this bus to a session's observer. Returns self."""
        self._observer = observer
        return self

    async def _record(self, point: HookPoint, kwargs: dict[str, Any]) -> None:
        """Best-effort record onto the bound observer transport."""
        if self._observer is None:
            return
        try:
            await self._observer.emit(HookSignal(point=point, kwargs=dict(kwargs)))
        except Exception:  # noqa: BLE001 — transport recording is best-effort
            logger.exception("HookSignal record failed: %s", point.value)

    def on(self, point: HookPoint | str, handler: HookHandler) -> None:
        point = _normalize_point(point)
        self._handlers.setdefault(point, []).append(handler)

    def off(self, point: HookPoint | str, handler: HookHandler) -> None:
        point = _normalize_point(point)
        handlers = self._handlers.get(point, [])
        if handler in handlers:
            handlers.remove(handler)

    def handlers_for(self, point: HookPoint | str) -> list[HookHandler]:
        point = _normalize_point(point)
        return list(self._handlers.get(point, []))

    async def blocking_emit(self, point: HookPoint | str, /, **kwargs: Any) -> None:
        """Fire handlers, propagating exceptions (used for TOOL_PRE guards)."""
        point = _normalize_point(point)
        handlers = list(self._handlers.get(point, []))
        for handler in handlers:
            try:
                result = handler(**kwargs)
                if inspect.isawaitable(result):
                    await result
            except StopHook:
                break
        await self._record(point, kwargs)

    async def emit(self, point: HookPoint | str, /, **kwargs: Any) -> None:
        """Fire handlers sequentially; exceptions logged, not propagated."""
        point = _normalize_point(point)
        if point is HookPoint.TOOL_PRE:
            await self.blocking_emit(point, **kwargs)
            return
        handlers = list(self._handlers.get(point, []))
        for handler in handlers:
            try:
                result = handler(**kwargs)
                if inspect.isawaitable(result):
                    await result
            except StopHook:
                break
            except Exception:  # noqa: BLE001 — hook isolation invariant
                logger.exception("Hook failed: %s", point.value)
        await self._record(point, kwargs)


# ── Decorator for user-defined handlers ───────────────────────────────────────


def hook(point: HookPoint | str) -> Callable[[HookHandler], HookHandler]:
    """Tag a callable as a handler for *point* (registered at load time)."""
    point_enum = point if isinstance(point, HookPoint) else HookPoint(point)

    def _wrap(fn: HookHandler) -> HookHandler:
        fn.__lionagi_hook_point__ = point_enum  # type: ignore[attr-defined]
        return fn

    return _wrap
