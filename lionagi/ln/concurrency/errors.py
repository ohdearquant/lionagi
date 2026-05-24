# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Error/cancellation utilities with backend-agnostic behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

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
    "split_cancellation",
)

# Module-level cache populated by ``cache_cancelled_exc_class()`` from inside
# a running event loop.  Falls back to ``(asyncio.CancelledError,)`` when
# called outside a loop (e.g. during teardown after the loop has stopped).
_CANCELLED_EXC_CLASS: tuple[type[BaseException], ...] | None = None


def cache_cancelled_exc_class() -> None:
    """Cache the backend cancellation exception class for safe out-of-loop use.

    Must be called from inside a running event loop (e.g. at the top of an
    async entry point).  Subsequent calls to :func:`cancelled_exc_classes`
    will return the cached tuple even after the loop has exited.

    Safe to call multiple times; subsequent calls are no-ops.
    """
    global _CANCELLED_EXC_CLASS
    if _CANCELLED_EXC_CLASS is not None:
        return
    try:
        cls = anyio.get_cancelled_exc_class()
        # Build a tuple that covers both asyncio and the backend-specific type
        # (they may be the same, but de-dup to avoid CPython isinstance quirks).
        _CANCELLED_EXC_CLASS = tuple({asyncio.CancelledError, cls})
    except Exception:
        # If anyio itself raises here (shouldn't happen inside a loop, but be
        # defensive), record the asyncio baseline so the cache is populated.
        _CANCELLED_EXC_CLASS = (asyncio.CancelledError,)


def cancelled_exc_classes() -> tuple[type[BaseException], ...]:
    """Return cached cancellation exception types, safe to call after loop exit.

    Returns the tuple populated by :func:`cache_cancelled_exc_class`.  If the
    cache was never populated (e.g. the function was not called inside a loop),
    falls back to ``(asyncio.CancelledError,)`` so callers never raise
    ``NoEventLoopError``.

    Returns:
        Tuple of exception types that represent cancellation.
    """
    if _CANCELLED_EXC_CLASS is not None:
        return _CANCELLED_EXC_CLASS
    # Graceful degradation: no cache yet → use asyncio baseline.
    return (asyncio.CancelledError,)


def get_cancelled_exc_class() -> type[BaseException]:
    """Return backend-specific cancellation exception type.

    Returns:
        asyncio.CancelledError for asyncio, trio.Cancelled for trio.
    """
    return anyio.get_cancelled_exc_class()


def is_cancelled(exc: BaseException) -> bool:
    """Check if exception is a backend cancellation.

    Args:
        exc: Exception to check.

    Returns:
        True if exc is the backend's cancellation exception type.
    """
    return isinstance(exc, anyio.get_cancelled_exc_class())


async def shield(func: Callable[P, Awaitable[T]], *args: P.args, **kwargs: P.kwargs) -> T:
    """Execute async function protected from outer cancellation.

    Args:
        func: Async callable to shield.
        *args: Positional arguments for func.
        **kwargs: Keyword arguments for func.

    Returns:
        Result of func(*args, **kwargs).

    Note:
        Use sparingly. Shielded code cannot be cancelled, which may
        delay shutdown. Prefer short critical sections only.
    """
    with anyio.CancelScope(shield=True):
        result = await func(*args, **kwargs)
    return result  # type: ignore[return-value]


def split_cancellation(
    eg: BaseExceptionGroup,
) -> tuple[BaseExceptionGroup | None, BaseExceptionGroup | None]:
    """Partition ExceptionGroup into cancellations and other errors.

    Args:
        eg: ExceptionGroup to split.

    Returns:
        Tuple of (cancellation_group, other_errors_group).
        Either may be None if no matching exceptions.
    """
    return eg.split(anyio.get_cancelled_exc_class())


def non_cancel_subgroup(eg: BaseExceptionGroup) -> BaseExceptionGroup | None:
    """Extract non-cancellation exceptions from ExceptionGroup.

    Args:
        eg: ExceptionGroup to filter.

    Returns:
        ExceptionGroup of non-cancellation errors, or None if all were cancellations.
    """
    _, rest = eg.split(anyio.get_cancelled_exc_class())
    return rest
