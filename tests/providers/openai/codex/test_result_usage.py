# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: Codex's "turn.completed" (and the legacy "result"/"response"/
"session.end") events captured usage/cost/turns onto the CodexSession object
but never re-surfaced them as a StreamChunk, so run.py's "result" chunk
handler never saw them and every codex-driven session row ended up with
total_cost_usd frozen at 0.0 and zero token counts. These tests pin the fix:
both event shapes must also yield a StreamChunk(type="result", metadata=...),
mirroring gemini_code and claude_code."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi.providers.openai.codex import CodexCodeRequest, stream_codex_cli
from lionagi.service.types.stream_chunk import StreamChunk


def _make_request() -> CodexCodeRequest:
    return CodexCodeRequest(prompt="test", verbose_output=False)


async def _chunks_from_events(events: list[dict]) -> list[StreamChunk]:
    async def fake_events(_request):
        for ev in events:
            yield ev

    collected = []
    with patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake_events,
    ):
        async for item in stream_codex_cli(_make_request()):
            if isinstance(item, StreamChunk):
                collected.append(item)
    return collected


@pytest.mark.asyncio
async def test_turn_completed_yields_result_chunk_with_usage():
    """Codex's usage schema reports tokens; it typically does not report a
    dollar cost (unlike claude_code) -- see run.py's own note ("codex:
    tokens; claude_code: cost/turns/duration")."""
    events = [
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 200, "output_tokens": 60},
        }
    ]
    chunks = await _chunks_from_events(events)
    result_chunks = [c for c in chunks if c.type == "result"]
    assert len(result_chunks) == 1
    meta = result_chunks[0].metadata
    assert meta["usage"] == {"input_tokens": 200, "output_tokens": 60}
    assert meta["num_turns"] == 1
    assert "total_cost_usd" not in meta  # never reported -> never fabricated


@pytest.mark.asyncio
async def test_turn_completed_with_cost_reports_it():
    events = [
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "total_cost_usd": 0.002,
        }
    ]
    chunks = await _chunks_from_events(events)
    result_chunks = [c for c in chunks if c.type == "result"]
    assert len(result_chunks) == 1
    assert result_chunks[0].metadata["total_cost_usd"] == pytest.approx(0.002)


@pytest.mark.asyncio
async def test_turn_completed_without_usage_fabricates_no_tokens_or_cost():
    """num_turns is unconditionally incremented per turn.completed event (codex
    doesn't report a running turn counter), so a result chunk still appears --
    but it must never carry a "usage" or "total_cost_usd" key it wasn't given."""
    chunks = await _chunks_from_events([{"type": "turn.completed"}])
    result_chunks = [c for c in chunks if c.type == "result"]
    assert len(result_chunks) == 1
    meta = result_chunks[0].metadata
    assert "usage" not in meta
    assert "total_cost_usd" not in meta


@pytest.mark.asyncio
async def test_multiple_turn_completed_events_emit_marginal_deltas():
    """Codex may emit turn.completed more than once per run(); each event's
    usage/cost is cumulative-to-date, not a per-turn delta. Regression: this
    code used to re-emit the raw (cumulative) snapshot on every occurrence.
    Since _collect_branch_usage SUMS every "result" chunk's usage across all
    flushed messages, and a tool call between two turn.completed events can
    flush a separate message for each snapshot, re-emitting cumulative values
    double-counts earlier turns. Each event must instead carry only its
    marginal (this-turn-only) contribution, and num_turns must be exactly 1
    per event (never the running turn count)."""
    events = [
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        {"type": "turn.completed", "usage": {"input_tokens": 30, "output_tokens": 15}},
    ]
    chunks = await _chunks_from_events(events)
    result_chunks = [c for c in chunks if c.type == "result"]
    assert len(result_chunks) == 2
    assert result_chunks[0].metadata["num_turns"] == 1
    assert result_chunks[1].metadata["num_turns"] == 1
    assert result_chunks[0].metadata["usage"] == {"input_tokens": 10, "output_tokens": 5}
    # Second event's cumulative snapshot (30/15) minus the first (10/5).
    assert result_chunks[1].metadata["usage"] == {"input_tokens": 20, "output_tokens": 10}


@pytest.mark.asyncio
async def test_legacy_result_event_yields_result_chunk():
    events = [
        {
            "type": "result",
            "result": "done",
            "usage": {"input_tokens": 50, "output_tokens": 25},
            "num_turns": 2,
            "duration_ms": 900,
        }
    ]
    chunks = await _chunks_from_events(events)
    result_chunks = [c for c in chunks if c.type == "result"]
    assert len(result_chunks) == 1
    meta = result_chunks[0].metadata
    assert meta["usage"] == {"input_tokens": 50, "output_tokens": 25}
    assert meta["num_turns"] == 2
    assert meta["duration_ms"] == 900


