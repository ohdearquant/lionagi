# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The input cap and the host-side context fold of an API-model `li agent` leg.

A leg runs under ``max_input_tokens``: a profile's whole number, ``auto`` (the model's first
price-tier boundary, the default) or ``none``. Before a request would pass fold_at of the cap,
older tool calls and results are replaced by a host-written list of them; a request the cap
refuses anyway is folded and the leg continues; a refusal the fold cannot get under the cap
fails the run. Every fold is recorded in run.json. The endpoint is scripted, so no request
leaves the process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lionagi._errors import ConfigurationError, InputTokenCapError
from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
from lionagi.service.imodel import iModel
from tests.cli import test_agent_api_provider as _provider_tests
from tests.cli.test_agent_api_provider import (
    _answer,
    _last_round,
    _last_user_text,
    _profile,
    _read_round,
    _text,
)

leg = _provider_tests.leg  # the scripted-endpoint fixture the provider tests run on

_CAP = 20_000
_SUMMARY_PROMPT = "Write a concise, factual summary of the agent work below"
_RECORD_KEYS = {
    "at",
    "reason",
    "stage",
    "kept_tail",
    "messages_folded",
    "tool_calls",
    "tool_calls_total",
    "summary_source",
    "estimate_before",
    "estimate_after",
    "cap",
    "input_scale",
    "outcome",
    "target_reached",
    "summary_usage",
}
# The first request carries the fixed prompt alone and stays under it; the second carries one
# file of these lines and passes it, and keeps that file (the newest tool result) after a fold.
_ONE_FILE_LINES = 300
_SMALL_CAP = 8_000
# Over the unfolded run's largest request divided by fold_at, under 1.5 times it: the run folds
# only when the provider counts more than the cap does.
_SCALED_CAP = 33_000


def _haiku(cap: int | None) -> iModel:
    return iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        max_input_tokens=cap,
    )


def _manifest(tmp_path: Path) -> dict:
    (path,) = (tmp_path / "runs").glob("*/run.json")
    return json.loads(path.read_text())


# ── the profile key ───────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [50_000, "auto", "none"])
def test_a_profile_s_input_cap_is_read_with_the_read_set(value):
    import lionagi.cli.agent as agent_mod

    launch = agent_mod._profile_api_launch(_profile(api_tools="read", max_input_tokens=value))
    assert launch["max_input_tokens"] == value


def test_an_unset_input_cap_reads_as_auto():
    import lionagi.cli.agent as agent_mod

    assert agent_mod._profile_api_launch(_profile(api_tools="read"))["max_input_tokens"] == "auto"


@pytest.mark.parametrize("value", [0, -5, True, 1.5, "100k", "None", None])
def test_an_input_cap_that_is_no_policy_is_refused_before_a_run_exists(value):
    import lionagi.cli.agent as agent_mod

    with pytest.raises(ConfigurationError, match="max_input_tokens"):
        agent_mod._profile_api_launch(_profile(api_tools="read", max_input_tokens=value))


def test_an_input_cap_without_the_read_set_is_refused():
    import lionagi.cli.agent as agent_mod

    with pytest.raises(ConfigurationError, match="max_input_tokens but no api_tools"):
        agent_mod._profile_api_launch(_profile(max_input_tokens=50_000))


def test_a_launch_record_written_before_the_cap_existed_reads_as_auto():
    import lionagi.cli.agent as agent_mod

    record = {"tools": "read", "max_extensions": 3, "loop": "react"}
    assert agent_mod._recorded_api_launch(record)["max_input_tokens"] == "auto"
    with pytest.raises(ConfigurationError, match="input cap"):
        agent_mod._recorded_api_launch({**record, "max_input_tokens": 0})


@pytest.mark.parametrize(
    ("policy", "model", "cap"),
    [
        # Priced by prompt length: the first tier's boundary.
        ("auto", _haiku(None), 100_000),
        # Priced flat: nothing to stay under.
        ("auto", iModel(provider="anthropic", model="claude-sonnet-5", api_key="x"), None),
        ("none", _haiku(None), None),
        (30_000, iModel(provider="anthropic", model="claude-sonnet-5", api_key="x"), 30_000),
    ],
)
def test_the_cap_a_leg_runs_under(policy, model, cap):
    import lionagi.cli.agent as agent_mod

    resolved, source = agent_mod._resolve_api_input_cap(policy, model)
    assert resolved == cap
    assert source


def test_auto_is_the_pricing_table_s_first_tier():
    import lionagi.cli.agent as agent_mod
    from lionagi.service.accounts._pricing import CURRENT_REVISION

    tiers = CURRENT_REVISION.models["claude-haiku-5-5"].tiers
    assert len(tiers) > 1
    assert agent_mod._resolve_api_input_cap("auto", _haiku(None))[0] == tiers[0].max_prompt_tokens


# ── end to end through _run_agent ─────────────────────────────────────────


def _reading_run(leg, monkeypatch, *, reads=6, lines=150, cap=_CAP):
    """A leg that reads ``reads`` files of ``lines`` lines, one per round, then answers.

    Returns the files' paths, the cap's own estimate of every agent request built, in order (a
    refused request is built and counted, never sent), and the fold's summary requests, which
    are answered apart from the leg's scripted rounds.
    """
    from lionagi.service.connections.api_calling import APICalling

    # A deep directory: each read's arguments are longer than one clipped value, so the note
    # must keep the path whole to keep the file name.
    deep = Path("d" * 90) / ("e" * 90)
    (leg.workspace / deep).mkdir(parents=True)
    files = []
    for k in range(reads):
        rel = deep / f"f{k}.txt"
        (leg.workspace / rel).write_text(
            "".join(f"line {i}: alpha beta gamma delta {i * 7 + k}\n" for i in range(lines))
        )
        files.append(str(rel))
    scripted = AnthropicMessagesEndpoint.call
    summaries: list[dict] = []

    async def call(self, request, *a, **kw):
        if _last_user_text(request).startswith(_SUMMARY_PROMPT):
            summaries.append(request)
            return _text("SUMMARY: the files read so far")
        return await scripted(self, request, *a, **kw)

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    real = APICalling.input_token_estimate.fget
    built: list[int] = []

    def counted(self):
        n = real(self)
        if not _last_user_text(self.payload).startswith(_SUMMARY_PROMPT):
            built.append(n)
        return n

    monkeypatch.setattr(APICalling, "input_token_estimate", property(counted))
    leg.replies = [_read_round(f) for f in files] + [_last_round(), _answer("read every file")]
    profile = _profile(api_tools="read", max_input_tokens=cap)
    return files, profile, built, summaries


@pytest.mark.asyncio
async def test_a_run_past_the_threshold_folds_its_tool_records_and_completes(
    leg, monkeypatch, tmp_path
):
    # Nine reads exercise more than one threshold crossing after the first fold's two
    # shrinking passes. Six reads now finish after that first, more effective fold.
    files, profile, built, summaries = _reading_run(leg, monkeypatch, reads=9)

    result, _p, _b, status, _s = await leg.run(profile, "read every file")

    assert (result, status) == ("read every file", "completed")
    assert leg.replies == [], "every scripted round is consumed: none was refused or added"
    assert len(leg.payloads) == len(built) == len(files) + 2
    # Without the fold these reads pass the cap: the input grows by one file per round.
    assert max(built) <= _CAP
    folds = _manifest(tmp_path)["context_folds"]
    assert folds, "the run crossed fold_at and nothing was folded"
    for record in folds:
        assert set(record) == _RECORD_KEYS | (
            {"clipped", "clipped_total"} if record["stage"] == "clip" else set()
        )
        assert record["reason"] == "threshold"
        assert record["cap"] == _CAP
        if record["outcome"] == "landed":
            assert record["estimate_after"] < record["estimate_before"]
        else:
            assert record["outcome"] == "rolled_back"
            assert record["estimate_after"] >= record["estimate_before"]
        if record["stage"] == "clip":
            assert record["messages_folded"] == 0
            assert (
                record["summary_source"] is record["kept_tail"] is record["summary_usage"] is None
            )
            assert len(record["clipped"]) == min(record["clipped_total"], 50)
            continue
        assert record["messages_folded"] > 0
        # Tool records are host-listed; reasoning is summarized by the (scripted) model.
        expected = "host" if record["stage"] == "tool_io" else "model"
        assert record["summary_source"] == expected, folds
    # Tool records go first: the summary stage runs only after tool_io left the estimate high.
    assert folds[0]["stage"] == "tool_io", folds
    assert len(summaries) == sum(r["stage"] == "all" for r in folds)
    # A fold starts at fold_at and stops at or under the target. Its stages chain: each stage
    # starts from the estimate the previous one left, and the next request raises it again.
    calls: list[list[dict]] = []
    for record in folds:
        previous = calls[-1][-1] if calls else None
        kept_estimate = (
            (
                previous["estimate_after"]
                if previous["outcome"] == "landed"
                else previous["estimate_before"]
            )
            if previous
            else None
        )
        if calls and record["estimate_before"] == kept_estimate:
            calls[-1].append(record)
        else:
            calls.append([record])
    assert len(calls) >= 2, folds
    assert all(c[0]["estimate_before"] >= 0.8 * _CAP for c in calls), folds
    assert all(c[-1]["target_reached"] is True for c in calls), folds
    # The record and the note the model reads both keep which files were read.
    recorded = [c for r in folds for c in r["tool_calls"]]
    assert {c["function"] for c in recorded} == {"reader"}
    assert any(files[0] in c["arguments"] for c in recorded)
    later = json.dumps(leg.payloads[-1], ensure_ascii=False)
    assert "[CONTEXT COMPACTION]" in later
    assert files[0] in later
    assert "get_messages" in later


