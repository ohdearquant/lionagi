# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Async coordination patterns (gather, race, bounded_map, CompletionStream, retry) on AnyIO."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import TypeVar

import anyio
import anyio.abc

from ._compat import BaseExceptionGroup
from .cancel import effective_deadline, move_on_at
from .errors import non_cancel_subgroup
from .primitives import CapacityLimiter
from .task import create_task_group
from .utils import current_time

T = TypeVar("T")
R = TypeVar("R")


__all__ = (
    "gather",
    "race",
    "bounded_map",
    "CompletionStream",
    "retry",
)


async def gather(*aws: Awaitable[T], return_exceptions: bool = False) -> list[T | BaseException]:
    """Run awaitables concurrently; return ordered results or raise on first error (return_exceptions=False)."""
    if not aws:
        return []

    results: list[T | BaseException | None] = [None] * len(aws)

    async def _runner(idx: int, aw: Awaitable[T]) -> None:
        try:
            results[idx] = await aw
        except BaseException as exc:
            results[idx] = exc
            if not return_exceptions:
                raise

    try:
        async with create_task_group() as tg:
            for i, aw in enumerate(aws):
                tg.start_soon(_runner, i, aw)
    except BaseExceptionGroup as eg:
        if not return_exceptions:
            rest = non_cancel_subgroup(eg)
            if rest is not None:
                if len(rest.exceptions) == 1:
                    raise rest.exceptions[0] from rest
                raise rest from eg
            raise

    return results  # type: ignore


async def race(*aws: Awaitable[T]) -> T:
    """First-completion race: return first result (or re-raise first exception); cancel remaining tasks."""
    if not aws:
        raise ValueError("race() requires at least one awaitable")
    send, recv = anyio.create_memory_object_stream(1)

    async def _runner(aw: Awaitable[T]) -> None:
        try:
            res = await aw
            await send.send((True, res))
        except BaseException as exc:
            await send.send((False, exc))

    async with send, recv, create_task_group() as tg:
        for aw in aws:
            tg.start_soon(_runner, aw)
        ok, payload = await recv.receive()
        tg.cancel_scope.cancel()

    # Raise outside the TaskGroup context to avoid ExceptionGroup wrapping
    if ok:
        return payload  # type: ignore[return-value]
    raise payload  # type: ignore[misc]


async def bounded_map(
    func: Callable[[T], Awaitable[R]],
    items: Iterable[T],
    *,
    limit: int,
    return_exceptions: bool = False,
) -> list[R | BaseException]:
    """Map async func over items with at most limit concurrent tasks; ordered results (or raise on error)."""
    if limit <= 0:
        raise ValueError("limit must be >= 1")

    seq = list(items)
    if not seq:
        return []

    out: list[R | BaseException | None] = [None] * len(seq)
    limiter = CapacityLimiter(limit)

    async def _runner(i: int, x: T) -> None:
        async with limiter:
            try:
                out[i] = await func(x)
            except BaseException as exc:
                out[i] = exc
                if not return_exceptions:
                    raise  # Propagate to the TaskGroup

    try:
        async with create_task_group() as tg:
            for i, x in enumerate(seq):
                tg.start_soon(_runner, i, x)
    except BaseExceptionGroup as eg:
        if not return_exceptions:
            rest = non_cancel_subgroup(eg)
            if rest is not None:
                if len(rest.exceptions) == 1:
                    raise rest.exceptions[0] from rest
                raise rest from eg
            raise

    return out  # type: ignore


class CompletionStream:
    """Async context manager that yields (index, result) pairs in first-finished order."""

    def __init__(
        self,
        aws: Sequence[Awaitable[T]],
        *,
        limit: int | None = None,
        return_exceptions: bool = False,
    ):
        self.aws = aws
        self.limit = limit
        self.return_exceptions = return_exceptions
        self._task_group: anyio.abc.TaskGroup | None = None
        self._send: anyio.abc.ObjectSendStream[tuple[int, T]] | None = None
        self._recv: anyio.abc.ObjectReceiveStream[tuple[int, T]] | None = None
        self._completed_count = 0
        self._total_count = len(aws)

    async def __aenter__(self):
        n = len(self.aws)
        self._send, self._recv = anyio.create_memory_object_stream(n)
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()

        limiter = CapacityLimiter(self.limit) if self.limit else None

        async def _runner(i: int, aw: Awaitable[T]) -> None:
            if limiter:
                await limiter.acquire()
            try:
                try:
                    res = await aw
                except BaseException as exc:
                    if self.return_exceptions:
                        res = exc  # type: ignore[assignment]
                    else:
                        raise
                try:
                    assert self._send is not None
                    await self._send.send((i, res))  # type: ignore[arg-type]
                except anyio.ClosedResourceError:
                    # Consumer closed the receive end early (e.g. break after first
                    # result) — discard silently, this is expected early-exit behavior.
                    pass
            finally:
                if limiter:
                    limiter.release()

        for i, aw in enumerate(self.aws):
            self._task_group.start_soon(_runner, i, aw)

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if self._task_group:
                await self._task_group.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            if self._send:
                await self._send.aclose()
            if self._recv:
                await self._recv.aclose()
        return False

    def __aiter__(self):
        if not self._recv:
            raise RuntimeError("CompletionStream must be used as async context manager")
        return self

    async def __anext__(self):
        if self._completed_count >= self._total_count:
            raise StopAsyncIteration

        try:
            result = await self._recv.receive()
            self._completed_count += 1
            return result
        except anyio.EndOfStream:
            raise StopAsyncIteration from None


async def retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.1,
    max_delay: float = 2.0,
    backoff_factor: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    jitter: float = 0.1,
) -> T:
    """Retry fn up to attempts times with exponential backoff; deadline-aware, never retries cancellation."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if base_delay <= 0:
        raise ValueError("base_delay must be > 0")
    if max_delay < 0:
        raise ValueError("max_delay must be >= 0")
    if jitter < 0:
        raise ValueError("jitter must be >= 0")
    if backoff_factor < 1.0:
        raise ValueError("backoff_factor must be >= 1.0")

    cancelled_exc = anyio.get_cancelled_exc_class()
    if any(issubclass(cancelled_exc, t) for t in retry_on):
        raise ValueError("retry_on must not include the cancellation exception type")

    attempt = 0
    deadline = effective_deadline()
    while True:
        try:
            return await fn()
        except retry_on:
            attempt += 1
            if attempt >= attempts:
                raise

            delay = min(max_delay, base_delay * (backoff_factor ** (attempt - 1)))
            if jitter:
                delay *= 1 + random.random() * jitter  # noqa: S311  # non-crypto: jitter for backoff timing

            if deadline is not None:
                remaining = deadline - current_time()
                if remaining <= 0:
                    raise
                with move_on_at(deadline):
                    await anyio.sleep(delay)
                if current_time() >= deadline:
                    raise
            else:
                await anyio.sleep(delay)
