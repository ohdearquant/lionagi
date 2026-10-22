"""The ADR-0132 D4 pilot: its summaries and the runner each was computed by, the D4 rule
and when it is left unset, the inputs `summarize.py` refuses, the task files' checks, and
the run figures its README and ADR-0132 give."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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


@pytest.mark.parametrize("named", ["absolute", "climbing", "absolute-inside"])
def test_a_task_file_outside_the_pilot_is_refused_though_its_hash_matches(r5_copy, named):
    outside = r5_copy.parent / "tasks.r5.yaml"
    shutil.copyfile(PILOT / "runs" / "tasks.r5.yaml", outside)
    name = {
        "absolute": str(outside),
        "climbing": os.path.relpath(outside, PILOT),
        # The run's own task file, named by a path that only holds on this machine.
        "absolute-inside": str(PILOT / "runs" / "tasks.r5.yaml"),
    }[named]
    header = json.loads((r5_copy / "header.json").read_text())
    header["tasks_file"] = name
    (r5_copy / "header.json").write_text(json.dumps(header))
    with pytest.raises(ValueError, match="is not in lndl_pilot/"):
        pilot.build_summary(r5_copy)


def test_a_path_that_leaves_the_pilot_and_comes_back_is_read(r5_copy):
    header = json.loads((r5_copy / "header.json").read_text())
    header["tasks_file"] = "../lndl_pilot/runs/tasks.r5.yaml"
    (r5_copy / "header.json").write_text(json.dumps(header))
    committed = json.loads((PILOT / "runs" / "r5" / "summary.json").read_text())
    assert pilot.build_summary(r5_copy) == committed


@pytest.mark.parametrize("linked", [True, False])
def test_a_task_file_in_the_pilot_that_links_out_of_it_is_refused(
    r5_copy, tmp_path, monkeypatch, linked
):
    here = tmp_path.resolve() / "lndl_pilot"
    (here / "runs").mkdir(parents=True)
    named = here / "runs" / "tasks.r5.yaml"
    if linked:
        outside = tmp_path / "tasks.r5.yaml"
        shutil.copyfile(PILOT / "runs" / "tasks.r5.yaml", outside)
        named.symlink_to(outside)
    else:
        shutil.copyfile(PILOT / "runs" / "tasks.r5.yaml", named)
    monkeypatch.setattr(pilot, "HERE", here)
    if linked:
        with pytest.raises(ValueError, match="runs/tasks.r5.yaml is not in lndl_pilot/"):
            pilot.build_summary(r5_copy)
    else:
        committed = json.loads((PILOT / "runs" / "r5" / "summary.json").read_text())
        assert pilot.build_summary(r5_copy) == committed


def test_summarize_loads_the_runner_beside_it_without_the_pilot_on_the_import_path(monkeypatch):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p or ".").resolve() != PILOT])
    for name in ("run_pilot", "summarize"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    loaded = _load("lndl_pilot_summarize_alone", PILOT / "summarize.py")
    assert loaded.summarize.__module__ == "lndl_pilot_run_pilot"
    assert "run_pilot" not in sys.modules


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
    # The read tree and `li` are made on the machine that runs the pilot, not committed.
    (tmp_path / "li").touch(mode=0o755)
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
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
    # The run directory is made after the header's calls, so neither arm leaves one: a task set
    # that passes the checks shows it by reaching the first of them.
    assert not out.exists()
    if misplaced:
        assert "t01: `none_of` outside `accept`\nnothing run" in capsys.readouterr().err


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


_RESULT_ROW = re.compile(
    r"^\| (r\d) \| `[0-9a-f]{8}` \| (react|lndl) \| (\d+) / 24 \| (\d+) / 24 \| (\d+) \| ([0-9.]+) \| (\d+) \|$"
)


@pytest.mark.parametrize("run", ["r2", "r3", "r4"])
def test_each_reading_covers_its_runs_rows_and_the_adr_result_table_is_its_tally(run):
    results = {(r["id"], r["loop"]): r for r in _jsonl(PILOT / "runs" / run / "results.jsonl")}
    reading = _jsonl(PILOT / "runs" / run / "reading.jsonl")
    assert sorted((r["id"], r["loop"]) for r in reading) == sorted(results)
    for r in reading:
        accepted = results[(r["id"], r["loop"])]["accepted"]
        assert r["check"] == ("accepted" if accepted else "rejected")
        assert r["reading"] in {"right", "wrong"}
    table = {}
    for line in ADR.read_text().splitlines():
        if m := _RESULT_ROW.match(line):
            assert (m[1], m[2]) not in table
            table[(m[1], m[2])] = (int(m[3]), int(m[4]), int(m[5]), m[6], int(m[7]))
    for loop in LOOPS:
        mine = [results[(r["id"], r["loop"])] | r for r in reading if r["loop"] == loop]
        right = sum(r["reading"] == "right" for r in mine)
        requests = sum(r["requests"] for r in mine)
        assert table[(run, loop)] == (
            sum(r["check"] == "accepted" for r in mine),
            right,
            requests,
            f"{requests / right:.2f}",
            sum(r["tool_calls"] for r in mine),
        )


def test_the_reading_and_the_checks_differ_in_the_eleven_answers_the_adr_names():
    differ = sorted(
        (run, r["id"], r["loop"])
        for run in ("r2", "r3", "r4", "r5")
        for r in _jsonl(PILOT / "runs" / run / "reading.jsonl")
        if (r["check"] == "accepted") != (r["reading"] == "right")
    )
    assert differ == [
        ("r2", "t06", "lndl"),
        ("r2", "t06", "react"),
        ("r2", "t24", "lndl"),
        ("r3", "t06", "lndl"),
        ("r3", "t06", "react"),
        ("r3", "t24", "lndl"),
        ("r4", "t06", "lndl"),
        ("r4", "t06", "react"),
        ("r4", "t20", "lndl"),
        ("r4", "t24", "lndl"),
        ("r5", "t24", "lndl"),
    ]


def test_the_t05_answers_the_readme_names_say_max_tokens_does_not_fit_the_field():
    answers = {
        (run, r["loop"]): r["answer"]
        for run in RUNS
        for r in _jsonl(PILOT / "runs" / run / "answers.jsonl")
        if r["id"] == "t05"
    }
    assert len(answers) == 8
    # A sentence of the answer names `max_tokens` and says it is not there or does not match.
    says_so = {
        key
        for key, answer in answers.items()
        if any(
            "max_tokens" in sentence and re.search(r"\b(?:no|not)\b", sentence)
            for sentence in re.split(r"(?<=[.;])\s+", answer)
        )
    }
    assert says_so == {
        ("r2", "react"),
        ("r2", "lndl"),
        ("r3", "react"),
        ("r4", "react"),
        ("r5", "react"),
        ("r5", "lndl"),
    }
    assert "It is a time limit, not a token count." in answers[("r3", "lndl")]
    readme = " ".join((PILOT / "README.md").read_text().split())
    assert (
        "Six of them also say the question's `max_tokens` does not fit the field (the r2 ReAct, "
        "r2 `lndl`, r3 ReAct and r4 ReAct answers and both r5 answers)"
    ) in readme


_R5_ROW = re.compile(
    # The one cell where the check and the reading differ is in bold.
    r"^\| `(t\d\d)` \| `([^`]+)` \| `([^`]+)` \| (?:\*\*)?(accepted|rejected), (right|wrong)(?:\*\*)? "
    r"\| (?:\*\*)?(accepted|rejected), (right|wrong)(?:\*\*)? \|$"
)


def test_the_readmes_r5_table_is_r5s_reading():
    text = (PILOT / "README.md").read_text().split("### r5, task by task", 1)[1]
    table = {}
    for line in text.splitlines():
        if m := _R5_ROW.match(line):
            table[m[1]] = (m[2], m[3], {"lndl": (m[4], m[5]), "react": (m[6], m[7])})
    reading = _jsonl(PILOT / "runs" / "r5" / "reading.jsonl")
    expected = {}
    for r in reading:
        sources, by_loop = expected.setdefault(r["id"], ((), {}))
        expected[r["id"]] = ((r["source_6fa69b19"], r["source_208bcc08"]), by_loop)
        assert sources in ((), expected[r["id"]][0])
        by_loop[r["loop"]] = (r["check"], r["reading"])
    assert len(table) == 24
    assert table == {tid: (*sources, by_loop) for tid, (sources, by_loop) in expected.items()}
    # "and one in r5"
    differ = [
        (tid, loop)
        for tid, (_, _, by_loop) in table.items()
        for loop, (check, read) in by_loop.items()
        if (check == "accepted") != (read == "right")
    ]
    assert differ == [("t24", "lndl")]


@pytest.mark.parametrize("run", RUNS)
def test_each_answer_is_the_text_its_row_measured_and_checked(run):
    """answers.jsonl holds each leg's answer as the check read it: its length is the row's
    `answer_chars`, the check of the task set the run ran, re-run on it, gives the row's
    `accepted`, and the last spend line gives the row's requests, tool calls and wall time."""
    as_run = "runs/tasks.r5.yaml" if run == "r5" else "runs/tasks.r2-r4.yaml"
    loaded = runner.yaml.load((PILOT / as_run).read_bytes(), Loader=runner._TaskLoader)
    accept = {task["id"]: task["accept"] for task in loaded["tasks"]}
    result_rows = _jsonl(PILOT / "runs" / run / "results.jsonl")
    answer_rows = _jsonl(PILOT / "runs" / run / "answers.jsonl")
    spend_rows = _jsonl(PILOT / "runs" / run / "spend.jsonl")
    # One line per row in each file: a second line for a key would hide the first.
    assert len(result_rows) == len(answer_rows) == len(spend_rows) == 48
    results = {(r["id"], r["loop"]): r for r in result_rows}
    answers = {(a["id"], a["loop"]): a["answer"] for a in answer_rows}
    spend = {(s["id"], s["loop"]): s["spend"] for s in spend_rows}
    assert sorted(answers) == sorted(spend) == sorted(results)
    for key, row in results.items():
        assert len(answers[key]) == row["answer_chars"]
        assert runner.check_answer(answers[key], accept[key[0]]) is row["accepted"]
        lines = re.findall(
            r"\[api\] spend: (\d+) request\(s\), (\d+) tool call\(s\), ([0-9.]+)s",
            "\n".join(spend[key]),
        )
        assert len(lines) == row["spend_lines"]
        requests, tool_calls, wall = lines[-1]
        assert (int(requests), int(tool_calls), float(wall)) == (
            row["requests"],
            row["tool_calls"],
            row["wall_seconds"],
        )


