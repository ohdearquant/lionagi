# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: Codex's thread.started event carries "thread_id", not
"session_id" — run.py's engine-session-id capture (used to link a
profile-typed agent session to its claude/codex-mirror engine session at
teardown, see lionagi/cli/_runs.py) only reads chunk.metadata["session_id"].
Without normalizing the system chunk's metadata, teardown always sees
engine_session_uid=None for Codex and the linked-engine reconciliation path
never runs."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi.providers.openai.codex import CodexCodeRequest, stream_codex_cli
from lionagi.service.types.stream_chunk import StreamChunk


def _make_request() -> CodexCodeRequest:
    return CodexCodeRequest(prompt="test", verbose_output=False)


async def _chunks_from_events(events: list[dict]) -> list[StreamChunk]:
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
async def test_thread_started_system_chunk_metadata_carries_session_id():
    """A real Codex thread.started event ({"thread_id": ...}, no "session_id")
    must still yield a system StreamChunk whose metadata has a non-None
    "session_id" — the key run.py reads to populate endpoint.session_id."""
    events = [{"type": "thread.started", "thread_id": "codex-thread-abc123"}]
    chunks = await _chunks_from_events(events)

    system_chunks = [c for c in chunks if c.type == "system"]
    assert len(system_chunks) == 1
    assert system_chunks[0].metadata.get("session_id") == "codex-thread-abc123"


@pytest.mark.asyncio
async def test_thread_started_preserves_other_metadata_fields():
    """Normalizing session_id must not drop the rest of the raw event."""
    events = [
        {
            "type": "thread.started",
            "thread_id": "codex-thread-xyz",
            "model": "gpt-5.5-codex",
        }
    ]
    chunks = await _chunks_from_events(events)

    system_chunks = [c for c in chunks if c.type == "system"]
    assert system_chunks[0].metadata.get("thread_id") == "codex-thread-xyz"
    assert system_chunks[0].metadata.get("model") == "gpt-5.5-codex"


@pytest.mark.asyncio
async def test_end_to_end_run_captures_codex_engine_session_id():
    """The full path a real `li agent -a <profile> codex ...` teardown relies
    on: run()'s streaming loop reads chunk.metadata["session_id"] off the
    system chunk and stamps it onto endpoint.session_id — proving Codex's
    thread.started event now reaches that path with a non-None id."""
    import types

    from lionagi.operations.run.run import RunParam, run
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch

    events = [{"type": "thread.started", "thread_id": "codex-thread-e2e"}]
    chunks = await _chunks_from_events(events)

    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        to_dict=lambda: {"type": "fake_cli"},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None

    from unittest.mock import AsyncMock

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

    assert m.endpoint.session_id == "codex-thread-e2e"
