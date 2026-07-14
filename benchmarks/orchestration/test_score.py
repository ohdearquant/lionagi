"""Tests for the orchestration benchmark aggregate report."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.task import ScoredResult  # noqa: E402
from score import report  # noqa: E402


def test_compute_table_reports_mean_cache_read_and_write_tokens(capsys):
    scored = [
        ScoredResult(
            task_id="task-a",
            config_key="single__first",
            trial=1,
            found_defect=True,
            false_positive=False,
            engaged=True,
            reported_severity="high",
            severity_error=0,
            input_tokens=80,
            cached_tokens=200,
            cache_write_tokens=400,
            output_tokens=160,
            est_cost_usd=0.01,
            usage_source="reported",
        ),
        ScoredResult(
            task_id="task-b",
            config_key="single__second",
            trial=2,
            found_defect=True,
            false_positive=False,
            engaged=True,
            reported_severity="high",
            severity_error=0,
            input_tokens=120,
            cached_tokens=400,
            cache_write_tokens=600,
            output_tokens=240,
            est_cost_usd=0.02,
            usage_source="reported",
        ),
    ]

    report(scored, {"task-a": "defect", "task-b": "defect"})

    output = capsys.readouterr().out
    assert "cached read" in output
    assert "cache write" in output
    compute_output = output.split("COMPUTE & COST", 1)[1]
    row = next(line for line in compute_output.splitlines() if line.startswith("single"))
    assert re.match(r"single\s+100\s+300\s+500\s+200\s+", row)
