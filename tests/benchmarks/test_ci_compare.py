from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CI_COMPARE = Path(__file__).parents[2] / "benchmarks" / "ci_compare.py"

_RESULT_JSON = {
    "results": {"scenario": {"runs": 1, "min": 1.0, "mean": 1.0, "median": 1.0, "max": 1.0}}
}


def _run_cli(baseline: str, current: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CI_COMPARE), "--baseline", baseline, "--current", current],
        check=False,
        capture_output=True,
        text=True,
    )


def test_missing_baseline_hard_fails(tmp_path: Path) -> None:
    """A missing baseline JSON is a hard failure (exit 1), not a graceful skip.

    The same-machine A/B CI setup always produces a baseline before invoking
    ci_compare.py, so a missing baseline means the setup itself failed and
    must fail loud rather than silently disabling the regression gate.
    """
    current = tmp_path / "current.json"
    current.write_text(json.dumps(_RESULT_JSON), encoding="utf-8")
    missing_baseline = tmp_path / "no-such-baseline.json"

    completed = _run_cli(str(missing_baseline), str(current))

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "Baseline missing" in completed.stdout


def test_missing_current_hard_fails(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(_RESULT_JSON), encoding="utf-8")
    missing_current = tmp_path / "no-such-current.json"

    completed = _run_cli(str(baseline), str(missing_current))

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "Current results missing" in completed.stdout


def test_matching_results_pass(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(_RESULT_JSON), encoding="utf-8")
    current.write_text(json.dumps(_RESULT_JSON), encoding="utf-8")

    completed = _run_cli(str(baseline), str(current))

    assert completed.returncode == 0, completed.stdout + completed.stderr
