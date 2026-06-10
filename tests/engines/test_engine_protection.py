# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine base protections — bundle-aware by_type, hard budgets, judge gate,
emission repair, export. No LLM."""

from __future__ import annotations

import pytest

from lionagi.casts.emission import build_emission_operable
from lionagi.engines.engine import Engine, EngineBudgetError, JudgeVerdict
from lionagi.engines.hypothesis import (
    ApplicationMapped,
    ChainEvent,
    ConclusionDrawn,
    ExperimentDesigned,
    HypothesisEngine,
    QuestionRaised,
)
from lionagi.engines.research import FindingEmitted
from lionagi.session.signal import StructuredOutput


class _StubEngine(Engine):
    async def _run(self, run, *a, **kw):  # pragma: no cover - not exercised
        return ""


# -- by_type: the production bundle path -------------------------------------


@pytest.mark.asyncio
async def test_by_type_unwraps_capability_bundles():
    """Agent emissions arrive as StructuredOutput bundles; by_type must return
    the typed events, not the envelopes (this crashed research/review synthesis
    in any real run)."""
    run = _StubEngine().new_run()
    op = build_emission_operable((FindingEmitted,))
    bundle_model = op.create_model(include={"finding_emitted"})
    bundle = bundle_model.model_validate(
        {"finding_emitted": {"description": "prod finding", "novelty": 0.4}}
    )
    await run.session.emit(StructuredOutput(data=bundle))
    got = run.by_type(FindingEmitted)
    assert len(got) == 1
    assert isinstance(got[0], FindingEmitted)
    assert got[0].novelty == 0.4


@pytest.mark.asyncio
async def test_by_type_still_returns_directly_emitted_events():
    run = _StubEngine().new_run()
    await run.emit(FindingEmitted(description="direct", novelty=0.9))
    got = run.by_type(FindingEmitted)
    assert len(got) == 1 and got[0].description == "direct"


# -- hard budgets -------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_denied_when_agent_budget_exhausted():
    run = _StubEngine(max_agents=0).new_run()
    events: list[dict] = []
    run.on_event = events.append

    async def work():  # pragma: no cover - must never run
        raise AssertionError("spawned past budget")

    assert run.spawn(work()) is None
    await run.wait_quiescence()
    assert any(e["type"] == "budget_exhausted" for e in events)


@pytest.mark.asyncio
async def test_make_agent_raises_when_exhausted_but_exempt_passes():
    run = _StubEngine(max_agents=0).new_run()
    with pytest.raises(EngineBudgetError):
        await run.make_agent("synthesizer")
    branch = await run.make_agent("synthesizer", name="synth", exempt=True)
    assert branch.name == "synth"
    assert run.agents_made == 1


@pytest.mark.asyncio
async def test_deadline_blocks_spawning():
    run = _StubEngine(deadline_s=0.0).new_run()

    async def work():  # pragma: no cover - must never run
        raise AssertionError("spawned past deadline")

    assert not run.budget_left()
    assert run.spawn(work()) is None


# -- judge gate ---------------------------------------------------------------


class _FakeJudge:
    name = "judge"

    def __init__(self, run, verdict: JudgeVerdict | None, text: str = ""):
        self._run = run
        self._verdict = verdict
        self._text = text

    async def operate(self, *, instruction):
        assert "Root objective" in instruction
        if self._verdict is not None:
            await self._run.emit(self._verdict)
        return self._text


@pytest.mark.asyncio
async def test_judge_disabled_always_passes():
    eng = _StubEngine()
    run = eng.new_run()
    assert await eng.judge(run, "Q-1", "anything") is True
    assert run.agents_made == 0


@pytest.mark.asyncio
async def test_judge_verdict_gates_expansion():
    eng = _StubEngine(judge_model="scripted/judge")
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append

    async def fake_make(role, **kw):
        return _FakeJudge(run, JudgeVerdict(subject="Q-1", allow=False, reason="off-topic"))

    run.make_agent = fake_make
    assert await eng.judge(run, "Q-1", "tangent question") is False
    assert any(e["type"] == "gated" and e["eid"] == "Q-1" for e in events)


@pytest.mark.asyncio
async def test_judge_text_fallback_for_weak_judges():
    eng = _StubEngine(judge_model="scripted/judge")
    run = eng.new_run()

    async def fake_make(role, **kw):
        return _FakeJudge(run, None, text="REJECT — duplicative")

    run.make_agent = fake_make
    assert await eng.judge(run, "Q-2", "dup question") is False


@pytest.mark.asyncio
async def test_judge_fails_open_on_error():
    eng = _StubEngine(judge_model="scripted/judge")
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append

    async def fake_make(role, **kw):
        raise RuntimeError("judge backend down")

    run.make_agent = fake_make
    assert await eng.judge(run, "Q-3", "question") is True
    assert any(e["type"] == "judge_error" for e in events)


@pytest.mark.asyncio
async def test_judge_denied_when_budget_exhausted():
    eng = _StubEngine(judge_model="scripted/judge", max_agents=0)
    run = eng.new_run()
    assert await eng.judge(run, "Q-4", "question") is False


# -- emission repair ----------------------------------------------------------


class _ProseBranch:
    """Emits nothing on the first call; emits on the repair call."""

    name = "weak-agent"

    def __init__(self, run, emit_on_call: int, event):
        self._run = run
        self._event = event
        self._emit_on = emit_on_call
        self.calls: list[str] = []

    async def operate(self, *, instruction):
        self.calls.append(instruction)
        if len(self.calls) == self._emit_on:
            await self._run.emit(self._event)
        return "prose"


@pytest.mark.asyncio
async def test_repair_reprompts_until_emission_arrives():
    eng = _StubEngine()
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    branch = _ProseBranch(run, 2, FindingEmitted(description="late", novelty=0.1))

    await run.operate_with_repair(
        branch,
        "find things",
        arrived=lambda: bool(run.by_type(FindingEmitted)),
        emits=(FindingEmitted,),
        retries=2,
    )
    assert len(branch.calls) == 2
    assert "produced no valid emission" in branch.calls[1]
    assert "finding_emitted" in branch.calls[1]  # repair names the expected key
    assert any(e["type"] == "emission_repair" for e in events)
    assert not any(e["type"] == "emission_missing" for e in events)


@pytest.mark.asyncio
async def test_repair_gives_up_and_notifies():
    eng = _StubEngine()
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    branch = _ProseBranch(run, 99, FindingEmitted(description="never", novelty=0.1))

    await run.operate_with_repair(
        branch,
        "find things",
        arrived=lambda: False,
        emits=(FindingEmitted,),
        retries=1,
    )
    assert len(branch.calls) == 2
    assert any(e["type"] == "emission_missing" for e in events)


# -- hypothesis judge wiring ----------------------------------------------------


@pytest.mark.asyncio
async def test_question_expansion_gated_by_judge():
    eng = HypothesisEngine(judge_model="scripted/judge")
    run = eng.new_run()
    judged: list[str] = []
    made: list[str] = []

    async def deny(run_, eid, subject):
        judged.append(eid)
        return False

    async def fake_make(role, **kw):  # pragma: no cover - must never run
        made.append(role)
        raise AssertionError("agent made despite judge denial")

    eng.judge = deny
    run.make_agent = fake_make
    run.observe(ChainEvent, lambda e, _c: run.collect(e))
    run.observe(QuestionRaised, lambda q, _c: eng._on_question(run, q))
    await run.emit(QuestionRaised(area="x", what_is_unknown="why tangent?"))
    await run.wait_quiescence()
    assert judged == ["Q-1"]
    assert made == []


# -- export ---------------------------------------------------------------------


def test_export_writes_chains_json_and_report(tmp_path):
    import json as _json

    eng = HypothesisEngine()
    run = eng.new_run()
    run.root = "seed finding"
    q = run.collect(QuestionRaised(area="a", what_is_unknown="why X over Y?"))
    c = run.collect(
        ConclusionDrawn(
            question_ref=q.eid,
            verdict="keep X",
            rationale="cheaper",
            basis="quantitative",
        )
    )
    run.collect(ApplicationMapped(conclusion_ref=c.eid, decision_ref="D-007", effect="supports"))
    pend = run.collect(
        ExperimentDesigned(hypothesis_ref="H-9", method="benchmark", procedure="criterion run")
    )
    run.pending.append(pend)

    paths = run.export(tmp_path / "out", report="THE REPORT")
    data = _json.loads((tmp_path / "out" / "chains.json").read_text())
    assert data["root"] == "seed finding"
    assert data["decisions_touched"] == ["D-007"]
    assert data["chains"] == [["Q-1", "C-1", "A-1"]]
    assert data["pending_experiments"][0]["procedure"] == "criterion run"
    report = (tmp_path / "out" / "report.md").read_text()
    assert report.startswith("THE REPORT")
    assert "criterion run" in report and "D-007" in report
    assert paths["report"].endswith("report.md")