@pytest.mark.asyncio
async def test_the_fold_estimate_is_the_cap_s_count_of_the_request_it_precedes(
    leg, monkeypatch, tmp_path
):
    from lionagi.agent.context_fold import ContextFold

    _files, profile, built, _summaries = _reading_run(leg, monkeypatch)
    estimates: list[int] = []
    handler = ContextFold.on_api_pre_call

    async def recording(self, **kw):
        await handler(self, **kw)
        estimates.append(self.estimate())

    monkeypatch.setattr(ContextFold, "on_api_pre_call", recording)

    await leg.run(profile, "read every file")

    assert _manifest(tmp_path)["context_folds"], "the comparison must cover requests after a fold"
    assert len(estimates) == len(built)
    # The first request has no recorded instruction yet: nothing to fold, estimate 0.
    assert estimates[0] == 0
    pairs = list(zip(estimates[1:], built[1:], strict=True))
    # The newest instruction stands in for the round being built: close for the tool rounds,
    # high (never low) for the final answer, whose instruction carries no tool schemas.
    for mine, cap_count in pairs[:-1]:
        assert abs(mine - cap_count) <= 0.02 * cap_count, (mine, cap_count, pairs)
    assert all(mine >= 0.98 * cap_count for mine, cap_count in pairs), pairs


@pytest.mark.asyncio
async def test_a_request_the_cap_refuses_is_folded_and_the_leg_continues(
    leg, monkeypatch, tmp_path
):
    from lionagi.agent.context_fold import ContextFold

    _files, profile, built, _summaries = _reading_run(leg, monkeypatch)

    async def missed(self, **kw):  # the threshold check misses: only the cap catches it
        return None

    monkeypatch.setattr(ContextFold, "on_api_pre_call", missed)

    result, _p, _b, status, _s = await leg.run(profile, "read every file")

    assert (result, status) == ("read every file", "completed")
    assert leg.replies == []
    refused = [n for n in built if n > _CAP]
    assert refused, "the cap refused nothing: the backstop was not exercised"
    # A refused request is built and counted, never sent.
    assert len(leg.payloads) == len(built) - len(refused)
    folds = _manifest(tmp_path)["context_folds"]
    assert folds and {r["reason"] for r in folds} == {"refused"}
    assert folds[0]["estimate_before"] >= refused[0]
    # The pass after a fold is told what happened and to continue.
    resumed = [p for p in leg.payloads if "The host folded part of your context" in str(p)]
    assert resumed


@pytest.mark.asyncio
async def test_a_refusal_the_fold_cannot_get_under_the_cap_fails_the_run(
    leg, monkeypatch, tmp_path
):
    from lionagi.agent.context_fold import ContextFold

    # The first refusal can clip the first read and resume. After the second read, both
    # bodies reach their 1024-character floors and the remaining fixed prompt still refuses.
    files, profile, built, summaries = _reading_run(
        leg, monkeypatch, reads=2, lines=_ONE_FILE_LINES, cap=_SMALL_CAP
    )

    async def missed(self, **kw):
        return None

    monkeypatch.setattr(ContextFold, "on_api_pre_call", missed)

    with pytest.raises(InputTokenCapError):
        await leg.run(profile, "read every file")

    refused = [n for n in built if n > _SMALL_CAP]
    assert refused
    assert len(leg.payloads) == len(built) - len(refused)
    # Both read decisions were sent, but the scripted finish and answer were never sent.
    assert len(leg.payloads) == len(files)
    assert leg.replies == [_last_round(), _answer("read every file")]
    from lionagi.service.connections.api_calling import estimate_payload_input_tokens

    assert all(estimate_payload_input_tokens(p) <= _SMALL_CAP for p in leg.payloads)
    final = _manifest(tmp_path)
    assert final["status"] == leg.status == "failed"
    assert f"max_input_tokens={_SMALL_CAP:,}" in final["error"]
    folds = final["context_folds"]
    assert folds and {r["reason"] for r in folds} == {"refused"}
    # The final clipped view remains over the cap: no final model decision is sent.
    assert len(summaries) == len([r for r in folds if r["stage"] == "all"]) <= 2
    assert folds[-1]["estimate_after"] > _SMALL_CAP


@pytest.mark.asyncio
async def test_a_leg_without_a_cap_installs_no_fold(leg, tmp_path):
    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("the notes say hello")]

    _r, _p, _b, status, _s = await leg.run(_profile(api_tools="read", max_input_tokens="none"))

    assert status == "completed"
    assert leg.branch.chat_model.max_input_tokens is None
    assert _manifest(tmp_path)["context_folds"] == []


@pytest.mark.asyncio
async def test_an_auto_cap_reaches_the_model_and_its_working_context(leg, tmp_path):
    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("the notes say hello")]

    await leg.run(_profile(api_tools="read"))

    model = leg.branch.chat_model
    assert model.max_input_tokens == 100_000
    assert model.endpoint.config.context_window == 100_000
    assert _manifest(tmp_path)["context_folds"] == []


@pytest.mark.asyncio
async def test_a_resume_takes_the_input_cap_its_profile_names_now(leg):
    import lionagi.cli.agent as agent_mod

    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("the notes say hello")]
    _r, _p, branch_id, _s, _ = await leg.run(_profile(api_tools="read", max_input_tokens=50_000))
    assert leg.branch.metadata[agent_mod._API_LAUNCH_KEY]["max_input_tokens"] == 50_000

    leg.replies = [_last_round(), _answer("still hello")]
    resumed = await agent_mod._run_agent(
        None,
        "and now?",
        resume=branch_id,
        agent_name="reader-leg",
        cwd=str(leg.workspace),
    )
    assert resumed[3] == "completed"


# ── a refusal the loop caught ─────────────────────────────────────────────


