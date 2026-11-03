# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Live stream buffer behavior using fake providers."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi.cli._runs import RunDir
from lionagi.operations.run.run import RunParam, _append_chunk, run
from lionagi.protocols.messages import Instruction
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("LIONAGI_STATE_DB_URL", raising=False)
    monkeypatch.setattr("lionagi._paths._find_git_root", lambda _cwd: None)
    monkeypatch.setattr("lionagi.cli._project._find_git_root", lambda _cwd: None)


def _run_dir(tmp_path):
    root = tmp_path / "runs" / "run-one"
    return RunDir(run_id="run-one", state_root=root, artifact_root=root / "artifacts")


async def _collect(generator):
    return [item async for item in generator]


def _paused_model(paused, release):
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    model.endpoint = SimpleNamespace(
        is_cli=True, session_id=None, to_dict=lambda: {"type": "fake_cli", "session_id": None}
    )
    model.streaming_process_func = None
    model.executor = SimpleNamespace(append=AsyncMock(), config={})

    async def create_event(**_kwargs):
        return object()

    async def stream(api_call=None):
        yield StreamChunk(type="text", content="first streamed text")
        # The caller asks for the next item only after it has processed the
        # first one, including its buffer write.
        paused.set()
        await release.wait()
        yield StreamChunk(type="text", content=" then more")

    model.create_event = create_event
    model.stream = stream
    return model


@pytest.mark.asyncio
async def test_real_stream_chunk_is_visible_at_run_dir_path_while_live(tmp_path):
    paused, release = asyncio.Event(), asyncio.Event()
    branch = Branch()
    branch.chat_model = _paused_model(paused, release)
    run_dir = _run_dir(tmp_path)
    expected = run_dir.stream_buffer_path(str(branch.id))
    gen = run(
        branch,
        "hello",
        RunParam(
            stream_persist=True, persist_dir=run_dir.stream_dir, snapshot_dir=run_dir.branches_dir
        ),
    )
    assert isinstance(await gen.__anext__(), Instruction)
    collector = asyncio.create_task(_collect(gen))
    try:
        await asyncio.wait_for(paused.wait(), 5)
        assert expected.is_file()
        rows = [json.loads(line) for line in expected.read_text().splitlines()]
        assert rows and rows[0]["type"] == "text"
        assert rows[0]["content"] == "first streamed text"
        assert run_dir.branch_path(str(branch.id)).is_file()
    finally:
        release.set()
        await asyncio.wait_for(collector, 5)
    assert not expected.exists()
    assert run_dir.branch_path(str(branch.id)).is_file()


@pytest.mark.asyncio
async def test_buffer_uses_the_reader_filename_with_a_serializable_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(
        StreamChunk,
        "to_dict",
        lambda chunk: {"type": chunk.type, "content": chunk.content},
        raising=False,
    )
    paused, release = asyncio.Event(), asyncio.Event()
    branch = Branch()
    branch.chat_model = _paused_model(paused, release)
    run_dir = _run_dir(tmp_path)
    expected = run_dir.stream_buffer_path(str(branch.id))
    gen = run(
        branch,
        "hello",
        RunParam(
            stream_persist=True,
            persist_dir=run_dir.stream_dir,
            snapshot_dir=run_dir.branches_dir,
        ),
    )
    assert isinstance(await gen.__anext__(), Instruction)
    collector = asyncio.create_task(_collect(gen))
    try:
        await asyncio.wait_for(paused.wait(), 5)
        assert expected.is_file()
        assert json.loads(expected.read_text().splitlines()[0])["content"] == "first streamed text"
    finally:
        release.set()
        await asyncio.wait_for(collector, 5)
    assert not expected.exists()


@pytest.mark.asyncio
async def test_append_serializes_a_real_stream_chunk(tmp_path):
    path = tmp_path / "buffer.jsonl"
    await _append_chunk(path, StreamChunk(type="text", content="first", metadata={"step": 1}))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["type"] == "text"
    assert rows[0]["content"] == "first"
    assert rows[0]["metadata"] == {"step": 1}


