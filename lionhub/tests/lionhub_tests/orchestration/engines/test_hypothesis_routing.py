# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""HypothesisEngine v2 — dedup gate routing, per-stage model/effort resolution,
recursion narrowing, filing queue, and loud total-failure. No LLM."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pydantic
import pytest

import lionhub.orchestration.engines.engine as engine_mod
import lionhub.orchestration.engines.hypothesis as hyp_mod
from lionagi import Branch
from lionagi._errors import ContainmentRefusedError
from lionhub.orchestration.engines.hypothesis import (
    ChainEvent,
    ConclusionDrawn,
    DedupChecked,
    ExperimentDesigned,
    FindingPosted,
    HypothesisEngine,
    HypothesisFormed,
    QuestionRaised,
    ResultRecorded,
    _judge_bar,
    filing_queue,
)


@pytest.fixture(autouse=True)
def _no_local_profiles(monkeypatch):
    """Stage routing must not depend on this machine's agent profiles."""
    monkeypatch.setattr(engine_mod, "_ROLE_PROFILE_CACHE", {})
    monkeypatch.setattr(engine_mod, "role_profile_route", lambda role: (None, None))
    import lionhub.orchestration.engines.hypothesis as hyp_mod

    monkeypatch.setattr(hyp_mod, "role_profile_route", lambda role: (None, None))


def _wire(eng, run):
    run.observe(ChainEvent, lambda e, _c: run._collect_emitted(e))
    run.observe(FindingPosted, lambda f, _c: eng._on_finding(run, f))
    run.observe(run._agent_events(DedupChecked), lambda d, _c: eng._on_dedup(run, d))
    run.observe(QuestionRaised, lambda q, _c: eng._on_question(run, q))


async def _search_verdict(run, verdict):
    """Run the real search stage, replacing only its provider-facing calls."""
    finding = run.find(verdict.finding_ref)
    previous = run._dedup_agents.get(finding.eid)
    branch = previous[1] if previous is not None else run.session.new_branch(name="search")

    async def make_agent(*_args, **_kwargs):
        return branch

    async def operate(agent, *_args, **_kwargs):
        assert agent is branch
        await agent.emit(verdict)

    with (
        patch.object(run, "make_agent", make_agent),
        patch.object(run, "operate_with_repair", operate),
    ):
        await HypothesisEngine._dedup_search(run.engine, run, finding)


def _mute(eng, *stages):
    calls: list[tuple] = []

    def make(stage):
        async def rec(_run, event, *a):
            calls.append((stage, event))

        return rec

    for s in stages:
        setattr(eng, f"_{s}", make(s))
    return calls


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
    import lionhub.orchestration.engines.hypothesis as hyp_mod

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
    await _search_verdict(
        run,
        DedupChecked(
            finding_ref=f.eid,
            verdict="duplicate",
            issue_number=42,
            issue_state="closed",
        ),
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
    await _search_verdict(run, DedupChecked(finding_ref=f.eid, verdict=verdict))
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
    await _search_verdict(run, DedupChecked(finding_ref=f.eid, verdict="new"))
    await _search_verdict(run, DedupChecked(finding_ref=f.eid, verdict="extends", issue_number=7))
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


@pytest.mark.asyncio
async def test_dedup_stage_exception_releases_finding_to_extract():
    """An exception raised inside the dedup leg (agent construction, timeout,
    ...) must still route the finding to extraction, exactly like a dedup
    leg that ran but never emitted a verdict."""
    eng = HypothesisEngine(dedup_repo="owner/repo", judge_model=None)
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)

    async def boom_dedup(_run, _f):
        raise TimeoutError("agent construction timed out")

    eng._dedup_search = boom_dedup

    events: list[dict] = []
    run.on_event = lambda e: events.append(e)

    await run.emit(FindingPosted(description="dedup leg dies mid-construction"))
    await run.wait_quiescence()

    assert [s for s, _ in calls] == ["extract"]
    assert any(e["type"] == "dedup_missing" for e in events)
    assert any(e["type"] == "stage_error" and e["stage"] == "dedup" for e in events)


