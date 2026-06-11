# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""HypothesisEngine reaction logic — eid stamping, stage spawning, cycle caps,
pending experiments, chain tracing, synthesis. No LLM."""

from __future__ import annotations

import pytest

from lionagi.engines.hypothesis import (
    ApplicationMapped,
    ChainEvent,
    ConclusionDrawn,
    EvidenceCollected,
    ExperimentDesigned,
    FindingPosted,
    HypothesisEngine,
    HypothesisFormed,
    QuestionRaised,
    ResultRecorded,
    trace_chains,
)


def _wire(eng, run):
    """Register the engine's collector + reactions on a run, as ``_run`` does."""
    run.observe(ChainEvent, lambda e, _c: run.collect(e))
    run.observe(FindingPosted, lambda f, _c: eng._on_finding(run, f))
    run.observe(QuestionRaised, lambda q, _c: eng._on_question(run, q))
    run.observe(HypothesisFormed, lambda h, _c: eng._on_hypothesis(run, h))
    run.observe(ExperimentDesigned, lambda x, _c: eng._on_experiment(run, x))
    run.observe(ResultRecorded, lambda r, _c: eng._on_result(run, r))
    run.observe(ConclusionDrawn, lambda c, _c: eng._on_conclusion(run, c))


def _mute(eng, *stages):
    """Replace stage coroutines with recorders; returns the call log."""
    calls: list[tuple] = []

    def make(stage):
        async def rec(_run, event, *a):
            calls.append((stage, event))

        return rec

    for s in stages:
        setattr(eng, f"_{s}", make(s))
    return calls


@pytest.mark.asyncio
async def test_collector_stamps_sequential_eids_and_stores():
    eng = HypothesisEngine()
    run = eng.new_run()
    _mute(eng, "extract", "research")
    _wire(eng, run)
    await run.emit(FindingPosted(description="finding one"))
    await run.emit(QuestionRaised(area="x", what_is_unknown="why A over B?"))
    await run.emit(QuestionRaised(area="x", what_is_unknown="why C over D?"))
    await run.wait_quiescence()
    assert [f.eid for f in run.events_of(FindingPosted)] == ["F-1"]
    assert [q.eid for q in run.events_of(QuestionRaised)] == ["Q-1", "Q-2"]
    assert run.find("Q-2").what_is_unknown == "why C over D?"


@pytest.mark.asyncio
async def test_finding_spawns_extraction_and_dedups():
    eng = HypothesisEngine()
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)
    await run.emit(FindingPosted(description="CSR chosen for BFS"))
    await run.emit(FindingPosted(description="csr chosen for bfs"))  # dup after normalize
    await run.wait_quiescence()
    assert [s for s, _ in calls] == ["extract"]


@pytest.mark.asyncio
async def test_cycle_cap_blocks_question_expansion():
    eng = HypothesisEngine(max_depth=2)
    run = eng.new_run()
    calls = _mute(eng, "research")
    _wire(eng, run)
    await run.emit(QuestionRaised(area="a", what_is_unknown="in budget?", gen=2))
    await run.emit(QuestionRaised(area="a", what_is_unknown="over budget?", gen=3))
    await run.wait_quiescence()
    assert [e.what_is_unknown for _, e in calls] == ["in budget?"]


@pytest.mark.asyncio
async def test_question_dedup_spawns_once():
    eng = HypothesisEngine()
    run = eng.new_run()
    calls = _mute(eng, "research")
    _wire(eng, run)
    await run.emit(QuestionRaised(area="a", what_is_unknown="Why HNSW over Vamana?"))
    await run.emit(QuestionRaised(area="b", what_is_unknown="why hnsw over vamana?"))
    await run.wait_quiescence()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_benchmark_experiment_goes_pending_analysis_runs():
    eng = HypothesisEngine()
    run = eng.new_run()
    calls = _mute(eng, "validate")
    _wire(eng, run)
    await run.emit(
        ExperimentDesigned(hypothesis_ref="H-1", method="benchmark", procedure="run criterion")
    )
    await run.emit(
        ExperimentDesigned(hypothesis_ref="H-2", method="analysis", procedure="count AST nodes")
    )
    await run.wait_quiescence()
    assert [x.method for x in run.pending] == ["benchmark"]
    assert [e.method for _, e in calls] == ["analysis"]