def _block(syntax: str) -> bool:
    return "F" in syntax or "L" in syntax


def _says(phrase: str, text: str) -> bool:
    """Whether *text* holds *phrase*, with no digit just before it: a count that opens the
    phrase is not matched inside a longer number."""
    return re.search(r"(?<!\d)" + re.escape(phrase), text) is not None


def test_the_adr_counts_of_rounds_requests_and_exits_are_the_runs_tallies():
    """The Result's counts of `lndl` rounds, of the runs that opened with an `<invoke>` tag and
    of the requests that went to rounds with no block are recomputed from rounds.jsonl, and the
    exits and request counts beside them from results.jsonl."""
    flat = " ".join(ADR.read_text().split())
    runs = ("r2", "r3", "r4")
    rounds = {
        run: {r["id"]: r["rounds"] for r in _jsonl(PILOT / "runs" / run / "rounds.jsonl")}
        for run in runs
    }
    results = {
        run: {(r["id"], r["loop"]): r for r in _jsonl(PILOT / "runs" / run / "results.jsonl")}
        for run in runs
    }
    for run in runs:
        assert sorted(rounds[run]) == sorted(i for i, loop in results[run] if loop == "lndl")
        for task, syntax in rounds[run].items():
            assert len(syntax) == results[run][(task, "lndl")]["requests"]

    opened = {run: [t for t, syntax in rounds[run].items() if syntax[0] == "I"] for run in runs}
    assert len(opened["r2"]) == len(opened["r3"])
    assert (
        f"in both runs {len(opened['r2'])} of the 24 `lndl` runs opened with a call written as an "
        "`<invoke>` tag outside any block"
    ) in flat
    r2 = rounds["r2"]
    by_fifth = [t for t in opened["r2"] if any(map(_block, r2[t][:5]))]
    late = sorted(t for t in opened["r2"] if not any(map(_block, r2[t][:9])))
    named = ", ".join(f"`{t}`" for t in late)
    assert (
        f"In r2, {len(by_fifth)} of those wrote a block by their fifth round, and {len(late)} "
        f"({named}) wrote none before their tenth"
    ) in flat
    first = {t: next(i for i, s in enumerate(r2[t], 1) if _block(s)) for t in late}
    assert first == {"t03": 10, "t04": 11, "t17": 11, "t18": 11, "t19": 11}
    assert {len(r2[t]) for t in late} == {11}
    assert results["r2"][("t03", "lndl")]["tool_calls"] == 1
    assert (
        "`t03` wrote one in its tenth round, which ran its one tool call, and the other four first "
        "wrote one in the eleventh and last round"
    ) in flat
    assert all(_block(rounds["r3"][t][1]) for t in opened["r3"])
    assert f"In r3 each of the {len(opened['r3'])} wrote a block in its next round" in flat
    assert all(_block(rounds["r4"][t][1]) for t in opened["r4"])
    assert _says(
        f"{len(opened['r4'])} of the 24 `lndl` runs again opened with an `<invoke>` tag, each wrote "
        "a block in its next round",
        flat,
    )
    for run, said in (
        ("r2", "{} of the {} `lndl` requests"),
        ("r3", "and {} of the {} requests"),
        ("r4", "{} of the {} requests"),
    ):
        no_block = sum(not _block(s) for syntax in rounds[run].values() for s in syntax)
        total = sum(len(syntax) for syntax in rounds[run].values())
        assert _says(said.format(no_block, total) + " went to rounds with no block", flat)
    t04 = results["r3"][("t04", "react")]
    assert (t04["requests"], t04["tool_calls"]) == (3, 0)
    assert "it made no tool call in its 3 requests" in flat

    rows = [row for run in RUNS for row in _jsonl(PILOT / "runs" / run / "results.jsonl")]
    assert len(rows) == 48 * len(RUNS)
    assert {row["rc"] for row in rows} == {0}
    assert f"All {len(rows)} legs exited 0" in flat
    spent_all = sorted(t for t, syntax in r2.items() if len(syntax) == 11)
    assert spent_all == late
    assert all("O" in r2[t][-1] for t in spent_all)
    assert (
        "The five r2 `lndl` legs that spent all 11 requests wrote an `OUT{}` in their last round"
    ) in flat
    r5_rounds = [r["rounds"] for r in _jsonl(PILOT / "runs" / "r5" / "rounds.jsonl")]
    assert max(len(syntax) for run in ("r3", "r4") for syntax in rounds[run].values()) <= 5
    assert max(len(syntax) for syntax in r5_rounds) <= 5
    assert "No `lndl` leg in r3, r4 or r5 spent more than 5" in flat


class _Legs:
    """Stands in for subprocess.run: the header's two probes answer, and each leg is counted."""

    def __init__(self, leg=None):
        self.legs: list[list[str]] = []
        self._leg = leg

    def __call__(self, argv, **kwargs):
        if argv[1:2] != ["agent"]:
            return subprocess.CompletedProcess(argv, 0, stdout="probe\n", stderr="")
        self.legs.append(argv)
        if self._leg is not None:
            self._leg(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="the answer is 42\n",
            stderr="[api] spend: 2 request(s), 1 tool call(s), 1.5s\n",
        )


def _run_main(monkeypatch, tmp_path, legs, ids, *flags, accept=None):
    tasks = [
        {
            "id": tid,
            "class": "lookup",
            "prompt": f"question {tid}",
            "accept": {"all_of": ["42"]} if accept is None else accept,
        }
        for tid in ids
    ]
    tasks_file = tmp_path / "tasks.yaml"
    tasks_file.write_text(json.dumps({"tasks": tasks}))
    out = tmp_path / "run"
    # The read tree and `li` are made on the machine that runs the pilot, not committed.
    (tmp_path / "li").touch(mode=0o755)
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    monkeypatch.setattr(runner.subprocess, "run", legs)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr(
        sys, "argv", ["run_pilot.py", "--tasks", str(tasks_file), "--out", str(out), *flags]
    )
    return runner.main(), out


