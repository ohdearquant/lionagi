# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Typed authority follows the actual branch emission, including replayed payloads."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel

import lionhub.orchestration.engines.engine as engine_mod
import lionhub.orchestration.engines.hypothesis as hyp_mod
from lionagi import Branch
from lionagi.ln.types import Operable, Spec
from lionagi.operations._observe import emit_message
from lionagi.protocols.messages import AssistantResponse
from lionagi.session.signal import StructuredOutput
from lionhub.orchestration.engines.engine import Engine, JudgeVerdict
from lionhub.orchestration.engines.hypothesis import (
    ChainEvent,
    DedupChecked,
    FindingPosted,
    HypothesisEngine,
    filing_queue,
    render_evidence,
)

pytestmark = pytest.mark.asyncio


class _Trigger(BaseModel):
    text: str


@pytest.fixture
def agents(monkeypatch):
    scripts = {}
    made = []

    async def operate(branch, *, instruction, **kwargs):
        return await scripts[branch.name](branch, instruction)

    async def create(_spec, **kwargs):
        branch = Branch()
        branch.grant_capabilities(
            Operable(
                (
                    Spec(JudgeVerdict, name="judge_verdict"),
                    Spec(DedupChecked, name="dedup_checked"),
                ),
                name="critic",
            ),
            prompt=False,
        )
        made.append(branch)
        return branch

    monkeypatch.setattr(engine_mod, "create_agent", create)
    monkeypatch.setattr(engine_mod, "role_profile_route", lambda role: (None, None))
    monkeypatch.setattr(engine_mod, "role_profile_injection", lambda role: None)
    monkeypatch.setattr(hyp_mod, "role_profile_route", lambda role: (None, None))
    monkeypatch.setattr(Branch, "operate", operate)
    return scripts, made


async def _emit_model(branch, key, **fields):
    reply = AssistantResponse.from_response("```json\n" + json.dumps({key: fields}) + "\n```")
    await emit_message(branch, reply)


def _dedup_run(monkeypatch):
    engine = HypothesisEngine(
        dedup_repo="owner/repo", judge_model=None, repair_retries=0, max_concurrent=4
    )
    run = engine.new_run()
    extracted = []
    notices = []
    run.on_event = notices.append

    async def extract(_run, finding):
        extracted.append(finding)

    monkeypatch.setattr(engine, "_extract", extract)
    run.observe(ChainEvent, lambda event, _ctx: run._collect_emitted(event))
    run.observe(run._agent_events(DedupChecked), lambda event, _ctx: engine._on_dedup(run, event))
    first = run.collect(FindingPosted(description="first caller finding"))
    second = run.collect(FindingPosted(description="second caller finding"))
    return engine, run, first, second, extracted, notices


@pytest.mark.parametrize("foreign_allow", [True, False])
async def test_judge_uses_own_typed_verdict_not_another_agents_subject(agents, foreign_allow):
    scripts, _ = agents
    engine = Engine(judge_model="openai/scripted")
    run = engine.new_run()

    async def first(branch, _instruction):
        await _emit_model(branch, "judge_verdict", subject="B", allow=foreign_allow)
        return "PASS"

    async def second(branch, _instruction):
        await _emit_model(branch, "judge_verdict", subject="B", allow=not foreign_allow)
        return "PASS" if foreign_allow else "REJECT"

    scripts.update({"judge-A": first, "judge-B": second})
    await engine.judge(run, "A", "first item")
    assert await engine.judge(run, "B", "second item") is (not foreign_allow)


async def test_concurrent_judges_with_same_role_do_not_share_authority(agents):
    scripts, made = agents
    engine = Engine(judge_model="openai/scripted", max_concurrent=2)
    run = engine.new_run()
    second_started = asyncio.Event()
    foreign_sent = asyncio.Event()

    async def first(branch, _instruction):
        await second_started.wait()
        await _emit_model(branch, "judge_verdict", subject="B", allow=True)
        foreign_sent.set()
        return "PASS"

    async def second(branch, _instruction):
        second_started.set()
        await foreign_sent.wait()
        await _emit_model(branch, "judge_verdict", subject="B", allow=False)
        return "PASS"

    scripts.update({"judge-A": first, "judge-B": second})
    assert await asyncio.gather(
        engine.judge(run, "A", "first"), engine.judge(run, "B", "second")
    ) == [True, False]
    assert len(made) == 2


