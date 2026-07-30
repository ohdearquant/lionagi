# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest, stream_claude_code_cli
from lionagi.service.types import StreamChunk


async def _chunks(events: list[dict]) -> list[StreamChunk]:
    async def fake_events(_request):
        for event in events:
            yield event

    out: list[StreamChunk] = []
    request = ClaudeCodeRequest(prompt="test", include_partial_messages=True)
    with patch(
        "lionagi.providers.anthropic.claude_code.stream_cc_cli_events",
        side_effect=fake_events,
    ):
        async for item in stream_claude_code_cli(request):
            if isinstance(item, StreamChunk):
                out.append(item)
    return out


@pytest.mark.asyncio
async def test_partial_text_events_are_forwarded_as_deltas_without_duplicate_final_text():
    events = [
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "msg-1"}},
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hel"},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "lo"},
            },
        },
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "content": [{"type": "text", "text": "hello"}],
            },
        },
        {"type": "done"},
    ]

    chunks = await _chunks(events)
    text_chunks = [chunk for chunk in chunks if chunk.type == "text"]

    assert [chunk.content for chunk in text_chunks] == ["hel", "lo"]
    assert all(chunk.is_delta for chunk in text_chunks)


@pytest.mark.asyncio
async def test_complete_assistant_text_is_kept_when_no_partial_delta_arrived():
    chunks = await _chunks(
        [
            {
                "type": "assistant",
                "message": {
                    "id": "msg-1",
                    "content": [{"type": "text", "text": "complete"}],
                },
            },
            {"type": "done"},
        ]
    )

    text_chunks = [chunk for chunk in chunks if chunk.type == "text"]
    assert [(chunk.content, chunk.is_delta) for chunk in text_chunks] == [("complete", False)]


@pytest.mark.asyncio
async def test_partial_text_does_not_suppress_final_tool_use_from_same_message():
    chunks = await _chunks(
        [
            {
                "type": "stream_event",
                "event": {"type": "message_start", "message": {"id": "msg-1"}},
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Checking"},
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg-1",
                    "content": [
                        {"type": "text", "text": "Checking"},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Read",
                            "input": {"file_path": "README.md"},
                        },
                    ],
                },
            },
            {"type": "done"},
        ]
    )

    assert [chunk.content for chunk in chunks if chunk.type == "text"] == ["Checking"]
    tool_chunks = [chunk for chunk in chunks if chunk.type == "tool_use"]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].tool_name == "Read"