def test_a_leg_that_raises_is_its_own_row_and_the_run_still_writes_its_summary(
    monkeypatch, tmp_path
):
    def leg(argv):
        if argv[-1].endswith("t01-lndl.prompt.txt"):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    legs = _Legs(leg)
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01", "t02"])

    assert rc == 1
    assert len(legs.legs) == 4
    rows = _jsonl(out / "results.jsonl")
    assert len(rows) == 4
    failed = next(r for r in rows if (r["id"], r["loop"]) == ("t01", "lndl"))
    assert failed["rc"] == "harness-error"
    assert failed["spend_lines"] == 0
    assert failed["accepted"] is False
    assert (out / "t01-lndl.stderr.txt").read_text().startswith("UnicodeDecodeError:")
    summary = json.loads((out / "summary.json").read_text())
    assert summary["d4_rule"] is None
    assert summary["d4_rule_unset"] == "1 leg(s) have no spend line"
    assert "partial" not in summary
    assert "| t01 |" in (out / "table.md").read_text()


def test_a_leg_that_times_out_inside_a_character_is_its_own_row(monkeypatch, tmp_path):
    def leg(argv):
        if argv[-1].endswith("t01-react.prompt.txt"):
            # Its output so far ends with the first two bytes of a three-byte character.
            raise subprocess.TimeoutExpired(argv, 420, output=b"partial \xe4\xb8", stderr=b"")

    legs = _Legs(leg)
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01"])

    assert rc == 0
    assert len(legs.legs) == 2
    rows = {(r["id"], r["loop"]): r for r in _jsonl(out / "results.jsonl")}
    assert rows[("t01", "react")]["rc"] == "harness-timeout"
    assert rows[("t01", "react")]["spend_lines"] == 0
    assert rows[("t01", "lndl")]["rc"] == 0
    assert (out / "t01-react.stdout.txt").read_text() == "partial �"


def test_a_leg_whose_check_raises_after_it_ran_is_its_own_row(monkeypatch, tmp_path):
    real_check = runner.check_answer
    calls = []

    def check_answer(answer, accept):
        calls.append(answer)
        if len(calls) == 1:
            raise ValueError("the check broke")
        return real_check(answer, accept)

    monkeypatch.setattr(runner, "check_answer", check_answer)
    legs = _Legs()
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01", "t02"], "--concurrency", "1")

    assert rc == 1
    # The first leg is t01's ReAct leg; its pair's second leg still runs.
    assert len(legs.legs) == 4
    rows = {(r["id"], r["loop"]): r for r in _jsonl(out / "results.jsonl")}
    assert len(rows) == 4
    assert rows[("t01", "react")]["rc"] == "harness-error"
    assert rows[("t01", "react")]["spend_lines"] == 0
    assert rows[("t01", "lndl")]["accepted"] is True
    assert (out / "t01-react.stdout.txt").read_text() == "the answer is 42\n"
    summary = json.loads((out / "summary.json").read_text())
    assert summary["d4_rule"] is None
    assert summary["d4_rule_unset"] == "1 leg(s) have no spend line"
    assert "| t01 |" in (out / "table.md").read_text()


@pytest.mark.parametrize(
    ("accept", "refused"),
    [
        ({"all_of": ["re:("]}, "`re:(` does not compile"),
        ({"all_of": ["42"], "none_of": ["re:4["]}, "`re:4[` does not compile"),
        # These two raise OverflowError and RecursionError rather than re.error.
        ({"all_of": ["re:4{4294967296}"]}, "`re:4{4294967296}` does not compile"),
        ({"all_of": ["re:" + "(" * 3000 + "4" + ")" * 3000]}, "does not compile"),
        # A pattern that matches an empty answer lets all_of accept one, and in none_of it
        # can refuse every answer (`.*` matches each); a blank one is a stray space.
        ({"all_of": ["re:"]}, "`accept.all_of` has a `re:` pattern that is blank or matches"),
        ({"all_of": ["re:(?:)"]}, "`accept.all_of` has a `re:` pattern that is blank or matches"),
        ({"all_of": ["re:x*"]}, "`accept.all_of` has a `re:` pattern that is blank or matches"),
        ({"all_of": ["re:^"]}, "`accept.all_of` has a `re:` pattern that is blank or matches"),
        ({"all_of": ["re: "]}, "`accept.all_of` has a `re:` pattern that is blank or matches"),
        (
            {"all_of": ["42"], "none_of": ["re:"]},
            "`accept.none_of` has a `re:` pattern that is blank or matches",
        ),
        (
            {"all_of": ["42"], "none_of": ["re:.*"]},
            "`accept.none_of` has a `re:` pattern that is blank or matches",
        ),
        # Each of these would accept an answer its check never looked at.
        ({}, "`accept.all_of` is empty"),
        ({"all_of": []}, "`accept.all_of` is empty"),
        ({"all_of": [""]}, "`accept.all_of` is not a list of non-empty strings"),
        ({"all_of": "42"}, "`accept.all_of` is not a list of non-empty strings"),
        (
            {"all_of": ["42"], "none_of": "24"},
            "`accept.none_of` is not a list of non-empty strings",
        ),
        (
            {"all_of": ["42"], "any_of": ["24"]},
            "`accept` is not a mapping of `all_of` and `none_of`",
        ),
        ({"all_of": ["re:4(2)"]}, None),
        ({"all_of": ["re:4+2"], "none_of": ["re:\\b24\\b"]}, None),
    ],
)
def test_a_check_that_would_fail_or_pass_unchecked_is_refused_before_anything_runs(
    monkeypatch, tmp_path, capsys, accept, refused
):
    legs = _Legs()
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01"], accept=accept)
    if refused:
        assert rc == 2
        assert legs.legs == []
        assert not out.exists()
        err = capsys.readouterr().err
        assert err.startswith("t01: ")
        assert refused in err
        return
    assert rc == 0
    assert [r["accepted"] for r in _jsonl(out / "results.jsonl")] == [True, True]


@pytest.mark.parametrize(
    ("edit", "refused"),
    [
        (lambda tasks: tasks[0].pop("class"), "t01: `class` is not a non-empty string"),
        (lambda tasks: tasks[0].update(prompt=""), "t01: `prompt` is not a non-empty string"),
        (
            lambda tasks: tasks[0].update(id="../t01"),
            "../t01: `id` is not 1 to 100 letters, digits, `_` and `-`",
        ),
        (lambda tasks: tasks[1].update(id="t01"), "t01: `id` repeats"),
        # Each leg's files are named after its id; on a volume that ignores letter case these
        # two would write the same files.
        (lambda tasks: tasks[1].update(id="T01"), "T01: `id` repeats, ignoring letter case"),
        (
            lambda tasks: tasks[0].update(id=["t01"]),
            "task 1: `id` is not 1 to 100 letters, digits, `_` and `-`",
        ),
        (lambda tasks: tasks.__setitem__(1, "t02"), "task 2: not a mapping"),
        # 100 characters is the runner's own limit for an id.
        (lambda tasks: tasks[0].update(id="t" * 101), "`id` is not 1 to 100 letters"),
        # These two would pass as strings and fail once the run directory had been made.
        (lambda tasks: tasks[0].update(prompt="q\ud800"), "t01: `prompt` is not valid UTF-8 text"),
        (
            lambda tasks: tasks[0].update({"class": "\ud800"}),
            "t01: `class` is not valid UTF-8 text",
        ),
        # A table row is one line.
        (
            lambda tasks: tasks[0].update({"class": "look\nup"}),
            "t01: `class` holds a line break",
        ),
        (
            lambda tasks: tasks[0].update({"class": "look\u2028up"}),
            "t01: `class` holds a line break",
        ),
        # An item that is not UTF-8 text never matches an answer read from a leg's output.
        (
            lambda tasks: tasks[0]["accept"].update(all_of=["\ud800"]),
            "t01: `accept.all_of` has an item that is not valid UTF-8",
        ),
        (
            lambda tasks: tasks[0]["accept"].update(none_of=["\ud800"]),
            "t01: `accept.none_of` has an item that is not valid UTF-8",
        ),
    ],
)
def test_a_malformed_task_is_refused_before_anything_runs(
    monkeypatch, tmp_path, capsys, edit, refused
):
    real_load = runner.yaml.load

    def load(data, Loader):
        loaded = real_load(data, Loader=Loader)
        edit(loaded["tasks"])
        return loaded

    monkeypatch.setattr(runner.yaml, "load", load)
    legs = _Legs()
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01", "t02"])

    assert rc == 2
    assert legs.legs == []
    assert not out.exists()
    assert refused in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flags", "refused"),
    [
        (["--loops", "react,foo"], "--loops: no profile d4-foo.md"),
        (["--concurrency", "0"], "--concurrency 0 is below 1"),
        (["--only", "t01,t99"], "--only: no task 't99'"),
        (["--only", "t01,"], "--only: no task ''"),
        (["--loops", "react,react"], "--loops: 'react,react' names a loop twice"),
        (["--loops", "react,REACT"], "--loops: 'react,REACT' names a loop twice"),
        # On a volume that ignores letter case d4-REACT.md opens d4-react.md.
        (["--loops", "REACT,lndl"], "--loops: no profile d4-REACT.md"),
        # A loop name heads a table column and names files.
        (["--loops", "re|act,lndl"], "--loops: 're|act' is not 1 to 100 letters"),
        (["--out", ""], "--out is empty"),
        ([], "is not executable"),
    ],
)
def test_an_unusable_argument_is_refused_before_anything_runs(
    monkeypatch, tmp_path, capsys, flags, refused
):
    real_main = runner.main

    def main():
        if not flags:
            runner.LI.chmod(0o644)
        return real_main()

    monkeypatch.setattr(runner, "main", main)
    legs = _Legs()
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01", "t02"], *flags)

    assert rc == 2
    assert legs.legs == []
    assert not out.exists()
    assert refused in capsys.readouterr().err


