# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""RoundOutcome — algebraic data type for one LNDL round's result.

A multi-round LNDL run is a state machine: each round produces an outcome,
and the outer loop matches on it to decide what to do next. Replaces the
ad-hoc branches (parse-fail, validate-fail, missing-out, etc.) with one
small set of variants.

Ported from krons.agent.operations.round_outcome.
"""

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
    """No OUT{} block this round — model is still thinking. Lacts that
    ran this round have already been persisted as tool messages, so the
    next round sees their results in chat history."""

    notes_committed: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class Retry:
    """OUT{} produced but parse/resolve/validate failed. Feed ``error`` to
    the model in the next round so it can self-correct. Scratchpad and
    chat history from prior rounds remain intact."""

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
