# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.operations.run.run — the CLI streaming operation."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from lionagi.operations.run.run import RunParam, run, run_and_collect
from lionagi.operations.types import ChatParam
from lionagi.protocols.messages import (
    ActionRequest,
    ActionResponse,
    AssistantResponse,
    AssistantResponseContent,
)
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_cli_model(chunks: list[StreamChunk], session_id: str | None = None):
    """Return (model, captured_kwargs_dict) where model is an iModel patched to
    behave as a CLI endpoint yielding *chunks* from its stream() method."""
    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=session_id,
        to_dict=lambda: {"type": "fake_cli", "session_id": session_id},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None
    captured: dict = {}

    async def create_event(**kw):
        captured.update(kw)
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        for chunk in chunks:
            yield chunk

    m.stream = stream
    return m, captured


async def _collect(gen) -> list:
    """Drain an async generator into a list."""
    results = []
    async for item in gen:
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# P0 tests — run()
# ---------------------------------------------------------------------------


async def test_run_rejects_non_cli_chat_model():
    """run() raises ValueError when chat_model is not a CLI endpoint."""
    branch = Branch()
    # Default iModel is not CLI
    assert not branch.chat_model.is_cli

    with pytest.raises(ValueError, match="run operation only supports CLI endpoints"):
        async for _ in run(branch, "hello", RunParam()):
            pass


async def test_run_passes_resume_from_provider_session_id_and_updates_endpoint_session():
    """resume kwarg forwarded from provider_session_id; system chunk updates endpoint."""
    model, captured = _make_fake_cli_model(
        [
            StreamChunk(
                type="system",
                metadata={"session_id": "new-session"},
            ),
            StreamChunk(type="text", content="ok"),
        ],
        session_id="old-session",
    )
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "hi", RunParam()))

    # create_event should have received resume="old-session"
    assert captured.get("resume") == "old-session"
    # Endpoint session should be updated from the system chunk
    assert model.endpoint.session_id == "new-session"
    # Final yielded text message
    text_msgs = [r for r in results if isinstance(r, AssistantResponse)]
    assert len(text_msgs) == 1
    assert text_msgs[0].response == "ok"


async def test_run_flushes_text_before_tool_use_and_links_tool_result():
    """Text is flushed before tool_use; tool_result is linked to the request."""
    model, _ = _make_fake_cli_model(
        [
            StreamChunk(type="thinking", content="think"),
            StreamChunk(type="text", content="before"),
            StreamChunk(
                type="tool_use",
                tool_name="fn",
                tool_id="call-1",
                tool_input={"x": 1},
            ),
            StreamChunk(
                type="tool_result",
                tool_id="call-1",
                tool_output={"v": 1},
            ),
            StreamChunk(type="text", content="after"),
        ]
    )
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "go", RunParam()))

    type_seq = [type(r).__name__ for r in results]
    assert type_seq == [
        "Instruction",
        "AssistantResponse",
        "ActionRequest",
        "ActionResponse",
        "AssistantResponse",
    ], f"Unexpected sequence: {type_seq}"

    # "before" text with thinking metadata
    first_ar: AssistantResponse = results[1]
    assert first_ar.response == "before"
    assert first_ar.metadata.get("thinking") == "think"

    # Tool name preserved on request
    act_req: ActionRequest = results[2]
    assert act_req.function == "fn"

    # "after" text
    last_ar: AssistantResponse = results[4]
    assert last_ar.response == "after"


async def test_run_unmatched_tool_result_is_skipped():
    """tool_result with unknown tool_id is silently skipped (no matching request)."""
    model, _ = _make_fake_cli_model(
        [
            StreamChunk(
                type="tool_result",
                tool_id="missing",
                tool_name="read",
                tool_output={"error": "no request"},
                is_error=True,
            ),
        ]
    )
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "go", RunParam()))

    action_responses = [r for r in results if isinstance(r, ActionResponse)]
    assert len(action_responses) == 0, "Unmatched tool_result should be skipped"


async def test_run_matched_tool_result_with_error():
    """Matched tool_result with is_error=True preserves error metadata."""
    model, _ = _make_fake_cli_model(
        [
            StreamChunk(
                type="tool_use",
                tool_id="call_1",
                tool_name="read",
                tool_input={"path": "/tmp"},
            ),
            StreamChunk(
                type="tool_result",
                tool_id="call_1",
                tool_name="read",
                tool_output={"error": "permission denied"},
                is_error=True,
            ),
        ]
    )
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "go", RunParam()))

    action_responses = [r for r in results if isinstance(r, ActionResponse)]
    assert len(action_responses) == 1
    assert action_responses[0].metadata.get("is_error") is True
    assert action_responses[0].function == "read"


