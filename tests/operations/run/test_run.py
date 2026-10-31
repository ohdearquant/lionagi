# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.operations.run.run — the CLI streaming operation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import types
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import BaseModel

from lionagi.operations.run.run import (
    RunParam,
    _accumulate_result_meta,
    _stream_with_deadline,
    _stream_with_liveness,
    _write_branch_snapshot,
    run,
    run_and_collect,
)
from lionagi.operations.types import ChatParam
from lionagi.protocols.generic.progression import Progression
from lionagi.protocols.messages import (
    ActionRequest,
    ActionResponse,
    AssistantResponse,
    AssistantResponseContent,
    Instruction,
)
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch

# Helpers


def _make_fake_cli_model(
    chunks: list[StreamChunk],
    session_id: str | None = None,
    api_call_metadata: dict | None = None,
    api_call_metadata_on_close: dict | None = None,
):
    """Return (model, captured_kwargs_dict) where model is an iModel patched to
    behave as a CLI endpoint yielding *chunks* from its stream() method."""
    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    api_call_endpoint = m.endpoint
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
        if api_call_metadata is not None or api_call_metadata_on_close is not None:
            return APICalling(
                payload=kw,
                endpoint=api_call_endpoint,
                metadata=dict(api_call_metadata or {}),
            )
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        try:
            for chunk in chunks:
                yield chunk
        finally:
            if api_call_metadata_on_close is not None:
                api_call.metadata.update(api_call_metadata_on_close)

    m.stream = stream
    return m, captured


async def _collect(gen) -> list:
    """Drain an async generator into a list."""
    results = []
    async for item in gen:
        results.append(item)
    return results


@contextlib.contextmanager
def _fail_on_next_checkpoint(_delay: float):
    """Deterministic fail_after seam whose deadline is the next checkpoint."""
    import anyio

    with anyio.CancelScope() as scope:
        scope.cancel()
        yield scope
    if scope.cancelled_caught:
        raise TimeoutError


# P0 tests — run()


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


async def test_run_persists_exact_call_served_model_on_final_response():
    """Close the call before persisting evidence on a benign early stream end."""
    model, _ = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="verified"),
            StreamChunk(type="error", metadata={"benign_eos": True}),
        ],
        api_call_metadata_on_close={"served_model": "claude-opus-4-1"},
    )
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "attest", RunParam()))

    response = next(result for result in results if isinstance(result, AssistantResponse))
    assert response.metadata["api_call_meta"]["metadata"]["served_model"] == "claude-opus-4-1"

    branch_record = branch.to_dict()
    messages = branch_record["messages"]
    terminal_id = messages["progression"]["order"][-1]
    terminal_message = next(
        message for message in messages["collections"] if message["id"] == terminal_id
    )
    assert terminal_message["metadata"]["api_call_meta"]["metadata"]["served_model"] == (
        "claude-opus-4-1"
    )


async def test_run_threads_context_provider_blocks_into_first_prompt():
    class _Provider:
        async def provide(self, branch, instruction):
            return "stream-context"

    model, captured = _make_fake_cli_model([StreamChunk(type="text", content="ok")])
    branch = Branch(system="You are helpful")
    branch.chat_model = model
    branch.providers.register(_Provider())

    await _collect(run(branch, "hi", RunParam()))

    assert "stream-context" in captured["messages"][0]["content"]


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


async def test_run_preserves_primary_exception_when_aclose_raises_cancelled_error():
    """The CLI close chain can raise asyncio.CancelledError, a BaseException a plain `except Exception` doesn't catch -- left unguarded it would escape run()'s cleanup finally and replace the real propagating error with a misleading CancelledError."""
    import asyncio

    async def stream(api_call=None):
        try:
            yield StreamChunk(type="error", content="boom")
        except GeneratorExit:
            raise asyncio.CancelledError("cleanup cancelled mid-close")

    model, _ = _make_fake_cli_model([])
    model.stream = stream

    branch = Branch()
    branch.chat_model = model

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in run(branch, "go", RunParam()):
            pass


async def test_run_caller_abandonment_closes_stream_without_raising():
    """A consumer that stops iterating early (explicit aclose() mid-stream) triggers GeneratorExit through run()'s own async generator; the finally block's stream_gen.aclose() must complete cleanly and the underlying CLI stream must be closed."""
    closed = False

    async def stream(api_call=None):
        nonlocal closed
        try:
            yield StreamChunk(type="tool_use", tool_name="do_thing", tool_input={}, tool_id="t1")
            yield StreamChunk(type="text", content="never reached")
        finally:
            closed = True

    model, _ = _make_fake_cli_model([])
    model.stream = stream

    branch = Branch()
    branch.chat_model = model

    agen = run(branch, "go", RunParam())
    async for msg in agen:
        if isinstance(msg, ActionRequest):
            break  # abandon mid-stream, after the wrapper actually started streaming
    await agen.aclose()

    assert closed, "abandoning the consumer must still close the underlying CLI stream"


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


