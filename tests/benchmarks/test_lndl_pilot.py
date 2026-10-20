"""The ADR-0132 D4 pilot runner's summary: the D4 rule and when it is left unset."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import threading
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


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


ADR = PILOT.parents[1] / "docs" / "adr" / "ADR-0132-batched-lndl-rounds-for-api-model-legs.md"
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


def test_the_reading_and_the_checks_differ_in_the_ten_answers_the_adr_names():
    differ = sorted(
        (run, r["id"], r["loop"])
        for run in ("r2", "r3", "r4")
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
    ]


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
        assert f"t01: {refused}" in capsys.readouterr().err
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
            "../t01: `id` is not letters, digits, `_` and `-`",
        ),
        (lambda tasks: tasks[1].update(id="t01"), "t01: `id` repeats"),
        (lambda tasks: tasks.__setitem__(1, "t02"), "task 2: not a mapping"),
    ],
)
def test_a_malformed_task_is_refused_before_anything_runs(
    monkeypatch, tmp_path, capsys, edit, refused
):
    real_load = runner.yaml.safe_load

    def safe_load(data):
        loaded = real_load(data)
        edit(loaded["tasks"])
        return loaded

    monkeypatch.setattr(runner.yaml, "safe_load", safe_load)
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


def test_a_task_file_that_cannot_be_read_leaves_no_run_directory(monkeypatch, tmp_path):
    out = tmp_path / "run"
    (tmp_path / "li").touch()
    monkeypatch.setattr(runner, "TREE", tmp_path)
    monkeypatch.setattr(runner, "LI", tmp_path / "li")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_pilot.py", "--tasks", str(tmp_path / "missing.yaml"), "--out", str(out)],
    )
    with pytest.raises(FileNotFoundError):
        runner.main()
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
        # Both waits are interrupted while the first leg runs.
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