@pytest.mark.asyncio
async def test_a_tracker_search_that_answers_nothing_releases_the_finding_once():
    """A search agent that runs and posts no verdict releases the finding to extraction,
    once, and records that the verdict is missing."""
    eng = HypothesisEngine(dedup_repo="owner/repo", judge_model=None)
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)
    built: list[_StubAgent] = []

    async def make_agent(role, *, name=None, **kwargs):
        agent = _StubAgent(name, None)
        run.session.include_branches(agent)
        built.append(agent)
        return agent

    operated: list[_StubAgent] = []

    async def operate_with_repair(agent, *args, **kwargs):
        operated.append(agent)
        return None

    run.make_agent = make_agent
    run.operate_with_repair = operate_with_repair
    events: list[dict] = []
    run.on_event = lambda e: events.append(e)

    await run.emit(FindingPosted(description="the search finds nothing to say"))
    await run.wait_quiescence()

    assert [agent.name.split("-")[0] for agent in built] == ["dedup"]
    assert len(operated) == len(built) == 1
    assert operated[0] is built[0]
    assert [s for s, _ in calls] == ["extract"]
    assert sum(e["type"] == "dedup_missing" for e in events) == 1
    assert not any(e["type"] == "stage_error" for e in events)


@pytest.mark.asyncio
async def test_dedup_stage_exception_does_not_double_release_with_late_verdict():
    """A dedup leg that raises after already emitting a verdict must not
    double-spawn extraction."""
    eng = HypothesisEngine(dedup_repo="owner/repo", judge_model=None)
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)

    async def dedup_then_boom(run, f):
        await _search_verdict(run, DedupChecked(finding_ref=f.eid, verdict="new"))
        raise TimeoutError("post-verdict cleanup failed")

    eng._dedup_search = dedup_then_boom

    await run.emit(FindingPosted(description="verdict lands then leg still errors"))
    await run.wait_quiescence()

    assert [s for s, _ in calls] == ["extract"]


class _StubAgent(Branch):
    def __init__(self, name, judge_error):
        super().__init__(name=name)
        object.__setattr__(self, "_judge_error", judge_error)

    async def operate(self, **kwargs):
        if self._judge_error is not None and self.name.startswith("judge-"):
            raise self._judge_error
        return "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contained", "judge_error", "searched"),
    [
        (True, RuntimeError("provider failed"), False),
        (True, ContainmentRefusedError("codex config selects a profile"), False),
        (False, RuntimeError("provider failed"), True),
    ],
    ids=["contained-provider-error", "contained-refusal", "uncontained-fails-open"],
)
async def test_a_judge_that_raises_in_the_dedup_stage_ends_the_finding(
    contained, judge_error, searched
):
    """A contained judge raises any error, and the finding stops there: neither the
    tracker search nor extraction runs, since extraction skips the judge for a finding
    the dedup stage released. Without containment the judge fails open (control)."""
    eng = HypothesisEngine(
        dedup_repo="owner/repo",
        judge_model="codex/gpt-5.5",
        contain_account_integrations=contained,
        max_depth=1,
    )
    run = eng.new_run()
    _wire(eng, run)
    built: list[str] = []

    async def make_agent(role, *, name=None, **kwargs):
        built.append(name)
        agent = _StubAgent(name, judge_error)
        run.session.include_branches(agent)
        return agent

    async def operate_with_repair(*args, **kwargs):
        return None

    run.make_agent = make_agent
    run.operate_with_repair = operate_with_repair
    events: list[dict] = []
    run.on_event = lambda e: events.append(e)

    q = run.collect(QuestionRaised(area="a", what_is_unknown="why X?", gen=0))
    h = run.collect(HypothesisFormed(question_ref=q.eid, statement="X < 500us"))
    x = run.collect(ExperimentDesigned(hypothesis_ref=h.eid, method="analysis", procedure="bench"))
    await run.emit(FindingPosted(description="a finding from validation", parent_ref=x.eid))
    await run.wait_quiescence()

    f = run.events_of(FindingPosted)[-1]
    assert f.gen == 1
    stages = sorted({name.split("-")[0] for name in built})
    if searched:
        assert stages == ["dedup", "extract", "judge"]
    else:
        assert stages == ["judge"]
        assert f.eid not in run.dedup_cleared
        assert not any(e["type"] == "dedup_missing" for e in events)
        assert any(e["type"] == "stage_error" and e["stage"] == "dedup" for e in events)


