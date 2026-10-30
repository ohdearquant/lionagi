# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fake-only checks for a release gate around process signal sites."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import threading

import pytest

from lionagi.ln import _proc


def _forbid(*_args, **_kwargs):
    raise AssertionError("test forbids real subprocesses, OS signals, and networking")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, _forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)


class ReleaseAfter(_proc.SignalGate):
    def __init__(self, count):
        super().__init__()
        self.count = count
        self.calls = 0
        if count == 0:
            self.release()

    def run(self, action, *, skipped):
        result = super().run(action, skipped=skipped)
        self.calls += 1
        if self.calls == self.count:
            self.release()
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("release_after", [0, 1, 2, 3, 4, None])
async def test_gate_covers_initial_and_escalation_group_and_child_signals(
    monkeypatch, release_after
):
    gate = ReleaseAfter(release_after)
    calls = []
    witnesses = ((4242, 12.0),)

    class Child:
        pid = 4242

        def terminate(self):
            assert gate._lock.locked()
            calls.append("child-term")

        def kill(self):
            assert gate._lock.locked()
            calls.append("child-kill")

        async def wait(self):
            await asyncio.Event().wait()

    def group_outcome(pgid, actual, sig):
        assert gate._lock.locked()
        assert pgid == 4242 and actual is witnesses
        calls.append("group-term" if sig == signal.SIGTERM else "group-kill")
        return "sent"

    monkeypatch.setattr(_proc, "_safe_pgid", lambda _proc: 4242)
    monkeypatch.setattr(_proc, "_signal_group_outcome", group_outcome)
    monkeypatch.setattr(_proc, "_group_has_members", lambda _pgid: True)
    await _proc.aterminate_process_group(
        Child(), grace=0.01, group_witnesses=witnesses, signal_gate=gate
    )
    expected = ["group-term", "child-term", "group-kill", "child-kill"]
    assert calls == (expected if release_after is None else expected[:release_after])


@pytest.mark.asyncio
@pytest.mark.parametrize("release_after", [0, 1, 2, None])
async def test_gate_covers_immediate_group_and_child_kill(monkeypatch, release_after):
    gate, calls = ReleaseAfter(release_after), []
    witnesses = ((4242, 12.0),)

    class Child:
        pid = 4242

        def kill(self):
            assert gate._lock.locked()
            calls.append("child-kill")

    def signal_group(pgid, actual, sig):
        assert gate._lock.locked()
        assert (pgid, actual, sig) == (4242, witnesses, signal.SIGKILL)
        calls.append("group-kill")
        return True

    monkeypatch.setattr(_proc, "_safe_pgid", lambda _proc: 4242)
    monkeypatch.setattr(_proc, "signal_group_if_witnessed", signal_group)
    await _proc.aterminate_process_group(
        Child(), grace=None, group_witnesses=witnesses, signal_gate=gate
    )
    expected = ["group-kill", "child-kill"]
    assert calls == (expected if release_after is None else expected[:release_after])


@pytest.mark.asyncio
async def test_group_witness_check_and_fake_syscall_run_inside_gate(monkeypatch):
    gate = _proc.SignalGate()
    witnesses = ((4242, 12.0),)
    events = []

    class Child:
        pid = 4242

        def kill(self):
            assert gate._lock.locked()
            events.append("child-kill")

    def witnessed(pgid, current):
        assert gate._lock.locked() and pgid == 4242 and current is witnesses
        events.append("witness")
        return True

    def fake_killpg(pgid, sig):
        assert gate._lock.locked() and (pgid, sig) == (4242, signal.SIGKILL)
        events.append("group-kill")

    monkeypatch.setattr(_proc, "_safe_pgid", lambda _proc: 4242)
    monkeypatch.setattr(_proc, "safe_pgid_value", lambda pgid: pgid)
    monkeypatch.setattr(_proc, "_group_has_witness", witnessed)
    monkeypatch.setattr(os, "killpg", fake_killpg)
    await _proc.aterminate_process_group(Child(), group_witnesses=witnesses, signal_gate=gate)
    assert events == ["witness", "group-kill", "child-kill"]
    gate.release()
    await _proc.aterminate_process_group(Child(), group_witnesses=witnesses, signal_gate=gate)
    assert events == ["witness", "group-kill", "child-kill"]


def test_release_serializes_with_in_flight_action_and_blocks_later_actions():
    gate, entered, finish, released = (
        _proc.SignalGate(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    events = []

    def action():
        entered.set()
        assert finish.wait(2)
        events.append("signal")
        return "sent"

    def signal_thread():
        assert gate.run(action, skipped="skipped") == "sent"

    def release_thread():
        gate.release()
        events.append("released")
        released.set()

    first = threading.Thread(target=signal_thread)
    second = threading.Thread(target=release_thread)
    first.start()
    try:
        assert entered.wait(1)
        second.start()
        assert not released.is_set()
        finish.set()
        first.join(2)
        second.join(2)
        assert not first.is_alive() and not second.is_alive()
        assert events == ["signal", "released"]
        assert gate.run(lambda: events.append("late"), skipped=None) is None
        assert events == ["signal", "released"]
    finally:
        finish.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)


@pytest.mark.asyncio
@pytest.mark.parametrize("grace", [None, 0.01])
async def test_no_gate_preserves_original_group_and_child_calls(monkeypatch, grace):
    calls = []
    witnesses = ((4242, 12.0),)

    class Child:
        pid = 4242

        def kill(self):
            calls.append("child-kill")

        def terminate(self):
            calls.append("child-term")

        async def wait(self):
            await asyncio.Event().wait()

    def group(pgid, actual, sig):
        assert pgid == 4242 and actual is witnesses
        calls.append("group-term" if sig == signal.SIGTERM else "group-kill")
        return "sent"

    def immediate(pgid, actual, sig):
        group(pgid, actual, sig)
        return True

    monkeypatch.setattr(_proc, "_safe_pgid", lambda _proc: 4242)
    monkeypatch.setattr(_proc, "_signal_group_outcome", group)
    monkeypatch.setattr(_proc, "signal_group_if_witnessed", immediate)
    monkeypatch.setattr(_proc, "_group_has_members", lambda _pgid: True)
    await _proc.aterminate_process_group(Child(), grace=grace, group_witnesses=witnesses)
    expected = (
        ["group-kill", "child-kill"]
        if grace is None
        else ["group-term", "child-term", "group-kill", "child-kill"]
    )
    assert calls == expected


@pytest.mark.asyncio
async def test_gate_does_not_authorize_unwitnessed_group(monkeypatch):
    gate, calls = _proc.SignalGate(), []

    class Child:
        pid = 4242

        def kill(self):
            calls.append("child-kill")

    monkeypatch.setattr(_proc, "_safe_pgid", lambda _proc: 4242)
    monkeypatch.setattr(_proc, "safe_pgid_value", lambda pgid: pgid)
    monkeypatch.setattr(_proc, "_group_has_witness", lambda *_args: False)
    monkeypatch.setattr(os, "killpg", lambda *_args: calls.append("group-kill"))
    await _proc.aterminate_process_group(Child(), group_witnesses=((4242, 12.0),), signal_gate=gate)
    assert calls == ["child-kill"]