async def test_run_stream_persist_snapshot_survives_mid_stream_cancellation(tmp_path, monkeypatch):
    """A branch checkpoint exists and is loadable even if the turn is killed before the model produces a single chunk -- the snapshot is written before streaming starts, not only on clean completion, so a branch whose first turn never finished can still be resumed."""
    import anyio as _anyio

    branches_dir = tmp_path / "branches"
    branches_dir.mkdir()

    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    m.endpoint = types.SimpleNamespace(
        is_cli=True, session_id=None, to_dict=lambda: {"type": "fake_cli"}
    )
    m.streaming_process_func = None

    async def create_event(**kw):
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    hang = _anyio.Event()
    checkpoint_written = asyncio.Event()
    stream_started = asyncio.Event()
    checkpoint_state_at_stream_start = []

    async def stream(api_call=None):
        checkpoint_state_at_stream_start.append(checkpoint_written.is_set())
        stream_started.set()
        await hang.wait()  # never set — simulates a subprocess still running
        yield StreamChunk(type="text", content="unreachable")  # pragma: no cover

    m.stream = stream
    branch = Branch()
    branch.chat_model = m

    async def write_snapshot_and_signal(*args, **kwargs):
        await _write_branch_snapshot(*args, **kwargs)
        checkpoint_written.set()

    monkeypatch.setattr(
        "lionagi.operations.run.run._write_branch_snapshot",
        write_snapshot_and_signal,
    )

    param = RunParam(stream_persist=True, persist_dir=branches_dir, snapshot_dir=branches_dir)
    gen = run(branch, "long-running instruction", param)

    first = await gen.__anext__()
    assert isinstance(first, Instruction)

    task = asyncio.ensure_future(gen.__anext__())
    await stream_started.wait()
    assert checkpoint_state_at_stream_start == [True]
    await checkpoint_written.wait()

    snaps = list(branches_dir.glob("*.json"))
    assert snaps, "checkpoint must exist before the stream produces any output"

    # A resumer must be able to parse it — not a torn/partial write.
    data = json.loads(snaps[0].read_text())
    assert data  # non-empty, valid JSON

    task.cancel()
    with contextlib.suppress(BaseException):
        await task
    with contextlib.suppress(Exception):
        await gen.aclose()

    # Still there (and still valid) after the simulated kill.
    snaps_after = list(branches_dir.glob("*.json"))
    assert snaps_after
    data_after = json.loads(snaps_after[0].read_text())
    assert data_after["id"] == str(branch.id)

    # The instruction was recorded even though no assistant response arrived —
    # the checkpoint carries what a resumer needs (find_branch + json.loads
    # both succeed; the fake CLI endpoint used here isn't a real serializable
    # Endpoint, so this asserts against the message record directly rather
    # than round-tripping the whole branch through Branch.from_dict).
    msg_classes = [
        entry["metadata"].get("lion_class") for entry in data_after["messages"]["collections"]
    ]
    assert any(cls == "lionagi.protocols.messages.instruction.Instruction" for cls in msg_classes)
    assert not any(
        cls == "lionagi.protocols.messages.assistant_response.AssistantResponse"
        for cls in msg_classes
    )


