# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The host-side fold around the first round kept ahead of it while the prompt cache is placed
(ADR-0130 Amendment 4): what the fold protects, where its spans start, when it resets, and
that a request's decision outlives a failed send. The endpoint is scripted, so no request
leaves the process."""

import pytest

from lionagi.agent.context_fold import ContextFold, install_context_fold
from lionagi.operations.chat._prepare import _prepare_run_kwargs
from lionagi.operations.types import ChatParam
from lionagi.protocols.messages import ActionResponse, AssistantResponse
from lionagi.service import _prompt_cache as cache
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from lionhub.providers.anthropic.messages import AnthropicMessagesEndpoint
from tests.cli.test_agent_api_provider import _text

_CAP = 8_000


def _haiku(cap, *, places=True):
    return iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        max_input_tokens=cap,
        prompt_caching=places,
    )


async def _first_round(branch, output="EVIDENCE"):
    """The task, its answer, and that answer's one call and result; returns the member ids."""
    msgs = branch.msgs
    await msgs.a_add_message(instruction="check src/app/core.py and report")
    await msgs.a_add_message(assistant_response="reading src/app/core.py")
    request = await msgs.a_add_message(
        action_function="reader", action_arguments={"path": "src/app/core.py"}
    )
    await msgs.a_add_message(action_request=request, action_output=output)
    return [str(uid) for uid in branch.progression]


def _steps(branch, count, *, repeats=600, start=0):
    for i in range(start, start + count):
        branch.msgs.add_message(
            assistant_response=f"step {i}: src/app/core.py line {i * 40} " + "detail " * repeats
        )


def _note(branch, first_index, words=1):
    """A compaction note as the context tool writes it."""
    return branch.msgs.add_message(
        assistant_response=AssistantResponse(
            content={"assistant_response": "[CONTEXT COMPACTION] " + "summary " * words},
            metadata={"context_first_index": first_index},
        )
    )


def _decide(fold, cap=None):
    view = {}
    _ins, kw = _prepare_run_kwargs(
        fold.branch, "the next step", ChatParam(), imodel=fold.branch.chat_model, cache_view=view
    )
    if cap is None:
        return fold._decide_protection(view, kw["messages"])
    return cache.decide_protection(
        fold.branch.metadata,
        view,
        kw["messages"],
        cap=cap,
        scale=1.0,
        max_chars=None,
        decided_by="test",
    )


def _summarizing(monkeypatch):
    async def call(self, request, *args, **kwargs):
        return _text("SUMMARY: src/app/core.py checked")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)


@pytest.mark.asyncio
async def test_the_fold_protects_a_pending_or_included_round_and_never_an_excluded_one():
    branch = Branch(chat_model=_haiku(_CAP))
    members = await _first_round(branch)
    _steps(branch, 2)
    first_note = _note(branch, 4)
    newest = _note(branch, 5)
    fold = ContextFold(branch, cap=_CAP)

    def bounds():
        protected, frontier, note = fold._cache_bounds(list(branch.progression))
        return sorted(str(uid) for uid in protected), frontier, note

    newest_at = list(branch.progression).index(newest.id)
    assert newest_at > list(branch.progression).index(first_note.id)
    assert bounds() == (sorted(members), len(members) - 1, newest_at)

    assert _decide(fold)["included"] is True
    assert bounds() == (sorted(members), len(members) - 1, newest_at)

    branch.metadata["prompt_cache"].pop("protected")
    assert _decide(fold, cap=1)["included"] is False
    assert bounds() == ([], -1, newest_at)

    off = Branch(chat_model=_haiku(_CAP, places=False))
    await _first_round(off)
    _note(off, 4)
    assert ContextFold(off, cap=_CAP)._cache_bounds(list(off.progression)) == (set(), -1, -1)


@pytest.mark.asyncio
async def test_a_fold_keeps_the_round_and_folds_only_what_follows_the_newest_note(monkeypatch):
    _summarizing(monkeypatch)
    branch = Branch(chat_model=_haiku(_CAP))
    members = await _first_round(branch)
    _steps(branch, 10)
    fold = ContextFold(branch, cap=_CAP)
    assert _decide(fold)["included"] is True

    assert await fold.fold(reason="threshold")

    active = [str(uid) for uid in branch.progression]
    assert active[: len(members)] == members
    notes = [
        uid
        for uid in branch.progression
        if "context_first_index" in branch.msgs.messages[uid].metadata
    ]
    assert notes and "all" in {record["stage"] for record in fold.records}
    first_note = notes[-1]

    _steps(branch, 10, start=10)
    # Completed requests carried the note and the steps (ContextFold.on_api_post_call), so
    # the span start alone keeps the note out of the next fold.
    branch.metadata["context_delivered"] = [str(uid) for uid in branch.progression]
    fold.records.clear()
    assert await fold.fold(reason="threshold")

    active = [str(uid) for uid in branch.progression]
    assert active[: len(members)] == members
    assert active[len(members)] == str(first_note), "the earlier note is never refolded"
    assert {record["stage"] for record in fold.records} <= {"tool_io", "clip", "all"}