@pytest.mark.parametrize("verdict", ["duplicate", "new", "extends"])
async def test_foreign_dedup_does_not_answer_or_certify_another_finding(
    agents, monkeypatch, verdict, tmp_path
):
    scripts, _ = agents
    engine, run, first, second, extracted, notices = _dedup_run(monkeypatch)

    async def first_search(branch, _instruction):
        await _emit_model(branch, "dedup_checked", finding_ref=second.eid, verdict=verdict)
        await _emit_model(branch, "dedup_checked", finding_ref=first.eid, verdict="new")
        return ""

    async def second_search(_branch, _instruction):
        return "no verdict"

    scripts.update({f"dedup-{first.eid}": first_search, f"dedup-{second.eid}": second_search})
    await engine._dedup_search(run, first)
    await engine._dedup_search(run, second)
    await run.wait_quiescence()

    assert second.eid not in run.dedup_verdicts
    assert extracted.count(first) == extracted.count(second) == 1
    assert [event["eid"] for event in notices if event["type"] == "dedup_missing"] == [second.eid]
    assert [item["finding_ref"] for item in filing_queue(run)] == [first.eid]
    assert f"{second.eid}: {verdict} (not accepted)" in render_evidence(run)
    run.export(tmp_path)
    exported = json.loads((tmp_path / "chains.json").read_text())
    assert [item["finding_ref"] for item in exported["filing_queue"]] == [first.eid]


async def test_own_dedup_verdict_wins_after_a_foreign_answer(agents, monkeypatch):
    scripts, _ = agents
    engine, run, first, second, extracted, _notices = _dedup_run(monkeypatch)

    async def first_search(branch, _instruction):
        await _emit_model(branch, "dedup_checked", finding_ref=second.eid, verdict="new")
        await _emit_model(branch, "dedup_checked", finding_ref=first.eid, verdict="duplicate")
        return ""

    async def second_search(branch, _instruction):
        await _emit_model(branch, "dedup_checked", finding_ref=second.eid, verdict="duplicate")
        return ""

    scripts.update({f"dedup-{first.eid}": first_search, f"dedup-{second.eid}": second_search})
    await engine._dedup_search(run, first)
    await engine._dedup_search(run, second)
    await run.wait_quiescence()

    assert run.dedup_verdicts[second.eid].verdict == "duplicate"
    assert extracted == []
    assert filing_queue(run) == []


@pytest.mark.parametrize("own_first", [False, True])
async def test_replayed_dedup_object_keeps_each_occurrences_acceptance(
    agents, monkeypatch, own_first
):
    scripts, _ = agents
    engine, run, first, second, _extracted, _notices = _dedup_run(monkeypatch)
    same = DedupChecked(finding_ref=second.eid, verdict="new", rationale="same exact object")

    async def emit_same(branch, _instruction):
        await branch.emit(same)
        return ""

    scripts.update({f"dedup-{first.eid}": emit_same, f"dedup-{second.eid}": emit_same})
    order = (second, first) if own_first else (first, second)
    for finding in order:
        await engine._dedup_search(run, finding)
    await run.wait_quiescence()

    assert [item["finding_ref"] for item in filing_queue(run)] == [second.eid]
    trail = render_evidence(run)
    assert trail.count(f"{second.eid}: new") == 2
    assert trail.count(f"{second.eid}: new (not accepted)") == 1


async def test_replayed_object_mutation_does_not_rewrite_previous_verdict_receipt(
    agents, monkeypatch
):
    scripts, _ = agents
    engine, run, first, second, _extracted, _notices = _dedup_run(monkeypatch)
    same = DedupChecked(finding_ref=second.eid, verdict="new", rationale="before mutation")

    async def first_search(branch, _instruction):
        await branch.emit(same)
        return ""

    async def second_search(branch, _instruction):
        same.verdict = "duplicate"
        same.rationale = "after mutation"
        await branch.emit(same)
        return ""

    scripts.update({f"dedup-{first.eid}": first_search, f"dedup-{second.eid}": second_search})
    await engine._dedup_search(run, first)
    await engine._dedup_search(run, second)
    await run.wait_quiescence()

    trail = render_evidence(run)
    assert f"{second.eid}: new (not accepted) — before mutation" in trail
    assert f"{second.eid}: duplicate — after mutation" in trail
    assert filing_queue(run) == []