def _swallowing_pass(monkeypatch, refused_model, refusal):
    """A loop pass that a refused request ends inside, caught there, the earlier text returned."""
    import lionagi.cli.agent as agent_mod

    async def swallowed(branch, instruction, loop, max_extensions):
        refused_model.input_cap_refusal = refusal
        return "an earlier round's text"

    monkeypatch.setattr(agent_mod, "_api_loop_pass", swallowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("refused_by", ["chat", "parse"])
async def test_a_refusal_the_loop_swallowed_still_fails_the_turn(tmp_path, monkeypatch, refused_by):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    branch = Branch(chat_model=_haiku(1_000), parse_model=_haiku(1_000))
    branch.metadata[agent_mod._API_LAUNCH_KEY] = {"tools": "read", "max_extensions": 1}
    refusal = InputTokenCapError("refused", details={"estimate": 9, "cap": 1})
    # The parse model's refusal is a repair refused somewhere above the loop.
    refused_model = branch.chat_model if refused_by == "chat" else branch.parse_model
    _swallowing_pass(monkeypatch, refused_model, refusal)

    with pytest.raises(InputTokenCapError) as exc_info:
        await agent_mod._run_api_turn(
            branch, "task", timeout=None, max_extensions=1, snapshot_dir=str(tmp_path)
        )
    assert exc_info.value is refusal
    # The turn's spend and snapshot are written before the refusal leaves it.
    assert "spend" in branch.metadata[agent_mod._API_LAUNCH_KEY]
    assert list(tmp_path.glob("*.json"))


# ── the fold on a branch ─────────────────────────────────────────────────


def _branch_with_reasoning(cap: int, turns: int = 10, *, detail_repeats: int = 600):
    from lionagi import Branch

    branch = Branch(chat_model=_haiku(cap))
    msgs = branch.msgs
    task = msgs.create_instruction(instruction="check src/app/core.py and report")
    msgs.messages.include(task)
    msgs.progression.include(task.id)
    for i in range(turns):
        note = msgs.create_assistant_response(
            assistant_response=f"step {i}: src/app/core.py line {i * 40} "
            + "detail " * detail_repeats
        )
        msgs.messages.include(note)
        msgs.progression.include(note.id)
    return branch, task


def _compiled_view_payload(branch):
    from lionagi.operations.chat._prepare import _prepare_run_kwargs
    from lionagi.operations.types import ChatParam
    from lionagi.protocols.messages import Instruction

    latest = next(
        branch.msgs.messages[uid]
        for uid in reversed(list(branch.progression))
        if isinstance(branch.msgs.messages[uid], Instruction)
    )
    _instruction, request = _prepare_run_kwargs(
        branch, None, ChatParam.from_branch(branch), ins=latest, imodel=branch.chat_model
    )
    payload, _headers = branch.chat_model.endpoint.create_payload(request=request)
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [True, False])
async def test_delivery_marks_only_the_view_carried_by_a_completed_main_request(completed):
    from lionagi.agent.context_fold import install_context_fold
    from lionagi.protocols.generic.event import EventStatus
    from lionagi.testing import LionAGIMockFactory

    branch = LionAGIMockFactory.create_mocked_branch(
        response="answer", status=EventStatus.COMPLETED if completed else EventStatus.FAILED
    )
    carried = await branch.msgs.a_add_message(instruction="earlier task")
    before = [str(uid) for uid in branch.progression]
    install_context_fold(branch, cap=1_000_000)
    original_invoke = branch.chat_model.invoke
    arrived = []

    async def invoke(**kwargs):
        assert branch.metadata["context_delivered"] == []
        arrived.append(await branch.msgs.a_add_message(instruction="arrived after compilation"))
        return await original_invoke(**kwargs)

    branch.chat_model.invoke = invoke
    if completed:
        assert await branch.chat("next request") == "answer"
        assert branch.metadata["context_delivered"] == before
        assert str(carried.id) in branch.metadata["context_delivered"]
    else:
        from lionagi._errors import ExecutionError

        with pytest.raises(ExecutionError):
            await branch.chat("next request")
        assert branch.metadata["context_delivered"] == []
    assert str(arrived[0].id) not in branch.metadata["context_delivered"]


@pytest.mark.asyncio
async def test_context_automatic_summary_never_marks_a_main_request_delivered():
    from lionagi.agent.context_fold import install_context_fold
    from lionagi.testing import LionAGIMockFactory
    from lionagi.tools.context.context import ContextTool

    branch = LionAGIMockFactory.create_mocked_branch(response="summary")
    await branch.msgs.a_add_message(instruction="task")
    await branch.msgs.a_add_message(assistant_response="earlier reasoning")
    install_context_fold(branch, cap=1_000_000)
    delivered = list(branch.metadata["context_delivered"])
    result = (
        await ContextTool()
        .bind(branch)
        .func_callable(action="compact", start=1, end=2, mode="all", auto=True)
    )
    assert result["success"] is True
    assert branch.metadata["context_delivered"] == delivered


def test_candidate_count_uses_the_real_compiler_without_admitting_candidate_messages():
    from lionagi.agent.context_fold import ContextFold
    from lionagi.protocols.messages import AssistantResponse

    branch, task = _branch_with_reasoning(cap=1_000_000, turns=1)
    fold = ContextFold(branch, cap=1_000_000)
    before = branch.to_dict()
    fired = []
    branch.on_message_added.append(fired.append)
    candidate = AssistantResponse(content={"assistant_response": "candidate-only body " * 100})
    count = fold._estimate_view([task, candidate])
    larger = fold._estimate_view(
        [task, AssistantResponse(content={"assistant_response": "larger " * 5000})]
    )
    assert larger > count > 0
    assert candidate.id not in branch.msgs.messages
    assert fired == []
    assert branch.to_dict() == before


def test_work_count_excludes_host_copies_but_keeps_tool_owned_provenance_keys():
    from lionagi.cli.agent import _tool_results
    from lionagi.protocols.messages import ActionResponse
    from lionagi.session.branch import Branch

    branch = Branch()
    original = ActionResponse(content={"function": "bash", "output": {"clipped_from": "tool data"}})
    copy = ActionResponse(
        content={"function": "bash", "output": "clip"}, metadata={"clipped_from": str(original.id)}
    )
    branch.msgs.messages.include([original, copy])
    assert _tool_results(branch) == 1


@pytest.mark.asyncio
async def test_undelivered_middle_result_and_its_call_survive_tool_io_compaction():
    from lionagi.agent.context_fold import ContextFold
    from lionagi.session.branch import Branch
    from lionagi.tools.context.context import ContextRequest

    branch = Branch(system="system", chat_model=_haiku(1_000_000))
    await branch.msgs.a_add_message(instruction="task")
    old_request = await branch.msgs.a_add_message(
        action_function="reader", action_arguments={"path": "old.py"}
    )
    old_result = await branch.msgs.a_add_message(
        action_request=old_request, action_output="old-rendered-evidence " * 3000
    )
    new_request = await branch.msgs.a_add_message(
        action_function="reader", action_arguments={"path": "new.py"}
    )
    new_result = await branch.msgs.a_add_message(
        action_request=new_request, action_output="unread " * 500
    )
    # The real compiler carries tool results in the following instruction. An intervening
    # assistant turn ensures that instruction is rendered rather than dropped as consecutive.
    await branch.msgs.a_add_message(assistant_response="ready to inspect both results")
    await branch.msgs.a_add_message(instruction="latest task")
    await branch.msgs.a_add_message(assistant_response="latest reasoning")
    branch.metadata["context_delivered"] = [
        str(uid) for uid in branch.progression if uid != new_result.id
    ]
    fold = ContextFold(branch, cap=1_000_000)
    original_request = new_request.to_dict()
    before_payload = json.dumps(_compiled_view_payload(branch))
    before_count = fold.estimate()
    assert "old-rendered-evidence" in before_payload and "unread " in before_payload
    assert await fold._fold_once("tool_io", 2, "threshold")
    after_payload = json.dumps(_compiled_view_payload(branch))
    assert "old-rendered-evidence" not in after_payload and "unread " in after_payload
    assert fold.estimate() < before_count
    active = list(branch.progression)
    assert old_request.id not in active and old_result.id not in active
    assert new_request.id in active and new_result.id in active
    assert active.index(new_result.id) == active.index(new_request.id) + 1
    assert new_request.to_dict() == original_request
    assert fold.records[-1]["tool_calls_total"] == 1
    assert fold.records[-1]["tool_calls"][0]["arguments"] == '{"path": "old.py"}'
    assert "_keep" not in ContextRequest.model_fields


@pytest.mark.asyncio
async def test_the_summary_stage_sends_the_span_alone_and_keeps_the_summary(monkeypatch):
    from lionagi.agent.context_fold import ContextFold

    branch, task = _branch_with_reasoning(cap=8_000)
    durable_before = len(branch.msgs.messages)
    sent: list[dict] = []

    async def fake_call(self, request, *a, **kw):
        sent.append(request)
        return _text("SUMMARY: src/app/core.py checked through line 360")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", fake_call)
    fold = ContextFold(branch, cap=8_000)

    assert await fold.fold(reason="threshold")

    assert sent, "the summary stage sent no request"
    assert all(len(p["messages"]) == 1 and "system" not in p for p in sent)
    summary_records = [record for record in fold.records if record["stage"] == "all"]
    assert summary_records
    assert {r["stage"] for r in fold.records} == {"clip", "all"}
    assert {r["summary_source"] for r in summary_records} == {"model"}
    assert fold.estimate() <= 0.6 * 8_000
    active = list(branch.progression)
    assert active[0] == task.id, "the task instruction stays first and verbatim"
    notes = [branch.msgs.messages[u].rendered for u in active[1:]]
    assert any("SUMMARY: src/app/core.py checked through line 360" in str(n) for n in notes)
    # Nothing is deleted: the durable record gains the notes and loses nothing.
    assert len(branch.msgs.messages) == durable_before + len(summary_records)


