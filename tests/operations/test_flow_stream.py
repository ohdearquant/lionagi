# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Streaming reactive flow — yields each op as it completes, no LLM."""

from __future__ import annotations

import anyio
import pytest

from lionagi.casts.emission import SpawnRequest
from lionagi.operations import FlowEvent, flow_stream
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.operations.node import create_operation
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.graph.graph import Graph
from lionagi.session.session import Session


def _session(**ops):
    from lionagi.session.branch import Branch

    s = Session()
    b = Branch(name="root")
    s.include_branches(b)
    s.default_branch = b
    for name, fn in ops.items():
        s.register_operation(name, fn)
    return s


@pytest.mark.asyncio
async def test_yields_each_op_on_completion():
    async def fast(**kw):
        return "fast-done"

    async def slow(**kw):
        await anyio.sleep(0.05)
        return "slow-done"

    session = _session(fast=fast, slow=slow)
    g = Graph()
    n1 = create_operation("fast", parameters={})
    n2 = create_operation("slow", parameters={})
    g.add_node(n1)
    g.add_node(n2)

    events = []
    async for ev in session.flow_stream(g):
        assert isinstance(ev, FlowEvent)
        events.append(ev)

    assert len(events) == 2
    # fast completes before slow -> arrives first
    assert events[0].result == "fast-done"
    assert events[1].result == "slow-done"
    assert all(ev.ok for ev in events)


@pytest.mark.asyncio
async def test_spawned_node_streams_its_own_event():
    async def spawner(**kw):
        return SpawnRequest(instruction="extra", independent=True)

    async def extra(**kw):
        return "extra-done"

    session = _session(spawner=spawner, extra=extra)
    g = OperationGraphBuilder()
    g.add_operation("spawner")

    def node_builder(req, emitter):
        return create_operation("extra", parameters={})

    names = []
    spawned_flags = []
    async for ev in session.flow_stream(g.get_graph(), node_builder=node_builder):
        names.append(ev.result)
        spawned_flags.append(ev.spawned)

    assert "extra-done" in names  # injected node streamed its completion
    assert any(spawned_flags)  # one event flagged as spawned


@pytest.mark.asyncio
async def test_early_break_does_not_hang():
    async def quick(**kw):
        return "x"

    async def slow(**kw):
        await anyio.sleep(10)  # would hang if not cancelled on break
        return "never"

    session = _session(quick=quick, slow=slow)
    g = Graph()
    g.add_node(create_operation("quick", parameters={}))
    g.add_node(create_operation("slow", parameters={}))

    seen = 0
    with anyio.fail_after(2):  # whole thing must finish well under slow's 10s
        async for _ev in session.flow_stream(g):
            seen += 1
            break  # bail after first — generator close must cancel the driver

    assert seen == 1


@pytest.mark.asyncio
async def test_dependent_op_streams_after_predecessor():
    order = []

    async def a(**kw):
        order.append("a")
        return "a"

    async def b(**kw):
        order.append("b")
        return "b"

    session = _session(a=a, b=b)
    root = session.default_branch
    g = Graph()
    na = create_operation("a", parameters={})
    nb = create_operation("b", parameters={})
    # pin both to root so the dependent node keeps the registered ops (a
    # context-inheritance clone would not carry them — harness detail).
    na.branch_id = root.id
    nb.branch_id = root.id
    g.add_node(na)
    g.add_node(nb)
    g.add_edge(Edge(head=na.id, tail=nb.id))  # b depends on a

    results = [ev.result async for ev in session.flow_stream(g)]
    assert results == ["a", "b"]


@pytest.mark.asyncio
async def test_non_mapping_response_context_reports_failed_progress():
    async def bad_context(**kw):
        return {"context": ["not", "a", "mapping"]}

    session = _session(bad_context=bad_context)
    graph = Graph()
    node = create_operation("bad_context", parameters={})
    graph.add_node(node)
    progress: list[str] = []

    result = await session.flow(
        graph,
        parallel=False,
        on_progress=lambda _op_id, _name, status, _elapsed: progress.append(status),
    )

    assert progress == ["queued", "started", "failed"]
    assert node.execution.status.value == "failed"
    assert "must be a Mapping" in result["operation_results"][node.id]["error"]


@pytest.mark.asyncio
async def test_non_mapping_response_context_streams_failed_event():
    async def bad_context(**kw):
        return {"context": "not-a-mapping"}

    session = _session(bad_context=bad_context)
    graph = Graph()
    node = create_operation("bad_context", parameters={})
    graph.add_node(node)

    events = [event async for event in session.flow_stream(graph)]

    assert len(events) == 1
    assert events[0].status == "failed"
    assert not events[0].ok
    assert "must be a Mapping" in events[0].result["error"]


def test_execute_stream_yields_events_under_trio():
    """The streaming driver must be scheduled by whatever anyio backend is
    active. asyncio.ensure_future() silently no-ops under Trio, leaving the
    stream waiting forever for an operation that already completed."""

    async def quick(**kw):
        return "done"

    async def scenario():
        session = _session(quick=quick)
        graph = Graph()
        graph.add_node(create_operation("quick", parameters={}))

        events = []
        with anyio.fail_after(2):
            async for ev in session.flow_stream(graph):
                events.append(ev)
        return events

    events = anyio.run(scenario, backend="trio")

    assert len(events) == 1
    assert events[0].result == "done"


def test_execute_stream_early_break_cancels_driver_under_trio():
    """An early consumer break must still cancel the still-running driver
    task promptly, not hang until the slow operation finishes."""

    async def quick(**kw):
        return "quick-done"

    async def slow(**kw):
        await anyio.sleep(10)
        return "never"

    async def scenario():
        session = _session(quick=quick, slow=slow)
        graph = Graph()
        graph.add_node(create_operation("quick", parameters={}))
        graph.add_node(create_operation("slow", parameters={}))

        seen = 0
        with anyio.fail_after(2):
            async for _ev in session.flow_stream(graph):
                seen += 1
                break
        return seen

    seen = anyio.run(scenario, backend="trio")
    assert seen == 1
