# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Core async primitives (thin wrappers over anyio)"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import anyio
import anyio.abc

T = TypeVar("T")


__all__ = (
    "Lock",
    "Semaphore",
    "CapacityLimiter",
    "Queue",
    "Event",
    "Condition",
)


class Lock:
    """Async mutex lock (anyio.Lock wrapper)."""

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock = anyio.Lock()

    async def acquire(self) -> None:
        await self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    async def __aenter__(self) -> Lock:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


class Semaphore:
    """Async semaphore (anyio.Semaphore wrapper)."""

    __slots__ = ("_sem",)

    def __init__(self, initial_value: int) -> None:
        if initial_value < 0:
            raise ValueError("initial_value must be >= 0")
        self._sem = anyio.Semaphore(initial_value)

    async def acquire(self) -> None:
        await self._sem.acquire()

    def release(self) -> None:
        self._sem.release()

    async def __aenter__(self) -> Semaphore:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


class CapacityLimiter:
    """Async capacity limiter (anyio.CapacityLimiter wrapper)."""

    __slots__ = ("_lim",)

    def __init__(self, total_tokens: float) -> None:
        if total_tokens <= 0:
            raise ValueError("total_tokens must be > 0")
        self._lim = anyio.CapacityLimiter(total_tokens)

    async def acquire(self) -> None:
        await self._lim.acquire()

    def release(self) -> None:
        self._lim.release()

    @property
    def remaining_tokens(self) -> float:
        return self._lim.available_tokens

    @property
    def total_tokens(self) -> float:
        return self._lim.total_tokens

    @total_tokens.setter
    def total_tokens(self, value: float) -> None:
        if value <= 0:
            raise ValueError("total_tokens must be > 0")
        self._lim.total_tokens = value

    @property
    def borrowed_tokens(self) -> float:
        return self._lim.borrowed_tokens

    @property
    def available_tokens(self) -> float:
        return self._lim.available_tokens

    async def acquire_on_behalf_of(self, borrower: object) -> None:
        await self._lim.acquire_on_behalf_of(borrower)

    def release_on_behalf_of(self, borrower: object) -> None:
        self._lim.release_on_behalf_of(borrower)

    async def __aenter__(self) -> CapacityLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


@dataclass(slots=True)
class Queue(Generic[T]):
    """Async FIFO queue backed by anyio memory object streams."""

    _send: anyio.abc.ObjectSendStream[T]
    _recv: anyio.abc.ObjectReceiveStream[T]

    @classmethod
    def with_maxsize(cls, maxsize: int) -> Queue[T]:
        send, recv = anyio.create_memory_object_stream(maxsize)
        return cls(send, recv)

    async def put(self, item: T) -> None:
        await self._send.send(item)

    def put_nowait(self, item: T) -> None:
        self._send.send_nowait(item)

    async def get(self) -> T:
        return await self._recv.receive()

    def get_nowait(self) -> T:
        return self._recv.receive_nowait()

    async def close(self) -> None:
        await self._send.aclose()
        await self._recv.aclose()

    async def __aenter__(self) -> Queue[T]:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    @property
    def sender(self) -> anyio.abc.ObjectSendStream[T]:
        return self._send

    @property
    def receiver(self) -> anyio.abc.ObjectReceiveStream[T]:
        return self._recv


class Event:
    """Async event for signaling between tasks (anyio.Event wrapper)."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = anyio.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def statistics(self) -> anyio.EventStatistics:
        return self._event.statistics()


class Condition:
    """Async condition variable (anyio.Condition wrapper)."""

    __slots__ = ("_condition",)

    def __init__(self, lock: Lock | None = None) -> None:
        _lock = lock._lock if lock else None
        self._condition = anyio.Condition(_lock)

    async def acquire(self) -> None:
        await self._condition.acquire()

    def release(self) -> None:
        self._condition.release()

    async def __aenter__(self) -> Condition:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    async def wait(self) -> None:
        await self._condition.wait()

    def notify(self, n: int = 1) -> None:
        self._condition.notify(n)

    def notify_all(self) -> None:
        self._condition.notify_all()

    def statistics(self) -> anyio.ConditionStatistics:
        return self._condition.statistics()
