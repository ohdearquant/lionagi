# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A failed group signal still attempts direct-child cleanup."""

import asyncio
import os
import socket
import subprocess
from types import SimpleNamespace

import pytest

from lionagi.ln import _proc
from lionhub.providers import _cli_subprocess as cli


def _forbid(*_args, **_kwargs):
    raise AssertionError("fake-only test forbids process, signal and network effects")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, _forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)


@pytest.mark.asyncio
async def test_raising_group_signal_still_terminates_child_and_preserves_error(monkeypatch):
    actions = []
    failure = ValueError("private group identity detail")
    proc = SimpleNamespace(pid=4242, returncode=None)
    spawned = cli.SpawnedProcess(4242, 4242, 1.0)

    def terminate():
        actions.append("terminate")
        proc.returncode = 0

    def kill():
        actions.append("kill")
        proc.returncode = 0

    def group_signal(_pgid, _witnesses, _signal):
        actions.append("group")
        raise failure

    def backstop(child, witnesses, *, signal_gate):
        assert child is proc and witnesses == ((4242, 1.0),)
        signal_gate.run(lambda: actions.append("backstop"), skipped=None)
        return "unproven"

    async def exited(_proc):
        return None

    proc.terminate = terminate
    proc.kill = kill
    monkeypatch.setattr(cli, "_witnesses_for_spawn", lambda *_: ((4242, 1.0),))
    monkeypatch.setattr(cli, "_end_group_with_evidence", backstop)
    monkeypatch.setattr(_proc, "_safe_pgid", lambda _proc: 4242)
    monkeypatch.setattr(_proc, "_signal_group_outcome", group_signal)
    monkeypatch.setattr(_proc, "_await_child_exit", exited)
    monkeypatch.setattr(_proc, "_group_has_members", lambda _pgid: False)
    result = await cli._bounded_cli_cleanup(proc, spawned, grace=0.02)
    assert result.outcome is cli._ChildGroupCleanup.UNCONFIRMED
    assert result.error is failure
    assert actions == ["group", "terminate", "backstop"]
