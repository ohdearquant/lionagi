# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0021: structured outputs that skills produce and Studio renders.

The shared contract between CLI skill runners (producers) and Studio
(consumer). Outcomes are persisted as ``artifacts`` table rows with
``kind = outcome_kind`` so the frontend's kind-dispatched renderer can
pick the right card component.

This package holds *domain* types (review verdicts, CI results, gate
outcomes). Infrastructure types live in :mod:`lionagi.models`; the
separation is deliberate so a future Studio-only consumer can import
outcomes without pulling in framework machinery.
"""

from ._base import SkillOutcome
from .ci import CIResult
from .verdict import Finding, GateVerdict, ReviewVerdict

__all__ = [
    "SkillOutcome",
    "ReviewVerdict",
    "GateVerdict",
    "Finding",
    "CIResult",
]