async def test_run_error_chunk_raises_and_restores_streaming_processor():
    """Error chunk raises RuntimeError; finally block restores streaming_process_func."""
    sentinel = object()
    model, _ = _make_fake_cli_model([StreamChunk(type="error", content="boom")])
    model.streaming_process_func = sentinel

    branch = Branch()
    branch.chat_model = model

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in run(branch, "go", RunParam()):
            pass

    # finally block in run() restores the original streaming_process_func
    assert model.streaming_process_func is sentinel


async def test_run_stream_persist_writes_final_state_and_removes_buffer(tmp_path):
    """stream_persist=True writes branch JSON and removes buffer JSONL."""
    model, _ = _make_fake_cli_model([StreamChunk(type="text", content="done")])
    branch = Branch()
    branch.chat_model = model

    param = RunParam(stream_persist=True, persist_dir=tmp_path)
    await _collect(run(branch, "persist-me", param))

    # Branch JSON should exist
    json_files = list(tmp_path.glob("*.json"))
    assert json_files, "Expected branch JSON file after stream_persist"

    # Buffer JSONL should be removed after successful completion
    buffer_files = list(tmp_path.glob("*.jsonl"))
    assert not buffer_files, f"Buffer JSONL should be removed: {buffer_files}"

    # Original streaming processor restored
    assert model.streaming_process_func is None


async def test_run_stream_persist_snapshot_dir_routes_snapshot_separately(
    tmp_path,
):
    """snapshot_dir routes branch snapshot to a separate dir from the streaming buffer; find_branch looks in snapshot_dir."""
    stream_dir = tmp_path / "stream"
    branches_dir = tmp_path / "branches"
    stream_dir.mkdir()
    branches_dir.mkdir()

    model, _ = _make_fake_cli_model([StreamChunk(type="text", content="done")])
    branch = Branch()
    branch.chat_model = model

    param = RunParam(
        stream_persist=True,
        persist_dir=stream_dir,
        snapshot_dir=branches_dir,
    )
    await _collect(run(branch, "persist-me", param))

    # Snapshot landed in branches_dir, NOT stream_dir.
    branch_snaps = list(branches_dir.glob("*.json"))
    stream_snaps = list(stream_dir.glob("*.json"))
    assert branch_snaps, "snapshot should be in branches_dir"
    assert not stream_snaps, "no snapshot should land in stream_dir when snapshot_dir is set"
    # The snapshot is named after the branch id.
    assert branch_snaps[0].name == f"{branch.id}.json"


async def test_run_stream_persist_snapshot_dir_default_falls_back_to_persist_dir(
    tmp_path,
):
    """When snapshot_dir is None (default), the snapshot lands in
    persist_dir — backwards-compatible behavior for non-CLI callers.
    """
    model, _ = _make_fake_cli_model([StreamChunk(type="text", content="done")])
    branch = Branch()
    branch.chat_model = model

    param = RunParam(stream_persist=True, persist_dir=tmp_path)
    # snapshot_dir defaults to a sentinel/None — fallback uses persist_dir
    await _collect(run(branch, "persist-me", param))

    # Snapshot is in persist_dir
    assert list(tmp_path.glob("*.json"))


# ---------------------------------------------------------------------------
# P0/P1 tests — run_and_collect()
# ---------------------------------------------------------------------------


async def test_run_and_collect_clears_messages_and_joins_assistant_text(monkeypatch):
    """clear_messages=True clears branch before run; text chunks are joined."""
    branch = Branch()
    # Add a prior message so we can confirm it gets cleared
    branch.msgs.add_message(instruction=branch.msgs.create_instruction(instruction="prior"))
    assert len(branch.messages) == 1

    def make_ar(text: str) -> AssistantResponse:
        ar = AssistantResponse(
            content=AssistantResponseContent(assistant_response=text),
            sender=branch.id,
            recipient="user",
        )
        return ar

    async def fake_run(b, ins, param):
        yield make_ar("one")
        yield make_ar("two")

    monkeypatch.setattr("lionagi.operations.run.run.run", fake_run)

    result = await run_and_collect(
        branch,
        "test",
        ChatParam(),
        skip_validation=True,
        clear_messages=True,
    )

    # Messages cleared before run; fake_run doesn't add any
    assert len(branch.messages) == 0
    assert result == "one\n\ntwo"


