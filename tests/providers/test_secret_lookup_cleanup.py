# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Bounded fake-child cleanup for declared-secret lookups."""

from __future__ import annotations

import asyncio
import logging
import sys

import anyio
import pytest

from lionagi.providers import _secret_resolution as secret_lookup

pytestmark = pytest.mark.asyncio

_SECRET = "secret-lookup-output-never-log-this"
_NAME = "LOOKUP_TOKEN"
_LOOKUP = secret_lookup.ResolvedSecretLookup(("lookup", "{name}"), (_NAME,))


class _Pipe:
    def __init__(self, *, closed=False):
        self.closed = closed
        self.close_calls = 0

    def is_closing(self):
        return self.closed

    def close(self):
        self.close_calls += 1
        self.closed = True


class _Transport:
    def __init__(self, pipe):
        self.pipe = pipe

    def get_pipe_transport(self, fd):
        assert fd == 1
        return self.pipe


class _Reader:
    def __init__(self, proc, *, stall, overflow):
        self.proc = proc
        self.stall = stall
        self.overflow = overflow
        self.before_kill_reads = 0
        self.drain_started = asyncio.Event()
        self.drain_finished = asyncio.Event()
        self.drain_tasks = []

    async def read(self, size):
        if not self.proc.killed.is_set():
            self.before_kill_reads += 1
            self.proc.active_started.set()
            if self.overflow:
                return _SECRET.encode()[:size]
            await asyncio.Event().wait()
            raise AssertionError("the initial read must be cancelled")

        self.proc.events.append("drain")
        self.drain_tasks.append(asyncio.current_task())
        self.drain_started.set()
        try:
            if self.proc.fail_on == "drain":
                await self.proc.wait_started.wait()
                if self.proc.hold_failure:
                    await self.proc.failure_release.wait()
                raise self.proc.failure
            if self.proc.fail_on == "drain-after-wait":
                await self.proc.wait_finished.wait()
                raise self.proc.failure
            if self.proc.rendezvous:
                await self.proc.wait_started.wait()
            if self.stall:
                await asyncio.Event().wait()
            return b""
        finally:
            self.drain_finished.set()


class _Child:
    def __init__(
        self,
        *,
        stall_read,
        stall_wait,
        overflow=False,
        pipe_kind="open",
        rendezvous=False,
        fail_on=None,
        hold_failure=False,
    ):
        self.pid = None
        self.returncode = None
        self.killed = asyncio.Event()
        self.active_started = asyncio.Event()
        self.events = []
        self.stdout = _Reader(self, stall=stall_read, overflow=overflow)
        self.stall_wait = stall_wait
        self.rendezvous = rendezvous
        self.fail_on = fail_on
        self.hold_failure = hold_failure
        self.failure_release = asyncio.Event()
        self.failure = OSError(_SECRET)
        self.wait_started = asyncio.Event()
        self.wait_finished = asyncio.Event()
        self.wait_tasks = []
        self.pipe = _Pipe(closed=pipe_kind == "closed")
        if pipe_kind != "missing-transport":
            self._transport = _Transport(None if pipe_kind == "missing-pipe" else self.pipe)

    async def communicate(self):
        self.active_started.set()
        await asyncio.Event().wait()
        raise AssertionError("the initial communicate must be cancelled")

    async def wait(self):
        assert self.killed.is_set(), "the child must be killed before waiting for cleanup"
        self.events.append("wait")
        self.wait_tasks.append(asyncio.current_task())
        self.wait_started.set()
        try:
            if self.fail_on == "wait":
                await self.stdout.drain_started.wait()
                if self.hold_failure:
                    await self.failure_release.wait()
                raise self.failure
            if self.rendezvous:
                await self.stdout.drain_started.wait()
            if self.stall_wait:
                await asyncio.Event().wait()
            return -9
        finally:
            self.wait_finished.set()


