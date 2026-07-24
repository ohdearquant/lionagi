# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine pause gate tests: soft pause at operation boundaries (flow control plane, part 2)."""

from __future__ import annotations

import asyncio
import importlib
import logging

import anyio
import pytest

from lionagi.ln.concurrency import CapacityLimiter
from lionagi.operations.flow import DependencyAwareExecutor, ReactiveExecutor, flow
from lionagi.operations.node import Operation
from lionagi.protocols.graph.graph import Graph
from lionagi.protocols.types import EventStatus
from lionagi.session.session import Session


def _session_with_ops(**ops):
    """A Session whose default branch resolves the given named operations."""
    from lionagi.session.branch import Branch

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    for name, fn in ops.items():
        session.register_operation(name, fn)
    return session


# ---------------------------------------------------------------------------
# pause() / resume() as sync no-op-safe controls
# ---------------------------------------------------------------------------


def test_pause_installs_event_and_is_idempotent():
    session = Session()
    executor = DependencyAwareExecutor(session=session, graph=Graph())

    assert executor._pause_event is None
    executor.pause()
    gate = executor._pause_event
    assert gate is not None

    executor.pause()  # second call is a no-op — same event instance
    assert executor._pause_event is gate


def test_resume_when_not_paused_is_a_safe_noop():
    session = Session()
    executor = DependencyAwareExecutor(session=session, graph=Graph())

    assert executor._pause_event is None
    executor.resume()  # must not raise
    assert executor._pause_event is None


def test_pause_resume_pause_installs_a_fresh_event():
    session = Session()
    executor = DependencyAwareExecutor(session=session, graph=Graph())

    executor.pause()
    first_gate = executor._pause_event

    executor.resume()
    assert executor._pause_event is None

    executor.pause()
    second_gate = executor._pause_event
    assert second_gate is not None
    assert second_gate is not first_gate


# ---------------------------------------------------------------------------
# Boundary semantics: already-running ops complete; not-yet-started ops block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_blocks_not_yet_started_op_while_running_op_completes():
    """Op already past the gate (inside invoke()) runs to completion under pause;
    an op that has not yet reached the gate stays blocked until resume()."""
    running_started = asyncio.Event()
    running_may_finish = asyncio.Event()
    executed: list[str] = []

    async def running_op(**kw):
        running_started.set()
        await running_may_finish.wait()
        executed.append("running")
        return "running done"

    async def blocked_op(**kw):
        executed.append("blocked")
        return "blocked done"

    session = _session_with_ops(running=running_op, blocked=blocked_op)
    graph = Graph()
    op_running = Operation(operation="running", parameters={})
    op_blocked = Operation(operation="blocked", parameters={})
    graph.add_node(op_running)
    graph.add_node(op_blocked)

    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    limiter = CapacityLimiter(10)

    running_task = asyncio.create_task(executor._execute_operation(op_running, limiter))
    await asyncio.wait_for(running_started.wait(), timeout=2)

    # Pause after op_running is already inside the limiter, invoking.
    executor.pause()

    blocked_task = asyncio.create_task(executor._execute_operation(op_blocked, limiter))
    await asyncio.sleep(0.05)
    assert "blocked" not in executed, "blocked op must not start once paused"

    # Let the already-running op finish — pause must not have interrupted it.
    running_may_finish.set()
    await asyncio.wait_for(running_task, timeout=2)
    assert "running" in executed
    assert op_running.execution.status == EventStatus.COMPLETED

    # Blocked op is still gated.
    assert "blocked" not in executed
    assert not blocked_task.done()

    executor.resume()
    await asyncio.wait_for(blocked_task, timeout=2)
    assert "blocked" in executed
    assert op_blocked.execution.status == EventStatus.COMPLETED


@pytest.mark.asyncio
async def test_resume_releases_blocked_op_to_completion():
    executed: list[str] = []

    async def op_fn(**kw):
        executed.append(kw["op_id"])
        return "ok"

    session = _session_with_ops(op=op_fn)
    graph = Graph()
    op = Operation(operation="op", parameters={"op_id": "x"})
    graph.add_node(op)

    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    limiter = CapacityLimiter(10)

    executor.pause()
    task = asyncio.create_task(executor._execute_operation(op, limiter))
    await asyncio.sleep(0.05)
    assert executed == []

    executor.resume()
    await asyncio.wait_for(task, timeout=2)
    assert executed == ["x"]
    assert op.execution.status == EventStatus.COMPLETED


