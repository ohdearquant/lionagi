# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0021 outcome model tests.

The outcomes package is the contract between skill producers and Studio
consumers. These tests pin the public schema so any breaking change
shows up here, not in a downstream serialization mismatch.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lionagi.outcomes import (
    CIResult,
    Finding,
    GateVerdict,
    ReviewVerdict,
    SkillOutcome,
)
from lionagi.outcomes.ci import CIRunCommand

# ── SkillOutcome base ─────────────────────────────────────────────────────────


def test_skill_outcome_passed_defaults_to_none():
    o = SkillOutcome(outcome_kind="x", summary="hello")
    assert o.passed is None
    assert o.outcome_kind == "x"
    assert o.summary == "hello"


def test_skill_outcome_dump_round_trips():
    o = SkillOutcome(outcome_kind="x", summary="hello", passed=True)
    dumped = o.model_dump()
    again = SkillOutcome.model_validate(dumped)
    assert again == o


def test_review_verdict_default_outcome_kind_pinned():
    """ADR-0021 promises kind='review_verdict' — frontend dispatch depends on it."""
    v = ReviewVerdict(
        verdict="APPROVE",
        summary="LGTM",
    )
    assert v.outcome_kind == "review_verdict"
    assert v.round == 1
    assert v.findings == []


def test_review_verdict_accepts_hyphenated_producer_string():
    """Producer strings like APPROVE-WITH-SUGGESTIONS are normalized on ingest."""
    v = ReviewVerdict.model_validate({"verdict": "APPROVE-WITH-SUGGESTIONS", "summary": "ok"})
    assert v.verdict == "APPROVE_WITH_SUGGESTIONS"


def test_review_verdict_accepts_spaced_producer_string():
    v = ReviewVerdict.model_validate({"verdict": "REQUEST CHANGES", "summary": "ok"})
    assert v.verdict == "REQUEST_CHANGES"


def test_review_verdict_rejects_unknown_decision():
    with pytest.raises(ValidationError):
        ReviewVerdict(verdict="MEH", summary="?")


def test_review_verdict_round_must_be_positive():
    with pytest.raises(ValidationError):
        ReviewVerdict(verdict="APPROVE", summary="ok", round=0)


def test_finding_severity_constrained():
    Finding(
        severity="critical",
        category="security",
        description="rm -rf in user input",
    )
    with pytest.raises(ValidationError):
        Finding(severity="hot", category="x", description="y")


def test_review_verdict_dump_round_trips():
    v = ReviewVerdict(
        verdict="REQUEST_CHANGES",
        summary="3 issues",
        passed=False,
        round=2,
        findings=[
            Finding(
                severity="high",
                category="correctness",
                file="src/main.py",
                line=42,
                description="off-by-one",
                suggestion="use range(n+1)",
            )
        ],
    )
    dumped = v.model_dump()
    assert dumped["outcome_kind"] == "review_verdict"
    again = ReviewVerdict.model_validate(dumped)
    assert again == v


# ── GateVerdict ───────────────────────────────────────────────────────────────


def test_gate_verdict_without_summary():
    """GateVerdict must validate raw play-gate JSON that has no summary field."""
    g = GateVerdict.model_validate({"gate_passed": True, "passed": True})
    assert g.summary is None
    assert g.gate_passed is True
    assert g.outcome_kind == "gate_verdict"


def test_gate_verdict_dump_round_trips():
    g = GateVerdict(
        summary="gate failed: missing artifact",
        gate_passed=False,
        feedback="implementation_1012.md missing from artifact path",
        passed=False,
    )
    dumped = g.model_dump()
    assert dumped["outcome_kind"] == "gate_verdict"
    assert dumped["gate_passed"] is False
    assert GateVerdict.model_validate(dumped) == g


# ── CIResult ──────────────────────────────────────────────────────────────────


def test_ci_result_all_optional_when_step_skipped():
    """A CIResult that ran only tests has None for lint/build/typecheck."""
    r = CIResult(
        summary="119/119",
        passed=True,
        tests_passed=True,
        test_count=119,
        test_failures=0,
    )
    assert r.lint_passed is None
    assert r.build_passed is None
    assert r.typecheck_passed is None