@pytest.fixture
def install_fake(monkeypatch):
    monkeypatch.setattr(secret_lookup, "_LOOKUP_TIMEOUT_SECONDS", 0.01)
    # The constant is absent on the old implementation, so these tests still
    # exercise its observable unbounded wait when run as a red baseline.
    monkeypatch.setattr(secret_lookup, "_REAP_TIMEOUT_SECONDS", 0.03, raising=False)
    monkeypatch.setattr(secret_lookup, "observe_process_group_leader", lambda *a, **k: None)
    monkeypatch.setattr(
        secret_lookup, "prepare_process_group_env", lambda env=None: ({"VISIBLE": "yes"}, "mark")
    )

    def install(proc):
        async def spawn(*args, **kwargs):
            return proc

        async def terminate(child, **kwargs):
            assert child is proc
            assert kwargs["grace"] is None
            proc.events.append("kill")
            proc.killed.set()

        monkeypatch.setattr(secret_lookup.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(secret_lookup, "aterminate_process_group", terminate)
        return proc

    return install


async def _lookup(path, proc):
    if path == "ordinary":
        return await secret_lookup._run_lookup(_LOOKUP, _NAME)
    return await secret_lookup.lookup_one_name(_LOOKUP, _NAME, env={"VISIBLE": "yes"}, max_bytes=4)


def _assert_finished(proc):
    assert proc.events[0] == "kill"
    assert proc.wait_tasks
    assert proc.stdout.drain_tasks
    assert all(task.done() for task in proc.wait_tasks)
    assert all(task.done() for task in proc.stdout.drain_tasks)
    assert proc.wait_finished.is_set()
    assert proc.stdout.drain_finished.is_set()


async def test_cleanup_reap_budget_is_two_seconds():
    assert secret_lookup._REAP_TIMEOUT_SECONDS == 2.0


@pytest.mark.parametrize(
    ("path", "overflow", "stall_read", "stall_wait", "failure"),
    [
        ("ordinary", False, False, True, None),
        ("ordinary", False, True, False, None),
        ("strict", False, False, True, "timeout"),
        ("strict", False, True, False, "timeout"),
        ("strict", True, False, True, "too_long"),
        ("strict", True, True, False, "too_long"),
        ("strict", True, True, True, "too_long"),
    ],
)
async def test_cleanup_bounds_wait_and_drain_and_closes_pipe(
    install_fake, caplog, path, overflow, stall_read, stall_wait, failure
):
    proc = install_fake(_Child(stall_read=stall_read, stall_wait=stall_wait, overflow=overflow))
    caplog.set_level(logging.DEBUG, logger=secret_lookup.__name__)

    result = await asyncio.wait_for(_lookup(path, proc), timeout=0.75)

    if path == "ordinary":
        assert result is None
    else:
        assert result.value is None
        assert result.failure == failure
    _assert_finished(proc)
    assert proc.pipe.close_calls == 1
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert _SECRET not in caplog.text
    assert "VISIBLE" not in caplog.text


@pytest.mark.parametrize("pipe_kind", ["missing-transport", "missing-pipe", "closed"])
async def test_missing_or_closed_stdout_transport_is_safe(install_fake, caplog, pipe_kind):
    proc = install_fake(
        _Child(stall_read=True, stall_wait=False, overflow=True, pipe_kind=pipe_kind)
    )
    caplog.set_level(logging.DEBUG, logger=secret_lookup.__name__)

    result = await asyncio.wait_for(_lookup("strict", proc), timeout=0.75)

    assert (result.value, result.failure) == (None, "too_long")
    _assert_finished(proc)
    assert proc.pipe.close_calls == 0
    assert any(record.levelno == logging.DEBUG for record in caplog.records)
    assert _SECRET not in caplog.text


@pytest.mark.parametrize("path", ["ordinary", "strict"])
async def test_cleanup_that_finishes_does_not_close_stdout(install_fake, path):
    proc = install_fake(_Child(stall_read=False, stall_wait=False, overflow=path == "strict"))

    result = await asyncio.wait_for(_lookup(path, proc), timeout=0.75)

    if path == "ordinary":
        assert result is None
    else:
        assert (result.value, result.failure) == (None, "too_long")
    _assert_finished(proc)
    assert proc.pipe.close_calls == 0


@pytest.mark.parametrize("path", ["ordinary", "strict"])
async def test_wait_and_drain_run_concurrently_without_cleanup_timeout(install_fake, caplog, path):
    proc = install_fake(
        _Child(
            stall_read=False,
            stall_wait=False,
            overflow=path == "strict",
            rendezvous=True,
        )
    )
    caplog.set_level(logging.WARNING, logger=secret_lookup.__name__)

    result = await asyncio.wait_for(_lookup(path, proc), timeout=0.75)

    if path == "ordinary":
        assert result is None
        assert [record.getMessage() for record in caplog.records] == [
            "secret lookup lookup for LOOKUP_TOKEN timed out"
        ]
    else:
        assert (result.value, result.failure) == (None, "too_long")
        assert not caplog.records
    _assert_finished(proc)
    assert proc.pipe.close_calls == 0


@pytest.mark.parametrize("path", ["ordinary", "strict"])
async def test_cancellation_during_lookup_kills_child_and_propagates(install_fake, path):
    proc = install_fake(_Child(stall_read=False, stall_wait=False))
    task = asyncio.create_task(_lookup(path, proc))
    await asyncio.wait_for(proc.active_started.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.75)
    _assert_finished(proc)
    assert proc.pipe.close_calls == 0


@pytest.mark.parametrize("path", ["ordinary", "strict"])
async def test_first_cancellation_survives_another_cancel_during_cleanup(
    install_fake, monkeypatch, path
):
    monkeypatch.setattr(secret_lookup, "_LOOKUP_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(secret_lookup, "_REAP_TIMEOUT_SECONDS", 0.2)
    proc = install_fake(_Child(stall_read=True, stall_wait=True))
    existing_tasks = asyncio.all_tasks()
    entered, returned = [], []
    real_await_cleanup = secret_lookup._await_cleanup

    async def capture_cleanup(operation):
        entered.append(sys.exc_info()[1])
        await real_await_cleanup(operation)

    async def capture_cancellation():
        try:
            await _lookup(path, proc)
        except asyncio.CancelledError as exc:
            returned.append(exc)
            raise

    monkeypatch.setattr(secret_lookup, "_await_cleanup", capture_cleanup)
    task = asyncio.create_task(capture_cancellation())
    await asyncio.wait_for(proc.active_started.wait(), timeout=0.5)
    task.cancel("first")
    await asyncio.wait_for(proc.killed.wait(), timeout=0.5)
    await asyncio.wait_for(proc.stdout.drain_started.wait(), timeout=0.5)
    await asyncio.wait_for(proc.wait_started.wait(), timeout=0.5)
    task.cancel("second")

    done, _ = await asyncio.wait({task}, timeout=1.5)
    assert task in done
    with pytest.raises(asyncio.CancelledError):
        task.result()

    # Python 3.10's wait_for may strip the first task.cancel() message before
    # lookup catches it; preserve the actual cancellation it received.
    assert len(entered) == len(returned) == 1
    assert isinstance(entered[0], asyncio.CancelledError)
    assert returned[0] is entered[0]
    _assert_finished(proc)
    assert proc.pipe.close_calls == 1
    assert not {
        pending
        for pending in asyncio.all_tasks()
        if pending not in existing_tasks and not pending.done()
    }


@pytest.mark.parametrize("path", ["ordinary", "strict"])
@pytest.mark.parametrize("fail_on", ["drain", "wait"])
async def test_active_cancellation_survives_cleanup_failure_without_leaking_tasks(
    install_fake, monkeypatch, caplog, path, fail_on
):
    monkeypatch.setattr(secret_lookup, "_REAP_TIMEOUT_SECONDS", 0.5)
    proc = install_fake(
        _Child(stall_read=fail_on == "wait", stall_wait=fail_on == "drain", fail_on=fail_on)
    )
    caplog.set_level(logging.DEBUG, logger=secret_lookup.__name__)
    existing_tasks = asyncio.all_tasks()
    task = asyncio.create_task(_lookup(path, proc))
    await asyncio.wait_for(proc.active_started.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.5)

    _assert_finished(proc)
    assert proc.pipe.close_calls == 1
    assert _SECRET not in caplog.text
    assert not {
        task for task in asyncio.all_tasks() if task not in existing_tasks and not task.done()
    }


@pytest.mark.parametrize("path", ["ordinary", "strict"])
@pytest.mark.parametrize("fail_on", ["drain", "wait"])
async def test_cancellation_during_cleanup_survives_later_cleanup_failure(
    install_fake, monkeypatch, caplog, path, fail_on
):
    monkeypatch.setattr(secret_lookup, "_REAP_TIMEOUT_SECONDS", 0.5)
    proc = install_fake(
        _Child(
            stall_read=fail_on == "wait",
            stall_wait=fail_on == "drain",
            overflow=path == "strict",
            fail_on=fail_on,
            hold_failure=True,
        )
    )
    caplog.set_level(logging.DEBUG, logger=secret_lookup.__name__)
    existing_tasks = asyncio.all_tasks()
    task = asyncio.create_task(_lookup(path, proc))
    await asyncio.wait_for(proc.killed.wait(), timeout=0.5)
    await asyncio.wait_for(proc.stdout.drain_started.wait(), timeout=0.5)
    await asyncio.wait_for(proc.wait_started.wait(), timeout=0.5)
    task.cancel()
    await asyncio.sleep(0)
    proc.failure_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.5)

    _assert_finished(proc)
    assert proc.pipe.close_calls == 1
    assert _SECRET not in caplog.text
    assert not {
        pending
        for pending in asyncio.all_tasks()
        if pending not in existing_tasks and not pending.done()
    }


@pytest.mark.parametrize("path", ["ordinary", "strict"])
@pytest.mark.parametrize("fail_on", ["drain", "wait"])
async def test_cleanup_failure_finalizes_pending_sibling_and_preserves_error(
    install_fake, monkeypatch, caplog, path, fail_on
):
    monkeypatch.setattr(secret_lookup, "_REAP_TIMEOUT_SECONDS", 0.5)
    proc = install_fake(
        _Child(
            stall_read=fail_on == "wait",
            stall_wait=fail_on == "drain",
            overflow=path == "strict",
            fail_on=fail_on,
        )
    )
    caplog.set_level(logging.DEBUG, logger=secret_lookup.__name__)
    existing_tasks = asyncio.all_tasks()

    if path == "ordinary" and fail_on == "drain":
        assert await asyncio.wait_for(_lookup(path, proc), timeout=1.5) is None
    else:
        with pytest.raises(OSError) as caught:
            await asyncio.wait_for(_lookup(path, proc), timeout=1.5)
        assert caught.value is proc.failure
    _assert_finished(proc)
    assert proc.pipe.close_calls == 1
    assert _SECRET not in caplog.text
    assert not {
        task for task in asyncio.all_tasks() if task not in existing_tasks and not task.done()
    }


@pytest.mark.parametrize("path", ["ordinary", "strict"])
async def test_drain_failure_after_wait_finishes_closes_stdout(
    install_fake, monkeypatch, caplog, path
):
    monkeypatch.setattr(secret_lookup, "_REAP_TIMEOUT_SECONDS", 0.5)
    proc = install_fake(
        _Child(
            stall_read=False,
            stall_wait=False,
            overflow=path == "strict",
            fail_on="drain-after-wait",
        )
    )
    caplog.set_level(logging.DEBUG, logger=secret_lookup.__name__)
    existing_tasks = asyncio.all_tasks()

    if path == "ordinary":
        assert await asyncio.wait_for(_lookup(path, proc), timeout=1.5) is None
    else:
        with pytest.raises(OSError) as caught:
            await asyncio.wait_for(_lookup(path, proc), timeout=1.5)
        assert caught.value is proc.failure
    _assert_finished(proc)
    assert proc.pipe.close_calls == 1
    assert _SECRET not in caplog.text
    assert not {
        task for task in asyncio.all_tasks() if task not in existing_tasks and not task.done()
    }


@pytest.mark.parametrize("path", ["ordinary", "strict"])
async def test_cancellation_before_kill_finishes_cleanup_and_propagates(
    install_fake, monkeypatch, path
):
    proc = install_fake(_Child(stall_read=False, stall_wait=False))
    cleanup_name = "_terminate" if path == "ordinary" else "_terminate_draining"
    real_cleanup = getattr(secret_lookup, cleanup_name)
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_cleanup(child, leader_created):
        started.set()
        await release.wait()
        await real_cleanup(child, leader_created)

    monkeypatch.setattr(secret_lookup, cleanup_name, delayed_cleanup)
    task = asyncio.create_task(_lookup(path, proc))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.75)
    _assert_finished(proc)