def test_a_leg_whose_output_does_not_decode_keeps_its_row(monkeypatch, tmp_path):
    real_run = subprocess.run
    script = tmp_path / "leg.sh"
    # Exits 0 with a byte that is not UTF-8 in its answer.
    script.write_text(
        "#!/bin/sh\n"
        "printf 'the answer is 42 \\377\\n'\n"
        "printf '[api] spend: 2 request(s), 1 tool call(s), 1.5s\\n' >&2\n"
    )
    script.chmod(0o755)
    legs = _Legs()

    def run(argv, **kwargs):
        # The header's probes are stubbed; each leg runs for real, as the script in place of
        # `li`, with the arguments the runner passes.
        if argv[1:2] != ["agent"]:
            return legs(argv, **kwargs)
        legs.legs.append(argv)
        return real_run([str(script)], **kwargs)  # noqa: S603

    rc, out = _run_main(monkeypatch, tmp_path, run, ["t01"])

    assert rc == 0
    assert len(legs.legs) == 2
    rows = _jsonl(out / "results.jsonl")
    assert [(r["rc"], r["accepted"], r["spend_lines"]) for r in rows] == [(0, True, 1)] * 2
    assert (out / "t01-react.stdout.txt").read_text() == "the answer is 42 \ufffd\n"


def test_a_row_that_cannot_be_written_is_not_counted(monkeypatch, tmp_path):
    def leg(argv):
        # results.jsonl cannot be opened for appending once a directory holds its name.
        (tmp_path / "run" / "results.jsonl").mkdir(exist_ok=True)

    legs = _Legs(leg)
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01", "t02"], "--concurrency", "1")

    assert rc == 1
    # Each pair stops after its first leg, whose row could not be written.
    assert len(legs.legs) == 2
    summary = json.loads((out / "summary.json").read_text())
    assert [summary[loop]["runs"] for loop in LOOPS] == [0, 0]
    assert summary["partial"] == "2 task(s) stopped on a harness error"
    table = (out / "table.md").read_text().splitlines()[2:]
    assert table == [
        "| t01 | lookup | not run | not run |",
        "| t02 | lookup | not run | not run |",
    ]


@pytest.mark.parametrize("missing", ["TREE", "LI"])
def test_a_missing_read_tree_or_li_is_refused_before_anything_runs(
    monkeypatch, tmp_path, capsys, missing
):
    real_main = runner.main

    def main():
        monkeypatch.setattr(runner, missing, tmp_path / "absent")
        return real_main()

    monkeypatch.setattr(runner, "main", main)
    legs = _Legs()
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01"])

    assert rc == 2
    assert legs.legs == []
    assert not out.exists()
    assert f"{tmp_path / 'absent'} not found; nothing run" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["directory", "file", "link"])
def test_an_out_that_already_exists_is_refused_by_mkdir(monkeypatch, tmp_path, capsys, kind):
    out = tmp_path / "run"
    if kind == "directory":
        out.mkdir()
    elif kind == "file":
        out.write_text("not a directory\n")
    else:
        out.symlink_to(tmp_path / "elsewhere")
    legs = _Legs()
    rc, _ = _run_main(monkeypatch, tmp_path, legs, ["t01"])

    assert rc == 2
    assert legs.legs == []
    assert f"--out: {out} cannot be created: {os.strerror(errno.EEXIST)}" in capsys.readouterr().err
    if kind == "directory":
        assert list(out.iterdir()) == []
    elif kind == "file":
        assert out.read_text() == "not a directory\n"
    else:
        assert out.is_symlink()
        assert not (tmp_path / "elsewhere").exists()


def test_an_li_that_is_not_a_file_is_refused_before_anything_runs(monkeypatch, tmp_path, capsys):
    real_main = runner.main

    def main():
        runner.LI.unlink()
        runner.LI.mkdir()
        return real_main()

    monkeypatch.setattr(runner, "main", main)
    legs = _Legs()
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01"])

    assert rc == 2
    assert legs.legs == []
    assert not out.exists()
    assert f"{runner.LI} is not executable" in capsys.readouterr().err


def test_only_beside_an_id_that_is_not_text_is_refused_before_anything_runs(
    monkeypatch, tmp_path, capsys
):
    real_load = runner.yaml.load

    def load(data, Loader):
        loaded = real_load(data, Loader=Loader)
        loaded["tasks"][0]["id"] = ["t01"]
        loaded["tasks"][1]["id"] = {"t02": 1}
        return loaded

    monkeypatch.setattr(runner.yaml, "load", load)
    legs = _Legs()
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01", "t02"], "--only", "t01")

    assert rc == 2
    assert legs.legs == []
    assert not out.exists()
    err = capsys.readouterr().err
    assert "task 1: `id` is not 1 to 100" in err
    assert "task 2: `id` is not 1 to 100" in err
    assert "--only: no task 't01'" in err


@pytest.mark.parametrize(
    ("content", "refused"),
    [
        ("- t01\n", "has no top-level `tasks`"),
        ("{}\n", "has no top-level `tasks`"),
        ("", "has no top-level `tasks`"),
        ("tasks: [t01\n", "is not YAML"),
        # safe_load would keep the second of a repeated key and run that alone.
        ("tasks: [t01]\ntasks: [t02]\n", "is not YAML: found the key 'tasks' twice"),
        ("tasks:\n- id: t01\n  id: t02\n", "is not YAML: found the key 'id' twice"),
        ("tasks: " + "[" * 3000 + "]" * 3000 + "\n", "cannot be loaded: RecursionError"),
        # Each of these is valid YAML whose value PyYAML fails to build.
        ("tasks: [2026-02-30]\n", "cannot be loaded: ValueError"),
        ("tasks: [2026-01-01T25:00:00Z]\n", "cannot be loaded: ValueError"),
        ("tasks: [!!int abc]\n", "cannot be loaded: ValueError"),
        ("tasks: [!!float abc]\n", "cannot be loaded: ValueError"),
        ("tasks: [!!bool abc]\n", "cannot be loaded: KeyError"),
        ('tasks: [!!int ""]\n', "cannot be loaded: IndexError"),
        ("tasks: [!!timestamp abc]\n", "cannot be loaded: AttributeError"),
        ('tasks:\n- ? !!int ""\n  : x\n', "cannot be loaded: IndexError"),
    ],
)
def test_a_task_file_of_another_shape_is_refused_before_anything_runs(
    monkeypatch, tmp_path, capsys, content, refused
):
    tasks_file = tmp_path / "tasks.yaml"
    tasks_file.write_text(content)
    out = tmp_path / "run"
    (tmp_path / "li").touch(mode=0o755)
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    legs = _Legs()
    monkeypatch.setattr(runner.subprocess, "run", legs)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr(
        sys, "argv", ["run_pilot.py", "--tasks", str(tasks_file), "--out", str(out)]
    )

    assert runner.main() == 2
    assert legs.legs == []
    assert not out.exists()
    err = capsys.readouterr().err
    assert f"{tasks_file} {refused}" in err
    assert "nothing run" in err