def test_ci_run_command_rejects_negative_duration():
    with pytest.raises(ValidationError):
        CIRunCommand(command="pytest", duration_seconds=-1, passed=True)


def test_ci_result_dump_round_trips():
    r = CIResult(
        summary="all green",
        passed=True,
        tests_passed=True,
        lint_passed=True,
        test_count=42,
        test_failures=0,
        commands=[
            CIRunCommand(command="pytest", duration_seconds=12.5, passed=True),
            CIRunCommand(command="ruff", duration_seconds=0.8, passed=True),
        ],
    )
    dumped = r.model_dump()
    assert dumped["outcome_kind"] == "ci_result"
    again = CIResult.model_validate(dumped)
    assert again == r


# ── Kind dispatch contract ────────────────────────────────────────────────────


def test_outcome_kinds_are_distinct():
    """Each concrete outcome must have a unique outcome_kind string —
    the frontend's switch dispatches on this value."""
    kinds = {
        ReviewVerdict(verdict="APPROVE", summary="x").outcome_kind,
        GateVerdict(summary="x", gate_passed=True).outcome_kind,
        CIResult(summary="x").outcome_kind,
    }
    assert len(kinds) == 3


def test_finding_rejects_absolute_unix_path():
    with pytest.raises(ValidationError, match="repo-relative"):
        Finding(severity="high", category="security", description="x", file="/etc/passwd")


def test_finding_rejects_absolute_windows_path():
    with pytest.raises(ValidationError, match="repo-relative"):
        Finding(severity="high", category="security", description="x", file="C:\\secret.py")


def test_finding_rejects_parent_traversal():
    with pytest.raises(ValidationError, match="traversal"):
        Finding(severity="high", category="security", description="x", file="../secret.py")


def test_finding_rejects_deep_traversal():
    with pytest.raises(ValidationError, match="traversal"):
        Finding(
            severity="medium",
            category="correctness",
            description="x",
            file="foo/../../etc/passwd",
        )


def test_finding_rejects_nul_byte():
    with pytest.raises(ValidationError, match="NUL"):
        Finding(severity="low", category="style", description="x", file="foo\x00bar.py")


def test_finding_rejects_line_zero():
    """Line numbers must be 1-indexed (ge=1); 0 is invalid."""
    with pytest.raises(ValidationError):
        Finding(severity="low", category="style", description="x", line=0)


def test_finding_rejects_negative_line():
    with pytest.raises(ValidationError):
        Finding(severity="low", category="style", description="x", line=-3)


def test_finding_accepts_valid_relative_path():
    f = Finding(severity="low", category="style", description="x", file="src/main.py")
    assert f.file == "src/main.py"


def test_finding_accepts_none_file():
    f = Finding(severity="low", category="style", description="x", file=None)
    assert f.file is None


def test_finding_accepts_positive_line():
    f = Finding(severity="info", category="docs", description="x", line=42)
    assert f.line == 42


def test_gate_verdict_defaults_passed_to_gate_passed_true():
    """When passed is omitted, it defaults to gate_passed (LIONAGI-AUDIT-003)."""
    g = GateVerdict(gate_passed=True, summary="ok")
    assert g.passed is True


def test_gate_verdict_defaults_passed_to_gate_passed_false():
    g = GateVerdict(gate_passed=False, summary="nope")
    assert g.passed is False


def test_gate_verdict_rejects_contradictory_passed():
    """gate_passed=True and passed=False is a contradictory state and must fail."""
    with pytest.raises(ValidationError, match="gate_passed"):
        GateVerdict(gate_passed=True, passed=False, summary="contradicts")


def test_gate_verdict_rejects_contradictory_passed_reversed():
    with pytest.raises(ValidationError, match="gate_passed"):
        GateVerdict(gate_passed=False, passed=True, summary="contradicts")


def test_gate_verdict_consistent_both_true():
    g = GateVerdict(gate_passed=True, passed=True, summary="consistent")
    assert g.gate_passed is True
    assert g.passed is True


def test_gate_verdict_consistent_both_false():
    g = GateVerdict(gate_passed=False, passed=False, summary="consistent")
    assert g.gate_passed is False
    assert g.passed is False
