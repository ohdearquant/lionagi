"""The ADR-0132 D4 pilot runner's summary: the D4 rule and when it is left unset."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PILOT = Path(__file__).resolve().parents[2] / "benchmarks" / "lndl_pilot"
_SPEC = importlib.util.spec_from_file_location("lndl_pilot_runner", PILOT / "run_pilot.py")
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)

LOOPS = ["react", "lndl"]


def _row(task: str, loop: str, *, requests: int, accepted: bool, spend_lines: int = 1) -> dict:
    return {
        "id": task,
        "loop": loop,
        "requests": requests if spend_lines else None,
        "tool_calls": 1 if spend_lines else None,
        "wall_seconds": 1.0 if spend_lines else None,
        "spend_lines": spend_lines,
        "accepted": accepted,
    }


def test_the_rule_is_computed_when_every_leg_has_a_spend_line():
    rows = [
        _row("t01", "react", requests=4, accepted=True),
        _row("t02", "react", requests=4, accepted=True),
        _row("t01", "lndl", requests=3, accepted=True),
        _row("t02", "lndl", requests=3, accepted=True),
    ]
    summary = runner.summarize(rows, LOOPS)
    assert summary["d4_rule"] == {
        "lndl_fewer_requests_per_accepted": True,
        "lndl_accepts_no_fewer": True,
    }
    assert "d4_rule_unset" not in summary


@pytest.mark.parametrize("loop", LOOPS)
def test_a_leg_with_no_spend_line_leaves_the_rule_unset(loop):
    rows = [
        _row("t01", "react", requests=4, accepted=True),
        _row("t02", "react", requests=4, accepted=True),
        _row("t01", "lndl", requests=3, accepted=True),
        _row("t02", "lndl", requests=3, accepted=True),
    ]
    rows[[r["loop"] for r in rows].index(loop)] = _row(
        "t01", loop, requests=0, accepted=True, spend_lines=0
    )
    summary = runner.summarize(rows, LOOPS)
    # Counted as zero requests, the leg would make its loop look cheaper; the rule is not computed.
    assert summary[loop]["missing_spend"] == 1
    assert summary["d4_rule"] is None
    assert summary["d4_rule_unset"] == "1 leg(s) have no spend line"


@pytest.mark.parametrize("run", ["r2", "r3", "r4"])
def test_each_committed_summary_is_what_the_runner_computes_from_its_rows(run):
    rows = [
        json.loads(line)
        for line in (PILOT / "runs" / run / "results.jsonl").read_text().splitlines()
    ]
    committed = json.loads((PILOT / "runs" / run / "summary.json").read_text())
    assert len(rows) == 48
    assert runner.summarize(rows, LOOPS) == committed