@pytest.mark.asyncio
async def test_result_spawns_conclude_and_conclusion_spawns_apply():
    eng = HypothesisEngine()
    run = eng.new_run()
    calls = _mute(eng, "conclude", "apply")
    _wire(eng, run)
    await run.emit(ResultRecorded(experiment_ref="X-1", measurements="180us vs 4.2ms"))
    await run.emit(
        ConclusionDrawn(
            question_ref="Q-1",
            verdict="keep CSR",
            rationale="23x faster",
            basis="empirical",
        )
    )
    await run.wait_quiescence()
    assert sorted(s for s, _ in calls) == ["apply", "conclude"]


@pytest.mark.asyncio
async def test_taste_conclusion_without_result_still_applies():
    eng = HypothesisEngine()
    run = eng.new_run()
    calls = _mute(eng, "apply")
    _wire(eng, run)
    await run.emit(
        ConclusionDrawn(
            question_ref="Q-1",
            result_ref="",
            verdict="keep || for concat",
            rationale="SQL convention",
            basis="taste",
        )
    )
    await run.wait_quiescence()
    assert [s for s, _ in calls] == ["apply"]
    assert run.events_of(ConclusionDrawn)[0].basis == "taste"


@pytest.mark.asyncio
async def test_stage_error_does_not_kill_pipeline():
    eng = HypothesisEngine()
    run = eng.new_run()
    errors: list[dict] = []
    run.on_event = lambda e: errors.append(e) if e["type"] == "stage_error" else None

    async def boom(_run, _q):
        raise RuntimeError("research exploded")

    eng._research = boom
    _wire(eng, run)
    await run.emit(QuestionRaised(area="a", what_is_unknown="why X?"))
    await run.wait_quiescence()  # must not raise
    assert errors and errors[0]["stage"] == "research"


def test_trace_chains_reconstructs_full_chain_and_taste_path():
    f = FindingPosted(eid="F-1", description="CSR chosen")
    q = QuestionRaised(eid="Q-1", area="graph", what_is_unknown="why CSR?", parent_ref="F-1")
    h = HypothesisFormed(eid="H-1", question_ref="Q-1", statement="CSR < 500us")
    x = ExperimentDesigned(eid="X-1", hypothesis_ref="H-1", method="analysis", procedure="bench")
    r = ResultRecorded(eid="R-1", experiment_ref="X-1", measurements="180us", passed=True)
    c = ConclusionDrawn(
        eid="C-1",
        question_ref="Q-1",
        result_ref="R-1",
        verdict="keep",
        rationale="fast",
        basis="empirical",
    )
    a = ApplicationMapped(eid="A-1", conclusion_ref="C-1", decision_ref="D-012", effect="supports")
    # taste path: conclusion straight off a question, no application yet
    q2 = QuestionRaised(eid="Q-2", area="ql", what_is_unknown="why ||?", parent_ref="F-1")
    c2 = ConclusionDrawn(
        eid="C-2", question_ref="Q-2", verdict="keep ||", rationale="convention", basis="taste"
    )

    chains = trace_chains([f, q, h, x, r, c, a, q2, c2])
    assert len(chains) == 2
    full = next(ch for ch in chains if ch[-1].eid == "A-1")
    assert [e.eid for e in full] == ["F-1", "Q-1", "H-1", "X-1", "R-1", "C-1", "A-1"]
    taste = next(ch for ch in chains if ch[-1].eid == "C-2")
    assert [e.eid for e in taste] == ["F-1", "Q-2", "C-2"]


@pytest.mark.asyncio
async def test_synthesis_reports_chains_pending_and_open_questions():
    eng = HypothesisEngine()
    run = eng.new_run()
    run.collect(QuestionRaised(area="a", what_is_unknown="why HNSW over Vamana at 100K?"))
    run.collect(
        ConclusionDrawn(
            question_ref="Q-9",  # no matching question -> Q-1 stays open
            verdict="keep",
            rationale="r",
            basis="quantitative",
        )
    )
    pending = run.collect(
        ExperimentDesigned(hypothesis_ref="H-1", method="benchmark", procedure="criterion 100K")
    )
    run.pending.append(pending)

    captured: dict = {}

    class FakeSynth:
        name = "synthesizer"

        async def operate(self, *, instruction):
            captured["instruction"] = instruction
            return "REPORT"

    async def fake_make(role, **kw):
        return FakeSynth()

    run.make_agent = fake_make
    out = await eng._synthesize(run)
    assert out == "REPORT"
    text = captured["instruction"]
    assert "why HNSW over Vamana at 100K?" in text  # open question listed
    assert "criterion 100K" in text  # pending experiment listed
    assert "quantitative" in text  # conclusion basis listed