@pytest.mark.asyncio
async def test_a_refused_summary_request_leaves_no_refusal_on_the_model(monkeypatch):
    from lionagi.agent.context_fold import ContextFold
    from lionagi.service.connections.api_calling import APICalling

    async def never_sent(self, request, *a, **kw):
        raise AssertionError("a refused request must not be sent")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", never_sent)
    branch, _task = _branch_with_reasoning(cap=8_000)
    real = APICalling.input_token_estimate.fget

    def refuse_the_summary(self):
        if len(self.payload.get("messages") or []) == 1:
            return 10**9
        return real(self)

    monkeypatch.setattr(APICalling, "input_token_estimate", property(refuse_the_summary))
    fold = ContextFold(branch, cap=8_000)

    assert await fold.fold(reason="threshold")

    assert {r["summary_source"] for r in fold.records if r["stage"] != "clip"} == {"host"}
    assert branch.chat_model.input_cap_refusal is None


@pytest.mark.asyncio
async def test_a_refusal_calibrates_the_estimate_and_always_folds(monkeypatch):
    from lionagi.agent.context_fold import ContextFold

    async def fake_call(self, request, *a, **kw):
        return _text("SUMMARY: nothing new")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", fake_call)
    branch, _task = _branch_with_reasoning(cap=1_000_000)
    fold = ContextFold(branch, cap=1_000_000)
    raw = fold.estimate()
    assert raw < 0.6 * 1_000_000

    # Under target by the estimate, yet refused: the refusal's count wins.
    assert await fold.fold(reason="refused", observed=2 * raw)

    assert fold.records and fold.records[0]["estimate_before"] >= 2 * raw - 1
    assert fold._scale == pytest.approx(2.0, rel=0.01)


