# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coding engine unit tests — the gated implement/test/fix loop.

These exercise the engine's own logic with direct event emission and a REAL
subprocess test runner (the ground-truth stage is the point). The fix loop is
driven with a ``test_cmd`` that flips from failing to passing via a tmp_path
flag file — deterministic, no time/random dependence. The scripted-provider
e2e (the live emission path) lives in test_engines_scripted_e2e.py.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from lionagi.engines.coding import (
    ChangeProposed,
    CodeResultRecorded,
    CodingChainEvent,
    CodingEngine,
    TestsRan,
    VerifyResult,
    WorkPlanned,
    _normalize_spec,
    _resolve_cmd,
    _tail,
)
from lionagi.engines.hypothesis import ResultRecorded


class _ScriptedBranch:
    """A fake agent whose ``operate`` emits a queued event each call — stands in
    for plan/implement/verify agents so the engine's own control flow (and the
    real test runner) is what's under test."""

    def __init__(self, run, events: list, *, name: str = "agent"):
        self._run = run
        self._events = list(events)
        self.name = name
        self.calls: list[str] = []

    async def operate(self, *, instruction):
        self.calls.append(instruction)
        if self._events:
            await self._run.emit(self._events.pop(0))
        return "ok"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_resolve_cmd_list_runs_shell_false():
    assert _resolve_cmd(["uv", "run", "pytest"]) == (["uv", "run", "pytest"], False)


def test_resolve_cmd_plain_string_is_split_shell_false():
    assert _resolve_cmd("pytest -q tests/") == (["pytest", "-q", "tests/"], False)


def test_resolve_cmd_shell_control_runs_shell_true():
    cmd, shell = _resolve_cmd("pytest && echo ok")
    assert cmd == "pytest && echo ok" and shell is True


def test_normalize_spec_string_is_task_with_no_experiment_ref():
    assert _normalize_spec("add a parser") == ("add a parser", "")


def test_normalize_spec_dict_renders_experiment_and_carries_eid():
    text, ref = _normalize_spec(
        {"eid": "X-3", "procedure": "count ops", "acceptance": "CSR < CTE/10", "method": "analysis"}
    )
    assert ref == "X-3"
    assert "count ops" in text and "CSR < CTE/10" in text and "analysis" in text


def test_normalize_spec_rejects_empty():
    with pytest.raises(ValueError):
        _normalize_spec("   ")
    with pytest.raises(ValueError):
        _normalize_spec({})
    with pytest.raises(TypeError):
        _normalize_spec(42)


def test_tail_bounds_lines_and_chars():
    text = "\n".join(str(i) for i in range(200))
    tail = _tail(text, lines=5, max_chars=1000)
    assert tail.splitlines() == ["195", "196", "197", "198", "199"]
    big = "x" * 9000
    assert len(_tail(big, lines=5, max_chars=4000)) <= 4001  # leading ellipsis


# ---------------------------------------------------------------------------
# Ground-truth subprocess runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_stage_passes_on_zero_exit(tmp_path):
    """A real ``python -c`` that exits 0 produces passed=True; the full output
    lands in the export dir, the event carries only a tail."""
    eng = CodingEngine()
    run = eng.new_run()
    run.workspace = str(tmp_path)
    run.export_dir = tmp_path / "out"
    run.export_dir.mkdir()
    run.test_cmd = [sys.executable, "-c", "print('hello-stdout'); exit(0)"]
    run.observe(CodingChainEvent, lambda e, _c: run.collect(e))
    change = run.collect(ChangeProposed(summary="noop"))

    tests = await eng._test(run, change, round_no=0)
    assert tests.passed is True
    assert tests.returncode == 0
    assert "hello-stdout" in tests.output_tail
    assert tests.output_file and (tmp_path / "out" / "test_output_1.txt").exists()
    assert "hello-stdout" in (tmp_path / "out" / "test_output_1.txt").read_text()


