# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Caller seeds and agent findings retain distinct live admission paths."""

from __future__ import annotations

import asyncio
import json

import pytest

from lionagi.ln.types import Operable, Spec
from lionagi.operations._observe import emit_message
from lionagi.protocols.messages import AssistantResponse
from lionhub.orchestration.engines.hypothesis import (
    ChainEvent,
    DedupChecked,
    ExperimentDesigned,
    FindingPosted,
    HypothesisEngine,
    HypothesisFormed,
    QuestionRaised,
    filing_queue,
    render_evidence,
)


def _wire(engine, run, monkeypatch):
    calls = []

    def record(stage):
        async def observe(_run, event):
            calls.append((stage, event))

        return observe

    for stage in ("dedup", "extract", "research"):
        monkeypatch.setattr(engine, f"_{stage}", record(stage))
    run.observe(ChainEvent, lambda event, _ctx: run._collect_emitted(event))
    run.observe(FindingPosted, lambda event, _ctx: engine._on_finding(run, event))
    run.observe(run._agent_events(DedupChecked), lambda event, _ctx: engine._on_dedup(run, event))
    run.observe(QuestionRaised, lambda event, _ctx: engine._on_question(run, event))
    return calls


def _agent(run):
    branch = run.session.new_branch(name="validator")
    branch.grant_capabilities(
        Operable(
            (
                Spec(FindingPosted, name="finding_posted"),
                Spec(DedupChecked, name="dedup_checked"),
                Spec(QuestionRaised, name="question_raised"),
            ),
            name="validator",
        ),
        prompt=False,
    )
    return branch


async def _emit(branch, key, **fields):
    reply = AssistantResponse.from_response("```json\n" + json.dumps({key: fields}) + "\n```")
    await emit_message(branch, reply)


@pytest.mark.asyncio
@pytest.mark.parametrize("dedup_repo", [None, "owner/repo"])
async def test_agent_parentless_finding_cannot_enter_seed_stages(monkeypatch, dedup_repo):
    engine = HypothesisEngine(dedup_repo=dedup_repo)
    run = engine.new_run()
    calls = _wire(engine, run, monkeypatch)
    branch = _agent(run)

    await _emit(
        branch,
        "finding_posted",
        description="unparented model finding",
        source="seed",
        gen=0,
        eid="F-1",
    )
    await run.wait_quiescence()

    assert len(run.events_of(FindingPosted)) == 1
    assert calls == []


@pytest.mark.asyncio
async def test_refused_live_finding_cannot_be_certified_or_become_question_parent(
    monkeypatch, tmp_path
):
    engine = HypothesisEngine(dedup_repo="owner/repo")
    run = engine.new_run()
    calls = _wire(engine, run, monkeypatch)
    branch = _agent(run)
    await _emit(branch, "finding_posted", description="unparented model finding", source="seed")
    await run.wait_quiescence()
    finding = run.events_of(FindingPosted)[0]
    calls.clear()

    await _emit(branch, "dedup_checked", finding_ref=finding.eid, verdict="new")
    await _emit(
        branch,
        "question_raised",
        area="storage",
        what_is_unknown="why serialize?",
        parent_ref=finding.eid,
        gen=0,
    )
    await run.wait_quiescence()

    assert finding.eid not in run.dedup_verdicts
    assert finding.eid not in run.dedup_cleared
    assert calls == []
    assert filing_queue(run) == []
    assert "(not accepted)" in render_evidence(run)
    run.export(tmp_path)
    assert json.loads((tmp_path / "chains.json").read_text())["filing_queue"] == []


@pytest.mark.asyncio
async def test_stored_verdict_cannot_certify_refused_live_finding(monkeypatch):
    engine = HypothesisEngine()
    run = engine.new_run()
    _wire(engine, run, monkeypatch)
    await _emit(_agent(run), "finding_posted", description="refused model finding")
    await run.wait_quiescence()
    finding = run.events_of(FindingPosted)[0]

    # A collected verdict dispatches no reaction, exercising the export fallback.
    run.collect(DedupChecked(finding_ref=finding.eid, verdict="new"))

    assert filing_queue(run) == []
    assert "(not accepted)" in render_evidence(run)


