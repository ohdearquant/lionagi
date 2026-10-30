# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Cancellation cleanup uses fake process, stream, lease and witness seams."""

import asyncio
import os
import socket
import subprocess
import sys

import pytest

from lionagi.providers import _cli_subprocess as cli
from tests.providers.test_cli_observer_bounded_cleanup import (
    fake_observer_boundary as fake_observer_boundary,
)


def _forbid(*_args, **_kwargs):
    raise AssertionError("fake-only test forbids process, signal and network effects")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, _forbid)
    for name in ("kill", "killpg", "getpgid", "system"):
        monkeypatch.setattr(os, name, _forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_cancel", [False, True], ids=["clean", "cleanup-cancel"])
async def test_cancelled_stream_closes_outside_exception_handler_and_owns_helpers(
    fake_observer_boundary, monkeypatch, caplog, cleanup_cancel
):
    process, lease, _seen = fake_observer_boundary
    read_started, stderr_release = asyncio.Event(), asyncio.Event()
    stdout_stopped, stderr_drained = asyncio.Event(), asyncio.Event()
    observed_context = []
    warning_order = []

    class Stdout:
        async def read(self, _size):
            read_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stdout_stopped.set()

    class Stderr:
        count = 0

        async def read(self, _size):
            await stderr_release.wait()
            self.count += 1
            if self.count > 1:
                stderr_drained.set()
            return b"late detail" if self.count == 1 else b""

    async def wait():
        await asyncio.Event().wait()

    async def end(child, *, spawned, witnesses, signal_gate):
        assert child is process and spawned.pid == process.pid
        assert witnesses == ((process.pid, 1.0),)
        assert isinstance(signal_gate, cli.SignalGate)
        observed_context.append(sys.exc_info()[1])
        stderr_release.set()
        if cleanup_cancel:
            raise asyncio.CancelledError("cleanup cancellation")
        return cli._ChildGroupCleanup.CONFIRMED_EMPTY

    process.stdout, process.stderr, process.wait = Stdout(), Stderr(), wait
    monkeypatch.setattr(cli, "end_child_group", end)
    original_warning = cli.log.warning

    def warning(message, *args, **kwargs):
        if message.startswith("CLI subprocess produced no output before it was abandoned"):
            warning_order.append((stderr_drained.is_set(), stdout_stopped.is_set()))
        return original_warning(message, *args, **kwargs)

    monkeypatch.setattr(cli.log, "warning", warning)
    stream = cli.ndjson_from_cli(["fake-cli"], env={})
    task = asyncio.create_task(stream.__anext__())
    try:
        await asyncio.wait_for(read_started.wait(), 1)
        with caplog.at_level("DEBUG", logger=cli.log.name):
            task.cancel("original cancellation")
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 2)
        assert observed_context == [None]
        assert "CLI stream cleanup follows CancelledError" in caplog.text
        assert "its stderr said: late detail" in caplog.text
        assert warning_order == [(True, False)]
        assert stdout_stopped.is_set()
        if sys.version_info >= (3, 11):
            assert caught.value.args == (
                "cleanup cancellation" if cleanup_cancel else "original cancellation",
            )
        assert lease.released == (0 if cleanup_cancel else 1)
    finally:
        stderr_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("prior_group_error", [False, True], ids=["no-prior", "prior-group-error"])
