# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Stream teardown joins its helpers even when ending the child group fails."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from lionagi.providers import _cli_subprocess as cs


@pytest_asyncio.fixture(loop_scope="function")
async def suspended_cli(monkeypatch):
    started = {name: asyncio.Event() for name in ("stdout", "stderr", "stdin", "wait")}
    gates = {name: asyncio.Event() for name in started}
    state = SimpleNamespace(tasks={}, order=[], error=None, kept=0, released=0, stderr=[])

    class Pipe:
        def __init__(self, name):
            self.name = name

        async def read(self, _size):
            started[self.name].set()
            try:
                await gates[self.name].wait()
            finally:
                if not gates[self.name].is_set():
                    state.order.append(f"{self.name} stopped")
            chunk = state.stderr.pop(0) if self.name == "stderr" and state.stderr else b""
            if self.name == "stderr" and not chunk:
                state.order.append("stderr drained")
            return chunk

    class Input:
        def write(self, payload):
            assert payload == b"input"

        async def drain(self):
            started["stdin"].set()
            try:
                await gates["stdin"].wait()
            finally:
                state.order.append("stdin stopped")

        def close(self):
            state.order.append("stdin closed")

    class Process:
        pid = 12345
        returncode = None
        stdout = Pipe("stdout")
        stderr = Pipe("stderr")
        stdin = Input()

        async def wait(self):
            started["wait"].set()
            try:
                await gates["wait"].wait()
            finally:
                state.order.append("wait stopped")
            return 0

    proc = Process()

    class Lease:
        slots = [object()]

        def record_child(self, pid, create_time):
            assert (pid, create_time) == (proc.pid, 1.0)

        def keep_for_child(self):
            state.kept += 1
            return True

        def release(self):
            state.released += 1

    async def admit(**_kwargs):
        return Lease()

    async def fill(env):
        return env, frozenset()

    async def spawn(*_args, **_kwargs):
        return proc

    async def end(child, *, spawned):
        assert child is proc and spawned.pid == proc.pid
        state.order.append("end group")
        if state.error is not None:
            raise state.error
        gates["stderr"].set()

    real_create_task = asyncio.create_task
    names = {
        "_drain_stderr": "stderr",
        "_write_stdin": "stdin",
        "_await_child_exit": "exit",
        "read": "read",
    }

    def create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        code = getattr(coro, "cr_code", None)
        name = names.get(code.co_name if code is not None else None)
        if name is not None:
            assert name not in state.tasks
            state.tasks[name] = task
        return task

    monkeypatch.setattr(cs, "admit_subagent", admit)
    monkeypatch.setattr(cs, "fill_declared_secrets_and_names", fill)
    monkeypatch.setattr(cs, "refuse_nested_agent", lambda _engine: None)
    monkeypatch.setattr(
        cs, "observe_spawned", lambda pid, **_kwargs: cs.SpawnedProcess(pid, pid, 1.0)
    )
    monkeypatch.setattr(cs, "child_agent_depth", lambda: 0)
    monkeypatch.setattr(cs, "end_child_group", end)
    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(cs.asyncio, "create_task", create_task)

    async def consume():
        async for _item in cs.ndjson_from_cli(["synthetic-cli"], env={}, stdin_data=b"input"):
            pytest.fail("the blocked stdout must not produce a value")

    state.runner = real_create_task(consume())
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 5.0)
        assert set(state.tasks) == {"stderr", "stdin", "exit", "read"}
        assert all(not task.done() for task in state.tasks.values())
        yield state
    finally:
        if not state.runner.done():
            state.runner.cancel()
        for task in state.tasks.values():
            task.cancel()
        await asyncio.gather(state.runner, *state.tasks.values(), return_exceptions=True)


@pytest.mark.asyncio(loop_scope="function")
async def test_helper_tasks_are_reaped_when_group_teardown_is_cancelled(suspended_cli):
    state = suspended_cli
    state.error = asyncio.CancelledError("child group teardown cancelled")
    state.runner.cancel()
    with pytest.raises(asyncio.CancelledError, match="child group teardown cancelled"):
        await asyncio.wait_for(state.runner, 5.0)

    assert state.kept == 1
    assert state.released == 0
    assert all(task.done() for task in state.tasks.values()), (
        "all four stream helpers must be reaped"
    )
    assert state.order.count("stdin closed") == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_abandoned_output_is_drained_then_warned_before_helpers_are_reaped(
    suspended_cli, monkeypatch
):
    state = suspended_cli
    state.stderr[:] = [b"late stderr detail", b""]
    original_warning = cs.log.warning

    def warning(message, *args, **kwargs):
        if message.startswith("CLI subprocess produced no output before it was abandoned"):
            state.order.append("warn")
            assert state.tasks["stderr"].done()
            assert all(not state.tasks[name].done() for name in ("stdin", "exit", "read"))
            assert args == ("its stderr said: late stderr detail",)
        original_warning(message, *args, **kwargs)

    monkeypatch.setattr(cs.log, "warning", warning)
    state.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(state.runner, 5.0)

    assert state.kept == 0
    assert state.released == 1
    assert all(task.done() for task in state.tasks.values())
    assert state.order.index("stderr drained") < state.order.index("warn")
    assert state.order.index("warn") < state.order.index("stdin stopped")
    assert state.order.index("warn") < state.order.index("stdout stopped")
