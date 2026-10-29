"""Event-barrier probes for observer failure and cancellation.

No real subprocess, signal, socket or process-group operation is permitted.
The tests exercise the real observer boundary with fake cleanup and lease seams.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace

import anyio
import pytest

from lionagi.providers import _cli_subprocess as cli


def _context_contains(error: BaseException, target: BaseException) -> bool:
    """Follow task cancellation wrappers without looping on cyclic contexts."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if current is target:
            return True
        seen.add(id(current))
        current = current.__context__
    return False


class FakeProcess:
    pid = 42424242
    returncode = None
    stdout = None
    stderr = None
    stdin = None

    def kill(self):
        pytest.fail("fake process kill must not be called")

    def terminate(self):
        pytest.fail("fake process terminate must not be called")


class FakeLease:
    slots = (SimpleNamespace(pool="fake"),)

    def __init__(self):
        self.released = 0
        self.recorded = []

    def record_child(self, pid, create_time):
        self.recorded.append((pid, create_time))

    def release(self):
        self.released += 1


def _forbid(*_args, **_kwargs):
    pytest.fail("real process, signal, or socket operation attempted")


@pytest.fixture
def fake_observer_boundary(monkeypatch):
    process = FakeProcess()
    lease = FakeLease()
    seen = {"spawned": 0, "cleanup_started": 0, "cleanup_completed": 0, "interrupted": None}

    async def fake_spawn(*_args, **_kwargs):
        seen["spawned"] += 1
        return process

    async def fake_admit(**_kwargs):
        return lease

    async def fake_secrets(env):
        return env, set()

    async def fake_end(child, *, spawned=None, **_kwargs):
        assert child is process
        assert spawned is not None and spawned.pid == process.pid
        seen["cleanup_started"] += 1
        try:
            await anyio.sleep(0)
        except BaseException as exc:
            seen["interrupted"] = type(exc).__name__
            raise
        seen["cleanup_completed"] += 1
        return cli._ChildGroupCleanup.CONFIRMED_EMPTY

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _forbid)
    monkeypatch.setattr(subprocess, "Popen", _forbid)
    monkeypatch.setattr(os, "kill", _forbid)
    if hasattr(os, "killpg"):
        monkeypatch.setattr(os, "killpg", _forbid)
    if hasattr(os, "getpgid"):
        monkeypatch.setattr(os, "getpgid", _forbid)
    if hasattr(signal, "pthread_kill"):
        monkeypatch.setattr(signal, "pthread_kill", _forbid)
    if hasattr(signal, "raise_signal"):
        monkeypatch.setattr(signal, "raise_signal", _forbid)
    monkeypatch.setattr(socket.socket, "connect", _forbid)
    monkeypatch.setattr(cli, "refuse_nested_agent", lambda _engine: None)
    monkeypatch.setattr(cli, "fill_declared_secrets_and_names", fake_secrets)
    monkeypatch.setattr(cli, "prepare_process_group_env", lambda _env: ({}, "fake-marker"))
    monkeypatch.setattr(cli, "child_agent_depth", lambda: 0)
    monkeypatch.setattr(cli, "admit_subagent", fake_admit)
    monkeypatch.setattr(cli, "_require_cli_executable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_get_cli_worker_recorder", lambda: None)
    monkeypatch.setattr(
        cli,
        "observe_spawned",
        lambda pid, **_kwargs: cli.SpawnedProcess(pid, pid, 1.0, "fake-marker"),
    )
    monkeypatch.setattr(cli, "end_child_group", fake_end)
    monkeypatch.setattr(cli, "_witnesses_for_spawn", lambda *_: ((process.pid, 1.0),))
    return process, lease, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_kind", ["run_observer", "on_spawn"])