async def test_nested_session_emission_cannot_borrow_the_judge_branch(agents):
    scripts, _ = agents
    engine = Engine(judge_model="openai/scripted")
    run = engine.new_run()

    async def nested(_event, _ctx):
        await run.session.emit(JudgeVerdict(subject="B", allow=True))

    run.observe(_Trigger, nested)

    async def judge(branch, _instruction):
        await branch.emit(_Trigger(text="trigger nested session emission"))
        await _emit_model(branch, "judge_verdict", subject="B", allow=False)
        return "PASS"

    scripts["judge-B"] = judge
    assert await engine.judge(run, "B", "item") is False


async def test_denied_stored_judge_verdict_has_no_authority(agents):
    scripts, _ = agents
    engine = Engine(judge_model="openai/scripted")
    run = engine.new_run()
    gates = []

    def deny(payload):
        gates.append(payload)
        return False

    run.session.observer.gate(deny)

    async def judge(branch, _instruction):
        assert await branch.emit(JudgeVerdict(subject="B", allow=False)) == []
        return "PASS"

    scripts["judge-B"] = judge
    assert await engine.judge(run, "B", "item") is True
    assert len(gates) == 1


async def test_reinclusion_callback_and_emission_results_stay_unchanged(agents):
    _scripts, made = agents
    engine = Engine()
    callback_calls = []
    emitter_calls = []
    sync_result, async_result, callback_result = object(), object(), object()

    def created(branch):
        callback_calls.append(branch)
        original = branch.emit

        async def decorated(event):
            emitter_calls.append(event)
            return [*(await original(event)), callback_result]

        object.__setattr__(branch, "emit", decorated)

    run = engine.new_run(on_branch_created=created)
    branch = await run.make_agent("critic")
    run.session.include_branches(branch)
    run.observe(JudgeVerdict, lambda _event, _ctx: sync_result)

    async def asynchronous(_event, _ctx):
        return async_result

    run.observe(JudgeVerdict, asynchronous)
    verdict = JudgeVerdict(subject="item")
    assert await branch.emit(verdict) == [sync_result, async_result, callback_result]
    assert callback_calls == made == [branch]
    assert emitter_calls == [verdict]
    gate_calls = []
    run.session.observer.gate(lambda action: gate_calls.append(action) or False)
    action = object()
    assert await branch.authorize(action) is False
    assert gate_calls == [action]


async def test_explicit_historical_collection_still_certifies_a_seed():
    engine = HypothesisEngine(judge_model=None)
    run = engine.new_run()
    seed = run.collect(FindingPosted(description="historical caller seed"))
    verdict = run.collect(DedupChecked(finding_ref=seed.eid, verdict="new"))
    assert verdict in run.events_of(DedupChecked)
    assert [item["finding_ref"] for item in filing_queue(run)] == [seed.eid]


class _PassLookingReply:
    def __str__(self):
        return "PASS"


@pytest.mark.parametrize(
    "reply, allowed",
    [
        pytest.param("PASS", True, id="pass"),
        pytest.param(" \tPASS\r\n", True, id="padded-pass"),
        pytest.param("pass", True, id="lowercase-pass"),
        pytest.param("Pass", True, id="mixed-case-pass"),
        pytest.param("PASS.", False, id="punctuated-pass"),
        pytest.param("**PASS**", False, id="markdown-pass"),
        pytest.param("REJECT", False, id="reject"),
        pytest.param("reject", False, id="lowercase-reject"),
        pytest.param("NO - duplicative", False, id="no"),
        pytest.param("I refuse: off-topic", False, id="refusal-prose"),
        pytest.param("", False, id="empty"),
        pytest.param(" \t\r\n", False, id="whitespace"),
        pytest.param(None, False, id="none"),
        pytest.param("PASS — useful", False, id="pass-with-prose"),
        pytest.param("PASS\nREJECT", False, id="mixed-tokens"),
        pytest.param(b"PASS", False, id="bytes"),
        pytest.param({"decision": "PASS"}, False, id="mapping"),
        pytest.param(_PassLookingReply(), False, id="non-string-pass-like"),
    ],
)
async def test_no_verdict_text_requires_explicit_pass(agents, reply, allowed):
    scripts, _ = agents

    async def no_verdict(_branch, _instruction):
        return reply

    scripts["judge-B"] = no_verdict
    notices = []
    engine = Engine(judge_model="openai/scripted")
    run = engine.new_run(on_event=notices.append)
    assert await engine.judge(run, "B", "item") is allowed
    gated = [(notice["eid"], notice["reason"]) for notice in notices if notice["type"] == "gated"]
    assert gated == ([] if allowed else [("B", "text-reject")])