@pytest.mark.parametrize("kind", ["missing", "directory", "link loop"])
def test_a_task_file_that_cannot_be_read_is_refused_before_anything_runs(
    monkeypatch, tmp_path, capsys, kind
):
    tasks_file = tmp_path / "tasks.yaml"
    if kind == "directory":
        tasks_file.mkdir()
    elif kind == "link loop":
        tasks_file.symlink_to(tasks_file)
    out = tmp_path / "run"
    (tmp_path / "li").touch(mode=0o755)
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    legs = _Legs()
    monkeypatch.setattr(runner.subprocess, "run", legs)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr(
        sys, "argv", ["run_pilot.py", "--tasks", str(tasks_file), "--out", str(out)]
    )

    assert runner.main() == 2
    assert legs.legs == []
    assert not out.exists()
    err = capsys.readouterr().err
    assert f"--tasks: {tasks_file} cannot be read" in err
    assert "nothing run" in err


@pytest.mark.parametrize("key", [None, ""])
def test_a_run_without_an_api_key_is_refused_before_anything_runs(
    monkeypatch, tmp_path, capsys, key
):
    tasks_file = tmp_path / "tasks.yaml"
    shutil.copyfile(PILOT / "tasks.yaml", tasks_file)
    out = tmp_path / "run"
    (tmp_path / "li").touch(mode=0o755)
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    legs = _Legs()
    monkeypatch.setattr(runner.subprocess, "run", legs)
    if key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setattr(
        sys, "argv", ["run_pilot.py", "--tasks", str(tasks_file), "--out", str(out)]
    )

    assert runner.main() == 2
    assert legs.legs == []
    assert not out.exists()
    assert "ANTHROPIC_API_KEY is not set in this process; nothing run" in capsys.readouterr().err


def test_a_table_row_escapes_its_class_and_marks_a_missing_count(monkeypatch, tmp_path):
    real_load = runner.yaml.load

    def load(data, Loader):
        loaded = real_load(data, Loader=Loader)
        loaded["tasks"][0]["class"] = "look|up"
        return loaded

    def no_spend(argv, **kwargs):
        if argv[1:2] != ["agent"]:
            return subprocess.CompletedProcess(argv, 0, stdout="probe\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="the answer is 42\n", stderr="")

    monkeypatch.setattr(runner.yaml, "load", load)
    rc, out = _run_main(monkeypatch, tmp_path, no_spend, ["t01"])

    assert rc == 0
    assert (out / "table.md").read_text().splitlines()[2:] == [
        "| t01 | look\\|up | - / - / - / yes | - / - / - / yes |",
    ]


@pytest.mark.parametrize(
    "cls",
    [
        "look\u00a0up",
        "look\u3000up",
        "family \U0001f468\u200d\U0001f469\u200d\U0001f467",
        "soft\u00adhyphen",
    ],
)
def test_a_class_without_a_line_break_is_written_as_it_is(monkeypatch, tmp_path, cls):
    real_load = runner.yaml.load

    def load(data, Loader):
        loaded = real_load(data, Loader=Loader)
        loaded["tasks"][0]["class"] = cls
        return loaded

    monkeypatch.setattr(runner.yaml, "load", load)
    rc, out = _run_main(monkeypatch, tmp_path, _Legs(), ["t01"])

    assert rc == 0
    rows = (out / "table.md").read_text(encoding="utf-8").splitlines()[2:]
    assert rows == [f"| t01 | {cls} | 2 / 1 / 1.5 / yes | 2 / 1 / 1.5 / yes |"]


def test_two_legs_that_would_write_the_same_files_are_refused_before_anything_runs(
    monkeypatch, tmp_path, capsys
):
    # Task `x-a` under loop `b` and task `x` under loop `a-b` both name their files `x-a-b`.
    profiles = tmp_path / "pilot" / ".lionagi" / "agents"
    profiles.mkdir(parents=True)
    for loop in ("b", "a-b"):
        (profiles / f"d4-{loop}.md").write_text("profile\n")
    monkeypatch.setattr(runner, "HERE", tmp_path / "pilot")
    legs = _Legs()
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["x-a", "x"], "--loops", "b,a-b")

    assert rc == 2
    assert legs.legs == []
    assert not out.exists()
    assert "two legs would write the same files: x-a-b" in capsys.readouterr().err


def test_an_out_through_a_link_loop_is_refused_by_mkdir(monkeypatch, tmp_path, capsys):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    out = tmp_path / "loop" / "run"
    legs = _Legs()
    rc, _ = _run_main(monkeypatch, tmp_path, legs, ["t01"], "--out", str(out))

    assert rc == 2
    assert legs.legs == []
    assert f"--out: {out} cannot be created: {os.strerror(errno.ELOOP)}" in capsys.readouterr().err


def test_an_out_naming_a_dangling_link_with_a_trailing_slash_is_refused_by_mkdir(
    monkeypatch, tmp_path, capsys
):
    (tmp_path / "dang").symlink_to(tmp_path / "elsewhere")
    legs = _Legs()
    rc, _ = _run_main(monkeypatch, tmp_path, legs, ["t01"], "--out", f"{tmp_path / 'dang'}/")

    assert rc == 2
    assert legs.legs == []
    assert not (tmp_path / "elsewhere").exists()
    assert f"--out: {tmp_path / 'dang'}/ cannot be created: {os.strerror(errno.EEXIST)}" in (
        capsys.readouterr().err
    )


def test_an_out_under_a_file_is_refused_by_mkdir(monkeypatch, tmp_path, capsys):
    (tmp_path / "afile").write_text("")
    legs = _Legs()
    rc, _ = _run_main(
        monkeypatch, tmp_path, legs, ["t01"], "--out", str(tmp_path / "afile" / "run")
    )

    assert rc == 2
    assert legs.legs == []
    err = capsys.readouterr().err
    reason = os.strerror(errno.ENOTDIR)
    assert f"--out: {tmp_path / 'afile' / 'run'} cannot be created: {reason}" in err


def test_an_out_under_a_missing_directory_is_refused_by_mkdir_and_makes_nothing(
    monkeypatch, tmp_path, capsys
):
    out = tmp_path / "a" / "b" / "run"
    legs = _Legs()
    rc, _ = _run_main(monkeypatch, tmp_path, legs, ["t01"], "--out", str(out))

    assert rc == 2
    assert legs.legs == []
    assert not (tmp_path / "a").exists()
    assert f"--out: {out} cannot be created: {os.strerror(errno.ENOENT)}" in (
        capsys.readouterr().err
    )


def test_an_out_through_a_directory_that_cannot_be_searched_is_refused(
    monkeypatch, tmp_path, capsys
):
    closed = tmp_path / "P" / "U"
    closed.mkdir(parents=True)
    closed.chmod(0o600)
    out = closed / "run"
    try:
        if os.access(closed / ".", os.X_OK):
            pytest.skip("this user can search a directory without search permission")
        legs = _Legs()
        rc, _ = _run_main(monkeypatch, tmp_path, legs, ["t01"], "--out", str(out))
    finally:
        closed.chmod(0o700)

    assert rc == 2
    assert legs.legs == []
    assert not (closed / "run").exists()
    assert f"--out: {out} cannot be created: {os.strerror(errno.EACCES)}" in (
        capsys.readouterr().err
    )


def test_an_out_through_a_link_whose_target_goes_up_out_of_a_file_is_refused(
    monkeypatch, tmp_path, capsys
):
    (tmp_path / "P").mkdir()
    (tmp_path / "P" / "F").write_text("")
    (tmp_path / "P" / "D").mkdir()
    (tmp_path / "P" / "L").symlink_to("F/../D")
    out = tmp_path / "P" / "L" / "run"
    legs = _Legs()
    rc, _ = _run_main(monkeypatch, tmp_path, legs, ["t01"], "--out", str(out))

    assert rc == 2
    assert legs.legs == []
    assert not (tmp_path / "P" / "D" / "run").exists()
    assert f"--out: {out} cannot be created: {os.strerror(errno.ENOTDIR)}" in (
        capsys.readouterr().err
    )


