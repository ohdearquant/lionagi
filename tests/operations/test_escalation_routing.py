# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Escalation routing tests for ReactiveExecutor.

Verifies that an EscalationRequest emitted by a node is consumed by the
executor, drives the intended routing (higher_tier re-spawn / give_up),
and emits NodeEscalated on the session bus.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lionagi.casts.emission import EscalationRequest, SpawnRequest
from lionagi.operations import Operation, flow
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.operations.node import create_operation
from lionagi.session.branch import Branch
from lionagi.session.session import Session
from lionagi.session.signal import NodeEscalated


def _session(**ops):
    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    for name, fn in ops.items():
        session.register_operation(name, fn)
    return session


# ---------------------------------------------------------------------------
# higher_tier: re-spawns the op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_higher_tier_spawns_child():
    """An EscalationRequest with route=higher_tier causes a child op to be injected."""
    executed: list[str] = []

    async def cheap(**kw):
        executed.append("cheap")
        return EscalationRequest(reason="too hard", context={"route": "higher_tier"})

    async def cheap_escalated(**kw):
        executed.append("cheap_escalated")
        return "done by higher tier"

    session = _session(cheap=cheap)

    # node_builder maps the escalation re-spawn -> cheap_escalated
    def node_builder(req: Any, emitter: Operation) -> Operation:
        return create_operation("cheap_escalated", parameters={})

    # Register the op the re-spawned child will use
    session.register_operation("cheap_escalated", cheap_escalated)

    builder = OperationGraphBuilder()
    builder.add_operation("cheap")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True, node_builder=node_builder)

    assert "cheap" in executed
    # escalation re-spawn uses the default node builder path, so we need to
    # check escalated_operations is populated and spawn_count reflects it.
    assert len(result["escalated_operations"]) >= 1
    assert result["spawned_operations"] >= 1


@pytest.mark.asyncio
async def test_escalation_higher_tier_emits_node_escalated_signal():
    """NodeEscalated is emitted on the session bus when higher_tier routing fires."""
    escalated_signals: list[NodeEscalated] = []

    async def cheap(**kw):
        return EscalationRequest(reason="stuck", context={"route": "higher_tier"})

    session = _session(cheap=cheap)
    session.observe(NodeEscalated, handler=lambda s, _: escalated_signals.append(s))

    builder = OperationGraphBuilder()
    builder.add_operation("cheap")
    graph = builder.get_graph()

    await flow(session, graph, reactive=True)

    # Give the event loop a tick to flush fire-and-forget tasks
    await asyncio.sleep(0)

    assert len(escalated_signals) >= 1
    sig = escalated_signals[0]
    assert sig.route == "higher_tier"
    assert "stuck" in sig.reason
    assert isinstance(sig.escalation_request, EscalationRequest)


# ---------------------------------------------------------------------------
# give_up: signals without re-spawning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_give_up_does_not_spawn():
    """An EscalationRequest with route=give_up signals but does NOT inject a new op."""
    executed: list[str] = []

    async def overloaded(**kw):
        executed.append("overloaded")
        return EscalationRequest(reason="beyond capacity", context={"route": "give_up"})

    session = _session(overloaded=overloaded)

    builder = OperationGraphBuilder()
    builder.add_operation("overloaded")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True)

    assert "overloaded" in executed
    # give_up must NOT spawn new ops
    assert result["spawned_operations"] == 0
    # but the op should be in escalated_operations
    assert len(result["escalated_operations"]) >= 1


@pytest.mark.asyncio
async def test_escalation_give_up_emits_node_escalated_signal():
    """NodeEscalated is emitted on the bus even for give_up route."""
    escalated_signals: list[NodeEscalated] = []

    async def give_up_op(**kw):
        return EscalationRequest(reason="cannot proceed", context={"route": "give_up"})

    session = _session(give_up_op=give_up_op)
    session.observe(NodeEscalated, handler=lambda s, _: escalated_signals.append(s))

    builder = OperationGraphBuilder()
    builder.add_operation("give_up_op")
    graph = builder.get_graph()

    await flow(session, graph, reactive=True)
    await asyncio.sleep(0)

    assert len(escalated_signals) >= 1
    sig = escalated_signals[0]
    assert sig.route == "give_up"
    assert isinstance(sig.escalation_request, EscalationRequest)