@pytest.mark.asyncio
async def test_append_preserves_every_normalized_chunk_field(tmp_path):
    path = tmp_path / "buffer.jsonl"
    chunk = StreamChunk(
        type="tool_use",
        content="summary",
        tool_name="lookup",
        tool_id="call-1",
        tool_input={"terms": ["one", "two"]},
        tool_output={"count": 2},
        is_error=True,
        is_delta=True,
        metadata={"nested": {"step": 1}},
    )
    await _append_chunk(path, chunk)
    assert json.loads(path.read_text()) == {
        "type": "tool_use",
        "content": "summary",
        "tool_name": "lookup",
        "tool_id": "call-1",
        "tool_input": {"terms": ["one", "two"]},
        "tool_output": {"count": 2},
        "is_error": True,
        "is_delta": True,
        "metadata": {"nested": {"step": 1}},
    }


class _LegacyChunk:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return self.payload


@pytest.mark.asyncio
async def test_raising_legacy_serializer_keeps_accessible_text(tmp_path):
    class BrokenLegacyChunk:
        type = "text"
        content = "still visible"

        def to_dict(self):
            raise ValueError("no projection")

    path = tmp_path / "buffer.jsonl"
    await _append_chunk(path, BrokenLegacyChunk())
    row = json.loads(path.read_text())
    assert row["type"] == "text"
    assert row["content"] == "still visible"
    assert row["serialization_error"]


@pytest.mark.asyncio
async def test_fallback_keeps_string_subclass_text_without_calling_its_str(tmp_path):
    class Text(str):
        def __str__(self):
            raise AssertionError("must not call user conversion")

    path = tmp_path / "buffer.jsonl"
    await _append_chunk(
        path, StreamChunk(type="text", content=Text("visible"), metadata={"bad": object()})
    )
    assert json.loads(path.read_text())["content"] == "visible"


@pytest.mark.asyncio
async def test_reader_displays_the_actual_nonfinite_fallback(tmp_path):
    from lionagi.cli.monitor import _stream_tail

    stream = tmp_path / "stream"
    stream.mkdir()
    await _append_chunk(
        stream / "branch.buffer.jsonl",
        StreamChunk(type="text", content="visible fallback", metadata={"bad": float("nan")}),
    )
    assert _stream_tail(tmp_path, "branch") == ["visible fallback"]


@pytest.mark.asyncio
async def test_append_keeps_legacy_to_dict_serialization(tmp_path):
    path = tmp_path / "buffer.jsonl"
    await _append_chunk(path, _LegacyChunk({"text": "legacy"}))
    assert json.loads(path.read_text()) == {"text": "legacy"}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["text", {"text": "value"}, ["text", 7], 7, 1.25])
async def test_plain_legacy_values_keep_their_original_json_bytes(tmp_path, value):
    from lionagi.ln import json_dumps

    path = tmp_path / "buffer.jsonl"
    await _append_chunk(path, _LegacyChunk(value))
    assert path.read_text() == json_dumps(value, check_non_finite=True) + "\n"


@pytest.mark.asyncio
async def test_leaf_with_both_adapters_uses_to_dict_once(tmp_path):
    class Both:
        calls = []

        def to_dict(self):
            self.calls.append("to_dict")
            return {"chosen": "to_dict"}

        def model_dump(self):
            self.calls.append("model_dump")
            return {"chosen": "model_dump"}

    leaf = Both()
    path = tmp_path / "buffer.jsonl"
    await _append_chunk(path, StreamChunk(type="text", content="visible", metadata={"leaf": leaf}))
    assert json.loads(path.read_text())["metadata"]["leaf"] == {"chosen": "to_dict"}
    assert leaf.calls == ["to_dict"]