def _agent_sourced_finding_chain(run):
    q = run.collect(QuestionRaised(area="a", what_is_unknown="why X?", gen=0))
    h = run.collect(HypothesisFormed(question_ref=q.eid, statement="X < 500us"))
    return run.collect(ExperimentDesigned(hypothesis_ref=h.eid, method="analysis", procedure="b"))


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["new", "extends", "duplicate"])
async def test_a_verdict_naming_a_finding_its_judge_refused_is_an_orphan(verdict):
    """A dedup agent can name any finding in its verdict. A finding whose judge refused
    it had no search sent, so a verdict naming it neither extracts it nor marks it."""
    eng = HypothesisEngine(dedup_repo="owner/repo", max_depth=1)
    run = eng.new_run()
    calls = _mute(eng, "extract", "dedup_search")
    _wire(eng, run)
    judged: list[str] = []

    async def judge(_run, eid, _subject):
        judged.append(eid)
        return False

    eng.judge = judge
    events: list[dict] = []
    run.on_event = lambda e: events.append(e)
    x = _agent_sourced_finding_chain(run)
    await run.emit(FindingPosted(description="junk", parent_ref=x.eid))
    await run.wait_quiescence()
    f = run.events_of(FindingPosted)[-1]
    assert f.gen == 1

    await run.emit(DedupChecked(finding_ref=f.eid, verdict=verdict, issue_number=3))
    await run.wait_quiescence()

    assert judged == [f.eid]
    assert calls == []
    assert f.eid not in run.dedup_cleared
    assert any(e["type"] == "dedup_orphan" and e["finding_ref"] == f.eid for e in events)
    assert not any(e["type"] == "finding_duplicate" for e in events)


@pytest.mark.asyncio
async def test_a_finding_its_judge_passed_is_extracted_without_a_second_judge():
    eng = HypothesisEngine(dedup_repo="owner/repo", max_depth=1)
    run = eng.new_run()
    _wire(eng, run)
    judged: list[str] = []

    async def judge(_run, eid, _subject):
        judged.append(eid)
        return True

    async def search(_run, f):
        await _search_verdict(_run, DedupChecked(finding_ref=f.eid, verdict="new"))

    extracted: list[str] = []

    async def make_agent(role, *, name=None, **kwargs):
        extracted.append(name)
        agent = _StubAgent(name, None)
        run.session.include_branches(agent)
        return agent

    async def operate_with_repair(*args, **kwargs):
        return None

    eng.judge = judge
    eng._dedup_search = search
    run.make_agent = make_agent
    run.operate_with_repair = operate_with_repair
    x = _agent_sourced_finding_chain(run)
    await run.emit(FindingPosted(description="a real finding", parent_ref=x.eid))
    await run.wait_quiescence()
    f = run.events_of(FindingPosted)[-1]

    assert judged == [f.eid]
    assert extracted == [f"extract-{f.eid}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["new", "extends"])
async def test_a_refused_finding_named_by_a_verdict_stays_out_of_the_filing_queue(verdict):
    eng = HypothesisEngine(dedup_repo="owner/repo", max_depth=1)
    run = eng.new_run()
    _mute(eng, "extract", "dedup_search")
    _wire(eng, run)

    async def judge(_run, eid, _subject):
        return False

    eng.judge = judge
    x = _agent_sourced_finding_chain(run)
    await run.emit(FindingPosted(description="junk", parent_ref=x.eid))
    await run.wait_quiescence()
    f = run.events_of(FindingPosted)[-1]
    await run.emit(DedupChecked(finding_ref=f.eid, verdict=verdict))
    await run.wait_quiescence()

    assert filing_queue(run) == []
    report = hyp_mod.render_evidence(run)
    assert f"{f.eid}: {verdict} (not accepted)" in report
    assert "Filing queue" not in report


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["new", "duplicate"])
async def test_a_verdict_while_the_judge_decides_does_not_answer_the_search(verdict):
    """The early verdict is an orphan. Once the judge passes, the finding's own search
    posts nothing, so the finding is released and extracted."""
    gate = asyncio.Event()
    eng = HypothesisEngine(dedup_repo="owner/repo", max_depth=1)
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)

    async def judge(_run, eid, _subject):
        await gate.wait()
        return True

    async def make_agent(role, *, name=None, **kwargs):
        agent = _StubAgent(name, None)
        run.session.include_branches(agent)
        return agent

    async def operate_with_repair(*args, **kwargs):
        return None

    eng.judge = judge
    run.make_agent = make_agent
    run.operate_with_repair = operate_with_repair
    events: list[dict] = []
    run.on_event = lambda e: events.append(e)
    x = _agent_sourced_finding_chain(run)
    await run.emit(FindingPosted(description="agent claim", parent_ref=x.eid))
    f = run.events_of(FindingPosted)[-1]
    await asyncio.sleep(0.01)
    await run.emit(DedupChecked(finding_ref=f.eid, verdict=verdict, issue_number=3))
    gate.set()
    await run.wait_quiescence()

    assert [e["type"] for e in events if e["type"] == "dedup_orphan"] == ["dedup_orphan"]
    assert [s for s, _ in calls] == ["extract"]
    assert any(e["type"] == "dedup_missing" and e["eid"] == f.eid for e in events)
    assert f.eid not in run.dedup_verdicts