async def test_write_branch_snapshot_torn_write_keeps_prior_snapshot(tmp_path, monkeypatch):
    """A kill landing mid-write must never corrupt an existing snapshot: the write stages through a sibling .tmp file, so a failure mid-write tears only the staging file and the target keeps its previous complete snapshot -- fails if the helper ever writes the target in place."""
    import anyio as _anyio

    branch = Branch()
    await _write_branch_snapshot(branch, tmp_path)

    target = tmp_path / f"{branch.id}.json"
    v1 = target.read_text()
    assert json.loads(v1)["id"] == str(branch.id)

    real_open_file = _anyio.open_file

    def torn_open_file(path, mode="r", *args, **kwargs):
        class _TornCtx:
            async def __aenter__(self):
                self._f = await real_open_file(path, mode, *args, **kwargs)
                await self._f.__aenter__()

                class _TornFile:
                    def __init__(self, inner):
                        self._inner = inner

                    async def write(self, data):
                        await self._inner.write(data[: len(data) // 2])
                        raise OSError("simulated kill mid-write")

                return _TornFile(self._f)

            async def __aexit__(self, *exc):
                return await self._f.__aexit__(*exc)

        async def _make():
            return _TornCtx()

        return _make()

    monkeypatch.setattr(_anyio, "open_file", torn_open_file)

    with pytest.raises(OSError, match="simulated kill mid-write"):
        await _write_branch_snapshot(branch, tmp_path)

    # The target is byte-identical to the prior complete snapshot — not torn.
    assert target.read_text() == v1
    assert json.loads(target.read_text())["id"] == str(branch.id)


async def test_write_branch_snapshot_failed_replace_keeps_prior_snapshot(tmp_path, monkeypatch):
    """If the final rename fails, the target must be untouched, pinning that the helper publishes via os.replace rather than writing the target directly."""
    import os as _os

    branch = Branch()
    await _write_branch_snapshot(branch, tmp_path)
    target = tmp_path / f"{branch.id}.json"
    v1 = target.read_text()

    def failing_replace(src, dst):
        raise OSError("simulated kill before publish")

    monkeypatch.setattr(_os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated kill before publish"):
        await _write_branch_snapshot(branch, tmp_path)

    assert target.read_text() == v1


# P0/P1 tests — run_and_collect()


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


# Timeout enforcement tests — regression for the "timeout silently ignored"
# bug where ``branch.operate(timeout=N)`` / ``li agent --timeout N`` flowed
# through ``imodel_kw`` into ``model.create_event(**kw)`` but the streaming
# loop never wrapped the consumer with ``anyio.fail_after``, so CLI
# subprocesses (codex, claude_code) ran unbounded.


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


async def test_run_honors_caller_timeout_on_slow_stream(monkeypatch):
    """When the caller passes ``timeout=N`` via imodel_kw, the stream loop
    raises TimeoutError once N seconds elapse, even if the upstream provider
    would otherwise stream forever."""
    import importlib

    import anyio

    caller_timeout = 0.15
    model, _ = _make_slow_cli_model(chunk_delay=0, n_chunks=20)
    branch = Branch()
    branch.chat_model = model
    cancellation_order: list[str] = []

    async def stream(api_call=None):
        try:
            await anyio.lowlevel.checkpoint()
        except BaseException:
            cancellation_order.append("cancelled_before_deadline")
            raise
        cancellation_order.append("stream_advanced_past_deadline")
        yield StreamChunk(type="text", content="late")

    model.stream = stream
    run_mod = importlib.import_module("lionagi.operations.run.run")
    monkeypatch.setattr(run_mod.anyio, "fail_after", _fail_on_next_checkpoint)

    stream_responses: list = []
    with pytest.raises(TimeoutError):
        async for msg in run(branch, "go", RunParam(imodel_kw={"timeout": caller_timeout})):
            if isinstance(msg, AssistantResponse):
                stream_responses.append(msg)

    # The injected deadline cancels at the provider's first checkpoint. If
    # timeout delivery is late or absent, the stream records that it advanced.
    assert cancellation_order == ["cancelled_before_deadline"]
    assert stream_responses == [], (
        f"timeout fired after {len(stream_responses)} stream response(s) — "
        "timeout is not enforced before first chunk"
    )


async def test_stream_deadline_preserves_timeout_when_inner_aclose_raises():
    """A deadline TimeoutError must not be replaced by a cleanup failure from
    closing the provider stream."""
    import asyncio

    import anyio

    async def stream(api_call=None):
        try:
            yield StreamChunk(type="text", content="first")
        except GeneratorExit:
            raise asyncio.CancelledError("cleanup cancelled mid-close")

    model = types.SimpleNamespace(stream=stream)
    agen = _stream_with_deadline(model, object(), anyio.current_time() + 0.01)

    first = await agen.__anext__()
    assert first.content == "first"

    await anyio.sleep(0.02)

    with pytest.raises(TimeoutError, match="stream timeout exceeded"):
        await agen.__anext__()


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


async def test_run_derives_gemini_print_timeout_from_caller_timeout():
    """The shared run seam forwards its deadline to buffered Gemini CLI requests."""
    from lionhub.providers.google.gemini_code import GeminiCodeRequest

    model, captured = _make_slow_cli_model(chunk_delay=0.0, n_chunks=1)
    model.endpoint._request_model = GeminiCodeRequest
    branch = Branch()
    branch.chat_model = model

    await _collect(run(branch, "hi", RunParam(imodel_kw={"timeout": 1200})))

    derived = captured["print_timeout"]
    assert int(derived.removesuffix("s")) > 1200
    assert "timeout" not in captured


async def test_run_rejects_gemini_timeout_at_provider_ceiling():
    """Reject a caller deadline that cannot have a later agy backstop."""
    from lionhub.providers.google.gemini_code import GeminiCodeRequest

    model, captured = _make_slow_cli_model(chunk_delay=0.0, n_chunks=1)
    model.endpoint._request_model = GeminiCodeRequest
    branch = Branch()
    branch.chat_model = model

    with pytest.raises(ValueError, match="caller deadline"):
        await _collect(
            run(
                branch,
                "hi",
                RunParam(imodel_kw={"timeout": (2**63 - 1) // 10**9}),
            )
        )

    assert captured == {}


async def test_run_preserves_explicit_gemini_print_timeout():
    """An explicit provider cap wins over the timeout-derived default."""
    from lionhub.providers.google.gemini_code import GeminiCodeRequest

    model, captured = _make_slow_cli_model(chunk_delay=0.0, n_chunks=1)
    model.endpoint._request_model = GeminiCodeRequest
    branch = Branch()
    branch.chat_model = model

    await _collect(
        run(
            branch,
            "hi",
            RunParam(imodel_kw={"timeout": 1200, "print_timeout": "45m"}),
        )
    )

    assert captured["print_timeout"] == "45m"


async def test_run_without_timeout_sets_configured_gemini_print_timeout(monkeypatch):
    """The shared run seam supplies the configured cap without an outer deadline."""
    import lionagi.config as config_module
    from lionhub.providers.google.gemini_code import GeminiCodeRequest

    configured_cap = 3600.0
    monkeypatch.setattr(
        config_module,
        "settings",
        config_module.AppSettings(LIONAGI_ANTIGRAVITY_PRINT_TIMEOUT=configured_cap),
    )
    model, captured = _make_slow_cli_model(chunk_delay=0.0, n_chunks=1)
    model.endpoint._request_model = GeminiCodeRequest
    branch = Branch()
    branch.chat_model = model

    await _collect(run(branch, "hi", RunParam()))

    assert captured["print_timeout"] == "3600s"


async def test_configured_gemini_print_timeout_stays_a_parseable_go_duration(monkeypatch):
    """A large configured cap must not reach agy as scientific notation.

    The setting exists to be overridden, and general float formatting turns
    values at or above a million into "1e+06", which Go's duration parser
    rejects. That failure would surface as agy's own uninformative timeout
    error, which is the failure this whole path exists to stop producing.
    """
    import lionagi.config as config_module
    from lionhub.providers.google.gemini_code import GeminiCodeRequest

    monkeypatch.setattr(
        config_module,
        "settings",
        config_module.AppSettings(LIONAGI_ANTIGRAVITY_PRINT_TIMEOUT=1_000_000.0),
    )
    model, captured = _make_slow_cli_model(chunk_delay=0.0, n_chunks=1)
    model.endpoint._request_model = GeminiCodeRequest
    branch = Branch()
    branch.chat_model = model

    await _collect(run(branch, "hi", RunParam()))

    emitted = captured["print_timeout"]
    assert emitted.endswith("s")
    # int() rejects both "1e+06" and any other non-integer spelling, so this
    # asserts parseability rather than restating the formatting expression.
    assert int(emitted.removesuffix("s")) == 1_000_000


async def test_run_preserves_explicit_gemini_print_timeout_without_timeout():
    """An explicit provider cap also wins when no outer deadline is configured."""
    from lionhub.providers.google.gemini_code import GeminiCodeRequest

    model, captured = _make_slow_cli_model(chunk_delay=0.0, n_chunks=1)
    model.endpoint._request_model = GeminiCodeRequest
    branch = Branch()
    branch.chat_model = model

    await _collect(run(branch, "hi", RunParam(imodel_kw={"print_timeout": "45m"})))

    assert captured["print_timeout"] == "45m"


async def test_run_preserves_endpoint_gemini_print_timeout_without_timeout():
    """Endpoint configuration is explicit and must not receive a request default."""
    from lionhub.providers.google.gemini_code import GeminiCodeRequest

    model, captured = _make_slow_cli_model(chunk_delay=0.0, n_chunks=1)
    model.endpoint._request_model = GeminiCodeRequest
    model.endpoint.config = types.SimpleNamespace(kwargs={"print_timeout": "50m"})
    branch = Branch()
    branch.chat_model = model

    await _collect(run(branch, "hi", RunParam()))

    assert "print_timeout" not in captured
    assert model.endpoint.config.kwargs["print_timeout"] == "50m"


async def test_run_preserves_endpoint_gemini_print_timeout_with_caller_timeout():
    """The fourth precedence case: endpoint cap set AND a caller deadline set.

    The other three combinations of (caller timeout, explicit cap) are covered
    above. This is the one where a derived value exists and could plausibly
    overwrite an explicit one, so it is the cell most worth pinning down.
    """
    from lionhub.providers.google.gemini_code import GeminiCodeRequest

    model, captured = _make_slow_cli_model(chunk_delay=0.0, n_chunks=1)
    model.endpoint._request_model = GeminiCodeRequest
    model.endpoint.config = types.SimpleNamespace(kwargs={"print_timeout": "50m"})
    branch = Branch()
    branch.chat_model = model

    await _collect(run(branch, "hi", RunParam(imodel_kw={"timeout": 1200})))

    assert "print_timeout" not in captured
    assert model.endpoint.config.kwargs["print_timeout"] == "50m"


async def test_gemini_timeout_arms_produce_distinct_caps():
    """A caller deadline receives headroom; the configured default is the cap."""
    from lionhub.providers.google.gemini_code import GeminiCodeRequest

    async def captured_cap(imodel_kw):
        model, captured = _make_slow_cli_model(chunk_delay=0.0, n_chunks=1)
        model.endpoint._request_model = GeminiCodeRequest
        branch = Branch()
        branch.chat_model = model
        await _collect(run(branch, "hi", RunParam(imodel_kw=imodel_kw)))
        return captured["print_timeout"]

    assert await captured_cap({"timeout": 1200}) != await captured_cap({})


# Regression: Branch.operate() must flatten **kwargs so timeout reaches run()


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


# Regression: iModel.stream() must not yield in finally (swallows cancellation)


async def test_imodel_stream_propagates_cancellation():
    """iModel.stream() must propagate CancelledError from the inner stream,
    not swallow it via a yield-in-finally."""
    import anyio

    from lionagi.protocols.generic.event import EventStatus
    from lionagi.service.connections.api_calling import APICalling

    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    cancellation_order: list[str] = []

    class SlowEndpoint:
        is_cli = True
        session_id = None
        DEFAULT_QUEUE_CAPACITY = 10

        def _require_api_key(self):
            return None

        async def stream(self, request=None, extra_headers=None, **kw):
            yield StreamChunk(type="text", content="unused")

    m.endpoint = SlowEndpoint()

    api_call = AsyncMock(spec=APICalling)
    api_call.endpoint = m.endpoint
    # A real UUID: Progression.exclude() rejects anything else, which would
    # make the pending-queue bookkeeping below a no-op.
    api_call.id = uuid4()
    api_call.metadata = {}
    api_call.execution = AsyncMock()
    api_call.execution.status = EventStatus.PENDING

    async def fake_core_stream():
        try:
            await anyio.lowlevel.checkpoint()
        except BaseException:
            cancellation_order.append("cancelled_before_deadline")
            raise
        cancellation_order.append("stream_advanced_past_deadline")
        yield StreamChunk(type="text", content="late")

    api_call.stream = fake_core_stream
    pending = Progression()
    m.executor = types.SimpleNamespace(
        append=AsyncMock(side_effect=lambda event: pending.include(event.id)),
        pending=pending,
        pile=types.SimpleNamespace(ainclude=AsyncMock(), pop=lambda *a, **kw: None),
        processor=types.SimpleNamespace(
            _concurrency_sem=None,
            is_stopped=lambda: False,
        ),
        config={},
    )

    # A caller that queued the call itself before streaming it.
    await m.executor.append(api_call)

    chunks_yielded: list = []
    with pytest.raises(TimeoutError):
        with _fail_on_next_checkpoint(0.1):
            async for chunk in m.stream(api_call=api_call):
                chunks_yielded.append(chunk)

    # Cancellation must reach the inner stream at its first checkpoint. A
    # swallowed or late cancellation lets the stream advance and fails here.
    assert cancellation_order == ["cancelled_before_deadline"]
    assert chunks_yielded == [], (
        f"stream yielded {len(chunks_yielded)} chunk(s) after cancellation — "
        "CancelledError was swallowed instead of propagated"
    )
    # The call does not stay in the pending queue the executor's forward()
    # would later pop.
    assert len(pending) == 0


# Worker-liveness watchdog — regression for a CLI subprocess that dies at/near
# spawn (or otherwise produces no first output): the leg must retry once and
# then fail loud with WorkerLivenessError instead of hanging as a zombie
# "running" operation forever.


def _make_hanging_cli_model(create_event_calls: list, streams_first_output_early: bool = True):
    """A CLI iModel whose stream() never yields anything, simulating a worker subprocess that dies at/near spawn; streams_first_output_early defaults True (claude_code/codex-style early streamer) since that's what most of these fakes stand in for."""
    import anyio

    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        streams_first_output_early=streams_first_output_early,
        to_dict=lambda: {"type": "fake_cli", "session_id": None},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None

    async def create_event(**kw):
        create_event_calls.append(kw)
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        # Never yields — the subprocess "hangs" forever.
        await anyio.sleep(999)
        yield StreamChunk(type="text", content="unreachable")  # pragma: no cover

    m.stream = stream
    return m


def _make_retry_recovers_cli_model(create_event_calls: list):
    """A CLI iModel whose FIRST invocation hangs forever (dead worker) and
    whose SECOND invocation (the liveness retry) streams normally — proves
    the watchdog's retry path actually recovers a transiently-dead worker."""
    import anyio

    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        streams_first_output_early=True,
        to_dict=lambda: {"type": "fake_cli", "session_id": None},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None

    async def create_event(**kw):
        create_event_calls.append(kw)
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        if len(create_event_calls) < 2:
            await anyio.sleep(999)
            yield StreamChunk(type="text", content="unreachable")  # pragma: no cover
        else:
            yield StreamChunk(type="text", content="recovered")

    m.stream = stream
    return m


def _make_buffered_delay_cli_model(create_event_calls: list, delay: float):
    """A CLI iModel standing in for a buffered transport (e.g. gemini_code): it doesn't declare streams_first_output_early, and its stream() sleeps `delay` before its one chunk -- indistinguishable from a dead worker until the delay elapses."""
    import anyio

    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        to_dict=lambda: {"type": "fake_cli", "session_id": None},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None

    async def create_event(**kw):
        create_event_calls.append(kw)
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        await anyio.sleep(delay)
        yield StreamChunk(type="text", content="buffered result")

    m.stream = stream
    return m


def _make_scheduled_cli_model(
    create_event_calls: list,
    schedule: list[tuple[float, StreamChunk]],
    *,
    hang_after: bool = False,
    streams_first_output_early: bool = True,
):
    """A CLI iModel that emits chunks after controlled per-chunk delays."""
    import anyio

    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        streams_first_output_early=streams_first_output_early,
        to_dict=lambda: {"type": "fake_cli", "session_id": None},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None

    async def create_event(**kw):
        create_event_calls.append(kw)
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        for delay, chunk in schedule:
            await anyio.sleep(delay)
            yield chunk
        if hang_after:
            await anyio.sleep(999)

    m.stream = stream
    return m


async def test_run_liveness_watchdog_raises_after_exhausting_retries():
    """A worker that never produces a first chunk is retried once, then
    fails loud with WorkerLivenessError — the operation must be able to
    transition to FAILED instead of hanging forever."""
    from lionagi._provider_errors import WorkerLivenessError

    create_event_calls: list = []
    model = _make_hanging_cli_model(create_event_calls)
    branch = Branch()
    branch.chat_model = model

    with pytest.raises(WorkerLivenessError, match="worker.no_first_output|no first stream output"):
        async for _ in run(branch, "go", RunParam(imodel_kw={"liveness_timeout": 0.05})):
            pass

    # Exactly one retry: two fresh subprocess invocations total.
    assert len(create_event_calls) == 2, (
        f"expected exactly 2 create_event calls (1 initial + 1 retry), got {len(create_event_calls)}"
    )


def test_the_stall_context_names_the_call():
    """A burst of stalls must be tellable from one worker retrying."""
    from lionagi.operations.run.run import _stalled_worker_context

    assert _stalled_worker_context(types.SimpleNamespace(id="call-123")) == "call=call-123"


def test_the_stall_context_survives_an_object_that_answers_nothing():
    """Built on the failure path, so it must not raise there."""
    from lionagi.operations.run.run import _stalled_worker_context

    assert _stalled_worker_context(object()) == "worker unidentified"


async def test_the_liveness_error_names_the_worker_that_stalled(monkeypatch):
    """The identity has to reach the raised message, not merely be computable.

    Diagnosis reads the error text. A helper that is correct but never wired
    in leaves every log line exactly as unattributed as before, and that
    failure looks identical to a working one from the helper's own tests.
    """
    from lionagi._provider_errors import WorkerLivenessError

    class NeverYields:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()
            raise StopAsyncIteration  # pragma: no cover

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        "lionagi.operations.run.run._stream_with_deadline",
        lambda model, api_call, deadline: NeverYields(),
    )

    # A key in the caller-configured fields, which is why they are not logged.
    secret = "01234567-89ab-cdef-0123-456789abcdef"
    model = types.SimpleNamespace(
        endpoint=types.SimpleNamespace(config=types.SimpleNamespace(provider=secret)),
        model_name=secret,
        create_event=AsyncMock(return_value=types.SimpleNamespace(id="call-abc")),
        executor=types.SimpleNamespace(append=AsyncMock()),
    )

    stream = _stream_with_liveness(
        model,
        {},
        stream_deadline=None,
        liveness_timeout=0.05,
        api_call_holder=[],
        max_attempts=1,
    )
    with pytest.raises(WorkerLivenessError) as excinfo:
        async for _ in stream:
            pass

    message = str(excinfo.value)
    assert "call=call-abc" in message, message
    # The prefix external log greps key on must survive the addition.
    assert "no first stream output within" in message, message
    # Pins the context to the generated id, so a masked field back reddens too.
    assert "[call=call-abc]" in message, message
    assert secret not in message, message


async def test_liveness_cancel_during_first_chunk_wait_closes_inner_stream(monkeypatch):
    """Cancelling and closing before chunk one must close the owned inner stream."""

    class BlockingFirstChunkStream:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.started.set()
            await asyncio.Event().wait()
            raise StopAsyncIteration  # pragma: no cover

        async def aclose(self) -> None:
            self.closed = True

    inner = BlockingFirstChunkStream()
    model, _ = _make_fake_cli_model([])
    monkeypatch.setattr(
        "lionagi.operations.run.run._stream_with_deadline",
        lambda model, api_call, deadline: inner,
    )

    stream = _stream_with_liveness(
        model,
        {},
        stream_deadline=None,
        liveness_timeout=30,
        api_call_holder=[],
    )
    first_chunk = asyncio.create_task(anext(stream))
    await asyncio.wait_for(inner.started.wait(), timeout=1)

    first_chunk.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_chunk
    await stream.aclose()

    assert inner.closed


async def test_run_liveness_watchdog_recovers_on_retry():
    """A worker whose first subprocess hangs but whose retried subprocess
    streams normally must succeed — the watchdog does not fail loud when
    the retry recovers."""
    create_event_calls: list = []
    model = _make_retry_recovers_cli_model(create_event_calls)
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "go", RunParam(imodel_kw={"liveness_timeout": 0.05})))

    text_msgs = [r for r in results if isinstance(r, AssistantResponse)]
    assert len(text_msgs) == 1
    assert text_msgs[0].response == "recovered"
    assert len(create_event_calls) == 2


async def test_run_liveness_watchdog_disabled_by_zero():
    """liveness_timeout=0 disables the watchdog cleanly (deterministic/test
    runs) — legacy unbounded-wait-for-first-chunk behaviour, no retry."""
    create_event_calls: list = []
    model = _make_hanging_cli_model(create_event_calls)
    branch = Branch()
    branch.chat_model = model

    import asyncio

    with pytest.raises(asyncio.TimeoutError):
        # The watchdog is disabled, so the hang is unbounded; wrap with an
        # outer wait_for as the test-level guard instead.
        await asyncio.wait_for(
            _collect(run(branch, "go", RunParam(imodel_kw={"liveness_timeout": 0}))),
            timeout=0.2,
        )

    # Disabled watchdog still only spawns the subprocess once — no retry.
    assert len(create_event_calls) == 1


async def test_run_liveness_watchdog_uses_configured_default_when_absent(monkeypatch):
    """When the caller omits liveness_timeout, run() falls back to LIONAGI_WORKER_LIVENESS_TIMEOUT (monkeypatched small here) for an endpoint that declares streams_first_output_early."""
    import lionagi.config as config_module
    from lionagi._provider_errors import WorkerLivenessError

    create_event_calls: list = []
    model = _make_hanging_cli_model(create_event_calls)
    branch = Branch()
    branch.chat_model = model

    fast_settings = config_module.AppSettings(LIONAGI_WORKER_LIVENESS_TIMEOUT=0.05)
    monkeypatch.setattr(config_module, "settings", fast_settings)

    with pytest.raises(WorkerLivenessError):
        async for _ in run(branch, "go", RunParam()):
            pass

    assert len(create_event_calls) == 2


async def test_run_liveness_watchdog_strips_kwarg_from_create_event():
    """liveness_timeout is a run()-only knob; the provider must never see it
    in create_event kwargs (matches the existing `timeout` strip pattern)."""
    model, captured = _make_fake_cli_model([StreamChunk(type="text", content="ok")])
    branch = Branch()
    branch.chat_model = model

    await _collect(run(branch, "hi", RunParam(imodel_kw={"liveness_timeout": 5})))
    assert "liveness_timeout" not in captured, (
        f"liveness_timeout leaked into create_event kwargs: {captured!r}"
    )


async def test_run_liveness_watchdog_yields_to_caller_stream_timeout(monkeypatch):
    """When the caller's own stream `timeout` is tighter than the liveness window, the caller's TimeoutError fires unmodified -- that deliberate total-stream budget must not be reinterpreted as a worker-liveness failure."""
    import importlib

    import anyio

    create_event_calls: list = []
    model = _make_hanging_cli_model(create_event_calls)
    branch = Branch()
    branch.chat_model = model
    cancellation_order: list[str] = []

    async def stream(api_call=None):
        try:
            await anyio.lowlevel.checkpoint()
        except BaseException:
            cancellation_order.append("cancelled_before_deadline")
            raise
        cancellation_order.append("stream_advanced_past_deadline")
        yield StreamChunk(type="text", content="late")

    model.stream = stream
    run_mod = importlib.import_module("lionagi.operations.run.run")
    monkeypatch.setattr(run_mod.anyio, "fail_after", _fail_on_next_checkpoint)

    with pytest.raises(TimeoutError):
        async for _ in run(
            branch,
            "go",
            RunParam(imodel_kw={"timeout": 0.05, "liveness_timeout": 120}),
        ):
            pass

    # The caller deadline cancels at the provider's first checkpoint, before
    # the much wider liveness window can own the failure or trigger a retry.
    assert cancellation_order == ["cancelled_before_deadline"]
    assert len(create_event_calls) == 1


async def test_run_liveness_watchdog_default_path_skips_buffered_endpoint(monkeypatch):
    """A buffered transport whose first chunk legitimately arrives after the default liveness window must complete successfully with no retry -- the default watchdog isn't applied at all for endpoints that don't declare streams_first_output_early."""
    import lionagi.config as config_module

    create_event_calls: list = []
    model = _make_buffered_delay_cli_model(create_event_calls, delay=0.15)
    branch = Branch()
    branch.chat_model = model

    fast_settings = config_module.AppSettings(LIONAGI_WORKER_LIVENESS_TIMEOUT=0.05)
    monkeypatch.setattr(config_module, "settings", fast_settings)

    results = await _collect(run(branch, "go", RunParam()))

    text_msgs = [r for r in results if isinstance(r, AssistantResponse)]
    assert len(text_msgs) == 1
    assert text_msgs[0].response == "buffered result"
    # No retry — the watchdog never engaged for this endpoint.
    assert len(create_event_calls) == 1


async def test_run_liveness_watchdog_explicit_timeout_enforced_on_buffered_endpoint():
    """An explicitly-passed liveness_timeout is always honored, even for a buffered endpoint -- the caller opted in deliberately, so the watchdog still retries then raises WorkerLivenessError on a worker that never produces output."""
    from lionagi._provider_errors import WorkerLivenessError

    create_event_calls: list = []
    model = _make_hanging_cli_model(create_event_calls, streams_first_output_early=False)
    branch = Branch()
    branch.chat_model = model

    with pytest.raises(WorkerLivenessError):
        async for _ in run(branch, "go", RunParam(imodel_kw={"liveness_timeout": 0.05})):
            pass

    assert len(create_event_calls) == 2


async def test_run_idle_watchdog_fails_after_partial_output_without_retry(monkeypatch):
    """A worker that emits once then stalls fails distinctly and is not rerun."""
    import lionagi.config as config_module
    from lionagi._provider_errors import WorkerLivenessError

    create_event_calls: list[dict] = []
    model = _make_scheduled_cli_model(
        create_event_calls,
        [(0, StreamChunk(type="text", content="partial"))],
        hang_after=True,
    )
    branch = Branch()
    branch.chat_model = model
    fast_settings = config_module.AppSettings(LIONAGI_WORKER_IDLE_TIMEOUT=0.03)
    monkeypatch.setattr(config_module, "settings", fast_settings)

    with pytest.raises(WorkerLivenessError) as exc_info:
        await _collect(
            run(
                branch,
                "go",
                RunParam(
                    imodel_kw={
                        "timeout": 0.2,
                        "liveness_timeout": 0.1,
                    }
                ),
            )
        )

    assert exc_info.value.reason == "worker.stream_idle"
    assert len(create_event_calls) == 1
    assert "idle_timeout" not in create_event_calls[0]


async def test_run_idle_watchdog_resets_after_every_chunk():
    """Total stream time may exceed the idle window when each gap stays below it."""
    create_event_calls: list[dict] = []
    model = _make_scheduled_cli_model(
        create_event_calls,
        [
            (0, StreamChunk(type="text", content="a")),
            (0.03, StreamChunk(type="text", content="b")),
            (0.03, StreamChunk(type="text", content="c")),
            (0.03, StreamChunk(type="text", content="d")),
        ],
    )
    branch = Branch()
    branch.chat_model = model

    results = await _collect(
        run(
            branch,
            "go",
            RunParam(
                imodel_kw={
                    "timeout": 0.5,
                    "liveness_timeout": 0.1,
                    "idle_timeout": 0.08,
                }
            ),
        )
    )

    text_msgs = [r for r in results if isinstance(r, AssistantResponse)]
    assert [msg.response for msg in text_msgs] == ["abcd"]
    assert len(create_event_calls) == 1
    assert "idle_timeout" not in create_event_calls[0]


async def test_run_idle_watchdog_yields_to_tighter_overall_deadline():
    """The caller's total-stream budget retains ownership when it expires first."""
    from lionagi._provider_errors import WorkerLivenessError

    create_event_calls: list[dict] = []
    model = _make_scheduled_cli_model(
        create_event_calls,
        [(0, StreamChunk(type="text", content="partial"))],
        hang_after=True,
    )
    branch = Branch()
    branch.chat_model = model

    with pytest.raises(TimeoutError) as exc_info:
        await _collect(
            run(
                branch,
                "go",
                RunParam(
                    imodel_kw={
                        "timeout": 0.04,
                        "liveness_timeout": 0.1,
                        "idle_timeout": 0.2,
                    }
                ),
            )
        )

    assert not isinstance(exc_info.value, WorkerLivenessError)
    assert len(create_event_calls) == 1
    assert "idle_timeout" not in create_event_calls[0]


async def test_run_idle_watchdog_allows_normal_completion():
    """The run-only idle setting is stripped and a healthy stream completes."""
    create_event_calls: list[dict] = []
    model = _make_scheduled_cli_model(
        create_event_calls,
        [
            (0, StreamChunk(type="text", content="hello")),
            (0.01, StreamChunk(type="text", content=" world")),
        ],
    )
    branch = Branch()
    branch.chat_model = model

    results = await _collect(run(branch, "go", RunParam(imodel_kw={"idle_timeout": 0.1})))

    text_msgs = [r for r in results if isinstance(r, AssistantResponse)]
    assert [msg.response for msg in text_msgs] == ["hello world"]
    assert len(create_event_calls) == 1
    assert "idle_timeout" not in create_event_calls[0]


async def test_run_idle_watchdog_default_skips_buffered_endpoint(monkeypatch):
    """Buffered transports remain exempt from the default mid-stream bound."""
    import lionagi.config as config_module

    create_event_calls: list[dict] = []
    model = _make_scheduled_cli_model(
        create_event_calls,
        [
            (0, StreamChunk(type="text", content="buffered")),
            (0.08, StreamChunk(type="text", content=" result")),
        ],
        streams_first_output_early=False,
    )
    branch = Branch()
    branch.chat_model = model

    fast_settings = config_module.AppSettings(LIONAGI_WORKER_IDLE_TIMEOUT=0.02)
    monkeypatch.setattr(config_module, "settings", fast_settings)

    results = await _collect(run(branch, "go", RunParam()))

    text_msgs = [r for r in results if isinstance(r, AssistantResponse)]
    assert [msg.response for msg in text_msgs] == ["buffered result"]
    assert len(create_event_calls) == 1


def _collected_usage(merged_usage: dict) -> dict:
    from lionagi.session.signal import _collect_branch_usage

    msg = MagicMock()
    msg.metadata = {"model_response": {"usage": merged_usage}}
    branch = MagicMock()
    branch.msgs.messages = [msg]
    return _collect_branch_usage(branch)


def _merge_two_chunks(first_usage: dict, second_usage: dict) -> dict:
    result_meta: dict = {}
    _accumulate_result_meta(result_meta, {"usage": first_usage})
    _accumulate_result_meta(result_meta, {"usage": second_usage})
    return result_meta["usage"]


def _merge_chunks(*usages: dict) -> dict:
    result_meta: dict = {}
    for usage in usages:
        _accumulate_result_meta(result_meta, {"usage": usage})
    return result_meta["usage"]


def test_accumulate_result_meta_matches_one_message_for_an_empty_split_map():
    """One report read as a message and as a single result chunk must give the
    same answer. ``{"cache_read_input_tokens": 5, "cache_creation": {}}``
    claims a split with no cache-write total to check it against, so the
    lifetimes read 0 and the usage is not valid -- both ways."""
    report = {"cache_read_input_tokens": 5, "cache_creation": {}}
    as_message = _collected_usage(report)
    as_chunk = _collected_usage(_merge_chunks(report))
    assert as_message == as_chunk
    assert as_message["cache_write_5m_tokens"] == 0
    assert as_message["cache_write_1h_tokens"] == 0
    assert as_message["usage_valid"] is False


def test_accumulate_result_meta_matches_one_report_when_a_split_spans_two_chunks():
    """One chunk may report the five-minute writes and the next the one-hour
    writes; merged, they describe the same report as the summed counts do."""
    first = {
        "input_tokens": 1,
        "cache_creation_input_tokens": 100,
        "cache_creation": {"ephemeral_5m_input_tokens": 100},
    }
    second = {
        "input_tokens": 1,
        "cache_creation_input_tokens": 50,
        "cache_creation": {"ephemeral_1h_input_tokens": 50},
    }
    as_one_report = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 150,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 50},
    }
    assert _collected_usage(_merge_chunks(first, second)) == _collected_usage(as_one_report)