@pytest.mark.asyncio
async def test_a_refusal_calibrates_on_top_of_the_model_s_scale(monkeypatch):
    from lionagi.agent.context_fold import ContextFold

    async def fake_call(self, request, *a, **kw):
        return _text("SUMMARY: nothing new")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", fake_call)
    branch, _task = _branch_with_reasoning(cap=1_000_000)
    fold = ContextFold(branch, cap=1_000_000)
    raw = fold.estimate()

    branch.chat_model.input_token_scale = 1.5
    assert fold.estimate() == int(raw * 1.5)
    assert await fold.fold(reason="refused", observed=3 * raw)

    assert fold._scale == pytest.approx(2.0, rel=0.01)
    assert fold.records[0]["input_scale"] == pytest.approx(3.0, rel=0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("ratio", [1.0, 1.5])
async def test_the_provider_s_count_moves_the_fold_line(leg, monkeypatch, tmp_path, ratio):
    from lionagi.service.connections.api_calling import estimate_payload_input_tokens

    _files, profile, built, _summaries = _reading_run(leg, monkeypatch, cap=_SCALED_CAP)
    routed = AnthropicMessagesEndpoint.call
    reported: list[int] = []

    async def reporting(self, request, *a, **kw):
        response = await routed(self, request, *a, **kw)
        response["usage"]["input_tokens"] = int(ratio * estimate_payload_input_tokens(request))
        reported.append(response["usage"]["input_tokens"])
        return response

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", reporting)

    _result, _p, _b, status, _s = await leg.run(profile, "read every file")

    assert status == "completed"
    assert leg.replies == []
    folds = _manifest(tmp_path)["context_folds"]
    if ratio == 1.0:
        assert folds == []
        assert max(built) < 0.8 * _SCALED_CAP, built
        assert 1.5 * max(built) > 0.8 * _SCALED_CAP, built
    else:
        assert folds, built
        assert {r["reason"] for r in folds} == {"threshold"}
        assert all(r["input_scale"] == pytest.approx(1.5, rel=0.02) for r in folds), folds
        # On the provider's own count, every request sent stayed under the cap.
        assert max(reported) <= _SCALED_CAP, reported


@pytest.mark.asyncio
async def test_a_span_holding_only_the_task_sends_no_summary_request(monkeypatch):
    from lionagi.agent.context_fold import ContextFold

    sent: list[dict] = []

    async def fake_call(self, request, *a, **kw):
        sent.append(request)
        return _text("SUMMARY: of nothing")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", fake_call)
    # The task and the two newest answers, which the shortest tail keeps: nothing to fold.
    branch, _task = _branch_with_reasoning(cap=8_000, turns=2)
    fold = ContextFold(branch, cap=8_000)

    assert await fold.fold(reason="refused", observed=10**6) is False

    assert sent == []
    assert all(
        record["stage"] == "clip"
        and record["outcome"] == "rolled_back"
        and record["clipped_total"] == 0
        and record["estimate_after"] == record["estimate_before"]
        for record in fold.records
    )


@pytest.mark.asyncio
async def test_a_fold_that_cuts_between_a_call_and_its_result_keeps_the_call_in_view(
    monkeypatch,
):
    # Tool calls reach the model only as their results, which carry the call's function and
    # arguments, so a cut that folds a call and keeps its result loses nothing it reads.
    from lionagi import Branch
    from lionagi.agent.context_fold import ContextFold
    from lionagi.operations.chat._prepare import _prepare_run_kwargs
    from lionagi.operations.types import ChatParam
    from lionagi.protocols.generic.progression import Progression
    from lionagi.protocols.messages import ActionRequest, ActionResponse

    branch = Branch(chat_model=_haiku(_CAP))
    msgs = branch.msgs
    added = [msgs.create_instruction(instruction="read the three files and report")]
    added.append(msgs.create_assistant_response(assistant_response="reading the three files"))
    for i in range(3):
        call = {"function": "read_file", "arguments": {"path": f"src/f{i}.py"}}
        added.append(ActionRequest(content=call))
        added.append(ActionResponse(content={**call, "output": "line\n" * 1000}))
    added.append(msgs.create_instruction(instruction="continue"))
    for m in added:
        msgs.messages.include(m)
        msgs.progression.include(m.id)
    request, result = added[-3], added[-2]
    original_view = list(branch.progression)
    sent = []

    async def completed(self, request, *args, **kwargs):
        sent.append(request)
        return _text("all three results inspected")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", completed)
    from lionagi.agent.context_fold import install_context_fold

    fold = install_context_fold(branch, cap=_CAP)
    await branch.chat("inspect the three results")
    assert len(sent) == 1
    assert all(f"src/f{i}.py" in json.dumps(sent[0]) for i in range(3))
    assert set(map(str, original_view)) <= set(branch.metadata["context_delivered"])
    # Return to that delivered view so the cut falls between the third call and result.
    branch.metadata["current_progression"] = Progression(order=original_view)

    assert await fold._fold_once("tool_io", 2, "threshold")

    active = list(branch.progression)
    assert request.id not in active and result.id in active
    _ins, kw = _prepare_run_kwargs(
        branch, None, ChatParam.from_branch(branch), ins=added[-1], imodel=branch.chat_model
    )
    payload, _headers = branch.chat_model.endpoint.create_payload(request=kw)
    sent = json.dumps(payload["messages"])
    assert "src/f2.py" in sent and "read_file" in sent
    note = next(msgs.messages[u] for u in active if u not in {m.id for m in added})
    assert "src/f0.py" in str(note.rendered) and "src/f1.py" in str(note.rendered)


def test_the_note_clips_long_values_and_keeps_paths_whole():
    from lionagi.agent.context_fold import _short

    deep = "/w/" + "/".join(["segment" * 4] * 12) + "/target_file.py"
    assert len(deep) > 160
    shown = json.loads(_short({"action": "write", "file_path": deep, "content": "x" * 50_000}))

    assert shown["file_path"] == deep
    assert shown["action"] == "write"
    assert shown["content"].startswith("x" * 160)
    assert shown["content"].endswith("... (50,000 chars)")
    assert len(shown["content"]) < 200

    # Path arguments go by many names; each keeps its value whole, a list of them each element.
    for key in ("file", "dir", "directory", "target", "pattern", "root", "cwd", "notebook_path"):
        assert json.loads(_short({key: deep}))[key] == deep, key
    many = ["/w/" + "x" * 200 + f"/f{i}.py" for i in range(40)]
    listed = json.loads(_short({"files": many}))["files"]
    assert listed[:3] == many[:3]
    assert listed[-1] == f"... {40 - (len(listed) - 1)} more"
    assert len(json.dumps(listed)) < 5_000
    assert json.loads(_short({"note": deep}))["note"].endswith(f"... ({len(deep):,} chars)")


def test_a_second_install_returns_the_branch_s_fold():
    from lionagi.agent.context_fold import install_context_fold
    from lionagi.hooks.bus import HookPoint
    from lionagi.session.branch import Branch

    branch = Branch()
    first = install_context_fold(branch, cap=1_000)
    assert install_context_fold(branch, cap=1_000) is first
    handlers = [
        h for point, h in branch._pending_hook_bus_entries if point == HookPoint.API_PRE_CALL
    ]
    assert handlers == [first.on_api_pre_call]
    assert branch._hooks is not None, "a branch with no bus gets one, or the fold never fires"
    with pytest.raises(ValueError, match="cap"):
        install_context_fold(branch, cap=2_000)


async def _clip_fixture(outputs, cap=6000):
    from lionagi.session.branch import Branch

    branch = Branch(system="Keep the evidence.", chat_model=_haiku(cap))
    await branch.msgs.a_add_message(instruction="inspect every result and answer")
    originals = []
    requests = []
    for number, output in enumerate(outputs):
        request = await branch.msgs.a_add_message(
            action_function=f"tool_{number:02}_end", action_arguments={"item": number}
        )
        requests.append(request)
        originals.append(
            await branch.msgs.a_add_message(action_request=request, action_output=output)
        )
    return branch, requests, originals


def _active_results(branch):
    from lionagi.protocols.messages import ActionResponse

    return [
        branch.msgs.messages[uid]
        for uid in branch.progression
        if isinstance(branch.msgs.messages[uid], ActionResponse)
    ]


@pytest.mark.asyncio
async def test_amendment2_newest_100045_result_reaches_target_in_the_actual_request(monkeypatch):
    from lionagi.agent.context_fold import install_context_fold

    output = ("evidence é🦁 0123456789\n" * 6000)[:100045]
    branch, requests, originals = await _clip_fixture([output])
    original = originals[0]
    before = original.to_dict()
    request_before = requests[0].to_dict()
    fired = []
    branch.on_message_added.append(fired.append)
    sent = []

    async def call(self, request, *args, **kwargs):
        assert _SUMMARY_PROMPT not in json.dumps(request)
        sent.append(request)
        return _text("read the result")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    fold = install_context_fold(branch, cap=6000)
    assert await branch.chat("inspect every result and answer") == "read the result"
    assert fold.estimate() <= 3600
    assert len(sent) == 1
    from lionagi.service.connections.api_calling import estimate_payload_input_tokens

    assert estimate_payload_input_tokens(sent[0]) <= 3600
    assert 'context(action="read_result"' in _last_user_text(sent[0])
    copies = _active_results(branch)
    assert len(copies) == 1 and copies[0].metadata["clipped_from"] == str(original.id)
    assert copies[0].content.action_request_id is None
    assert [message for message in fired if message.metadata.get("clipped_from")] == copies
    assert original.to_dict() == before
    assert requests[0].to_dict() == request_before
    clip = next(record for record in fold.records if record["stage"] == "clip")
    assert clip["outcome"] == "landed" and clip["target_reached"] is True
    assert clip["messages_folded"] == 0 and clip["kept_tail"] is None
    assert clip["summary_source"] is clip["summary_usage"] is None


@pytest.mark.asyncio
async def test_amendment2_sixteen_unread_results_all_reach_the_next_compiled_request(monkeypatch):
    from lionagi.agent.context_fold import install_context_fold

    branch, requests, originals = await _clip_fixture(
        [(f"result-{number:02} unique data 0123456789\n" * 1200) for number in range(16)], cap=20000
    )
    sent = []

    async def call(self, request, *args, **kwargs):
        assert _SUMMARY_PROMPT not in json.dumps(request)
        sent.append(request)
        return _text("all sixteen retained")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    fold = install_context_fold(branch, cap=20000)
    await branch.chat("inspect every result and answer")
    assert fold.estimate() <= 12000
    active = _active_results(branch)
    assert len(active) == 16
    assert {message.metadata.get("clipped_from", str(message.id)) for message in active} == {
        str(message.id) for message in originals
    }
    assert all(request.id in branch.progression for request in requests)
    compiled = _last_user_text(sent[0])
    assert all(f"tool_{number:02}_end" in compiled for number in range(16))
    assert not any(record["stage"] in ("tool_io", "all") for record in fold.records)


@pytest.mark.asyncio
async def test_amendment2_refusal_and_fold_summary_do_not_deliver_and_legacy_fallback_is_exact(
    monkeypatch,
):
    from lionagi.agent.context_fold import ContextFold, install_context_fold
    from lionagi.service.connections.api_calling import APICalling
    from lionagi.testing import LionAGIMockFactory

    branch = LionAGIMockFactory.create_mocked_branch(response="answer")
    earlier = await branch.msgs.a_add_message(instruction="earlier")
    answer = await branch.msgs.a_add_message(assistant_response="last completed answer")
    later = await branch.msgs.a_add_message(instruction="later")
    await branch.msgs.a_add_message(assistant_response="[CONTEXT COMPACTION] a note")
    install_context_fold(branch, cap=1000000)
    assert branch.metadata["context_delivered"] == [str(earlier.id)]
    assert str(answer.id) not in branch.metadata["context_delivered"]
    assert str(later.id) not in branch.metadata["context_delivered"]
    refused_branch, _requests, _originals = await _clip_fixture(["unread evidence"], cap=6000)
    refused_fold = install_context_fold(refused_branch, cap=6000)
    monkeypatch.setattr(APICalling, "input_token_estimate", property(lambda self: 10**9))
    with pytest.raises(InputTokenCapError):
        await refused_branch.chat("refused")
    assert refused_branch.metadata["context_delivered"] == []
    assert branch.metadata["context_delivered"] == [str(earlier.id)]
    # The fold's summary invokes the model directly, so neither PRE nor POST marks it.
    other, _task = _branch_with_reasoning(1000000, turns=2)
    other_fold = ContextFold(other, cap=1000000)
    before = list(other.metadata["context_delivered"])
    monkeypatch.undo()

    async def call(self, request, *args, **kwargs):
        return _text("summary only")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    assert await other_fold._model_summary(list(other.msgs.messages)) == "summary only"
    assert other.metadata["context_delivered"] == before
    assert refused_fold._request_views.get() == ()


@pytest.mark.asyncio
async def test_amendment2_restoring_a_note_and_middle_message_uses_replaced_chronology():
    import math

    from lionagi.agent.context_fold import ContextFold, estimate_next_request_tokens
    from lionagi.session.branch import Branch
    from lionagi.tools.context.context import ContextTool

    branch = Branch(system="system", chat_model=_haiku(1000000))
    await branch.msgs.a_add_message(instruction="task")
    await branch.msgs.a_add_message(assistant_response="earlier reasoning " * 100)
    middle = await branch.msgs.a_add_message(instruction="middle message")
    request = await branch.msgs.a_add_message(
        action_function="reader", action_arguments={"path": "a"}
    )
    original = await branch.msgs.a_add_message(
        action_request=request, action_output="raw evidence " * 5000
    )
    later = await branch.msgs.a_add_message(instruction="later message")
    context = ContextTool().bind(branch).func_callable
    compacted = await context(
        action="compact", start=2, end=3, mode="all", summary="earlier reasoning kept"
    )
    assert compacted["success"] is True
    note = branch.msgs.messages[list(branch.msgs.progression)[-1]]
    assert note.metadata["context_first_index"] == 2
    await context(action="evict", start=list(branch.progression).index(middle.id))
    fold = ContextFold(branch, cap=math.ceil(estimate_next_request_tokens(branch) / 0.8))
    assert await fold._clip_once("threshold")
    copy = _active_results(branch)[0]
    restored = await context(action="restore", start=branch.msgs.progression.index(middle.id))
    assert restored["success"] is True
    view = list(branch.progression)
    assert view.index(note.id) < view.index(middle.id) < view.index(copy.id) < view.index(later.id)
    await context(action="evict", start=view.index(note.id))
    restored_note = await context(action="restore", start=branch.msgs.progression.index(note.id))
    assert restored_note["success"] is True
    view = list(branch.progression)
    assert view.index(note.id) < view.index(middle.id) < view.index(copy.id) < view.index(later.id)
    assert original.id not in view


@pytest.mark.asyncio
async def test_amendment2_small_deficit_preserves_mapping_keys_and_yaml_only_values():
    import datetime
    import math

    from lionagi.agent.context_fold import ContextFold, estimate_next_request_tokens

    output = {
        "body": 'evidence é🦁 and quotes "yes"\n' * 2000,
        "items": ["entry", {"z": 2, "a": 1}],
        "success": "whole success " * 100,
        "error": "whole error " * 100,
        "bytes": b"whole bytes",
        "set": {"whole", "set"},
        "date": datetime.date(2026, 10, 24),
        "datetime": datetime.datetime(2026, 10, 24, 5, 0, tzinfo=datetime.timezone.utc),
    }
    branch, _requests, originals = await _clip_fixture([output], cap=1000000)
    before = estimate_next_request_tokens(branch)
    fold = ContextFold(branch, cap=math.ceil((before - 300) / 0.6))
    assert await fold._clip_once("threshold")
    copy = _active_results(branch)[0]
    assert list(copy.content.output) == list(output)
    assert copy.metadata["context_clip_kept"][0] > 1024
    assert len(output["body"]) - copy.metadata["context_clip_kept"][0] < len(output["body"]) / 2
    assert "characters omitted" in copy.content.output["body"]
    assert copy.content.output["items"] == '[JSON preview]\n["entry",{"a":1,"z":2}]'
    for key in ("success", "error", "bytes", "set", "date", "datetime"):
        assert copy.content.output[key] == output[key]
    assert originals[0].content.output == output
    assert fold.estimate() <= fold.target * fold.cap


@pytest.mark.asyncio
async def test_amendment2_unrenderable_object_refuses_at_estimate_before_clip(monkeypatch):
    from unittest.mock import AsyncMock

    from yaml.representer import RepresenterError

    from lionagi.agent.context_fold import ContextFold

    branch, _requests, _originals = await _clip_fixture([object()])
    fold = ContextFold(branch, cap=6000)
    clip = AsyncMock(side_effect=AssertionError("clip must not run"))
    monkeypatch.setattr(fold, "_clip_once", clip)
    with pytest.raises(RepresenterError):
        await fold.fold(reason="threshold")
    clip.assert_not_awaited()
    assert fold.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("over_ceiling", [True, False])
async def test_amendment2_floor_view_controls_summary_admission(monkeypatch, over_ceiling):
    import math

    from lionagi.agent.context_fold import ContextFold

    branch, task = _branch_with_reasoning(1000000, turns=6)
    branch.metadata["context_delivered"] = [str(uid) for uid in branch.progression]
    probe = ContextFold(branch, cap=1000000)
    floor = probe._estimate_view([task, *list(branch.msgs.messages)[-2:]])
    probe.cap = max(1, math.floor(floor / 1.2)) if over_ceiling else math.ceil(floor / 0.8)
    sent = []

    async def call(self, request, *args, **kwargs):
        sent.append(request)
        return _text("short factual summary")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    task_before = task.to_dict()
    await probe.fold(reason="threshold")
    assert task.to_dict() == task_before
    summaries = [record for record in probe.records if record["stage"] == "all"]
    if over_ceiling:
        assert sent == summaries == []
        assert all(record["target_reached"] is False for record in probe.records)
        branch.chat_model.max_input_tokens = probe.cap
        with pytest.raises(InputTokenCapError):
            await branch.chat("check again")
        assert sent == []
    else:
        assert len(sent) == len(summaries) == 1
        assert summaries[0]["kept_tail"] == 2
        assert summaries[0]["summary_usage"] == {"input_tokens": 1, "output_tokens": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("curated", [False, True])
@pytest.mark.parametrize("raises_scale", [False, True])
async def test_amendment2_nonshrinking_summary_rolls_back_at_settled_scale(curated, raises_scale):
    from lionagi.agent.context_fold import ContextFold
    from lionagi.protocols.generic.progression import Progression
    from lionagi.protocols.messages import AssistantResponse

    # The bounded 8000-character host note must be larger than the replaced reasoning,
    # even after _note truncates the deliberately oversized model response.
    branch, _task = _branch_with_reasoning(1000000, turns=4, detail_repeats=50)
    if curated:
        branch.metadata["current_progression"] = Progression(order=list(branch.progression))
    active = list(branch.progression)
    fold = ContextFold(
        branch,
        cap=1000000,
        estimate=lambda b: sum(len(str(b.msgs.messages[uid].rendered)) for uid in b.progression),
    )

    premises = []

    async def growing(_span):
        if raises_scale:
            branch.chat_model.input_token_scale = 2.5
        text = "summary larger than the span " * 10000
        note = AssistantResponse(
            content={"assistant_response": "[CONTEXT COMPACTION] " + fold._note(2, [], text)}
        )
        pile = branch.msgs.messages
        original = [pile[uid] for uid in active]
        candidate = [original[0], note, *original[3:]]
        before, after = fold._estimate_view(original), fold._estimate_view(candidate)
        assert after >= before, "the bounded note must genuinely fail strict shrink"
        premises.append((before, after, fold._provider_scale()))
        return text

    fold._model_summary = growing
    before_record = len(branch.msgs.messages)
    assert await fold._fold_once("all", 2, "refused") is False
    assert len(premises) == 1
    assert premises[0][2] == (2.5 if raises_scale else 1.0)
    assert list(branch.progression) == active
    assert "current_progression" in branch.metadata
    assert len(branch.msgs.messages) == before_record + 1
    rejected = list(branch.msgs.progression)[-1]
    assert rejected not in branch.progression and rejected not in fold._note_calls
    record = fold.records[-1]
    assert record["outcome"] == "rolled_back"
    assert record["estimate_after"] >= record["estimate_before"]
    assert record["estimate_before"] == fold._estimate_view(
        [branch.msgs.messages[uid] for uid in active]
    )


@pytest.mark.asyncio
async def test_amendment2_restore_original_twice_and_range_keeps_one_chronological_form():
    import math

    from lionagi.agent.context_fold import ContextFold, estimate_next_request_tokens
    from lionagi.protocols.generic.progression import Progression
    from lionagi.tools.context.context import ContextTool

    branch, _requests, originals = await _clip_fixture(["raw evidence " * 4000], cap=1000000)
    original = originals[0]
    later = await branch.msgs.a_add_message(instruction="later message to restore")
    trailing = await branch.msgs.a_add_message(assistant_response="later answer")
    branch.metadata["current_progression"] = Progression(
        order=[uid for uid in branch.progression if uid != later.id]
    )
    fold = ContextFold(branch, cap=math.ceil(estimate_next_request_tokens(branch) / 0.8))
    assert await fold._clip_once("threshold")
    copy = _active_results(branch)[0]
    context = ContextTool().bind(branch).func_callable
    later_index = branch.msgs.progression.index(later.id)
    restored = await context(action="restore", start=later_index)
    assert restored["success"] is True
    assert (
        list(branch.progression).index(copy.id)
        < list(branch.progression).index(later.id)
        < list(branch.progression).index(trailing.id)
    )
    index = branch.msgs.progression.index(original.id)
    first = await context(action="restore", start=index)
    assert first["success"] is True and first["restored"] == 1
    second = await context(action="restore", start=index)
    assert second["success"] is True and second["restored"] == 0
    whole = await context(action="restore", start=0, end=len(branch.msgs.progression))
    assert whole["success"] is True
    assert original.id in branch.progression and copy.id not in branch.progression
    assert len([message for message in _active_results(branch) if message.id == original.id]) == 1
    only_copy = await context(action="restore", start=branch.msgs.progression.index(copy.id))
    assert only_copy["restored"] == 0 and copy.id not in branch.progression


async def _file_clip_fixture(tmp_path):
    import math

    from lionagi.agent.context_fold import ContextFold, estimate_next_request_tokens
    from lionagi.protocols.messages import ActionResponse
    from lionagi.session.branch import Branch
    from lionagi.tools.coding import ALL_CODING_TOOLS, CodingToolkit

    path = tmp_path / "evidence.txt"
    path.write_text("stored " + "é🦁 evidence 0123456789 " * 4000)
    branch = Branch(system="Read the file before editing.", chat_model=_haiku(1000000))
    await branch.msgs.a_add_message(instruction="read, retain evidence, then edit")
    toolkit = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = toolkit.bind(branch)
    branch.register_tools(tools)
    await branch.act(
        {"function": "reader", "arguments": {"action": "read", "path": str(path)}},
        strategy="sequential",
        suppress_errors=False,
    )
    original = next(
        message
        for message in branch.msgs.messages
        if isinstance(message, ActionResponse) and message.content.function == "reader"
    )
    fold = ContextFold(branch, cap=math.ceil(estimate_next_request_tokens(branch) / 0.8))
    assert await fold._clip_once("threshold")
    context = next(tool.func_callable for tool in tools if tool.func_callable.__name__ == "context")
    return branch, fold, original, context, path


@pytest.mark.asyncio
async def test_amendment2_ceiling_refusal_and_coding_recovery_read_the_stored_file_result(tmp_path):
    import re

    from lionagi.cli.agent import _tool_results
    from lionagi.tools.context.context import _result_text

    branch, fold, original, context, path = await _file_clip_fixture(tmp_path)
    copy = _active_results(branch)[0]
    before = original.to_dict()
    text, bodies = _result_text(original.content.output)
    body = next(body for body in bodies if body.key == "content")
    marker = re.search(r"offset=(\d+), limit=(\d+)", copy.content.output["content"])
    assert marker is not None
    offset = int(marker.group(1))
    path.write_text("file changed after the stored read")
    fold.cap = 1
    refused = await context(action="restore", start=branch.msgs.progression.index(original.id))
    assert refused["success"] is False and "read_result" in refused["error"]
    assert copy.id in branch.progression and original.id not in branch.progression
    count = _tool_results(branch)
    await branch.act(
        {
            "function": "context",
            "arguments": {
                "action": "read_result",
                "index": branch.msgs.progression.index(original.id),
                "offset": offset,
                "limit": 20000,
            },
        },
        strategy="sequential",
        suppress_errors=False,
    )
    page = next(
        message
        for message in reversed(_active_results(branch))
        if message.content.function == "context"
    )
    assert page.content.output["text"] == text[offset : offset + 16000]
    assert len(page.content.output["text"]) == 16000
    assert body.start <= offset < body.end
    assert original.to_dict() == before
    assert _tool_results(branch) == count + 1
    # A just-read page must remain whole before the next successful main completion.
    page_before = page.to_dict()
    await fold._clip_once("threshold")
    assert page.id in branch.progression and page.to_dict() == page_before


@pytest.mark.asyncio
@pytest.mark.parametrize("release", ["restore", "reread"])
async def test_amendment2_clipped_file_read_cannot_authorize_edit_until_visible(tmp_path, release):
    branch, _fold, original, context, path = await _file_clip_fixture(tmp_path)
    before = path.read_text()
    edit = {
        "function": "editor",
        "arguments": {
            "action": "edit",
            "file_path": str(path),
            "old_string": "stored",
            "new_string": "changed",
        },
    }
    await branch.act(edit, strategy="sequential", suppress_errors=False)
    blocked = _active_results(branch)[-1]
    assert blocked.content.output["success"] is False
    assert "Must read file before editing" in blocked.content.output["error"]
    assert path.read_text() == before
    if release == "restore":
        restored = await context(action="restore", start=branch.msgs.progression.index(original.id))
        assert restored["success"] is True
    else:
        await branch.act(
            {"function": "reader", "arguments": {"action": "read", "path": str(path)}},
            strategy="sequential",
            suppress_errors=False,
        )
    await branch.act(edit, strategy="sequential", suppress_errors=False)
    accepted = _active_results(branch)[-1]
    assert accepted.content.output["success"] is True
    assert path.read_text().startswith("changed")


@pytest.mark.asyncio
async def test_amendment2_restored_read_preserves_the_external_mtime_guard(tmp_path):
    import os

    branch, _fold, original, context, path = await _file_clip_fixture(tmp_path)
    before = path.stat().st_mtime
    path.write_text("external modification")
    os.utime(path, (before + 5, before + 5))
    result = await context(action="restore", start=branch.msgs.progression.index(original.id))
    assert result["success"] is True
    await branch.act(
        {
            "function": "editor",
            "arguments": {
                "action": "edit",
                "file_path": str(path),
                "old_string": "external",
                "new_string": "wrong",
            },
        },
        strategy="sequential",
        suppress_errors=False,
    )
    refused = _active_results(branch)[-1]
    assert refused.content.output["success"] is False
    assert "File changed since last read" in refused.content.output["error"]
    assert path.read_text() == "external modification"


@pytest.mark.asyncio
async def test_amendment2_repeated_clip_clone_and_store_keep_one_original_based_copy():
    from lionagi.agent.context_fold import ContextFold
    from lionagi.session.branch import Branch
    from lionagi.tools.context.context import ContextTool

    output = "original é🦁 evidence 0123456789\n" * 4000
    branch, requests, originals = await _clip_fixture([output], cap=10000)
    original = originals[0]
    original_before = original.to_dict()
    request_before = requests[0].to_dict()
    fold = ContextFold(branch, cap=10000)
    assert await fold._clip_once("threshold")
    first = _active_results(branch)[0]
    fold.cap = 6000
    assert await fold._clip_once("threshold")
    second = _active_results(branch)[0]
    assert first.id != second.id and first.id not in branch.progression
    assert second.metadata["clipped_from"] == str(original.id)
    assert "context(action=" not in output
    assert second.content.output.count('context(action="read_result"') == 1
    assert original.to_dict() == original_before and requests[0].to_dict() == request_before
    restored = Branch.from_dict(branch.clone().to_dict())
    copies = _active_results(restored)
    assert len(copies) == 1
    cloned_original = restored.msgs.messages[copies[0].metadata["clipped_from"]]
    assert cloned_original.content.output == output
    assert copies[0].metadata["clipped_from"] != str(original.id)
    index = restored.msgs.progression.index(cloned_original.id)
    page = (
        await ContextTool()
        .bind(restored)
        .func_callable(action="read_result", index=index, offset=1000, limit=16000)
    )
    assert page["text"] == output[1000:17000]


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True])
async def test_amendment2_later_tool_fold_lists_a_clipped_call_once_at_original_index(delivered):
    from lionagi.agent.context_fold import ContextFold

    branch, requests, originals = await _clip_fixture(["original data " * 7000], cap=8000)
    fold = ContextFold(branch, cap=8000)
    assert await fold._clip_once("threshold")
    copy = _active_results(branch)[0]
    await branch.msgs.a_add_message(assistant_response="ready to inspect the clipped result")
    await branch.msgs.a_add_message(instruction="tail task")
    await branch.msgs.a_add_message(assistant_response="tail answer")
    if delivered:
        branch.metadata["context_delivered"] = [str(uid) for uid in branch.progression]
        before_payload = json.dumps(_compiled_view_payload(branch))
        before_count = fold.estimate()
        assert "characters omitted" in before_payload and "original data " in before_payload
        assert await fold._fold_once("tool_io", 2, "threshold")
        after_payload = json.dumps(_compiled_view_payload(branch))
        assert "characters omitted" not in after_payload
        assert fold.estimate() < before_count
        assert copy.id not in branch.progression and requests[0].id not in branch.progression
        calls = fold.records[-1]["tool_calls"]
        assert len(calls) == 1
        assert calls[0]["index"] == branch.msgs.progression.index(originals[0].id)
        assert calls[0]["output_chars"] == len(originals[0].content.output)
    else:
        assert await fold._fold_once("tool_io", 2, "threshold") is False
        assert copy.id in branch.progression and requests[0].id in branch.progression


