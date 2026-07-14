# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Worker-safety tests: bounded cancel, turn timeout, spec lint, heartbeat/activity, stage watchdog."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from lionagi.engines.coding import (
    ChangeProposed,
    CodingEngine,
    VerifyResult,
    WorkAborted,
    WorkerActivity,
    WorkerHeartbeat,
    WorkPlanned,
    _lint_spec,
)
from lionagi.engines.engine import Engine, EngineRun

# ---------------------------------------------------------------------------
# Helpers shared across sections
# ---------------------------------------------------------------------------


class _StubEngine(Engine):
    async def _run(self, run: EngineRun, *a: Any, **kw: Any) -> Any:  # pragma: no cover
        return ""


def _async(value):
    async def _coro():
        return value

    return _coro()


class _ScriptedBranch:
    def __init__(self, run, events: list, *, name: str = "agent"):
        self._run = run
        self._events = list(events)
        self.name = name
        self.calls: list[str] = []

    async def operate(self, *, instruction, **kw):
        self.calls.append(instruction)
        if self._events:
            await self._run.emit(self._events.pop(0))
        return "ok"


# ---------------------------------------------------------------------------
# bounded cancel_active timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.slow_timing
async def test_cancel_active_returns_within_timeout_when_task_swallows_cancelled_error():
    """cancel_active must return within ~timeout+buffer even when a task swallows CancelledError."""
    run = _StubEngine().new_run()

    async def stubborn():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # swallow — does NOT re-raise
            await asyncio.sleep(60)  # second indefinite wait

    run.spawn(stubborn())
    run.engine.cancel_timeout_s = 0.2
    t0 = time.monotonic()
    await run.cancel_active()
    elapsed = time.monotonic() - t0
    # Must finish within 2s; no hang
    assert elapsed < 2.0, f"cancel_active hung for {elapsed:.2f}s"
    assert not run._active, "_active must be cleared even after timeout"


@pytest.mark.asyncio
async def test_cancel_active_returns_fast_when_tasks_cooperate(monkeypatch: pytest.MonkeyPatch):
    """When tasks cooperate with cancellation, cancel_active returns well before timeout."""
    run = _StubEngine().new_run()

    async def cooperative():
        await asyncio.sleep(60)  # waits indefinitely; will cooperate with CancelledError

    run.spawn(cooperative())
    run.spawn(cooperative())
    run.engine.cancel_timeout_s = 5.0
    original_wait = asyncio.wait
    wait_results: list[tuple[float | None, set[asyncio.Task]]] = []

    async def recording_wait(tasks, *, timeout=None):
        done, pending = await original_wait(tasks, timeout=timeout)
        wait_results.append((timeout, pending))
        return done, pending

    monkeypatch.setattr(asyncio, "wait", recording_wait)
    await run.cancel_active()

    assert wait_results == [(5.0, set())]
    assert not run._active


@pytest.mark.asyncio
async def test_cancel_active_noop_when_empty():
    run = _StubEngine().new_run()
    await run.cancel_active()  # must not raise


# ---------------------------------------------------------------------------
# turn-level timeout into repair loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_timeout_enters_fix_loop(tmp_path, monkeypatch):
    """When branch.operate raises asyncio.TimeoutError, the engine must enter the fix loop."""
    eng = CodingEngine(max_fix_rounds=2, repair_retries=0, turn_timeout_s=0.01)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do something", acceptance_criteria=["passes"])
    timeout_count = 0

    class _TimeoutBranch:
        name = "implement"
        calls: list[str] = []

        async def operate(self, *, instruction, **kw):
            nonlocal timeout_count
            self.calls.append(instruction)
            # First call times out; fix-round call emits a change
            if timeout_count == 0:
                timeout_count += 1
                raise asyncio.TimeoutError("turn timed out")
            await run.emit(ChangeProposed(summary="fixed after timeout", plan_ref="W-1"))
            return "ok"

    impl = _TimeoutBranch()
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)
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

    result = await eng._run(
        run,
        "do something",
        test_cmd=["python", "-c", "exit(0)"],
        workspace=str(tmp_path),
    )
    assert result.passed is True
    assert any(e["type"] == "turn_timeout" for e in events), (
        f"turn_timeout event not found in {[e['type'] for e in events]}"
    )
    assert timeout_count == 1


