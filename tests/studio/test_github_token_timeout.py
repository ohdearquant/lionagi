# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""GitHub CLI token lookups use bounded, cancellable process-group execution."""

from __future__ import annotations

import asyncio
import logging

import pytest

from lionagi.ln import _proc
from lionagi.studio.scheduler import github

pytestmark = pytest.mark.asyncio


class _Child:
    pid = None  # No fake PID can authorize a real process-group signal.

    def __init__(self, *, response=None, returncode=0):
        self.response = response
        self.returncode = None
        self.exit_code = returncode
        self.started = asyncio.Event()
        self.killed = asyncio.Event()
        self.exited = asyncio.Event()
        self.stdin = None
        self.kills = 0
        self.waits = 0
        self.reaped = False

    async def communicate(self, data=None):
        self.stdin = data
        self.started.set()
        if self.response is not None:
            self.returncode = self.exit_code
            self.exited.set()
            return self.response
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def kill(self):
        self.kills += 1
        self.killed.set()
        self.returncode = -9
        self.exited.set()

    async def wait(self):
        self.waits += 1
        await self.exited.wait()
        self.reaped = True
        return self.returncode


def _install_child(monkeypatch, child):
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return child

    monkeypatch.setattr(_proc.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(_proc, "observe_process_group_leader", lambda *args, **kwargs: None)
    return calls


def _short_exchange(monkeypatch):
    """Keep the actual helper and record its configured deadlines."""
    real_wait_for = asyncio.wait_for
    deadlines = []

    async def wait_for(awaitable, timeout):
        deadlines.append(timeout)
        return await real_wait_for(awaitable, timeout=0.01 if timeout in (10, 2) else timeout)

    monkeypatch.setattr(_proc.asyncio, "wait_for", wait_for)
    return deadlines


def _warnings(caplog):
    return [
        record
        for record in caplog.records
        if record.name == github.__name__ and record.levelno >= logging.WARNING
    ]


async def test_token_timeout_kills_reaps_and_warns_without_token(monkeypatch, caplog):
    child = _Child()
    calls = _install_child(monkeypatch, child)
    deadlines = _short_exchange(monkeypatch)
    caplog.set_level(logging.WARNING, logger=github.__name__)

    assert await asyncio.wait_for(github._gh_cli_token(), 0.5) is None
    assert calls[0][0] == ("gh", "auth", "token")
    assert calls[0][1]["start_new_session"] is True
    assert child.stdin == b""
    assert child.kills == 1
    assert child.waits == 1
    assert child.reaped
    assert 10 in deadlines
    assert 2.0 in deadlines
    assert len(_warnings(caplog)) == 1
    message = _warnings(caplog)[0].getMessage().lower()
    assert "timeout" in message or "timed out" in message
    assert "synthetic-secret-token" not in caplog.text


async def test_cancellation_during_communicate_kills_reaps_and_propagates(monkeypatch):
    child = _Child()
    calls = _install_child(monkeypatch, child)
    task = asyncio.create_task(github._gh_cli_token())
    await asyncio.wait_for(child.started.wait(), 0.5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.5)
    assert calls[0][1]["start_new_session"] is True
    assert child.kills == 1
    assert child.reaped


async def test_token_timeout_bounds_wait_for_unreapable_child(monkeypatch, caplog):
    class _Unreapable(_Child):
        async def wait(self):
            self.waits += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.wait_cancelled = True
                raise

    child = _Unreapable()
    child.wait_cancelled = False
    _install_child(monkeypatch, child)
    deadlines = _short_exchange(monkeypatch)
    caplog.set_level(logging.WARNING, logger=github.__name__)

    assert await asyncio.wait_for(github._gh_cli_token(), 0.5) is None
    assert child.kills == 1
    assert child.waits == 1
    assert child.wait_cancelled
    assert not child.reaped
    assert 10 in deadlines and 2.0 in deadlines
    assert len(_warnings(caplog)) == 1


async def test_timeout_warning_does_not_copy_exception_details(monkeypatch, caplog):
    class _SensitiveTimeout(_Child):
        async def communicate(self, data=None):
            self.stdin = data
            raise asyncio.TimeoutError("synthetic-secret-token")

    child = _SensitiveTimeout()
    _install_child(monkeypatch, child)
    _short_exchange(monkeypatch)
    caplog.set_level(logging.WARNING, logger=github.__name__)

    assert await asyncio.wait_for(github._gh_cli_token(), 0.5) is None
    assert len(_warnings(caplog)) == 1
    assert "synthetic-secret-token" not in caplog.text
    assert child.kills == 1
    assert child.reaped


async def test_cancellation_during_spawn_kills_late_child(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    child = _Child()
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return child

    monkeypatch.setattr(_proc.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(_proc, "observe_process_group_leader", lambda *args, **kwargs: None)
    task = asyncio.create_task(github._gh_cli_token())
    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        await asyncio.wait_for(child.killed.wait(), 0.5)
        assert child.kills == 1
        assert calls[0][1]["start_new_session"] is True
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait({task}, timeout=0.5)


async def test_success_returns_stripped_token_in_separate_session(monkeypatch, caplog):
    child = _Child(response=(b"synthetic-secret-token\n", b""))
    monkeypatch.setenv("LIONAGI_STATE_DB_URL", "synthetic-store")
    calls = _install_child(monkeypatch, child)
    deadlines = _short_exchange(monkeypatch)
    caplog.set_level(logging.WARNING, logger=github.__name__)

    assert await asyncio.wait_for(github._gh_cli_token(), 0.5) == "synthetic-secret-token"
    assert calls[0][0] == ("gh", "auth", "token")
    assert calls[0][1]["start_new_session"] is True
    assert "LIONAGI_STATE_DB_URL" not in calls[0][1]["env"]
    assert child.stdin == b""
    assert 10 in deadlines
    assert child.kills == 0
    assert not _warnings(caplog)
    assert "synthetic-secret-token" not in caplog.text


async def test_nonzero_exit_is_debug_without_copying_child_output(monkeypatch, caplog):
    child = _Child(response=(b"synthetic-secret-token", b"synthetic-secret-token"), returncode=1)
    _install_child(monkeypatch, child)
    _short_exchange(monkeypatch)
    caplog.set_level(logging.DEBUG, logger=github.__name__)

    assert await asyncio.wait_for(github._gh_cli_token(), 0.5) is None
    assert not _warnings(caplog)
    assert [record.levelno for record in caplog.records if record.name == github.__name__] == [
        logging.DEBUG
    ]
    assert "synthetic-secret-token" not in caplog.text
    assert child.kills == 0


async def test_success_with_empty_stdout_returns_none(monkeypatch, caplog):
    child = _Child(response=(b"", b""))
    _install_child(monkeypatch, child)
    caplog.set_level(logging.WARNING, logger=github.__name__)

    assert await github._gh_cli_token() is None
    assert not _warnings(caplog)
    assert child.kills == 0


async def test_missing_gh_returns_none_without_exposing_error(monkeypatch, caplog):
    async def spawn(*args, **kwargs):
        raise FileNotFoundError("synthetic-secret-token")

    monkeypatch.setattr(_proc.asyncio, "create_subprocess_exec", spawn)
    caplog.set_level(logging.DEBUG, logger=github.__name__)

    assert await github._gh_cli_token() is None
    assert not _warnings(caplog)
    assert [record.levelno for record in caplog.records if record.name == github.__name__] == [
        logging.DEBUG
    ]
    assert "synthetic-secret-token" not in caplog.text


async def test_spawn_failure_returns_none(monkeypatch, caplog):
    async def spawn(*args, **kwargs):
        raise OSError("synthetic-secret-token")

    monkeypatch.setattr(_proc.asyncio, "create_subprocess_exec", spawn)
    caplog.set_level(logging.WARNING, logger=github.__name__)
    assert await github._gh_cli_token() is None
    assert "synthetic-secret-token" not in caplog.text
