# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coding engine unit tests — gated implement/test/fix loop with a real subprocess test runner."""

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
    # A git workspace is required so workspace-check succeeds and the delta is
    # provably empty — the no-change verdict is only valid when the check itself
    # did not fail (check failure must fail open, not collapse to no-change).
    _make_git_workspace(tmp_path)
    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()
    plan_ev = WorkPlanned(approach="x")
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _ScriptedBranch(run, [], name="implement"),  # emits nothing, writes nothing
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
# Workspace is ground truth when emission fails
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
    """No ChangeProposed but workspace has changes: engine must run test gate, not record no-change."""
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
    """No emission and no workspace changes must preserve the no-change failure verdict."""
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


@pytest.mark.asyncio
async def test_pre_dirty_workspace_no_worker_output_preserves_no_change(tmp_path, monkeypatch):
    """Pre-existing dirty workspace files before implement must not be attributed to the worker."""
    _make_git_workspace(tmp_path)
    # Pre-existing dirty state: a staged file and an untracked file, both
    # created before the engine starts — not worker output.
    staging_dir = tmp_path / "_staging"
    staging_dir.mkdir()
    (staging_dir / "old.txt").write_text("pre-existing\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "_staging/old.txt"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    (tmp_path / "fixture.py").write_text("# fixture\n", encoding="utf-8")
    # Both are present before _run; the implementer is a no-op.

    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()
    plan_ev = WorkPlanned(approach="do nothing")
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
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
    # Pre-existing dirty state is in the baseline → delta is empty → no-change.
    assert result.passed is False
    assert any("emitted no change" in c for c in result.caveats)
    assert run.events_of(TestsRan) == [], "test stage ran on pre-existing dirty state"


@pytest.mark.asyncio
async def test_untracked_only_work_verify_diff_contains_file_content(tmp_path, monkeypatch):
    """Untracked file written by implementer must appear in verify stage diff, not as empty diff."""
    _make_git_workspace(tmp_path)

    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="write lib.py")
    target_file = tmp_path / "lib.py"
    lib_content = "def compute(): return 99\n"

    class _UntrackedBranch:
        name = "implement"

        async def operate(self, *, instruction):
            target_file.write_text(lib_content, encoding="utf-8")
            return "wrote lib.py"  # prose, no emission

    verdict_holder: list[VerifyResult] = []

    class _CapturingVerifyBranch:
        name = "verify"

        async def operate(self, *, instruction):
            # Capture the diff the verify stage received via its instruction text.
            verdict_holder.append(instruction)
            await run.emit(
                VerifyResult(verdict="APPROVE", rationale="content present", meets_acceptance=True)
            )
            return "ok"

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _UntrackedBranch(),
        "verify": _CapturingVerifyBranch(),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)

    test_cmd = [
        sys.executable,
        "-c",
        f"import sys; sys.exit(0 if __import__('os').path.exists(r'{target_file}') else 1)",
    ]
    result = await eng._run(run, "write lib.py", test_cmd=test_cmd, workspace=str(tmp_path))

    assert result.passed is True
    # The verify instruction must contain the file content — not the "(no diff captured)" stub.
    assert verdict_holder, "verify stage never ran"
    verify_instruction = verdict_holder[0]
    assert "compute" in verify_instruction, (
        f"untracked file content missing from verify diff; got: {verify_instruction[:300]!r}"
    )
    assert "(no diff captured" not in verify_instruction


@pytest.mark.asyncio
async def test_workspace_check_failure_fails_open_to_test_gate(tmp_path, monkeypatch):
    """git status failure must emit workspace_check_failed and fail open to the test gate."""
    # tmp_path is NOT a git repo → git status will fail → check_failed=True.
    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="write something")

    class _WritingBranch:
        name = "implement"

        async def operate(self, *, instruction):
            (tmp_path / "output.txt").write_text("done\n", encoding="utf-8")
            return "wrote output.txt"  # prose, no emission

    verdict_ev = VerifyResult(verdict="APPROVE", rationale="gate passed", meets_acceptance=True)
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _WritingBranch(),
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    events: list[dict] = []
    run.on_event = events.append

    # Gate always passes — we want to confirm the test stage ran at all.
    result = await eng._run(
        run,
        "write something",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    # workspace_check_failed event must have fired.
    assert any(e["type"] == "workspace_check_failed" for e in events), (
        f"workspace_check_failed not in: {[e['type'] for e in events]}"
    )
    # Fail open: test gate ran.
    assert len(run.events_of(TestsRan)) >= 1, "test stage did not run after workspace_check_failed"
    # Test gate passed → result passed.
    assert result.passed is True
    # No-change caveat must NOT appear (that is only for provably-empty delta).
    assert not any("emitted no change" in c for c in result.caveats)


@pytest.mark.asyncio
async def test_baseline_capture_failure_triggers_workspace_check_failed_not_no_change(
    tmp_path, monkeypatch
):
    """Baseline capture failure must set check_failed=True and fail open, not collapse to no-change."""
    _make_git_workspace(tmp_path)

    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="x")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="gate passed", meets_acceptance=True)
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        # Emits nothing, writes nothing — but baseline capture fails.
        "implement": _ScriptedBranch(run, [], name="implement"),
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))
    # Simulate baseline capture failure.
    monkeypatch.setattr(eng, "_capture_ws_baseline", lambda r: _async(None))

    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run,
        "do nothing",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    # Baseline failure → workspace_check_failed, NOT no-change.
    assert any(e["type"] == "workspace_check_failed" for e in events), (
        f"workspace_check_failed missing; got: {[e['type'] for e in events]}"
    )
    assert not any("emitted no change" in c for c in result.caveats), (
        "no-change verdict taken despite baseline failure — should have been workspace_check_failed"
    )
    # Fail open: test gate ran.
    assert len(run.events_of(TestsRan)) >= 1, "test gate did not run after baseline failure"
    # Test gate exits 0 → passed.
    assert result.passed is True


