# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0


import pytest
from pydantic import BaseModel

from lionagi._errors import InputTokenCapError
from lionagi.operations.parse.parse import ExtractionError
from lionagi.protocols.messages.assistant_response import AssistantResponse
from lionagi.session.branch import Branch
from lionagi.testing import LionAGIMockFactory


class Answer(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_successful_repair_replaces_the_assistant_turn_in_later_api_requests():
    original = "unstructured original response"
    repaired = '{"answer":"repaired value"}'
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=[original, repaired, '{"answer":"later value"}']
    )
    assert not branch.chat_model.is_cli

    first = await branch.operate(
        instruction="First question",
        response_format=Answer,
        invoke_actions=False,
    )
    assert first.answer == "repaired value"
    await branch.operate(instruction="Next question", invoke_actions=False, skip_validation=True)

    assert branch.chat_model.invoke.await_count == 3
    messages = branch.chat_model.invoke.await_args_list[2].kwargs["messages"]
    assistant_turns = [message["content"] for message in messages if message["role"] == "assistant"]
    assert assistant_turns == [repaired]
    assert original not in assistant_turns


@pytest.mark.asyncio
async def test_react_api_next_round_receives_repaired_analysis():
    original = "unstructured analysis response"
    repaired = '{"analysis":"corrected analysis","extension_needed":false}'
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=[original, repaired, '{"answer":"done"}']
    )
    assert not branch.chat_model.is_cli
    result = await branch.ReAct(instruction="Question", response_format=Answer, max_extensions=0)
    assert branch.chat_model.invoke.await_count == 3, result
    messages = branch.chat_model.invoke.await_args_list[2].kwargs["messages"]
    assistant_turns = [message["content"] for message in messages if message["role"] == "assistant"]
    assert assistant_turns == [repaired]
    assert original not in assistant_turns


@pytest.mark.asyncio
async def test_first_valid_reply_stays_in_history_without_repair():
    original = '{"answer":"already valid"}'
    branch = LionAGIMockFactory.create_mocked_branch(responses=[original])
    result = await branch.operate(
        instruction="Question", response_format=Answer, invoke_actions=False
    )
    assert result.answer == "already valid"
    assert branch.chat_model.invoke.await_count == 1
    message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
    assert message.response == original
    assert "original_text" not in message.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["raise", "return_none", "return_value"])
async def test_failed_repair_keeps_original_history(mode):
    original = "not structured"
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=[original] + ["still not structured"] * 4
    )
    kwargs = dict(
        instruction="Question",
        response_format=Answer,
        invoke_actions=False,
        handle_validation=mode,
    )
    if mode == "raise":
        with pytest.raises(ExtractionError):
            await branch.operate(**kwargs)
    else:
        result = await branch.operate(**kwargs)
        assert (result is None) == (mode == "return_none")
    assert branch.chat_model.invoke.await_count == 5
    message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
    assert message.response == original
    assert "original_text" not in message.metadata


@pytest.mark.asyncio
async def test_empty_reply_does_not_trigger_repair():
    branch = LionAGIMockFactory.create_mocked_branch(responses=[""])
    result = await branch.operate(
        instruction="Question",
        response_format=Answer,
        invoke_actions=False,
        handle_validation="return_none",
    )
    assert result is None
    assert branch.chat_model.invoke.await_count == 1
    message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
    assert message.response == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        InputTokenCapError("refused", details={"estimate": 9, "cap": 1}),
    ],
)
async def test_refused_repair_keeps_original_history(monkeypatch, refusal):
    original = "unstructured reply"
    branch = LionAGIMockFactory.create_mocked_branch(responses=[original])

    async def refuse(*args, **kwargs):
        raise refusal

    monkeypatch.setattr(Branch, "chat", refuse)
    with pytest.raises(type(refusal)):
        await branch.operate(
            instruction="Question",
            response_format=Answer,
            invoke_actions=False,
            handle_validation="return_value",
        )
    assert branch.chat_model.invoke.await_count == 1
    message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
    assert message.response == original
    assert "original_text" not in message.metadata


@pytest.mark.asyncio
async def test_later_successful_retry_is_the_history_text():
    original = "unstructured"
    repaired = '{"answer":"finally"}'
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=[original, "still unstructured", repaired, '{"answer":"next"}']
    )
    result = await branch.operate(
        instruction="Question", response_format=Answer, invoke_actions=False
    )
    assert result.answer == "finally"
    await branch.operate(instruction="Next", invoke_actions=False, skip_validation=True)
    messages = branch.chat_model.invoke.await_args_list[3].kwargs["messages"]
    assert [m["content"] for m in messages if m["role"] == "assistant"] == [repaired]
    message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
    assert message.metadata["original_text"] == original