async def test_run_and_collect_parses_when_response_format_is_set(monkeypatch):
    """When response_format is set, run_and_collect passes full text to parse."""
    from lionagi.operations.types import ParseParam

    class Answer(BaseModel):
        value: int

    branch = Branch()
    parse_calls: list[str] = []

    async def fake_run(b, ins, param):
        ar = AssistantResponse(
            content=AssistantResponseContent(assistant_response='{"value": 42}'),
            sender=branch.id,
            recipient="user",
        )
        yield ar

    async def fake_parse(b, text, pp):
        parse_calls.append(text)
        return Answer(value=42)

    monkeypatch.setattr("lionagi.operations.run.run.run", fake_run)
    # Patch at the source module since run_and_collect uses a lazy import
    monkeypatch.setattr("lionagi.operations.parse.parse.parse", fake_parse)

    from lionagi.operations.parse.parse import get_default_call

    pp = ParseParam(
        response_format=Answer,
        imodel=branch.chat_model,
        imodel_kw={},
        alcall_params=get_default_call(),
    )

    result = await run_and_collect(branch, "test", ChatParam(), parse_param=pp)

    assert isinstance(result, Answer)
    assert result.value == 42
    assert len(parse_calls) == 1
    assert parse_calls[0] == '{"value": 42}'


# ---------------------------------------------------------------------------
# Timeout enforcement tests — regression for the "timeout silently ignored"
# bug where ``branch.operate(timeout=N)`` / ``li agent --timeout N`` flowed
# through ``imodel_kw`` into ``model.create_event(**kw)`` but the streaming
# loop never wrapped the consumer with ``anyio.fail_after``, so CLI
# subprocesses (codex, claude_code) ran unbounded.
# ---------------------------------------------------------------------------


def _make_slow_cli_model(chunk_delay: float, n_chunks: int = 100):
    """A CLI iModel whose stream sleeps between each chunk. Use a long delay
    + many chunks so the total runtime exceeds any test timeout."""
    import anyio

    from lionagi.service.types.stream_chunk import StreamChunk

    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        to_dict=lambda: {"type": "fake_cli", "session_id": None},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None
    captured: dict = {}

    async def create_event(**kw):
        captured.update(kw)
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        for _ in range(n_chunks):
            await anyio.sleep(chunk_delay)
            yield StreamChunk(type="text", content="x")

    m.stream = stream
    return m, captured


async def test_run_honors_caller_timeout_on_slow_stream():
    """When the caller passes ``timeout=N`` via imodel_kw, the stream loop
    raises TimeoutError once N seconds elapse, even if the upstream provider
    would otherwise stream forever."""
    import time

    # chunk_delay is large so that timeout (0.15s) fires before ANY chunk
    # arrives.  Keeping it large (3.0s) also provides CI-tolerant headroom
    # for the elapsed-time bound below.
    chunk_delay = 3.0
    caller_timeout = 0.15
    model, _ = _make_slow_cli_model(chunk_delay=chunk_delay, n_chunks=20)
    branch = Branch()
    branch.chat_model = model

    stream_responses: list = []
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        async for msg in run(branch, "go", RunParam(imodel_kw={"timeout": caller_timeout})):
            if isinstance(msg, AssistantResponse):
                stream_responses.append(msg)
    elapsed = time.monotonic() - started

    # Behavioral: timeout must fire before the stream produces any content.
    # run() yields an Instruction first (always), then AssistantResponse per chunk.
    # If timeout fires correctly, no AssistantResponse should be yielded.
    assert stream_responses == [], (
        f"timeout fired after {len(stream_responses)} stream response(s) — "
        "timeout is not enforced before first chunk"
    )
    # Relative timing: elapsed must be less than one chunk interval,
    # which proves the timeout tripped before the provider would have sent anything.
    assert elapsed < chunk_delay, (
        f"timeout fired at {elapsed:.2f}s but first chunk was due at {chunk_delay}s"
    )


async def test_run_no_timeout_when_kwarg_absent():
    """Back-compat: callers that don't supply timeout get the legacy
    unbounded behaviour (subject to chunk count, not wall clock)."""

    model, _ = _make_slow_cli_model(chunk_delay=0.0, n_chunks=3)
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "go", RunParam()))
    text_msgs = [r for r in results if isinstance(r, AssistantResponse)]
    # 3 chunks of "x" → single flushed AssistantResponse with "xxx".
    assert len(text_msgs) == 1
    assert text_msgs[0].response == "xxx"


