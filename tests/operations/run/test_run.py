# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.operations.run.run — the CLI streaming operation."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from lionagi.operations.run.run import RunParam, run, run_and_collect
from lionagi.operations.types import ChatParam
from lionagi.protocols.messages import (
    ActionRequest,
    ActionResponse,
    AssistantResponse,
    AssistantResponseContent,
    Instruction,
)
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_cli_model(chunks: list[StreamChunk], session_id: str | None = None):
    """Return (model, captured_kwargs_dict) where model is an iModel patched to
    behave as a CLI endpoint yielding *chunks* from its stream() method."""
    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=session_id,
        to_dict=lambda: {"type": "fake_cli", "session_id": session_id},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None
    captured: dict = {}

    async def create_event(**kw):
        captured.update(kw)
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        for chunk in chunks:
            yield chunk

    m.stream = stream
    return m, captured


async def _collect(gen) -> list:
    """Drain an async generator into a list."""
    results = []
    async for item in gen:
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# P0 tests — run()
# ---------------------------------------------------------------------------


async def test_run_rejects_non_cli_chat_model():
    """run() raises ValueError when chat_model is not a CLI endpoint."""
    branch = Branch()
    # Default iModel is not CLI
    assert not branch.chat_model.is_cli

    with pytest.raises(ValueError, match="run operation only supports CLI endpoints"):
        async for _ in run(branch, "hello", RunParam()):
            pass


async def test_run_passes_resume_from_provider_session_id_and_updates_endpoint_session():
    """resume kwarg forwarded from provider_session_id; system chunk updates endpoint."""
    model, captured = _make_fake_cli_model(
        [
            StreamChunk(
                type="system",
                metadata={"session_id": "new-session"},
            ),
            StreamChunk(type="text", content="ok"),
        ],
        session_id="old-session",
    )
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "hi", RunParam()))

    # create_event should have received resume="old-session"
    assert captured.get("resume") == "old-session"
    # Endpoint session should be updated from the system chunk
    assert model.endpoint.session_id == "new-session"
    # Final yielded text message
    text_msgs = [r for r in results if isinstance(r, AssistantResponse)]
    assert len(text_msgs) == 1
    assert text_msgs[0].response == "ok"


async def test_run_flushes_text_before_tool_use_and_links_tool_result():
    """Text is flushed before tool_use; tool_result is linked to the request."""
    model, _ = _make_fake_cli_model(
        [
            StreamChunk(type="thinking", content="think"),
            StreamChunk(type="text", content="before"),
            StreamChunk(
                type="tool_use",
                tool_name="fn",
                tool_id="call-1",
                tool_input={"x": 1},
            ),
            StreamChunk(
                type="tool_result",
                tool_id="call-1",
                tool_output={"v": 1},
            ),
            StreamChunk(type="text", content="after"),
        ]
    )
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "go", RunParam()))

    type_seq = [type(r).__name__ for r in results]
    assert type_seq == [
        "Instruction",
        "AssistantResponse",
        "ActionRequest",
        "ActionResponse",
        "AssistantResponse",
    ], f"Unexpected sequence: {type_seq}"

    # "before" text with thinking metadata
    first_ar: AssistantResponse = results[1]
    assert first_ar.response == "before"
    assert first_ar.metadata.get("thinking") == "think"

    # Tool name preserved on request
    act_req: ActionRequest = results[2]
    assert act_req.function == "fn"

    # "after" text
    last_ar: AssistantResponse = results[4]
    assert last_ar.response == "after"


async def test_run_unmatched_tool_result_is_skipped():
    """tool_result with unknown tool_id is silently skipped (no matching request)."""
    model, _ = _make_fake_cli_model(
        [
            StreamChunk(
                type="tool_result",
                tool_id="missing",
                tool_name="read",
                tool_output={"error": "no request"},
                is_error=True,
            ),
        ]
    )
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "go", RunParam()))

    action_responses = [r for r in results if isinstance(r, ActionResponse)]
    assert len(action_responses) == 0, "Unmatched tool_result should be skipped"


