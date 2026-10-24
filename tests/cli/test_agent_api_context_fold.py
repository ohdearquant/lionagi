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
    files, profile, built, summaries = _reading_run(leg, monkeypatch)

    result, _p, _b, status, _s = await leg.run(profile, "read every file")

    assert (result, status) == ("read every file", "completed")
    assert leg.replies == [], "every scripted round is consumed: none was refused or added"
    assert len(leg.payloads) == len(built) == len(files) + 2
    # Without the fold these reads pass the cap: the input grows by one file per round.
    assert max(built) <= _CAP
    folds = _manifest(tmp_path)["context_folds"]
    assert folds, "the run crossed fold_at and nothing was folded"
    for record in folds:
        assert set(record) == _RECORD_KEYS
        assert record["reason"] == "threshold"
        assert record["cap"] == _CAP
        assert record["estimate_after"] < record["estimate_before"]
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
        if calls and record["estimate_before"] == calls[-1][-1]["estimate_after"]:
            calls[-1].append(record)
        else:
            calls.append([record])
    assert len(calls) >= 2, folds
    assert all(c[0]["estimate_before"] >= 0.8 * _CAP for c in calls), folds
    assert all(c[-1]["estimate_after"] <= 0.6 * _CAP for c in calls), folds
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

    # The second request carries the first file read, which the fold keeps (it is the newest
    # tool result); the rest is the fixed prompt, so no fold gets the request under the cap.
    _files, profile, built, summaries = _reading_run(
        leg, monkeypatch, reads=2, lines=_ONE_FILE_LINES, cap=_SMALL_CAP
    )

    async def missed(self, **kw):
        return None

    monkeypatch.setattr(ContextFold, "on_api_pre_call", missed)

    with pytest.raises(InputTokenCapError):
        await leg.run(profile, "read every file")

    assert len(leg.payloads) == 1
    # Built: the first round and the refused second round; no resume was attempted.
    assert [n > _SMALL_CAP for n in built] == [False, True]
    final = _manifest(tmp_path)
    assert final["status"] == leg.status == "failed"
    assert f"max_input_tokens={_SMALL_CAP:,}" in final["error"]
    folds = final["context_folds"]
    assert folds and {r["reason"] for r in folds} == {"refused"}
    # One fold attempt, no resume: the calibrated estimate after it was still over the cap.
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


def _branch_with_reasoning(cap: int, turns: int = 10):
    from lionagi import Branch

    branch = Branch(chat_model=_haiku(cap))
    msgs = branch.msgs
    task = msgs.create_instruction(instruction="check src/app/core.py and report")
    msgs.messages.include(task)
    msgs.progression.include(task.id)
    for i in range(turns):
        note = msgs.create_assistant_response(
            assistant_response=f"step {i}: src/app/core.py line {i * 40} " + "detail " * 600
        )
        msgs.messages.include(note)
        msgs.progression.include(note.id)
    return branch, task


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
    assert {r["stage"] for r in fold.records} == {"all"}
    assert {r["summary_source"] for r in fold.records} == {"model"}
    assert fold.estimate() <= 0.6 * 8_000
    active = list(branch.progression)
    assert active[0] == task.id, "the task instruction stays first and verbatim"
    notes = [branch.msgs.messages[u].rendered for u in active[1:]]
    assert any("SUMMARY: src/app/core.py checked through line 360" in str(n) for n in notes)
    # Nothing is deleted: the durable record gains the notes and loses nothing.
    assert len(branch.msgs.messages) == durable_before + len(fold.records)


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

    assert {r["summary_source"] for r in fold.records} == {"host"}
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
    assert fold.records == []


@pytest.mark.asyncio
async def test_a_fold_that_cuts_between_a_call_and_its_result_keeps_the_call_in_view():
    # Tool calls reach the model only as their results, which carry the call's function and
    # arguments, so a cut that folds a call and keeps its result loses nothing it reads.
    from lionagi import Branch
    from lionagi.agent.context_fold import ContextFold
    from lionagi.operations.chat._prepare import _prepare_run_kwargs
    from lionagi.operations.types import ChatParam
    from lionagi.protocols.messages import ActionRequest, ActionResponse

    branch = Branch(chat_model=_haiku(_CAP))
    msgs = branch.msgs
    added = [msgs.create_instruction(instruction="read the three files and report")]
    for i in range(3):
        call = {"function": "read_file", "arguments": {"path": f"src/f{i}.py"}}
        added.append(ActionRequest(content=call))
        added.append(ActionResponse(content={**call, "output": "line\n" * 50}))
    added.append(msgs.create_instruction(instruction="continue"))
    for m in added:
        msgs.messages.include(m)
        msgs.progression.include(m.id)
    request, result = added[5], added[6]

    assert await ContextFold(branch, cap=_CAP)._fold_once("tool_io", 2, "threshold")

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