@pytest.mark.asyncio
async def test_a_later_verdict_does_not_reopen_a_duplicate():
    eng = HypothesisEngine(dedup_repo="owner/repo")
    run = eng.new_run()
    calls = _mute(eng, "dedup", "extract")
    _wire(eng, run)
    f = run.collect(FindingPosted(description="already tracked"))
    await _search_verdict(
        run,
        DedupChecked(finding_ref=f.eid, verdict="duplicate", issue_number=9, issue_state="open"),
    )
    await _search_verdict(run, DedupChecked(finding_ref=f.eid, verdict="new"))
    await run.wait_quiescence()

    assert calls == []
    assert run.dedup_verdicts[f.eid].verdict == "duplicate"
    assert filing_queue(run) == []


@pytest.mark.asyncio
async def test_a_search_that_records_a_duplicate_and_then_fails_does_not_release_the_finding():
    eng = HypothesisEngine(dedup_repo="owner/repo", judge_model=None)
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)

    async def make_agent(role, *, name=None, **kwargs):
        agent = _StubAgent(name, None)
        run.session.include_branches(agent)
        return agent

    async def operate_with_repair(agent, *args, **kwargs):
        f = run.events_of(FindingPosted)[-1]
        await agent.emit(
            DedupChecked(finding_ref=f.eid, verdict="duplicate", issue_number=9, issue_state="open")
        )
        raise TimeoutError("timed out after the verdict")

    run.make_agent = make_agent
    run.operate_with_repair = operate_with_repair
    events: list[dict] = []
    run.on_event = lambda e: events.append(e)
    await run.emit(FindingPosted(description="already tracked"))
    await run.wait_quiescence()

    assert calls == []
    assert not any(e["type"] == "dedup_missing" for e in events)
    assert any(e["type"] == "stage_error" and e["stage"] == "dedup" for e in events)
    assert run.dedup_verdicts[run.events_of(FindingPosted)[-1].eid].verdict == "duplicate"


