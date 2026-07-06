# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Reactive flow executor tests — exercised without LLM using registered coroutine operations."""

from __future__ import annotations

import pytest

from lionagi.casts.emission import SpawnRequest
from lionagi.operations import Operation, flow
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.operations.node import create_operation
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


@pytest.mark.asyncio
async def test_spawn_injects_node_into_running_graph():
    """A node that emits a SpawnRequest grows the live DAG by one node."""
    executed: list[str] = []

    async def spawner(**kw):
        executed.append("spawner")
        return SpawnRequest(instruction="follow-up", independent=True)

    async def follow_up(**kw):
        executed.append("follow_up")
        return "did the follow-up work"

    session = _session_with_ops(spawner=spawner, follow_up=follow_up)

    # node_builder maps the spawn request -> a follow_up operation node
    def node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        return create_operation("follow_up", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("spawner")
    graph = builder.get_graph()

    result = await flow(
        session,
        graph,
        reactive=True,
        node_builder=node_builder,
    )

    assert "spawner" in executed
    assert "follow_up" in executed  # injected node actually ran
    assert result["spawned_operations"] == 1
    assert result["dropped_spawns"] == []
    # both the original and injected op are in the results
    assert len(result["completed_operations"]) == 2


@pytest.mark.asyncio
async def test_recursive_spawn_until_condition():
    """A node can spawn a node that spawns again — the DAG grows transitively."""
    counter = {"n": 0}

    async def chain(**kw):
        counter["n"] += 1
        if counter["n"] < 3:
            return SpawnRequest(instruction=f"step {counter['n']}", independent=True)
        return "done"

    session = _session_with_ops(chain=chain)

    def node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        return create_operation("chain", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("chain")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True, node_builder=node_builder)

    assert counter["n"] == 3  # 1 initial + 2 spawned, then it stopped
    assert result["spawned_operations"] == 2


@pytest.mark.asyncio
async def test_spawn_cap_enforced():
    """An endlessly-spawning node is bounded by max_spawn (no runaway)."""

    async def forever(**kw):
        return SpawnRequest(instruction="more", independent=True)

    session = _session_with_ops(forever=forever)

    def node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        return create_operation("forever", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("forever")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True, node_builder=node_builder, max_spawn=5)

    # exactly the cap is honored — 1 initial + 5 injected, then refused
    assert result["spawned_operations"] == 5
    dropped = [d for d in result["dropped_spawns"] if d["reason"] == "max_spawn_exceeded"]
    assert len(dropped) == 1
    assert dropped[0]["op_id"]  # the rejected child's id is traceable
    # pin the exact entry shape so a regression dropping/adding a key is caught
    assert set(dropped[0]) == {"reason", "assignee", "emitter_id", "op_id"}


@pytest.mark.asyncio
async def test_dependent_spawn_runs_after_emitter():
    """A non-independent spawn depends on its emitter (runs after it)."""
    order: list[str] = []

    async def lead(**kw):
        order.append("lead")
        return SpawnRequest(instruction="downstream", independent=False)

    async def downstream(**kw):
        order.append("downstream")
        return "ok"

    session = _session_with_ops(lead=lead, downstream=downstream)

    def node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        return create_operation("downstream", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("lead")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True, node_builder=node_builder)

    assert order == ["lead", "downstream"]
    assert result["spawned_operations"] == 1


def test_inject_rejected_when_not_running():
    """inject() is a no-op (returns False) outside an active flow."""
    from lionagi.operations.flow import ReactiveExecutor
    from lionagi.protocols.graph.graph import Graph

    session = _session_with_ops()
    executor = ReactiveExecutor(session, Graph())
    node = create_operation("noop", parameters={})
    assert executor.inject(node) is False


def test_cycle_injection_rejected():
    """A back-edge injection that would close a cycle is rejected, not run."""
    from lionagi.operations.flow import ReactiveExecutor
    from lionagi.protocols.graph.edge import Edge
    from lionagi.protocols.graph.graph import Graph

    session = _session_with_ops()
    graph = Graph()
    a = create_operation("op", parameters={})
    b = create_operation("op", parameters={})
    graph.add_node(a)
    graph.add_node(b)
    graph.add_edge(Edge(head=a.id, tail=b.id))  # a -> b

    executor = ReactiveExecutor(session, graph)
    executor._running = True

    class _DummyTG:
        def start_soon(self, *a, **k):
            raise AssertionError("rejected injection must not be scheduled")

    executor._tg = _DummyTG()

    # inject existing node `a` after `b` => edge b -> a, closing a<->b cycle
    assert executor.inject(a, after=b, independent=False) is False
    assert graph.is_acyclic()  # graph left clean (edge reverted)

    dropped = [d for d in executor._dropped_spawns if d["reason"] == "cycle"]
    assert len(dropped) == 1
    assert dropped[0]["op_id"] == str(a.id)
    assert set(dropped[0]) == {"reason", "assignee", "emitter_id", "op_id"}


def test_builder_error_recorded_as_dropped_spawn():
    """A node_builder exception is recorded with reason + error, not just logged."""
    from lionagi.operations.flow import ReactiveExecutor
    from lionagi.protocols.graph.graph import Graph

    def raising_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        raise ValueError("unknown assignee: ghost")

    session = _session_with_ops()
    executor = ReactiveExecutor(session, Graph(), node_builder=raising_builder)
    req = SpawnRequest(instruction="x", assignee="ghost")

    assert executor._inject_request(req, emitter=None) is False
    assert executor._dropped_spawns == [
        {
            "reason": "builder_error",
            "assignee": "ghost",
            "emitter_id": None,
            "error": "unknown assignee: ghost",
        }
    ]


def test_null_child_recorded_as_dropped_spawn():
    """A node_builder returning None is recorded, no op_id (no child was built)."""
    from lionagi.operations.flow import ReactiveExecutor
    from lionagi.protocols.graph.graph import Graph

    def none_builder(req: SpawnRequest, emitter: Operation) -> Operation | None:
        return None

    session = _session_with_ops()
    executor = ReactiveExecutor(session, Graph(), node_builder=none_builder)
    req = SpawnRequest(instruction="x")

    assert executor._inject_request(req, emitter=None) is False
    assert executor._dropped_spawns == [
        {"reason": "null_child", "assignee": None, "emitter_id": None}
    ]


def test_duplicate_request_recorded_as_dropped_spawn():
    """The same SpawnRequest object seen twice: first succeeds, second is a de-dup."""
    from lionagi.operations.flow import ReactiveExecutor
    from lionagi.protocols.graph.graph import Graph

    class _DummyTG:
        def start_soon(self, *a, **k):
            pass

    def node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        return create_operation("op", parameters={})

    session = _session_with_ops()
    executor = ReactiveExecutor(session, Graph(), node_builder=node_builder)
    executor._running = True
    executor._tg = _DummyTG()
    req = SpawnRequest(instruction="x", independent=True)

    assert executor._inject_request(req, emitter=None) is True
    assert executor._inject_request(req, emitter=None) is False  # same object: duplicate

    dropped = [d for d in executor._dropped_spawns if d["reason"] == "duplicate"]
    assert len(dropped) == 1
    assert "op_id" not in dropped[0]  # dropped before a child was built
    assert set(dropped[0]) == {"reason", "assignee", "emitter_id"}


@pytest.mark.asyncio
async def test_no_spawn_behaves_like_normal_flow():
    """With no SpawnRequest emitted, reactive flow == normal flow."""
    ran: list[str] = []

    async def plain(**kw):
        ran.append("plain")
        return "result"

    session = _session_with_ops(plain=plain)

    builder = OperationGraphBuilder()
    builder.add_operation("plain")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True)

    assert ran == ["plain"]
    assert result["spawned_operations"] == 0
    assert len(result["completed_operations"]) == 1


