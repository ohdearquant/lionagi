# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: the Claude Code CLI's terminal "result" event (usage,
total_cost_usd, num_turns, duration_ms) was captured onto the CLISession
object but never re-surfaced as a StreamChunk, so run.py's "result" chunk
handler (the only channel that stamps usage onto an AssistantResponse's
metadata["model_response"]) never saw it -- every claude_code-driven session
row ended up with total_cost_usd frozen at 0.0 and zero token counts, even
though the CLI reported real values. These tests pin the fix: the "result"
event must also yield a StreamChunk(type="result", metadata=...) carrying the
same fields gemini_code already emits.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest, stream_claude_code_cli
from lionagi.service.types.stream_chunk import StreamChunk


def _make_request() -> ClaudeCodeRequest:
    return ClaudeCodeRequest(prompt="test", verbose_output=False)


async def _chunks_from_events(events: list[dict]) -> list[StreamChunk]:
    async def fake_events(_request):
        for ev in events:
            yield ev

    collected = []
    with patch(
        "lionagi.providers.anthropic.claude_code.stream_cc_cli_events",
        side_effect=fake_events,
    ):
        async for item in stream_claude_code_cli(_make_request()):
            if isinstance(item, StreamChunk):
                collected.append(item)
    return collected


def _result_event(**over) -> dict:
    ev = {
        "type": "result",
        "result": "done",
        "usage": {"input_tokens": 120, "output_tokens": 40},
        "total_cost_usd": 0.0123,
        "num_turns": 3,
        "duration_ms": 4500,
        "duration_api_ms": 4000,
        "is_error": False,
    }
    ev.update(over)
    return ev


@pytest.mark.asyncio
async def test_result_event_yields_result_chunk_with_usage_and_cost():
    chunks = await _chunks_from_events([_result_event()])
    result_chunks = [c for c in chunks if c.type == "result"]
    assert len(result_chunks) == 1
    meta = result_chunks[0].metadata
    assert meta["usage"] == {"input_tokens": 120, "output_tokens": 40}
    assert meta["total_cost_usd"] == pytest.approx(0.0123)
    assert meta["num_turns"] == 3
    assert meta["duration_ms"] == 4500


@pytest.mark.asyncio
async def test_result_event_without_usage_yields_no_result_chunk():
    """A result event carrying no usage/cost/turns/duration must not fabricate
    one -- no StreamChunk(type="result") at all, rather than one with zeros."""
    chunks = await _chunks_from_events(
        [
            {
                "type": "result",
                "result": "done",
                "usage": {},
                "total_cost_usd": None,
                "num_turns": None,
                "duration_ms": None,
                "duration_api_ms": None,
                "is_error": False,
            }
        ]
    )
    result_chunks = [c for c in chunks if c.type == "result"]
    assert result_chunks == []


@pytest.mark.asyncio
async def test_end_to_end_run_populates_branch_usage_from_claude_code_result():
    """Full path: run() consumes the provider's StreamChunks, stamps
    metadata["model_response"] on the flushed AssistantResponse, and
    _collect_branch_usage sums it into real (non-zero, non-None-cost) totals --
    the exact seam that was dead before this fix."""
    import types
    from unittest.mock import AsyncMock

    from lionagi.operations.run.run import RunParam, run
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch
    from lionagi.session.signal import _collect_branch_usage

    events = [
        {"type": "system", "session_id": "cc-sess-1", "model": "sonnet-5", "tools": []},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        },
        _result_event(),
        {"type": "done"},
    ]
    chunks = await _chunks_from_events(events)

    m = iModel(provider="anthropic", model="claude-3-5-sonnet-20241022", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        to_dict=lambda: {"type": "fake_cli"},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None

    async def create_event(**kw):
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        for chunk in chunks:
            yield chunk

    m.stream = stream

    branch = Branch()
    branch.chat_model = m

    async for _ in run(branch, "hi", RunParam()):
        pass

    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 120
    assert usage["output_tokens"] == 40
    assert usage["total_cost_usd"] == pytest.approx(0.0123)
    assert usage["num_turns"] == 3
