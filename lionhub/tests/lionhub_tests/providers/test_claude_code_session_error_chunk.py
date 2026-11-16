# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: a Claude Code session that ended in error looked exactly like one
that succeeded.

The CLI's terminal "result" event sets ``is_error`` on the CLISession, and the
endpoint's ``stream()`` drops the session rather than yielding it. The result
chunk emitted alongside it carries usage, cost, turns and duration and never the
error flag, so nothing a streaming consumer receives distinguishes a failed run
from a good one -- and the one consumer that inspects ``chunk.is_error`` could
never observe a session-terminal failure. Per-tool failures were unaffected;
they already have their own chunk carriers.

These tests pin the contract: a result event carrying ``is_error=True`` must
produce an error chunk on the streaming path.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi._provider_errors import (
    ProviderQuotaError,
    ProviderStreamDisconnectError,
    classify_provider_error,
)
from lionagi.service.types.cli_session import CLISession
from lionagi.service.types.stream_chunk import StreamChunk
from lionhub.providers.anthropic.claude_code import (
    ClaudeCodeCLIEndpoint,
    ClaudeCodeRequest,
)


def _result_event(**over) -> dict:
    ev = {
        "type": "result",
        "result": "the model refused",
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "total_cost_usd": 0.001,
        "num_turns": 1,
        "duration_ms": 100,
        "duration_api_ms": 90,
        "is_error": True,
    }
    ev.update(over)
    return ev


async def _endpoint_chunks(events: list[dict]) -> list[StreamChunk]:
    """Drive the ENDPOINT, not the underlying generator.

    The generator yields the CLISession; the endpoint is what decides whether
    anything downstream ever learns what was on it, so a test against the
    generator would pass while the defect was live.
    """

    async def fake_events(_request):
        for ev in events:
            yield ev

    endpoint = ClaudeCodeCLIEndpoint()
    request = ClaudeCodeRequest(prompt="test", verbose_output=False)
    collected: list[StreamChunk] = []
    with patch(
        "lionhub.providers.anthropic.claude_code.stream_cc_cli_events",
        side_effect=fake_events,
    ):
        async for chunk in endpoint.stream({"request": request}):
            collected.append(chunk)
    return collected


@pytest.mark.asyncio
async def test_a_session_that_ends_in_error_yields_an_error_chunk():
    chunks = await _endpoint_chunks([_result_event(), {"type": "done"}])
    errors = [c for c in chunks if c.type == "error"]
    assert len(errors) == 1, "a failed session produced nothing a consumer can branch on"
    assert errors[0].is_error is True
    assert errors[0].content == "the model refused", (
        "the error chunk must carry the provider's own reason, not a generic string"
    )


@pytest.mark.asyncio
async def test_the_error_chunk_falls_back_to_a_named_reason_when_the_provider_gave_none():
    """An empty result must not produce an error chunk with empty content.

    A consumer branching on the chunk gets nothing to report, which is how a
    real failure ends up logged as a blank line.
    """
    chunks = await _endpoint_chunks([_result_event(result=""), {"type": "done"}])
    errors = [c for c in chunks if c.type == "error"]
    assert len(errors) == 1
    assert errors[0].content == "Claude Code session failed"


@pytest.mark.asyncio
async def test_a_successful_session_yields_no_error_chunk():
    """The other direction. A fix that emits unconditionally would turn every
    healthy run into a reported failure, which is worse than the defect."""
    chunks = await _endpoint_chunks([_result_event(is_error=False), {"type": "done"}])
    assert [c for c in chunks if c.type == "error"] == []
    assert [c for c in chunks if c.type == "result"], (
        "the result chunk stopped being emitted, so this asserts nothing about the error path"
    )