def test_accumulate_result_meta_treats_an_unknown_lifetime_key_as_not_a_valid_split():
    """A split whose keys are not a subset of the two lifetimes is not a valid
    one, whichever reader reads it: the merged split is uncertifiable, so the
    run's lifetimes read 0 and the usage is invalid."""
    usage = _merge_chunks(
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 50,
                "ephemeral_24h_input_tokens": 0,
            },
        }
    )
    collected = _collected_usage(usage)
    assert collected["cache_write_tokens"] == 50
    assert collected["cache_write_5m_tokens"] == 0
    assert collected["cache_write_1h_tokens"] == 0
    assert collected["usage_valid"] is False


def test_accumulate_result_meta_does_not_raise_when_an_unknown_key_value_is_not_a_count():
    """A key that is not a lifetime is not merged at all, so a non-count value
    under it followed by a numeric one must not reach an arithmetic step: the
    merge completes and the split stays uncertifiable."""
    usage = _merge_chunks(
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 50,
                "ephemeral_2h_input_tokens": None,
            },
        },
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_2h_input_tokens": 5},
        },
    )
    collected = _collected_usage(usage)
    assert collected["cache_write_tokens"] == 100
    assert collected["cache_write_5m_tokens"] == 0
    assert collected["cache_write_1h_tokens"] == 0
    assert collected["usage_valid"] is False


