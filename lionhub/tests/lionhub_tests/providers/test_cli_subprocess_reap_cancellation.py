# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Consumer cancellation survives joining the stream's four helper tasks."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio

from lionhub.providers import _cli_subprocess as cs


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


@pytest_asyncio.fixture(loop_scope="function")
async def held_helpers(monkeypatch):
    names = ("stdout", "stderr", "stdin", "wait")
    started = {name: asyncio.Event() for name in names}
    gates = {name: asyncio.Event() for name in names}
    state = SimpleNamespace(
        tasks={},
        gates=gates,
        order=[],
        kept=0,
        released=0,
        runner=None,
        stdout_error=None,
        end_error=None,
        cancel_at_end=False,
        cancel_at_drain=False,
        after_end=False,
        pending_at_end=set(),
        reap_cancelled=None,
        drain_cancelled=None,
        stderr_pending_at_warning=None,
    )

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
            if self.name == "stdout" and state.stdout_error is not None:
                raise state.stdout_error
            return b""

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

    async def end(child, *, spawned, witnesses, signal_gate):
        assert child is proc and spawned.pid == proc.pid
        assert isinstance(witnesses, tuple) and isinstance(signal_gate, cs.SignalGate)
        state.order.append("end group")
        state.after_end = True
        state.pending_at_end = {name for name, task in state.tasks.items() if not task.done()}
        if state.end_error is not None:
            raise state.end_error
        if state.cancel_at_end:
            asyncio.get_running_loop().call_soon(state.runner.cancel)
        return cs._ChildGroupCleanup.CONFIRMED_EMPTY

    real_create_task = asyncio.create_task
    real_wait = asyncio.wait
    real_wait_for = asyncio.wait_for
    task_names = {
        "_drain_stderr": "stderr",
        "_write_stdin": "stdin",
        "_await_child_exit": "exit",
        "read": "read",
    }

    def create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        code = getattr(coro, "cr_code", None)
        name = task_names.get(code.co_name if code is not None else None)
        if name is not None:
            assert name not in state.tasks
            state.tasks[name] = task
        return task

    async def wait(tasks, *args, **kwargs):
        on_drain = (
            asyncio.current_task() is state.runner
            and state.after_end
            and tasks == {state.tasks["stderr"]}
        )
        if on_drain and state.cancel_at_drain:
            state.cancel_at_drain = False
            asyncio.get_running_loop().call_soon(state.runner.cancel)
        try:
            return await real_wait(tasks, *args, **kwargs)
        except asyncio.CancelledError as error:
            if asyncio.current_task() is state.runner and state.after_end:
                if on_drain:
                    state.drain_cancelled = error
                else:
                    state.reap_cancelled = error
            raise

    original_warning = cs.log.warning

    def warning(message, *args, **kwargs):
        if message.startswith("CLI subprocess produced no output before it was abandoned"):
            state.order.append("warn")
            state.stderr_pending_at_warning = not state.tasks["stderr"].done()
        original_warning(message, *args, **kwargs)

    monkeypatch.setattr(cs, "admit_subagent", admit)
    monkeypatch.setattr(cs, "fill_declared_secrets_and_names", fill)
    monkeypatch.setattr(cs, "refuse_nested_agent", lambda _engine: None)
    monkeypatch.setattr(
        cs, "observe_spawned", lambda pid, **_kwargs: cs.SpawnedProcess(pid, pid, 1.0)
    )
    monkeypatch.setattr(cs, "child_agent_depth", lambda: 0)
    monkeypatch.setattr(cs, "end_child_group", end)
    monkeypatch.setattr(cs.log, "warning", warning)
    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(cs.asyncio, "create_task", create_task)
    monkeypatch.setattr(cs.asyncio, "wait", wait)

    async def consume():
        async for _item in cs.ndjson_from_cli([sys.executable], env={}, stdin_data=b"input"):
            pytest.fail("the stand-in stdout must not produce a value")
        return "returned normally"

    state.runner = real_create_task(consume())
    try:
        await real_wait_for(asyncio.gather(*(event.wait() for event in started.values())), 5.0)
        assert set(state.tasks) == {"stderr", "stdin", "exit", "read"}
        assert all(not task.done() for task in state.tasks.values())
        yield state
    finally:
        for gate in gates.values():
            gate.set()
        if not state.runner.done():
            state.runner.cancel()
        for task in state.tasks.values():
            if not task.done():
                task.cancel()
        await real_wait_for(
            asyncio.gather(state.runner, *state.tasks.values(), return_exceptions=True), 5.0
        )