@pytest.mark.asyncio
async def test_a_failure_already_carrying_an_error_chunk_is_not_reported_twice():
    """Pins the guard, which is vacuous on the current event set.

    Nothing in the module constructs an error chunk today, so no sequence of
    real events reaches this branch; the session is built directly instead. The
    contract is pinned now because the emission being guarded lives in this file
    and a future error chunk added a few lines away would otherwise double-report.
    Read this as documenting intent, not as covering a reachable path.

    Deleting the guard does make this fail, so it is not passing vacuously. Those
    are two independent things and only the first one holds: the guard expression
    cannot be silently removed, and the guard is still never reached by a real
    event sequence. Seeing this go red is not evidence that it fires in
    production, which is the inference it cannot support.
    """
    session = CLISession()
    session.is_error = True
    session.result = "already reported"
    session.chunks.append(StreamChunk(type="error", content="already reported", is_error=True))

    async def only_the_session(_request, **_kwargs):
        yield session

    endpoint = ClaudeCodeCLIEndpoint()
    request = ClaudeCodeRequest(prompt="test", verbose_output=False)
    collected: list[StreamChunk] = []
    with patch(
        "lionhub.providers.anthropic.claude_code.stream_claude_code_cli",
        side_effect=only_the_session,
    ):
        async for chunk in endpoint.stream({"request": request}):
            collected.append(chunk)

    assert [c for c in collected if c.type == "error"] == [], (
        "the session already carried an error chunk, so the endpoint added a second report"
    )


def _partial_turn(emit_done: bool) -> list[dict]:
    events = [
        {"type": "system", "session_id": "test-session"},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "partial text"},
            },
        },
    ]
    if emit_done:
        events.append({"type": "done"})
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("emit_done", [False, True], ids=["eof", "done"])
async def test_missing_result_yields_error_after_partial_text(emit_done):
    chunks = await _endpoint_chunks(_partial_turn(emit_done))

    assert [c.type for c in chunks] == ["system", "text", "error"]
    assert chunks[0].metadata["session_id"] == "test-session"
    assert chunks[1].content == "partial text"
    assert chunks[1].is_delta is True
    error = chunks[-1]
    assert error.is_error is True
    assert "Claude Code stream ended without a result event" in error.content
    assert "test-session" in error.content
    assert isinstance(
        classify_provider_error(error.content, status=error.metadata.get("error_status")),
        ProviderStreamDisconnectError,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("emit_done", [False, True], ids=["eof", "done"])
async def test_missing_result_raises_without_auto_finish(emit_done):
    async def fake_events(_request):
        for event in _partial_turn(emit_done):
            yield event

    endpoint = ClaudeCodeCLIEndpoint()
    request = ClaudeCodeRequest(prompt="test", verbose_output=False, auto_finish=True)
    with patch(
        "lionhub.providers.anthropic.claude_code.stream_cc_cli_events",
        side_effect=fake_events,
    ) as raw_stream:
        with pytest.raises(
            ProviderStreamDisconnectError, match="Claude Code stream ended without a result event"
        ) as exc:
            await endpoint._call({"request": request}, {})

    assert "test-session" in str(exc.value)
    assert exc.value.retryable is True
    assert endpoint.session_id == "test-session"
    raw_stream.assert_called_once_with(request)


@pytest.mark.asyncio
async def test_explicit_empty_result_succeeds_without_auto_finish():
    events = [{"type": "result", "result": ""}, {"type": "done"}]

    async def fake_events(_request):
        for event in events:
            yield event

    endpoint = ClaudeCodeCLIEndpoint()
    request = ClaudeCodeRequest(prompt="test", verbose_output=False, auto_finish=True)
    with patch(
        "lionhub.providers.anthropic.claude_code.stream_cc_cli_events",
        side_effect=fake_events,
    ) as raw_stream:
        response = await endpoint._call({"request": request}, {})

    assert response["result"] == ""
    assert response["is_error"] is False
    raw_stream.assert_called_once_with(request)
    assert all(chunk.type != "error" for chunk in await _endpoint_chunks(events))


@pytest.mark.asyncio
async def test_explicit_error_result_keeps_its_classification():
    event = _result_event(result="usage limit reached", api_error_status=429)

    async def fake_events(_request):
        yield event
        yield {"type": "done"}

    endpoint = ClaudeCodeCLIEndpoint()
    request = ClaudeCodeRequest(prompt="test", verbose_output=False)
    with patch(
        "lionhub.providers.anthropic.claude_code.stream_cc_cli_events",
        side_effect=fake_events,
    ):
        with pytest.raises(ProviderQuotaError, match="usage limit reached"):
            await endpoint._call({"request": request}, {})

    chunks = await _endpoint_chunks([event, {"type": "done"}])
    error = chunks[-1]
    assert error.type == "error"
    assert error.content == "usage limit reached"
    assert error.metadata["error_status"] == 429