async def test_cross_run_replay_with_repeated_finding_ids_keeps_origin_separate(
    agents, monkeypatch, tmp_path
):
    scripts, _ = agents
    first_engine, first_run, _first, original, _out, _notices = _dedup_run(monkeypatch)
    same = DedupChecked(finding_ref=original.eid, verdict="new", rationale="original run")

    async def original_search(branch, _instruction):
        await branch.emit(same)
        return ""

    scripts[f"dedup-{original.eid}"] = original_search
    await first_engine._dedup_search(first_run, original)
    await first_run.wait_quiescence()

    engine, run, foreign, target, _extracted, _notices = _dedup_run(monkeypatch)
    assert target.eid == original.eid
    same.verdict = "duplicate"
    same.rationale = "replayed by another run's other agent"

    async def foreign_search(branch, _instruction):
        await branch.emit(same)
        return ""

    async def silent(_branch, _instruction):
        return ""

    scripts.update({f"dedup-{foreign.eid}": foreign_search, f"dedup-{target.eid}": silent})
    await engine._dedup_search(run, foreign)
    await engine._dedup_search(run, target)
    await run.wait_quiescence()

    assert target.eid not in run.dedup_verdicts
    assert filing_queue(run) == []
    assert [item["verdict"] for item in filing_queue(first_run)] == ["new"]
    assert f"{original.eid}: new — original run" in render_evidence(first_run)
    assert f"{target.eid}: duplicate (not accepted)" in render_evidence(run)
    first_run.export(tmp_path)
    exported = json.loads((tmp_path / "chains.json").read_text())
    verdicts = [event for event in exported["events"] if event["type"] == "DedupChecked"]
    assert [(event["verdict"], event["rationale"]) for event in verdicts] == [
        ("new", "original run")
    ]


async def test_judge_matches_a_typed_child_inside_its_structured_bundle(agents):
    scripts, _ = agents

    class Bundle(BaseModel):
        decision: JudgeVerdict

    async def judge(branch, _instruction):
        await branch.emit(
            StructuredOutput(data=Bundle(decision=JudgeVerdict(subject="B", allow=False)))
        )
        return "PASS"

    scripts["judge-B"] = judge
    engine = Engine(judge_model="openai/scripted")
    assert await engine.judge(engine.new_run(), "B", "item") is False


async def test_foreign_dedup_does_not_suppress_the_own_agents_one_repair(agents, monkeypatch):
    scripts, _ = agents
    engine, run, first, second, extracted, _notices = _dedup_run(monkeypatch)
    engine.repair_retries = 1
    attempts = []

    async def first_search(branch, _instruction):
        await _emit_model(branch, "dedup_checked", finding_ref=second.eid, verdict="new")
        await _emit_model(branch, "dedup_checked", finding_ref=first.eid, verdict="duplicate")
        return ""

    async def second_search(branch, instruction):
        attempts.append(instruction)
        if len(attempts) == 2:
            await _emit_model(branch, "dedup_checked", finding_ref=second.eid, verdict="duplicate")
        return "no verdict on the first attempt"

    scripts.update({f"dedup-{first.eid}": first_search, f"dedup-{second.eid}": second_search})
    await engine._dedup_search(run, first)
    await engine._dedup_search(run, second)
    await run.wait_quiescence()
    assert len(attempts) == 2
    assert run.dedup_verdicts[second.eid].verdict == "duplicate"
    assert extracted == []
    assert filing_queue(run) == []


async def test_nested_session_replay_of_the_same_envelope_does_not_borrow_origin(agents):
    scripts, _ = agents
    engine = Engine(judge_model="openai/scripted")
    run = engine.new_run()
    envelope = StructuredOutput(data=JudgeVerdict(subject="B", allow=False))
    gates = []

    async def gate(payload):
        gates.append(payload)
        if len(gates) == 1:
            await run.session.emit(envelope)
            return False
        return True

    run.session.observer.gate(gate)

    async def judge(branch, _instruction):
        assert await branch.emit(envelope) == []
        return "PASS"

    scripts["judge-B"] = judge
    assert await engine.judge(run, "B", "item") is True
    assert len(gates) == 2