@pytest.mark.asyncio
async def test_pause_resume_pause_cycle_gates_each_op_in_turn():
    executed: list[str] = []

    async def op_fn(**kw):
        executed.append(kw["op_id"])
        return "ok"

    session = _session_with_ops(op=op_fn)
    graph = Graph()
    op1 = Operation(operation="op", parameters={"op_id": "op1"})
    op2 = Operation(operation="op", parameters={"op_id": "op2"})
    graph.add_node(op1)
    graph.add_node(op2)

    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    limiter = CapacityLimiter(10)

    # Cycle 1: pause blocks op1, resume releases it.
    executor.pause()
    gate1 = executor._pause_event
    t1 = asyncio.create_task(executor._execute_operation(op1, limiter))
    await asyncio.sleep(0.05)
    assert executed == []
    executor.resume()
    await asyncio.wait_for(t1, timeout=2)
    assert executed == ["op1"]

    # Cycle 2: a fresh pause blocks op2 independently of the first gate.
    executor.pause()
    gate2 = executor._pause_event
    assert gate2 is not gate1
    t2 = asyncio.create_task(executor._execute_operation(op2, limiter))
    await asyncio.sleep(0.05)
    assert executed == ["op1"]  # op2 still gated
    executor.resume()
    await asyncio.wait_for(t2, timeout=2)
    assert executed == ["op1", "op2"]


# ---------------------------------------------------------------------------
# NodePaused signal + lifecycle-lane projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_paused_emitted_for_blocked_op_and_lane_resets_on_start():
    from lionagi.session.signal import NodePaused, NodeStarted, lane_for

    may_finish = asyncio.Event()

    async def op_fn(**kw):
        await may_finish.wait()
        return "ok"

    session = _session_with_ops(op=op_fn)
    graph = Graph()
    op = Operation(operation="op", parameters={})
    graph.add_node(op)

    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    limiter = CapacityLimiter(10)

    signals: list[NodePaused] = []
    session.observe(NodePaused, handler=lambda s, _ctx: signals.append(s))

    executor.pause()
    task = asyncio.create_task(executor._execute_operation(op, limiter))

    for _ in range(20):
        if signals:
            break
        await asyncio.sleep(0.01)

    assert len(signals) == 1
    assert signals[0].op_id == str(op.id)

    # lane projection: paused, then a later NodeStarted resets it to running
    assert lane_for([signals[0]]) == "paused"
    assert lane_for([signals[0], NodeStarted(op_id=str(op.id))]) == "running"

    executor.resume()
    may_finish.set()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_node_paused_reemitted_on_resume_then_fresh_pause():
    """A resume followed by a new pause legitimately re-emits NodePaused for the same op."""
    from lionagi.session.signal import NodePaused

    reached_second_gate = asyncio.Event()
    may_finish = asyncio.Event()
    attempts: list[int] = []

    async def op_fn(**kw):
        await may_finish.wait()
        return "ok"

    session = _session_with_ops(op=op_fn)
    graph = Graph()
    op = Operation(operation="op", parameters={})
    graph.add_node(op)

    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    limiter = CapacityLimiter(10)

    signals: list[NodePaused] = []

    def _on_paused(s, _ctx):
        signals.append(s)
        attempts.append(1)
        if len(attempts) == 2:
            reached_second_gate.set()

    session.observe(NodePaused, handler=_on_paused)

    executor.pause()
    task = asyncio.create_task(executor._execute_operation(op, limiter))

    for _ in range(20):
        if signals:
            break
        await asyncio.sleep(0.01)
    assert len(signals) == 1

    # Resume, then re-pause immediately before the op has a chance to enter
    # the limiter — it should hit the (new) gate again and re-emit.
    executor.resume()
    executor.pause()

    await asyncio.wait_for(reached_second_gate.wait(), timeout=2)
    assert len(signals) == 2

    executor.resume()
    may_finish.set()
    await asyncio.wait_for(task, timeout=2)


