# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""RoundOutcome — algebraic data type for one LNDL round's result. A
multi-round run is a state machine; the outer loop matches on the outcome variant."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = (
    "Continue",
    "Exhausted",
    "Failed",
    "Retry",
    "RoundOutcome",
    "Success",
)


@dataclass(slots=True, frozen=True)
class Success:
    """OUT{} present, parsed, and validated. Loop returns ``output``."""

    output: Any


@dataclass(slots=True, frozen=True)
class Continue:
    """No OUT{} block this round — model still thinking. Lacts run this
    round are already persisted as tool messages, visible next round."""

    notes_committed: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class Retry:
    """OUT{} produced but parse/resolve/validate failed. Feed ``error`` to
    the model next round to self-correct; prior scratchpad/history stays intact."""

    error: str
    note_keys: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class Exhausted:
    """Hit the round budget without a Success. Carries the most recent
    error so the caller can surface something useful."""

    last_error: str | None = None


@dataclass(slots=True, frozen=True)
class Failed:
    """Unrecoverable error — no point retrying. Caller should raise."""

    error: BaseException


RoundOutcome = Success | Continue | Retry | Exhausted | Failed