def test_an_out_through_a_dangling_link_is_refused_by_mkdir(monkeypatch, tmp_path, capsys):
    (tmp_path / "dang").symlink_to(tmp_path / "elsewhere")
    out = tmp_path / "dang" / "run"
    legs = _Legs()
    rc, _ = _run_main(monkeypatch, tmp_path, legs, ["t01"], "--out", str(out))

    assert rc == 2
    assert legs.legs == []
    assert not (tmp_path / "elsewhere").exists()
    assert f"--out: {out} cannot be created: {os.strerror(errno.ENOENT)}" in (
        capsys.readouterr().err
    )


def test_a_relative_out_from_a_removed_current_directory_is_refused(monkeypatch, tmp_path, capsys):
    gone = tmp_path / "gone"
    gone.mkdir()
    tasks_file = tmp_path / "tasks.yaml"
    tasks_file.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "t01",
                        "class": "lookup",
                        "prompt": "question t01",
                        "accept": {"all_of": ["42"]},
                    }
                ]
            }
        )
    )
    (tmp_path / "li").touch(mode=0o755)
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    monkeypatch.setattr(runner.subprocess, "run", _Legs())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr(sys, "argv", ["run_pilot.py", "--tasks", str(tasks_file), "--out", "run"])
    os.chdir(gone)
    gone.rmdir()
    try:
        rc = runner.main()
    finally:
        os.chdir(tmp_path)

    assert rc == 2
    assert f"--out: run cannot be made absolute: {os.strerror(errno.ENOENT)}" in (
        capsys.readouterr().err
    )
    assert not (tmp_path / "run").exists()


def test_a_relative_tasks_file_from_a_removed_current_directory_is_refused(
    monkeypatch, tmp_path, capsys
):
    gone = tmp_path / "gone"
    gone.mkdir()
    (tmp_path / "li").touch(mode=0o755)
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    legs = _Legs()
    monkeypatch.setattr(runner.subprocess, "run", legs)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    out = tmp_path / "run"
    monkeypatch.setattr(sys, "argv", ["run_pilot.py", "--tasks", "tasks.yaml", "--out", str(out)])
    os.chdir(gone)
    gone.rmdir()
    try:
        rc = runner.main()
    finally:
        os.chdir(tmp_path)

    assert rc == 2
    assert legs.legs == []
    assert f"--tasks: tasks.yaml cannot be read: {os.strerror(errno.ENOENT)}" in (
        capsys.readouterr().err
    )
    assert not out.exists()


def test_a_relative_out_hands_every_leg_an_absolute_prompt_path_in_the_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    legs = _Legs()
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01"], "--out", "run")

    assert rc == 0
    assert len(legs.legs) == 2
    prompts = [Path(argv[argv.index("--prompt-file") + 1]) for argv in legs.legs]
    assert all(prompt.is_absolute() for prompt in prompts)
    assert all(prompt.parent == out for prompt in prompts)
    assert {prompt.name for prompt in prompts} == {
        "t01-react.prompt.txt",
        "t01-lndl.prompt.txt",
    }


def _path_max(path: Path) -> int:
    fallback = 4096 if sys.platform.startswith("linux") else 1024
    try:
        limit = os.pathconf(path, "PC_PATH_MAX")
    except (OSError, ValueError):
        return fallback
    return limit if limit > 0 else fallback


def _deep_directory(root: Path, target_length: int) -> Path:
    base = root
    while len(str(base)) + 41 <= target_length:
        base = base / ("d" * 40)
        base.mkdir()
    remaining = target_length - len(str(base))
    if remaining > 1:
        base = base / ("d" * (remaining - 1))
        base.mkdir()
    return base


@pytest.mark.parametrize(
    ("tid", "refused"), [("t" * 100, True), ("t01", False)], ids=["long-id", "short-id"]
)
def test_an_out_whose_leg_file_paths_would_be_too_long_is_refused(
    monkeypatch, tmp_path, capsys, tid, refused
):
    limit = _path_max(tmp_path)
    base = _deep_directory(tmp_path, limit - 70)
    out = base / "run"
    legs = _Legs()
    rc, _ = _run_main(monkeypatch, tmp_path, legs, [tid], "--out", str(out))

    if not refused:
        assert rc == 0
        assert len(legs.legs) == 2
        assert (out / "header.json").is_file()
        return
    assert rc == 2
    assert legs.legs == []
    assert not out.exists()
    err = capsys.readouterr().err
    assert f"--out: {out} is too long: a leg's file path would be" in err
    assert f"and this file system takes at most {limit - 1}" in err


@pytest.mark.parametrize(("short_by", "refused"), [(0, True), (1, False)], ids=["limit", "limit-1"])
def test_a_leg_file_path_of_the_limit_is_refused_and_one_byte_shorter_runs(
    monkeypatch, tmp_path, capsys, short_by, refused
):
    limit = _path_max(tmp_path)
    leg_file = "/t01-react.prompt.txt"
    out_length = limit - short_by - len(leg_file)
    base = _deep_directory(tmp_path, out_length - 12)
    out = base / ("r" * (out_length - len(str(base)) - 1))
    assert len(os.fsencode(str(out) + leg_file)) == limit - short_by
    legs = _Legs()
    rc, _ = _run_main(monkeypatch, tmp_path, legs, ["t01"], "--out", str(out))

    if not refused:
        assert rc == 0
        assert len(legs.legs) == 2
        assert Path(str(out) + leg_file).is_file()
        return
    assert rc == 2
    assert legs.legs == []
    assert not out.exists()
    assert f"a leg's file path would be {limit} bytes" in capsys.readouterr().err


@pytest.mark.parametrize("lookup", ["raises", "not positive"])
@pytest.mark.parametrize(("platform", "fallback"), [("linux", 4096), ("darwin", 1024)])
def test_the_path_limit_falls_back_to_the_platform_value_when_it_cannot_be_read(
    monkeypatch, tmp_path, platform, fallback, lookup
):
    def pathconf(path, name):
        if lookup == "raises":
            raise OSError(22, "Invalid argument")
        return -1

    monkeypatch.setattr(runner.os, "pathconf", pathconf)
    monkeypatch.setattr(runner.sys, "platform", platform)
    assert runner._path_max(tmp_path) == fallback


def test_an_out_under_a_link_to_a_directory_runs_there(monkeypatch, tmp_path):
    (tmp_path / "real" / "D").mkdir(parents=True)
    (tmp_path / "V").symlink_to(tmp_path / "real" / "D")
    legs = _Legs()
    rc, _ = _run_main(monkeypatch, tmp_path, legs, ["t01"], "--out", f"{tmp_path}/V/run")

    assert rc == 0
    assert len(legs.legs) == 2
    assert (tmp_path / "real" / "D" / "run" / "header.json").is_file()


def test_an_out_whose_name_the_file_system_refuses_leaves_no_directory_behind(
    monkeypatch, tmp_path, capsys
):
    out = tmp_path / ("n" * 300)
    legs = _Legs()
    rc, _ = _run_main(monkeypatch, tmp_path, legs, ["t01"], "--out", str(out))

    assert rc == 2
    assert legs.legs == []
    assert not os.path.lexists(out)
    assert f"--out: {out} cannot be created: {os.strerror(errno.ENAMETOOLONG)}" in (
        capsys.readouterr().err
    )


def test_an_out_through_a_directory_that_cannot_be_searched_is_refused_by_mkdir(
    monkeypatch, tmp_path, capsys
):
    closed = tmp_path / "closed"
    closed.mkdir()
    closed.chmod(0o600)
    try:
        if os.access(closed / ".", os.X_OK):
            pytest.skip("this user can search a directory without search permission")
        legs = _Legs()
        rc, _ = _run_main(monkeypatch, tmp_path, legs, ["t01"], "--out", str(closed / "run"))
    finally:
        closed.chmod(0o700)

    assert rc == 2
    assert legs.legs == []
    assert not (closed / "run").exists()
    assert f"--out: {closed / 'run'} cannot be created: {os.strerror(errno.EACCES)}" in (
        capsys.readouterr().err
    )


