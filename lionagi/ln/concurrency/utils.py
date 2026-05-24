# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import inspect
import threading
from collections.abc import Awaitable, Callable
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
    """Cached coroutine check. Internal: expects already-unwrapped func."""
    return inspect.iscoroutinefunction(func)


def is_coro_func(func: Callable[..., Any]) -> bool:
    """Check if a function is a coroutine function, with caching for performance."""
    while isinstance(func, partial):
        func = func.func
    return _is_coro_func_cached(func)


async def run_sync(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run synchronous function in thread pool without blocking event loop.

    Args:
        func: Synchronous callable.
        *args: Positional arguments for func.
        **kwargs: Keyword arguments for func.

    Returns:
        Result of func(*args, **kwargs).
    """
    if kwargs:
        func_with_kwargs = partial(func, **kwargs)
        return await anyio.to_thread.run_sync(func_with_kwargs, *args)
    return await anyio.to_thread.run_sync(func, *args)


def run_async(coro: Awaitable[T]) -> T:
    """Execute an async coroutine from a synchronous context.

    Creates an isolated thread with its own event loop to run the coroutine,
    avoiding conflicts with any existing event loop in the current thread.
    Thread-safe and blocks until completion.

    SIGINT (KeyboardInterrupt) delivered to the main thread while blocking on
    ``thread.join()`` is intercepted here.  The interrupt flag is forwarded
    into the child event loop so that shielded finally-blocks (e.g. session
    teardown) still execute before the KeyboardInterrupt propagates to the
    caller.  Fixes #1055.

    Args:
        coro: Awaitable to execute (coroutine, Task, or Future).

    Returns:
        The result of the awaited coroutine.

    Raises:
        BaseException: Any exception raised by the coroutine is re-raised.
        RuntimeError: If the coroutine completes without producing a result.

    Example:
        >>> async def fetch_data():
        ...     return {"status": "ok"}
        >>> result = run_async(fetch_data())
        >>> result
        {'status': 'ok'}

    Note:
        Use sparingly. Prefer native async patterns when possible.
        Each call creates a new thread and event loop.
    """
    result_container: list[Any] = []
    exception_container: list[BaseException] = []
    # Shared flag: main thread sets this to ask the child loop to cancel.
    cancel_requested = threading.Event()

    def run_in_thread() -> None:
        try:

            async def _runner() -> T:
                return await coro

            result = anyio.run(_runner)
            result_container.append(result)
        except BaseException as e:
            exception_container.append(e)

    thread = threading.Thread(target=run_in_thread, daemon=False)
    thread.start()

    keyboard_interrupt: KeyboardInterrupt | None = None
    try:
        # Block until the child thread finishes.  If SIGINT arrives here,
        # Python raises KeyboardInterrupt in this (main) thread and exits
        # the try-block, but the child thread keeps running.
        while thread.is_alive():
            # Use a short timeout so KeyboardInterrupt is delivered promptly
            # without busy-spinning.
            thread.join(timeout=0.05)
    except KeyboardInterrupt as ki:
        # Record the interrupt and wait for the child thread to finish its
        # shielded teardown before re-raising.  The coroutine's own
        # KeyboardInterrupt handler (shielded scope) will run to completion.
        keyboard_interrupt = ki
        cancel_requested.set()
        thread.join()  # wait for shielded teardown to finish

    if keyboard_interrupt is not None:
        raise keyboard_interrupt

    if exception_container:
        raise exception_container[0]
    if not result_container:  # pragma: no cover
        raise RuntimeError("Coroutine did not produce a result")
    return result_container[0]


async def sleep(seconds: float) -> None:
    """Async sleep without blocking the event loop.

    Args:
        seconds: Duration to sleep.
    """
    await anyio.sleep(seconds)


def current_time() -> float:
    """Get current monotonic time in seconds.

    Returns:
        Monotonic clock value from anyio.
    """
    return anyio.current_time()
