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


# ── SIGINT-aware run_async ────────────────────────────────────────────────────
# SIGINT (Ctrl-C) is delivered to the MAIN thread by the OS.  The default
# CPython handler raises KeyboardInterrupt in whatever bytecode the main
# thread is currently executing — including threading.Thread.join().  When
# that happens the join() call aborts immediately, the child thread is
# orphaned with a live event loop, and the session row is never transitioned
# away from "running" (the phantom-session bug, issue #1055).
#
# Fix: install a SIGINT handler in the parent thread *before* spawning the
# child.  The handler shares the child's asyncio task reference (via a
# Future) and calls task.cancel() through call_soon_threadsafe(), which
# schedules the cancellation inside the running event loop.  The child's
# coroutine then unwinds through its ``finally`` / ``with CancelScope(shield=True)``
# teardown, closes the DB, transitions the session, and exits cleanly.
# The parent blocks on thread.join() the whole time (join() is only
# interrupted if KeyboardInterrupt arrives *outside* our handler, which
# cannot happen while our handler is installed).
#
# Platform notes:
# - signal.signal() requires the main thread; we guard with a
#   threading.current_thread() check so nested/worker-thread callers are
#   completely unaffected.
# - This is tested on macOS (darwin).  On Linux the behaviour is identical
#   because both use POSIX signals with the same CPython handler semantics.
#   Windows does not have SIGINT in the POSIX sense; signal.SIGINT maps to
#   a Ctrl-C event that Python also routes to the main thread, so the guard
#   is compatible but untested on Windows.
# - signal.getsignal() is safe from any thread; only signal.signal() is
#   main-thread-only.


def run_async(coro: Awaitable[T]) -> T:
    """Execute an async coroutine from a synchronous context.

    Creates an isolated thread with its own event loop to run the coroutine,
    avoiding conflicts with any existing event loop in the current thread.
    Thread-safe and blocks until completion.

    When called from the main thread, installs a temporary SIGINT handler so
    that Ctrl-C cancels the inner coroutine through its structured teardown
    path (``finally`` / ``anyio.CancelScope(shield=True)``) instead of
    orphaning the child thread.  The previous SIGINT handler is always
    restored before this function returns.

    Args:
        coro: Awaitable to execute (coroutine, Task, or Future).

    Returns:
        The result of the awaited coroutine.

    Raises:
        KeyboardInterrupt: If SIGINT was received while the coroutine was
            running.  By the time this is raised the inner coroutine's
            teardown has already completed and the child thread has exited.
        BaseException: Any other exception raised by the coroutine is
            re-raised.
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

    # Shared state between parent and child threads.
    # _loop_and_task_future: child resolves with (loop, task) so the parent's
    #   SIGINT handler can cancel the task via call_soon_threadsafe.
    # _cancel_requested: set by the SIGINT handler so run_async knows to raise KBI.
    _loop_and_task_future: _ThreadFuture[tuple[Any, Any]] = _ThreadFuture()
    _cancel_requested = threading.Event()

    def run_in_thread() -> None:
        import asyncio

        try:

            async def _runner() -> T:
                # Capture (loop, task) from INSIDE the coroutine — this is the
                # actual event loop that anyio.run() created, not any loop we
                # might create manually before calling anyio.run().  We expose
                # both to the parent's SIGINT handler BEFORE awaiting ``coro``
                # so the handler can cancel the task while we're running.
                _loop_and_task_future.set_result((asyncio.get_event_loop(), asyncio.current_task()))
                return await coro

            result = anyio.run(_runner)
            result_container.append(result)
        except BaseException as e:
            exception_container.append(e)
        finally:
            # If _runner never ran (e.g. anyio setup failed before it was
            # scheduled), the future would block the parent's SIGINT handler.
            # Cancel the future with a sentinel so the handler gets a quick
            # "no task available" signal and falls back gracefully.
            if not _loop_and_task_future.done():
                _loop_and_task_future.cancel()

    thread = threading.Thread(target=run_in_thread, daemon=False)

    # Only install the SIGINT handler when we are in the main thread.
    # signal.signal() raises ValueError from non-main threads.
    in_main_thread = threading.current_thread() is threading.main_thread()

    if in_main_thread:
        old_sigint_handler = signal.getsignal(signal.SIGINT)

        def _sigint_handler(signum: int, frame: Any) -> None:  # noqa: ARG001
            """Cancel the inner task instead of raising KBI in the parent thread."""
            _cancel_requested.set()
            # Try to get the (loop, task) pair.  Use a short timeout: if the
            # child hasn't set it yet (startup race), fall back to the previous
            # handler so the process can still be interrupted.
            try:
                child_loop, task = _loop_and_task_future.result(timeout=0.5)
            except Exception:  # noqa: BLE001
                # Pair unavailable (startup race or already done) — fall back.
                if callable(old_sigint_handler):
                    old_sigint_handler(signum, frame)
                return
            if task is not None and child_loop is not None:
                child_loop.call_soon_threadsafe(task.cancel)

        signal.signal(signal.SIGINT, _sigint_handler)

    thread.start()
    try:
        # This join() will now NOT be interrupted by KeyboardInterrupt because
        # our handler above intercepts SIGINT and schedules task cancellation
        # instead.  The child thread runs teardown and exits normally, then
        # join() returns here.
        thread.join()
    finally:
        if in_main_thread:
            # Always restore the previous handler before we raise anything.
            signal.signal(signal.SIGINT, old_sigint_handler)

    if _cancel_requested.is_set():
        # The inner coroutine's teardown has already completed (thread.join()
        # returned).  Now raise KeyboardInterrupt so callers (e.g. run_agent)
        # see the expected signal-interrupted exit path.
        raise KeyboardInterrupt

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
