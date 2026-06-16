# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = ("LoopDirective", "LoopControl", "LoopBreak", "ToolInvocation")


class LoopDirective(Enum):
    CONTINUE = "continue"
    CANCEL = "cancel"
    BREAK = "break"


@dataclass(frozen=True, slots=True)
class LoopControl:
    directive: LoopDirective
    reason: str | None = None


class LoopBreak(Exception):  # noqa: N818
    """Raised by an observer requesting a hard stop; propagates out of run() as RunFailed."""

    def __init__(self, reason: str | None = None) -> None:
        super().__init__(reason or "loop broken by observer")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Proposed tool call passed to the pre-invoke gate; falsy/raised verdict blocks execution."""

    function: str
    arguments: dict = field(default_factory=dict)
    branch_id: str | None = None
