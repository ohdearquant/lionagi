# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0021 §A: review + gate verdicts.

Produced by codex-pr-review (ReviewVerdict), play-gate (GateVerdict),
or any skill that issues a binary or graded judgment.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from lionagi.models import HashableModel

from ._base import SkillOutcome

Severity = Literal["critical", "high", "medium", "low", "info"]


class Finding(HashableModel):
    """One reviewer finding with optional file/line + suggestion."""

    severity: Severity = Field(
        description="Operator severity bucket (drives sort + render color).",
    )
    category: str = Field(
        description=(
            "Short free-text taxonomy: 'security', 'correctness', "
            "'style', 'adr_consistency', 'scope', ..."
        ),
    )
    file: str | None = Field(
        default=None,
        description="Repo-relative path the finding applies to, if any.",
    )
    line: int | None = Field(
        default=None,
        description="1-indexed line number when known.",
    )
    description: str = Field(
        description="What the reviewer noticed (one sentence preferred).",
    )
    suggestion: str | None = Field(
        default=None,
        description=(
            "Concrete fix the reviewer recommends. None when the finding "
            "is informational only."
        ),
    )


VerdictDecision = Literal[
    "APPROVE",
    "APPROVE_WITH_SUGGESTIONS",
    "REQUEST_CHANGES",
    "REJECT",
]


class ReviewVerdict(SkillOutcome):
    """Reviewer judgment + findings list.

    The frontend renders this as the ``ReviewVerdictCard`` (ADR-0021 §E)
    — severity/category breakdown on top, blocking findings expanded,
    minor suggestions collapsed.
    """

    outcome_kind: Literal["review_verdict"] = "review_verdict"
    verdict: VerdictDecision = Field(
        description="Top-level decision; drives card color + downstream chain conditions.",
    )
    findings: list[Finding] = Field(
        default_factory=list,
        description="Findings list — blocking-first ordering is the writer's responsibility.",
    )
    round: int = Field(
        default=1,
        ge=1,
        description=(
            "1-indexed iteration number for multi-round reviews. The "
            "codex-pr-review skill writes one ReviewVerdict per round."
        ),
    )


class GateVerdict(SkillOutcome):
    """Acceptance-criteria gate outcome (play-gate, show-gate, etc.)."""

    outcome_kind: Literal["gate_verdict"] = "gate_verdict"
    gate_passed: bool = Field(
        description="Did the gate pass? (Mirrors plays.gate_passed in the DB.)",
    )
    feedback: str | None = Field(
        default=None,
        description="Reviewer-facing rationale shown on the play detail page.",
    )
    notes: str | None = Field(
        default=None,
        description="Operator notes (free text). Often empty; rendered when present.",
    )