@pytest.mark.asyncio
async def test_invalid_json_fragment_gets_a_marked_fallback(tmp_path):
    import orjson

    path = tmp_path / "buffer.jsonl"
    chunk = StreamChunk(type="text", content="visible", metadata={"bad": orjson.Fragment(b"NaN")})
    await _append_chunk(path, chunk)
    row = json.loads(path.read_text(), parse_constant=lambda value: pytest.fail(value))
    assert row["content"] == "visible"
    assert row["serialization_error"]


@pytest.mark.asyncio
async def test_append_propagates_file_write_errors(tmp_path, monkeypatch):
    import anyio

    monkeypatch.setattr(anyio, "open_file", AsyncMock(side_effect=OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        await _append_chunk(tmp_path / "buffer.jsonl", StreamChunk(type="text", content="visible"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["legacy", "normalized"])
@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "negative-inf"]
)
async def test_append_writes_a_marked_text_fallback_for_nonfinite_values(tmp_path, kind, bad):
    path = tmp_path / "buffer.jsonl"
    chunk = (
        _LegacyChunk({"text": "visible", "usage": {"rate": bad}})
        if kind == "legacy"
        else StreamChunk(type="text", content="visible", metadata={"usage": {"rate": bad}})
    )
    await _append_chunk(path, chunk)
    row = json.loads(path.read_text(), parse_constant=lambda value: pytest.fail(value))
    assert row.get("content", row.get("text")) == "visible"
    assert "non-finite" in row["serialization_error"].lower()
    assert "usage" not in row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"delta": {"text": "visible"}, "bad": object()}, None),
        ({"type": "text", "content": "visible", "bad": object()}, "text"),
        ({"type": "thinking", "content": "visible", "bad": object()}, "thinking"),
    ],
)
async def test_legacy_fallback_preserves_text_and_known_chunk_type(
    tmp_path, payload, expected_type
):
    path = tmp_path / "buffer.jsonl"
    await _append_chunk(path, _LegacyChunk(payload))
    row = json.loads(path.read_text())
    assert row.get("content", row.get("text")) == "visible"
    assert row.get("type") == expected_type
    assert row["serialization_error"]


@pytest.mark.asyncio
async def test_append_marks_an_unserializable_leaf_and_continues(tmp_path):
    class Unserializable:
        def __repr__(self):
            raise AssertionError("must not render the leaf")

    path = tmp_path / "buffer.jsonl"
    await _append_chunk(
        path, StreamChunk(type="text", content="visible", metadata={"x": Unserializable()})
    )
    await _append_chunk(path, StreamChunk(type="text", content="next"))
    rows = [
        json.loads(line, parse_constant=lambda value: pytest.fail(value))
        for line in path.read_text().splitlines()
    ]
    assert len(rows) == 2
    assert rows[0]["type"] == "text" and rows[0]["content"] == "visible"
    assert rows[0]["serialization_error"]
    assert rows[1]["content"] == "next"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["legacy", "normalized"])
async def test_stateful_leaf_is_converted_once_before_nonfinite_fallback(tmp_path, kind):
    class ChangingLeaf:
        calls = 0

        def to_dict(self):
            self.calls += 1
            return {"rate": float("nan") if self.calls == 1 else 1.0}

    leaf = ChangingLeaf()
    path = tmp_path / "buffer.jsonl"
    chunk = (
        _LegacyChunk({"text": "visible", "metadata": {"leaf": leaf}})
        if kind == "legacy"
        else StreamChunk(type="text", content="visible", metadata={"leaf": leaf})
    )
    await _append_chunk(path, chunk)
    row = json.loads(path.read_text(), parse_constant=lambda value: pytest.fail(value))
    assert leaf.calls == 1
    assert row.get("content", row.get("text")) == "visible"
    assert "non-finite" in row["serialization_error"].lower()