async def test_direct_cancel_during_observer_failure_cleanup_wins(
    fake_observer_boundary, monkeypatch, observer_kind
):
    process, lease, seen = fake_observer_boundary
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    primary = RuntimeError("observer sentinel")

    async def fake_end(child, *, spawned=None, **_kwargs):
        assert child is process and spawned.pid == process.pid
        seen["cleanup_started"] += 1
        entered.set()
        await release.wait()
        seen["cleanup_completed"] += 1
        completed.set()
        return cli._ChildGroupCleanup.CONFIRMED_EMPTY

    async def observer(*_args):
        raise primary

    monkeypatch.setattr(cli, "end_child_group", fake_end)

    async def consume():
        with cli._observe_run_cli_spawns(observer if observer_kind == "run_observer" else None):
            stream = cli.ndjson_from_cli(
                ["fake-cli"], env={}, on_spawn=observer if observer_kind == "on_spawn" else None
            )
            return await stream.__anext__()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert seen["spawned"] == 1 and seen["cleanup_started"] == 1
        assert lease.recorded == [(process.pid, 1.0)]
        # A release may occur only after the fake confirmed-empty completion.
        if lease.released:
            assert completed.is_set() and lease.released == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_kind", ["run_observer", "on_spawn"])
async def test_inherited_cancelled_scope_allows_observer_error_cleanup(
    fake_observer_boundary, monkeypatch, observer_kind
):
    process, lease, seen = fake_observer_boundary
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    primary = RuntimeError("observer sentinel")

    async def fake_end(child, *, spawned=None, **_kwargs):
        assert child is process and spawned.pid == process.pid
        seen["cleanup_started"] += 1
        entered.set()
        await release.wait()
        seen["cleanup_completed"] += 1
        completed.set()
        return cli._ChildGroupCleanup.CONFIRMED_EMPTY

    monkeypatch.setattr(cli, "end_child_group", fake_end)

    async def consume():
        with anyio.CancelScope() as scope:

            async def observer(*_args):
                scope.cancel()
                raise primary

            with cli._observe_run_cli_spawns(observer if observer_kind == "run_observer" else None):
                stream = cli.ndjson_from_cli(
                    ["fake-cli"], env={}, on_spawn=observer if observer_kind == "on_spawn" else None
                )
                return await stream.__anext__()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        release.set()
        with pytest.raises(RuntimeError) as caught:
            await asyncio.wait_for(task, 2)
        assert caught.value is primary
        assert seen["spawned"] == 1 and seen["cleanup_started"] == 1
        assert completed.is_set() and seen["cleanup_completed"] == 1
        assert lease.recorded == [(process.pid, 1.0)] and lease.released == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_resistant_nested_process_wait_retains_actual_exit_helper(
    fake_observer_boundary, monkeypatch
):
    process, lease, _seen = fake_observer_boundary
    read_started, wait_started, wait_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    retained, observed, loop_errors = [], [], []
    retain = cli._retain_cli_task
    forget = cli._forget_cli_task
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    class Stdout:
        count = 0

        async def read(self, _size):
            self.count += 1
            if self.count == 1:
                return b'{"partial":true}\n'
            read_started.set()
            await asyncio.Event().wait()

    async def wait():
        wait_started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            wait_cancelled.set()
            await release.wait()
        raise RuntimeError("controlled late wait failure")

    async def group(_proc, **_kwargs):
        return cli._ChildGroupCleanup.CONFIRMED_EMPTY

    def record(task, *args, **kwargs):
        retained.append(task)
        retain(task, *args, **kwargs)

    def consume(task):
        observed.append(task)
        forget(task)

    process.stdout = Stdout()
    process.wait = wait
    monkeypatch.setattr(cli, "end_child_group", group)
    monkeypatch.setattr(cli, "_retain_cli_task", record)
    monkeypatch.setattr(cli, "_forget_cli_task", consume)
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    stream = cli.ndjson_from_cli(["fake-cli"], env={})
    consuming = None
    try:
        assert await stream.__anext__() == {"partial": True}
        consuming = asyncio.create_task(stream.__anext__())
        await asyncio.wait_for(asyncio.gather(read_started.wait(), wait_started.wait()), 1)
        consuming.cancel("direct")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consuming, 2)
        assert wait_cancelled.is_set()
        stuck = [task for task in retained if not task.done()]
        assert len(stuck) == 1 and stuck[0] in cli._RETAINED_CLI_TASKS
        assert lease.released == 1
        release.set()
        await asyncio.gather(*stuck, return_exceptions=True)
        await asyncio.sleep(0)
        assert retained.count(stuck[0]) == 1 and observed.count(stuck[0]) == 1
        assert stuck[0] not in cli._RETAINED_CLI_TASKS
        assert not loop_errors
    finally:
        release.set()
        if consuming is not None and not consuming.done():
            consuming.cancel()
        if consuming is not None:
            await asyncio.gather(consuming, return_exceptions=True)
        await asyncio.gather(*retained, return_exceptions=True)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_kind", ["run_observer", "on_spawn"])
