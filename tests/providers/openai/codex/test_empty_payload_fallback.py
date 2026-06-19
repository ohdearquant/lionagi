# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the empty-payload fallback in stream_codex_cli.

turn.failed with err={} must produce a self-describing string, not the useless str({})=='{}'; populated-message path must remain unchanged.
"""

from __future__ import annotations

import pytest

from lionagi.providers.openai.codex import CodexCodeRequest, stream_codex_cli
from lionagi.service.types.stream_chunk import StreamChunk


def _make_request() -> CodexCodeRequest:
    return CodexCodeRequest(prompt="test", verbose_output=False)


async def _chunks_from_events(events: list[dict]) -> list[StreamChunk]:
    """Drive stream_codex_cli with a mocked event stream, collect StreamChunks."""
    from unittest.mock import patch

    async def fake_events(request):
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


# ---------------------------------------------------------------------------
# turn.failed with empty error payload → self-describing message
# ---------------------------------------------------------------------------


async def test_turn_failed_empty_payload_has_self_describing_content():
    """turn.failed with err={} must NOT produce '{}' as content; must be a human-readable description naming the event type."""
    events = [{"type": "turn.failed", "error": {}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.type == "error"
    # Must NOT be the useless literal '{}' from str({})
    assert chunk.content != "{}", (
        f"empty-payload turn.failed must not render as '{{}}'; got: {chunk.content!r}"
    )
    # Must be self-describing: mention the event type
    assert "turn.failed" in chunk.content, (
        f"self-describing message must name the event type; got: {chunk.content!r}"
    )
    assert "CLI failure" in chunk.content or "empty error payload" in chunk.content, (
        f"self-describing message must indicate empty payload; got: {chunk.content!r}"
    )


async def test_error_event_empty_payload_benign_eos_still_applies():
    """'error' event with err={} is the benign-EOS sentinel; the empty-payload fallback must NOT override the benign_eos tagging path."""
    events = [{"type": "error", "error": {}}]
    chunks = await _chunks_from_events(events)

    error_chunks = [c for c in chunks if c.type == "error"]
    assert len(error_chunks) == 1
    # For the 'error' type with empty dict, the benign-EOS guard retracted
    # is_error; the content for such events is handled by the same block but
    # the benign_eos sentinel is the real distinguishing property.
    # Accept any content — what matters is the benign_eos tag.
    assert error_chunks[0].metadata.get("benign_eos") is True


# ---------------------------------------------------------------------------
# Populated message — existing behaviour preserved
# ---------------------------------------------------------------------------


async def test_turn_failed_populated_message_preserved():
    """turn.failed with a real error.message must surface that exact text."""
    msg = "Rate limit hit — please retry in 60 seconds"
    events = [{"type": "turn.failed", "error": {"message": msg}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert chunks[0].content == msg, (
        f"populated message must be preserved unchanged; got: {chunks[0].content!r}"
    )


async def test_turn_failed_top_level_message_surfaced_when_no_nested():
    """turn.failed with a top-level 'message' but no error.message must surface the top-level message (fallback path)."""
    msg = "Provider failure: model unavailable"
    events = [{"type": "turn.failed", "error": {}, "message": msg}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    # obj.get("message") should win over the fallback
    assert chunks[0].content == msg, (
        f"top-level message must be surfaced as fallback; got: {chunks[0].content!r}"
    )


async def test_error_event_top_level_message_preserved():
    """'error' event with top-level 'message' (usage-limit shape) is unchanged."""
    msg = "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits."
    events = [{"type": "error", "message": msg}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert chunks[0].content == msg


# ---------------------------------------------------------------------------
# Edge cases from codex review finding #2:
# error=null, error missing, {"code": ...} no message, non-dict error
# ---------------------------------------------------------------------------


async def test_turn_failed_error_null_has_self_describing_content():
    """turn.failed with JSON 'error':null must NOT surface as the string 'None'; same self-describing fallback as empty dict."""
    # JSON {"type": "turn.failed", "error": null} → obj.get("error", {}) returns None
    events = [{"type": "turn.failed", "error": None}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.type == "error"
    assert chunk.content != "None", (
        f"null error payload must not render as the string 'None'; got: {chunk.content!r}"
    )
    assert "turn.failed" in chunk.content, (
        f"self-describing message must name the event type; got: {chunk.content!r}"
    )


async def test_turn_failed_missing_error_key_is_self_describing():
    """turn.failed with no 'error' key at all should produce a self-describing message."""
    events = [{"type": "turn.failed"}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.type == "error"
    assert chunk.content != "{}", (
        f"missing-error turn.failed must not render as '{{}}'; got: {chunk.content!r}"
    )
    assert "turn.failed" in chunk.content, (
        f"self-describing message must name event type; got: {chunk.content!r}"
    )


async def test_turn_failed_code_no_message_uses_str_repr():
    """turn.failed with {'code':'rate_limit'} but no 'message' key surfaces str(err), not the empty-payload fallback."""
    events = [{"type": "turn.failed", "error": {"code": "rate_limit"}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.type == "error"
    # str({'code': 'rate_limit'}) is informative — acceptable per spec
    assert "rate_limit" in chunk.content or "code" in chunk.content, (
        f"non-empty dict without message should use str(err); got: {chunk.content!r}"
    )
    # Must NOT be the empty-payload fallback string
    assert "empty error payload" not in chunk.content, (
        f"non-empty dict must not trigger empty-payload fallback; got: {chunk.content!r}"
    )
    assert "null error payload" not in chunk.content, (
        f"non-empty dict must not trigger null-payload fallback; got: {chunk.content!r}"
    )


async def test_turn_failed_non_dict_error_uses_obj_message_or_str():
    """turn.failed with a non-dict error (e.g. a string) falls to obj.get('message', str(err)) — the non-dict branch."""
    events = [{"type": "turn.failed", "error": "process failed"}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.type == "error"
    assert chunk.content == "process failed", (
        f"non-dict error string should be surfaced directly; got: {chunk.content!r}"
    )