def test_a_collected_seed_with_two_certifying_verdicts_is_queued_once():
    eng = HypothesisEngine(dedup_repo="owner/repo")
    run = eng.new_run()
    f = run.collect(FindingPosted(description="stale checkpoint overwrites newer segment"))
    run.collect(DedupChecked(finding_ref=f.eid, verdict="new"))
    run.collect(
        DedupChecked(finding_ref=f.eid, verdict="extends", issue_number=7, issue_state="open")
    )

    assert [(i["finding_ref"], i["verdict"]) for i in filing_queue(run)] == [(f.eid, "new")]
    report = hyp_mod.render_evidence(run)
    assert f"{f.eid}: extends #7 (open) (not accepted)" in report
    assert f"{f.eid}: new (not accepted)" not in report


def test_a_collected_duplicate_then_new_queues_nothing_and_marks_the_new():
    eng = HypothesisEngine(dedup_repo="owner/repo")
    run = eng.new_run()
    f = run.collect(FindingPosted(description="already tracked, then reported as novel"))
    run.collect(
        DedupChecked(finding_ref=f.eid, verdict="duplicate", issue_number=9, issue_state="open")
    )
    run.collect(DedupChecked(finding_ref=f.eid, verdict="new"))

    assert filing_queue(run) == []
    report = hyp_mod.render_evidence(run)
    assert f"{f.eid}: new (not accepted)" in report
    assert f"{f.eid}: duplicate #9 (open) (not accepted)" not in report


async def _released_on_an_empty_search(eng):
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)

    async def make_agent(role, *, name=None, **kwargs):
        agent = _StubAgent(name, None)
        run.session.include_branches(agent)
        return agent

    async def operate_with_repair(*args, **kwargs):
        return None

    run.make_agent = make_agent
    run.operate_with_repair = operate_with_repair
    await run.emit(FindingPosted(description="released on an empty search"))
    await run.wait_quiescence()
    f = run.events_of(FindingPosted)[-1]
    # The search posted nothing, so the finding was released: extracted once, no verdict yet.
    assert [stage for stage, _ in calls] == ["extract"]
    assert f.eid in run.dedup_cleared
    assert f.eid not in run.dedup_verdicts
    return run, calls, f


@pytest.mark.asyncio
async def test_a_duplicate_after_a_release_is_reported_and_extracts_nothing_more():
    eng = HypothesisEngine(dedup_repo="owner/repo", judge_model=None)
    run, calls, f = await _released_on_an_empty_search(eng)
    events: list[dict] = []
    run.on_event = events.append
    await _search_verdict(
        run,
        DedupChecked(finding_ref=f.eid, verdict="duplicate", issue_number=9, issue_state="open"),
    )
    await run.wait_quiescence()

    assert [stage for stage, _ in calls] == ["extract"]
    assert run.dedup_verdicts[f.eid].verdict == "duplicate"
    assert any(e["type"] == "finding_duplicate" for e in events)
    report = hyp_mod.render_evidence(run)
    assert f"{f.eid}: duplicate #9 (open)" in report
    assert "(not accepted)" not in report
    assert filing_queue(run) == []


@pytest.mark.asyncio
async def test_a_new_after_a_release_reaches_the_filing_queue_once():
    eng = HypothesisEngine(dedup_repo="owner/repo", judge_model=None)
    run, calls, f = await _released_on_an_empty_search(eng)
    await _search_verdict(
        run, DedupChecked(finding_ref=f.eid, verdict="new", source_confirmed=True)
    )
    await run.wait_quiescence()

    assert [stage for stage, _ in calls] == ["extract"]
    assert run.dedup_verdicts[f.eid].verdict == "new"
    assert [(i["finding_ref"], i["verdict"]) for i in filing_queue(run)] == [(f.eid, "new")]


@pytest.mark.asyncio
async def test_a_total_failure_with_only_an_orphan_verdict_still_raises():
    eng = HypothesisEngine(dedup_repo="owner/repo", judge_model=None)

    async def search(run, f):
        await run.emit(DedupChecked(finding_ref="F-9", verdict="new"))
        raise TimeoutError("search timed out")

    async def extract_boom(run, f):
        raise RuntimeError("no API key")

    async def fake_synth(run):
        return "REPORT"

    eng._dedup_search = search
    eng._extract = extract_boom
    eng._synthesize = fake_synth
    with pytest.raises(RuntimeError, match="produced no events"):
        await eng.run("seed finding")