@pytest.mark.parametrize("path", ["ordinary", "strict"])
async def test_direct_cancellation_during_cleanup_is_propagated_after_finalization(
    install_fake, path
):
    proc = install_fake(_Child(stall_read=True, stall_wait=True, overflow=path == "strict"))
    task = asyncio.create_task(_lookup(path, proc))
    await asyncio.wait_for(proc.killed.wait(), timeout=0.5)
    await asyncio.wait_for(proc.wait_started.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.75)
    _assert_finished(proc)
    assert proc.pipe.close_calls == 1


@pytest.mark.parametrize("path", ["ordinary", "strict"])
async def test_repeated_direct_cancellation_does_not_abandon_cleanup(
    install_fake, monkeypatch, path
):
    monkeypatch.setattr(secret_lookup, "_REAP_TIMEOUT_SECONDS", 0.15, raising=False)
    proc = install_fake(_Child(stall_read=True, stall_wait=True, overflow=path == "strict"))
    task = asyncio.create_task(_lookup(path, proc))
    await asyncio.wait_for(proc.killed.wait(), timeout=0.5)
    await asyncio.wait_for(proc.wait_started.wait(), timeout=0.5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.75)
    _assert_finished(proc)
    assert proc.pipe.close_calls == 1


@pytest.mark.parametrize("path", ["ordinary", "strict"])
async def test_anyio_cancellation_during_cleanup_reaches_caller(install_fake, path):
    proc = install_fake(_Child(stall_read=True, stall_wait=True, overflow=path == "strict"))
    scopes = []
    cancellation_seen = []

    async def caller():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            try:
                await _lookup(path, proc)
            except asyncio.CancelledError:
                cancellation_seen.append(True)
                raise

    task = asyncio.create_task(caller())
    await asyncio.wait_for(proc.killed.wait(), timeout=0.5)
    await asyncio.wait_for(proc.wait_started.wait(), timeout=0.5)
    scopes[0].cancel()
    await asyncio.wait_for(task, timeout=0.75)
    assert cancellation_seen == [True]
    _assert_finished(proc)
    assert proc.pipe.close_calls == 1