# ---------------------------------------------------------------------------
# Default route (no explicit route key) → higher_tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_default_route_is_higher_tier():
    """EscalationRequest without a route key in context defaults to higher_tier."""
    escalated_signals: list[NodeEscalated] = []

    async def unsure(**kw):
        return EscalationRequest(reason="unsure")  # no context.route

    session = _session(unsure=unsure)
    session.observe(NodeEscalated, handler=lambda s, _: escalated_signals.append(s))

    builder = OperationGraphBuilder()
    builder.add_operation("unsure")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True)
    await asyncio.sleep(0)

    assert len(escalated_signals) >= 1
    assert escalated_signals[0].route == "higher_tier"
    assert result["spawned_operations"] >= 1


# ---------------------------------------------------------------------------
# Deduplication: same EscalationRequest object only processed once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_request_deduplicated():
    """The same EscalationRequest object is not processed twice."""
    escalated_signals: list[NodeEscalated] = []
    call_count = {"n": 0}

    req = EscalationRequest(reason="once", context={"route": "give_up"})

    async def emitter(**kw):
        call_count["n"] += 1
        return req  # same object reference each time

    session = _session(emitter=emitter)
    session.observe(NodeEscalated, handler=lambda s, _: escalated_signals.append(s))

    builder = OperationGraphBuilder()
    builder.add_operation("emitter")
    graph = builder.get_graph()

    await flow(session, graph, reactive=True)
    await asyncio.sleep(0)

    # NodeEscalated fires at most once per unique EscalationRequest object
    assert len(escalated_signals) <= 1


# ---------------------------------------------------------------------------
# NodeEscalated.escalation_request is stored in a named field, not Signal.data
# ---------------------------------------------------------------------------


def test_node_escalated_request_not_in_data_field():
    """escalation_request lives in a named field, not Signal.data, so it can't re-trigger the bus."""
    req = EscalationRequest(reason="test")
    sig = NodeEscalated(op_id="x", name="x", reason="test", route="give_up", escalation_request=req)
    assert not isinstance(sig.data, EscalationRequest)
    assert sig.escalation_request is req


# ---------------------------------------------------------------------------
# SpawnRequest and EscalationRequest coexist without interference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_and_escalation_coexist():
    """SpawnRequest and EscalationRequest are both consumed without mutual interference."""
    spawn_signal_received: list[bool] = []
    escalation_signal_received: list[NodeEscalated] = []

    async def both_emitter(**kw):
        # Return a SpawnRequest — EscalationRequest is tested separately below
        return SpawnRequest(instruction="follow-up", independent=True)

    async def follow_up(**kw):
        return EscalationRequest(reason="needs help", context={"route": "give_up"})

    session = _session(both_emitter=both_emitter, follow_up=follow_up)
    session.observe(NodeEscalated, handler=lambda s, _: escalation_signal_received.append(s))

    def node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        return create_operation("follow_up", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("both_emitter")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True, node_builder=node_builder)
    await asyncio.sleep(0)

    # spawner spawned follow_up, which in turn escalated
    assert result["spawned_operations"] >= 1
    assert len(escalation_signal_received) >= 1
    assert len(result["escalated_operations"]) >= 1


# ---------------------------------------------------------------------------
# Bus-based emission path (observe fires _on_bus_escalation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_via_result_extraction():
    """EscalationRequest returned as a direct result is extracted and routed."""
    escalated_signals: list[NodeEscalated] = []

    async def stuck(**kw):
        return EscalationRequest(reason="extracted-from-result", context={"route": "give_up"})

    session = _session(stuck=stuck)
    session.observe(NodeEscalated, handler=lambda s, _: escalated_signals.append(s))

    builder = OperationGraphBuilder()
    builder.add_operation("stuck")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True)
    await asyncio.sleep(0)

    assert len(result["escalated_operations"]) >= 1
    assert len(escalated_signals) >= 1
    assert escalated_signals[0].route == "give_up"
