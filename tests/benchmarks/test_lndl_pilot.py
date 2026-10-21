"""The ADR-0132 D4 pilot runner's summary: the D4 rule and when it is left unset."""

from __future__ import annotations

import errno
import importlib.util
import json
import os
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


@pytest.mark.parametrize("run", ["r2", "r3", "r4"])
def test_each_answer_is_the_text_its_row_measured_and_checked(run):
    """answers.jsonl holds each leg's answer as the check read it: its length is the row's
    `answer_chars`, the check re-run on it gives the row's `accepted`, and the last spend line
    gives the row's requests, tool calls and wall time."""
    loaded = runner.yaml.load((PILOT / "tasks.yaml").read_bytes(), Loader=runner._TaskLoader)
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

    rows = [row for run in runs for row in results[run].values()]
    assert len(rows) == 144
    assert {row["rc"] for row in rows} == {0}
    assert "All 144 legs exited 0" in flat
    spent_all = sorted(t for t, syntax in r2.items() if len(syntax) == 11)
    assert spent_all == late
    assert all("O" in r2[t][-1] for t in spent_all)
    assert (
        "The five r2 `lndl` legs that spent all 11 requests wrote an `OUT{}` in their last round"
    ) in flat
    assert max(len(syntax) for run in ("r3", "r4") for syntax in rounds[run].values()) <= 5
    assert "No `lndl` leg in r3 or r4 spent more than 5" in flat


def test_the_documents_record_three_runs_and_name_the_fourth_as_unrecorded():
    for document in (ADR, PILOT / "README.md"):
        text = document.read_text()
        assert "each run is recorded in the Result" not in text
        assert re.search(r"r2[^\n]{0,8}(?:-|,\s*)r3[^\n]{0,8}r4", text)
        flat = " ".join(text.split())
        assert re.search(
            r"A fourth run, `r5`, ran before the [^.]*? was adopted[^.]*\. "
            r"It is not recorded here and decides nothing until",
            flat,
        )


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
        # either would point these commands at the hook's repository.
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
