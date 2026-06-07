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
    """Raised inside the run loop when an observer requests a hard stop.

    The exception propagates out of ``run()`` so that ``Branch.operate``
    can surface it as a ``RunFailed`` event.
    """

    def __init__(self, reason: str | None = None) -> None:
        super().__init__(reason or "loop broken by observer")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """A proposed tool call presented to the pre-invoke governance gate.

    The session gate (``session.gate(check)``) receives this *before* the tool
    runs (ADR-0076 Follow-up 1); a falsy or raised verdict blocks execution and
    the denial is surfaced to the model as a tool-result, not raised. ``branch_id``
    lets a gate scope policy per branch/agent.
    """

    function: str
    arguments: dict = field(default_factory=dict)
    branch_id: str | None = None
