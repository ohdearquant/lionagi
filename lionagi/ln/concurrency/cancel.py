# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Cancellation helpers for structured concurrency (anyio-backed)."""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager

import anyio

CancelScope = anyio.CancelScope
_INF = float("inf")


__all__ = (
    "CancelScope",
    "fail_after",
    "move_on_after",
    "fail_at",
    "move_on_at",
    "effective_deadline",
)


@contextmanager
def fail_after(seconds: float | None) -> Iterator[CancelScope]:
    """Context manager that raises TimeoutError after seconds (None = no timeout, still cancellable)."""
    if seconds is None:
        # No timeout, but still cancellable by outer scopes
        with CancelScope() as scope:
            yield scope
        return
    with anyio.fail_after(seconds) as scope:
        yield scope


@contextmanager
def move_on_after(seconds: float | None) -> Iterator[CancelScope]:
    """Context manager that silently cancels after seconds; check scope.cancelled_caught on exit."""
    if seconds is None:
        # No timeout, but still cancellable by outer scopes
        with CancelScope() as scope:
            yield scope
        return
    with anyio.move_on_after(seconds) as scope:
        yield scope


@contextmanager
def fail_at(deadline: float | None) -> Iterator[CancelScope]:
    """Like fail_after but takes an absolute monotonic deadline instead of a duration."""
    if deadline is None:
        # No timeout, but still cancellable by outer scopes
        with CancelScope() as scope:
            yield scope
        return
    # The deadline goes on the scope itself, not through a duration: converting
    # it for fail_after reads the clock a second time, which moves the deadline
    # later by however long passed between the two reads.
    with CancelScope(deadline=_scope_deadline(deadline)) as scope:
        yield scope
    if scope.cancelled_caught and anyio.current_time() >= scope.deadline:
        raise TimeoutError


@contextmanager
def move_on_at(deadline: float | None) -> Iterator[CancelScope]:
    """Like move_on_after but takes an absolute monotonic deadline instead of a duration."""
    if deadline is None:
        # No timeout, but still cancellable by outer scopes
        with CancelScope() as scope:
            yield scope
        return
    with CancelScope(deadline=_scope_deadline(deadline)) as scope:
        yield scope


def _scope_deadline(deadline: float) -> float:
    # NaN compares false against every time: on asyncio a scope given it never
    # expires, and trio refuses it. It is read as a deadline already passed.
    return anyio.current_time() if math.isnan(deadline) else deadline


def effective_deadline() -> float | None:
    """Innermost cancel scope deadline; None if unlimited, -inf if already cancelled."""
    d = anyio.current_effective_deadline()
    return None if d == _INF else d
