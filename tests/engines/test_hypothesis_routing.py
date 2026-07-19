# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""HypothesisEngine v2 — dedup gate routing, per-stage model/effort resolution,
recursion narrowing, filing queue, and loud total-failure. No LLM."""

from __future__ import annotations

import json

import pytest

import lionagi.engines.engine as engine_mod
from lionagi.engines.hypothesis import (
    ChainEvent,
    ConclusionDrawn,
    DedupChecked,
    FindingPosted,
    HypothesisEngine,
    QuestionRaised,
    _judge_bar,
    filing_queue,
)


@pytest.fixture(autouse=True)
def _no_local_profiles(monkeypatch):
    """Stage routing must not depend on this machine's agent profiles."""
    monkeypatch.setattr(engine_mod, "_ROLE_PROFILE_CACHE", {})
    monkeypatch.setattr(engine_mod, "role_profile_route", lambda role: (None, None))
    import lionagi.engines.hypothesis as hyp_mod

    monkeypatch.setattr(hyp_mod, "role_profile_route", lambda role: (None, None))


def _wire(eng, run):
    run.observe(ChainEvent, lambda e, _c: run.collect(e))
    run.observe(FindingPosted, lambda f, _c: eng._on_finding(run, f))
    run.observe(DedupChecked, lambda d, _c: eng._on_dedup(run, d))
    run.observe(QuestionRaised, lambda q, _c: eng._on_question(run, q))


def _mute(eng, *stages):
    calls: list[tuple] = []

    def make(stage):
        async def rec(_run, event, *a):
            calls.append((stage, event))

        return rec

    for s in stages:
        setattr(eng, f"_{s}", make(s))
    return calls


# ── Defaults and overrides ────────────────────────────────────────────────


def test_recursion_and_judge_defaults():
    eng = HypothesisEngine()
    assert eng.max_depth == 2
    assert eng.judge_model == HypothesisEngine.DEFAULT_JUDGE_MODEL


def test_explicit_overrides_beat_defaults():
    eng = HypothesisEngine(judge_model=None, max_depth=4)
    assert eng.judge_model is None
    assert eng.max_depth == 4


def test_stage_routing_falls_back_to_tables():
    eng = HypothesisEngine()
    for stage in HypothesisEngine.STAGE_MODEL_DEFAULTS:
        assert eng.model_for(stage) == HypothesisEngine.STAGE_MODEL_DEFAULTS[stage]
    assert eng.effort_for("conclude") == "xhigh"
    assert eng.effort_for("synthesize") is None


def test_engine_wide_model_beats_stage_table():
    eng = HypothesisEngine(model="claude/sonnet")
    assert eng.model_for("validate") == "claude/sonnet"
    # stage effort defaults still differentiate stages
    assert eng.effort_for("validate") == "high"


def test_stage_dict_beats_engine_wide():
    eng = HypothesisEngine(model="claude/sonnet", models={"validate": "codex/gpt-5.6-terra"})
    assert eng.model_for("validate") == "codex/gpt-5.6-terra"
    eng2 = HypothesisEngine(effort="low", efforts={"conclude": "xhigh"})
    assert eng2.effort_for("conclude") == "xhigh"
    assert eng2.effort_for("research") == "low"


def test_effort_suffix_in_model_suppresses_table_effort():
    eng = HypothesisEngine(model="codex/gpt-5.6-luna-high")
    assert eng.model_for("conclude") == "codex/gpt-5.6-luna-high"
    assert eng.effort_for("conclude") is None
    # explicit effort still wins over the suffix
    eng2 = HypothesisEngine(model="codex/gpt-5.6-luna-high", effort="medium")
    assert eng2.effort_for("conclude") == "medium"


def test_role_profile_layer_beats_table(monkeypatch):
    import lionagi.engines.hypothesis as hyp_mod

    monkeypatch.setattr(
        hyp_mod,
        "role_profile_route",
        lambda role: ("prov/model-x", "low") if role == "critic" else (None, None),
    )
    eng = HypothesisEngine()
    assert eng.model_for("conclude") == "prov/model-x"
    assert eng.effort_for("conclude") == "low"
    # explicit engine-wide still beats the profile
    eng2 = HypothesisEngine(model="claude/sonnet")
    assert eng2.model_for("conclude") == "claude/sonnet"


def test_question_cap_halves_per_cycle():
    eng = HypothesisEngine(max_questions=8)
    assert [eng.question_cap(g) for g in range(5)] == [8, 4, 2, 1, 1]


def test_judge_bar_escalates():
    assert _judge_bar(0) == ""
    assert "register decision" in _judge_bar(1)
    assert "correctness" in _judge_bar(2)
    assert "correctness" in _judge_bar(5)


# ── Dedup gate routing ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finding_routes_to_dedup_when_repo_set():
    eng = HypothesisEngine(dedup_repo="owner/repo")
    run = eng.new_run()
    calls = _mute(eng, "dedup", "extract")
    _wire(eng, run)
    await run.emit(FindingPosted(description="finding one"))
    await run.wait_quiescence()
    assert [s for s, _ in calls] == ["dedup"]


@pytest.mark.asyncio
async def test_finding_routes_to_extract_without_repo():
    eng = HypothesisEngine()
    run = eng.new_run()
    calls = _mute(eng, "dedup", "extract")
    _wire(eng, run)
    await run.emit(FindingPosted(description="finding one"))
    await run.wait_quiescence()
    assert [s for s, _ in calls] == ["extract"]