# ---------------------------------------------------------------------------
# Public path: executor.pause()/.resume() around a full execute() run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_pause_before_execute_blocks_then_resume_releases():
    executed: list[str] = []

    async def op_fn(**kw):
        executed.append(kw.get("op_id"))
        return "ok"

    session = _session_with_ops(op=op_fn)
    graph = Graph()
    for i in range(3):
        graph.add_node(Operation(operation="op", parameters={"op_id": f"op_{i}"}))

    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    executor.pause()

    task = asyncio.create_task(executor.execute())
    await asyncio.sleep(0.1)
    assert executed == []

    executor.resume()
    result = await asyncio.wait_for(task, timeout=5)
    assert sorted(executed) == ["op_0", "op_1", "op_2"]
    assert len(result["completed_operations"]) == 3


@pytest.mark.asyncio
async def test_reactive_executor_inherits_pause_gate():
    """ReactiveExecutor shares _execute_operation with the base class, so pause() gates it too."""
    executed: list[str] = []

    async def op_fn(**kw):
        executed.append(kw.get("op_id"))
        return "ok"

    session = _session_with_ops(op=op_fn)
    graph = Graph()
    graph.add_node(Operation(operation="op", parameters={"op_id": "r1"}))

    executor = ReactiveExecutor(session=session, graph=graph, max_concurrent=10)
    executor.pause()

    task = asyncio.create_task(executor.execute())
    await asyncio.sleep(0.1)
    assert executed == []

    executor.resume()
    result = await asyncio.wait_for(task, timeout=5)
    assert executed == ["r1"]
    assert len(result["completed_operations"]) == 1


# ---------------------------------------------------------------------------
# _emit_best_effort: shared fire-and-forget flow signal scheduling helper
# ---------------------------------------------------------------------------


def test_emit_best_effort_construction_failure_is_logged_and_task_free(caplog):
    """A factory that raises logs a structured warning; nothing is scheduled."""
    session = Session()
    executor = DependencyAwareExecutor(session=session, graph=Graph())

    def _bad_factory():
        raise ValueError("boom")

    with caplog.at_level(logging.WARNING, logger="lionagi.operations.flow"):
        executor._emit_best_effort(_bad_factory)  # must not raise

    assert executor._signal_tasks == set()
    assert any("construction failed" in r.message for r in caplog.records)


def test_emit_best_effort_no_running_loop_is_silent_noop(caplog):
    """No running loop is an expected sync/test condition: silent, not a warning."""
    from lionagi.session.signal import NodePaused

    session = Session()
    executor = DependencyAwareExecutor(session=session, graph=Graph())

    with caplog.at_level(logging.WARNING, logger="lionagi.operations.flow"):
        executor._emit_best_effort(lambda: NodePaused(op_id="x", name="x"))

    assert executor._signal_tasks == set()
    assert caplog.records == []