@pytest.mark.asyncio
async def test_end_to_end_run_populates_branch_usage_from_codex_turn_completed():
    """Full path: run() consumes the provider's StreamChunks, stamps
    metadata["model_response"] on the flushed AssistantResponse, and
    _collect_branch_usage sums it into real totals with total_cost_usd left
    None (codex doesn't report cost here) -- not the old always-0.0 lie."""
    import types
    from unittest.mock import AsyncMock

    from lionagi.operations.run.run import RunParam, run
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch
    from lionagi.session.signal import _collect_branch_usage

    events = [
        {"type": "thread.started", "thread_id": "codex-thread-1"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "hi there"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 75, "output_tokens": 25},
        },
        {"type": "done"},
    ]
    chunks = await _chunks_from_events(events)

    m = iModel(provider="openai", model="gpt-5.5-codex", api_key="test_key")
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
    assert usage["input_tokens"] == 75
    assert usage["output_tokens"] == 25
    assert usage["total_cost_usd"] is None


@pytest.mark.asyncio
async def test_end_to_end_run_does_not_double_count_across_multiple_turns():
    """Regression for the multi-turn double-counting bug: a tool call between
    two turn.completed events flushes a separate AssistantResponse for each
    cumulative snapshot. _collect_branch_usage sums every message's usage, so
    if turn.completed re-emitted the raw cumulative snapshot each time, the
    first turn's tokens would be counted twice (once on the message flushed
    right after turn 1, again inside turn 2's cumulative total on the message
    flushed at the end). The collected totals must equal the FINAL cumulative
    snapshot (input=30, output=15) -- not the sum of the two raw snapshots
    (10+30=40, 5+15=20)."""
    import types
    from unittest.mock import AsyncMock

    from lionagi.operations.run.run import RunParam, run
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch
    from lionagi.session.signal import _collect_branch_usage

    events = [
        {"type": "thread.started", "thread_id": "codex-thread-multi"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "turn one"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        {
            "type": "item.completed",
            "item": {
                "type": "function_call",
                "id": "call-1",
                "name": "some_tool",
                "arguments": {"x": 1},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "tool result",
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "turn two"}},
        {"type": "turn.completed", "usage": {"input_tokens": 30, "output_tokens": 15}},
        {"type": "done"},
    ]
    chunks = await _chunks_from_events(events)

    m = iModel(provider="openai", model="gpt-5.5-codex", api_key="test_key")
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

    # Sanity: the tool call really did split the turn across two flushed
    # AssistantResponse messages (the double-counting bug only manifests
    # with at least two such messages carrying usage metadata).
    assistant_messages_with_usage = [
        msg
        for msg in branch.msgs.messages
        if isinstance(msg.metadata.get("model_response"), dict) and msg.metadata["model_response"]
    ]
    assert len(assistant_messages_with_usage) >= 2

    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 30
    assert usage["output_tokens"] == 15


@pytest.mark.asyncio
async def test_end_to_end_run_sums_result_chunks_within_a_single_flush_window():
    """Two turn.completed events with NO tool call (and thus no intervening
    flush) between them land in the SAME result_meta accumulation window
    inside run.py. Each event already carries its own marginal delta (per
    codex.py's turn.completed handler); run.py must SUM those deltas into
    result_meta rather than dict.update()-replacing them, or the first
    turn's contribution is silently discarded when the second turn's
    "usage"/"num_turns" keys overwrite it wholesale."""
    import types
    from unittest.mock import AsyncMock

    from lionagi.operations.run.run import RunParam, run
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch
    from lionagi.session.signal import _collect_branch_usage

    events = [
        {"type": "thread.started", "thread_id": "codex-thread-no-intervening-tool"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "turn one"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "turn two"}},
        {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 8}},
        {"type": "done"},
    ]
    chunks = await _chunks_from_events(events)

    # Sanity: codex.py already emits marginal (not cumulative) deltas per
    # event -- {10,5} then {10,3} (20-10, 8-5) -- confirming this test
    # exercises run.py's accumulation, not codex.py's delta computation.
    result_chunks = [c for c in chunks if c.type == "result"]
    assert len(result_chunks) == 2
    assert result_chunks[0].metadata["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert result_chunks[1].metadata["usage"] == {"input_tokens": 10, "output_tokens": 3}

    m = iModel(provider="openai", model="gpt-5.5-codex", api_key="test_key")
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

    # No tool call fired between the two turn.completed events, so both
    # deltas land on the SAME (only) flushed AssistantResponse.
    assistant_messages_with_usage = [
        msg
        for msg in branch.msgs.messages
        if isinstance(msg.metadata.get("model_response"), dict) and msg.metadata["model_response"]
    ]
    assert len(assistant_messages_with_usage) == 1

    usage = _collect_branch_usage(branch)
    assert usage["input_tokens"] == 20
    assert usage["output_tokens"] == 8
    assert assistant_messages_with_usage[0].metadata["model_response"]["num_turns"] == 2
