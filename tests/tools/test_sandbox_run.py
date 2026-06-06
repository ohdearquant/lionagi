# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Glue tests for run_in_sandbox with a FAKE sandbox (ADR-0083 Phase 2).

Drives the host glue end-to-end — sandbox lifecycle → ``exec_stream`` stdout →
``on_stdout`` → asyncio queue → ``SandboxBridge`` → local ``state.db`` — without
any Daytona dependency or network. The fake sandbox replays canned ``@@LIONDB@@``
lines (chunked across callback calls, to exercise the line-buffering in
``on_stdout``) and returns a result.json, exactly as the real driver would.
"""

from __future__ import annotations

import json
from pathlib import Path

from lionagi import Branch
from lionagi.protocols.messages.manager import MessageManager
from lionagi.state.db import StateDB
from lionagi.tools.sandbox_protocol import branch_event, encode_event, message_event
from lionagi.tools.sandbox_run import run_in_sandbox


class _FakeSandbox:
    """Async-context sandbox that replays canned stdout and a result.json."""

    def __init__(self, stdout_chunks: list[str], result: dict):
        self._chunks = stdout_chunks
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def home_dir(self) -> str:
        return "/home/daytona"

    async def clone(self, *a, **k):
        return None

    async def upload_file(self, *a, **k):
        return None

    async def write_text(self, *a, **k):
        return None

    async def exec_stream(self, command, *, on_stdout=None, on_stderr=None, **k) -> int:
        for chunk in self._chunks:
            if on_stdout:
                on_stdout(chunk)
        return 0

    async def read_text(self, path: str) -> str:
        return json.dumps(self._result)


def _canned_stream(worker: Branch, msgs: list) -> list[str]:
    """Build the @@LIONDB@@ wire lines a real in-sandbox run would emit, then
    re-chunk them so a line is split ACROSS callback calls (tests buffering)."""
    events = [branch_event(worker.to_dict(mode="db"), system_msg=None)]
    events += [message_event(str(worker.id), m.to_dict(mode="db")) for m in msgs]
    blob = "".join(encode_event(ev) for ev in events)  # each ends with "\n"
    mid = len(blob) // 2
    return [blob[:mid], blob[mid:]]  # two chunks, boundary mid-line


async def test_run_in_sandbox_streams_to_local_db(tmp_path: Path):
    worker = Branch(name="sandbox-coder")
    m1 = MessageManager.create_instruction(
        instruction="reproduce", sender="u", recipient=str(worker.id)
    )
    m2 = MessageManager.create_instruction(instruction="fix", sender="u", recipient=str(worker.id))
    chunks = _canned_stream(worker, [m1, m2])

    async def fake_factory():
        return _FakeSandbox(chunks, {"status": "ok", "final": "done", "model": "openrouter/x"})

    db_path = str(tmp_path / "bridge.db")
    ret = await run_in_sandbox(
        instruction="resolve the issue",
        model="openrouter/deepseek/deepseek-v4-flash",
        provider="pi",
        project="lionagi",
        env={"OPENROUTER_API_KEY": "test-key"},
        db_path=db_path,
        sandbox_factory=fake_factory,
    )

    assert ret["status"] == "completed"
    assert ret["result"]["status"] == "ok"
    sid = ret["session_id"]

    async with StateDB(db_path) as db:
        s = await db.get_session(sid)
        b = await db.get_branch(str(worker.id))
        m1_row = await db.get_message(str(m1.id))
    # The sandboxed run is a normal session in the local DB: monitor/Studio-ready.
    assert s["status"] == "completed"
    assert s["provider"] == "pi"
    assert s["model"] == "openrouter/deepseek/deepseek-v4-flash"
    assert s["first_msg_id"] == str(m1.id)
    assert s["last_msg_id"] == str(m2.id)
    assert b is not None and b["session_id"] == sid
    assert m1_row is not None


async def test_run_in_sandbox_missing_result_marks_failed(tmp_path: Path):
    """No result.json (or a non-ok status) → the session lands as failed."""

    class _NoResultSandbox(_FakeSandbox):
        async def read_text(self, path: str) -> str:
            raise FileNotFoundError(path)

    worker = Branch(name="sandbox-coder")
    chunks = _canned_stream(worker, [])

    async def fake_factory():
        return _NoResultSandbox(chunks, {})

    db_path = str(tmp_path / "bridge.db")
    ret = await run_in_sandbox(
        instruction="x",
        model="openrouter/x",
        provider="pi",
        db_path=db_path,
        sandbox_factory=fake_factory,
    )
    assert ret["status"] == "failed"
    async with StateDB(db_path) as db:
        s = await db.get_session(ret["session_id"])
    assert s["status"] == "failed"
