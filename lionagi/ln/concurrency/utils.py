# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect
import logging
import signal
import threading
from collections.abc import Awaitable, Callable, Iterator, Sequence
from concurrent.futures import Future as _ThreadFuture
from contextlib import contextmanager
from functools import partial
from types import FrameType
from typing import Any, TypeVar

import anyio
import anyio.to_thread

from .primitives import CapacityLimiter

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
    "temporary_signal_handlers",
)


def is_coro_func(func: Callable[..., Any]) -> bool:
    """Recognize coroutine functions, methods and async callable instances."""
    # Not cached: a cache keyed on a bound method or a closure keeps its owner alive.
    while isinstance(func, partial):
        func = func.func
    if inspect.iscoroutinefunction(func):
        return True
    return callable(func) and inspect.iscoroutinefunction(
        getattr(type(func), "__call__", None)  # noqa: B004 - inspect the call slot after callable().
    )


async def run_sync(
    func: Callable[..., R],
    *args: Any,
    abandon_on_cancel: bool = False,
    limiter: CapacityLimiter | None = None,
    **kwargs: Any,
) -> R:
    """Run a synchronous callable in an AnyIO worker thread.

    Other keyword arguments belong to the callable. Prebind them with
    ``functools.partial`` when the callable itself needs a keyword named
    ``abandon_on_cancel`` or ``limiter``.

    The default shields the active worker wait from AnyIO scope cancellation;
    waiting for admission can still be cancelled. With ``abandon_on_cancel=True``,
    cancellation stops waiting but leaves the thread running, discarding its
    eventual result or error. ``limiter`` selects an ln capacity limiter instead
    of AnyIO's default. On asyncio, abandoning the wait releases its token; on
    Trio, the token stays borrowed until the worker finishes.
    """
    if limiter is not None and not isinstance(limiter, CapacityLimiter):
        raise TypeError("limiter must be a lionagi.ln.concurrency.CapacityLimiter or None")
    if type(abandon_on_cancel) is not bool:
        raise TypeError("abandon_on_cancel must be a bool")
    if kwargs:
        func = partial(func, **kwargs)
    return await anyio.to_thread.run_sync(
        func,
        *args,
        abandon_on_cancel=abandon_on_cancel,
        limiter=limiter._lim if limiter is not None else None,
    )


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


def _restore_signal_handlers(installed: list[tuple[int, Any]]) -> None:
    # Each restore stands alone: a failure on one signal must not strand
    # the others, and failures log rather than raise (this often runs
    # while an exception is already propagating).
    for signum, prior in installed:
        try:
            signal.signal(signum, prior)
        except Exception:
            logging.getLogger(__name__).warning(
                "could not restore the previous handler for signal %s; it "
                "remains overridden for the rest of this process",
                signum,
                exc_info=True,
            )


@contextmanager
def temporary_signal_handlers(
    handler: Callable[[int, FrameType | None], Any],
    signals: Sequence[int] = (signal.SIGINT, signal.SIGTERM),
) -> Iterator[None]:
    """Install *handler* for *signals* inside the block and give each back on exit.

    Takes over a signal on the same terms as ``run_async``: only on the main
    thread, and only when its current handler can be reinstalled afterwards.
    See docs/internals/agent-runtime.md#signal-handler-takeover-and-restore.
    """
    installed: list[tuple[int, Any]] = []
    try:
        if threading.current_thread() is threading.main_thread():
            for signum in dict.fromkeys(signals):
                prior = signal.getsignal(signum)
                if prior is None:
                    continue
                signal.signal(signum, handler)
                installed.append((signum, prior))
        yield
    finally:
        _restore_signal_handlers(installed)


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

    # Take over a signal only when the previous handler can be restored later
    # (getsignal() reports None for one installed outside Python, and
    # signal.signal(signum, None) raises). See
    # docs/internals/agent-runtime.md#signal-handler-takeover-and-restore.
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
        _restore_signal_handlers(installed)

    if _cancel_requested.is_set():
        raise KeyboardInterrupt
    if _term_requested.is_set():
        raise SigtermInterrupt("process received SIGTERM; inner task cancelled")

    if exception_container:
        raise exception_container[0]
    if not result_container:  # pragma: no cover
        raise RuntimeError("Coroutine did not produce a result")
    return result_container[0]


async def maybe_await(result: Any, *, classifier: Callable[[Any], bool] | None = None) -> Any:
    """Await one result if the selected classifier accepts it; otherwise return it unchanged."""
    predicate = inspect.isawaitable if classifier is None else classifier
    if predicate(result):
        return await result
    return result


async def sleep(seconds: float) -> None:
    await anyio.sleep(seconds)


def current_time() -> float:
    return anyio.current_time()