@pytest.mark.parametrize("bad_verdict", ["uncertain", "Duplicate", "dupe", "NEW", ""])
def test_dedup_checked_rejects_non_literal_verdicts(bad_verdict):
    with pytest.raises(pydantic.ValidationError):
        DedupChecked(finding_ref="F-1", verdict=bad_verdict)


@pytest.mark.parametrize("good_verdict", ["new", "duplicate", "extends"])
def test_dedup_checked_accepts_exact_literal_verdicts(good_verdict):
    d = DedupChecked(finding_ref="F-1", verdict=good_verdict)
    assert d.verdict == good_verdict


@pytest.mark.asyncio
async def test_findings_with_same_description_different_evidence_both_survive():
    eng = HypothesisEngine()
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)
    await run.emit(FindingPosted(description="CSR chosen for BFS", evidence="benchmark A"))
    await run.emit(FindingPosted(description="CSR chosen for BFS", evidence="benchmark B"))
    await run.wait_quiescence()
    assert len(calls) == 2  # distinct citations must not collapse into one


@pytest.mark.asyncio
async def test_reworded_duplicate_finding_still_reaches_dedup_stage():
    """A true reworded duplicate is not the local idempotence guard's job —
    it must still reach the (mocked here) dedup stage rather than being
    silently dropped before any audit record exists."""
    eng = HypothesisEngine(dedup_repo="owner/repo")
    run = eng.new_run()
    calls = _mute(eng, "dedup")
    _wire(eng, run)
    await run.emit(FindingPosted(description="CSR chosen for BFS over adjacency lists"))
    await run.emit(FindingPosted(description="csr picked for bfs instead of adjacency list"))
    await run.wait_quiescence()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_same_description_and_evidence_exact_repeat_still_dedups_locally():
    eng = HypothesisEngine()
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)
    await run.emit(FindingPosted(description="CSR chosen for BFS", evidence="benchmark A"))
    await run.emit(FindingPosted(description="CSR chosen for BFS", evidence="benchmark A"))
    await run.wait_quiescence()
    assert len(calls) == 1  # a byte-identical repeat is still a local no-op


@pytest.mark.asyncio
async def test_finding_gen_omitted_by_model_is_derived_from_validation_parent():
    """A FindingPosted from validation must get its gen from the traced
    question generation (via experiment -> hypothesis -> question), not from
    whatever (or nothing) the model put in the gen field."""
    eng = HypothesisEngine(max_depth=1)
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)

    q = run.collect(QuestionRaised(area="a", what_is_unknown="why X?", gen=0))
    h = run.collect(HypothesisFormed(question_ref=q.eid, statement="X < 500us"))
    x = run.collect(ExperimentDesigned(hypothesis_ref=h.eid, method="analysis", procedure="bench"))

    # Model omits gen entirely (defaults to 0) despite the true cycle being 1.
    await run.emit(FindingPosted(description="new fact from validation", parent_ref=x.eid))
    await run.wait_quiescence()

    posted = run.events_of(FindingPosted)[-1]
    assert posted.gen == 1  # derived: q.gen(0) + 1, not the omitted/defaulted 0
    assert len(calls) == 1  # 1 <= max_depth(1): admitted at the inclusive boundary


@pytest.mark.asyncio
async def test_finding_gen_forged_low_by_model_still_capped_by_derived_value():
    """max_depth=0: a forged/stale gen=0 on a validation-route finding must
    not bypass the cap — the true derived generation (1) is what's checked."""
    eng = HypothesisEngine(max_depth=0)
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)

    q = run.collect(QuestionRaised(area="a", what_is_unknown="why X?", gen=0))
    h = run.collect(HypothesisFormed(question_ref=q.eid, statement="X < 500us"))
    x = run.collect(ExperimentDesigned(hypothesis_ref=h.eid, method="analysis", procedure="bench"))

    events: list[dict] = []
    run.on_event = lambda e: events.append(e)
    # Forged: model claims gen=0 (would pass max_depth=0) though the real
    # parent chain puts this at gen 1.
    await run.emit(FindingPosted(description="forged-low gen", parent_ref=x.eid, gen=0))
    await run.wait_quiescence()

    assert calls == []  # never reached extraction
    assert any(e["type"] == "cycle_capped" for e in events)


