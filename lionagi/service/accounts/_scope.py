# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Private request-context scope for routed Anthropic calls."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from ._types import Scope

_scope: ContextVar[Scope | None] = ContextVar("lionagi_anthropic_scope", default=None)


def current_scope() -> Scope | None:
    return _scope.get()


@contextmanager
def routing_scope(scope: Scope) -> Iterator[None]:
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


__all__ = ("current_scope", "routing_scope")