@pytest.mark.asyncio
async def test_test_stage_fails_on_nonzero_exit(tmp_path):
    eng = CodingEngine()
    run = eng.new_run()
    run.workspace = str(tmp_path)
    run.test_cmd = [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
    run.observe(CodingChainEvent, lambda e, _c: run.collect(e))
    change = run.collect(ChangeProposed(summary="bad"))

    tests = await eng._test(run, change, round_no=0)
    assert tests.passed is False
    assert tests.returncode == 3
    assert "boom" in tests.output_tail


@pytest.mark.asyncio
async def test_test_stage_times_out(tmp_path):
    """A command exceeding the timeout is killed and reported timed_out — passed
    is False regardless of what the process would have returned."""
    eng = CodingEngine(test_timeout_s=0.5)
    run = eng.new_run()
    run.workspace = str(tmp_path)
    run.test_cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    run.observe(CodingChainEvent, lambda e, _c: run.collect(e))
    change = run.collect(ChangeProposed(summary="hang"))

    tests = await eng._test(run, change, round_no=0)
    assert tests.passed is False
    assert tests.timed_out is True


# ---------------------------------------------------------------------------
# Full pipeline — plan -> implement -> test (pass path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_pass_path(tmp_path, monkeypatch):
    """plan -> implement -> test(pass) -> verify -> conclude(passed=True) with a
    trivial passing test command and fake plan/implement/verify agents."""
    eng = CodingEngine(max_fix_rounds=3)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="add foo", acceptance_criteria=["foo exists"])
    change_ev = ChangeProposed(summary="added foo", files_touched=["foo.py"], plan_ref="W-1")
    verdict_ev = VerifyResult(
        verdict="APPROVE", rationale="meets criteria", meets_acceptance=True, tests_ref="T-1"
    )

    # Each named stage gets its scripted branch; make_agent is the only seam.
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _ScriptedBranch(run, [change_ev], name="implement"),
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async("diff --git a/foo.py b/foo.py"))

    result = await eng._run(
        run,
        "add a foo function",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
        export_dir=tmp_path / "out",
    )
    assert isinstance(result, CodeResultRecorded)
    assert result.passed is True
    assert run.last(TestsRan).passed is True
    assert run.last(VerifyResult).meets_acceptance is True
    # chain refs wired: change.plan_ref -> plan, tests.change_ref -> change
    assert run.last(ChangeProposed).plan_ref == "W-1"
    assert run.last(TestsRan).change_ref == "P-1"
    # export written
    data = json.loads((tmp_path / "out" / "results.json").read_text())
    assert data["passed"] is True
    assert (tmp_path / "out" / "report.md").exists()


def _async(value):
    async def _coro():
        return value

    return _coro()


# ---------------------------------------------------------------------------
# Fix loop — fail then pass on a flipping test command
# ---------------------------------------------------------------------------


def _flip_test_cmd(flag_path) -> list[str]:
    """python -c that exits 1 the first time (creating the flag), 0 thereafter.

    The flip is keyed on the flag file existing BEFORE this invocation —
    deterministic across re-runs, no Date/random."""
    code = (
        "import os,sys;"
        f"p=r'{flag_path}';"
        "existed=os.path.exists(p);"
        "open(p,'a').close();"
        "sys.exit(0 if existed else 1)"
    )
    return [sys.executable, "-c", code]


@pytest.mark.asyncio
async def test_fix_loop_recovers_on_second_test(tmp_path, monkeypatch):
    """First test run fails; the implementer is re-prompted, emits a new change,
    the second test run passes — passed=True, exactly one fix round."""
    eng = CodingEngine(max_fix_rounds=3, repair_retries=0)
    run = eng.new_run()
    flag = tmp_path / "flip.flag"

    plan_ev = WorkPlanned(approach="fix it", acceptance_criteria=["passes"])
    # implementer emits the first change, then a second change on the fix prompt
    impl = _ScriptedBranch(
        run,
        [
            ChangeProposed(summary="attempt 1", plan_ref="W-1"),
            ChangeProposed(summary="attempt 2 (fixed)", plan_ref="W-1"),
        ],
        name="implement",
    )
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": impl,
        "verify": _ScriptedBranch(
            run,
            [VerifyResult(verdict="APPROVE", rationale="green", meets_acceptance=True)],
            name="verify",
        ),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    result = await eng._run(
        run,
        "make the test pass",
        test_cmd=_flip_test_cmd(flag),
        workspace=str(tmp_path),
    )
    assert result.passed is True
    test_runs = run.events_of(TestsRan)
    assert [t.passed for t in test_runs] == [False, True]
    assert [t.round for t in test_runs] == [0, 1]
    # implementer was re-prompted once with the failure output
    assert any("fix round 1" in c for c in impl.calls)
    assert result.measurements["fix_rounds"] == 1