@pytest.mark.parametrize("reuse_session", [False, True])
async def test_a_previous_judge_occurrence_does_not_decide_a_new_call(agents, reuse_session):
    scripts, _ = agents
    engine = Engine(judge_model="openai/scripted")
    first_run = engine.new_run()

    async def first(branch, _instruction):
        await branch.emit(JudgeVerdict(subject="B", allow=False))
        return "PASS"

    scripts["judge-B"] = first
    assert await engine.judge(first_run, "B", "old item") is False

    async def silent(_branch, _instruction):
        return "PASS"

    scripts["judge-B"] = silent
    run = engine.new_run(session=first_run.session) if reuse_session else first_run
    assert await engine.judge(run, "B", "new item with repeated id") is True


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_dispatch_does_not_leave_a_judge_capture_active(agents, cancelled):
    scripts, _ = agents
    engine = Engine(judge_model="openai/scripted")
    run = engine.new_run()

    def fail(_event, _ctx):
        if cancelled:
            raise asyncio.CancelledError()
        raise RuntimeError("scripted dispatch error")

    run.observe(_Trigger, fail)

    async def broken(branch, _instruction):
        await branch.emit(_Trigger(text="fail within the actual dispatch"))
        return "PASS"

    scripts["judge-B"] = broken
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await engine.judge(run, "B", "failed item")
    else:
        assert await engine.judge(run, "B", "failed item") is True

    # Session evidence arriving after failure is not a verdict from the next
    # judge, even when it names the same item.
    await run.session.emit(JudgeVerdict(subject="B", allow=False))

    async def silent(_branch, _instruction):
        return "PASS"

    scripts["judge-B"] = silent
    assert await engine.judge(run, "B", "next item") is True


async def test_delayed_child_session_emission_has_no_judge_authority(agents):
    scripts, _ = agents
    engine = Engine(judge_model="openai/scripted")
    run = engine.new_run()
    release = asyncio.Event()
    tasks = []

    async def delayed():
        await release.wait()
        await run.session.emit(JudgeVerdict(subject="B", allow=False))

    async def spawn(_event, _ctx):
        tasks.append(asyncio.create_task(delayed()))

    run.observe(_Trigger, spawn)

    async def first(branch, _instruction):
        await branch.emit(_Trigger(text="create a delayed child task"))
        return "PASS"

    async def second(_branch, _instruction):
        release.set()
        await asyncio.gather(*tasks)
        return "PASS"

    scripts.update({"judge-A": first, "judge-B": second})
    assert await engine.judge(run, "A", "first") is True
    assert await engine.judge(run, "B", "second") is True


@pytest.mark.parametrize("recollect", [False, True])
async def test_same_object_twice_in_one_bundle_accepts_its_first_occurrence(
    agents, monkeypatch, recollect
):
    scripts, _ = agents
    engine, run, _first, target, _extracted, _notices = _dedup_run(monkeypatch)
    same = DedupChecked(finding_ref=target.eid, verdict="new")

    class Bundle(BaseModel):
        first: DedupChecked
        second: DedupChecked

    recollected = []

    def notify(event):
        if recollect and event["type"] == "DedupChecked" and not recollected:
            recollected.append(True)
            run.collect(same)

    run.on_event = notify

    async def search(branch, _instruction):
        bundle = Bundle(first=same, second=same)
        assert bundle.first is bundle.second is same
        await branch.emit(StructuredOutput(data=bundle))
        return ""

    scripts[f"dedup-{target.eid}"] = search
    await engine._dedup_search(run, target)
    await run.wait_quiescence()
    rows = [
        line for line in render_evidence(run).splitlines() if line.startswith(f"- {target.eid}: ")
    ]
    expected = [f"- {target.eid}: new — "] + [f"- {target.eid}: new (not accepted) — "] * (
        2 if recollect else 1
    )
    assert rows == expected
    assert [item["finding_ref"] for item in filing_queue(run)] == [target.eid]


async def test_handed_off_judge_branch_is_foreign_until_the_new_stage_binds_it(agents, monkeypatch):
    scripts, _ = agents
    engine = Engine(judge_model="openai/scripted")
    first_run = engine.new_run()
    handed_off = await first_run.make_agent("critic", name="old judge")
    await handed_off.emit(JudgeVerdict(subject="old", allow=False))
    first_count = len(first_run.by_type(JudgeVerdict))
    first_run.session.remove_branch(handed_off.id)
    second_run = engine.new_run()
    second_run.session.include_branches(handed_off)

    async def other_judge(branch, _instruction):
        await handed_off.emit(JudgeVerdict(subject="B", allow=False))
        await branch.emit(JudgeVerdict(subject="B", allow=True))
        return "REJECT"

    scripts["judge-B"] = other_judge
    assert await engine.judge(second_run, "B", "new session item") is True
    assert len(first_run.by_type(JudgeVerdict)) == first_count
    assert len(second_run.by_type(JudgeVerdict)) == 2

    async def reuse(_spec, **_kwargs):
        return handed_off

    monkeypatch.setattr(engine_mod, "create_agent", reuse)

    async def rebound(branch, _instruction):
        assert branch is handed_off
        await branch.emit(JudgeVerdict(subject="C", allow=False))
        return "PASS"

    scripts["judge-C"] = rebound
    assert await engine.judge(second_run, "C", "activated handed-off judge") is False
    assert len(first_run.by_type(JudgeVerdict)) == first_count


