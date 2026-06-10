# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for budget/deadline cancellation exporting partial results.

Acceptance criteria:
- A run that exhausts its budget with >=1 conclusion-equivalent collected exits
  normally (no CancelledError raised), writes report.md + chains.json, and the
  report states it was budget-bounded.
- External cancellation (caller-initiated) still propagates as CancelledError.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from lionagi.engines.engine import Engine, EngineRun
from lionagi.engines.hypothesis import (
    ChainEvent,
    ConclusionDrawn,
    FindingPosted,
    HypothesisEngine,
    HypothesisRun,
    QuestionRaised,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wire_minimal(eng: HypothesisEngine, run: HypothesisRun) -> None:
    """Register collector + reactions on the run, mirroring what _run() does."""
    from lionagi.engines.hypothesis import (
        ExperimentDesigned,
        HypothesisFormed,
        ResultRecorded,
    )

    run.observe(ChainEvent, lambda e, _c: run.collect(e))
    run.observe(FindingPosted, lambda f, _c: eng._on_finding(run, f))
    run.observe(QuestionRaised, lambda q, _c: eng._on_question(run, q))
    run.observe(HypothesisFormed, lambda h, _c: eng._on_hypothesis(run, h))
    run.observe(ExperimentDesigned, lambda x, _c: eng._on_experiment(run, x))
    run.observe(ResultRecorded, lambda r, _c: eng._on_result(run, r))
    run.observe(ConclusionDrawn, lambda c, _c: eng._on_conclusion(run, c))


class _StubEngine(Engine):
    async def _run(self, run: EngineRun, *a: Any, **kw: Any) -> Any:  # pragma: no cover
        return ""


# ---------------------------------------------------------------------------
# Internal cancellation — partial export must happen and return normally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_cancel_produces_partial_report_and_returns(tmp_path: Path) -> None:
    """When the engine's own deadline watchdog cancels the run after >=1
    ConclusionDrawn has been collected, Engine.run() must return normally
    (not raise CancelledError) and write report.md + chains.json.

    The report must carry the 'budget_exhausted' status marker.
    """
    eng = HypothesisEngine(repair_retries=0)
    run_holder: list[HypothesisRun] = []

    async def fake_extract(run: HypothesisRun, f: FindingPosted) -> None:
        # Simulate the pipeline collecting one conclusion before budget cuts in.
        q = run.collect(QuestionRaised(area="perf", what_is_unknown="why X over Y?"))
        run.collect(
            ConclusionDrawn(
                question_ref=q.eid,
                verdict="X is faster",
                rationale="measured 2x",
                basis="empirical",
            )
        )

    async def fake_synthesize(run: HypothesisRun) -> str:
        return "PARTIAL SYNTHESIS"

    eng._extract = fake_extract  # type: ignore[method-assign]
    eng._synthesize = fake_synthesize  # type: ignore[method-assign]

    # Override new_run to capture the run object so we can pre-set
    # _budget_notified (simulating what the watchdog would do) without
    # actually setting a real deadline (which would be racy in tests).
    original_new_run = eng.new_run

    def capturing_new_run(**kw: Any) -> HypothesisRun:
        run = original_new_run(**kw)
        run_holder.append(run)
        return run

    eng.new_run = capturing_new_run  # type: ignore[method-assign]

    export_dir = tmp_path / "out"

    # Override _run to collect state then cancel itself as the watchdog would.
    async def simulated_run(
        run: HypothesisRun,
        findings: str | list[str],
        *,
        decisions: str = "",
        export_dir: str | Path | None = None,
    ) -> str:
        _wire_minimal(eng, run)
        run.decisions = decisions
        run.root = "test finding"
        # Emit the seed finding so fake_extract runs and collects a conclusion.
        await run.emit(FindingPosted(description="test finding", source="seed", gen=0))
        await run.wait_quiescence()
        # Simulate the watchdog: mark budget notified, then cancel self.
        run._budget_notified = True
        assert run._run_task is not None
        run._run_task.cancel()
        # This await is what gets cancelled.
        await asyncio.sleep(10)
        return "should never reach here"

    eng._run = simulated_run  # type: ignore[method-assign]

    events: list[dict] = []
    result = await eng.run(
        "test finding",
        export_dir=export_dir,
        on_event=events.append,
    )

    # Must return normally with a report string.
    assert isinstance(result, str), f"Expected str result, got {type(result)}: {result!r}"
    assert result, "Expected non-empty partial report"
    assert "budget_exhausted" in result, (
        f"Report must contain 'budget_exhausted' status marker; got: {result!r}"
    )

    # Export files must exist.
    assert (export_dir / "report.md").exists(), "report.md must be written on budget cancellation"
    assert (export_dir / "chains.json").exists(), (
        "chains.json must be written on budget cancellation"
    )

    report_text = (export_dir / "report.md").read_text()
    assert "budget_exhausted" in report_text, (
        f"report.md must carry budget_exhausted status; got: {report_text[:300]!r}"
    )

    # The 'exported' event must have been emitted.
    assert any(e["type"] == "exported" for e in events), (
        f"'exported' event must be emitted; got event types: {[e['type'] for e in events]}"
    )


@pytest.mark.asyncio
async def test_budget_cancel_with_no_conclusions_returns_without_crash() -> None:
    """When the watchdog cancels a run that collected NO events, Engine.run()
    must return cleanly (no crash, no CancelledError) with an empty/None result.
    """
    eng = HypothesisEngine(repair_retries=0)

    async def simulated_run(
        run: HypothesisRun,
        findings: str | list[str],
        *,
        decisions: str = "",
        export_dir: str | Path | None = None,
    ) -> str:
        run.decisions = decisions
        run.root = "empty"
        # Mark budget notified (what the watchdog does), then cancel self.
        run._budget_notified = True
        assert run._run_task is not None
        run._run_task.cancel()
        await asyncio.sleep(10)
        return "never"

    eng._run = simulated_run  # type: ignore[method-assign]

    # Must not raise.
    result = await eng.run("empty finding")
    # No events collected — _partial_export returns "" or None, both are valid.
    assert result in (None, ""), f"Expected None or empty string, got {result!r}"


# ---------------------------------------------------------------------------
# External cancellation — must still propagate as CancelledError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_cancellation_propagates_as_cancelled_error() -> None:
    """When the *caller* cancels Engine.run() (not the engine's own watchdog),
    CancelledError must propagate to the caller — the engine must not treat
    external cancellation as budget exhaustion and swallow it.
    """
    eng = HypothesisEngine(repair_retries=0)
    started = asyncio.Event()

    async def blocking_run(
        run: HypothesisRun,
        findings: str | list[str],
        *,
        decisions: str = "",
        export_dir: str | Path | None = None,
    ) -> str:
        # Do NOT set _budget_notified — this simulates a caller cancel, not watchdog.
        started.set()
        await asyncio.sleep(60)
        return "never"

    eng._run = blocking_run  # type: ignore[method-assign]

    outer = asyncio.ensure_future(eng.run("some finding"))
    await asyncio.wait_for(started.wait(), timeout=5)
    outer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await outer


@pytest.mark.asyncio
async def test_external_cancel_during_partial_export_propagates() -> None:
    """Regression: if the caller cancels Engine.run() while it is executing the
    shielded partial export phase, CancelledError must still propagate to the
    caller — the engine must not swallow it and return a result.

    Repro: a _partial_export override that signals when it has started, then
    sleeps; the caller cancels after the signal; Engine.run() must raise.
    """
    eng = _StubEngine()
    partial_entered = asyncio.Event()

    async def slow_partial_export(run_obj: EngineRun, *a: Any, **kw: Any) -> Any:
        partial_entered.set()
        await asyncio.sleep(30)  # long enough for the caller cancel to arrive
        return "should never reach"

    eng._partial_export = slow_partial_export  # type: ignore[method-assign]

    async def simulated_run_internal_cancel(run_obj: EngineRun, *a: Any, **kw: Any) -> Any:
        run_obj._budget_notified = True
        assert run_obj._run_task is not None
        run_obj._run_task.cancel()
        await asyncio.sleep(10)
        return "never"

    eng._run = simulated_run_internal_cancel  # type: ignore[method-assign]

    outer = asyncio.ensure_future(eng.run())
    # Wait until partial export has started, then cancel from the caller side.
    await asyncio.wait_for(partial_entered.wait(), timeout=5)
    outer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await outer


@pytest.mark.asyncio
async def test_active_tasks_cancelled_before_partial_export() -> None:
    """Regression: spawned background tasks (run._active) must be cancelled
    BEFORE _partial_export starts so synthesis sees a stable snapshot and no
    tokens are burned past budget exhaustion.

    The test plants a spawned task that appends to a list if it survives past
    the budget cancel, and a _partial_export override that records whether any
    active tasks remain on entry.  After the fix, _active must be empty when
    _partial_export is called.
    """
    eng = _StubEngine()
    active_on_entry: list[int] = []  # count of _active tasks when partial export started
    survived_appends: list[str] = []  # appended if a spawned task ran past cancel

    async def leaky_worker() -> None:
        try:
            await asyncio.sleep(30)
            survived_appends.append("leaked")
        except asyncio.CancelledError:
            raise

    async def recording_partial_export(run_obj: EngineRun, *a: Any, **kw: Any) -> Any:
        active_on_entry.append(len(run_obj._active))
        return "partial"

    eng._partial_export = recording_partial_export  # type: ignore[method-assign]

    async def simulated_run_with_spawn(run_obj: EngineRun, *a: Any, **kw: Any) -> Any:
        run_obj.spawn(leaky_worker())
        run_obj._budget_notified = True
        assert run_obj._run_task is not None
        run_obj._run_task.cancel()
        await asyncio.sleep(10)
        return "never"

    eng._run = simulated_run_with_spawn  # type: ignore[method-assign]

    result = await eng.run()
    assert result == "partial"
    # No background tasks must remain active when partial export runs.
    assert active_on_entry == [0], (
        f"_active must be empty when _partial_export starts; had {active_on_entry[0]} tasks"
    )
    # Give the loop a tick and verify the worker did not survive.
    await asyncio.sleep(0)
    assert not survived_appends, "spawned task must not have run to completion past budget cancel"


@pytest.mark.asyncio
async def test_budget_cancel_without_export_dir_does_not_crash() -> None:
    """Budget cancellation without an export_dir still returns the partial
    report string and does not crash.
    """
    eng = HypothesisEngine(repair_retries=0)

    async def fake_extract(run: HypothesisRun, f: FindingPosted) -> None:
        q = run.collect(QuestionRaised(area="a", what_is_unknown="why?"))
        run.collect(
            ConclusionDrawn(
                question_ref=q.eid,
                verdict="answer",
                rationale="because",
                basis="taste",
            )
        )

    async def fake_synthesize(run: HypothesisRun) -> str:
        return "SYNTHESIS WITHOUT EXPORT"

    eng._extract = fake_extract  # type: ignore[method-assign]
    eng._synthesize = fake_synthesize  # type: ignore[method-assign]

    async def simulated_run(
        run: HypothesisRun,
        findings: str | list[str],
        *,
        decisions: str = "",
        export_dir: str | Path | None = None,
    ) -> str:
        _wire_minimal(eng, run)
        run.decisions = decisions
        run.root = "no export dir"
        await run.emit(FindingPosted(description="no export dir", source="seed", gen=0))
        await run.wait_quiescence()
        run._budget_notified = True
        assert run._run_task is not None
        run._run_task.cancel()
        await asyncio.sleep(10)
        return "never"

    eng._run = simulated_run  # type: ignore[method-assign]

    # No export_dir — must not crash, must return partial report string.
    result = await eng.run("no export dir")
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert "budget_exhausted" in result