@pytest.mark.asyncio
async def test_turn_timeout_all_rounds_concludes_failed(tmp_path, monkeypatch):
    """If every turn times out and all fix rounds are spent, concludes failed."""
    import sys

    eng = CodingEngine(max_fix_rounds=1, repair_retries=0, turn_timeout_s=0.01)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="impossible")

    class _AlwaysTimeout:
        name = "implement"

        async def operate(self, *, instruction, **kw):
            raise asyncio.TimeoutError("always")

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _AlwaysTimeout(),
        "verify": _ScriptedBranch(
            run, [VerifyResult(verdict="REJECT", rationale="x")], name="verify"
        ),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    result = await eng._run(
        run,
        "impossible",
        test_cmd=[sys.executable, "-c", "exit(1)"],
        workspace=str(tmp_path),
    )
    assert result.passed is False


# ---------------------------------------------------------------------------
# spec lint
# ---------------------------------------------------------------------------


def test_lint_spec_warns_missing_acceptance_criteria():
    warnings = _lint_spec("Implement a parser", workspace=None)
    assert any("acceptance" in w.lower() for w in warnings)


def test_lint_spec_no_warnings_for_complete_spec(tmp_path):
    (tmp_path / "src.py").write_text("# placeholder\n")
    spec = (
        "Implement a parser.\n"
        "Acceptance criteria: all edge cases pass.\n"
        f"See {tmp_path}/src.py for context."
    )
    warnings = _lint_spec(spec, workspace=str(tmp_path))
    # Must have no acceptance-criteria warning
    assert not any("acceptance" in w.lower() for w in warnings)


def test_lint_spec_strict_raises_on_warnings():
    from lionagi.engines.coding import _lint_spec

    with pytest.raises(ValueError, match="acceptance"):
        _lint_spec("no acceptance here", workspace=None, strict=True)


def test_lint_spec_test_cmd_without_count_assertion_warns():
    spec = "Implement foo. Acceptance criteria: bar. test_cmd: pytest tests/"
    warnings = _lint_spec(spec, workspace=None)
    assert any("count" in w.lower() or "assertion" in w.lower() for w in warnings)


def test_lint_spec_missing_referenced_file_warns(tmp_path):
    spec = f"See {tmp_path}/nonexistent.py for details. Acceptance: passes."
    warnings = _lint_spec(spec, workspace=str(tmp_path))
    assert any("nonexistent.py" in w for w in warnings)


