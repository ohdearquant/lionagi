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


class _LegacyChunk:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return self.payload


@pytest.mark.asyncio
async def test_append_keeps_legacy_to_dict_serialization(tmp_path):
    path = tmp_path / "buffer.jsonl"
    await _append_chunk(path, _LegacyChunk({"text": "legacy"}))
    assert json.loads(path.read_text()) == {"text": "legacy"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["legacy", "normalized"])
async def test_append_rejects_nonfinite_without_creating_a_file(tmp_path, kind):
    path = tmp_path / "buffer.jsonl"
    chunk = (
        _LegacyChunk({"usage": {"rate": float("nan")}})
        if kind == "legacy"
        else StreamChunk(type="result", metadata={"usage": {"rate": float("nan")}})
    )
    with pytest.raises(ValueError, match="non-finite float"):
        await _append_chunk(path, chunk)
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