def test_an_li_that_cannot_start_leaves_no_run_directory(monkeypatch, tmp_path):
    legs = _Legs()

    def run(argv, **kwargs):
        if argv[1:] == ["--version"]:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        return legs(argv, **kwargs)

    real_main = runner.main

    def main():
        monkeypatch.setattr(runner.subprocess, "run", run)
        return real_main()

    monkeypatch.setattr(runner, "main", main)
    with pytest.raises(FileNotFoundError):
        _run_main(monkeypatch, tmp_path, legs, ["t01"])
    assert legs.legs == []
    assert not (tmp_path / "run").exists()


def test_a_task_file_that_cannot_be_read_leaves_no_run_directory(monkeypatch, tmp_path, capsys):
    out = tmp_path / "run"
    (tmp_path / "li").touch()
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    missing = tmp_path / "missing.yaml"
    monkeypatch.setattr(sys, "argv", ["run_pilot.py", "--tasks", str(missing), "--out", str(out)])

    assert runner.main() == 2
    assert f"--tasks: {missing} cannot be read: {os.strerror(errno.ENOENT)}" in (
        capsys.readouterr().err
    )
    assert not out.exists()


def test_a_task_file_that_is_a_symlink_loop_leaves_no_run_directory(monkeypatch, tmp_path, capsys):
    out = tmp_path / "run"
    (tmp_path / "li").touch()
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    loop = tmp_path / "loop.yaml"
    loop.symlink_to(loop)
    monkeypatch.setattr(sys, "argv", ["run_pilot.py", "--tasks", str(loop), "--out", str(out)])

    assert runner.main() == 2
    assert f"--tasks: {loop} cannot be read: " in capsys.readouterr().err
    assert not out.exists()


def test_a_run_over_part_of_the_task_set_leaves_the_rule_unset(monkeypatch, tmp_path):
    legs = _Legs()
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01", "t02"], "--only", "t01")

    assert rc == 0
    assert len(legs.legs) == 2
    summary = json.loads((out / "summary.json").read_text())
    assert summary["d4_rule"] is None
    assert summary["partial"] == "the run covers 1 of 2 tasks (--only)"
    assert summary["d4_rule_unset"] == "the run covers 1 of 2 tasks (--only)"


@pytest.mark.parametrize("inside", [True, False])
def test_the_header_names_a_task_file_in_the_pilot_by_its_path_there(monkeypatch, tmp_path, inside):
    tasks_file = PILOT / "tasks.yaml"
    if not inside:
        tasks_file = tmp_path / "tasks.yaml"
        shutil.copyfile(PILOT / "tasks.yaml", tasks_file)
    out = tmp_path / "run"
    (tmp_path / "li").touch(mode=0o755)
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    monkeypatch.setattr(runner.subprocess, "run", _Legs())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_pilot.py", "--tasks", str(tasks_file), "--out", str(out), "--only", "t01"],
    )
    assert runner.main() == 0
    header = json.loads((out / "header.json").read_text())
    assert header["tasks_file"] == ("tasks.yaml" if inside else str(tasks_file.resolve()))


def test_a_task_file_in_the_pilot_spelled_in_another_case_is_named_as_the_directory_lists_it(
    monkeypatch, tmp_path
):
    here = tmp_path.resolve() / "Pilot"
    agents = here / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    for loop in LOOPS:
        shutil.copyfile(PILOT / ".lionagi" / "agents" / f"d4-{loop}.md", agents / f"d4-{loop}.md")
    (here / "runs").mkdir()
    shutil.copyfile(PILOT / "tasks.yaml", here / "runs" / "Tasks.yaml")
    spelled = tmp_path / "PILOT" / "RUNS" / "TASKS.YAML"
    if not spelled.is_file():
        pytest.skip("this volume tells file names apart by case")
    out = tmp_path / "run"
    (tmp_path / "li").touch(mode=0o755)
    monkeypatch.setattr(runner, "HERE", here)
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    monkeypatch.setattr(runner.subprocess, "run", _Legs())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr(
        sys, "argv", ["run_pilot.py", "--tasks", str(spelled), "--out", str(out), "--only", "t01"]
    )
    assert runner.main() == 0
    assert json.loads((out / "header.json").read_text())["tasks_file"] == "runs/Tasks.yaml"


def _header_tasks_file(monkeypatch, tmp_path, name, links, spelled, listed_last):
    """Runs t01 with the task set at `runs/<name>` in a copy of the pilot and returns the header's
    `tasks_file`; `links` makes the other entries, and the listing puts `listed_last` last."""
    here = tmp_path.resolve() / "Pilot"
    agents = here / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    for loop in LOOPS:
        shutil.copyfile(PILOT / ".lionagi" / "agents" / f"d4-{loop}.md", agents / f"d4-{loop}.md")
    runs = here / "runs"
    runs.mkdir()
    shutil.copyfile(PILOT / "tasks.yaml", runs / name)
    links(runs)
    real_listdir = os.listdir
    monkeypatch.setattr(
        runner.os,
        "listdir",
        lambda path: sorted(real_listdir(path), key=lambda entry: entry == listed_last),
    )
    out = tmp_path / "run"
    (tmp_path / "li").touch(mode=0o755)
    monkeypatch.setattr(runner, "HERE", here)
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    monkeypatch.setattr(runner.subprocess, "run", _Legs())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_pilot.py", "--tasks", str(runs / spelled), "--out", str(out), "--only", "t01"],
    )
    assert runner.main() == 0
    return json.loads((out / "header.json").read_text())["tasks_file"]


@pytest.mark.parametrize("spelled", ["Tasks.yaml", "TASKS.YAML"])
def test_a_task_file_is_named_by_its_own_entry_and_not_by_a_link_beside_it(
    monkeypatch, tmp_path, spelled
):
    def links(runs):
        if not (runs / spelled).is_file():
            pytest.skip("this volume tells file names apart by case")
        (runs / "dangling.yaml").symlink_to(runs / "missing.yaml")
        (runs / "loop.yaml").symlink_to(runs / "loop.yaml")
        (runs / "alias.yaml").symlink_to(runs / "Tasks.yaml")
        os.link(runs / "Tasks.yaml", runs / "hard.yaml")

    # Each of the other entries is listed before the task file's own.
    name = _header_tasks_file(monkeypatch, tmp_path, "Tasks.yaml", links, spelled, "Tasks.yaml")
    assert name == "runs/Tasks.yaml"


def test_a_task_file_with_a_hard_link_named_in_another_case_is_named_as_given(
    monkeypatch, tmp_path
):
    def links(runs):
        if (runs / "TASKS.yaml").exists():
            pytest.skip("this volume does not tell file names apart by case")
        os.link(runs / "tasks.yaml", runs / "TASKS.yaml")

    name = _header_tasks_file(
        monkeypatch, tmp_path, "tasks.yaml", links, "tasks.yaml", "tasks.yaml"
    )
    assert name == "runs/tasks.yaml"


def test_a_task_file_in_a_directory_that_cannot_be_listed_keeps_the_name_given(
    monkeypatch, tmp_path
):
    def links(runs):
        runs.chmod(0o311)
        if os.access(runs, os.R_OK):
            runs.chmod(0o755)
            pytest.skip("this process can list a directory without read permission")

    try:
        name = _header_tasks_file(
            monkeypatch, tmp_path, "Tasks.yaml", links, "Tasks.yaml", "Tasks.yaml"
        )
    finally:
        (tmp_path / "Pilot" / "runs").chmod(0o755)
    assert name == "runs/Tasks.yaml"


def test_an_interrupt_starts_no_further_leg_and_writes_a_partial_summary(monkeypatch, tmp_path):
    started, release = threading.Event(), threading.Event()

    def leg(argv):
        started.set()
        assert release.wait(10)

    real_wait = runner.cf.wait
    waits = []

    def wait(futures, *args, **kwargs):
        # The first wait is interrupted while the first leg runs; the second lets it finish.
        waits.append(len(futures))
        if len(waits) == 1:
            assert started.wait(10)
            raise KeyboardInterrupt
        release.set()
        return real_wait(futures, *args, **kwargs)

    monkeypatch.setattr(runner.cf, "wait", wait)
    legs = _Legs(leg)
    rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01", "t02", "t03"], "--concurrency", "1")

    assert rc == 130
    # The running leg ends and is kept; its pair's second leg and the queued pairs never start.
    assert len(legs.legs) == 1
    rows = _jsonl(out / "results.jsonl")
    assert [(r["id"], r["loop"], r["rc"]) for r in rows] == [("t01", "react", 0)]
    summary = json.loads((out / "summary.json").read_text())
    assert summary["partial"] == "interrupted after 1 of 6 legs"
    assert summary["d4_rule"] is None
    # Every task the run covers has a line, the ones never started included.
    assert (out / "table.md").read_text().splitlines()[2:] == [
        "| t01 | lookup | 2 / 1 / 1.5 / yes | not run |",
        "| t02 | lookup | not run | not run |",
        "| t03 | lookup | not run | not run |",
    ]