@pytest.mark.asyncio
async def test_fix_loop_exhausts_and_concludes_failed(tmp_path, monkeypatch):
    """A test that never passes exhausts max_fix_rounds and concludes with
    passed=False — graceful, with the failure recorded as a caveat."""
    eng = CodingEngine(max_fix_rounds=2, repair_retries=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="impossible", acceptance_criteria=["passes"])
    # implementer always emits a *new* change (so the loop is bounded by rounds,
    # not by no-change detection)
    impl = _ScriptedBranch(
        run,
        [ChangeProposed(summary=f"attempt {i}", plan_ref="W-1") for i in range(1, 5)],
        name="implement",
    )
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": impl,
        "verify": _ScriptedBranch(
            run,
            [
                VerifyResult(
                    verdict="REQUEST-CHANGES",
                    rationale="still red",
                    meets_acceptance=False,
                    unmet=["passes"],
                )
            ],
            name="verify",
        ),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run,
        "never passes",
        test_cmd=[sys.executable, "-c", "exit(1)"],
        workspace=str(tmp_path),
    )
    assert result.passed is False
    # first pass + 2 fix rounds = 3 test runs
    assert len(run.events_of(TestsRan)) == 3
    assert result.measurements["fix_rounds"] == 2
    assert any(e["type"] == "fix_exhausted" for e in events)
    assert any("unmet" in c for c in result.caveats)


@pytest.mark.asyncio
async def test_fix_loop_stops_when_implementer_repeats_change(tmp_path, monkeypatch):
    """If a fix round produces no *new* change (same event), the loop stops
    early rather than spinning to max_fix_rounds."""
    eng = CodingEngine(max_fix_rounds=5, repair_retries=0)
    run = eng.new_run()
    plan_ev = WorkPlanned(approach="stuck", acceptance_criteria=["passes"])
    # implementer emits ONE change, then emits nothing on the fix prompt
    impl = _ScriptedBranch(
        run, [ChangeProposed(summary="only attempt", plan_ref="W-1")], name="implement"
    )
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": impl,
        "verify": _ScriptedBranch(
            run, [VerifyResult(verdict="REJECT", rationale="x")], name="verify"
        ),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))
    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run, "stuck", test_cmd=[sys.executable, "-c", "exit(1)"], workspace=str(tmp_path)
    )
    assert result.passed is False
    # one initial test; the single fix round emitted no new change -> stop
    assert len(run.events_of(TestsRan)) == 1
    assert any(e["type"] == "fix_no_change" for e in events)


# ---------------------------------------------------------------------------
# Implementer emits nothing -> graceful conclude
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_change_proposed_concludes_failed(tmp_path, monkeypatch):
    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()
    plan_ev = WorkPlanned(approach="x")
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _ScriptedBranch(run, [], name="implement"),  # emits nothing
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)

    result = await eng._run(
        run, "do nothing", test_cmd=[sys.executable, "-c", "exit(0)"], workspace=str(tmp_path)
    )
    assert result.passed is False
    assert any("emitted no change" in c for c in result.caveats)
    # no test ran (nothing to test)
    assert run.events_of(TestsRan) == []


# ---------------------------------------------------------------------------
# Regression: #1364 — workspace is ground truth when emission fails
# ---------------------------------------------------------------------------


