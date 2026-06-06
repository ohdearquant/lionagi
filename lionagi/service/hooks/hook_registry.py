# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any, TypeVar

from lionagi.ln.concurrency import get_cancelled_exc_class
from lionagi.ln.types import Undefined
from lionagi.protocols.types import Event, EventStatus

from ._types import HookDict, HookEventTypes, StreamHandlers
from ._utils import get_handler, validate_hooks, validate_stream_handlers

E = TypeVar("E", bound=Event)
F = TypeVar("F", bound=Callable)


def _normalize_hook_key(key: HookEventTypes | str) -> HookEventTypes | str:
    """Accept the documented string aliases for hook keys.

    ``HookDict`` is typed with string keys ("pre_invoke", "post_invoke",
    "pre_event_create"), but ``validate_hooks`` requires
    :class:`HookEventTypes` members. Callers that construct the
    registry from a plain dict were rejected at validation despite the
    advertised contract; this helper coerces known strings before
    validation runs.
    """
    if isinstance(key, HookEventTypes):
        return key
    if isinstance(key, str):
        aliases = {
            "pre_invoke": HookEventTypes.PreInvocation,
            "post_invoke": HookEventTypes.PostInvocation,
            "pre_event_create": HookEventTypes.PreEventCreate,
            # Legacy alias — decorator name on HookRegistry is
            # `pre_event_create_hook`, so callers constructing via dict often
            # use this string. Preserve both spellings for backward compat.
            "pre_event_create_hook": HookEventTypes.PreEventCreate,
        }
        if key in aliases:
            return aliases[key]
        try:
            return HookEventTypes(key)
        except ValueError:
            return key
    return key