@pytest.mark.asyncio
async def test_spec_lint_events_emitted_before_plan(tmp_path, monkeypatch):
    """spec_lint_warning events must arrive before WorkPlanned."""
    eng = CodingEngine(repair_retries=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="x")
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _ScriptedBranch(
            run, [ChangeProposed(summary="x", plan_ref="W-1")], name="implement"
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

    events: list[dict] = []
    run.on_event = events.append
    import sys

    await eng._run(
        run,
        "Build the module from scratch.",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    lint_idxs = [i for i, e in enumerate(events) if e["type"] == "spec_lint_warning"]
    plan_idxs = [i for i, e in enumerate(events) if e["type"] == "WorkPlanned"]
    assert lint_idxs, "spec_lint_warning events must be emitted"
    assert plan_idxs, "WorkPlanned event must be emitted"
    assert min(lint_idxs) < min(plan_idxs), "lint warnings must precede plan"


@pytest.mark.asyncio
async def test_spec_lint_strict_raises_before_run(tmp_path, monkeypatch):
    """strict_spec=True must raise ValueError before any agent is made."""
    eng = CodingEngine(repair_retries=0, strict_spec=True)
    run = eng.new_run()

    made: list[str] = []

    async def fake_make(role, *, name=None, **kw):
        made.append(name)
        return _ScriptedBranch(run, [], name=name)

    monkeypatch.setattr(run, "make_agent", fake_make)
    import sys

    # "Build the module." has no acceptance criteria → strict mode raises
    with pytest.raises(ValueError):
        await eng._run(
            run,
            "Build the module.",
            test_cmd=[sys.executable, "-c", "exit(0)"],
            workspace=str(tmp_path),
        )
    assert not made, "no agent must be created when strict spec check fails"


# ---------------------------------------------------------------------------
# worker heartbeat + activity events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_events_arrive_at_cadence(tmp_path, monkeypatch):
    """WorkerHeartbeat events must be emitted at the configured cadence during the run."""
    import sys

    eng = CodingEngine(
        repair_retries=0,
        heartbeat_interval_s=0.05,
        max_fix_rounds=0,
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="quick")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    # Slow-ish implement branch so heartbeat fires at least twice
    class _SlowBranch:
        name = "implement"

        async def operate(self, *, instruction, **kw):
            await asyncio.sleep(0.15)
            await run.emit(change_ev)
            return "ok"

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _SlowBranch(),
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    events: list[dict] = []
    run.on_event = events.append

    await eng._run(
        run,
        "do quick work",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    hb_events = [e for e in events if e["type"] == "WorkerHeartbeat"]
    assert len(hb_events) >= 2, f"Expected >=2 heartbeat events, got {len(hb_events)}"
    # elapsed_s must be present and increasing
    assert all("elapsed_s" in e for e in hb_events)


@pytest.mark.asyncio
async def test_no_activity_event_when_no_writes(tmp_path, monkeypatch):
    """WorkerActivity must NOT be emitted when no files change during implement."""
    import sys

    eng = CodingEngine(
        repair_retries=0,
        heartbeat_interval_s=0.05,
        max_fix_rounds=0,
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="noop")
    change_ev = ChangeProposed(summary="nothing written", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    class _NopBranch:
        name = "implement"

        async def operate(self, *, instruction, **kw):
            await asyncio.sleep(0.12)
            await run.emit(change_ev)
            return "ok"

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _NopBranch(),
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    events: list[dict] = []
    run.on_event = events.append

    await eng._run(
        run,
        "do nothing",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    assert not any(e["type"] == "WorkerActivity" for e in events)


@pytest.mark.asyncio
async def test_activity_event_emitted_on_file_write(tmp_path, monkeypatch):
    """WorkerActivity must be emitted when a file is written during implement."""
    import sys

    eng = CodingEngine(
        repair_retries=0,
        heartbeat_interval_s=0.05,
        max_fix_rounds=0,
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="write file")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    target = tmp_path / "out.txt"

    class _WritingBranch:
        name = "implement"

        async def operate(self, *, instruction, **kw):
            await asyncio.sleep(0.02)
            target.write_text("hello\n")
            await asyncio.sleep(0.08)
            await run.emit(ChangeProposed(summary="wrote out.txt", plan_ref="W-1"))
            return "ok"

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

    await eng._run(
        run,
        "write a file",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    assert any(e["type"] == "WorkerActivity" for e in events), (
        f"WorkerActivity missing from {[e['type'] for e in events]}"
    )


@pytest.mark.asyncio
async def test_heartbeat_events_not_in_judge_context(tmp_path, monkeypatch):
    """WorkerHeartbeat events must be excluded from the judge context filter."""
    # Heartbeat events are EngineEvent but not CodingChainEvent — they should
    # not appear in run.events_of(WorkerHeartbeat) via chain store.
    import sys

    eng = CodingEngine(
        repair_retries=0,
        heartbeat_interval_s=0.05,
        max_fix_rounds=0,
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="quick")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    class _SlowBranch:
        name = "implement"

        async def operate(self, *, instruction, **kw):
            await asyncio.sleep(0.12)
            await run.emit(change_ev)
            return "ok"

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _SlowBranch(),
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    await eng._run(
        run,
        "do quick work",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    # WorkerHeartbeat is not a CodingChainEvent; chain store must not have it
    assert not run.events_of(WorkerHeartbeat), "HeartBeat must not pollute the chain store"


# ---------------------------------------------------------------------------
# stage watchdog + partial export on abort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hanging_implement_triggers_work_aborted_and_partial_export(tmp_path, monkeypatch):
    """A hung _implement stage must be aborted; WorkAborted event must fire; export dir must have report.md."""
    import sys

    eng = CodingEngine(
        repair_retries=0,
        max_fix_rounds=0,
        stage_timeout_s=0.2,
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="hang forever")

    class _HangingBranch:
        name = "implement"

        async def operate(self, *, instruction, **kw):
            await asyncio.sleep(60)  # hangs indefinitely
            return "never"

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _HangingBranch(),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches.get(name, _ScriptedBranch(run, [], name=name))

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    export_dir = tmp_path / "export"
    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run,
        "hang forever",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
        export_dir=export_dir,
    )

    # Must complete (not hang)
    work_aborted = [e for e in events if e["type"] == "WorkAborted"]
    assert work_aborted, f"WorkAborted not found in {[e['type'] for e in events]}"
    assert work_aborted[0].get("reason")

    # Partial export must have written report.md
    assert (export_dir / "report.md").exists(), "report.md must be written on abort"

    # Result must indicate failure
    assert result.passed is False


@pytest.mark.asyncio
async def test_no_watchdog_progress_aborts_implement(tmp_path, monkeypatch):
    """A stage with no workspace progress within the watchdog window must abort."""
    import sys

    eng = CodingEngine(
        repair_retries=0,
        max_fix_rounds=0,
        stage_timeout_s=0.3,
    )
    run = eng.new_run()
    plan_ev = WorkPlanned(approach="stall silently")

    class _StalledBranch:
        name = "implement"

        async def operate(self, *, instruction, **kw):
            await asyncio.sleep(60)  # no writes, no emission
            return "never"

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _StalledBranch(),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches.get(name, _ScriptedBranch(run, [], name=name))

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    export_dir = tmp_path / "export"
    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run,
        "stall silently",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
        export_dir=export_dir,
    )

    assert result.passed is False
    assert any(e["type"] == "WorkAborted" for e in events)
    assert (export_dir / "report.md").exists()


@pytest.mark.asyncio
async def test_hanging_fix_round_aborts_with_caveat(tmp_path, monkeypatch):
    """A fix round hung past the watchdog must abort with a caveat and skip verify, not be reported as a plain exhausted-fix-loop failure."""
    import sys

    eng = CodingEngine(repair_retries=0, max_fix_rounds=1, stage_timeout_s=0.2)
    run = eng.new_run()
    plan_ev = WorkPlanned(approach="implement then stall on fix")

    class _FixHangsBranch:
        name = "implement"

        def __init__(self):
            self.calls = 0

        async def operate(self, *, instruction, **kw):
            self.calls += 1
            if self.calls == 1:
                await run.emit(ChangeProposed(summary="initial change"))
                return "ok"
            await asyncio.sleep(60)  # fix round hangs -> watchdog aborts
            return "never"

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _FixHangsBranch(),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches.get(name, _ScriptedBranch(run, [], name=name))

    verify_calls: list[int] = []
    orig_verify = eng._verify

    async def spy_verify(*a, **kw):
        verify_calls.append(1)
        return await orig_verify(*a, **kw)

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_verify", spy_verify)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    export_dir = tmp_path / "export"
    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run,
        "implement then stall on fix",
        test_cmd=[sys.executable, "-c", "exit(1)"],  # fails -> enters fix loop
        workspace=str(tmp_path),
        export_dir=export_dir,
    )

    aborted = [e for e in events if e["type"] == "WorkAborted"]
    assert aborted, f"WorkAborted not found in {[e['type'] for e in events]}"
    assert aborted[0].get("stage") == "fix-1"

    assert result.passed is False
    assert "stage aborted by watchdog" in result.caveats
    assert verify_calls == [], "verify must not run after a watchdog abort"
    assert (export_dir / "report.md").exists()


@pytest.mark.asyncio
async def test_hanging_plan_is_bounded_but_does_not_abort_the_run(tmp_path, monkeypatch):
    """A hung _plan stage must be bounded (soft watchdog) and degrade to the raw-task
    plan, letting implement/test proceed — it must NOT poison run._aborted and fail the run."""
    import sys

    eng = CodingEngine(repair_retries=0, max_fix_rounds=0, stage_timeout_s=0.2)
    run = eng.new_run()

    class _HangingPlanBranch:
        name = "plan"

        async def operate(self, *, instruction, **kw):
            await asyncio.sleep(60)  # planner hangs indefinitely
            return "never"

    implement_branch = _ScriptedBranch(
        run, [ChangeProposed(summary="did the work")], name="implement"
    )
    branches = {"plan": _HangingPlanBranch(), "implement": implement_branch}

    async def fake_make(role, *, name=None, **kw):
        return branches.get(name, _ScriptedBranch(run, [], name=name))

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    export_dir = tmp_path / "export"
    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run,
        "plan hangs but work still happens",
        test_cmd=[sys.executable, "-c", "exit(0)"],  # tests pass
        workspace=str(tmp_path),
        export_dir=export_dir,
    )

    plan_aborted = [e for e in events if e["type"] == "WorkAborted" and e.get("stage") == "plan"]
    assert plan_aborted, f"soft plan-abort not found in {[e['type'] for e in events]}"
    assert plan_aborted[0].get("hard") is False, "plan watchdog must be soft (hard=False)"

    # The run must NOT be failed by the plan timeout: implement ran and tests passed.
    assert implement_branch.calls, "implement must run despite a hung planner"
    assert result.passed is True
    assert "stage aborted by watchdog" not in result.caveats


@pytest.mark.asyncio
async def test_hanging_verify_is_bounded_and_run_still_concludes(tmp_path, monkeypatch):
    """A hung _verify stage must be bounded (soft watchdog): the advisory verdict is
    dropped, but the run concludes on the test result rather than hanging or failing."""
    import sys

    eng = CodingEngine(repair_retries=0, max_fix_rounds=0, stage_timeout_s=0.2)
    run = eng.new_run()
    plan_ev = WorkPlanned(approach="implement, then verify hangs")

    class _HangingVerifyBranch:
        name = "verify"

        async def operate(self, *, instruction, **kw):
            await asyncio.sleep(60)  # verifier hangs indefinitely
            return "never"

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _ScriptedBranch(
            run, [ChangeProposed(summary="did the work")], name="implement"
        ),
        "verify": _HangingVerifyBranch(),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches.get(name, _ScriptedBranch(run, [], name=name))

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    export_dir = tmp_path / "export"
    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run,
        "verify hangs after passing tests",
        test_cmd=[sys.executable, "-c", "exit(0)"],  # tests pass
        workspace=str(tmp_path),
        export_dir=export_dir,
    )

    verify_aborted = [
        e for e in events if e["type"] == "WorkAborted" and e.get("stage") == "verify"
    ]
    assert verify_aborted, f"soft verify-abort not found in {[e['type'] for e in events]}"
    assert verify_aborted[0].get("hard") is False, "verify watchdog must be soft (hard=False)"

    # Verdict comes from the tests, not the (dropped) verify note; run concludes.
    assert result.passed is True
    assert "stage aborted by watchdog" not in result.caveats
    assert (export_dir / "report.md").exists()