async def test_handed_off_search_branch_is_foreign_until_the_new_stage_binds_it(
    agents, monkeypatch
):
    scripts, made = agents
    first_engine, first_run, first, _unused, _out, _notices = _dedup_run(monkeypatch)

    async def old_search(branch, _instruction):
        await branch.emit(DedupChecked(finding_ref=first.eid, verdict="duplicate"))
        return ""

    scripts[f"dedup-{first.eid}"] = old_search
    await first_engine._dedup_search(first_run, first)
    handed_off = made[-1]
    first_count = len(first_run.events_of(DedupChecked))
    first_run.session.remove_branch(handed_off.id)
    engine, run, other, target, extracted, _notices = _dedup_run(monkeypatch)
    run.session.include_branches(handed_off)

    async def other_search(branch, _instruction):
        await handed_off.emit(DedupChecked(finding_ref=target.eid, verdict="new"))
        await branch.emit(DedupChecked(finding_ref=other.eid, verdict="duplicate"))
        return ""

    scripts[f"dedup-{other.eid}"] = other_search
    await engine._dedup_search(run, other)
    assert target.eid not in run.dedup_verdicts
    assert f"{target.eid}: new (not accepted)" in render_evidence(run)

    async def reuse(_spec, **_kwargs):
        return handed_off

    monkeypatch.setattr(engine_mod, "create_agent", reuse)

    async def rebound(branch, _instruction):
        assert branch is handed_off
        await branch.emit(DedupChecked(finding_ref=target.eid, verdict="duplicate"))
        return ""

    scripts[f"dedup-{target.eid}"] = rebound
    await engine._dedup_search(run, target)
    await run.wait_quiescence()
    assert run.dedup_verdicts[target.eid].verdict == "duplicate"
    assert extracted == []
    assert filing_queue(run) == []
    assert len(first_run.events_of(DedupChecked)) == first_count


@pytest.mark.parametrize("stage", ["judge", "dedup"])
@pytest.mark.parametrize("path", ["background_message", "async_handler", "async_gate"])
async def test_async_branch_emission_paths_preserve_typed_authority(
    agents, monkeypatch, stage, path
):
    scripts, _ = agents
    if stage == "judge":
        engine = Engine(judge_model="openai/scripted")
        run = engine.new_run()
        subject, key = "B", "judge_verdict"
        fields = {"subject": subject, "allow": False}
    else:
        engine, run, _other, finding, extracted, _notices = _dedup_run(monkeypatch)
        subject, key = finding.eid, "dedup_checked"
        fields = {"finding_ref": subject, "verdict": "duplicate"}
    suspended = []

    if path == "async_gate":

        async def gate(_payload):
            await asyncio.sleep(0)
            suspended.append("gate")
            return True

        run.session.gate(gate)

    async def operate(branch, _instruction):
        if path == "background_message":
            reply = AssistantResponse.from_response(
                "```json\n" + json.dumps({key: fields}) + "\n```"
            )
            await branch.msgs.a_add_message(assistant_response=reply)
            await branch.drain_signals()
        elif path == "async_handler":

            async def emit_after_await(_event, _ctx):
                await asyncio.sleep(0)
                suspended.append("handler")
                await _emit_model(branch, key, **fields)

            run.observe(_Trigger, emit_after_await)
            await branch.emit(_Trigger(text="emit from an awaited child handler"))
        else:
            await _emit_model(branch, key, **fields)
        return "PASS"

    scripts[f"{stage}-{subject}"] = operate
    if stage == "judge":
        assert await engine.judge(run, subject, "item") is False
    else:
        await engine._dedup_search(run, finding)
        await run.wait_quiescence()
        assert run.dedup_verdicts[subject].verdict == "duplicate"
        assert extracted == []
        assert filing_queue(run) == []
    if path == "async_gate":
        assert suspended == ["gate", "gate"]
    elif path == "async_handler":
        assert suspended == ["handler"]
