# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine base protections — bundle-aware by_type, hard budgets, judge gate, emission repair, export."""

from __future__ import annotations

import asyncio

import pytest

from lionagi import Branch
from lionagi._errors import ContainmentRefusedError
from lionagi.agent.factory import _AgentSettingError
from lionagi.casts.emission import build_emission_operable
from lionagi.ln.concurrency._compat import ExceptionGroup
from lionagi.session.signal import StructuredOutput
from lionhub.orchestration.engines.engine import Engine, EngineBudgetError, JudgeVerdict
from lionhub.orchestration.engines.hypothesis import (
    ApplicationMapped,
    ChainEvent,
    ConclusionDrawn,
    ExperimentDesigned,
    HypothesisEngine,
    QuestionRaised,
)
from lionhub.orchestration.engines.research import FindingEmitted


class _StubEngine(Engine):
    async def _run(self, run, *a, **kw):  # pragma: no cover - not exercised
        return ""


# -- by_type: the production bundle path -------------------------------------


@pytest.mark.asyncio
async def test_by_type_unwraps_capability_bundles():
    """by_type must unwrap StructuredOutput bundles and return the typed events, not the envelopes."""
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


class _FakeJudge(Branch):
    def __init__(self, run, verdict: JudgeVerdict | None, text: str = ""):
        super().__init__(name="judge")
        object.__setattr__(self, "_verdict", verdict)
        object.__setattr__(self, "_text", text)
        run.session.include_branches(self)

    async def operate(self, *, instruction):
        assert "Root objective" in instruction
        if self._verdict is not None:
            await self.emit(self._verdict)
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
async def test_foreign_session_deny_does_not_gate_expansion(monkeypatch):
    eng = _StubEngine(judge_model="scripted/judge")
    run = eng.new_run()

    async def foreign(_judge, *, instruction):
        await run.emit(JudgeVerdict(subject="Q-1", allow=False, reason="foreign deny"))
        return "PASS"

    monkeypatch.setattr(_FakeJudge, "operate", foreign)

    async def fake_make(role, **kw):
        return _FakeJudge(run, None)

    run.make_agent = fake_make
    assert await eng.judge(run, "Q-1", "tangent question") is True
    assert len(run.by_type(JudgeVerdict)) == 1


@pytest.mark.asyncio
async def test_judge_text_fallback_for_weak_judges():
    eng = _StubEngine(judge_model="scripted/judge")
    run = eng.new_run()

    async def fake_make(role, **kw):
        return _FakeJudge(run, None, text="REJECT — duplicative")

    run.make_agent = fake_make
    assert await eng.judge(run, "Q-2", "dup question") is False


@pytest.mark.parametrize("typed", [True, False], ids=["typed-deny", "text-deny"])
@pytest.mark.parametrize("contained", [True, False], ids=["contained", "uncontained"])
@pytest.mark.parametrize("error_type", [RuntimeError, EngineBudgetError])
async def test_judge_notice_failure_does_not_reverse_a_deny(typed, contained, error_type, caplog):
    eng = _StubEngine(judge_model="scripted/judge", contain_account_integrations=contained)
    run = eng.new_run()
    events = []
    failure = error_type("notice sink failed password=synthetic-secret")

    def on_event(event):
        events.append(event)
        if event["type"] == "gated":
            raise failure

    run.on_event = on_event

    async def fake_make(role, **kw):
        verdict = JudgeVerdict(subject="Q-1", allow=False, reason="off-topic") if typed else None
        return _FakeJudge(run, verdict, text="REJECT")

    run.make_agent = fake_make
    assert await eng.judge(run, "Q-1", "tangent question") is False
    gated = [e for e in events if e["type"] == "gated"]
    assert len(gated) == 1
    assert gated[0]["reason"] == ("off-topic" if typed else "text-reject")
    assert not [e for e in events if e["type"] == "judge_error"]
    records = [r for r in caplog.records if "gated" in r.getMessage()]
    assert len(records) == 1
    assert error_type.__name__ in records[0].getMessage()
    assert "[redacted]" in records[0].getMessage()
    assert "synthetic-secret" not in records[0].getMessage()
    assert records[0].exc_info is None


@pytest.mark.parametrize("typed", [True, False], ids=["typed-deny", "text-deny"])
@pytest.mark.parametrize("error_type", [ContainmentRefusedError, _AgentSettingError])
@pytest.mark.parametrize("wrapping", ["direct", "cause", "context", "hidden", "group"])
async def test_judge_notice_propagates_strong_refusals(typed, error_type, wrapping):
    eng = _StubEngine(judge_model="scripted/judge")
    run = eng.new_run()
    refusal = error_type("refused")
    error = refusal
    if wrapping != "direct":
        if wrapping == "group":
            error = ExceptionGroup("group", [RuntimeError("other"), refusal])
        else:
            error = RuntimeError("wrapper")
            if wrapping == "cause":
                error.__cause__ = refusal
            else:
                error.__context__ = refusal
                error.__suppress_context__ = wrapping == "hidden"

    def on_event(event):
        if event["type"] == "gated":
            raise error

    run.on_event = on_event

    async def fake_make(role, **kw):
        verdict = JudgeVerdict(subject="Q-1", allow=False) if typed else None
        return _FakeJudge(run, verdict, text="REJECT")

    run.make_agent = fake_make
    with pytest.raises(error_type) as caught:
        await eng.judge(run, "Q-1", "question")
    assert caught.value is refusal


