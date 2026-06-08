# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import inspect
import signal
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future as _ThreadFuture
from functools import cache, partial
from typing import Any, ParamSpec, TypeVar

import anyio
import anyio.to_thread

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T")


__all__ = (
    "is_coro_func",
    "run_sync",
    "run_async",
    "sleep",
    "current_time",
)


@cache
def _is_coro_func_cached(func: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(func)


def is_coro_func(func: Callable[..., Any]) -> bool:
    while isinstance(func, partial):
        func = func.func
    return _is_coro_func_cached(func)


async def run_sync(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run synchronous function in a thread pool."""
    if kwargs:
        func_with_kwargs = partial(func, **kwargs)
        return await anyio.to_thread.run_sync(func_with_kwargs, *args)
    return await anyio.to_thread.run_sync(func, *args)


# SIGINT-aware run_async: installs a temporary SIGINT handler from the main
# thread that cancels the inner asyncio task via call_soon_threadsafe instead
# of raising KeyboardInterrupt in join(), which would orphan the child thread
# and leave session rows stuck in "running" state.


def run_async(coro: Awaitable[T]) -> T:
    """Run an awaitable from sync context in an isolated thread+event loop."""
    result_container: list[Any] = []
    exception_container: list[BaseException] = []

    _loop_and_task_future: _ThreadFuture[tuple[Any, Any]] = _ThreadFuture()
    _cancel_requested = threading.Event()

    def run_in_thread() -> None:
        import asyncio

        try:

            async def _runner() -> T:
                _loop_and_task_future.set_result((asyncio.get_event_loop(), asyncio.current_task()))
                return await coro

            result = anyio.run(_runner)
            result_container.append(result)
        except BaseException as e:
            exception_container.append(e)
        finally:
            if not _loop_and_task_future.done():
                _loop_and_task_future.cancel()

    thread = threading.Thread(target=run_in_thread, daemon=False)

    # signal.signal() raises ValueError from non-main threads
    in_main_thread = threading.current_thread() is threading.main_thread()

    if in_main_thread:
        old_sigint_handler = signal.getsignal(signal.SIGINT)

        def _sigint_handler(signum: int, frame: Any) -> None:  # noqa: ARG001
            _cancel_requested.set()
            try:
                child_loop, task = _loop_and_task_future.result(timeout=0.5)
            except Exception:  # noqa: BLE001
                if callable(old_sigint_handler):
                    old_sigint_handler(signum, frame)
                return
            if task is not None and child_loop is not None:
                child_loop.call_soon_threadsafe(task.cancel)

        signal.signal(signal.SIGINT, _sigint_handler)

    thread.start()
    try:
        thread.join()
    finally:
        if in_main_thread:
            signal.signal(signal.SIGINT, old_sigint_handler)

    if _cancel_requested.is_set():
        raise KeyboardInterrupt

    if exception_container:
        raise exception_container[0]
    if not result_container:  # pragma: no cover
        raise RuntimeError("Coroutine did not produce a result")
    return result_container[0]


async def sleep(seconds: float) -> None:
    await anyio.sleep(seconds)


def current_time() -> float:
    return anyio.current_time()