@pytest.mark.asyncio
async def test_agent_finding_with_resolved_experiment_keeps_derived_generation(monkeypatch):
    engine = HypothesisEngine(max_depth=2)
    run = engine.new_run()
    calls = _wire(engine, run, monkeypatch)
    question = run.collect(QuestionRaised(area="a", what_is_unknown="why?", gen=1))
    hypothesis = run.collect(HypothesisFormed(question_ref=question.eid, statement="prediction"))
    experiment = run.collect(
        ExperimentDesigned(hypothesis_ref=hypothesis.eid, method="analysis", procedure="count")
    )
    branch = _agent(run)

    await _emit(
        branch,
        "finding_posted",
        description="new validation fact",
        parent_ref=experiment.eid,
        gen=0,
    )
    await _emit(
        branch,
        "finding_posted",
        description="unresolved validation fact",
        parent_ref="missing",
        gen=0,
    )
    await run.wait_quiescence()

    assert [(stage, event.description, event.gen) for stage, event in calls] == [
        ("extract", "new validation fact", 2)
    ]


@pytest.mark.asyncio
async def test_caller_seed_trust_does_not_transfer_to_a_model_copy_or_another_run(monkeypatch):
    engine = HypothesisEngine()
    first = engine.new_run()
    first_calls = _wire(engine, first, monkeypatch)
    seed = FindingPosted(description="caller seed", source="seed", gen=0)
    await first.emit(seed)
    await first.wait_quiescence()
    assert [(stage, event) for stage, event in first_calls] == [("extract", seed)]
    first_calls.clear()

    await _emit(
        _agent(first),
        "finding_posted",
        **{**seed.model_dump(), "description": "model copy in first run"},
    )
    await first.wait_quiescence()
    assert first_calls == []

    second = engine.new_run()
    second_calls = _wire(engine, second, monkeypatch)
    await _emit(
        _agent(second),
        "finding_posted",
        **{**seed.model_dump(), "description": "model copy in second run"},
    )
    await second.wait_quiescence()
    assert second_calls == []

    await second.emit(FindingPosted(description="second caller seed"))
    await second.wait_quiescence()
    assert [(stage, event.description) for stage, event in second_calls] == [
        ("extract", "second caller seed")
    ]


@pytest.mark.asyncio
async def test_cancelled_caller_dispatch_does_not_trust_a_later_agent_replay(monkeypatch):
    engine = HypothesisEngine()
    run = engine.new_run()
    calls = _wire(engine, run, monkeypatch)
    entered = asyncio.Event()

    async def pause(_event, _ctx):
        entered.set()
        await asyncio.Event().wait()

    run.observe(FindingPosted, pause)
    seed = FindingPosted(description="cancelled caller dispatch")
    task = asyncio.create_task(run.emit(seed))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run.session.observer.unobserve(pause)
    await run.wait_quiescence()
    calls.clear()

    seed.description = "agent replay after cancelled caller dispatch"
    await _agent(run).emit(seed)
    await run.wait_quiescence()

    assert calls == []


@pytest.mark.asyncio
async def test_caller_scope_unwinds_after_observer_error(monkeypatch):
    engine = HypothesisEngine()
    run = engine.new_run()
    calls = _wire(engine, run, monkeypatch)

    def fail(_event, _ctx):
        raise ValueError("observer stopped caller dispatch")

    run.observe(FindingPosted, fail)
    seed = FindingPosted(description="caller dispatch with observer failure")
    with pytest.raises(ValueError, match="observer stopped caller dispatch"):
        await run.emit(seed)
    run.session.observer.unobserve(fail)
    await run.wait_quiescence()
    calls.clear()

    seed.description = "agent replay after observer failure"
    await _agent(run).emit(seed)
    await run.wait_quiescence()

    assert calls == []


@pytest.mark.asyncio
async def test_child_task_cannot_inherit_caller_seed_admission(monkeypatch):
    engine = HypothesisEngine()
    run = engine.new_run()
    calls = _wire(engine, run, monkeypatch)
    branch = _agent(run)

    async def replay(event, _ctx):
        if event.description == "original caller seed":
            event.description = "child task agent replay"
            await asyncio.create_task(branch.emit(event))

    run.observe(FindingPosted, replay)
    await run.emit(FindingPosted(description="original caller seed"))
    await run.wait_quiescence()

    # Only the original caller dispatch may schedule extraction.
    assert [stage for stage, _event in calls] == ["extract"]


