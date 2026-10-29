# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Bounded teardown with fake tasks, witnesses and signal actions only."""

import asyncio
import os
import socket
import subprocess
from types import SimpleNamespace

import pytest

from lionagi.providers import _cli_subprocess as cli


def _forbid(*_args, **_kwargs):
    raise AssertionError("real subprocess, signal or network forbidden")


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _forbid)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _forbid)
    monkeypatch.setattr(subprocess, "Popen", _forbid)
    monkeypatch.setattr(os, "kill", _forbid)
    monkeypatch.setattr(os, "killpg", _forbid, raising=False)
    monkeypatch.setattr(socket.socket, "connect", _forbid)
    monkeypatch.setattr(cli, "_witnesses_for_spawn", lambda *_: ((4242, 1.0),))


@pytest.mark.asyncio
async def test_cutoff_fences_late_group_signal_and_retains_worker(monkeypatch):
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    actions, owned = [], []
    proc = SimpleNamespace(pid=4242)
    spawned = cli.SpawnedProcess(4242, 4242, 1.0)
    retain = cli._retain_cli_task

    async def group(_proc, **_kwargs):
        _, witnesses, gate = cli._CLEANUP_EVIDENCE.get()
        assert witnesses == ((4242, 1.0),)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        gate.run(lambda: actions.append("late"), skipped=None)
        return cli._ChildGroupCleanup.CONFIRMED_EMPTY

    def backstop(_proc, witnesses, *, signal_gate):
        assert witnesses == ((4242, 1.0),)
        signal_gate.run(lambda: actions.append("backstop"), skipped=None)

    def record(task, *args, **kwargs):
        owned.append(task)
        retain(task, *args, **kwargs)

    monkeypatch.setattr(cli, "end_child_group", group)
    monkeypatch.setattr(cli, "_end_group_with_evidence", backstop)
    monkeypatch.setattr(cli, "_retain_cli_task", record)
    try:
        result = await cli._bounded_cli_cleanup(proc, spawned, grace=0.02)
        assert started.is_set()
        assert result.outcome is cli._ChildGroupCleanup.UNCONFIRMED
        await asyncio.wait_for(cancelled.wait(), 1)
        assert len(owned) == 1 and owned[0] in cli._RETAINED_CLI_TASKS
        release.set()
        await asyncio.wait_for(owned[0], 1)
        await asyncio.sleep(0)
        assert actions == ["backstop"]
        assert owned[0] not in cli._RETAINED_CLI_TASKS
    finally:
        release.set()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_second_direct_cancel_is_unconfirmed_and_fences_worker(monkeypatch):
    started, release, backstop = asyncio.Event(), asyncio.Event(), []
    proc = SimpleNamespace(pid=4242)
    spawned = cli.SpawnedProcess(4242, 4242, 1.0)
    owned = []
    retain = cli._retain_cli_task

    async def group(_proc, **_kwargs):
        started.set()
        await release.wait()
        return cli._ChildGroupCleanup.CONFIRMED_EMPTY

    monkeypatch.setattr(cli, "end_child_group", group)
    monkeypatch.setattr(cli, "_end_group_with_evidence", lambda *a, **k: backstop.append(True))
    monkeypatch.setattr(
        cli, "_retain_cli_task", lambda task, *a, **k: (owned.append(task), retain(task, *a, **k))
    )
    task = asyncio.create_task(cli._bounded_cli_cleanup(proc, spawned, grace=1))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel("first")
        await asyncio.sleep(0)
        task.cancel("second")
        result = await asyncio.wait_for(task, 1)
        assert result.cancellation.args == ("first",)
        assert result.outcome is cli._ChildGroupCleanup.UNCONFIRMED
        assert backstop == [True] and len(owned) == 1
    finally:
        release.set()
        await asyncio.gather(task, *owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_helpers_cancel_together_and_stuck_helpers_are_owned_once(monkeypatch):
    proc = SimpleNamespace(pid=4242)
    spawned = cli.SpawnedProcess(4242, 4242, 1.0)
    release = asyncio.Event()
    cancelled = [asyncio.Event() for _ in range(4)]
    started = [asyncio.Event() for _ in range(4)]
    owned = []
    observed = []
    retain = cli._retain_cli_task
    forget = cli._forget_cli_task

    async def group(_proc, **_kwargs):
        return cli._ChildGroupCleanup.CONFIRMED_EMPTY

    async def helper(index):
        started[index].set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled[index].set()
            await release.wait()
        raise RuntimeError("controlled late failure")

    def record(task, *args, **kwargs):
        owned.append(task)
        retain(task, *args, **kwargs)

    def consume(task):
        observed.append(task)
        forget(task)

    monkeypatch.setattr(cli, "end_child_group", group)
    monkeypatch.setattr(cli, "_retain_cli_task", record)
    monkeypatch.setattr(cli, "_forget_cli_task", consume)
    helpers = [asyncio.create_task(helper(i)) for i in range(4)]
    try:
        await asyncio.gather(*(event.wait() for event in started))
        result = await asyncio.wait_for(
            cli._bounded_cli_cleanup(
                proc, spawned, helpers=tuple((str(i), t) for i, t in enumerate(helpers)), grace=0.02
            ),
            1.5,
        )
        assert result.outcome is cli._ChildGroupCleanup.CONFIRMED_EMPTY
        assert all(event.is_set() for event in cancelled)
        assert set(owned) == set(helpers) and len(owned) == 4
        release.set()
        await asyncio.gather(*helpers, return_exceptions=True)
        await asyncio.sleep(0)
        assert sorted(map(id, observed)) == sorted(map(id, helpers))
        assert not any(t in cli._RETAINED_CLI_TASKS for t in helpers)
    finally:
        release.set()
        await asyncio.gather(*helpers, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_task_factory_failure_uses_backstop_and_owns_helpers(monkeypatch, failure_type):
    import inspect

    proc = SimpleNamespace(pid=4242)
    spawned = cli.SpawnedProcess(4242, 4242, 1.0)
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    actions, gates, created, retained, observed = [], [], [], [], []
    failure = failure_type("factory failure")
    retain, forget = cli._retain_cli_task, cli._forget_cli_task

    async def group(_proc, **_kwargs):
        raise AssertionError("the unscheduled coroutine must never run")

    async def helper():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        raise RuntimeError("controlled late helper failure")

    def reject_task(coro):
        created.append(coro)
        raise failure

    def backstop(_proc, witnesses, *, signal_gate):
        assert witnesses == ((4242, 1.0),)
        gates.append(signal_gate)
        signal_gate.run(lambda: actions.append("backstop"), skipped=None)

    def record(task, *args, **kwargs):
        retained.append(task)
        retain(task, *args, **kwargs)

    def consume(task):
        observed.append(task)
        forget(task)

    task = asyncio.create_task(helper())
    await started.wait()
    monkeypatch.setattr(cli, "end_child_group", group)
    monkeypatch.setattr(cli, "_end_group_with_evidence", backstop)
    monkeypatch.setattr(cli, "_retain_cli_task", record)
    monkeypatch.setattr(cli, "_forget_cli_task", consume)
    monkeypatch.setattr(asyncio, "create_task", reject_task)
    try:
        result = await cli._bounded_cli_cleanup(proc, spawned, helpers=(("stdout", task),))
        assert result.outcome is cli._ChildGroupCleanup.UNCONFIRMED
        assert result.error is failure
        assert result.cancellation is (failure if failure_type is asyncio.CancelledError else None)
        assert len(created) == 1 and inspect.getcoroutinestate(created[0]) == inspect.CORO_CLOSED
        assert actions == ["backstop"] and len(gates) == 1
        gates[0].run(lambda: actions.append("late"), skipped=None)
        assert actions == ["backstop"]
        assert cancelled.is_set() and retained == [task] and task in cli._RETAINED_CLI_TASKS
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert observed == [task] and task not in cli._RETAINED_CLI_TASKS
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_direct_cancellation_after_task_factory_failure_wins(monkeypatch):
    proc = SimpleNamespace(pid=4242)
    spawned = cli.SpawnedProcess(4242, 4242, 1.0)
    helper_started, helper_cancelled, release, begin = (asyncio.Event() for _ in range(4))
    failure = RuntimeError("factory failure")
    signals = []

    async def group(_proc, **_kwargs):
        raise AssertionError("the unscheduled coroutine must never run")

    async def helper():
        helper_started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            helper_cancelled.set()
            await release.wait()

    async def supervise(task):
        await begin.wait()
        return await cli._bounded_cli_cleanup(proc, spawned, helpers=(("stdout", task),))

    task = asyncio.create_task(helper())
    supervisor = asyncio.create_task(supervise(task))
    await helper_started.wait()

    def reject_task(_coro):
        raise failure

    monkeypatch.setattr(cli, "end_child_group", group)
    monkeypatch.setattr(cli, "_end_group_with_evidence", lambda *a, **k: signals.append("backstop"))
    monkeypatch.setattr(asyncio, "create_task", reject_task)
    try:
        begin.set()
        await asyncio.wait_for(helper_cancelled.wait(), 1)
        supervisor.cancel("direct during cleanup")
        await asyncio.sleep(0)
        release.set()
        result = await asyncio.wait_for(supervisor, 1)
        assert result.error is failure
        assert result.cancellation.args == ("direct during cleanup",)
        assert result.outcome is cli._ChildGroupCleanup.UNCONFIRMED
        assert signals == ["backstop"]
    finally:
        release.set()
        await asyncio.gather(supervisor, task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True], ids=["unconfirmed", "confirmed"])
async def test_frozen_witness_is_in_result_and_logs_once_without_marker(
    monkeypatch, caplog, confirmed
):
    import logging

    proc = SimpleNamespace(pid=4242)
    spawned = cli.SpawnedProcess(4242, 4242, 1.0, "private-marker-must-not-appear")
    lease = SimpleNamespace(slots=[SimpleNamespace(pool="fake")], release=lambda: None)
    expected = (
        cli._ChildGroupCleanup.CONFIRMED_EMPTY if confirmed else cli._ChildGroupCleanup.UNCONFIRMED
    )

    async def group(_proc, **_kwargs):
        return expected

    monkeypatch.setattr(cli, "end_child_group", group)
    with caplog.at_level(logging.DEBUG, logger=cli.log.name):
        result = await cli._bounded_cli_cleanup(proc, spawned)
        cli._finish_child_lease(lease, result.outcome, witnesses=result.witnesses)

    assert result.witnesses == ((4242, 1.0),)
    messages = [record.getMessage() for record in caplog.records if record.name == cli.log.name]
    starts = [message for message in messages if "CLI cleanup starting" in message]
    outcomes = [
        message
        for message in messages
        if "CLI cleanup finished" in message
        or "CLI child group cleanup remains unconfirmed" in message
    ]
    assert len(starts) == len(outcomes) == 1
    assert "((4242, 1.0),)" in starts[0] and "((4242, 1.0),)" in outcomes[0]
    assert "private-marker-must-not-appear" not in " ".join(messages)