@pytest.mark.asyncio
async def test_amendment2_delivered_tail_copy_leaves_its_folded_call_unlisted():
    from lionagi.agent.context_fold import ContextFold

    branch, requests, originals = await _clip_fixture(
        ["old result " * 6000, "new result " * 6000], cap=15000
    )
    fold = ContextFold(branch, cap=15000)
    assert await fold._clip_once("threshold")
    newest = _active_results(branch)[-1]
    branch.metadata["context_delivered"] = [str(uid) for uid in branch.progression]
    assert await fold._fold_once("tool_io", 1, "threshold")
    assert newest.id in branch.progression and requests[-1].id not in branch.progression
    listed = fold.records[-1]["tool_calls"]
    assert all(call["index"] != branch.msgs.progression.index(originals[-1].id) for call in listed)
    assert [call["index"] for call in listed] == [branch.msgs.progression.index(originals[0].id)]


@pytest.mark.asyncio
async def test_amendment2_tool_owned_clipped_from_data_never_redirects_provenance():
    from lionagi.agent.context_fold import ContextFold

    output = {"clipped_from": "a tool-owned value", "content": "long actual data " * 6000}
    branch, _requests, originals = await _clip_fixture([output])
    fold = ContextFold(branch, cap=6000)
    assert await fold._clip_once("threshold")
    copy = _active_results(branch)[0]
    assert copy.metadata["clipped_from"] == str(originals[0].id)
    assert copy.content.output["clipped_from"] == "a tool-owned value"


