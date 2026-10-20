"""The ADR-0132 D4 pilot: its summaries and the runner each was computed by, the D4 rule
and when it is left unset, the inputs `summarize.py` refuses, the task files' checks, and
the run figures its README and ADR-0132 give."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path
from unittest import mock

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PILOT = REPO / "benchmarks" / "lndl_pilot"
ADR = REPO / "docs" / "adr" / "ADR-0132-batched-lndl-rounds-for-api-model-legs.md"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load("lndl_pilot_runner", PILOT / "run_pilot.py")
pilot = _load("lndl_pilot_summarize", PILOT / "summarize.py")

LOOPS = ["react", "lndl"]
RUNS = ["r2", "r3", "r4", "r5"]
# The runner each run ran, byte for byte; its sha256 is the run's `runner_sha256`.
AS_RUN = {
    "r2": "runs/run_pilot.as-run.py.txt",
    "r3": "runs/run_pilot.as-run.py.txt",
    "r4": "runs/run_pilot.as-run.py.txt",
    "r5": "runs/r5/run_pilot.as-run.py.txt",
}
TASK_FILES = ["tasks.yaml", "runs/tasks.r2-r4.yaml", "runs/tasks.r5.yaml"]


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
        # A source is a full path; the file's name alone stands for the path only in the
        # truth, and only where the truth names the path.
        ("lionagi/mcp/config.py:16 — import; config.py:19 — RUNS_DIR", "config.py:19", False),
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


def _duplicate_a_result(run_dir: Path) -> None:
    rows = _jsonl(run_dir / "results.jsonl")
    _write_jsonl(run_dir / "results.jsonl", [*rows, rows[0]])


def _duplicate_a_reading(run_dir: Path) -> None:
    reading = _jsonl(run_dir / "reading.jsonl")
    first = next(row for row in reading if (row["id"], row["loop"]) == ("t01", "react"))
    _write_jsonl(run_dir / "reading.jsonl", [*reading, first])


def _drop_a_reading(run_dir: Path) -> None:
    reading = _jsonl(run_dir / "reading.jsonl")
    _write_jsonl(
        run_dir / "reading.jsonl", [r for r in reading if (r["id"], r["loop"]) != ("t03", "lndl")]
    )


def _read_as_maybe(run_dir: Path) -> None:
    reading = _jsonl(run_dir / "reading.jsonl")
    for row in reading:
        if (row["id"], row["loop"]) == ("t01", "react"):
            row["reading"] = "maybe"
    _write_jsonl(run_dir / "reading.jsonl", reading)


def _pin_another_task_set(run_dir: Path) -> None:
    header = json.loads((run_dir / "header.json").read_text())
    header["tasks_sha256"] = "0" * 64
    (run_dir / "header.json").write_text(json.dumps(header))


def test_an_unchanged_copy_of_a_run_is_accepted(r5_copy):
    committed = json.loads((PILOT / "runs" / "r5" / "summary.json").read_text())
    assert pilot.build_summary(r5_copy) == committed


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_duplicate_a_result, "the results do not hold each task and loop exactly once"),
        (_duplicate_a_reading, r"two readings for \('t01', 'react'\)"),
        (_drop_a_reading, "the reading does not cover each leg exactly once"),
        (_read_as_maybe, r"reading 'maybe' for \('t01', 'react'\)"),
        (_pin_another_task_set, "runs/tasks.r5.yaml is not the task set the run used"),
    ],
)
def test_a_malformed_run_is_refused(r5_copy, change, message):
    change(r5_copy)
    with pytest.raises(ValueError, match=message):
        pilot.build_summary(r5_copy)


def _as_run(run: str) -> types.ModuleType:
    path = PILOT / AS_RUN[run]
    header = json.loads((PILOT / "runs" / run / "header.json").read_text())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == header["runner_sha256"]
    module = types.ModuleType(f"lndl_pilot_as_run_{run}")
    module.__file__ = str(path)
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)  # noqa: S102
    return module


@pytest.mark.parametrize("run", RUNS)
def test_each_summary_is_what_the_runner_as_run_computes(run):
    """`summarize.py` computes with `run_pilot.py`; the runner each run ran gives the same."""
    as_run = _as_run(run)
    run_dir = PILOT / "runs" / run
    committed = json.loads((run_dir / "summary.json").read_text())
    rows = sorted(_jsonl(run_dir / "results.jsonl"), key=lambda r: (r["id"], r["loop"]))
    reading = {(r["id"], r["loop"]): r["reading"] for r in _jsonl(run_dir / "reading.jsonl")}
    by_checks = as_run.summarize(rows, LOOPS)
    by_reading = as_run.summarize(
        [dict(r, accepted=reading[(r["id"], r["loop"])] == "right") for r in rows], LOOPS
    )
    assert {loop: by_checks[loop] for loop in LOOPS} == {loop: committed[loop] for loop in LOOPS}
    assert by_checks["d4_rule"] == committed["d4_rule_by_checks"]
    assert committed["d4_rule_by_substance"] == {
        "react_accepted": by_reading["react"]["accepted"],
        "lndl_accepted": by_reading["lndl"]["accepted"],
        "react_requests_per_accepted": by_reading["react"]["requests_per_accepted"],
        "lndl_requests_per_accepted": by_reading["lndl"]["requests_per_accepted"],
        **by_reading["d4_rule"],
    }


@pytest.mark.parametrize("tasks_file", TASK_FILES)
def test_each_reference_answer_passes_its_check_and_each_decoy_fails(tasks_file):
    tasks = yaml.safe_load((PILOT / tasks_file).read_text())["tasks"]
    assert len(tasks) == 24
    wrong = [t["id"] for t in tasks if not runner.check_answer(t["reference_answer"], t["accept"])]
    wrong += [t["id"] for t in tasks if runner.check_answer(t["decoy_answer"], t["accept"])]
    assert wrong == []


@pytest.mark.parametrize("tasks_file", TASK_FILES)
def test_no_task_file_has_a_none_of_outside_its_check(tasks_file):
    tasks = yaml.safe_load((PILOT / tasks_file).read_text())["tasks"]
    assert [t["id"] for t in tasks if t.get("none_of")] == []


class _Reached(Exception):
    pass


@pytest.mark.parametrize("misplaced", [True, False])
def test_a_none_of_outside_the_check_is_refused_before_anything_runs(
    tmp_path, monkeypatch, capsys, misplaced
):
    tasks = yaml.safe_load((PILOT / "tasks.yaml").read_text())
    if misplaced:
        tasks["tasks"][0]["none_of"] = ["131072"]
    tasks_file = tmp_path / "tasks.yaml"
    tasks_file.write_text(yaml.safe_dump(tasks))
    out = tmp_path / "out"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    monkeypatch.setattr(
        sys, "argv", ["run_pilot.py", "--tasks", str(tasks_file), "--out", str(out)]
    )
    # The first process the runner starts is the header's `git rev-parse`.
    with mock.patch.object(runner.subprocess, "run", side_effect=_Reached):
        if misplaced:
            assert runner.main() == 2
        else:
            with pytest.raises(_Reached):
                runner.main()
    assert out.exists() is not misplaced
    if misplaced:
        assert "none_of outside accept in t01; nothing run" in capsys.readouterr().err


def _flat(path: Path) -> str:
    return " ".join(path.read_text().split())


def _figures(run: str) -> dict:
    run_dir = PILOT / "runs" / run
    summary = pilot.build_summary(run_dir)
    environment = json.loads((run_dir / "environment.json").read_text())
    substance = summary["d4_rule_by_substance"]
    figures = {"commit": environment["lionagi_checkout_head"]}
    for loop in LOOPS:
        reading = substance[f"{loop}_accepted"]
        figures[loop] = {
            "tasks": summary[loop]["runs"],
            "checks": summary[loop]["accepted"],
            "reading": reading,
            "requests": summary[loop]["requests"],
            # Two places from the counts, not the stored three-place value rounded again.
            "per_reading": f"{summary[loop]['requests'] / reading:.2f}",
            "per_checks": f"{summary[loop]['requests'] / summary[loop]['accepted']:.2f}",
            "tools": summary[loop]["tool_calls"],
            "wall": summary[loop]["wall_seconds"],
        }
    figures["met"] = all(
        substance[k] for k in ("lndl_fewer_requests_per_accepted", "lndl_accepts_no_fewer")
    )
    figures["met_by_checks"] = all(summary["d4_rule_by_checks"].values())
    return figures


def _table_rows(run: str, figures: dict) -> tuple[list[str], list[str]]:
    readme, adr = [], []
    for loop in LOOPS:
        f = figures[loop]
        counts = f"{f['checks']} / {f['tasks']} | {f['reading']} / {f['tasks']} | {f['requests']}"
        readme.append(
            f"| {run} | {loop} | {counts} | {f['per_reading']} | {f['tools']} | {f['wall']} s |"
        )
        adr.append(
            f"| {run} | `{figures['commit'][:8]}` | {loop} | {counts} | {f['per_reading']} "
            f"| {f['tools']} |"
        )
    return readme, adr


def _verb(figures: dict) -> str:
    return "meets" if figures["met"] else "fails"


def test_the_readme_gives_the_runs_own_figures():
    figures = {run: _figures(run) for run in RUNS}
    expected = []
    for run in RUNS:
        expected += _table_rows(run, figures[run])[0]
        expected.append(f"| {run} | `{figures[run]['commit'][:8]}` |")
        expected.append(figures[run]["commit"])
    r3, r4, r5 = figures["r3"], figures["r4"], figures["r5"]
    assert r3["lndl"]["reading"] == r3["react"]["reading"]
    assert r5["lndl"]["checks"] == r5["react"]["checks"]
    expected += [
        f"r2 {_verb(figures['r2'])} the rule",
        f"r3 {_verb(r3)} it with no margin ({r3['lndl']['reading']} accepted tasks on each side)",
        f"r4 {_verb(r4)} it (`lndl` {r4['lndl']['reading']} against ReAct {r4['react']['reading']})",
        f"r5, the run ADR-0132 D4 names as deciding, {_verb(r5)} it: `lndl` "
        f"{r5['lndl']['reading']} at {r5['lndl']['per_reading']} requests per accepted task "
        f"against ReAct {r5['react']['reading']} at {r5['react']['per_reading']}",
        f"By its checks r5 {'meets' if r5['met_by_checks'] else 'fails'} the rule with "
        f"{r5['lndl']['checks']} on each side",
    ]
    text = _flat(PILOT / "README.md")
    assert [line for line in expected if line not in text] == []


def test_adr_0132_gives_the_runs_own_figures():
    figures = {run: _figures(run) for run in RUNS}
    r3, r4, r5 = figures["r3"], figures["r4"], figures["r5"]
    fewer = r5["react"]["reading"] - r5["lndl"]["reading"]
    assert fewer == 1
    expected = [row for run in RUNS for row in _table_rows(run, figures[run])[1]]
    expected += [
        # The header and Amendment 1.
        f"`r5`, the run that decides, {'meets' if r5['met'] else 'does not meet'} the rule: "
        "`lndl` accepted one task fewer than ReAct",
        f"| each task's check, as r5 ran them | {r5['lndl']['checks']} / 24 | "
        f"{r5['react']['checks']} / 24 | {r5['lndl']['per_checks']} | "
        f"{r5['react']['per_checks']} | {'met' if r5['met_by_checks'] else 'not met'} |",
        f"| the reading | {r5['lndl']['reading']} / 24 | {r5['react']['reading']} / 24 | "
        f"{r5['lndl']['per_reading']} | {r5['react']['per_reading']} | "
        f"{'met' if r5['met'] else 'not met'} |",
        f"give {r5['lndl']['reading']} against {r5['react']['reading']} as well",
        # D4, Result.
        f"r3 {_verb(r3)} the rule with no margin: {r3['lndl']['reading']} tasks accepted on each "
        f"side, and `lndl` used {r3['lndl']['requests']} requests against "
        f"{r3['react']['requests']} ({r3['lndl']['per_reading']} per accepted task against "
        f"{r3['react']['per_reading']})",
        f"On reading it {_verb(r4)} the rule: `lndl` used fewer requests per accepted task "
        f"({r4['lndl']['requests']} requests, {r4['lndl']['per_reading']}, against "
        f"{r4['react']['requests']} and {r4['react']['per_reading']}) and accepted one task "
        f"fewer ({r4['lndl']['reading']} against {r4['react']['reading']})",
        f"The checks give the same outcome through different tasks ({r4['lndl']['checks']} "
        f"against {r4['react']['checks']})",
        f"The request saving held in both: {r3['lndl']['requests']} requests against "
        f"{r3['react']['requests']}, and {r4['lndl']['requests']} against "
        f"{r4['react']['requests']}",
        f"Its checks accept {r5['lndl']['checks']} tasks on each side",
        f"`lndl` accepted {r5['lndl']['reading']} tasks at {r5['lndl']['per_reading']} requests "
        f"per accepted task ({r5['lndl']['requests']} requests) and ReAct "
        f"{r5['react']['reading']} at {r5['react']['per_reading']} "
        f"({r5['react']['requests']} requests)",
        f"so r5 {'meets' if r5['met'] else 'does not meet'} the rule",
    ]
    text = _flat(ADR)
    assert [line for line in expected if line not in text] == []


def test_the_checks_as_they_now_stand_differ_from_r5s_only_on_its_lndl_t24_answer():
    tasks = {t["id"]: t for t in yaml.safe_load((PILOT / "tasks.yaml").read_text())["tasks"]}
    run_dir = PILOT / "runs" / "r5"
    answers = {(a["id"], a["loop"]): a["answer"] for a in _jsonl(run_dir / "answers.jsonl")}
    as_run = {(r["id"], r["loop"]): r["accepted"] for r in _jsonl(run_dir / "results.jsonl")}
    now = {
        key: runner.check_answer(answer, tasks[key[0]]["accept"]) for key, answer in answers.items()
    }
    assert len(now) == 48
    assert [key for key in now if now[key] != as_run[key]] == [("t24", "lndl")]
    reading = {(r["id"], r["loop"]): r["reading"] for r in _jsonl(run_dir / "reading.jsonl")}
    assert {loop: sum(now[k] for k in now if k[1] == loop) for loop in LOOPS} == {
        loop: sum(reading[k] == "right" for k in reading if k[1] == loop) for loop in LOOPS
    }