@pytest.mark.asyncio
async def test_finding_gen_at_normal_boundary_is_admitted():
    eng = HypothesisEngine(max_depth=1)
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)

    q = run.collect(QuestionRaised(area="a", what_is_unknown="why X?", gen=0))
    h = run.collect(HypothesisFormed(question_ref=q.eid, statement="X < 500us"))
    x = run.collect(ExperimentDesigned(hypothesis_ref=h.eid, method="analysis", procedure="bench"))

    await run.emit(FindingPosted(description="right at the boundary", parent_ref=x.eid))
    await run.wait_quiescence()

    assert len(calls) == 1  # derived gen 1 == max_depth 1: admitted (inclusive bound)


@pytest.mark.asyncio
async def test_finding_forged_unroutable_parent_ref_is_dropped_not_seeded():
    """A non-empty parent_ref that doesn't resolve to any indexed event (a
    forged id, or a reference to the wrong event type) must be rejected
    outright — never defaulted to gen 0, which would hand a forged emission
    a fresh depth budget instead of capping it."""
    eng = HypothesisEngine(max_depth=0)
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)
    events: list[dict] = []
    run.on_event = lambda e: events.append(e)

    await run.emit(FindingPosted(description="forged parent", parent_ref="does-not-exist"))
    await run.wait_quiescence()

    assert calls == []  # never reached extraction
    assert not run.events_of(DedupChecked)
    assert any(e["type"] == "unroutable_parent_ref" for e in events)
    assert not any(e["type"] == "cycle_capped" for e in events)


@pytest.mark.asyncio
async def test_finding_empty_parent_ref_still_seeds_gen_zero():
    eng = HypothesisEngine(max_depth=0)
    run = eng.new_run()
    calls = _mute(eng, "extract")
    _wire(eng, run)

    await run.emit(FindingPosted(description="a real seed"))
    await run.wait_quiescence()

    assert len(calls) == 1
    assert run.events_of(FindingPosted)[-1].gen == 0


@pytest.mark.asyncio
async def test_question_forged_unroutable_parent_ref_is_dropped_not_seeded():
    eng = HypothesisEngine(max_depth=0)
    run = eng.new_run()
    calls = _mute(eng, "research")
    _wire(eng, run)
    events: list[dict] = []
    run.on_event = lambda e: events.append(e)

    await run.emit(
        QuestionRaised(area="a", what_is_unknown="from nowhere", parent_ref="forged-ref")
    )
    await run.wait_quiescence()

    assert calls == []
    assert any(e["type"] == "unroutable_parent_ref" for e in events)
    assert not any(e["type"] == "cycle_capped" for e in events)


@pytest.mark.asyncio
async def test_question_empty_parent_ref_still_seeds_gen_zero():
    eng = HypothesisEngine(max_depth=0)
    run = eng.new_run()
    calls = _mute(eng, "research")
    _wire(eng, run)

    await run.emit(QuestionRaised(area="a", what_is_unknown="root question"))
    await run.wait_quiescence()

    assert len(calls) == 1
    assert run.events_of(QuestionRaised)[-1].gen == 0


