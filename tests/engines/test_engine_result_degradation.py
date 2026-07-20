# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""EngineResult + degradation contract: root/spawned budget errors must degrade, never crash or
be silently swallowed into a clean-looking result."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lionagi.engines.engine import Engine, EngineBudgetError, EngineResult
from lionagi.engines.hypothesis import HypothesisEngine
from lionagi.engines.planning import PlanningEngine
from lionagi.engines.research import FindingEmitted, ResearchEngine
from lionagi.engines.review import IssueFound, ReviewEngine, ReviewVerdict
from lionagi.ln import gather as ln_gather
from lionagi.ln.concurrency._compat import (
    ExceptionGroup,
    get_exception_group_exceptions,
    is_exception_group,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finding_factory(name: str) -> FindingEmitted:
    return FindingEmitted(description=f"finding via {name}", novelty=0.9)


def _issue_factory(name: str) -> IssueFound:
    dimension = name.removeprefix("review-")
    return IssueFound(dimension=dimension, description=f"issue via {name}")


class _FakeBranch:
    """Stand-in for a real agent Branch: no network, but emits a real event
    (except for the terminal "synthesizer"/"verdict" stage) so the run
    collects genuine structured events before degrading."""

    def __init__(self, run: Any, name: str, emit_factory: Any = _finding_factory) -> None:
        self.run = run
        self.name = name
        self._emit_factory = emit_factory

    async def operate(self, *, instruction: str) -> str:
        if self.name in ("synthesizer", "verdict"):
            return f"SYNTHESIS over {len(self.run.by_type(FindingEmitted))} events"
        await self.run.emit(self._emit_factory(self.name))
        return "found something"


def _budget_gated_make_agent(run: Any, emit_factory: Any = _finding_factory):
    """A fake EngineRun.make_agent with the exact budget check/raise the real one has, minus networking."""

    async def fake_make_agent(
        role: str, *, name: str | None = None, exempt: bool = False, **kw: Any
    ):
        if not exempt and not run.budget_left():
            run._notify_budget_once("make_agent")
            raise EngineBudgetError(
                f"agent budget exhausted ({run.agents_made}/{run.engine.max_agents})"
            )
        run.agents_made += 1
        return _FakeBranch(run, name=name or role, emit_factory=emit_factory)

    return fake_make_agent


# ---------------------------------------------------------------------------
# 1. Repro FRICTION_LOG run 5a — spawned discretionary expansion crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_verbose_root_spawn_budget_race_degrades_not_crashes(monkeypatch):
    """A verbose root turn spawning far more concurrent _explore() teams than
    the agent budget allows must not crash run() with an ExceptionGroup — the
    run must degrade and return a non-empty EngineResult."""
    eng = ResearchEngine(max_agents=10, repair_retries=0)
    run = eng.new_run()
    monkeypatch.setattr(run, "make_agent", _budget_gated_make_agent(run))
    monkeypatch.setattr(eng, "new_run", lambda **kw: run)

    async def fake_run(run_obj: Any, topic: str) -> str:
        run_obj.root = topic
        # One exploration completes fully and deterministically first (no
        # race), guaranteeing real findings exist before the race below.
        await eng._explore(run_obj, "seed sub-topic", 1)
        # Then the verbose-root-turn race: 8 more concurrent discretionary
        # expansions contend for the remaining budget — this is what crashed
        # the whole run pre-fix (ExceptionGroup of EngineBudgetError raised
        # out of run.spawn()'d _explore() -> _team_for() -> make_agent()).
        for i in range(8):
            run_obj.spawn(eng._explore(run_obj, f"sub-{i}", 1))
        await run_obj.wait_quiescence()
        return await eng._synthesize(run_obj, topic)

    monkeypatch.setattr(eng, "_run", fake_run)

    result = await eng.run("root topic")

    assert isinstance(result, EngineResult), f"expected EngineResult, got {type(result)}"
    assert result.degraded is True
    assert result.text, "expected non-empty synthesized text"
    assert result.events_by_type(FindingEmitted), "expected real findings collected before the cap"


# ---------------------------------------------------------------------------
# 2. Repro FRICTION_LOG run 3 — root-level (gathered) raw raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_root_gather_budget_error_does_not_raise(monkeypatch):
    """A too-tight max_agents hitting the root dimension fan-out (ln_gather in
    ReviewEngine._run) must not raise EngineBudgetError to the caller.

    ReviewEngine now overrides _partial_export (matching Research/Hypothesis):
    when the budget hits before any ReviewVerdict was ever computed, it
    returns an empty string instead of the base class's bare None default, so
    the caller always gets a wrapped EngineResult with the degrade signal
    surfaced through .degraded/.degrade_reason, not just the internal
    run._budget_notified flag.
    """
    eng = ReviewEngine(dimensions=("correctness", "security"), max_agents=1, repair_retries=0)
    run = eng.new_run()
    monkeypatch.setattr(
        run, "make_agent", _budget_gated_make_agent(run, emit_factory=_issue_factory)
    )
    monkeypatch.setattr(eng, "new_run", lambda **kw: run)

    result = await eng.run("some artifact text")

    assert isinstance(result, EngineResult), f"expected EngineResult, got {type(result)!r}"
    assert result.degraded is True
    assert result.degrade_reason == "budget"
    assert result.text == "", "no verdict was ever computed before the budget hit"
    assert run.agents_made == 1, "only one dimension reviewer should have been made"
    assert run._budget_notified is True, "the budget-exhaustion signal must still be recorded"


# ---------------------------------------------------------------------------
# 2b. Repro production bug gtd 6b76e4ff (2026-07-17) — verdict computed then
#     dropped when the deadline fires before synth.operate() returns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.slow_timing
async def test_review_verdict_emitted_on_exhaustion_not_dropped(monkeypatch):
    """A ReviewVerdict already captured onto the run's event store (via the
    branch's async signal-emission side channel, exactly like a real LLM
    turn's on_message_added -> fire-and-forget emit_message()) before the
    deadline cancels the run must be returned via _partial_export, not
    discarded.

    Reproduces the production bug (gtd 6b76e4ff, 2026-07-17, leo's reactive
    PR-review daemon): the codex terra engine sometimes needs emission
    retries before its structured verdict settles. If the deadline watchdog
    fires while _verdict()'s synth.operate() call is still in flight, the
    verdict was already computed/emitted onto run.by_type(ReviewVerdict), but
    pre-fix the base Engine._partial_export no-op (inherited by ReviewEngine)
    discarded it and Engine.run() returned a bare None — the daemon reported
    NO-VERDICT despite a verdict existing in the event stream.
    """
    verdict = ReviewVerdict(
        verdict="REQUEST-CHANGES",
        rationale="the sql query is not parameterized",
        blocking=["sqli in query()"],
    )

    class _FastReviewBranch:
        def __init__(self, dimension: str):
            self.name = f"review-{dimension}"

        async def operate(self, *, instruction: str) -> str:
            return ""  # no issues found — keeps the repro focused on the verdict stage

    class _SlowSynthBranch:
        name = "verdict"

        def __init__(self, run: Any) -> None:
            self._run = run

        async def operate(self, *, instruction: str) -> str:
            # The verdict is "computed": captured onto the session bus exactly
            # like the real fire-and-forget StructuredOutput path does for an
            # LLM turn, independent of whether this operate() call itself
            # ever returns.
            await self._run.emit(verdict)
            # ... then the codex terra engine's own emission-retry turn blows
            # past the deadline before operate() can return.
            await asyncio.sleep(10)
            return "unreachable"  # pragma: no cover

    eng = ReviewEngine(dimensions=("correctness",), deadline_s=0.05, repair_retries=0)
    run = eng.new_run()

    async def fake_make_agent(role: str, *, name: str | None = None, exempt: bool = False, **kw):
        if name == "verdict":
            return _SlowSynthBranch(run)
        return _FastReviewBranch(name or role)

    monkeypatch.setattr(run, "make_agent", fake_make_agent)
    monkeypatch.setattr(eng, "new_run", lambda **kw: run)

    result = await eng.run("some artifact text")

    assert isinstance(result, EngineResult), f"expected EngineResult, got {type(result)!r}"
    assert result.degraded is True
    assert result.degrade_reason == "deadline"
    captured = result.events_by_type(ReviewVerdict)
    assert captured, "the already-computed verdict must survive into the partial-export result"
    assert captured[-1].verdict == "REQUEST-CHANGES"
    assert "REQUEST-CHANGES" in result.text


# ---------------------------------------------------------------------------
# 3. Masking guard — a real error in a mixed group must still surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_masking_guard_reraises_real_error_in_mixed_group():
    """A mixed failure (one task raises EngineBudgetError, a sibling raises a
    genuine error) must not be laundered into a partial result — the real
    error must still surface from run()."""

    class _MixedFailEngine(Engine):
        async def _run(self, run: Any, *a: Any, **kw: Any) -> Any:
            async def budget_side() -> None:
                raise EngineBudgetError("out of budget")

            async def real_bug() -> None:
                raise ValueError("genuine bug")

            await ln_gather(budget_side(), real_bug())
            return "never"  # pragma: no cover

    eng = _MixedFailEngine(max_agents=1)
    with pytest.raises(BaseException) as exc_info:
        await eng.run()

    exc = exc_info.value
    subs = get_exception_group_exceptions(exc) if is_exception_group(exc) else [exc]
    assert any(isinstance(e, ValueError) for e in subs), (
        f"the genuine ValueError must surface, got {exc!r}"
    )


def _leaves(exc: BaseException) -> list[BaseException]:
    """Flatten an (optionally nested) exception group into its leaf exceptions."""
    if is_exception_group(exc):
        out: list[BaseException] = []
        for e in get_exception_group_exceptions(exc):
            out.extend(_leaves(e))
        return out
    return [exc]


@pytest.mark.asyncio
async def test_masking_guard_degrades_nested_all_budget_group():
    """A NESTED group whose every leaf is EngineBudgetError (a group inside a
    group, not just a flat list) must still degrade, not crash — the masking
    guard's predicate must recurse, not just inspect the top-level children.

    This test engine overrides _partial_export (a test-local fixture, not one
    of the five shipped engines) purely so the degrade path produces a str
    and the resulting EngineResult's .degraded/.degrade_reason are directly
    inspectable, without relying on the internal _budget_notified flag.
    """

    class _NestedBudgetEngine(Engine):
        async def _run(self, run: Any, *a: Any, **kw: Any) -> Any:
            raise ExceptionGroup(
                "outer", [ExceptionGroup("inner", [EngineBudgetError("out of budget")])]
            )

        async def _partial_export(self, run: Any, *a: Any, **kw: Any) -> str:
            return "partial output from nested budget group"

    eng = _NestedBudgetEngine(max_agents=1)
    result = await eng.run()

    assert isinstance(result, EngineResult), f"expected EngineResult, got {type(result)}"
    assert result.degraded is True
    assert result.degrade_reason == "budget"
    assert result.text == "partial output from nested budget group"


@pytest.mark.asyncio
async def test_masking_guard_reraises_real_error_buried_in_nested_group():
    """A genuine error buried two levels deep in a nested group (alongside a
    budget error at the same depth) must still surface — the recursive
    masking guard must not treat "not top-level EngineBudgetError" as license
    to swallow it, nor mistake a nested all-budget subgroup for safety when a
    sibling leaf is real."""

    class _NestedMixedEngine(Engine):
        async def _run(self, run: Any, *a: Any, **kw: Any) -> Any:
            raise ExceptionGroup(
                "outer",
                [
                    ExceptionGroup(
                        "inner", [EngineBudgetError("out of budget"), ValueError("genuine bug")]
                    )
                ],
            )

    eng = _NestedMixedEngine(max_agents=1)
    with pytest.raises(BaseException) as exc_info:
        await eng.run()

    leaves = _leaves(exc_info.value)
    assert any(isinstance(e, ValueError) for e in leaves), (
        f"the genuine ValueError must surface from the nested group, got {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# 5. Back-compat: await engine.run(...) stays isinstance(str)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_back_compat_str_contract_across_prose_engines():
    """isinstance(await engine.run(x), str) and str(result) == result.text.

    Covers the four prose-terminal engines (Planning/Research/Review/
    Hypothesis). CodingEngine's public run() returns a structured
    CodeResultRecorded — a pre-existing, untouched contract (its overrides
    are out of scope here) — so it is intentionally not included in this
    str-contract check.
    """
    cases: list[tuple[Any, tuple[Any, ...]]] = [
        (PlanningEngine(), ("do the thing",)),
        (ResearchEngine(), ("a topic",)),
        (ReviewEngine(), ("some artifact",)),
        (HypothesisEngine(), ("a finding",)),
    ]
    for eng, args in cases:

        async def fake_run(run: Any, *a: Any, **kw: Any) -> str:
            return "PROSE RESULT"

        eng._run = fake_run  # type: ignore[method-assign]
        result = await eng.run(*args)
        assert isinstance(result, str), f"{type(eng).__name__}: expected str, got {type(result)}"
        assert result == "PROSE RESULT"
        assert str(result) == result.text


# ---------------------------------------------------------------------------
# 6. R5 — success path after a filtered discretionary budget raise still
#    flags degraded (anti-silent-truncation guard for edit 1 + edit 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r5_success_path_flags_degraded_after_filtered_expansion(monkeypatch):
    """A run that COMPLETES after wait_quiescence silently filtered a
    discretionary EngineBudgetError must still report degraded=True,
    degrade_reason='budget' — edit 1 alone would otherwise turn the crash
    into a permanent, undetectable silent truncation."""
    eng = ResearchEngine(max_agents=3, repair_retries=0)
    run = eng.new_run()
    monkeypatch.setattr(run, "make_agent", _budget_gated_make_agent(run))
    monkeypatch.setattr(eng, "new_run", lambda **kw: run)

    async def fake_run(run_obj: Any, topic: str) -> str:
        run_obj.root = topic
        # The root team consumes the entire budget (3 agents) with real
        # findings collected — deterministic, not spawned, so no race.
        team = await eng._team_for(run_obj, 0)
        await eng._drive_node(run_obj, team, "explore the root topic")
        # Discretionary expansion: budget is already exhausted, both must be
        # filtered as benign "expansion stopped", not crash the run.
        run_obj.spawn(eng._explore(run_obj, "deeper question one", 1))
        run_obj.spawn(eng._explore(run_obj, "deeper question two", 1))
        await run_obj.wait_quiescence()
        return await eng._synthesize(run_obj, topic)

    monkeypatch.setattr(eng, "_run", fake_run)

    result = await eng.run("root topic")

    assert isinstance(result, EngineResult), f"expected EngineResult, got {type(result)}"
    assert result.degraded is True
    assert result.degrade_reason == "budget"
    assert result.text, "expected non-empty synthesized text"
    assert not result.skipped or all(isinstance(s, str) for s in result.skipped)
