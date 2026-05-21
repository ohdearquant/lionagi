# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0021 §A: CI / lint / test outcome models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from lionagi.models import HashableModel

from ._base import SkillOutcome


class CIRunCommand(HashableModel):
    """One executed CI command with timing.

    The CIResultCard renders these in a "Commands" panel below the
    pass/fail matrix.
    """

    command: str = Field(description="The shell command as executed.")
    duration_seconds: float = Field(
        ge=0,
        description="Wall-clock seconds the command took.",
    )
    passed: bool = Field(description="Did the command exit 0?")


class CIResult(SkillOutcome):
    """Aggregated CI outcome — lint + typecheck + tests + build."""

    outcome_kind: Literal["ci_result"] = "ci_result"
    lint_passed: bool | None = Field(
        default=None,
        description="None when lint wasn't run; bool otherwise.",
    )
    tests_passed: bool | None = Field(
        default=None,
        description="None when tests weren't run; bool otherwise.",
    )
    build_passed: bool | None = Field(
        default=None,
        description="None when build wasn't run; bool otherwise.",
    )
    typecheck_passed: bool | None = Field(
        default=None,
        description="None when typecheck wasn't run; bool otherwise.",
    )
    test_count: int | None = Field(
        default=None,
        ge=0,
        description="Total tests executed; None when tests weren't run.",
    )
    test_failures: int | None = Field(
        default=None,
        ge=0,
        description="Test failure count; None when tests weren't run.",
    )
    failure_summary: str | None = Field(
        default=None,
        description="Human-readable failure narrative when any step failed.",
    )
    commands: list[CIRunCommand] = Field(
        default_factory=list,
        description="Per-command timing for the Commands panel.",
    )