@pytest.mark.asyncio
async def test_amendment2_actual_run_work_count_is_unchanged_by_clipping(leg):
    counts = []
    for cap in (100000, 20000):
        (leg.workspace / "notes.txt").write_text("actual stored file evidence 0123456789\n" * 2000)
        leg.replies = [_read_round("notes.txt"), _last_round(), _answer("done")]
        result = await leg.run(_profile(api_tools="read", max_input_tokens=cap))
        assert result[3] == "completed"
        counts.append(leg.branch.metadata["api_launch"]["spend"]["tool_calls"])
        copies = [
            message
            for message in _active_results(leg.branch)
            if message.metadata.get("clipped_from")
        ]
        assert bool(copies) is (cap == 20000)
    assert counts == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("protect", ["restore", "read_result"])
async def test_amendment2_recovery_protection_lasts_until_a_completed_main_request(
    tmp_path, monkeypatch, protect
):
    from lionagi.agent.context_fold import install_context_fold

    branch, fold, original, context, _path = await _file_clip_fixture(tmp_path)
    index = branch.msgs.progression.index(original.id)
    if protect == "restore":
        result = await context(action="restore", start=index)
        assert result["success"] is True
        protected = original
    else:
        await branch.act(
            {
                "function": "context",
                "arguments": {
                    "action": "read_result",
                    "index": index,
                    "offset": 1000,
                    "limit": 16000,
                },
            },
            strategy="sequential",
            suppress_errors=False,
        )
        protected = _active_results(branch)[-1]
    protected_before = protected.to_dict()
    fold.cap = 1
    await fold._clip_once("threshold")
    assert protected.id in branch.progression and protected.to_dict() == protected_before

    async def call(self, request, *args, **kwargs):
        return _text("main completed")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    registered = install_context_fold(branch, cap=1000000)
    await branch.chat("read the recovered evidence")
    assert "context_restored_results" not in branch.metadata
    assert "context_read_results" not in branch.metadata
    registered.cap = 1
    assert await registered._clip_once("threshold")
    assert protected.id not in branch.progression
    assert any(
        message.metadata.get("clipped_from") == str(protected.id)
        for message in _active_results(branch)
    )