@pytest.mark.parametrize(
    "value",
    [None, "50", {"nested": 50}, [50], 50.5, True],
)
def test_accumulate_result_meta_never_raises_on_a_non_count_lifetime(value):
    """Every malformed lifetime shape -- a value that is not a count before or
    after a numeric one, a nested mapping, a sequence, a fractional float,
    None -- merges into a split that cannot be certified, and merging never
    raises on the way."""
    before = _merge_chunks(
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_5m_input_tokens": value},
        },
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_5m_input_tokens": 50},
        },
    )
    after = _merge_chunks(
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_5m_input_tokens": 50},
        },
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_5m_input_tokens": value},
        },
    )
    for usage in (before, after):
        collected = _collected_usage(usage)
        assert collected["cache_write_tokens"] == 100
        assert collected["cache_write_5m_tokens"] == 0
        assert collected["cache_write_1h_tokens"] == 0
        assert collected["usage_valid"] is False


def test_accumulate_result_meta_sums_cache_write_lifetimes_across_result_chunks():
    """A provider can report the cache-write lifetime split across more than
    one result chunk in the same flush window -- one chunk's writes at five
    minutes, the next chunk's at one hour. The nested ``cache_creation`` map
    must be summed per lifetime like the flat counters, not overwritten, or
    the earlier chunk's writes vanish from both the split and the total it
    certifies against."""
    usage = _merge_two_chunks(
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 100},
        },
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_1h_input_tokens": 50},
        },
    )
    assert usage["cache_creation_input_tokens"] == 150
    assert usage["cache_creation"] == {
        "ephemeral_5m_input_tokens": 100,
        "ephemeral_1h_input_tokens": 50,
    }