def _make_git_workspace(path) -> None:
    """Initialise a bare git repo so ``git status --porcelain`` works."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    # Commit a placeholder so HEAD exists and status works cleanly.
    readme = path / "README"
    readme.write_text("placeholder\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init", "--no-gpg-sign"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_emission_failure_with_workspace_changes_runs_test_gate(tmp_path, monkeypatch):
    """Regression for #1364: when the implementer emits no ChangeProposed but
    the workspace shows file changes, the engine MUST run the test gate and
    reflect its outcome — not record a no-change failure.

    Setup: a git workspace with a new file written by the ``implement`` agent
    (simulated by a side-effect in the branch's operate, standing in for the
    worker writing real files).  The test command verifies the file exists, so
    ground truth governs passed/failed, not the emission."""
    _make_git_workspace(tmp_path)

    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="add module.py")
    target_file = tmp_path / "module.py"

    class _WritingBranch:
        """Emits nothing but DOES write a file to the workspace — the production
        failure mode: real work, prose (non-structured) final response."""

        name = "implement"
        calls: list[str] = []

        async def operate(self, *, instruction):
            self.calls.append(instruction)
            target_file.write_text("def answer(): return 42\n", encoding="utf-8")
            return "I wrote module.py."  # prose, no structured emission

    impl = _WritingBranch()
    verdict_ev = VerifyResult(
        verdict="APPROVE", rationale="file present and gate passed", meets_acceptance=True
    )
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": impl,
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    events: list[dict] = []
    run.on_event = events.append

    # Test command: exits 0 iff the file exists.
    test_cmd = [
        sys.executable,
        "-c",
        f"import sys; sys.exit(0 if __import__('os').path.exists(r'{target_file}') else 1)",
    ]

    result = await eng._run(run, "add module.py", test_cmd=test_cmd, workspace=str(tmp_path))

    # The test gate ran and the file was there → passed=True.
    assert result.passed is True, f"expected passed=True; caveats={result.caveats}"
    # At least one TestsRan event — the test stage MUST have executed.
    assert len(run.events_of(TestsRan)) >= 1, "test stage never ran"
    assert run.events_of(TestsRan)[0].passed is True
    # A synthetic ChangeProposed must be in the store (from the workspace scan).
    synth = run.last(ChangeProposed)
    assert synth is not None
    assert "synthesized" in synth.summary.lower() or synth.files_touched
    # The metadata_missing warning must have fired.
    assert any(e["type"] == "metadata_missing" for e in events), (
        f"metadata_missing event not found in: {[e['type'] for e in events]}"
    )
    assert any(e.get("work_detected") for e in events if e["type"] == "metadata_missing")
    # The no-change caveat must NOT appear.
    assert not any("emitted no change" in c for c in result.caveats)


@pytest.mark.asyncio
async def test_emission_failure_no_workspace_changes_preserves_no_change_verdict(
    tmp_path, monkeypatch
):
    """Regression for #1364 (inverse case): when the implementer emits nothing
    AND the workspace shows no changes, the original no-change failure is
    preserved — we do not proceed to the test gate with an empty workspace."""
    _make_git_workspace(tmp_path)

    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do nothing")
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        # Emits nothing and writes nothing — both emission and workspace are empty.
        "implement": _ScriptedBranch(run, [], name="implement"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)

    result = await eng._run(
        run,
        "do nothing",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    # No work → no-change verdict preserved.
    assert result.passed is False
    assert any("emitted no change" in c for c in result.caveats)
    # The test gate must NOT have run.
    assert run.events_of(TestsRan) == [], "test stage ran despite no workspace changes"


# ---------------------------------------------------------------------------
# Pending-experiment ingestion + hypothesis seed round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_experiment_spec_carries_experiment_ref(tmp_path, monkeypatch):
    """A spec that is a pending experiment dict (from a hypothesis export)
    threads its eid into the CodeResultRecorded as experiment_ref."""
    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()
    plan_ev = WorkPlanned(approach="bench", acceptance_criteria=["CSR < CTE/10"])
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _ScriptedBranch(
            run, [ChangeProposed(summary="wrote bench", plan_ref="W-1")], name="implement"
        ),
        "verify": _ScriptedBranch(
            run,
            [VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)],
            name="verify",
        ),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    experiment = {
        "eid": "X-12",
        "method": "benchmark",
        "dataset": "synthetic 500K-edge graph",
        "procedure": "count page reads per traversal",
        "acceptance": "CSR ops < CTE ops / 10",
    }
    result = await eng._run(
        run, experiment, test_cmd=[sys.executable, "-c", "exit(0)"], workspace=str(tmp_path)
    )
    assert result.passed is True
    assert result.experiment_ref == "X-12"


def test_to_hypothesis_seeds_round_trips_into_result_recorded():
    """The bus-interop bridge: every seed must validate as hypothesis's
    ResultRecorded (the model a HypothesisRun ingests)."""
    eng = CodingEngine()
    run = eng.new_run()
    run.experiment_ref = "X-5"
    run.collect(
        CodeResultRecorded(
            passed=True,
            measurements={"returncode": 0, "fix_rounds": 1, "files_touched": ["a.py"]},
            caveats=["validated on python only"],
            experiment_ref="X-5",
        )
    )
    seeds = run.to_hypothesis_seeds()
    assert len(seeds) == 1
    rr = ResultRecorded.model_validate(seeds[0])
    assert rr.experiment_ref == "X-5"
    assert rr.passed is True
    assert isinstance(rr.measurements, str)  # dict was rendered to a string
    assert "fix_rounds" in rr.measurements
    assert rr.caveats == ["validated on python only"]


def test_to_hypothesis_seeds_empty_experiment_ref_still_validates():
    """A task-spec run (no experiment) still yields a valid ResultRecorded seed
    — experiment_ref is required by the model but '' is a valid string."""
    eng = CodingEngine()
    run = eng.new_run()
    run.collect(CodeResultRecorded(passed=False, measurements={"returncode": 1}))
    seed = run.to_hypothesis_seeds()[0]
    assert seed["experiment_ref"] == ""
    rr = ResultRecorded.model_validate(seed)
    assert rr.experiment_ref == "" and rr.passed is False


# ---------------------------------------------------------------------------
# Judge gate on the fix-loop boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_can_stop_fix_loop_early(tmp_path, monkeypatch):
    """With judge_model armed, a REJECT before a fix round stops the loop —
    no further test runs, graceful failed conclude."""
    eng = CodingEngine(max_fix_rounds=5, judge_model="scripted/judge", repair_retries=0)
    run = eng.new_run()
    plan_ev = WorkPlanned(approach="x", acceptance_criteria=["passes"])
    impl = _ScriptedBranch(
        run,
        [ChangeProposed(summary=f"a{i}", plan_ref="W-1") for i in range(1, 5)],
        name="implement",
    )
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": impl,
        "verify": _ScriptedBranch(
            run, [VerifyResult(verdict="REJECT", rationale="x")], name="verify"
        ),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    async def deny(run_, eid, subject):
        return False  # never worth another round

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "judge", deny)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))
    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run, "x", test_cmd=[sys.executable, "-c", "exit(1)"], workspace=str(tmp_path)
    )
    assert result.passed is False
    assert len(run.events_of(TestsRan)) == 1  # judge stopped before any fix round
    assert any(e["type"] == "fix_gated" for e in events)


# ---------------------------------------------------------------------------
# Regression: #1361 — chain events must reach on_event exactly once
# ---------------------------------------------------------------------------


class _BusBranch:
    """Emits events via the raw session bus only (no run.emit()) — the exact
    path a real LLM agent uses.  This is the path that was broken before the
    fix: collect() was triggered by the observer but notify() was never called,
    so WorkPlanned/ChangeProposed/VerifyResult never reached on_event."""

    def __init__(self, run, events: list, *, name: str = "agent"):
        self._run = run
        self._events = list(events)
        self.name = name

    async def operate(self, *, instruction):
        if self._events:
            await self._run.session.emit(self._events.pop(0))
        return "ok"


@pytest.mark.asyncio
async def test_chain_events_reach_on_event_exactly_once_bus_path(tmp_path, monkeypatch):
    """Regression for #1361: WorkPlanned, ChangeProposed, and VerifyResult that
    arrive via the session bus (the real agent path, not run.emit()) must reach
    on_event exactly once each.  The contract: set of eids delivered to on_event
    equals set of eids in the run store, and no eid is delivered twice."""
    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="add foo", acceptance_criteria=["foo exists"])
    change_ev = ChangeProposed(summary="added foo", files_touched=["foo.py"], plan_ref="W-1")
    verdict_ev = VerifyResult(
        verdict="APPROVE", rationale="meets criteria", meets_acceptance=True, tests_ref="T-1"
    )

    # _BusBranch emits via the raw session bus — the bug path
    branches = {
        "plan": _BusBranch(run, [plan_ev], name="plan"),
        "implement": _BusBranch(run, [change_ev], name="implement"),
        "verify": _BusBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    delivered: list[str] = []
    run.on_event = lambda e: delivered.append(e["type"])

    result = await eng._run(
        run,
        "add a foo function",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    assert result.passed is True

    # Every stored chain event kind must be in the delivered stream
    chain_types = {
        "WorkPlanned",
        "ChangeProposed",
        "TestsRan",
        "VerifyResult",
        "CodeResultRecorded",
    }
    delivered_set = set(delivered)
    assert chain_types <= delivered_set, (
        f"Missing from on_event stream: {chain_types - delivered_set}"
    )

    # No eid delivered twice (no double-delivery)
    from collections import Counter

    counts = Counter(delivered)
    doubled = [k for k, v in counts.items() if k in chain_types and v > 1]
    assert not doubled, f"Double-delivered chain event types: {doubled}"


@pytest.mark.asyncio
async def test_chain_events_reach_on_event_exactly_once_emit_path(tmp_path, monkeypatch):
    """Same contract as above but events arrive via run.emit() (the _ScriptedBranch
    path).  Verifies the emit() override does not suppress delivery for chain events
    that go through run.emit() (TestsRan, CodeResultRecorded) and that the agent-emit
    path does not double-deliver WorkPlanned/ChangeProposed/VerifyResult."""
    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="add bar")
    change_ev = ChangeProposed(summary="added bar", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _ScriptedBranch(run, [change_ev], name="implement"),
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    delivered: list[str] = []
    run.on_event = lambda e: delivered.append(e["type"])

    result = await eng._run(
        run,
        "add a bar function",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    assert result.passed is True

    chain_types = {
        "WorkPlanned",
        "ChangeProposed",
        "TestsRan",
        "VerifyResult",
        "CodeResultRecorded",
    }
    delivered_set = set(delivered)
    assert chain_types <= delivered_set, (
        f"Missing from on_event stream: {chain_types - delivered_set}"
    )

    from collections import Counter

    counts = Counter(delivered)
    doubled = [k for k, v in counts.items() if k in chain_types and v > 1]
    assert not doubled, f"Double-delivered chain event types: {doubled}"
