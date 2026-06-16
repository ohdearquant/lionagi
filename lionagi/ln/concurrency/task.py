# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Task group wrapper (thin facade over anyio.create_task_group)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import anyio
import anyio.abc

T = TypeVar("T")
R = TypeVar("R")

__all__ = (
    "TaskGroup",
    "create_task_group",
)


class TaskGroup:
    """Structured concurrency task group; obtain via create_task_group(), not directly."""

    __slots__ = ("_tg",)

    def __init__(self, tg: anyio.abc.TaskGroup) -> None:
        self._tg = tg

    @property
    def cancel_scope(self) -> anyio.CancelScope:
        """Cancel scope for this task group; call .cancel() to abort all tasks."""
        return self._tg.cancel_scope

    def start_soon(
        self,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        name: str | None = None,
    ) -> None:
        """Schedule a task without waiting for it to start."""
        self._tg.start_soon(func, *args, name=name)

    async def start(
        self,
        func: Callable[..., Awaitable[R]],
        *args: Any,
        name: str | None = None,
    ) -> R:
        """Start a task and wait for task_status.started() before returning."""
        return await self._tg.start(func, *args, name=name)


@asynccontextmanager
async def create_task_group() -> AsyncIterator[TaskGroup]:
    """Async context manager yielding a TaskGroup; all tasks complete before exit."""
    async with anyio.create_task_group() as tg:
        yield TaskGroup(tg)
