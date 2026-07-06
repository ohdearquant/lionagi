from __future__ import annotations

import logging
import threading
import weakref
from typing import TYPE_CHECKING, Any, ClassVar

from lionagi.ln.concurrency import maybe_await

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from lionagi.protocols.generic.event import Event

logger = logging.getLogger(__name__)

__all__ = ("Broadcaster",)


class Broadcaster:
    """Thread-safe singleton pub/sub; subclass and set ``_event_type`` for typed event broadcasting."""

    _instance: ClassVar[Broadcaster | None] = None
    _subscribers: ClassVar[list[weakref.ref]] = []
    _event_type: ClassVar[type[Event]]
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Each subclass gets its own subscriber list, singleton slot, and lock."""
        super().__init_subclass__(**kwargs)
        cls._instance = None
        cls._subscribers = []
        cls._lock = threading.Lock()

    @classmethod
    def subscribe(
        cls,
        callback: Callable[[Any], None] | Callable[[Any], Awaitable[None]],
    ) -> None:
        """Add subscriber (idempotent); bound methods stored as WeakMethod, plain functions as strong refs."""
        with cls._lock:
            for ref in cls._subscribers:
                if ref() is callback:
                    return
            if hasattr(callback, "__self__"):
                cls._subscribers.append(weakref.WeakMethod(callback))
            else:
                cls._subscribers.append(lambda cb=callback: cb)

    @classmethod
    def unsubscribe(
        cls,
        callback: Callable[[Any], None] | Callable[[Any], Awaitable[None]],
    ) -> None:
        """Remove a previously subscribed callback."""
        with cls._lock:
            for weak_ref in list(cls._subscribers):
                if weak_ref() is callback:
                    cls._subscribers.remove(weak_ref)
                    return

    @classmethod
    def _cleanup_dead_refs(
        cls,
    ) -> list:
        """Prune dead weakrefs, return live callbacks. Must hold _lock."""
        callbacks, alive_refs = [], []
        for weak_ref in cls._subscribers:
            if (cb := weak_ref()) is not None:
                callbacks.append(cb)
                alive_refs.append(weak_ref)
        cls._subscribers[:] = alive_refs
        return callbacks

    @classmethod
    async def broadcast(cls, event: Any) -> None:
        """Dispatch event to all live subscribers sequentially; raises ValueError on type mismatch."""
        if not isinstance(event, cls._event_type):
            raise ValueError(f"Event must be of type {cls._event_type.__name__}")
        with cls._lock:
            callbacks = cls._cleanup_dead_refs()
        for callback in callbacks:
            try:
                await maybe_await(callback(event))
            except Exception as e:
                logger.error(f"Error in subscriber callback: {e}", exc_info=True)

    @classmethod
    def get_subscriber_count(cls) -> int:
        """Count live subscribers (triggers dead ref cleanup)."""
        with cls._lock:
            return len(cls._cleanup_dead_refs())
