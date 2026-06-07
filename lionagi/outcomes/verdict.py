# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0021 §A: review + gate verdicts.

Produced by codex-pr-review (ReviewVerdict), play-gate (GateVerdict),
or any skill that issues a binary or graded judgment.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

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
        ge=1,
        description="1-indexed line number when known.",
    )
    description: str = Field(
        description="What the reviewer noticed (one sentence preferred).",
    )
    suggestion: str | None = Field(
        default=None,
        description=(
            "Concrete fix the reviewer recommends. None when the finding is informational only."
        ),
    )

    @field_validator("file", mode="before")
    @classmethod
    def _validate_file(cls, v: object) -> object:
        """Reject absolute paths and parent-traversal components."""
        if v is None:
            return v
        if not isinstance(v, str):
            return v
        # Reject absolute paths (Unix and Windows).
        if v.startswith("/") or (len(v) >= 2 and v[1] == ":"):
            raise ValueError(f"Finding.file must be a repo-relative path, not absolute: {v!r}")
        # Reject any component that is '..' (traversal).
        parts = v.replace("\\", "/").split("/")
        if any(p == ".." for p in parts):
            raise ValueError(f"Finding.file must not contain parent-traversal components: {v!r}")
        # Reject NUL bytes (path injection).
        if "\x00" in v:
            raise ValueError("Finding.file must not contain NUL bytes.")
        return v


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

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, v: object) -> object:
        if isinstance(v, str):
            return v.replace("-", "_").replace(" ", "_")
        return v

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

    @model_validator(mode="after")
    def _sync_passed(self) -> GateVerdict:
        """Keep SkillOutcome.passed consistent with gate_passed.

        - When ``passed`` is omitted (None), default it to ``gate_passed``.
        - When both are supplied, they must agree (no contradictory state).
        """
        if self.passed is None:
            self.passed = self.gate_passed
        elif self.passed != self.gate_passed:
            raise ValueError(
                f"GateVerdict.gate_passed ({self.gate_passed}) and "
                f".passed ({self.passed}) must be the same value."
            )
        return self
