"""The bus: commands are emitted, never awaited by the loop (ADR-0005/C2).

An in-process observer on one event loop. `emit` records the event, calls every matching handler,
and schedules any coroutine a handler returns as a task the bus tracks. A handler that raises is
recorded in `errors` and never breaks the emitter.
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
        self.tasks: set[asyncio.Task] = set()
        self.keep = keep

    @staticmethod
    def matches(pattern: str, kind: str) -> bool:
        return pattern == "*" or pattern == kind or (pattern.endswith(".") and kind.startswith(pattern))

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        pair = (pattern, handler)
        self._subs.append(pair)

        def unsubscribe() -> None:
            if pair in self._subs:
                self._subs.remove(pair)

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
                self.errors.append((event, handler, e))
                continue
            if inspect.isawaitable(result):
                self._schedule(event, handler, result)
        return event

    def _schedule(self, event: Event, handler: Handler, awaitable: Awaitable[None]) -> None:
        async def run() -> None:
            try:
                await awaitable
            except Exception as e:
                self.errors.append((event, handler, e))

        task = asyncio.get_running_loop().create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    def events(self, pattern: str = "*") -> list[Event]:
        return [e for e in self.log if self.matches(pattern, e.kind)]