def test_accumulate_result_meta_leaves_a_split_that_is_not_a_count_uncertifiable():
    """A chunk whose lifetime value is not a token count cannot be merged with
    a later chunk's numeric one: summing would coerce the non-count into a
    number and produce a split that looks exact. The merged split stays
    uncertifiable, so the run's lifetimes read 0 and the usage is invalid --
    and merging must not raise on the way."""
    usage = _merge_two_chunks(
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": None},
        },
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_5m_input_tokens": 50},
        },
    )
    collected = _collected_usage(usage)
    assert collected["cache_write_tokens"] == 50
    assert collected["cache_write_5m_tokens"] == 0
    assert collected["cache_write_1h_tokens"] == 0
    assert collected["usage_valid"] is False


def test_accumulate_result_meta_does_not_coerce_a_bool_lifetime_into_a_count():
    """A bool is not a token count even though ``True + 50`` evaluates to 51:
    the merged split must not read as an exact one."""
    usage = _merge_two_chunks(
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 1,
            "cache_creation": {"ephemeral_5m_input_tokens": True},
        },
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_5m_input_tokens": 50},
        },
    )
    collected = _collected_usage(usage)
    assert collected["cache_write_tokens"] == 51
    assert collected["cache_write_5m_tokens"] == 0
    assert collected["cache_write_1h_tokens"] == 0
    assert collected["usage_valid"] is False