async def test_cleanup_task_cancellation_wins_over_observer_failure(
    fake_observer_boundary, monkeypatch, observer_kind
):
    process, lease, seen = fake_observer_boundary
    primary = asyncio.CancelledError("cleanup cancellation")

    async def fake_end(child, *, spawned=None, **_kwargs):
        assert child is process and spawned.pid == process.pid
        seen["cleanup_started"] += 1
        raise primary

    async def observer(*_args):
        raise RuntimeError("observer sentinel")

    monkeypatch.setattr(cli, "end_child_group", fake_end)
    with cli._observe_run_cli_spawns(observer if observer_kind == "run_observer" else None):
        stream = cli.ndjson_from_cli(
            ["fake-cli"], env={}, on_spawn=observer if observer_kind == "on_spawn" else None
        )
        with pytest.raises(asyncio.CancelledError) as caught:
            await stream.__anext__()
    if sys.version_info < (3, 11):
        # bpo-45390: task cancellation wraps the original error on Python 3.10.
        assert _context_contains(caught.value, primary)
    else:
        assert caught.value.args == ("cleanup cancellation",)
    assert seen["cleanup_started"] == 1
    assert lease.recorded == [(process.pid, 1.0)]
    assert lease.released == 0


@pytest.mark.asyncio
async def test_synchronous_cleanup_cancellation_wins_over_observer_failure(
    fake_observer_boundary, monkeypatch
):
    process, lease, _seen = fake_observer_boundary

    async def observer(*_args):
        raise RuntimeError("observer sentinel")

    def reject_cleanup_task(_coro):
        raise RuntimeError("task factory sentinel")

    def cancelled_backstop(_proc, _witnesses, *, signal_gate):
        raise asyncio.CancelledError("backstop cancellation")

    # Reject only cleanup task creation: the spawn task is already in flight
    # before the observer is invoked. All process and signalling calls are fake.
    async def install_failure(*_args):
        monkeypatch.setattr(asyncio, "create_task", reject_cleanup_task)
        await observer()

    monkeypatch.setattr(cli, "_end_group_with_evidence", cancelled_backstop)
    with cli._observe_run_cli_spawns(install_failure):
        stream = cli.ndjson_from_cli(["fake-cli"], env={})
        with pytest.raises(asyncio.CancelledError) as caught:
            await stream.__anext__()
    assert caught.value.args == ("backstop cancellation",)
    assert lease.recorded == [(process.pid, 1.0)]
    assert lease.released == 0


@pytest.mark.asyncio
async def test_early_close_reports_unconfirmed_admitted_group_cleanup(
    fake_observer_boundary, monkeypatch
):
    """Closing a recorded stream does not hide an unconfirmed group behind GeneratorExit."""
    process, lease, _seen = fake_observer_boundary
    records = []

    class Admission:
        async def record(self, identity):
            records.append(identity)

        def retain(self, _task):
            pass

        def finish(self):
            pass

    admission = Admission()
    monkeypatch.setattr(
        cli, "_get_cli_worker_recorder", lambda: SimpleNamespace(admit=lambda: admission)
    )
    monkeypatch.setattr(cli, "_worker_spawn_identity", lambda _spawned: {"pid": process.pid})

    class Stdout:
        async def read(self, _size):
            return b'{"partial":true}\n'

    async def wait():
        await asyncio.Event().wait()

    async def unconfirmed(_proc, **_kwargs):
        return cli._ChildGroupCleanup.UNCONFIRMED

    process.stdout = Stdout()
    process.wait = wait
    monkeypatch.setattr(cli, "end_child_group", unconfirmed)
    stream = cli.ndjson_from_cli(["fake-cli"], env={})
    try:
        assert await stream.__anext__() == {"partial": True}
        with pytest.raises(cli.ProviderTeardownError, match="cleanup remains unconfirmed"):
            await stream.aclose()
        assert records == [{"pid": process.pid}]
        assert lease.released == 0
    finally:
        await stream.aclose()
