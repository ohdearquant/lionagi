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
    "maybe_await",
    "run_sync",
    "run_async",
    "sleep",
    "current_time",
    "SigtermInterrupt",
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


class SigtermInterrupt(BaseException):
    """Raised by run_async() when the process received SIGTERM mid-run.

    Not a KeyboardInterrupt subclass: that type is the SIGINT/Ctrl-C
    convention and callers treat it as user-initiated. SIGTERM is an
    external termination request (a supervisor, a process-group kill)
    and needs a distinct signal so callers can log/exit differently.
    Subclasses BaseException, like KeyboardInterrupt, so a bare
    ``except Exception:`` elsewhere doesn't silently swallow it.
    """


# Signal-aware run_async: installs temporary SIGINT/SIGTERM handlers from the
# main thread that cancel the inner asyncio task via call_soon_threadsafe
# instead of leaving the default disposition in place. SIGINT's default would
# raise KeyboardInterrupt in join(), orphaning the child thread and leaving
# session rows stuck in "running" state; SIGTERM's default is immediate
# process termination with no unwind at all, so without a handler here an
# external SIGTERM (a timeout supervisor, a process-group kill) is silent.


def run_async(coro: Awaitable[T]) -> T:
    """Run an awaitable from sync context in an isolated thread+event loop."""
    result_container: list[Any] = []
    exception_container: list[BaseException] = []

    _loop_and_task_future: _ThreadFuture[tuple[Any, Any]] = _ThreadFuture()
    _cancel_requested = threading.Event()
    _term_requested = threading.Event()

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

    def _make_handler(requested: threading.Event, old_handler: Any) -> Callable[[int, Any], None]:
        def _handler(signum: int, frame: Any) -> None:
            requested.set()
            try:
                child_loop, task = _loop_and_task_future.result(timeout=0.5)
            except Exception:  # noqa: BLE001
                if callable(old_handler):
                    old_handler(signum, frame)
                return
            if task is not None and child_loop is not None:
                child_loop.call_soon_threadsafe(task.cancel)

        return _handler

    if in_main_thread:
        old_sigint_handler = signal.getsignal(signal.SIGINT)
        old_sigterm_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _make_handler(_cancel_requested, old_sigint_handler))
        signal.signal(signal.SIGTERM, _make_handler(_term_requested, old_sigterm_handler))

    thread.start()
    try:
        thread.join()
    finally:
        if in_main_thread:
            signal.signal(signal.SIGINT, old_sigint_handler)
            signal.signal(signal.SIGTERM, old_sigterm_handler)

    if _cancel_requested.is_set():
        raise KeyboardInterrupt
    if _term_requested.is_set():
        raise SigtermInterrupt("process received SIGTERM; inner task cancelled")

    if exception_container:
        raise exception_container[0]
    if not result_container:  # pragma: no cover
        raise RuntimeError("Coroutine did not produce a result")
    return result_container[0]


async def maybe_await(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result


async def sleep(seconds: float) -> None:
    await anyio.sleep(seconds)


def current_time() -> float:
    return anyio.current_time()