def test_a_second_interrupt_writes_the_summary_and_a_running_legs_row_follows(
    monkeypatch, tmp_path
):
    started, release = threading.Event(), threading.Event()

    def leg(argv):
        started.set()
        assert release.wait(10)

    def wait(futures, *args, **kwargs):
        # Every wait is interrupted while the first leg runs, the one after the second interrupt
        # too, so the summary stays as the second interrupt wrote it.
        assert started.wait(10)
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.cf, "wait", wait)
    before = set(threading.enumerate())
    legs = _Legs(leg)

    try:
        rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01", "t02"], "--concurrency", "1")
        assert rc == 130
        summary = json.loads((out / "summary.json").read_text())
        assert summary["partial"] == (
            "interrupted twice after 0 of 4 legs; "
            "rows of legs still running may follow in results.jsonl"
        )
        assert not (out / "results.jsonl").exists()
    finally:
        release.set()
        for thread in set(threading.enumerate()) - before:
            thread.join(10)
    rows = _jsonl(out / "results.jsonl")
    assert [(r["id"], r["loop"], r["rc"]) for r in rows] == [("t01", "react", 0)]
    assert len(legs.legs) == 1


def test_after_a_second_interrupt_the_summary_and_table_are_written_again_once_the_legs_end(
    monkeypatch, tmp_path
):
    started, release = threading.Event(), threading.Event()

    def leg(argv):
        started.set()
        assert release.wait(10)

    real_wait = runner.cf.wait
    waits, written = [], []

    def wait(futures, *args, **kwargs):
        # The first two waits are interrupted while the first leg runs; the third reads the
        # summary and table the second interrupt wrote, then lets the leg end.
        assert started.wait(10)
        waits.append(len(futures))
        if len(waits) < 3:
            raise KeyboardInterrupt
        run = tmp_path / "run"
        written.append(
            (json.loads((run / "summary.json").read_text()), (run / "table.md").read_text())
        )
        release.set()
        return real_wait(futures, *args, **kwargs)

    monkeypatch.setattr(runner.cf, "wait", wait)
    legs = _Legs(leg)
    try:
        rc, out = _run_main(monkeypatch, tmp_path, legs, ["t01", "t02"], "--concurrency", "1")
    finally:
        release.set()

    assert rc == 130
    assert len(waits) == 3
    first_summary, first_table = written[0]
    assert first_summary["partial"] == (
        "interrupted twice after 0 of 4 legs; "
        "rows of legs still running may follow in results.jsonl"
    )
    assert first_table.splitlines()[2:] == [
        "| t01 | lookup | not run | not run |",
        "| t02 | lookup | not run | not run |",
    ]
    rows = _jsonl(out / "results.jsonl")
    assert [(r["id"], r["loop"], r["rc"]) for r in rows] == [("t01", "react", 0)]
    summary = json.loads((out / "summary.json").read_text())
    assert summary["partial"] == "interrupted twice after 1 of 4 legs"
    assert summary["d4_rule"] is None
    assert (out / "table.md").read_text().splitlines()[2:] == [
        "| t01 | lookup | 2 / 1 / 1.5 / yes | not run |",
        "| t02 | lookup | not run | not run |",
    ]


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        ("home/state.db", True),
        ("tree/README.md", True),
        ("venv/bin/li", True),
        ("runs/r9/t01-react.prompt.txt", True),
        ("runs/r9/t01-lndl.stdout.txt", True),
        ("runs/r9/t01-lndl.stderr.txt", True),
        ("runs/r9/x/t01-react.prompt.txt", True),
        ("runs/r9/x/t01-lndl.stdout.txt", True),
        ("runs/r9/x/t01-lndl.stderr.txt", True),
        ("r9/t01-react.prompt.txt", True),
        ("r9/t01-lndl.stdout.txt", True),
        ("r9/t01-lndl.stderr.txt", True),
        ("notes.stdout.txt/t01-react.stdout.txt", True),
        ("home", True),
        ("tree", True),
        ("venv", True),
        ("runs/r2/results.jsonl", False),
        ("runs/r2/header.json", False),
        ("runs/r2/answers.jsonl", False),
        ("runs/r2/table.md", False),
        ("runs/r2/summary.json", False),
        ("runs/r2/environment.json", False),
        ("runs/r2/spend.jsonl", False),
        ("runs/r2/rounds.jsonl", False),
        ("runs/r2/reading.jsonl", False),
        ("notes.stdout.txt/results.jsonl", False),
        ("runs/r9.prompt.txt/results.jsonl", False),
        ("runs/r9.stderr.txt/results.jsonl", False),
        ("runs/r9.stdout.txt/results.jsonl", False),
        ("runs/home/results.jsonl", False),
        ("runs/tree/results.jsonl", False),
        ("runs/venv/results.jsonl", False),
        ("homeX/results.jsonl", False),
        ("tr1/results.jsonl", False),
        ("v1/results.jsonl", False),
        ("runs/r9/t01-react.prompt.txt.bak", False),
        ("runs/r9/x.stdout.txt.bak", False),
        ("runs/r9/x.stderr.txt.bak", False),
        ("runs/run_pilot.as-run.py.txt", False),
        ("tasks.yaml", False),
    ],
)
def test_what_a_run_writes_beside_its_results_is_left_out_of_a_git_add(tmp_path, path, ignored):
    """The runner writes each leg's prompt, stdout and stderr into its run directory, wherever
    `--out` puts it under the runner's directory, and the README puts the read tree, the
    environment and every leg's LIONAGI_HOME beside the runner.
    Read with only this directory's ignore file, in a repository of its own, those are ignored
    and the files a run commits are not, whatever the run directory is named; a run directory
    inside `home`, `tree` or `venv` is ignored whole."""
    repo = tmp_path / "repo"
    pilot = repo / "benchmarks" / "lndl_pilot"
    pilot.mkdir(parents=True)
    (pilot / ".gitignore").write_bytes((PILOT / ".gitignore").read_bytes())
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {
        # A git commit hook runs with GIT_INDEX_FILE set, and GIT_DIR too in a linked worktree;
        # GIT_DIR would point these commands at the hook's repository, so the caller's GIT_
        # variables are dropped.
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        # git's default excludes file is $XDG_CONFIG_HOME/git/ignore, else $HOME/.config/git/ignore
        "XDG_CONFIG_HOME": str(empty),
        "HOME": str(empty),
    }
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    assert (repo / ".git").is_dir()
    probe = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "check-ignore",
            "--no-index",
            "-q",
            f"benchmarks/lndl_pilot/{path}",
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert probe.returncode in (0, 1), probe.stderr
    assert (probe.returncode == 0) is ignored


def test_the_ignore_file_holds_the_patterns_the_rows_above_were_chosen_for():
    """git reads every line of an ignore file as a pattern but comments, empty lines and lines of
    spaces alone (a tab is a pattern), split at LF with a CR before it dropped, and a loosened
    pattern can leave every row above green while it ignores a committed file under a name no row
    uses (`/tree*`, a `tree2` run directory). A change to a pattern line other than a CR before its
    LF fails here until the list below changes with it; rows above should show what it ignores."""
    lines = [x.removesuffix("\r") for x in (PILOT / ".gitignore").read_bytes().decode().split("\n")]
    patterns = [line for line in lines if line.strip(" ") and not line.startswith("#")]

    assert patterns == [
        "/home",
        "/tree",
        "/venv",
        "*.prompt.txt",
        "*.stdout.txt",
        "*.stderr.txt",
        "!*.prompt.txt/",
        "!*.stdout.txt/",
        "!*.stderr.txt/",
    ]