@pytest.mark.asyncio
async def test_explicit_caller_resubmission_can_admit_a_previously_refused_object(monkeypatch):
    engine = HypothesisEngine(dedup_repo="owner/repo")
    run = engine.new_run()
    dedup = engine._dedup
    calls = _wire(engine, run, monkeypatch)
    branch = _agent(run)
    await _emit(branch, "finding_posted", description="model finding without a parent")
    await run.wait_quiescence()
    finding = run.events_of(FindingPosted)[0]
    assert calls == []
    assert filing_queue(run) == []

    # Neither lower-level bus route may turn the same object into a caller seed.
    for name, emitter in (("branch", branch), ("session", run.session)):
        finding.description = f"raw {name} replay of refused finding"
        await emitter.emit(finding)
        await _emit(branch, "dedup_checked", finding_ref=finding.eid, verdict="new")
        await run.wait_quiescence()
        assert calls == []
        assert finding.eid not in run.dedup_verdicts
        assert filing_queue(run) == []

    # Use the real dedup stage and search; replace only the provider boundary.
    dedup_branch = _agent(run)

    async def make_agent(_role, **_kwargs):
        return dedup_branch

    async def operate(agent, _instruction, **_kwargs):
        assert agent is dedup_branch
        await _emit(agent, "dedup_checked", finding_ref=finding.eid, verdict="new")

    monkeypatch.setattr(engine, "_dedup", dedup)
    monkeypatch.setattr(run, "make_agent", make_agent)
    monkeypatch.setattr(run, "operate_with_repair", operate)
    finding.description = "caller explicitly adopts this finding as a seed"

    await run.emit(finding)
    await run.wait_quiescence()

    assert finding.gen == 0
    assert [(stage, event) for stage, event in calls] == [("extract", finding)]
    verdict = run.dedup_verdicts[finding.eid]
    assert verdict.finding_ref == finding.eid
    assert run.dedup_cleared == {finding.eid}
    assert [(item["finding_ref"], item["claim"]) for item in filing_queue(run)] == [
        (finding.eid, finding.description)
    ]
    assert f"{finding.eid}: new (not accepted)" not in render_evidence(run)


@pytest.mark.asyncio
async def test_agent_finding_can_recover_when_its_experiment_parent_becomes_resolvable(monkeypatch):
    engine = HypothesisEngine(dedup_repo="owner/repo", judge_model=None)
    run = engine.new_run()
    dedup = engine._dedup
    calls = _wire(engine, run, monkeypatch)
    branch = _agent(run)

    await _emit(
        branch,
        "finding_posted",
        description="experiment parent is not yet indexed",
        parent_ref="X-1",
        gen=0,
    )
    await run.wait_quiescence()
    finding = run.events_of(FindingPosted)[0]
    assert calls == []

    question = run.collect(QuestionRaised(area="storage", what_is_unknown="why?", gen=0))
    hypothesis = run.collect(HypothesisFormed(question_ref=question.eid, statement="prediction"))
    experiment = run.collect(
        ExperimentDesigned(hypothesis_ref=hypothesis.eid, method="analysis", procedure="count")
    )
    assert experiment.eid == finding.parent_ref
    dedup_branch = _agent(run)

    async def make_agent(_role, **_kwargs):
        return dedup_branch

    async def operate(agent, _instruction, **_kwargs):
        assert agent is dedup_branch
        await _emit(agent, "dedup_checked", finding_ref=finding.eid, verdict="new")

    monkeypatch.setattr(engine, "_dedup", dedup)
    monkeypatch.setattr(run, "make_agent", make_agent)
    monkeypatch.setattr(run, "operate_with_repair", operate)
    finding.description = "agent resubmits after the experiment parent is indexed"

    await branch.emit(finding)
    await run.wait_quiescence()

    assert finding.gen == 1
    assert finding.eid in run.dedup_judged
    assert [(stage, event) for stage, event in calls] == [("extract", finding)]
    verdict = run.dedup_verdicts[finding.eid]
    assert verdict.finding_ref == finding.eid
    assert run.dedup_cleared == {finding.eid}
    assert [(item["finding_ref"], item["claim"]) for item in filing_queue(run)] == [
        (finding.eid, finding.description)
    ]