def test_accumulate_result_meta_does_not_let_two_invalid_chunks_cancel_out():
    """Two chunks whose own splits each miss their own total must not be
    summed into a total they appear to cover: 20 of 100 and 130 of 50 sum to
    150 of 150, which would certify a split no chunk ever stated."""
    usage = _merge_two_chunks(
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 20},
        },
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_5m_input_tokens": 130},
        },
    )
    collected = _collected_usage(usage)
    assert collected["cache_write_tokens"] == 150
    assert collected["cache_write_5m_tokens"] == 0
    assert collected["cache_write_1h_tokens"] == 0
    assert collected["usage_valid"] is False


@pytest.mark.parametrize("not_a_split", ["not a mapping", [50], 0, 50])
def test_accumulate_result_meta_a_split_that_is_not_a_mapping_stays_uncertifiable(not_a_split):
    """A ``cache_creation`` that is not a mapping is a split that cannot be
    certified, as it is in one report. A later valid chunk does not certify
    it, and a number in that place is not added into the merged map."""
    usage = _merge_two_chunks(
        {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_creation": not_a_split},
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_5m_input_tokens": 50},
        },
    )
    collected = _collected_usage(usage)
    assert collected["cache_write_5m_tokens"] == 0
    assert collected["cache_write_1h_tokens"] == 0
    assert collected["usage_valid"] is False


