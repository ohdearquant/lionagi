# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The SSE heartbeat clock can follow a Trio-driven response consumer."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sniffio
from trio.testing import MockClock


@pytest.fixture
def clock():
    return MockClock()


@pytest.fixture
def anyio_backend(clock):
    return "trio", {"clock": clock}


@pytest.mark.anyio
async def test_operator_replay_resets_and_reads_heartbeat_clock_on_trio(monkeypatch, clock):
    from lionagi.studio.services import operator

    assert sniffio.current_async_library() == "trio"
    frame = {"conversationId": "conversation", "sequence": 1, "payload": {"content": "Hello"}}
    reads = []

    async def list_frames(conversation_id, *, after_sequence, limit):
        reads.append((conversation_id, after_sequence, limit))
        if after_sequence == 0:
            return [frame]
        clock.jump(6)
        return []

    store = SimpleNamespace(
        get_conversation=AsyncMock(return_value={"id": "conversation"}),
        list_frames=list_frames,
    )
    coordinator = SimpleNamespace(store=store, ensure_started=AsyncMock())
    monkeypatch.setattr(operator, "get_operator_coordinator", lambda: coordinator)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    response = await operator.stream_operator_conversation(
        "conversation", request, after_sequence=0
    )
    stream = response.body_iterator
    try:
        data = await anext(stream)
        assert json.loads(data.removeprefix("data:").strip()) == frame
        # The continuation first resets the deadline after replay; the next
        # store read advances real Trio time beyond that new deadline.
        assert await anext(stream) == ": heartbeat\n\n"
    finally:
        # The existing polling sleep is asyncio-specific. This test covers
        # clock reads through the emitted heartbeat, before the next sleep.
        await stream.aclose()
    assert reads == [("conversation", 0, 250), ("conversation", 1, 250)]
    coordinator.ensure_started.assert_awaited_once()
    store.get_conversation.assert_awaited_once_with("conversation")
