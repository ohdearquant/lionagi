"""The bus: events are emitted, and no handler is awaited inline (ADR-0005/C2).

An in-process observer on one event loop. `emit` records the event, calls every matching handler,
and schedules any coroutine a handler returns as a task the bus tracks. A handler that raises is
recorded in `errors` and never breaks the emitter. `log` holds the last `keep` events and `errors` the
last `keep` failures; a bus with `keep` 0 holds neither.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = (
    "Event",
    "Handler",
    "Bus",
)


@dataclass(frozen=True)
class Event:
    kind: str
    payload: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)


Handler = Callable[[Event], "Awaitable[None] | None"]


class Bus:
    def __init__(self, keep: int = 10_000):
        self._subs: list[tuple[str, Handler]] = []
        self.log: list[Event] = []
        self.errors: list[tuple[Event, Handler, BaseException]] = []
        self.tasks: set[asyncio.Future] = set()
        self.keep = keep

    @staticmethod
    def matches(pattern: str, kind: str) -> bool:
        return pattern == "*" or pattern == kind or (pattern.endswith(".") and kind.startswith(pattern))

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        pair = (pattern, handler)
        self._subs.append(pair)

        def unsubscribe() -> None:
            # by identity: an equal pair is another registration of the same handler, not this one
            for i, p in enumerate(self._subs):
                if p is pair:
                    del self._subs[i]
                    return

        return unsubscribe

    def on(self, pattern: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.subscribe(pattern, fn)
            return fn

        return deco

    def emit(self, kind: str, **payload: Any) -> Event:
        event = Event(kind=kind, payload=payload)
        if self.keep:
            self.log.append(event)
            if len(self.log) > self.keep:
                del self.log[: len(self.log) - self.keep]
        for pattern, handler in list(self._subs):
            if not self.matches(pattern, kind):
                continue
            try:
                result = handler(event)
            except Exception as e:
                self._failed(event, handler, e)
                continue
            if inspect.isawaitable(result):
                self._schedule(event, handler, result)
        return event

    def _failed(self, event: Event, handler: Handler, error: BaseException) -> None:
        # bounded like the log: a subscriber failing on every event of a resident agent would
        # otherwise grow this, and each exception keeps its traceback's frames alive
        if self.keep:
            self.errors.append((event, handler, error))
            if len(self.errors) > self.keep:
                del self.errors[: len(self.errors) - self.keep]

    def _schedule(self, event: Event, handler: Handler, awaitable: Awaitable[None]) -> None:
        # The awaitable itself is tracked, never a wrapper around it: cancelling a tracked task
        # cancels what the handler returned, and a coroutine cancelled before its first step is
        # closed, where a wrapper cancelled that early would never have awaited it.
        try:
            task = asyncio.ensure_future(awaitable, loop=asyncio.get_running_loop())
        except ValueError as e:  # a future bound to another loop: recorded, as a handler that raised
            self._failed(event, handler, e)
            return

        def done(task: asyncio.Future) -> None:
            self.tasks.discard(task)
            if not task.cancelled() and isinstance(e := task.exception(), Exception):
                self._failed(event, handler, e)

        self.tasks.add(task)
        task.add_done_callback(done)

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
            # A task that finished before drain looked has its done callback queued, and gathering
            # finished tasks may return without yielding; one step lets that callback run.
            await asyncio.sleep(0)

    def events(self, pattern: str = "*") -> list[Event]:
        return [e for e in self.log if self.matches(pattern, e.kind)]