@pytest.mark.asyncio
async def test_emit_best_effort_scheduling_failure_closes_coro_and_logs(caplog, monkeypatch):
    """A loop.create_task() failure closes the coroutine and logs, never raises."""
    from lionagi.session.signal import NodePaused

    session = Session()
    executor = DependencyAwareExecutor(session=session, graph=Graph())

    class _FailingLoop:
        def create_task(self, coro):
            coro.close()
            raise RuntimeError("scheduling boom")

    flow_module = importlib.import_module("lionagi.operations.flow")
    monkeypatch.setattr(flow_module.asyncio, "get_running_loop", lambda: _FailingLoop())

    with caplog.at_level(logging.WARNING, logger="lionagi.operations.flow"):
        executor._emit_best_effort(lambda: NodePaused(op_id="x", name="x"))  # must not raise

    assert executor._signal_tasks == set()
    assert any("scheduling failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_emit_best_effort_observer_failure_never_reaches_loop_handler(caplog, monkeypatch):
    """session.emit() raising is consumed inside the detached task and logged;
    the loop's default unhandled-exception handler is never invoked."""
    from lionagi.session.signal import NodePaused

    async def failing_emit(self, event):
        raise RuntimeError("observer boom")

    session = Session()
    executor = DependencyAwareExecutor(session=session, graph=Graph())
    monkeypatch.setattr(Session, "emit", failing_emit)

    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        with caplog.at_level(logging.WARNING, logger="lionagi.operations.flow"):
            executor._emit_best_effort(lambda: NodePaused(op_id="x", name="x"))
            assert len(executor._signal_tasks) == 1

            for _ in range(50):
                if not executor._signal_tasks:
                    break
                await asyncio.sleep(0.01)
    finally:
        loop.set_exception_handler(None)

    assert executor._signal_tasks == set()
    assert unhandled == []
    assert any("emission failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_emit_best_effort_task_set_is_empty_after_successful_emission():
    """The retained task is removed from the executor-local set once it completes."""
    from lionagi.session.signal import NodePaused

    session = Session()
    executor = DependencyAwareExecutor(session=session, graph=Graph())

    signals: list[NodePaused] = []
    session.observe(NodePaused, handler=lambda s, _ctx: signals.append(s))

    executor._emit_best_effort(lambda: NodePaused(op_id="x", name="x"))
    assert len(executor._signal_tasks) == 1

    for _ in range(50):
        if not executor._signal_tasks:
            break
        await asyncio.sleep(0.01)

    assert executor._signal_tasks == set()
    assert len(signals) == 1
    assert signals[0].op_id == "x"


# ---------------------------------------------------------------------------
# _emit_best_effort: must deliver signals on every anyio backend, not just
# asyncio — the executor's own open task group (self._tg) is the scheduling
# mechanism during a real execute()/execute_stream() run, so it works
# identically under Trio, which has no ambient loop to post a detached task
# onto.
#
# These use a bare `anyio.run(..., backend="trio")` rather than
# `@pytest.mark.anyio` — this repo's `asyncio_mode = "auto"` pytest-asyncio
# setting hijacks any `async def` test regardless of markers, so a
# `[trio]`-parametrized anyio test still silently executes on asyncio. Only a
# synchronous test driving its own `anyio.run` is guaranteed to actually
# exercise the Trio backend.
# ---------------------------------------------------------------------------


def test_pause_signal_delivered_on_reactive_executor_under_trio():
    async def op_fn(**kw):
        return "ok"

    from lionagi.session.signal import NodePaused

    async def _body():
        session = _session_with_ops(op=op_fn)
        signals: list[NodePaused] = []
        session.observe(NodePaused, handler=lambda s, _ctx: signals.append(s))

        graph = Graph()
        graph.add_node(Operation(operation="op"))

        executor = ReactiveExecutor(session=session, graph=graph, max_concurrent=5)
        executor.pause()

        async with anyio.create_task_group() as tg:
            tg.start_soon(executor.execute)
            with anyio.fail_after(2):
                while not signals:
                    await anyio.sleep(0.01)
            executor.resume()

        assert len(signals) == 1
        assert signals[0].op_id

    anyio.run(_body, backend="trio")


def test_pause_signal_delivered_on_base_executor_under_trio():
    """Same as above but for the plain (non-reactive) execute() path, which
    schedules operations via _alcall rather than a task group it owns for
    running nodes — it must still open one for signal delivery."""

    async def op_fn(**kw):
        return "ok"

    from lionagi.session.signal import NodePaused

    async def _body():
        session = _session_with_ops(op=op_fn)
        signals: list[NodePaused] = []
        session.observe(NodePaused, handler=lambda s, _ctx: signals.append(s))

        graph = Graph()
        graph.add_node(Operation(operation="op"))

        executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=5)
        executor.pause()

        async with anyio.create_task_group() as tg:
            tg.start_soon(executor.execute)
            with anyio.fail_after(2):
                while not signals:
                    await anyio.sleep(0.01)
            executor.resume()

        assert len(signals) == 1
        assert signals[0].op_id

    anyio.run(_body, backend="trio")


# ---------------------------------------------------------------------------
# Zero-cost: a flow that never touches pause/resume behaves identically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flow_without_pause_behaves_normally():
    executed: list[str] = []

    async def op_fn(**kw):
        executed.append(kw.get("op_id"))
        return "ok"

    session = _session_with_ops(op=op_fn)
    graph = Graph()
    for i in range(3):
        graph.add_node(Operation(operation="op", parameters={"op_id": f"op_{i}"}))

    result = await flow(session, graph, max_concurrent=5)
    assert len(result["completed_operations"]) == 3
    assert sorted(executed) == ["op_0", "op_1", "op_2"]


@pytest.mark.asyncio
async def test_flow_reactive_without_pause_behaves_normally():
    executed: list[str] = []

    async def op_fn(**kw):
        executed.append(kw.get("op_id"))
        return "ok"

    session = _session_with_ops(op=op_fn)
    graph = Graph()
    for i in range(3):
        graph.add_node(Operation(operation="op", parameters={"op_id": f"op_{i}"}))

    result = await flow(session, graph, max_concurrent=5, reactive=True)
    assert len(result["completed_operations"]) == 3
    assert sorted(executed) == ["op_0", "op_1", "op_2"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
