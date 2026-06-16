# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0021 §A: SkillOutcome base type."""

from __future__ import annotations

from pydantic import Field

from lionagi.models import HashableModel


class SkillOutcome(HashableModel):
    """Base for all structured skill outputs; subclasses set ``outcome_kind`` to a Literal string."""

    outcome_kind: str = Field(
        description=(
            "Discriminator key for the kind-dispatched renderer. Subclasses "
            "narrow this to a Literal[...] of one value."
        )
    )
    summary: str | None = Field(
        default=None,
        description=(
            "One-line human-readable summary. Shown in list views before "
            "the operator clicks through to the full structured outcome."
        ),
    )
    passed: bool | None = Field(
        default=None,
        description=(
            "Tri-state pass/fail. True/False for binary outcomes (gate, CI); "
            "None when not applicable (e.g. a research analysis with no "
            "pass/fail semantics)."
        ),
    )