@pytest.mark.asyncio
async def test_amendment2_parallel_main_requests_match_their_own_captured_views():
    import asyncio

    from lionagi.agent.context_fold import install_context_fold
    from lionagi.testing import LionAGIMockFactory

    branch = LionAGIMockFactory.create_mocked_branch(response="answer")
    earlier = await branch.msgs.a_add_message(instruction="earlier")
    install_context_fold(branch, cap=1000000)
    invoke = branch.chat_model.invoke
    started = {name: asyncio.Event() for name in ("request-a", "request-b")}
    release = {name: asyncio.Event() for name in started}

    async def controlled(**kwargs):
        name = "request-a" if "request-a" in json.dumps(kwargs["messages"]) else "request-b"
        started[name].set()
        await release[name].wait()
        return await invoke(**kwargs)

    branch.chat_model.invoke = controlled
    tasks = []
    try:
        tasks.append(asyncio.create_task(branch.chat("request-a")))
        await asyncio.wait_for(started["request-a"].wait(), timeout=5)
        later = await branch.msgs.a_add_message(instruction="arrived between requests")
        tasks.append(asyncio.create_task(branch.chat("request-b")))
        await asyncio.wait_for(started["request-b"].wait(), timeout=5)
        release["request-a"].set()
        await tasks[0]
        assert str(earlier.id) in branch.metadata["context_delivered"]
        assert str(later.id) not in branch.metadata["context_delivered"]
        release["request-b"].set()
        await tasks[1]
        assert str(later.id) in branch.metadata["context_delivered"]
    finally:
        for event in release.values():
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_amendment2_copy_persistence_refusal_stops_before_the_main_request(monkeypatch):
    from lionagi.agent.context_fold import install_context_fold
    from lionagi.state.compat import StateCompatibilityRefused

    branch, _requests, _originals = await _clip_fixture(["actual original result " * 6000])
    refusal = StateCompatibilityRefused("persist_live_message", "messages@1", "refused")
    drained = []

    async def refuse_copy(message):
        if message.metadata.get("clipped_from"):
            raise refusal

    def later(message):
        if message.metadata.get("clipped_from"):
            drained.append(message)

    async def never_sent(self, request, *args, **kwargs):
        raise AssertionError("a persistence refusal must precede provider invocation")

    branch.on_message_added.extend((refuse_copy, later))
    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", never_sent)
    install_context_fold(branch, cap=6000)
    with pytest.raises(StateCompatibilityRefused) as caught:
        await branch.chat("read the original")
    assert caught.value is refusal
    assert len(drained) == 1


@pytest.mark.asyncio
async def test_amendment2_busy_main_request_still_marks_its_actual_view_delivered():
    import asyncio

    from lionagi.agent.context_fold import install_context_fold
    from lionagi.testing import LionAGIMockFactory

    branch = LionAGIMockFactory.create_mocked_branch(response="main completed")
    await branch.msgs.a_add_message(instruction="the view carried by the main request")
    fold = install_context_fold(branch, cap=1000000)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_once = fold._fold_once

    async def held_once(mode, keep_tail, reason):
        entered.set()
        await release.wait()
        return await original_once(mode, keep_tail, reason)

    fold._fold_once = held_once
    # Real fold() owns _busy and its finally cleanup; no flag is assigned by the fixture.
    folding = asyncio.create_task(fold.fold(reason="refused", observed=100000))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert fold._busy is True
        captured = [str(uid) for uid in branch.progression]
        assert await branch.chat("main while another fold waits") == "main completed"
        assert branch.metadata["context_delivered"] == captured
    finally:
        release.set()
        await folding
    assert fold._busy is False


@pytest.mark.parametrize("value", [Path("notes-é.txt"), object()], ids=["path", "object"])
def test_fold_note_text_keeps_default_str_as_its_last_fallback(value):
    from lionagi.agent.context_fold import _short, _size
    from lionagi.agent.context_fold import _text as fold_text

    expected = json.dumps(value, ensure_ascii=False, default=str)
    assert fold_text(value) == expected
    assert json.loads(_short({"result": value})) == {"result": str(value)}
    assert _size(value) == len(expected)


def test_fold_note_text_keeps_yaml_values_before_the_string_fallback():
    import datetime

    import yaml

    from lionagi.agent.context_fold import _text as fold_text

    value = {"date": datetime.date(2026, 10, 24)}
    assert yaml.safe_load(fold_text(value)) == value


@pytest.mark.asyncio
async def test_fold_pre_call_refusal_retains_the_group_and_its_sibling(monkeypatch):
    from lionagi.agent.context_fold import ContextFold
    from lionagi.ln.concurrency import ExceptionGroup
    from lionagi.state.compat import StateCompatibilityRefused

    branch, _task = _branch_with_reasoning(cap=1000000, turns=1)
    fold = ContextFold(branch, cap=6000, estimate=lambda _branch: 6000)
    refusal = StateCompatibilityRefused("persist_live_message", "messages@1", "refused")
    sibling = ValueError("another persistence callback failed")
    group = ExceptionGroup("fold persistence failures", [refusal, sibling])

    async def refuse(**_kwargs):
        raise group

    monkeypatch.setattr(fold, "fold", refuse)
    with pytest.raises(StateCompatibilityRefused) as caught:
        await fold.on_api_pre_call()
    assert caught.value is refusal
    assert caught.value.__cause__ is group
    assert group.exceptions == (refusal, sibling)
    assert fold._request_views.get() == ()