async def test_run_strips_timeout_from_create_event_kwargs():
    """The provider does NOT consume ``timeout``; verify it is popped from
    kw before create_event sees it (otherwise codex would receive an
    unexpected kwarg and may crash)."""

    model, captured = _make_slow_cli_model(chunk_delay=0.0, n_chunks=1)
    branch = Branch()
    branch.chat_model = model

    await _collect(run(branch, "hi", RunParam(imodel_kw={"timeout": 5})))
    assert "timeout" not in captured, f"timeout leaked into create_event kwargs: {captured!r}"


# ---------------------------------------------------------------------------
# Regression: Branch.operate() must flatten **kwargs so timeout reaches run()
# ---------------------------------------------------------------------------


async def test_branch_operate_forwards_timeout_to_run(monkeypatch):
    """Branch.operate(**kwargs) must flatten kwargs before passing to
    prepare_operate_kw, otherwise timeout arrives as a nested dict
    {"kwargs": {"timeout": N}} and run() never sees it."""
    received_timeout = []

    original_run = run

    async def spy_run(branch, instruction, param):
        kw_copy = (param.imodel_kw or {}).copy()
        received_timeout.append(kw_copy.get("timeout"))
        async for msg in original_run(branch, instruction, param):
            yield msg

    monkeypatch.setattr("lionagi.operations.run.run.run", spy_run)

    model, _ = _make_fake_cli_model([StreamChunk(type="text", content="ok")])
    branch = Branch()
    branch.chat_model = model

    await branch.operate(instruction="test", timeout=42)

    assert received_timeout == [42], f"timeout not forwarded correctly: {received_timeout}"


async def test_branch_operate_forwards_extra_kwargs_to_run(monkeypatch):
    """Arbitrary **kwargs on Branch.operate() reach run() via imodel_kw."""
    received_kw = {}

    original_run = run

    async def spy_run(branch, instruction, param):
        received_kw.update(param.imodel_kw or {})
        async for msg in original_run(branch, instruction, param):
            yield msg

    monkeypatch.setattr("lionagi.operations.run.run.run", spy_run)

    model, _ = _make_fake_cli_model([StreamChunk(type="text", content="ok")])
    branch = Branch()
    branch.chat_model = model

    await branch.operate(instruction="test", repo="/tmp/test", timeout=99)

    assert received_kw.get("timeout") == 99
    assert received_kw.get("repo") == "/tmp/test"


# ---------------------------------------------------------------------------
# Regression: iModel.stream() must not yield in finally (swallows cancellation)
# ---------------------------------------------------------------------------


async def test_imodel_stream_propagates_cancellation():
    """iModel.stream() must propagate CancelledError from the inner stream,
    not swallow it via a yield-in-finally."""
    import anyio

    from lionagi.protocols.generic.event import EventStatus
    from lionagi.service.connections.api_calling import APICalling

    # chunk_delay is large so fail_after(cancel_after) fires before ANY chunk
    # arrives.  Keeping it large also gives CI-tolerant headroom for elapsed bound.
    chunk_delay = 3.0
    cancel_after = 0.1

    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")

    class SlowEndpoint:
        is_cli = True
        session_id = None
        DEFAULT_QUEUE_CAPACITY = 10

        async def stream(self, request=None, extra_headers=None, **kw):
            for _ in range(100):
                await anyio.sleep(chunk_delay)
                yield StreamChunk(type="text", content="x")

    m.endpoint = SlowEndpoint()

    api_call = AsyncMock(spec=APICalling)
    api_call.id = "test-api-call-id"
    api_call.execution = AsyncMock()
    api_call.execution.status = EventStatus.PENDING

    async def fake_core_stream():
        for _ in range(100):
            await anyio.sleep(chunk_delay)
            yield StreamChunk(type="text", content="x")

    api_call.stream = fake_core_stream
    m.executor = types.SimpleNamespace(
        append=AsyncMock(),
        pile=types.SimpleNamespace(pop=lambda *a, **kw: None),
        processor=types.SimpleNamespace(
            _concurrency_sem=None,
            is_stopped=lambda: False,
        ),
        config={},
    )

    import time

    chunks_yielded: list = []
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with anyio.fail_after(cancel_after):
            async for chunk in m.stream(api_call=api_call):
                chunks_yielded.append(chunk)
    elapsed = time.monotonic() - started

    # Behavioral: cancellation must propagate — no chunks should have been yielded.
    assert chunks_yielded == [], (
        f"stream yielded {len(chunks_yielded)} chunk(s) after cancellation — "
        "CancelledError was swallowed instead of propagated"
    )
    # Relative timing: cancellation must surface before the first chunk interval,
    # proving the generator did not block on a yield-in-finally.
    assert elapsed < chunk_delay, (
        f"cancellation surfaced at {elapsed:.2f}s but first chunk was due at {chunk_delay}s"
    )