@pytest.mark.asyncio
async def test_question_gen_stale_value_from_research_leg_is_overwritten():
    """A research leg's sub-question copies a stale gen (e.g. left over from a
    retry) instead of parent.gen + 1 — the engine must still derive the real
    value from the parent question, not trust the stale one."""
    eng = HypothesisEngine(max_depth=5)
    run = eng.new_run()
    calls = _mute(eng, "research")
    _wire(eng, run)

    parent_q = run.collect(QuestionRaised(area="a", what_is_unknown="parent", gen=2))
    await run.emit(
        QuestionRaised(area="a", what_is_unknown="child, stale gen", parent_ref=parent_q.eid, gen=0)
    )
    await run.wait_quiescence()

    child = run.events_of(QuestionRaised)[-1]
    assert child.gen == 3  # derived: parent.gen(2) + 1, not the stale emitted 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_question_gen_from_extract_route_shares_finding_cycle_not_plus_one():
    """A question raised directly off a finding (extraction) shares that
    finding's cycle — it must NOT be bumped to gen + 1 like a research or
    conclude follow-up would be."""
    eng = HypothesisEngine(max_depth=5)
    run = eng.new_run()
    calls = _mute(eng, "research")
    _wire(eng, run)

    f = run.collect(FindingPosted(description="a finding", gen=2))
    await run.emit(QuestionRaised(area="a", what_is_unknown="extracted", parent_ref=f.eid))
    await run.wait_quiescence()

    child = run.events_of(QuestionRaised)[-1]
    assert child.gen == 2
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_question_gen_from_conclude_route_derived_via_result_chain():
    eng = HypothesisEngine(max_depth=5)
    run = eng.new_run()
    calls = _mute(eng, "research")
    _wire(eng, run)

    q = run.collect(QuestionRaised(area="a", what_is_unknown="why X?", gen=1))
    h = run.collect(HypothesisFormed(question_ref=q.eid, statement="X < 500us"))
    x = run.collect(ExperimentDesigned(hypothesis_ref=h.eid, method="analysis", procedure="bench"))
    r = run.collect(ResultRecorded(experiment_ref=x.eid, measurements="180us"))

    # A conclude leg's follow-up question, gen omitted by the model.
    await run.emit(QuestionRaised(area="a", what_is_unknown="follow-up", parent_ref=r.eid))
    await run.wait_quiescence()

    child = run.events_of(QuestionRaised)[-1]
    assert (
        child.gen == 2
    )  # derived: q.gen(1) + 1 via result -> experiment -> hypothesis -> question
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dedup_stage_passes_actions_true_when_tools_configured():
    eng = HypothesisEngine(dedup_repo="owner/repo", dedup_tools=("bash",), judge_model=None)
    run = eng.new_run()

    captured: dict = {}

    class _FakeAgent:
        name = "dedup-F-1"
        chat_model = None

        async def operate(self, *, instruction, actions=False):
            captured["actions"] = actions
            return "novelty checked"

    async def fake_make_agent(role, **kw):
        return _FakeAgent()

    run.make_agent = fake_make_agent
    f = FindingPosted(description="needs a real gh query", eid="F-1")

    await eng._dedup(run, f)

    assert captured["actions"] is True


@pytest.mark.asyncio
async def test_dedup_stage_no_tools_configured_does_not_force_actions():
    eng = HypothesisEngine(dedup_repo="owner/repo", dedup_tools=(), judge_model=None)
    run = eng.new_run()

    captured: dict = {}

    class _FakeAgent:
        name = "dedup-F-1"
        chat_model = None

        async def operate(self, *, instruction, actions=False):
            captured["actions"] = actions
            return "novelty checked"

    async def fake_make_agent(role, **kw):
        return _FakeAgent()

    run.make_agent = fake_make_agent
    f = FindingPosted(description="no tools granted", eid="F-1")

    await eng._dedup(run, f)

    assert captured["actions"] is False


@pytest.mark.asyncio
async def test_hypothesis_stage_effort_suffix_survives_profile_default(monkeypatch):
    profile_route = lambda role: (None, "low") if role == "critic" else (None, None)  # noqa: E731
    monkeypatch.setattr(engine_mod, "role_profile_route", profile_route)
    monkeypatch.setattr(hyp_mod, "role_profile_route", profile_route)

    eng = HypothesisEngine(model="codex/gpt-5.6-luna-high", judge_model=None)
    run = eng.new_run()
    branch = await run.make_agent(
        eng.conclude_role,
        name="c1",
        model=eng.model_for("conclude"),
        effort=eng.effort_for("conclude"),
    )
    kwargs = branch.chat_model.endpoint.config.kwargs
    assert kwargs.get("reasoning_effort") == "high"  # the suffix, not the profile's "low"
