# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The CLI announces its own retry of a dropped provider stream as an
error-typed event ("Reconnecting... 1/5 (...)") and then keeps going. That
notice must not end the leg: the stream keeps consuming, and terminality comes
from a later real failure event or the process exiting. A message that does not
match the notice shape stays terminal — the fail-closed direction.
"""

from __future__ import annotations

import pytest

from lionagi.providers.openai.codex import CodexCodeRequest, stream_codex_cli
from lionagi.service.types.stream_chunk import StreamChunk

RECONNECT_MSG = (
    "Reconnecting... 1/5 (stream disconnected before completion: "
    "stream closed before response.completed)"
)


def _make_request() -> CodexCodeRequest:
    return CodexCodeRequest(prompt="test", verbose_output=False)


async def _chunks_from_events(events: list[dict]) -> list[StreamChunk]:
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


@pytest.mark.asyncio
async def test_reconnect_notice_is_not_terminal_and_stream_continues():
    """The notice yields a non-error chunk and later events still arrive."""
    events = [
        {"type": "error", "error": {"message": RECONNECT_MSG}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 7}},
    ]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 2, f"expected notice + result, got {[c.type for c in chunks]}"
    notice = chunks[0]
    assert notice.type == "error"
    assert notice.is_error is False
    assert notice.metadata.get("reconnect_notice") is True
    assert chunks[1].type == "result", "stream must keep consuming past the notice"


@pytest.mark.asyncio
async def test_last_attempt_notice_is_still_a_notice():
    """ "5/5" is the final retry ATTEMPT, not its outcome — a failed final retry
    produces its own terminal event afterwards."""
    msg = "Reconnecting... 5/5 (stream disconnected before completion)"
    events = [{"type": "error", "error": {"message": msg}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].is_error is False
    assert chunks[0].metadata.get("reconnect_notice") is True


@pytest.mark.asyncio
async def test_disconnect_without_reconnect_prefix_stays_terminal():
    """The same underlying cause reported WITHOUT the retry prefix means the
    CLI is not retrying — that one is a real failure."""
    msg = "stream disconnected before completion: stream closed before response.completed"
    events = [{"type": "error", "error": {"message": msg}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].is_error is True
    assert not chunks[0].metadata.get("reconnect_notice")


@pytest.mark.asyncio
async def test_turn_failed_with_reconnect_text_stays_terminal():
    """turn.failed is an explicit terminal verdict from the CLI; its message
    text never reclassifies it."""
    events = [{"type": "turn.failed", "error": {"message": RECONNECT_MSG}}]
    chunks = await _chunks_from_events(events)

    assert len(chunks) == 1
    assert chunks[0].is_error is True
    assert not chunks[0].metadata.get("reconnect_notice")
