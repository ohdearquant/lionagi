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


# ---------------------------------------------------------------------------
# MAJ-2 — streaming escalation parity with batch execute()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_escalation_higher_tier(monkeypatch):
    """execute_stream registers EscalationRequest and routes to higher_tier.

    ``_schedule_escalation`` is mocked to return True (no LLM) — the test
    verifies the streaming path wires the bus handler at parity with batch.
    """
    from lionagi.casts.emission import EscalationRequest
    from lionagi.operations.flow import ReactiveExecutor
    from lionagi.session.signal import NodeEscalated

    schedule_calls: list[str] = []

    def mock_schedule(self, req, emitter):
        schedule_calls.append(self._escalation_tier or "")
        return True

    monkeypatch.setattr(ReactiveExecutor, "_schedule_escalation", mock_schedule)

    session = _session()
    # Confirm the session has an observer so the bus handler wiring takes effect.
    assert session._observer is not None, "Session must have an observer for bus routing"

    captured: list[NodeEscalated] = []
    session.observe(NodeEscalated, handler=lambda sig, _: captured.append(sig))

    async def escalator(**kw):
        await session.emit(EscalationRequest(reason="streaming out of depth"))
        return "done"

    session.register_operation("escalator", escalator)

    g = Graph()
    g.add_node(create_operation("escalator", parameters={}))

    events = []
    async for ev in session.flow_stream(g, escalation_tier="test-tier"):
        events.append(ev)

    # MAJ-2: streaming path registered _on_bus_escalation — _schedule_escalation was called.
    assert len(schedule_calls) >= 1
    assert schedule_calls[0] == "test-tier"
    # MAJ-2 + handler: NodeEscalated emitted with higher_tier route.
    assert len(captured) >= 1
    assert captured[0].route == "higher_tier"
    # MAJ-4: escalation_request field preserved in streaming path too.
    assert isinstance(captured[0].escalation_request, EscalationRequest)


@pytest.mark.asyncio
async def test_streaming_escalation_no_tier_gives_up():
    """Streaming without escalation_tier → NodeEscalated(route='give_up')."""
    from lionagi.casts.emission import EscalationRequest
    from lionagi.session.signal import NodeEscalated

    session = _session()

    captured: list[NodeEscalated] = []
    session.observe(NodeEscalated, handler=lambda sig, _: captured.append(sig))

    async def escalator(**kw):
        await session.emit(EscalationRequest(reason="no tier configured"))
        return "done"

    session.register_operation("escalator", escalator)

    g = Graph()
    g.add_node(create_operation("escalator", parameters={}))

    events = []
    async for ev in session.flow_stream(g):  # no escalation_tier
        events.append(ev)

    assert len(captured) >= 1
    assert captured[0].route == "give_up"
    # No spawned events — give_up does not inject a child.
    assert not any(ev.spawned for ev in events)
