# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Error/cancellation utilities with backend-agnostic behavior."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

import anyio

from ._compat import BaseExceptionGroup

T = TypeVar("T")
P = ParamSpec("P")

__all__ = (
    "cache_cancelled_exc_class",
    "cancelled_exc_classes",
    "get_cancelled_exc_class",
    "is_cancelled",
    "non_cancel_subgroup",
    "shield",
    "shield_quietly",
    "split_cancellation",
)

# Populated by cache_cancelled_exc_class() inside a running loop; falls back to
# (asyncio.CancelledError,) when called outside one (e.g. post-loop teardown).
_CANCELLED_EXC_CLASS: tuple[type[BaseException], ...] | None = None


def cache_cancelled_exc_class() -> None:
    """Cache backend cancellation exception class; call once inside a running loop; subsequent calls are no-ops."""
    global _CANCELLED_EXC_CLASS
    if _CANCELLED_EXC_CLASS is not None:
        return
    try:
        cls = anyio.get_cancelled_exc_class()
        # De-dup: asyncio.CancelledError and the backend-specific type may be identical.
        _CANCELLED_EXC_CLASS = tuple({asyncio.CancelledError, cls})
    except Exception:
        # Shouldn't happen inside a loop; fall back to the asyncio baseline defensively.
        _CANCELLED_EXC_CLASS = (asyncio.CancelledError,)


def cancelled_exc_classes() -> tuple[type[BaseException], ...]:
    """Cached cancellation exception types; falls back to asyncio.CancelledError if never primed."""
    if _CANCELLED_EXC_CLASS is not None:
        return _CANCELLED_EXC_CLASS
    return (asyncio.CancelledError,)


def get_cancelled_exc_class() -> type[BaseException]:
    """Backend-specific cancellation exception type (asyncio.CancelledError or trio.Cancelled)."""
    return anyio.get_cancelled_exc_class()


def is_cancelled(exc: BaseException) -> bool:
    """True if exc is asyncio's or, once trio is imported, trio's cancellation exception."""
    if isinstance(exc, cancelled_exc_classes()):
        return True
    # The cache always holds asyncio's class and holds trio's only if it was
    # primed inside a trio loop, so trio's is checked directly; it can only be
    # raised once trio has been imported.
    trio = sys.modules.get("trio")
    return trio is not None and isinstance(exc, trio.Cancelled)


async def shield(func: Callable[P, Awaitable[T]], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run func inside a shielded cancel scope; use only for short critical sections."""
    with anyio.CancelScope(shield=True):
        result = await func(*args, **kwargs)
    return result  # type: ignore[return-value]


def _mark_exception_retrieved(inner: asyncio.Future[Any]) -> None:
    """Done callback that marks *inner*'s exception retrieved so that nothing reports it later."""
    if not inner.cancelled():
        inner.exception()


def shield_quietly(arg: Awaitable[T]) -> asyncio.Future[T]:
    """``asyncio.shield`` that never reports the inner's failure to the loop's exception handler.

    Like ``asyncio.shield``: *arg* (a coroutine, task or future) keeps running when the caller
    awaiting the returned future is cancelled, the caller gets its result or exception when it is
    not, and a cancelled inner cancels the caller. An already finished future is returned as it is.

    CPython 3.14.7 and 3.14.8 differ from other releases: when the caller was cancelled first and
    the inner then fails, they pass the failure to the loop's exception handler, and the default
    handler logs the exception text. A site that shields work whose failure it handles itself
    must not do that. Here the inner's exception is marked retrieved when it fails, so nothing is
    reported then or when the inner is collected: a caller that is still waiting gets it, and when
    every caller has been cancelled one shared function marks it retrieved. A later ``await`` of
    the inner still raises it.

    A cancelled wait removes its own callback from a pending inner, as ``asyncio.shield`` does,
    and leaves that one shared function in its place, so any number of cancelled waits leave at
    most one callback of this helper on the inner.

    The helper is used on every Python version because it also stops "exception was never
    retrieved" for a shielded failure nobody awaits, so it is not a workaround to remove after
    3.14.8.
    """
    inner = asyncio.ensure_future(arg)
    if inner.done():
        return inner
    outer = inner.get_loop().create_future()

    def _inner_done(inner: asyncio.Future[T]) -> None:
        if outer.cancelled():
            if not inner.cancelled():
                inner.exception()
            return
        if inner.cancelled():
            outer.cancel()
            return
        exc = inner.exception()
        if exc is not None:
            outer.set_exception(exc)
        else:
            outer.set_result(inner.result())

    def _outer_done(outer: asyncio.Future[T]) -> None:
        if inner.done():
            return
        # The caller is gone and the inner is still pending. Removing the shared function first
        # keeps it from being added a second time, however many waits are cancelled.
        inner.remove_done_callback(_inner_done)
        inner.remove_done_callback(_mark_exception_retrieved)
        inner.add_done_callback(_mark_exception_retrieved)

    inner.add_done_callback(_inner_done)
    outer.add_done_callback(_outer_done)
    return outer


def split_cancellation(
    eg: BaseExceptionGroup,
) -> tuple[BaseExceptionGroup | None, BaseExceptionGroup | None]:
    """Split ExceptionGroup into (cancellation_group, other_errors_group); either may be None."""
    return eg.split(anyio.get_cancelled_exc_class())


def non_cancel_subgroup(eg: BaseExceptionGroup) -> BaseExceptionGroup | None:
    """Non-cancellation sub-group of eg; None if all exceptions were cancellations."""
    _, rest = eg.split(anyio.get_cancelled_exc_class())
    return rest