# ---------------------------------------------------------------------------
# Regression: execute() must subscribe via the public observer property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_subscribes_via_public_observer_not_private():
    """ReactiveExecutor.execute() must reach the bus via session.observer (public property).

    Strategy: after normal session construction (which initialises _observer via
    the model_validator), we forcibly clear _observer back to None to simulate
    the scenario where the private attr is uninitialised.  If execute() still
    used getattr(session, '_observer', None) it would see None and skip
    subscribing, causing SpawnRequests to be silently dropped.  With the fix,
    execute() calls session.observer (the property) which re-creates the
    observer and the spawn is received.
    """
    executed: list[str] = []

    async def spawner(**kw):
        executed.append("spawner")
        return SpawnRequest(instruction="follow-up", independent=True)

    async def follow_up(**kw):
        executed.append("follow_up")
        return "done"

    session = _session_with_ops(spawner=spawner, follow_up=follow_up)

    # Forcibly clear the private attr to simulate the fragile pre-fix state.
    # After this, getattr(session, '_observer', None) returns None,
    # but session.observer (the property) will lazily recreate it.
    session._observer = None

    def node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        return create_operation("follow_up", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("spawner")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True, node_builder=node_builder)

    # Without the fix, follow_up would never be scheduled (spawn silently dropped).
    assert "follow_up" in executed, (
        "follow_up did not run — execute() did not subscribe via the public observer property"
    )
    assert result["spawned_operations"] == 1


@pytest.mark.asyncio
async def test_execute_stream_subscribes_via_public_observer_not_private():
    """flow_stream() must also subscribe via session.observer when _observer is None."""
    from lionagi.operations import flow_stream

    executed: list[str] = []

    async def spawner(**kw):
        executed.append("spawner")
        return SpawnRequest(instruction="follow-up", independent=True)

    async def follow_up(**kw):
        executed.append("follow_up")
        return "done"

    session = _session_with_ops(spawner=spawner, follow_up=follow_up)
    session._observer = None  # simulate uninitialised private attr

    def node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        return create_operation("follow_up", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("spawner")
    graph = builder.get_graph()

    events = []
    async for event in flow_stream(session, graph, node_builder=node_builder):
        events.append(event)

    assert "follow_up" in executed, (
        "follow_up did not run — execute_stream() did not subscribe via the public observer property"
    )
    assert any(e.spawned for e in events)