@pytest.mark.asyncio
async def test_fix_round_untracked_file_reaches_verify_diff(tmp_path, monkeypatch):
    """Untracked file created during a fix round must appear in the verify diff."""
    _make_git_workspace(tmp_path)

    fix_file = tmp_path / "fix_output.py"
    fix_content = "def fixed(): return 'repaired'\n"

    eng = CodingEngine(max_fix_rounds=2, repair_retries=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="fix it", acceptance_criteria=["passes"])
    # First change: emits but test fails (flip-style: flag absent → exit 1).
    flag = tmp_path / "flip.flag"
    change1 = ChangeProposed(summary="attempt 1", plan_ref="W-1")
    # Fix-round change: writes a new untracked file, emits ChangeProposed.
    change2 = ChangeProposed(
        summary="attempt 2 — wrote fix_output.py", files_touched=["fix_output.py"], plan_ref="W-1"
    )

    class _FixRoundBranch:
        """First operate() emits change1; second (fix round) writes the file
        and emits change2."""

        name = "implement"
        _calls = 0

        async def operate(self, *, instruction):
            self._calls += 1
            if self._calls == 1:
                await run.emit(change1)
            else:
                fix_file.write_text(fix_content, encoding="utf-8")
                await run.emit(change2)
            return "ok"

    verify_instruction_holder: list[str] = []

    class _CapturingVerifyBranch:
        name = "verify"

        async def operate(self, *, instruction):
            verify_instruction_holder.append(instruction)
            await run.emit(
                VerifyResult(verdict="APPROVE", rationale="fix present", meets_acceptance=True)
            )
            return "ok"

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _FixRoundBranch(),
        "verify": _CapturingVerifyBranch(),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)

    result = await eng._run(
        run,
        "fix it",
        test_cmd=_flip_test_cmd(flag),
        workspace=str(tmp_path),
    )

    assert result.passed is True, f"expected passed=True; caveats={result.caveats}"
    assert verify_instruction_holder, "verify stage never ran"
    verify_instruction = verify_instruction_holder[0]
    assert "fixed" in verify_instruction or "repaired" in verify_instruction, (
        f"fix-round untracked file content missing from verify diff; "
        f"got: {verify_instruction[:400]!r}"
    )


@pytest.mark.asyncio
async def test_absolute_files_touched_reaches_verify_diff(tmp_path, monkeypatch):
    """Absolute path in ChangeProposed.files_touched must still produce content in the verify diff."""
    _make_git_workspace(tmp_path)

    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="write abs.py")
    abs_file = tmp_path / "abs.py"
    abs_content = "def absolute(): return 'abs'\n"

    class _AbsPathBranch:
        """Writes a file and emits ChangeProposed with the ABSOLUTE path."""

        name = "implement"

        async def operate(self, *, instruction):
            abs_file.write_text(abs_content, encoding="utf-8")
            # Emit with the absolute path — as the coding tool would.
            await run.emit(
                ChangeProposed(
                    summary="wrote abs.py",
                    files_touched=[str(abs_file)],  # absolute path
                    plan_ref="W-1",
                )
            )
            return "ok"

    verify_instruction_holder: list[str] = []

    class _CapturingVerifyBranch:
        name = "verify"

        async def operate(self, *, instruction):
            verify_instruction_holder.append(instruction)
            await run.emit(
                VerifyResult(verdict="APPROVE", rationale="content present", meets_acceptance=True)
            )
            return "ok"

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _AbsPathBranch(),
        "verify": _CapturingVerifyBranch(),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)

    test_cmd = [
        sys.executable,
        "-c",
        f"import sys; sys.exit(0 if __import__('os').path.exists(r'{abs_file}') else 1)",
    ]
    result = await eng._run(run, "write abs.py", test_cmd=test_cmd, workspace=str(tmp_path))

    assert result.passed is True
    assert verify_instruction_holder, "verify stage never ran"
    verify_instruction = verify_instruction_holder[0]
    assert "absolute" in verify_instruction, (
        f"absolute-path untracked file content missing from verify diff; "
        f"got: {verify_instruction[:400]!r}"
    )
    assert "(no diff captured" not in verify_instruction


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
# Chain events must reach on_event exactly once
# ---------------------------------------------------------------------------


class _BusBranch:
    """Emits events via the raw session bus only (no run.emit()) — the path a real LLM agent uses."""

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
    """Chain events via the session bus must reach on_event exactly once each."""
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
    """Chain events via run.emit() must also reach on_event exactly once each."""
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