@pytest.mark.parametrize("chain", ["cycle", "exact-limit", "cycle-at-limit", "over-limit"])
async def test_judge_notice_exception_chain_is_bounded(chain, caplog):
    eng = _StubEngine(judge_model="scripted/judge")
    run = eng.new_run()
    error = RuntimeError("sink failed")
    if chain == "cycle":
        error.__cause__ = error
    elif chain != "cycle":
        cursor = error
        for _ in range(99 if chain != "over-limit" else 100):
            cursor.__cause__ = RuntimeError("wrapper")
            cursor = cursor.__cause__
        if chain == "cycle-at-limit":
            cursor.__cause__ = error

    def on_event(event):
        if event["type"] == "gated":
            raise error

    run.on_event = on_event

    async def fake_make(role, **kw):
        return _FakeJudge(run, None, text="REJECT")

    run.make_agent = fake_make
    if chain == "over-limit":
        with pytest.raises(RuntimeError) as caught:
            await eng.judge(run, "Q-1", "question")
        assert caught.value is error
    else:
        assert await eng.judge(run, "Q-1", "question") is False
        assert "[redacted]" in caplog.text


@pytest.mark.parametrize("unreadable", ["__cause__", "__context__"])
async def test_judge_notice_unreadable_exception_link_propagates_original(unreadable):
    class BrokenLink(RuntimeError):
        def __getattribute__(self, name):
            if name == unreadable:
                raise OSError("cannot inspect exception chain")
            return super().__getattribute__(name)

    eng = _StubEngine(judge_model="scripted/judge")
    run = eng.new_run()
    error = BrokenLink("sink failed")

    def on_event(event):
        if event["type"] == "gated":
            raise error

    run.on_event = on_event

    async def fake_make(role, **kw):
        return _FakeJudge(run, None, text="REJECT")

    run.make_agent = fake_make
    with pytest.raises(BrokenLink) as caught:
        await eng.judge(run, "Q-1", "question")
    assert caught.value is error


@pytest.mark.parametrize("shape", ["unreadable", "iteration", "wide"])
async def test_judge_notice_unreadable_or_wide_exception_group_propagates_original(shape):
    class BrokenGroup(ExceptionGroup):
        def __getattribute__(self, name):
            if name == "exceptions":
                if shape == "unreadable":
                    raise OSError("cannot read members")

                def broken_members():
                    yield RuntimeError("one member")
                    raise OSError("cannot read another member")

                return broken_members()
            return super().__getattribute__(name)

    eng = _StubEngine(judge_model="scripted/judge")
    run = eng.new_run()
    member = RuntimeError("member")
    error = (
        ExceptionGroup("group", [member] * 101)
        if shape == "wide"
        else BrokenGroup("group", [member])
    )

    def on_event(event):
        if event["type"] == "gated":
            raise error

    run.on_event = on_event

    async def fake_make(role, **kw):
        return _FakeJudge(run, None, text="REJECT")

    run.make_agent = fake_make
    caught = None
    try:
        await eng.judge(run, "Q-1", "question")
    except BaseException as exc:
        caught = exc
    assert caught is error


@pytest.mark.parametrize("typed", [True, False], ids=["typed-deny", "text-deny"])
async def test_judge_notice_cancellation_propagates(typed):
    eng = _StubEngine(judge_model="scripted/judge")
    run = eng.new_run()
    cancelled = asyncio.CancelledError("cancel notice")

    def on_event(event):
        if event["type"] == "gated":
            raise cancelled

    run.on_event = on_event

    async def fake_make(role, **kw):
        verdict = JudgeVerdict(subject="Q-1", allow=False) if typed else None
        return _FakeJudge(run, verdict, text="REJECT")

    run.make_agent = fake_make
    with pytest.raises(asyncio.CancelledError) as caught:
        await eng.judge(run, "Q-1", "question")
    assert caught.value is cancelled


@pytest.mark.parametrize("typed", [True, False], ids=["typed-allow", "text-allow"])
async def test_judge_allow_does_not_send_a_gated_notice(typed):
    eng = _StubEngine(judge_model="scripted/judge")
    run = eng.new_run()
    events = []

    def on_event(event):
        events.append(event)
        if event["type"] == "gated":
            raise RuntimeError("unexpected gated notice")

    run.on_event = on_event

    async def fake_make(role, **kw):
        verdict = JudgeVerdict(subject="Q-1", allow=True) if typed else None
        return _FakeJudge(run, verdict, text="PASS")

    run.make_agent = fake_make
    assert await eng.judge(run, "Q-1", "question") is True
    assert not [e for e in events if e["type"] == "gated"]


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
    """Emits nothing until the designated call index, then emits the given event."""

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