@pytest.mark.asyncio
async def test_duplicate_verdict_stops_extraction():
    eng = HypothesisEngine(dedup_repo="owner/repo")
    run = eng.new_run()
    calls = _mute(eng, "dedup", "extract")
    _wire(eng, run)
    events: list[dict] = []
    run.on_event = lambda e: events.append(e)
    f = run.collect(FindingPosted(description="already tracked"))
    await run.emit(
        DedupChecked(
            finding_ref=f.eid,
            verdict="duplicate",
            issue_number=42,
            issue_state="closed",
        )
    )
    await run.wait_quiescence()
    assert not [s for s, _ in calls if s == "extract"]
    dupes = [e for e in events if e["type"] == "finding_duplicate"]
    assert dupes and dupes[0]["issue"] == 42


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["new", "extends"])
async def test_new_and_extends_verdicts_proceed_to_extract(verdict):
    eng = HypothesisEngine(dedup_repo="owner/repo")
    run = eng.new_run()
    calls = _mute(eng, "dedup", "extract")
    _wire(eng, run)
    f = run.collect(FindingPosted(description="novel mechanism"))
    await run.emit(DedupChecked(finding_ref=f.eid, verdict=verdict))
    await run.wait_quiescence()
    assert [s for s, _ in calls] == ["extract"]
    assert f.eid in run.dedup_cleared


@pytest.mark.asyncio
async def test_second_dedup_verdict_does_not_double_extract():
    eng = HypothesisEngine(dedup_repo="owner/repo")
    run = eng.new_run()
    calls = _mute(eng, "dedup", "extract")
    _wire(eng, run)
    f = run.collect(FindingPosted(description="novel mechanism"))
    await run.emit(DedupChecked(finding_ref=f.eid, verdict="new"))
    await run.emit(DedupChecked(finding_ref=f.eid, verdict="extends", issue_number=7))
    await run.wait_quiescence()
    assert [s for s, _ in calls] == ["extract"]


@pytest.mark.asyncio
async def test_orphan_dedup_verdict_notifies():
    eng = HypothesisEngine(dedup_repo="owner/repo")
    run = eng.new_run()
    _mute(eng, "dedup", "extract")
    _wire(eng, run)
    events: list[dict] = []
    run.on_event = lambda e: events.append(e)
    await run.emit(DedupChecked(finding_ref="F-99", verdict="new"))
    await run.wait_quiescence()
    assert any(e["type"] == "dedup_orphan" for e in events)


# ── Filing queue ──────────────────────────────────────────────────────────


def _seed_certified_run(eng):
    run = eng.new_run()
    f = run.collect(FindingPosted(description="stale checkpoint overwrites newer segment"))
    run.collect(
        DedupChecked(
            finding_ref=f.eid,
            verdict="new",
            source_confirmed=True,
            rationale="no issue covers the publication ordering",
        )
    )
    f2 = run.collect(FindingPosted(description="already tracked elsewhere"))
    run.collect(
        DedupChecked(finding_ref=f2.eid, verdict="duplicate", issue_number=9, issue_state="open")
    )
    q = run.collect(QuestionRaised(area="storage", what_is_unknown="why MAX?", parent_ref=f.eid))
    run.collect(
        ConclusionDrawn(
            question_ref=q.eid,
            verdict="serialize per scope",
            rationale="race is reachable",
            basis="theoretical",
        )
    )
    return run, f


def test_filing_queue_contains_only_certified_findings():
    eng = HypothesisEngine(dedup_repo="owner/repo")
    run, f = _seed_certified_run(eng)
    queue = filing_queue(run)
    assert len(queue) == 1
    item = queue[0]
    assert item["finding_ref"] == f.eid
    assert item["verdict"] == "new"
    assert item["source_confirmed"] is True
    assert item["conclusions"] == ["C-1"]


def test_export_writes_filing_queue_and_report_section(tmp_path):
    eng = HypothesisEngine(dedup_repo="owner/repo")
    run, _ = _seed_certified_run(eng)
    paths = run.export(tmp_path)
    payload = json.loads((tmp_path / "chains.json").read_text())
    assert len(payload["filing_queue"]) == 1
    report = (tmp_path / "report.md").read_text()
    assert "Novelty verdicts" in report
    assert "Filing queue" in report
    assert "engine never files" in report


def test_no_dedup_stage_means_empty_filing_queue():
    eng = HypothesisEngine()
    run = eng.new_run()
    run.collect(FindingPosted(description="plain finding"))
    assert filing_queue(run) == []


# ── Loud total failure ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_stages_failing_raises_instead_of_empty_report():
    eng = HypothesisEngine(judge_model=None)

    async def boom(run, f):
        raise RuntimeError("no API key")

    eng._extract = boom
    with pytest.raises(RuntimeError, match="stage failure"):
        await eng.run("seed finding")


@pytest.mark.asyncio
async def test_partial_progress_still_synthesizes():
    eng = HypothesisEngine(judge_model=None)

    async def extract_ok(run, f):
        await run.emit(QuestionRaised(area="a", what_is_unknown="why X?", parent_ref=f.eid))

    async def research_boom(run, q):
        raise RuntimeError("transient")

    async def fake_synth(run):
        return "PARTIAL-REPORT"

    eng._extract = extract_ok
    eng._research = research_boom
    eng._synthesize = fake_synth
    out = await eng.run("seed finding")
    assert out == "PARTIAL-REPORT"
