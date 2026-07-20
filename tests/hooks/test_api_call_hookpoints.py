# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0047 delta row 2 (issue #1965): API_PRE_CALL / API_POST_CALL / API_STREAM_CHUNK
gain a real emit site through the typed, optional service-to-session adapter in
``operations/_api_hooks.py``, wired at ``operations/chat/chat.py`` (one-shot API
calls) and ``operations/run/run.py`` (CLI streaming). A standalone iModel (no
Branch, no HookBus) must be completely unaffected -- these callsites live in the
Branch-facing operation layer, never inside ``iModel``/``HookRegistry`` itself.
"""

from __future__ import annotations

import types

import pytest

from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.ln import json_dumps
from lionagi.operations.run.run import run
from lionagi.operations.types import RunParam
from lionagi.protocols.generic.event import EventStatus
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch
from lionagi.testing import LionAGIMockFactory


def _wire(branch: Branch, *points: HookPoint) -> dict[HookPoint, list[dict]]:
    bus = HookBus()
    calls: dict[HookPoint, list[dict]] = {p: [] for p in points}

    def _make(point):
        async def _handler(**kw):
            calls[point].append(kw)

        return _handler

    for p in points:
        bus.on(p, _make(p))
    branch._hooks = bus
    return calls


def _make_fake_cli_model(chunks: list[StreamChunk]) -> iModel:
    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    m.endpoint = types.SimpleNamespace(is_cli=True, session_id=None, to_dict=lambda: {})
    m.streaming_process_func = None

    async def create_event(**kw):
        return object()

    m.create_event = create_event
    from unittest.mock import AsyncMock

    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        for chunk in chunks:
            yield chunk

    m.stream = stream
    return m


# ── chat() (one-shot API path) ───────────────────────────────────────────────


async def test_chat_emits_api_pre_and_post_call_on_success():
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    result = await branch.chat("hi there")

    assert result == "hello"
    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_POST_CALL]) == 1

    pre = calls[HookPoint.API_PRE_CALL][0]
    assert pre["branch_id"] == str(branch.id)
    assert pre["model"] == "gpt-4o-mini"
    assert pre["provider"] == "openai"

    post = calls[HookPoint.API_POST_CALL][0]
    assert post["branch_id"] == str(branch.id)
    assert post["status"] == "completed"
    assert post["error"] is None
    # The mocked APICalling never runs through Event.invoke()'s timing
    # wrapper, so execution.duration stays unset — latency_ms is correctly
    # None rather than fabricated. "latency_ms" key presence is what matters.
    assert "latency_ms" in post


async def test_chat_emits_api_post_call_with_failed_status_before_raising():
    branch = LionAGIMockFactory.create_mocked_branch(response="broken", status=EventStatus.FAILED)
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    from lionagi._errors import ExecutionError

    with pytest.raises(ExecutionError):
        await branch.chat("hi there")

    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_POST_CALL]) == 1
    assert calls[HookPoint.API_POST_CALL][0]["status"] == "failed"


async def test_chat_emits_api_post_call_with_error_status_when_invoke_raises():
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    async def _boom(**kw):
        raise RuntimeError("provider is down")

    branch.chat_model.invoke = _boom

    with pytest.raises(RuntimeError, match="provider is down"):
        await branch.chat("hi there")

    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_POST_CALL]) == 1
    post = calls[HookPoint.API_POST_CALL][0]
    assert post["status"] == "error"
    # Class-name-only summary -- the raised exception's message must never
    # reach the emitted payload (it can carry request bodies, full URLs, or
    # credential fragments from the provider).
    assert post["error"] == "RuntimeError"
    assert "provider is down" not in post["error"]


async def test_chat_emits_paired_post_call_on_cancellation_during_invoke():
    """asyncio.CancelledError is a BaseException, not an Exception -- a bare
    ``except Exception`` around ``imodel.invoke()`` lets cancellation mid-call
    skip the post emission entirely, leaving an unpaired open span."""
    import asyncio

    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    invoke_started = asyncio.Event()

    async def _blocking_invoke(**kwargs):
        invoke_started.set()
        await asyncio.Event().wait()  # never resolves; only cancellation ends this

    branch.chat_model.invoke = _blocking_invoke

    task = asyncio.ensure_future(branch.chat("hi there"))
    await invoke_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_POST_CALL]) == 1
    assert calls[HookPoint.API_POST_CALL][0]["status"] == "error"


async def test_chat_without_session_bus_does_not_crash():
    """A Branch with no HookBus attached (branch._hooks is None, the default)
    must behave exactly as before -- the adapter is a strict no-op."""
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    assert branch._hooks is None

    result = await branch.chat("hi there")
    assert result == "hello"


# ── run() (CLI streaming path) ───────────────────────────────────────────────


async def test_run_emits_pre_call_stream_chunks_and_post_call():
    branch = Branch()
    branch.chat_model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="hel"),
            StreamChunk(type="text", content="lo"),
        ]
    )
    calls = _wire(
        branch, HookPoint.API_PRE_CALL, HookPoint.API_STREAM_CHUNK, HookPoint.API_POST_CALL
    )

    results = []
    async for msg in run(branch, "hi there", RunParam()):
        results.append(msg)

    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_STREAM_CHUNK]) == 2
    assert all(c["chunk_type"] == "text" for c in calls[HookPoint.API_STREAM_CHUNK])
    assert len(calls[HookPoint.API_POST_CALL]) == 1


async def test_run_emits_post_call_once_even_on_stream_failure():
    """A provider error mid-stream must still settle exactly one API_POST_CALL
    (status="error"), pairing with the one API_PRE_CALL that started the call."""
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="error", content="boom")])
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    with pytest.raises(Exception):
        async for _ in run(branch, "hi there", RunParam()):
            pass

    assert len(calls[HookPoint.API_PRE_CALL]) == 1
    assert len(calls[HookPoint.API_POST_CALL]) == 1
    assert calls[HookPoint.API_POST_CALL][0]["status"] == "error"


async def test_run_without_session_bus_never_calls_stream_chunk_adapter():
    """The no-bus guard belongs at the run.py call site, not only inside the
    adapter -- otherwise every streamed chunk still pays for constructing and
    awaiting a coroutine object that only checks ``branch._hooks is None``
    and returns. Patching the call site's own reference to
    ``emit_api_stream_chunk`` and asserting zero calls proves no coroutine
    for it is ever created on the no-bus path (not just that its body
    short-circuits)."""
    from unittest.mock import patch

    branch = Branch()
    branch.chat_model = _make_fake_cli_model(
        [StreamChunk(type="text", content="a"), StreamChunk(type="text", content="b")]
    )
    assert branch._hooks is None

    with patch("lionagi.operations.run.run.emit_api_stream_chunk") as mock_emit:
        results = []
        async for msg in run(branch, "hi there", RunParam()):
            results.append(msg)

    assert results
    mock_emit.assert_not_called()


async def test_run_without_session_bus_does_not_crash():
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="text", content="ok")])
    assert branch._hooks is None

    results = []
    async for msg in run(branch, "hi there", RunParam()):
        results.append(msg)
    assert results  # ran to completion unaffected


async def test_chat_api_post_call_never_leaks_secret_shaped_error_text():
    """A provider exception's message can carry request bodies, full URLs
    with query parameters, or credential fragments -- none of that may reach
    the emitted API_POST_CALL payload, which is persisted verbatim to
    observer telemetry."""
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire(branch, HookPoint.API_PRE_CALL, HookPoint.API_POST_CALL)

    secret = "sk-live-do-not-leak-4f3a9c2b"

    async def _boom(**kw):
        raise RuntimeError(
            f"POST https://api.openai.com/v1/chat/completions?api_key={secret} failed"
        )

    branch.chat_model.invoke = _boom

    with pytest.raises(RuntimeError):
        await branch.chat("hi there")

    post = calls[HookPoint.API_POST_CALL][0]
    assert secret not in json_dumps(post)
