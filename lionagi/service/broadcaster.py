from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from lionagi.protocols.generic.event import Event

logger = logging.getLogger(__name__)

__all__ = ("Broadcaster",)


class Broadcaster:
    """Singleton pub/sub with weakref-based automatic subscriber cleanup.

    Subclass and set ``_event_type`` to define typed broadcasters.
    Subscribers are stored as weakrefs (WeakMethod for bound methods)
    so they are automatically cleaned up when the referenced object
    is garbage collected.

    Thread-safe: all subscriber mutations are protected by a class-level lock.

    Example::

        class OrderBroadcaster(Broadcaster):
            _event_type = OrderEvent

        OrderBroadcaster.subscribe(my_handler)
        await OrderBroadcaster.broadcast(OrderEvent(...))
    """

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
        """Add subscriber callback (idempotent).

        Bound methods are stored as weak references (via ``WeakMethod``)
        so that the subscriber is automatically removed when the owning
        object is garbage collected.

        Plain functions, lambdas, and other non-bound callables are stored
        as **strong** references because they have no associated object
        whose lifetime should govern the subscription.  To unsubscribe a
        plain function, call :meth:`unsubscribe` explicitly.

        Args:
            callback: Sync or async callable receiving the event.
        """
        with cls._lock:
            for ref in cls._subscribers:
                if ref() is callback:
                    return
            if hasattr(callback, "__self__"):
                # Bound method — weak reference prevents leaking the object
                cls._subscribers.append(weakref.WeakMethod(callback))
            else:
                # Plain function/lambda — strong reference (prevent silent GC)
                cls._subscribers.append(lambda cb=callback: cb)

    @classmethod
    def unsubscribe(
        cls,
        callback: Callable[[Any], None] | Callable[[Any], Awaitable[None]],
    ) -> None:
        """Remove subscriber callback.

        Args:
            callback: Previously subscribed callback to remove.
        """
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
        """Broadcast event to all subscribers sequentially.

        Args:
            event: Event instance (must match _event_type).

        Raises:
            ValueError: If event type doesn't match _event_type.
        """
        if not isinstance(event, cls._event_type):
            raise ValueError(f"Event must be of type {cls._event_type.__name__}")
        with cls._lock:
            callbacks = cls._cleanup_dead_refs()
        for callback in callbacks:
            try:
                result = callback(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Error in subscriber callback: {e}", exc_info=True)

    @classmethod
    def get_subscriber_count(cls) -> int:
        """Count live subscribers (triggers dead ref cleanup)."""
        with cls._lock:
            return len(cls._cleanup_dead_refs())
