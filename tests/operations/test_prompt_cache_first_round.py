# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The first round kept ahead of every fold while the prompt cache is placed (ADR-0130
Amendment 4): how a round compiles, how it is decided, and what the decision anchors."""

import math

import pytest

from lionagi.operations.chat._prepare import _prepare_run_kwargs, first_round
from lionagi.operations.types import ChatParam
from lionagi.protocols.messages import ActionResponse, AssistantResponse
from lionagi.service import _prompt_cache as cache
from lionagi.service.connections.api_calling import estimate_payload_input_tokens
from lionagi.session.branch import Branch


class _Endpoint:
    def __init__(self, places_prompt_cache):
        self.places_prompt_cache = places_prompt_cache


class _Model:
    def __init__(self, places_prompt_cache):
        self.endpoint = _Endpoint(places_prompt_cache)


async def _round_one(branch, output="EVIDENCE"):
    await branch.msgs.a_add_message(instruction="read the notes")
    await branch.msgs.a_add_message(assistant_response="reading the notes")
    result = ActionResponse(
        content={"function": "reader", "arguments": {"path": "notes.txt"}, "output": output}
    )
    await branch.msgs._a_add_built_response(result)
    return result


def _note(branch, first_index):
    """A compaction note as the context tool writes it."""
    return branch.msgs.add_message(
        assistant_response=AssistantResponse(
            content={"assistant_response": "[CONTEXT COMPACTION] SUMMARY"},
            metadata={"context_first_index": first_index},
        )
    )


def _compile(branch, prompt, *, places=True, ins=None):
    view = {}
    ins, kw = _prepare_run_kwargs(
        branch, prompt, ChatParam(), ins=ins, imodel=_Model(places), cache_view=view
    )
    return ins, kw["messages"], view


def _ids(branch):
    return tuple(str(uid) for uid in branch.progression)


def _head_size(messages, frontier):
    head = cache._through(messages, *frontier)
    return math.ceil(estimate_payload_input_tokens({"messages": head}))


# The result envelope


@pytest.mark.asyncio
async def test_a_rounds_results_open_the_next_user_message_as_their_own_block():
    branch = Branch()
    await _round_one(branch)

    _ins, messages, view = _compile(branch, "the next step")

    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    results, *rest = messages[2]["content"]
    assert results["text"].startswith("ToolResults")
    assert "EVIDENCE" in results["text"] and "the next step" not in results["text"]
    assert any("the next step" in block["text"] for block in rest)
    assert view["round"] == _ids(branch)
    assert view["frontier"] == (2, 0)


@pytest.mark.asyncio
async def test_a_rounds_results_compile_alike_on_the_current_turn_and_in_history():
    branch = Branch()
    await _round_one(branch)

    ins, current, _view = _compile(branch, "the next step")
    branch.msgs.add_message(instruction=ins)
    branch.msgs.add_message(assistant_response="done")
    _ins, later, view = _compile(branch, "the third step")

    assert later[:2] == current[:2]
    assert later[2]["content"][0] == current[2]["content"][0]
    assert view["frontier"] == (2, 0)


@pytest.mark.asyncio
async def test_results_an_answer_follows_take_a_user_message_of_their_own():
    branch = Branch()
    await _round_one(branch)
    branch.msgs.add_message(assistant_response="the notes say enough")

    _ins, messages, view = _compile(branch, "the next step")

    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]
    (results,) = messages[2]["content"]
    assert results["text"].startswith("ToolResults") and "EVIDENCE" in results["text"]
    assert view["frontier"] == (2, 0)


@pytest.mark.asyncio
async def test_without_the_placement_results_still_merge_into_the_next_instruction():
    branch = Branch()
    await _round_one(branch)

    _ins, messages, view = _compile(branch, "the next step", places=False)

    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    rendered = str(messages[2]["content"])
    assert "EVIDENCE" in rendered and "the next step" in rendered
    assert "ToolResults" not in rendered
    assert view == {}


def test_an_answer_and_the_note_after_it_are_two_blocks_of_one_assistant_message():
    branch = Branch()
    branch.msgs.add_message(instruction="the task")
    branch.msgs.add_message(assistant_response="an answer")
    _note(branch, 2)

    _ins, messages, view = _compile(branch, "the next step")

    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert [block["text"] for block in messages[1]["content"]] == [
        "an answer",
        "[CONTEXT COMPACTION] SUMMARY",
    ]
    assert view["note"] == (1, 1)
    assert view["frontier"] == (1, 0)


# The first round


def test_the_first_round_has_no_members_until_it_is_answered():
    branch = Branch(system="be brief")
    branch.msgs.add_message(instruction="the task")

    assert first_round(branch, branch.progression) is None


@pytest.mark.asyncio
async def test_the_first_round_ends_at_the_next_answer_instruction_or_note():
    branch = Branch(system="be brief")
    await _round_one(branch)
    members = _ids(branch)[1:]
    for follower in (
        {"assistant_response": "the next answer"},
        {"instruction": "the next task"},
        None,
    ):
        probe = Branch(system="be brief")
        await _round_one(probe)
        if follower is None:
            _note(probe, 3)
        else:
            probe.msgs.add_message(**follower)
        assert len(first_round(probe, probe.progression)) == len(members)
    assert first_round(branch, branch.progression) == members

    # A note right after the task stands for the rounds it folded, never for this one.
    folded = Branch(system="be brief")
    folded.msgs.add_message(instruction="the task")
    _note(folded, 2)
    folded.msgs.add_message(assistant_response="a later answer")
    assert first_round(folded, folded.progression) is None


# The decision


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [0, 1], ids=["equal fits", "one over is excluded"])
async def test_the_round_is_included_when_it_is_at_most_a_tenth_of_the_cap(over):
    branch = Branch()
    await _round_one(branch)
    _ins, messages, view = _compile(branch, "the next step")
    size = _head_size(messages, view["frontier"])

    record = cache.decide_protection(
        branch.metadata,
        view,
        messages,
        cap=size * 10 - over,
        scale=1.0,
        max_chars=2048,
        decided_by="test",
    )

    assert record["included"] is (over == 0)
    assert record["size"] == size
    assert record["members"] == list(_ids(branch))
    assert record["frontier"]["message"] == 2 and record["frontier"]["slot"] == 0
    assert record["projection"]["max_chars"] == 2048
    assert cache.protection(branch.metadata) == record


@pytest.mark.asyncio
async def test_a_request_decides_the_round_once_and_anchors_its_end():
    branch = Branch()
    await _round_one(branch)
    _ins, messages, view = _compile(branch, "the next step")
    calls = []

    def decide(seen, compiled):
        calls.append(seen)
        return cache.decide_protection(
            branch.metadata,
            seen,
            compiled,
            cap=10**9,
            scale=1.0,
            max_chars=None,
            decided_by="test",
        )

    anchors, state = cache.cache_anchors(branch.metadata, view, messages, decide)
    again, state_again = cache.cache_anchors(branch.metadata, view, messages, decide)

    assert len(calls) == 1
    assert anchors == again == ((2, 0),)
    assert state == state_again == "kept"


@pytest.mark.asyncio
async def test_a_changed_head_is_recorded_and_keeps_the_members():
    branch = Branch()
    await _round_one(branch)
    _ins, messages, view = _compile(branch, "the next step")
    cache.decide_protection(
        branch.metadata, view, messages, cap=10**9, scale=1.0, max_chars=None, decided_by="test"
    )
    record = dict(cache.protection(branch.metadata))
    changed = [dict(m) for m in messages]
    changed[0] = {**changed[0], "content": "a different task"}

    anchors, state = cache.cache_anchors(branch.metadata, view, changed)

    assert anchors == ((2, 0),)
    assert state == "changed"
    assert cache.protection(branch.metadata) == record


@pytest.mark.asyncio
async def test_without_a_decider_the_round_stays_pending_and_only_a_note_is_anchored():
    branch = Branch()
    await _round_one(branch)
    _note(branch, 3)
    _ins, messages, view = _compile(branch, "the next step")

    anchors, state = cache.cache_anchors(branch.metadata, view, messages)

    assert cache.protection(branch.metadata) is None
    assert anchors == (view["note"],)
    assert state is None


@pytest.mark.asyncio
async def test_an_excluded_round_anchors_only_the_newest_note():
    branch = Branch()
    await _round_one(branch)
    _note(branch, 3)
    _ins, messages, view = _compile(branch, "the next step")
    cache.decide_protection(
        branch.metadata, view, messages, cap=1, scale=1.0, max_chars=None, decided_by="test"
    )

    anchors, state = cache.cache_anchors(branch.metadata, view, messages)

    assert cache.protection(branch.metadata)["included"] is False
    assert anchors == (view["note"],)
    assert state is None


# Durability


@pytest.mark.asyncio
async def test_the_decision_outlives_a_reset_of_the_rows_and_a_malformed_one_does_not():
    branch = Branch()
    await _round_one(branch)
    _ins, messages, view = _compile(branch, "the next step")
    record = cache.decide_protection(
        branch.metadata, view, messages, cap=10**9, scale=1.0, max_chars=None, decided_by="test"
    )
    branch.metadata["prompt_cache"]["entries"] = "not rows"

    ledger = cache.ledger_for(branch.metadata)

    assert ledger["entries"] == {}
    assert ledger["protected"] == record
    branch.metadata["prompt_cache"]["entries"] = "not rows"
    branch.metadata["prompt_cache"]["protected"] = {**record, "members": []}
    assert "protected" not in cache.ledger_for(branch.metadata)
    assert cache.protection(branch.metadata) is None


@pytest.mark.asyncio
async def test_a_clone_keeps_the_decision_for_its_own_messages():
    branch = Branch()
    await _round_one(branch)
    _ins, messages, view = _compile(branch, "the next step")
    record = cache.decide_protection(
        branch.metadata, view, messages, cap=10**9, scale=1.0, max_chars=None, decided_by="test"
    )

    clone = branch.clone()

    copied = cache.protection(clone.metadata)
    assert copied["members"] == [str(uid) for uid in clone.progression]
    assert copied["members"] != record["members"]
    assert {k: v for k, v in copied.items() if k != "members"} == {
        k: v for k, v in record.items() if k != "members"
    }
    renamed = dict(zip(record["members"], copied["members"]))
    assert cache.clone_ledger(branch.metadata["prompt_cache"], renamed)["protected"] == copied


@pytest.mark.asyncio
async def test_a_clone_missing_a_member_starts_its_round_pending_again():
    branch = Branch()
    await _round_one(branch)
    _ins, messages, view = _compile(branch, "the next step")
    record = cache.decide_protection(
        branch.metadata, view, messages, cap=10**9, scale=1.0, max_chars=None, decided_by="test"
    )
    partial = {member: f"clone-{member}" for member in record["members"][:-1]}

    copied = cache.clone_ledger(branch.metadata["prompt_cache"], partial)

    assert "protected" not in copied
    assert copied["entries"] == {}


# The frozen projection


@pytest.mark.asyncio
async def test_protected_results_keep_the_bound_they_were_decided_under():
    from lionagi.service.imodel import iModel

    model = iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        prompt_caching=True,
        tool_result_max_chars=1024,
    )
    branch = Branch(chat_model=model, parse_model=model)
    try:
        await _round_one(branch, output="head " * 250 + "MIDDLE" + " tail" * 4000)
        view = {}
        _ins, kw = _prepare_run_kwargs(
            branch, "the next step", ChatParam(), imodel=model, cache_view=view
        )
        decided = kw["messages"][2]["content"][0]
        cache.decide_protection(
            branch.metadata,
            view,
            kw["messages"],
            cap=10**9,
            scale=1.0,
            max_chars=model.tool_result_max_chars,
            decided_by="test",
        )
        assert "MIDDLE" not in decided["text"]

        model.tool_result_max_chars = 100_000
        _ins, kw = _prepare_run_kwargs(branch, "the next step", ChatParam(), imodel=model)

        assert kw["messages"][2]["content"][0] == decided
    finally:
        await branch.mdls.shutdown()