async def test_run_matched_tool_result_with_error():
    """Matched tool_result with is_error=True preserves error metadata."""
    model, _ = _make_fake_cli_model(
        [
            StreamChunk(
                type="tool_use",
                tool_id="call_1",
                tool_name="read",
                tool_input={"path": "/tmp"},
            ),
            StreamChunk(
                type="tool_result",
                tool_id="call_1",
                tool_name="read",
                tool_output={"error": "permission denied"},
                is_error=True,
            ),
        ]
    )
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "go", RunParam()))

    action_responses = [r for r in results if isinstance(r, ActionResponse)]
    assert len(action_responses) == 1
    assert action_responses[0].metadata.get("is_error") is True
    assert action_responses[0].function == "read"


async def test_run_error_chunk_raises_and_restores_streaming_processor():
    """Error chunk raises RuntimeError; finally block restores streaming_process_func."""
    sentinel = object()
    model, _ = _make_fake_cli_model([StreamChunk(type="error", content="boom")])
    model.streaming_process_func = sentinel

    branch = Branch()
    branch.chat_model = model

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in run(branch, "go", RunParam()):
            pass

    # finally block in run() restores the original streaming_process_func
    assert model.streaming_process_func is sentinel


async def test_run_stream_persist_writes_final_state_and_removes_buffer(tmp_path):
    """stream_persist=True writes branch JSON and removes buffer JSONL."""
    model, _ = _make_fake_cli_model([StreamChunk(type="text", content="done")])
    branch = Branch()
    branch.chat_model = model

    param = RunParam(stream_persist=True, persist_dir=tmp_path)
    await _collect(run(branch, "persist-me", param))

    # Branch JSON should exist
    json_files = list(tmp_path.glob("*.json"))
    assert json_files, "Expected branch JSON file after stream_persist"

    # Buffer JSONL should be removed after successful completion
    buffer_files = list(tmp_path.glob("*.jsonl"))
    assert not buffer_files, f"Buffer JSONL should be removed: {buffer_files}"

    # Original streaming processor restored
    assert model.streaming_process_func is None


# ---------------------------------------------------------------------------
# P0/P1 tests — run_and_collect()
# ---------------------------------------------------------------------------


async def test_run_and_collect_clears_messages_and_joins_assistant_text(monkeypatch):
    """clear_messages=True clears branch before run; text chunks are joined."""
    branch = Branch()
    # Add a prior message so we can confirm it gets cleared
    branch.msgs.add_message(
        instruction=branch.msgs.create_instruction(instruction="prior")
    )
    assert len(branch.messages) == 1

    def make_ar(text: str) -> AssistantResponse:
        ar = AssistantResponse(
            content=AssistantResponseContent(assistant_response=text),
            sender=branch.id,
            recipient="user",
        )
        return ar

    async def fake_run(b, ins, param):
        yield make_ar("one")
        yield make_ar("two")

    monkeypatch.setattr("lionagi.operations.run.run.run", fake_run)

    result = await run_and_collect(
        branch,
        "test",
        ChatParam(),
        skip_validation=True,
        clear_messages=True,
    )

    # Messages cleared before run; fake_run doesn't add any
    assert len(branch.messages) == 0
    assert result == "one\n\ntwo"


async def test_run_and_collect_parses_when_response_format_is_set(monkeypatch):
    """When response_format is set, run_and_collect passes full text to parse."""
    from lionagi.operations.types import ParseParam

    class Answer(BaseModel):
        value: int

    branch = Branch()
    parse_calls: list[str] = []

    async def fake_run(b, ins, param):
        ar = AssistantResponse(
            content=AssistantResponseContent(assistant_response='{"value": 42}'),
            sender=branch.id,
            recipient="user",
        )
        yield ar

    async def fake_parse(b, text, pp):
        parse_calls.append(text)
        return Answer(value=42)

    monkeypatch.setattr("lionagi.operations.run.run.run", fake_run)
    # Patch at the source module since run_and_collect uses a lazy import
    monkeypatch.setattr("lionagi.operations.parse.parse.parse", fake_parse)

    from lionagi.operations.parse.parse import get_default_call

    pp = ParseParam(
        response_format=Answer,
        imodel=branch.chat_model,
        imodel_kw={},
        alcall_params=get_default_call(),
    )

    result = await run_and_collect(branch, "test", ChatParam(), parse_param=pp)

    assert isinstance(result, Answer)
    assert result.value == 42
    assert len(parse_calls) == 1
    assert parse_calls[0] == '{"value": 42}'