class HookRegistry:
    def __init__(
        self,
        hooks: HookDict = None,
        stream_handlers: StreamHandlers = None,
    ):
        _hooks = {}
        _stream_handlers = {}

        if hooks is not None:
            hooks = {_normalize_hook_key(k): v for k, v in hooks.items()}
            validate_hooks(hooks)
            _hooks.update(hooks)

        if stream_handlers is not None:
            validate_stream_handlers(stream_handlers)
            _stream_handlers.update(stream_handlers)

        self._hooks = _hooks
        self._stream_handlers = _stream_handlers

    def pre_event_create_hook(self, fn: F) -> F:
        """Decorator that registers *fn* as the pre_event_create hook."""
        key = HookEventTypes.PreEventCreate
        if key in self._hooks:
            warnings.warn(f"Overwriting existing {key.value} hook", stacklevel=2)
        self._hooks[key] = fn
        return fn

    def pre_invoke(self, fn: F) -> F:
        """Decorator that registers *fn* as the pre_invocation hook."""
        key = HookEventTypes.PreInvocation
        if key in self._hooks:
            warnings.warn(f"Overwriting existing {key.value} hook", stacklevel=2)
        self._hooks[key] = fn
        return fn

    def post_invoke(self, fn: F) -> F:
        """Decorator that registers *fn* as the post_invocation hook."""
        key = HookEventTypes.PostInvocation
        if key in self._hooks:
            warnings.warn(f"Overwriting existing {key.value} hook", stacklevel=2)
        self._hooks[key] = fn
        return fn

    async def _call(
        self,
        ht_: HookEventTypes,
        ct_: str | type,
        ch_: Any,
        ev_: E | type[E],
        /,
        **kw,
    ) -> tuple[Any | Exception, bool]:
        if ht_ is None and ct_ is None:
            raise RuntimeError("Either hook_type or chunk_type must be provided")
        if ht_ and (self._hooks.get(ht_)):
            validate_hooks({ht_: self._hooks[ht_]})
            h = get_handler(self._hooks, ht_, True)
            return await h(ev_, **kw)
        elif not ct_:
            raise RuntimeError("Hook type is required when chunk_type is not provided")
        else:
            handler = self._stream_handlers.get(ct_)
            if handler is None:
                raise RuntimeError(f"No stream handler registered for {ct_}")
            validate_stream_handlers({ct_: handler})
            h = get_handler(self._stream_handlers, ct_, True)
            return await h(ev_, ct_, ch_, **kw)

    async def _call_stream_handler(
        self,
        ct_: str | type,
        ch_: Any,
        ev_,
        /,
        **kw,
    ):
        handler = self._stream_handlers.get(ct_)
        if handler is None:
            raise RuntimeError(f"No stream handler registered for {ct_}")
        validate_stream_handlers({ct_: handler})
        handler = get_handler(self._stream_handlers, ct_, True)
        return await handler(ev_, ct_, ch_, **kw)

    async def pre_event_create(
        self,
        event_type: type[E],
        /,
        exit: bool = False,  # noqa: A002
        **kw,
    ) -> tuple[E | Exception | None, bool, EventStatus]:
        """Hook to be called before an event is created.

        Typically used to modify or validate the event creation parameters.

        The hook function takes an event type and any additional keyword arguments.
        It can:
            - return an instance of the event type
            - return None if no event should be created during handling, event will be
                created in corresponding default manner
            - raise an exception if this event should be cancelled
                (status: cancelled, reason: f"pre-event-create hook aborted this event: {e}")
        """
        try:
            res = await self._call(
                HookEventTypes.PreEventCreate,
                None,
                None,
                event_type,
                exit=exit,
                **kw,
            )
            return (res, False, EventStatus.COMPLETED)
        except get_cancelled_exc_class() as e:
            return ((Undefined, e), True, EventStatus.CANCELLED)
        except Exception as e:
            return (e, exit, EventStatus.CANCELLED)

    async def pre_invocation(
        self,
        event: E,
        /,
        exit: bool = False,  # noqa: A002
        **kw,
    ) -> tuple[Any, bool, EventStatus]:
        """Hook to be called when an event is dequeued and right before it is invoked.

        Typically used to check permissions.

        The hook function takes the content of the event as a dictionary.
        It can either raise an exception to abort the event invocation or pass to continue (status: cancelled).
        It cannot modify the event itself, and won't be able to access the event instance.
        """
        try:
            res = await self._call(
                HookEventTypes.PreInvocation,
                None,
                None,
                event,
                exit=exit,
                **kw,
            )
            return (res, False, EventStatus.COMPLETED)
        except get_cancelled_exc_class() as e:
            return ((Undefined, e), True, EventStatus.CANCELLED)
        except Exception as e:
            return (e, exit, EventStatus.CANCELLED)

    async def post_invocation(
        self,
        event: E,
        /,
        exit: bool = False,  # noqa: A002
        **kw,
    ) -> tuple[None | Exception, bool, EventStatus]:
        """Hook to be called right after event finished its execution.
        It can either raise an exception to abort the event invocation or pass to continue (status: aborted).
        It cannot modify the event itself, and won't be able to access the event instance.
        """
        try:
            res = await self._call(
                HookEventTypes.PostInvocation,
                None,
                None,
                event,
                exit=exit,
                **kw,
            )
            return (res, False, EventStatus.COMPLETED)
        except get_cancelled_exc_class() as e:
            return ((Undefined, e), True, EventStatus.CANCELLED)
        except Exception as e:
            return (e, exit, EventStatus.ABORTED)

    async def handle_streaming_chunk(
        self,
        chunk_type: str | type,
        chunk: Any,
        /,
        exit: bool = False,  # noqa: A002
        **kw,
    ) -> tuple[Any, bool, EventStatus | None]:
        """Hook to be called to consume streaming chunks.

        Typically used for logging or stream event abortion.

        The handler function signature should be: `async def handler(chunk: Any) -> None`
        It can either raise an exception to mark the event invocation as "failed" or pass to continue (status: aborted).
        """
        try:
            res = await self._call_stream_handler(
                chunk_type,
                chunk,
                None,
                exit=exit,
                **kw,
            )
            return (res, False, None)
        except get_cancelled_exc_class() as e:
            return ((Undefined, e), True, EventStatus.CANCELLED)
        except Exception as e:
            return (e, exit, EventStatus.ABORTED)

    async def call(
        self,
        event_like: Event | type[Event],
        /,
        *,
        hook_type: HookEventTypes = None,
        chunk_type=None,
        chunk=None,
        exit=False,  # noqa: A002
        **kw,
    ):
        """Call a hook or stream handler.

        If method is provided, it will call the corresponding hook.
        If chunk_type is provided, it will call the corresponding stream handler.
        If both are provided, method will be used.
        """
        if hook_type is None and chunk_type is None:
            raise ValueError("Either method or chunk_type must be provided")
        if hook_type:
            # Align with AssociatedEventInfo
            meta = {"lion_class": event_like.class_name(full=True)}
            match hook_type:
                case HookEventTypes.PreEventCreate:
                    return (
                        await self.pre_event_create(event_like, exit=exit, **kw),
                        meta,
                    )
                case HookEventTypes.PreInvocation:
                    meta["event_id"] = str(event_like.id)
                    meta["event_created_at"] = event_like.created_at
                    return (
                        await self.pre_invocation(event_like, exit=exit, **kw),
                        meta,
                    )
                case HookEventTypes.PostInvocation:
                    meta["event_id"] = str(event_like.id)
                    meta["event_created_at"] = event_like.created_at
                    return (
                        await self.post_invocation(event_like, exit=exit, **kw),
                        meta,
                    )
        return await self.handle_streaming_chunk(chunk_type, chunk, exit=exit, **kw)

    def _can_handle(
        self,
        /,
        *,
        ht_: HookEventTypes = None,
        ct_=None,
    ) -> bool:
        """Check if the registry can handle the given event or chunk type."""
        if ht_:
            return ht_ in self._hooks
        if ct_:
            return ct_ in self._stream_handlers
        return False