@pytest.mark.asyncio
@pytest.mark.parametrize("words", [1, 1200], ids=["notes under 15%", "notes over 15%"])
async def test_notes_past_fifteen_percent_of_the_cap_are_reset_before_anything_else(
    monkeypatch, words
):
    _summarizing(monkeypatch)
    branch = Branch(chat_model=_haiku(_CAP))
    members = await _first_round(branch)
    _steps(branch, 2)
    old = _note(branch, 4, words=words)
    _steps(branch, 8, start=2)
    fold = ContextFold(branch, cap=_CAP)
    assert _decide(fold)["included"] is True
    over = fold._note_tokens(list(branch.progression)) * 100 > 15 * _CAP
    assert over is (words > 1)

    assert await fold.fold(reason="threshold")

    stages = [record["stage"] for record in fold.records]
    active = [str(uid) for uid in branch.progression]
    assert active[: len(members)] == members
    if over:
        assert stages[0] == "reset" and set(stages) == {"reset"}
        assert str(old.id) not in active
    else:
        assert "reset" not in stages[:1]
        assert str(old.id) in active


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bound, cap, later",
    [(None, 20_000, 3), (4_096, 6_000, 4)],
    ids=["unbounded results", "bounded results"],
)
async def test_a_clip_never_takes_bytes_from_a_protected_result(bound, cap, later):
    model = _haiku(cap)
    model.tool_result_max_chars = bound
    branch = Branch(chat_model=model)
    members = await _first_round(branch, output="first " * 6000)
    first_result = branch.msgs.messages[branch.progression[len(members) - 1]]
    assert isinstance(first_result, ActionResponse)
    await branch.msgs.a_add_message(assistant_response="reading the rest")
    for number in range(later):
        request = await branch.msgs.a_add_message(
            action_function=f"tool_{number}", action_arguments={"item": number}
        )
        await branch.msgs.a_add_message(action_request=request, action_output="later " * 3000)
    fold = ContextFold(branch, cap=cap)
    assert cache.protection(branch.metadata) is None  # pending, so protected

    assert await fold._clip_once("threshold")

    results = [
        branch.msgs.messages[uid]
        for uid in branch.progression
        if isinstance(branch.msgs.messages[uid], ActionResponse)
    ]
    assert first_result in results
    assert all(r.metadata.get("clipped_from") != str(first_result.id) for r in results)
    assert any(r.metadata.get("clipped_from") for r in results), "no later result was clipped"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bound, cap, later",
    [(None, 20_000, 3), (4_096, 6_000, 4)],
    ids=["unbounded results", "bounded results"],
)
@pytest.mark.parametrize("places", [True, False], ids=["cache placed", "cache off"])
async def test_a_clip_takes_only_what_follows_the_newest_note(bound, cap, later, places):
    model = _haiku(cap, places=places)
    model.tool_result_max_chars = bound
    branch = Branch(chat_model=model)
    members = await _first_round(branch)
    await branch.msgs.a_add_message(assistant_response="reading the rest")
    request = await branch.msgs.a_add_message(
        action_function="tool_early", action_arguments={"item": 0}
    )
    early = await branch.msgs.a_add_message(action_request=request, action_output="early " * 6000)
    note = _note(branch, len(members) + 1)
    for number in range(later):
        request = await branch.msgs.a_add_message(
            action_function=f"tool_{number}", action_arguments={"item": number}
        )
        await branch.msgs.a_add_message(action_request=request, action_output="later " * 3000)
    active = list(branch.progression)
    assert active.index(early.id) < active.index(note.id)
    fold = ContextFold(branch, cap=cap)

    assert await fold._clip_once("threshold")

    results = [
        branch.msgs.messages[uid]
        for uid in branch.progression
        if isinstance(branch.msgs.messages[uid], ActionResponse)
    ]
    clipped = {r.metadata.get("clipped_from") for r in results} - {None}
    if places:
        assert early in results and str(early.id) not in clipped
        assert clipped, "no result after the note was clipped"
    else:
        # Off, the largest result is clipped first wherever it sits, as before.
        assert str(early.id) in clipped


@pytest.mark.asyncio
@pytest.mark.parametrize("raised", [False, True], ids=["failed call", "raised send"])
async def test_a_request_decides_the_round_once_and_a_failed_send_keeps_the_decision(
    monkeypatch, raised
):
    branch = Branch(chat_model=_haiku(20_000))
    await _first_round(branch)
    install_context_fold(branch, cap=20_000)

    async def refused(self, request, *args, **kwargs):
        raise RuntimeError("provider unavailable")

    invoke = iModel.invoke

    async def raising(self, **kwargs):
        raise RuntimeError("send interrupted")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", refused)
    if raised:
        monkeypatch.setattr(iModel, "invoke", raising)
    with pytest.raises(Exception):
        await branch.chat("the next step")
    monkeypatch.setattr(iModel, "invoke", invoke)

    record = cache.protection(branch.metadata)
    assert record is not None and record["included"] is True
    assert record["decided_by"] == "context_fold"
    decided = dict(record)

    sent = []

    async def answered(self, request, *args, **kwargs):
        sent.append(request)
        return _text("done")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", answered)
    assert await branch.chat("the next step") == "done"

    assert cache.protection(branch.metadata) == decided
    assert len(sent) == 1
