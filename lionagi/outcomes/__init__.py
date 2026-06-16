# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0021 outcome contracts — domain types (review, CI, gate) persisted as Studio artifact rows."""

from __future__ import annotations

from ._base import SkillOutcome
from .ci import CIResult
from .verdict import GateVerdict, ReviewFinding, ReviewOutcome

__all__ = (
    "CIResult",
    "GateVerdict",
    "ReviewFinding",
    "ReviewOutcome",
    "SkillOutcome",
)