@pytest.mark.parametrize("not_a_split", ["not a mapping", [50], 50])
def test_accumulate_result_meta_a_non_mapping_split_after_a_valid_one_does_not_raise(not_a_split):
    """The same split arriving after a valid chunk: merging it neither raises
    nor keeps the earlier chunk's lifetimes as if the stream were exact."""
    usage = _merge_two_chunks(
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_5m_input_tokens": 50},
        },
        {"input_tokens": 1, "cache_creation_input_tokens": 50, "cache_creation": not_a_split},
    )
    collected = _collected_usage(usage)
    assert collected["cache_write_5m_tokens"] == 0
    assert collected["cache_write_1h_tokens"] == 0
    assert collected["usage_valid"] is False


def test_accumulate_result_meta_reads_a_null_split_as_no_split():
    """``cache_creation: null`` states no split, as an absent key does: it adds
    no part and does not erase the parts an earlier chunk reported."""
    usage = _merge_two_chunks(
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 100},
        },
        {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_creation": None},
    )
    assert usage["cache_creation"] == {
        "ephemeral_5m_input_tokens": 100,
        "ephemeral_1h_input_tokens": 0,
    }
    collected = _collected_usage(usage)
    assert collected["cache_write_5m_tokens"] == 100
    assert collected["cache_write_1h_tokens"] == 0
    assert collected["usage_valid"] is True


def test_accumulate_result_meta_sums_a_split_given_as_any_mapping():
    """A split is a mapping, not only a dict: two read-only maps sum the way
    two dicts do."""
    from types import MappingProxyType

    usage = _merge_two_chunks(
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 100,
            "cache_creation": MappingProxyType({"ephemeral_5m_input_tokens": 100}),
        },
        {
            "input_tokens": 1,
            "cache_creation_input_tokens": 50,
            "cache_creation": MappingProxyType({"ephemeral_1h_input_tokens": 50}),
        },
    )
    collected = _collected_usage(usage)
    assert collected["cache_write_5m_tokens"] == 100
    assert collected["cache_write_1h_tokens"] == 50
    assert collected["usage_valid"] is True


@pytest.mark.parametrize("not_a_count", [None, True, "50"])
def test_accumulate_result_meta_keeps_a_counter_that_was_not_a_count(not_a_count):
    """A counter one chunk reports as a non-count stays as reported when a
    later chunk reports a number: merging does not raise, a bool is not
    added into a count, and the reader still rejects the report."""
    usage = _merge_two_chunks(
        {"input_tokens": 1, "cache_creation_input_tokens": not_a_count},
        {"input_tokens": 1, "cache_creation_input_tokens": 50},
    )
    assert usage["cache_creation_input_tokens"] is not_a_count
    assert _collected_usage(usage)["usage_valid"] is False


def test_accumulate_result_meta_keeps_a_split_that_covers_each_chunks_total():
    """Each chunk splits its own cache-write total exactly, so the merged
    split is still exact against the merged total: both lifetimes survive
    into the run's totals."""
    usage = _merge_two_chunks(
        {
            "input_tokens": 10,
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0},
        },
        {
            "input_tokens": 15,
            "cache_creation_input_tokens": 150,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 50},
        },
    )
    assert usage["cache_creation_input_tokens"] == 250
    assert usage["cache_creation"] == {
        "ephemeral_5m_input_tokens": 200,
        "ephemeral_1h_input_tokens": 50,
    }
    collected = _collected_usage(usage)
    assert collected["cache_write_tokens"] == 250
    assert collected["cache_write_5m_tokens"] == 200
    assert collected["cache_write_1h_tokens"] == 50
    assert collected["usage_valid"] is True