@pytest.mark.asyncio
async def test_run_seeds_findings_and_synthesizes(monkeypatch):
    eng = HypothesisEngine()
    seen: list[str] = []

    async def fake_extract(run, f):
        seen.append(f.description)

    async def fake_synth(run):
        return "DONE"

    eng._extract = fake_extract
    eng._synthesize = fake_synth
    out = await eng.run(["alpha choice", "beta choice"], decisions="D-001: something")
    assert out == "DONE"
    assert seen == ["alpha choice", "beta choice"]


@pytest.mark.asyncio
async def test_empty_findings_raises():
    eng = HypothesisEngine()
    with pytest.raises(ValueError):
        await eng.run("   ")


# ---------------------------------------------------------------------------
# Regression: #1361 — chain events must reach on_event exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_events_reach_on_event_exactly_once_no_double_delivery():
    """Regression for #1361: every chain event collected by HypothesisRun must
    reach on_event exactly once — regardless of whether it arrived via run.emit()
    (seed FindingPosted) or the session bus (agent-emitted Q/E/H/etc.).

    Contract: set(eids delivered to on_event) == set(eids in run store)
    and no eid is delivered twice."""
    eng = HypothesisEngine()
    run = eng.new_run()
    # Mute all reactive stages so we control exactly which events land
    _mute(eng, "extract", "research", "hypothesize", "design", "validate", "conclude", "apply")
    _wire(eng, run)

    delivered_types: list[str] = []
    run.on_event = lambda e: delivered_types.append(e["type"])

    # Emit a mix via run.emit() (seed path) and session bus (agent path)
    await run.emit(FindingPosted(description="seed finding"))  # run.emit path
    await run.session.emit(QuestionRaised(area="a", what_is_unknown="why X over Y?"))  # bus path
    await run.session.emit(EvidenceCollected(question_ref="Q-1", description="ev", kind="analysis"))
    await run.session.emit(
        HypothesisFormed(question_ref="Q-1", statement="X is faster", metric="ms")
    )
    await run.wait_quiescence()

    # Every stored eid must appear in the delivered stream exactly once
    stored_eids = {e.eid for evs in run.store.values() for e in evs if e.eid}
    chain_types_seen = {
        e["type"]
        for e in [{"type": t} for t in delivered_types]
        if e["type"]
        in {
            "FindingPosted",
            "QuestionRaised",
            "EvidenceCollected",
            "HypothesisFormed",
            "ExperimentDesigned",
            "ResultRecorded",
            "ConclusionDrawn",
            "ApplicationMapped",
        }
    }
    expected_types = {"FindingPosted", "QuestionRaised", "EvidenceCollected", "HypothesisFormed"}
    assert expected_types <= chain_types_seen, (
        f"Missing from on_event stream: {expected_types - chain_types_seen}"
    )

    # No double delivery: each chain event type should appear exactly once here
    # (we emitted exactly one of each)
    from collections import Counter

    counts = Counter(delivered_types)
    doubled = [k for k, v in counts.items() if k in expected_types and v > 1]
    assert not doubled, f"Double-delivered chain event types: {doubled}"

    # The stored eids match what was stamped (sanity check)
    assert len(stored_eids) == 4, f"Expected 4 chain events stored, got {len(stored_eids)}"


@pytest.mark.asyncio
async def test_seed_finding_no_double_delivery_via_run_emit():
    """Seed FindingPosted goes through run.emit(); the emit() override must not
    cause a second on_event call for the same event (regression: collect()
    notifies once, emit() override skips its own call for ChainEvent)."""
    eng = HypothesisEngine()
    run = eng.new_run()
    _mute(eng, "extract")
    _wire(eng, run)

    delivered: list[dict] = []
    run.on_event = lambda e: delivered.append(e)

    await run.emit(FindingPosted(description="only seed"))
    await run.wait_quiescence()

    fp_events = [e for e in delivered if e["type"] == "FindingPosted"]
    assert len(fp_events) == 1, f"Expected 1 FindingPosted delivery, got {len(fp_events)}"
