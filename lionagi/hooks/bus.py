# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""HookBus and HookPoint vocabulary for session lifecycle hooks."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import Field

from lionagi.ln.concurrency import maybe_await
from lionagi.session.signal import Signal

if TYPE_CHECKING:
    from lionagi.session.observer import SessionObserver

logger = logging.getLogger("lionagi.hooks")

_emitting_bus: ContextVar[HookBus | None] = ContextVar("emitting_hook_bus", default=None)


def _current_emitting_bus() -> HookBus | None:
    return _emitting_bus.get()


__all__ = (
    "HookPoint",
    "HookBus",
    "HookSignal",
    "HookHandler",
    "StopHook",
    "hook",
    "DORMANT_POINTS",
)


class HookPoint(str, Enum):
    """Closed vocabulary of session lifecycle hook points (see docs/reference/agent-hooks.md)."""

    SESSION_START = "session.start"
    SESSION_END = "session.end"
    BRANCH_CREATE = "branch.create"
    BRANCH_END = "branch.end"
    API_PRE_CALL = "api.pre_call"
    API_POST_CALL = "api.post_call"
    API_STREAM_CHUNK = "api.stream_chunk"
    TOOL_PRE = "tool.pre"
    TOOL_POST = "tool.post"
    TOOL_ERROR = "tool.error"
    MESSAGE_ADD = "message.add"
    ARTIFACT_CREATED = "artifact.created"
    USER_PROMPT_SUBMIT = "prompt.submit"


# Members with no production emit site still register (never fire) — DORMANT_POINTS warns instead of silently no-op'ing.
DORMANT_POINTS = frozenset({HookPoint.ARTIFACT_CREATED})

# These points propagate handler exceptions instead of logging them, so a handler can veto the action.
_BLOCKING_POINTS = frozenset({HookPoint.TOOL_PRE, HookPoint.USER_PROMPT_SUBMIT})


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
    return HookPoint(point)


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
        if point in DORMANT_POINTS:
            warnings.warn(
                f"Registering a handler on HookPoint.{point.name} ({point.value!r}), which has "
                "no production emit site — this handler will never fire. See "
                "docs/reference/agent-hooks.md for the current wiring status.",
                UserWarning,
                stacklevel=2,
            )
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
                await maybe_await(handler(**kwargs))
            except StopHook:
                break
            except Exception as exc:
                await self._record(
                    point,
                    {
                        **kwargs,
                        "denied": True,
                        "exception": f"{type(exc).__name__}: {exc}",
                    },
                )
                raise
        await self._record(point, kwargs)

    async def emit(self, point: HookPoint | str, /, **kwargs: Any) -> None:
        """Fire handlers sequentially; exceptions logged, not propagated."""
        point = _normalize_point(point)
        if point in _BLOCKING_POINTS:
            await self.blocking_emit(point, **kwargs)
            return
        token = _emitting_bus.set(self)
        try:
            if point is HookPoint.SESSION_END:
                await self.flush_message_retries()
            handlers = list(self._handlers.get(point, []))
            for handler in handlers:
                try:
                    await maybe_await(handler(**kwargs))
                except StopHook:
                    break
                except Exception:  # noqa: BLE001 — hook isolation invariant
                    logger.exception("Hook failed: %s", point.value)
        finally:
            _emitting_bus.reset(token)
        # MESSAGE_ADD already reaches the signal bus via MessageAdded; recording it here would duplicate it.
        if point is not HookPoint.MESSAGE_ADD:
            await self._record(point, kwargs)

    async def flush_message_retries(self) -> None:
        """Flush default message-hook retries before terminal lifecycle work."""
        for retry_queue in getattr(self, "_message_retry_queues", {}).values():
            await retry_queue.flush()


def hook(point: HookPoint | str) -> Callable[[HookHandler], HookHandler]:
    """Tag a callable as a handler for *point* (registered at load time)."""
    point_enum = point if isinstance(point, HookPoint) else HookPoint(point)

    def _wrap(fn: HookHandler) -> HookHandler:
        fn.__lionagi_hook_point__ = point_enum  # type: ignore[attr-defined]
        return fn

    return _wrap
