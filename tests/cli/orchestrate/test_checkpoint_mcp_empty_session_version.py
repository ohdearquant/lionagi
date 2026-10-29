# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Version refusals remain visible when no session graph was recorded."""

import os
import socket
import subprocess

import pytest


def _deny(*_args, **_kwargs):
    raise AssertionError("offline test must not start processes, signal, or use sockets")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _deny)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _deny)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _deny)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checkpoint", "expected"),
    [
        ('{"version":4}', "unsupported_checkpoint_version"),
        ('{"version":2,"version":3}', "unsupported_checkpoint_version"),
        (None, "no_session_detail"),
    ],
    ids=["unknown-version", "ambiguous-version", "no-checkpoint"],
)
async def test_empty_session_preserves_checkpoint_version_refusal(
    tmp_path, monkeypatch, checkpoint, expected
):
    from lionagi import _paths
    from lionagi.state import db as state_db
    from lionagi.studio.services import sessions
    from lionhub.serve.mcp._run_detail import build_run_detail

    run_id = "empty-session-checkpoint"
    if checkpoint is not None:
        run_dir = tmp_path / run_id
        run_dir.mkdir()
        (run_dir / "checkpoint.json").write_text(checkpoint, encoding="utf-8")
    monkeypatch.setattr(_paths, "RUNS_ROOT", tmp_path)

    class FakeStateDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get_sessions_for_run(self, _run_id):
            return [{"id": "session", "updated_at": 1}]

    async def empty_session(_session_id):
        return {"graph": None, "branches": [], "node_metadata": None}

    monkeypatch.setattr(state_db, "StateDB", FakeStateDB)
    monkeypatch.setattr(sessions, "get_session", empty_session)
    assert await build_run_detail(run_id) == {"detail_unavailable": expected}