async def _finished(state):
    done, _ = await asyncio.wait({state.runner}, timeout=5.0)
    assert done, "the consumer must finish after its helpers are released or cancelled"


def _all_reaped(state):
    assert set(state.tasks) == {"stderr", "stdin", "exit", "read"}
    assert all(task.done() for task in state.tasks.values()), "all four helpers must be joined"
    assert state.order.count("stdin closed") == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_normal_eof_joins_the_held_helpers(held_helpers):
    state = held_helpers
    state.gates["stdout"].set()
    state.gates["wait"].set()
    await _finished(state)
    assert state.runner.result() == "returned normally"
    assert {"stderr", "stdin"} <= state.pending_at_end
    assert state.order.index("end group") < state.order.index("stderr stopped")
    assert state.released == 1 and state.kept == 0
    _all_reaped(state)


@pytest.mark.asyncio(loop_scope="function")
async def test_cancel_during_helper_reap_reaches_the_consumer(held_helpers):
    state = held_helpers
    state.cancel_at_end = True
    state.gates["stdout"].set()
    state.gates["wait"].set()
    await _finished(state)
    assert state.runner.cancelled(), "consumer cancellation during helper reap must survive"
    assert {"stderr", "stdin"} <= state.pending_at_end
    assert state.reap_cancelled is not None
    with pytest.raises(asyncio.CancelledError) as raised:
        await state.runner
    assert raised.value is state.reap_cancelled or raised.value.__context__ is state.reap_cancelled
    assert state.released == 1 and state.kept == 0
    _all_reaped(state)


@pytest.mark.asyncio(loop_scope="function")
async def test_cancel_during_abandoned_drain_is_warned_then_propagated(held_helpers):
    state = held_helpers
    state.stdout_error = ValueError("stdout read failed")
    state.cancel_at_drain = True
    state.gates["stdout"].set()
    await _finished(state)
    assert state.runner.cancelled(), "consumer cancellation during the abandoned drain must survive"
    assert state.drain_cancelled is not None
    with pytest.raises(asyncio.CancelledError) as raised:
        await state.runner
    assert (
        raised.value is state.drain_cancelled or raised.value.__context__ is state.drain_cancelled
    )
    assert state.stderr_pending_at_warning is True
    assert state.order.index("warn") < state.order.index("stderr stopped")
    assert state.released == 1 and state.kept == 0
    _all_reaped(state)


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("cancelled", [False, True], ids=["error", "cancelled"])
async def test_unconfirmed_group_end_keeps_the_slots_and_the_teardown_exception(
    held_helpers, cancelled
):
    state = held_helpers
    primary = asyncio.CancelledError() if cancelled else RuntimeError("group teardown failed")
    state.end_error = primary
    state.gates["stdout"].set()
    state.gates["wait"].set()
    await _finished(state)
    if cancelled:
        assert state.runner.cancelled(), "slot retention must not replace teardown cancellation"
        with pytest.raises(asyncio.CancelledError) as raised:
            await state.runner
        if sys.version_info < (3, 11):
            # bpo-45390: nested tasks may wrap the original cancellation.
            assert _context_contains(raised.value, primary)
        else:
            assert raised.value is primary or raised.value.__context__ is primary
    else:
        assert state.runner.exception() is primary, "the original teardown error must stay primary"
    assert state.kept == 0 and state.released == 0, "an unconfirmed group keeps its holder locks"
    _all_reaped(state)
