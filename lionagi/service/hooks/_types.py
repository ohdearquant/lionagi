# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from typing_extensions import TypedDict

from lionagi.ln.types import Enum

SC = TypeVar("SC")  # streaming chunk type

__all__ = (
    "HookEventTypes",
    "ALLOWED_HOOKS_TYPES",
    "HookDict",
    "StreamHandlers",
    "AssociatedEventInfo",
)


class HookEventTypes(str, Enum):
    PreEventCreate = "pre_event_create"
    PreInvocation = "pre_invocation"
    PostInvocation = "post_invocation"


ALLOWED_HOOKS_TYPES = HookEventTypes.allowed()


class HookDict(TypedDict):
    pre_event_create: Callable | None
    pre_invocation: Callable | None
    post_invocation: Callable | None


StreamHandlers = dict[str, Callable[[Any, str | type, SC], Awaitable[None]]]
"""Mapping of chunk type names to their respective asynchronous handler functions."""


class AssociatedEventInfo(TypedDict, total=False):
    """Information about the event associated with the hook."""

    lion_class: str
    """Full qualified name of the event class."""

    event_id: str
    """ID of the event."""

    event_created_at: float
    """Creation timestamp of the event."""
