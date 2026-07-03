"""Unit tests for the ADR-0088 gate-evaluation logic (report.py)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from suites.steering.arms import Arm  # noqa: E402
from suites.steering.report import MIN_VALID_N, _proportions, evaluate_gate  # noqa: E402
from suites.steering.runner import SteerRunResult  # noqa: E402


def _cell(provider: str, arm: Arm, adherent_count: int, n: int) -> list[SteerRunResult]:
    return [
        SteerRunResult(provider=provider, arm=arm.value, trial=i, adherent=i < adherent_count)
        for i in range(n)
    ]


def test_underpowered_two_provider_matrix_is_incomplete_not_pass():
    """Two-provider, one-trial-per-cell matrix (the proven counterexample) must never PASS."""
    results = []
    for provider in ("claude_code", "codex"):
        results += _cell(provider, Arm.NO_STEER, 0, 1)
        results += _cell(provider, Arm.STEER_BURIED, 0, 1)
        results += _cell(provider, Arm.STEER_RENDERED, 1, 1)
    verdict = evaluate_gate(_proportions(results))
    assert verdict["verdict"] == "INCOMPLETE"
    assert verdict["complete_providers"] == []


def test_two_complete_providers_clearing_the_gate_is_pass():
    """Exactly two complete provider families (>= MIN_VALID_N/arm) that clear is a real PASS."""
    n = MIN_VALID_N
    results = []
    for provider in ("claude_code", "codex"):
        results += _cell(provider, Arm.NO_STEER, 0, n)
        results += _cell(provider, Arm.STEER_BURIED, 0, n)
        results += _cell(provider, Arm.STEER_RENDERED, n, n)
    verdict = evaluate_gate(_proportions(results))
    assert verdict["verdict"] == "PASS"
    assert verdict["providers_clearing"] == 2
    assert set(verdict["complete_providers"]) == {"claude_code", "codex"}


def test_two_complete_providers_not_clearing_is_fail():
    n = MIN_VALID_N
    results = []
    for provider in ("claude_code", "codex"):
        results += _cell(provider, Arm.NO_STEER, 0, n)
        results += _cell(provider, Arm.STEER_BURIED, n, n)  # arm1 already adherent -> no lift
        results += _cell(provider, Arm.STEER_RENDERED, n, n)
    verdict = evaluate_gate(_proportions(results))
    assert verdict["verdict"] == "FAIL"
    assert verdict["providers_clearing"] == 0


def test_single_complete_provider_is_incomplete():
    """One complete provider alone cannot judge the >= 2-of-4 clause."""
    n = MIN_VALID_N
    results = []
    results += _cell("claude_code", Arm.NO_STEER, 0, n)
    results += _cell("claude_code", Arm.STEER_BURIED, 0, n)
    results += _cell("claude_code", Arm.STEER_RENDERED, n, n)
    verdict = evaluate_gate(_proportions(results))
    assert verdict["verdict"] == "INCOMPLETE"
    assert verdict["complete_providers"] == ["claude_code"]


def test_error_trials_do_not_count_toward_n():
    """Failed/errored trials must not count toward the MIN_VALID_N threshold."""
    n = MIN_VALID_N
    valid = _cell("claude_code", Arm.NO_STEER, 0, n - 1)
    errored = [
        SteerRunResult(provider="claude_code", arm=Arm.NO_STEER.value, trial=n, error="boom")
    ]
    props = _proportions(valid + errored)
    assert props[("claude_code", Arm.NO_STEER.value)].n == n - 1