@pytest.mark.asyncio
async def test_existing_buffer_is_truncated_before_the_first_chunk(tmp_path):
    started, release, written, finish = (asyncio.Event() for _ in range(4))
    branch = Branch()
    branch.chat_model = _paused_model(written, finish)

    async def stream(api_call=None):
        started.set()
        await release.wait()
        yield StreamChunk(type="text", content="fresh")
        written.set()
        await finish.wait()

    branch.chat_model.stream = stream
    run_dir = _run_dir(tmp_path)
    path = run_dir.stream_buffer_path(str(branch.id))
    path.parent.mkdir(parents=True)
    path.write_text('{"text":"stale"}\n')
    gen = run(
        branch,
        "hello",
        RunParam(
            stream_persist=True, persist_dir=run_dir.stream_dir, snapshot_dir=run_dir.branches_dir
        ),
    )
    assert isinstance(await gen.__anext__(), Instruction)
    collector = asyncio.create_task(_collect(gen))
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert path.read_text() == ""
        release.set()
        await asyncio.wait_for(written.wait(), 5)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert [row["content"] for row in rows] == ["fresh"]
    finally:
        release.set()
        finish.set()
        await asyncio.wait_for(collector, 5)
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["result", "result-exit"])
async def test_refused_attempt_is_absent_from_live_buffer_before_retry_completes(
    tmp_path, monkeypatch, how
):
    """The rejected attempt never reaches disk; the accepted attempt does."""
    import lionhub.providers.anthropic.claude_code as cc

    saved_id = "7d1c2e90-5b3a-4f6e-9a8d-0c1b2a3d4e5f"
    new_id = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    paused, release = asyncio.Event(), asyncio.Event()
    calls, refused_sent = [], []

    async def fake_cli(cmd, **_kwargs):
        calls.append(list(cmd))
        if "--resume" in cmd:
            refused_sent.append(True)
            yield {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "errors": [f"No conversation found with session ID: {saved_id}"],
                "usage": {"input_tokens": 777, "output_tokens": 7},
            }
            if how == "result-exit":
                raise RuntimeError("fake CLI exited with code 1")
            return
        yield {"type": "system", "subtype": "init", "session_id": new_id}
        yield {
            "type": "assistant",
            "message": {"id": "m-accepted", "content": [{"type": "text", "text": "accepted"}]},
        }
        paused.set()
        await release.wait()
        yield {
            "type": "result",
            "subtype": "success",
            "result": "accepted",
            "is_error": False,
            "session_id": new_id,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: "claude")
    monkeypatch.setattr(cc, "ndjson_from_cli", fake_cli)
    initial = Branch(
        chat_model=iModel(provider="claude_code", model="claude-sonnet-5-5", api_key="test-key")
    )
    initial.chat_model.endpoint.session_id = saved_id
    branch = Branch.from_dict(json.loads(json.dumps(initial.to_dict(mode="json"))))
    assert branch.chat_model.provider_session_id == saved_id
    assert branch.chat_model.endpoint.reported_session_id is None
    run_dir = _run_dir(tmp_path)
    expected = run_dir.stream_buffer_path(str(branch.id))
    gen = run(
        branch,
        "hello",
        RunParam(
            stream_persist=True, persist_dir=run_dir.stream_dir, snapshot_dir=run_dir.branches_dir
        ),
    )
    assert isinstance(await gen.__anext__(), Instruction)
    collector = asyncio.create_task(_collect(gen))
    try:
        await asyncio.wait_for(paused.wait(), 5)
        assert refused_sent == [True] and len(calls) == 2
        assert "--resume" in calls[0] and "--resume" not in calls[1]
        assert expected.is_file()
        rows = [json.loads(line) for line in expected.read_text().splitlines()]
        assert any(row.get("type") == "text" and row.get("content") == "accepted" for row in rows)
        assert "777" not in json.dumps(rows)
    finally:
        release.set()
        await asyncio.wait_for(collector, 5)
    assert not expected.exists()
