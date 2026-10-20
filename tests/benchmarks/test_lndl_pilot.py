"""The ADR-0132 D4 pilot's summaries: the D4 rule, when it is left unset, and the
inputs `summarize.py` refuses."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from unittest import mock

import pytest

PILOT = Path(__file__).resolve().parents[2] / "benchmarks" / "lndl_pilot"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load("lndl_pilot_runner", PILOT / "run_pilot.py")
# summarize.py imports the runner as its script sibling (`from run_pilot import ...`).
with mock.patch.dict(sys.modules, {"run_pilot": runner}):
    pilot = _load("lndl_pilot_summarize", PILOT / "summarize.py")

LOOPS = ["react", "lndl"]
RUNS = ["r2", "r3", "r4", "r5"]


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


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.fixture
def r5_copy(tmp_path: Path) -> Path:
    run_dir = tmp_path / "r5"
    run_dir.mkdir()
    for name in ("header.json", "results.jsonl", "reading.jsonl"):
        shutil.copy(PILOT / "runs" / "r5" / name, run_dir / name)
    return run_dir


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


@pytest.mark.parametrize("run", RUNS)
def test_each_committed_summary_is_what_summarize_computes(run):
    run_dir = PILOT / "runs" / run
    committed = json.loads((run_dir / "summary.json").read_text())
    rows = _jsonl(run_dir / "results.jsonl")
    assert len(rows) == 48
    assert pilot.build_summary(run_dir) == committed
    totals = runner.summarize(rows, LOOPS)
    assert {loop: totals[loop] for loop in LOOPS} == {loop: committed[loop] for loop in LOOPS}


def test_a_leg_with_no_spend_line_leaves_both_rules_unset(r5_copy):
    rows = _jsonl(r5_copy / "results.jsonl")
    rows[0].update(requests=None, tool_calls=None, wall_seconds=None, spend_lines=0)
    _write_jsonl(r5_copy / "results.jsonl", rows)
    summary = pilot.build_summary(r5_copy)
    assert summary["d4_rule_by_checks"] is None
    assert summary["d4_rule_by_substance"] is None
    assert summary["d4_rule_unset"] == "1 leg(s) have no spend line"


@pytest.mark.parametrize(
    "source",
    [
        "",
        "11",
        "lionagi/_spec_limits.py",
        "lionagi/version.py:1",
        "lionagi/_spec_limits.py:1",
        "_spec_limits.py:11",
    ],
)
def test_a_reading_source_the_truth_does_not_cite_is_refused(r5_copy, source):
    reading = _jsonl(r5_copy / "reading.jsonl")
    for row in reading:
        if (row["id"], row["loop"]) == ("t01", "react"):
            row["source_6fa69b19"] = source
    _write_jsonl(r5_copy / "reading.jsonl", reading)
    with pytest.raises(ValueError, match="which the task's truth does not"):
        pilot.build_summary(r5_copy)


@pytest.mark.parametrize(
    ("truth", "source", "cited"),
    [
        ("lionagi/_spec_limits.py:11 — MAX", "lionagi/_spec_limits.py:11", True),
        (
            "lionagi/mcp/config.py:16 — import; config.py:19 — RUNS_DIR",
            "lionagi/mcp/config.py:19",
            True,
        ),
        ("lionagi/mcp/config.py:16 — import; config.py:19 — RUNS_DIR", "config.py:19", True),
        # The file's name alone stands for the path only where the truth names the path.
        ("_spec_limits.py:11 — MAX", "lionagi/_spec_limits.py:11", False),
        ("lionagi/_spec_limits.py:11 — MAX", "_spec_limits.py:11", False),
        ("lionagi/_spec_limits.py:110 — MAX", "lionagi/_spec_limits.py:11", False),
    ],
)
def test_cited_matches_a_path_or_a_name_the_truth_gives(truth, source, cited):
    assert pilot._cited(truth, source) is cited


def test_a_leg_missing_from_both_files_is_refused(r5_copy):
    for name in ("results.jsonl", "reading.jsonl"):
        rows = [r for r in _jsonl(r5_copy / name) if (r["id"], r["loop"]) != ("t03", "lndl")]
        _write_jsonl(r5_copy / name, rows)
    with pytest.raises(ValueError, match="the results do not hold each task and loop exactly once"):
        pilot.build_summary(r5_copy)


def test_a_reading_row_with_an_unknown_loop_is_refused(r5_copy):
    reading = _jsonl(r5_copy / "reading.jsonl")
    for row in reading:
        if (row["id"], row["loop"]) == ("t05", "lndl"):
            row["loop"] = "LNDL"
    _write_jsonl(r5_copy / "reading.jsonl", reading)
    with pytest.raises(ValueError, match="the reading does not cover each leg exactly once"):
        pilot.build_summary(r5_copy)


def test_a_reading_that_disagrees_with_the_check_is_refused(r5_copy):
    rows = _jsonl(r5_copy / "results.jsonl")
    for row in rows:
        if (row["id"], row["loop"]) == ("t01", "react"):
            row["accepted"] = False
    _write_jsonl(r5_copy / "results.jsonl", rows)
    with pytest.raises(ValueError, match="results say the check rejected it"):
        pilot.build_summary(r5_copy)


def test_a_task_file_that_is_not_there_is_refused(r5_copy):
    header = json.loads((r5_copy / "header.json").read_text())
    header["tasks_file"] = "runs/tasks.missing.yaml"
    (r5_copy / "header.json").write_text(json.dumps(header))
    with pytest.raises(ValueError, match="runs/tasks.missing.yaml is not in lndl_pilot/"):
        pilot.build_summary(r5_copy)
