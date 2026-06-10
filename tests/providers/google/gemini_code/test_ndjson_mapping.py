# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for gemini_code NDJSON event mapping.

Covers two bugs reproduced from live gemini CLI stream-json output:

Bug A — tool payload loss: the real `tool_use` events carry arguments under
`parameters`, not `input`/`args`, and `tool_result` events carry the output
under `output`, not `content`/`result`.  Both fields were dropped, producing
empty `{}` argument dicts and empty output strings in the persisted session.

Bug B — assistant-answer echo: the gemini CLI echoes the user prompt as a
`{"type":"message","role":"user",...}` event before the assistant reply.
Both events share `type=="message"`, so the echo's text was included in the
fallback result accumulation, making `session.result` start with the raw
user prompt instead of the model's answer.
"""

from __future__ import annotations

import pytest

from lionagi.providers.google.gemini_code.models import (
    GeminiCodeRequest,
    GeminiSession,
    stream_gemini_cli,
)


def _make_request() -> GeminiCodeRequest:
    return GeminiCodeRequest(prompt="test", verbose_output=False)


async def _run_events(events: list[dict]) -> GeminiSession:
    """Drive stream_gemini_cli with a mocked event stream; return the final session."""
    from unittest.mock import patch

    async def fake_events(_request):
        for ev in events:
            yield ev
        yield {"type": "done"}

    session = None
    with patch(
        "lionagi.providers.google.gemini_code.models.stream_gemini_cli_events",
        side_effect=fake_events,
    ):
        async for item in stream_gemini_cli(_make_request()):
            if isinstance(item, GeminiSession):
                session = item

    assert session is not None, "stream_gemini_cli did not yield a GeminiSession"
    return session


# ---------------------------------------------------------------------------
# Bug A — tool payload: arguments land in session.tool_uses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_use_parameters_key_captured():
    """A `tool_use` event whose args are under `parameters` must be fully captured.

    Real event shape observed from the gemini CLI:
      {"type":"tool_use","tool_name":"google_web_search",
       "tool_id":"google_web_search__...", "parameters":{"query":"France capital"}}
    """
    events = [
        {"type": "init", "session_id": "s1", "model": "gemini-3-flash-preview"},
        {
            "type": "tool_use",
            "tool_name": "google_web_search",
            "tool_id": "tu-001",
            "parameters": {"query": "France capital"},
        },
        {"type": "result", "status": "success", "stats": {}},
    ]
    session = await _run_events(events)

    assert len(session.tool_uses) == 1, (
        f"expected 1 tool_use, got {len(session.tool_uses)}: {session.tool_uses}"
    )
    tu = session.tool_uses[0]
    assert tu["name"] == "google_web_search", f"wrong name: {tu}"
    assert tu["id"] == "tu-001", f"wrong id: {tu}"
    assert tu["input"] == {"query": "France capital"}, (
        f"tool arguments must be captured from 'parameters' key; got input={tu['input']!r}"
    )


@pytest.mark.asyncio
async def test_tool_result_output_key_captured():
    """A `tool_result` event whose payload is under `output` must be fully captured.

    Real event shape observed from the gemini CLI:
      {"type":"tool_result","tool_id":"google_web_search__...",
       "status":"success","output":"Search results returned."}
    """
    events = [
        {"type": "init", "session_id": "s1", "model": "gemini-3-flash-preview"},
        {
            "type": "tool_result",
            "tool_id": "tu-001",
            "status": "success",
            "output": "Paris is the capital of France.",
        },
        {"type": "result", "status": "success", "stats": {}},
    ]
    session = await _run_events(events)

    assert len(session.tool_results) == 1, (
        f"expected 1 tool_result, got {len(session.tool_results)}: {session.tool_results}"
    )
    tr = session.tool_results[0]
    assert tr["tool_use_id"] == "tu-001", f"wrong tool_use_id: {tr}"
    assert tr["content"] == "Paris is the capital of France.", (
        f"tool output must be captured from 'output' key; got content={tr['content']!r}"
    )
    assert tr["is_error"] is False, f"status=success must not be flagged as error: {tr}"


@pytest.mark.asyncio
async def test_tool_result_error_status_flagged():
    """A `tool_result` with status != success must be flagged as is_error=True."""
    events = [
        {"type": "init", "session_id": "s1", "model": "gemini-3-flash-preview"},
        {
            "type": "tool_result",
            "tool_id": "tu-002",
            "status": "error",
            "output": "Tool execution failed.",
        },
        {"type": "result", "status": "success", "stats": {}},
    ]
    session = await _run_events(events)

    assert len(session.tool_results) == 1
    tr = session.tool_results[0]
    assert tr["is_error"] is True, (
        f"status=error must set is_error=True; got is_error={tr['is_error']!r}"
    )


# ---------------------------------------------------------------------------
# Bug B — assistant answer, not user echo, becomes session.result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assistant_answer_not_user_echo_is_result():
    """session.result must be the assistant reply, not the echoed user prompt.

    The gemini CLI emits the user prompt as a message event before the
    assistant reply.  Both share type=="message".  Only the assistant role
    content must appear in session.result.

    Real sequence observed:
      {"type":"message","role":"user","content":"ping"}
      {"type":"message","role":"assistant","content":"Pong!","delta":true}
    """
    events = [
        {"type": "init", "session_id": "s1", "model": "gemini-3-flash-preview"},
        {"type": "message", "role": "user", "content": "ping"},
        {"type": "message", "role": "assistant", "content": "Pong!", "delta": True},
        {"type": "result", "status": "success", "stats": {}},
    ]
    session = await _run_events(events)

    assert "ping" not in session.result, (
        f"user echo must NOT appear in session.result; got {session.result!r}"
    )
    assert "Pong!" in session.result, (
        f"assistant answer must appear in session.result; got {session.result!r}"
    )


@pytest.mark.asyncio
async def test_user_echo_not_added_to_messages():
    """The user echo event must not be appended to session.messages.

    session.messages should contain only assistant turns so that resume context
    doesn't re-submit the user's own words as assistant output.
    """
    events = [
        {"type": "init", "session_id": "s1", "model": "gemini-3-flash-preview"},
        {"type": "message", "role": "user", "content": "Reply with exactly: OK"},
        {"type": "message", "role": "assistant", "content": "OK", "delta": True},
        {"type": "result", "status": "success", "stats": {}},
    ]
    session = await _run_events(events)

    for msg in session.messages:
        role = msg.get("role", "unknown")
        assert role != "user", (
            f"user echo must not be in session.messages; found role={role!r}, msg={msg}"
        )


@pytest.mark.asyncio
async def test_multi_delta_assistant_chunks_joined():
    """Multiple assistant delta chunks must be joined into a single result string."""
    events = [
        {"type": "init", "session_id": "s1", "model": "gemini-3-flash-preview"},
        {"type": "message", "role": "user", "content": "count"},
        {"type": "message", "role": "assistant", "content": "one ", "delta": True},
        {"type": "message", "role": "assistant", "content": "two ", "delta": True},
        {"type": "message", "role": "assistant", "content": "three", "delta": True},
        {"type": "result", "status": "success", "stats": {}},
    ]
    session = await _run_events(events)

    assert "one" in session.result and "two" in session.result and "three" in session.result, (
        f"all assistant delta parts must appear in result; got {session.result!r}"
    )
    assert "count" not in session.result, (
        f"user echo 'count' must not appear in result; got {session.result!r}"
    )


# ---------------------------------------------------------------------------
# Combined: tool call + answer, no echo contamination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_tool_turn_preserves_all_payloads():
    """A complete turn with user echo, tool call, tool result, and assistant answer
    must land everything correctly: args/output captured, result = assistant text only.
    """
    events = [
        {"type": "init", "session_id": "s1", "model": "gemini-3-flash-preview"},
        {
            "type": "message",
            "role": "user",
            "content": "Search the web for: what is the capital of France",
        },
        {
            "type": "tool_use",
            "tool_name": "google_web_search",
            "tool_id": "tu-abc",
            "parameters": {"query": "what is the capital of France"},
        },
        {
            "type": "tool_result",
            "tool_id": "tu-abc",
            "status": "success",
            "output": "Search results for 'what is the capital of France' returned.",
        },
        {
            "type": "message",
            "role": "assistant",
            "content": "The capital of France is Paris.",
            "delta": True,
        },
        {"type": "result", "status": "success", "stats": {}},
    ]
    session = await _run_events(events)

    # Tool use captured with full arguments
    assert len(session.tool_uses) == 1
    tu = session.tool_uses[0]
    assert tu["input"] == {"query": "what is the capital of France"}, (
        f"tool args must be captured; got {tu['input']!r}"
    )

    # Tool result captured with full output
    assert len(session.tool_results) == 1
    tr = session.tool_results[0]
    assert "capital of France" in tr["content"], (
        f"tool output must be captured; got {tr['content']!r}"
    )

    # Result is the assistant answer, not the user echo
    assert "Paris" in session.result, f"assistant answer must be in result; got {session.result!r}"
    assert "Search the web" not in session.result, (
        f"user echo must not be in result; got {session.result!r}"
    )
