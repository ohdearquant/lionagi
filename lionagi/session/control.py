# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ("LoopDirective", "LoopControl", "LoopBreak")


class LoopDirective(str, Enum):
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