async def test_diagnostic_failure_cannot_skip_helper_cleanup_or_release_gate(
    monkeypatch, caplog, error_type, prior_group_error
):
    started, stopped = asyncio.Event(), asyncio.Event()
    gates = []
    failure = error_type("private diagnostic failure")
    group_failure = ValueError("private group failure")

    async def group(_proc, **kwargs):
        assert kwargs["witnesses"] == ((4242, 1.0),)
        gates.append(kwargs["signal_gate"])
        if prior_group_error:
            raise group_failure
        return cli._ChildGroupCleanup.CONFIRMED_EMPTY

    async def blocked_helper():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def stderr():
        return b""

    def diagnostic(incomplete):
        assert incomplete is False
        assert stderr_task.done() and not helper.done()
        raise failure

    monkeypatch.setattr(cli, "_witnesses_for_spawn", lambda *_: ((4242, 1.0),))
    monkeypatch.setattr(cli, "end_child_group", group)
    proc = type("Process", (), {"pid": 4242})()
    spawned = cli.SpawnedProcess(4242, 4242, 1.0)
    stderr_task = asyncio.create_task(stderr())
    helper = asyncio.create_task(blocked_helper())
    try:
        await asyncio.wait_for(started.wait(), 1)
        with caplog.at_level("DEBUG", logger=cli.log.name):
            result = await cli._bounded_cli_cleanup(
                proc,
                spawned,
                helpers=(("stderr", stderr_task), ("stdout", helper)),
                stderr_task=stderr_task,
                drain_stderr=True,
                after_stderr_drain=diagnostic,
                grace=0.02,
            )
        assert result.error is (group_failure if prior_group_error else failure)
        assert result.cancellation is (failure if error_type is asyncio.CancelledError else None)
        if prior_group_error:
            assert f"CLI abandoned diagnostic failed ({error_type.__name__})" in caplog.text
        assert stopped.is_set() and helper.done()
        signals = []
        assert len(gates) == 1
        gates[0].run(lambda: signals.append(True), skipped=None)
        assert signals == []
        assert "private diagnostic failure" not in caplog.text
        assert "private group failure" not in caplog.text
    finally:
        for task in (stderr_task, helper):
            if not task.done():
                task.cancel()
        await asyncio.gather(stderr_task, helper, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_witness_capture_failure_still_cleans_child_and_owns_helpers(
    monkeypatch, failure_type
):
    failure = failure_type("private witness failure")
    started = [asyncio.Event(), asyncio.Event()]
    cancelled = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    owned, gates, actions = [], [], []
    original_retain = cli._retain_cli_task
    base_gate = cli.SignalGate

    class TrackingGate(base_gate):
        def __init__(self):
            super().__init__()
            gates.append(self)

    class FakeProcess:
        pid = 4242
        returncode = None

        def kill(self):
            actions.append("kill")
            self.returncode = 0

        def terminate(self):
            actions.append("terminate")
            self.returncode = 0

    async def helper(index):
        started[index].set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled[index].set()
            if index:
                await release.wait()

    async def group(child, *, witnesses, signal_gate, **_kwargs):
        assert witnesses == ()
        cli._end_group_with_evidence(child, witnesses, signal_gate=signal_gate)
        return cli._ChildGroupCleanup.UNCONFIRMED

    def capture_failure(*_args):
        raise failure

    def group_signal(_pgid, witnesses, _sig):
        assert witnesses == ()
        return False

    def retain(task, *args, **kwargs):
        owned.append(task)
        original_retain(task, *args, **kwargs)

    monkeypatch.setattr(cli, "SignalGate", TrackingGate)
    monkeypatch.setattr(cli, "_witnesses_for_spawn", capture_failure)
    monkeypatch.setattr(cli, "signal_group_if_witnessed", group_signal)
    monkeypatch.setattr(cli, "end_child_group", group)
    monkeypatch.setattr(cli, "_retain_cli_task", retain)
    proc = FakeProcess()
    spawned = cli.SpawnedProcess(proc.pid, proc.pid, 1.0)
    helpers = [asyncio.create_task(helper(index)) for index in range(2)]
    try:
        await asyncio.gather(*(event.wait() for event in started))
        try:
            result = await asyncio.wait_for(
                cli._bounded_cli_cleanup(
                    proc,
                    spawned,
                    helpers=tuple((str(index), task) for index, task in enumerate(helpers)),
                    grace=0.02,
                ),
                1.5,
            )
        except BaseException as exc:
            assert exc is failure
        else:
            assert result.error is failure
            assert result.cancellation is (
                failure if failure_type is asyncio.CancelledError else None
            )
            assert result.outcome is cli._ChildGroupCleanup.UNCONFIRMED
        assert "kill" in actions or "terminate" in actions
        assert all(event.is_set() for event in cancelled)
        assert helpers[0].done()
        assert helpers[1] in owned and helpers[1] in cli._RETAINED_CLI_TASKS
        assert len(gates) == 1
        late = []
        gates[0].run(lambda: late.append(True), skipped=None)
        assert not late
        release.set()
        await asyncio.gather(*helpers, return_exceptions=True)
        await asyncio.sleep(0)
        assert helpers[1] not in cli._RETAINED_CLI_TASKS
    finally:
        release.set()
        for task in helpers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*helpers, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("group_fails", [False, True], ids=["reported-empty", "secondary-error"])
async def test_witness_failure_stays_unconfirmed(monkeypatch, caplog, group_fails):
    witness_error = RuntimeError("private witness detail")
    group_error = ValueError("private cleanup detail")
    actions = []

    class FakeProcess:
        pid = 4242
        returncode = None

        def kill(self):
            actions.append("kill")

    def witness_failure(*_args):
        raise witness_error

    async def group(child, *, witnesses, signal_gate, **_kwargs):
        assert witnesses == ()
        signal_gate.run(child.kill, skipped=None)
        if group_fails:
            raise group_error
        return cli._ChildGroupCleanup.CONFIRMED_EMPTY

    monkeypatch.setattr(cli, "_witnesses_for_spawn", witness_failure)
    monkeypatch.setattr(cli, "end_child_group", group)
    proc = FakeProcess()
    with caplog.at_level("WARNING", logger=cli.log.name):
        result = await cli._bounded_cli_cleanup(
            proc, cli.SpawnedProcess(proc.pid, proc.pid, 1.0), grace=0.02
        )
    assert result.error is witness_error and result.cancellation is None
    assert result.outcome is cli._ChildGroupCleanup.UNCONFIRMED
    assert actions == ["kill"]
    assert "CLI cleanup witness capture failed (RuntimeError)" in caplog.text
    if group_fails:
        assert "CLI group cleanup failed (ValueError)" in caplog.text
    assert "private witness detail" not in caplog.text
    assert "private cleanup detail" not in caplog.text


@pytest.mark.asyncio
async def test_direct_cancel_wins_after_witness_capture_failure(monkeypatch):
    witness_error = RuntimeError("private witness detail")
    entered, release = asyncio.Event(), asyncio.Event()
    actions = []

    class FakeProcess:
        pid = 4242
        returncode = None

        def kill(self):
            actions.append("kill")

    def witness_failure(*_args):
        raise witness_error

    async def group(child, *, witnesses, signal_gate, **_kwargs):
        assert witnesses == ()
        signal_gate.run(child.kill, skipped=None)
        entered.set()
        await release.wait()
        return cli._ChildGroupCleanup.CONFIRMED_EMPTY

    monkeypatch.setattr(cli, "_witnesses_for_spawn", witness_failure)
    monkeypatch.setattr(cli, "end_child_group", group)
    proc = FakeProcess()
    task = asyncio.create_task(
        cli._bounded_cli_cleanup(proc, cli.SpawnedProcess(proc.pid, proc.pid, 1.0), grace=1)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel("direct cancellation")
        await asyncio.sleep(0)
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result.error is witness_error
        assert result.outcome is cli._ChildGroupCleanup.UNCONFIRMED
        with pytest.raises(asyncio.CancelledError) as caught:
            cli._raise_bounded_cleanup_result(
                result, None, RuntimeError("primary"), result.outcome, None, label="test"
            )
        assert caught.value.args == ("direct cancellation",)
        assert actions == ["kill"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic_type", [RuntimeError, asyncio.CancelledError])
async def test_fallback_diagnostic_failure_is_logged_after_cleanup_error(
    fake_observer_boundary, monkeypatch, caplog, diagnostic_type
):
    process, lease, _seen = fake_observer_boundary
    primary = ValueError("private stdout failure")
    cleanup_error = OSError("private cleanup failure")
    diagnostic_error = diagnostic_type("private diagnostic failure")
    original_cleanup = cli._bounded_cli_cleanup
    original_warning = cli.log.warning

    class Stdout:
        async def read(self, _size):
            raise primary

    async def wait():
        await asyncio.Event().wait()

    async def fail_after_cleanup(*args, **kwargs):
        # Force the outer fallback after doing the real fake-only helper cleanup.
        kwargs["after_stderr_drain"] = None
        await original_cleanup(*args, **kwargs)
        raise cleanup_error

    def warning(message, *args, **kwargs):
        if message.startswith("CLI subprocess produced no output before it was abandoned"):
            raise diagnostic_error
        return original_warning(message, *args, **kwargs)

    process.stdout, process.wait = Stdout(), wait
    monkeypatch.setattr(cli, "_bounded_cli_cleanup", fail_after_cleanup)
    monkeypatch.setattr(cli.log, "warning", warning)
    stream = cli.ndjson_from_cli(["fake-cli"], env={})
    try:
        with caplog.at_level("WARNING", logger=cli.log.name):
            expected = (
                asyncio.CancelledError if diagnostic_type is asyncio.CancelledError else ValueError
            )
            with pytest.raises(expected) as caught:
                await stream.__anext__()
        assert caught.value is (
            diagnostic_error if diagnostic_type is asyncio.CancelledError else primary
        )
        assert f"CLI abandoned diagnostic failed ({diagnostic_type.__name__})" in caplog.text
        assert "CLI child group teardown raised OSError" in caplog.text
        assert "private diagnostic failure" not in caplog.text
        assert "private cleanup failure" not in caplog.text
        assert lease.released == 0
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_direct_cancel_after_cleanup_cancel_during_group_wait_keeps_helpers_owned(
    fake_observer_boundary, monkeypatch
):
    """A second caller cancellation must survive an earlier cleanup cancellation."""
    process, _lease, _seen = fake_observer_boundary
    read_started, read_stopped = asyncio.Event(), asyncio.Event()
    wait_started, wait_stopped = asyncio.Event(), asyncio.Event()
    group_started, group_stopped = asyncio.Event(), asyncio.Event()
    release_group = asyncio.Event()
    backstops = []

    class Stdout:
        async def read(self, _size):
            read_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                read_stopped.set()

    async def wait():
        wait_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            wait_stopped.set()

    def witness_failure(*_args):
        raise asyncio.CancelledError()

    async def group(child, *, witnesses, signal_gate, **_kwargs):
        assert child is process and witnesses == ()
        assert isinstance(signal_gate, cli.SignalGate)
        group_started.set()
        try:
            await release_group.wait()
        finally:
            group_stopped.set()
        return cli._ChildGroupCleanup.UNCONFIRMED

    def backstop(child, witnesses, *, signal_gate):
        assert child is process and witnesses == ()
        signal_gate.run(lambda: backstops.append(True), skipped=None)
        return "unproven"

    process.stdout = Stdout()
    process.wait = wait
    monkeypatch.setattr(cli, "_witnesses_for_spawn", witness_failure)
    monkeypatch.setattr(cli, "end_child_group", group)
    monkeypatch.setattr(cli, "_end_group_with_evidence", backstop)
    stream = cli.ndjson_from_cli(["fake-cli"], env={})
    caller = asyncio.create_task(stream.__anext__())
    try:
        await asyncio.wait_for(asyncio.gather(read_started.wait(), wait_started.wait()), 1)
        caller.cancel()
        await asyncio.wait_for(group_started.wait(), 1)
        before = caller.cancelling() if hasattr(caller, "cancelling") else None
        caller.cancel()
        after = caller.cancelling() if hasattr(caller, "cancelling") else None
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 2)
        if before is not None:
            assert after >= before + 1
            assert caller.cancelling() >= after
        await asyncio.wait_for(
            asyncio.gather(group_stopped.wait(), read_stopped.wait(), wait_stopped.wait()), 1
        )
        assert backstops == [True]
    finally:
        release_group.set()
        if not caller.done():
            caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        await stream.aclose()
