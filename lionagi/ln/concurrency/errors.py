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
    """True if exc is the backend's cancellation exception type."""
    return isinstance(exc, cancelled_exc_classes())


async def shield(func: Callable[P, Awaitable[T]], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run func inside a shielded cancel scope; use only for short critical sections."""
    with anyio.CancelScope(shield=True):
        result = await func(*args, **kwargs)
    return result  # type: ignore[return-value]


def split_cancellation(
    eg: BaseExceptionGroup,
) -> tuple[BaseExceptionGroup | None, BaseExceptionGroup | None]:
    """Split ExceptionGroup into (cancellation_group, other_errors_group); either may be None."""
    return eg.split(anyio.get_cancelled_exc_class())


def non_cancel_subgroup(eg: BaseExceptionGroup) -> BaseExceptionGroup | None:
    """Non-cancellation sub-group of eg; None if all exceptions were cancellations."""
    _, rest = eg.split(anyio.get_cancelled_exc_class())
    return rest
