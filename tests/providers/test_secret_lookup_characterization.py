# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Characterize the two secret lookup contracts with fake child processes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from lionagi.service.accounts import _secret_resolution as lookup_module

pytestmark = pytest.mark.asyncio

_NAME = "LOOKUP_TEST_KEY"
_LOOKUP = lookup_module.ResolvedSecretLookup(
    argv=("/fake/secret-reader", "--name={name}", "{name}"), names=(_NAME,)
)


@pytest.fixture
def fake_child(monkeypatch):
    """Install a fake process, preserving the production environment filtering."""
    stdout = SimpleNamespace(read=AsyncMock(return_value=b""))
    proc = SimpleNamespace(
        pid=5151,
        returncode=0,
        stdout=stdout,
        communicate=AsyncMock(return_value=(b"", b"")),
        wait=AsyncMock(return_value=-9),
    )
    spawn = AsyncMock(return_value=proc)
    stop = AsyncMock()
    capture = Mock(return_value=((5151, 17.0),))
    observe = Mock(return_value=17.0)
    monkeypatch.setattr(lookup_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        lookup_module,
        "prepare_process_group_env",
        lambda env: ({**env, "GROUP_MARKER": "witness"}, "witness"),
    )
    monkeypatch.setattr(lookup_module, "observe_process_group_leader", observe)
    monkeypatch.setattr(lookup_module, "capture_group_witnesses", capture)
    monkeypatch.setattr(lookup_module, "aterminate_process_group", stop)
    return SimpleNamespace(proc=proc, spawn=spawn, stop=stop, capture=capture, observe=observe)


def _assert_spawn(fake, *, keep):
    args, kwargs = fake.spawn.await_args
    assert args == ("/fake/secret-reader", f"--name={_NAME}", _NAME)
    assert kwargs["stdin"] is asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["KEEP_FOR_LOOKUP"] == keep
    assert kwargs["env"]["GROUP_MARKER"] == "witness"
    assert all(key.upper() != "LIONAGI_STATE_DB_URL" for key in kwargs["env"])
    fake.observe.assert_called_once_with(5151, expected_marker="witness")


def _assert_stopped(fake):
    fake.capture.assert_called_once_with(fake.proc, expected_leader_created=17.0)
    fake.stop.assert_awaited_once_with(fake.proc, grace=None, group_witnesses=((5151, 17.0),))
    fake.proc.wait.assert_awaited_once()


@pytest.mark.parametrize("outcome", ["success", "nonzero", "timeout", "run-error"])
async def test_cli_fill_results_environment_and_cleanup(fake_child, monkeypatch, caplog, outcome):
    monkeypatch.setenv("KEEP_FOR_LOOKUP", "ambient")
    monkeypatch.setenv("lionagi_state_db_url", "must-not-reach-child")
    proc = fake_child.proc
    proc.communicate.return_value = (b"  value-\xff \n", b"")
    if outcome == "nonzero":
        proc.returncode = 7
    elif outcome == "timeout":
        proc.communicate.side_effect = asyncio.TimeoutError()
    elif outcome == "run-error":
        proc.communicate.side_effect = OSError("lookup-output-secret")

    result = await lookup_module._run_lookup(_LOOKUP, _NAME)

    assert result == ("value-\ufffd" if outcome == "success" else None)
    _assert_spawn(fake_child, keep="ambient")
    if outcome == "timeout":
        _assert_stopped(fake_child)
        assert "timed out" in caplog.text
    else:
        fake_child.stop.assert_not_awaited()
    assert "value-" not in caplog.text
    assert "lookup-output-secret" not in caplog.text


@pytest.mark.parametrize("outcome", ["success", "nonzero", "timeout", "too-long", "read-error"])
async def test_one_name_results_environment_and_cleanup(fake_child, caplog, outcome):
    proc = fake_child.proc
    original_env = {"KEEP_FOR_LOOKUP": "explicit", "lionagi_state_db_url": "excluded"}
    if outcome == "success":
        proc.stdout.read.side_effect = [b" x\xff ", b""]
    elif outcome == "nonzero":
        proc.returncode = 3
        proc.stdout.read.side_effect = [b" x\xff ", b""]
    elif outcome == "timeout":
        proc.stdout.read.side_effect = [asyncio.TimeoutError(), b""]
    elif outcome == "too-long":
        proc.stdout.read.side_effect = [b"12345", b""]
    else:
        proc.returncode = None
        proc.stdout.read.side_effect = [OSError("lookup-output-secret"), b""]

    result = await lookup_module.lookup_one_name(_LOOKUP, _NAME, env=original_env, max_bytes=4)

    if outcome == "success":
        assert (result.value, result.failure) == (b" x\xff ", None)
    else:
        failure = {
            "nonzero": "exit_nonzero",
            "timeout": "timeout",
            "too-long": "too_long",
            "read-error": "failed_to_run",
        }[outcome]
        assert (result.value, result.failure) == (None, failure)
    _assert_spawn(fake_child, keep="explicit")
    assert original_env == {"KEEP_FOR_LOOKUP": "explicit", "lionagi_state_db_url": "excluded"}
    if outcome in {"timeout", "too-long", "read-error"}:
        _assert_stopped(fake_child)
    else:
        fake_child.stop.assert_not_awaited()
        proc.wait.assert_awaited_once()
    assert "lookup-output-secret" not in caplog.text


async def test_cli_fill_cancel_stops_child_and_propagates(fake_child):
    started = asyncio.Event()
    never = asyncio.Event()

    async def communicate():
        started.set()
        await never.wait()

    fake_child.proc.communicate.side_effect = communicate
    task = asyncio.create_task(lookup_module._run_lookup(_LOOKUP, _NAME))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_stopped(fake_child)


async def test_strict_cancel_clears_partial_reader_frame_and_stops_child(fake_child):
    proc = fake_child.proc
    reading = asyncio.Event()
    never = asyncio.Event()
    reads = 0

    async def read(_size):
        nonlocal reads
        reads += 1
        if reads == 1:
            return b"partial-secret"
        if reads == 2:
            reading.set()
            await never.wait()
        return b""

    proc.stdout.read = AsyncMock(side_effect=read)
    task = asyncio.create_task(
        lookup_module.lookup_one_name(
            _LOOKUP, _NAME, env={"KEEP_FOR_LOOKUP": "explicit"}, max_bytes=64
        )
    )
    await reading.wait()
    frames = []
    for active in asyncio.all_tasks():
        coro = active.get_coro()
        while coro is not None:
            frame = getattr(coro, "cr_frame", None)
            if frame is not None and frame.f_code.co_name == "read_bounded":
                frames.append(frame)
            coro = getattr(coro, "cr_await", None) or getattr(coro, "gi_yieldfrom", None)
    assert len(frames) == 1 and frames[0].f_locals["out"] == b"partial-secret"
    reader_frame = frames[0]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert reader_frame.f_locals["out"] == bytearray()
    assert "chunk" not in reader_frame.f_locals
    _assert_stopped(fake_child)
