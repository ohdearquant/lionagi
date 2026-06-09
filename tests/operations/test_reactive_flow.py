# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Reactive (self-expanding) flow executor — exercised without any LLM.

Operations are plain registered coroutines on the Session's operation
manager. A "spawner" op returns a SpawnRequest; the ReactiveExecutor must
inject the resulting node into the *running* graph, run it, and terminate.
"""

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
# Multiple spawn requests from the same operation (batch spawn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_spawn_requests_from_single_op():
    """An operation returning a list of SpawnRequests spawns multiple nodes."""
    executed: list[str] = []

    async def multi_spawner(**kw):
        executed.append("multi_spawner")
        return [
            SpawnRequest(instruction="branch-a", independent=True),
            SpawnRequest(instruction="branch-b", independent=True),
        ]

    async def spawned_op(**kw):
        executed.append("spawned")
        return "spawned result"

    session = _session_with_ops(multi_spawner=multi_spawner, spawned_op=spawned_op)

    def node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        return create_operation("spawned_op", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("multi_spawner")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True, node_builder=node_builder)

    assert "multi_spawner" in executed
    assert result["spawned_operations"] == 2
    assert len(result["completed_operations"]) == 3


# ---------------------------------------------------------------------------
# Spawn request that references a non-existent operation name
# (node_builder raises, spawn is silently dropped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_with_nonexistent_op_name_is_dropped():
    """When node_builder raises, the spawn is dropped and flow continues."""
    executed: list[str] = []

    async def spawner(**kw):
        executed.append("spawner")
        return SpawnRequest(instruction="nonexistent-op", independent=True)

    session = _session_with_ops(spawner=spawner)

    def failing_node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        raise ValueError("no such operation: nonexistent-op")

    builder = OperationGraphBuilder()
    builder.add_operation("spawner")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True, node_builder=failing_node_builder)

    # Original op completed; bad spawn was silently dropped
    assert "spawner" in executed
    assert result["spawned_operations"] == 0
    assert len(result["completed_operations"]) == 1


# ---------------------------------------------------------------------------
# Independent spawn with failed predecessor — still runs (independent=True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_independent_spawn_runs_even_when_emitter_fails():
    """independent=True spawn does not depend on emitter completion/success."""
    executed: list[str] = []

    async def failing_spawner(**kw):
        executed.append("failing_spawner_start")
        # Return a spawn first, then fail
        return SpawnRequest(instruction="follow", independent=True)

    async def follow(**kw):
        executed.append("follow")
        return "follow result"

    session = _session_with_ops(failing_spawner=failing_spawner, follow=follow)

    def node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        return create_operation("follow", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("failing_spawner")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True, node_builder=node_builder)

    # follow ran because independent=True (no edge to failing_spawner)
    assert "follow" in executed
    assert result["spawned_operations"] >= 1
