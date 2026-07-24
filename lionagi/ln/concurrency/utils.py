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
    "sigterm_received",
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


# Process-wide latch set by run_async's SIGTERM handler; lets persist paths
# distinguish an external SIGTERM from an internal runtime cancel.
_SIGTERM_RECEIVED = threading.Event()
_SIGTERM_RECEIVED_LOCK = threading.Lock()


def sigterm_received() -> bool:
    """True if run_async's SIGTERM handler has fired in this process."""
    return _SIGTERM_RECEIVED.is_set()


def consume_sigterm_received() -> bool:
    """Read-and-clear the latch so one external SIGTERM labels one run
    (otherwise it stays set and mislabels every later cancellation)."""
    with _SIGTERM_RECEIVED_LOCK:
        received = _SIGTERM_RECEIVED.is_set()
        if received:
            _SIGTERM_RECEIVED.clear()
        return received


class SigtermInterrupt(BaseException):
    """Raised by run_async() when SIGTERM arrives mid-run; subclasses
    BaseException (not KeyboardInterrupt) so ``except Exception:`` can't swallow it."""


# Installs temporary SIGINT/SIGTERM handlers that cancel the inner task via
# call_soon_threadsafe instead of leaving the default (silent-kill) disposition.
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
                task = asyncio.current_task()
                _loop_and_task_future.set_result((asyncio.get_event_loop(), task))
                if _cancel_requested.is_set() or _term_requested.is_set():
                    # A signal was latched before this future existed (the only
                    # path for SIGTERM) — cancel ourselves instead of running on.
                    task.cancel()
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
            if signum == signal.SIGTERM:
                # Latch process-wide so teardown code that only sees a plain
                # CancelledError can still report the cancel as external.
                with _SIGTERM_RECEIVED_LOCK:
                    _SIGTERM_RECEIVED.set()
            try:
                child_loop, task = _loop_and_task_future.result(timeout=0.5)
            except Exception:  # noqa: BLE001
                if callable(old_handler):
                    old_handler(signum, frame)
                return
            if task is not None and child_loop is not None:
                child_loop.call_soon_threadsafe(task.cancel)

        return _handler

    # Take over a signal only when the previous handler can be given back.
    # getsignal() reports None for a handler installed outside Python, and
    # signal.signal(signum, None) raises, so restoring one is impossible:
    # taking it over would leave this runner's handler installed for the rest
    # of the process. Abstaining per signal costs the caller's own cancellation
    # wiring for that signal and keeps whatever was already there working.
    installed: list[tuple[int, Any]] = []
    if in_main_thread:
        for signum, requested in (
            (signal.SIGINT, _cancel_requested),
            (signal.SIGTERM, _term_requested),
        ):
            prior = signal.getsignal(signum)
            if prior is None:
                continue
            signal.signal(signum, _make_handler(requested, prior))
            installed.append((signum, prior))

    thread.start()
    try:
        thread.join()
    finally:
        # Restore only what was installed above.
        for signum, prior in installed:
            signal.signal(signum, prior)

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
