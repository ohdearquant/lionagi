# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Escalation routing tests for ReactiveExecutor.

Verifies that an EscalationRequest emitted by a node is consumed by the
executor, drives the intended routing (higher_tier re-spawn / give_up),
and emits NodeEscalated on the session bus.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from lionagi.casts.emission import EscalationRequest, SpawnRequest
from lionagi.operations import Operation, flow
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.operations.node import create_operation
from lionagi.protocols._concepts import Condition
from lionagi.protocols.graph.edge import Edge
from lionagi.session.branch import Branch
from lionagi.session.session import Session
from lionagi.session.signal import NodeEscalated, NodeSpawned


def _session(**ops):
    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    for name, fn in ops.items():
        session.register_operation(name, fn)
    return session


# higher_tier: re-spawns the op


@pytest.mark.asyncio
async def test_escalation_higher_tier_spawns_child():
    """An accepted higher-tier child is scheduled and runs to completion."""
    executed: list[str] = []

    async def cheap(instruction: str = "", **kw):
        executed.append(instruction)
        if instruction.startswith("[escalation]"):
            return "done by higher tier"
        return EscalationRequest(reason="too hard", context={"route": "higher_tier"})

    session = _session(cheap=cheap)

    builder = OperationGraphBuilder()
    builder.add_operation("cheap", instruction="original request")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True)

    assert executed == [
        "original request",
        "[escalation] too hard\nOriginal: original request",
    ]
    assert len(result["escalated_operations"]) >= 1
    assert result["spawned_operations"] >= 1
    assert len(result["completed_operations"]) == 2
    assert "done by higher tier" in result["operation_results"].values()


@pytest.mark.asyncio
async def test_escalation_retry_replaces_emitter_for_downstream_context():
    """The retry is spliced between the emitter and its existing dependent."""

    async def cheap(instruction: str = "", **kw):
        if instruction.startswith("[escalation]"):
            return "answer from escalation retry"
        return EscalationRequest(reason="needs a stronger model", urgency="blocked")

    async def downstream(context: dict[str, Any] | None = None, **kw):
        return dict(context or {})

    session = _session(cheap=cheap, downstream=downstream)
    builder = OperationGraphBuilder()
    emitter_id = builder.add_operation(
        "cheap",
        node_id="cheap",
        depends_on=[],
        instruction="original request",
    )
    downstream_id = builder.add_operation(
        "downstream",
        node_id="downstream",
        depends_on=[emitter_id],
    )
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True)

    retry = next(
        node
        for node in graph.internal_nodes.values()
        if isinstance(node, Operation) and node.metadata.get("escalated_from") == str(emitter_id)
    )
    assert graph.predecessor_ids(retry) == (emitter_id,)
    assert graph.successor_ids(emitter_id) == (retry.id,)
    assert graph.predecessor_ids(downstream_id) == (retry.id,)

    downstream_context = result["operation_results"][downstream_id]
    assert downstream_context == {f"{retry.id}_result": "answer from escalation retry"}
    assert f"{emitter_id}_result" not in downstream_context


class _ResultEquals(Condition):
    """Edge condition gating on the exact string an upstream op produced."""

    def __init__(self, expected: str) -> None:
        self.expected = expected

    async def apply(self, context: dict) -> bool:
        return context.get("result") == self.expected


@pytest.mark.asyncio
async def test_escalation_retry_result_reaches_conditional_dependent():
    """A dependent gated on the emitter's result must evaluate the RETRY's
    result, not the pre-escalation value: the dependent's task starts (and
    resolves which incoming edge to wait on) before the emitter even runs, so
    it is already waiting on the emitter's completion by the time the retry
    is spliced in. It must still end up reading the retry's result rather
    than the stale one it snapshotted its wait around."""

    async def cheap(instruction: str = "", **kw):
        if instruction.startswith("[escalation]"):
            return "retry"
        return EscalationRequest(reason="needs a stronger model", urgency="blocked")

    async def downstream(**kw):
        return "downstream ran"

    session = _session(cheap=cheap, downstream=downstream)
    builder = OperationGraphBuilder()
    emitter_id = builder.add_operation(
        "cheap",
        node_id="cheap",
        depends_on=[],
        instruction="original request",
    )
    downstream_id = builder.add_operation(
        "downstream",
        node_id="downstream",
        depends_on=[],
    )
    graph = builder.get_graph()
    graph.add_edge(
        Edge(
            head=emitter_id,
            tail=downstream_id,
            condition=_ResultEquals("retry"),
            label=["depends_on"],
        )
    )

    result = await flow(session, graph, reactive=True)

    assert downstream_id not in result["skipped_operations"]
    assert result["operation_results"][downstream_id] == "downstream ran"


@pytest.mark.asyncio
async def test_escalation_retry_inherits_the_emitter_branch_not_the_session_default():
    """The retry's authority is the emitter's, and no wider.

    The retry clones the emitter's branch rather than the session default.
    The default is the controller's branch, whose catalog includes the job
    control the leaf projection exists to withhold, so falling back to it
    would widen what the retry can reach rather than narrow it. Nothing else
    holds that invariant: `independent` reaches graph dependency only, and
    under `splice_after_emitter` not even that, so a future change routing
    this through the default would pass every other test in this module."""

    # Unannotated on purpose: this module carries `from __future__ import
    # annotations`, so an annotated signature reaches the schema builder as
    # strings and raises there instead of anywhere near the claim under test.
    def emitter_only_tool(x):
        """Registered on the emitter's own branch only."""
        return x

    async def cheap(instruction: str = "", **kw):
        if instruction.startswith("[escalation]"):
            return "answer from escalation retry"
        return EscalationRequest(reason="needs a stronger model", urgency="blocked")

    session = _session(cheap=cheap)
    emitter_branch = Branch(name="cheap-branch")
    emitter_branch.register_tools(emitter_only_tool)
    emitter_branch.msgs.add_message(instruction="prior turn only the emitter should see")
    session.include_branches(emitter_branch)

    builder = OperationGraphBuilder()
    emitter_id = builder.add_operation(
        "cheap",
        node_id="cheap",
        instruction="original request",
        branch=emitter_branch,
    )
    graph = builder.get_graph()

    await flow(session, graph, reactive=True)

    retry = next(
        node
        for node in graph.internal_nodes.values()
        if isinstance(node, Operation) and node.metadata.get("escalated_from") == str(emitter_id)
    )
    # Must-not-match control: the emitter's branch carries the tool and the
    # session default does not, so the two candidate bases are actually
    # distinguishable by this assertion.
    assert "emitter_only_tool" in emitter_branch.tools
    assert "emitter_only_tool" not in session.default_branch.tools

    retry_branch = session.branches[retry.branch_id]
    assert retry_branch is not emitter_branch
    assert retry_branch.metadata["clone_from"] is emitter_branch
    assert "emitter_only_tool" in retry_branch.tools


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


@pytest.mark.asyncio
async def test_escalation_child_carries_readable_name_and_parent_pointer():
    """The higher_tier child op is stamped with both `escalated_from` (the
    parent op id) and `escalated_from_name` (a readable label) — a caller
    outside operations/ (e.g. attributing the child's CLI transcript back to
    the node it retries) needs the label without re-deriving it from a
    possibly-stale reference to the parent op."""

    attempts = 0
    spawned_signals: list[NodeSpawned] = []
    progress_events: list[tuple[str, str, str]] = []

    async def cheap(**kw):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return EscalationRequest(reason="too hard", context={"route": "higher_tier"})
        return "completed after retry"

    session = _session(cheap=cheap)
    session.observe(NodeSpawned, handler=lambda signal, _: spawned_signals.append(signal))

    builder = OperationGraphBuilder()
    parent_id = builder.add_operation("cheap", node_id="cheap")
    graph = builder.get_graph()

    await flow(
        session,
        graph,
        reactive=True,
        on_progress=lambda op_id, name, status, _elapsed: progress_events.append(
            (op_id, name, status)
        ),
    )

    child = next(
        n
        for n in graph.internal_nodes.values()
        if isinstance(n, Operation) and n.metadata.get("escalated_from") == str(parent_id)
    )
    assert child.metadata["escalated_from_name"] == "cheap"
    assert child.metadata["display_name"] == "cheap escalation retry"
    assert child.metadata["reference_id"] == str(child.id)
    assert (str(child.id), "cheap escalation retry", "queued") in progress_events
    assert len(spawned_signals) == 1
    assert spawned_signals[0].parent_id == child.metadata["escalated_from"] == str(parent_id)
    assert spawned_signals[0].independent is False


@pytest.mark.asyncio
async def test_repeated_escalations_have_unique_retrievable_identity():
    """Two retries of one node must not collapse onto one join key."""

    async def cheap(instruction: str = "", **kw: Any):
        if instruction.startswith("[escalation]"):
            return "completed after retry"
        return [
            EscalationRequest(reason="try specialist", context={"route": "higher_tier"}),
            EscalationRequest(reason="try expert", context={"route": "higher_tier"}),
        ]

    session = _session(cheap=cheap)
    builder = OperationGraphBuilder()
    parent_id = builder.add_operation("cheap", node_id="cheap", instruction="original")

    await flow(session, builder.get_graph(), reactive=True)

    children = [
        node
        for node in builder.get_graph().internal_nodes.values()
        if isinstance(node, Operation) and node.metadata.get("escalated_from") == str(parent_id)
    ]

    assert len(children) == 2
    assert {child.metadata["escalated_from"] for child in children} == {str(parent_id)}
    assert {child.metadata["display_name"] for child in children} == {"cheap escalation retry"}

    reference_ids = [child.metadata["reference_id"] for child in children]
    assert len(set(reference_ids)) == 2
    assert all(
        builder.get_node_by_reference(reference_id) is child
        for reference_id, child in zip(reference_ids, children, strict=True)
    )


@pytest.mark.asyncio
async def test_escalation_splice_does_not_fail_a_dependent_waiting_on_another_upstream():
    """A dependent with two upstreams resolves its edges once and waits on
    the first-listed head. If the other upstream escalates during that wait,
    the splice removes its original edge; the dependent must still evaluate
    the retry's result rather than fail on the removed edge."""

    async def slow(**kw: Any):
        await asyncio.sleep(0.2)
        return "slow done"

    async def cheap(instruction: str = "", **kw: Any):
        if instruction.startswith("[escalation]"):
            return "retry"
        return EscalationRequest(reason="blocked", urgency="blocked")

    async def downstream(**kw: Any):
        return "downstream ran"

    session = _session(slow=slow, cheap=cheap, downstream=downstream)
    builder = OperationGraphBuilder()
    slow_id = builder.add_operation("slow", node_id="slow", depends_on=[])
    emitter_id = builder.add_operation("cheap", node_id="cheap", depends_on=[], instruction="x")
    downstream_id = builder.add_operation("downstream", node_id="downstream", depends_on=[])
    graph = builder.get_graph()
    graph.add_edge(Edge(head=slow_id, tail=downstream_id, label=["depends_on"]))
    graph.add_edge(Edge(head=emitter_id, tail=downstream_id, condition=_ResultEquals("retry")))

    result = await flow(session, graph, reactive=True)

    assert downstream_id not in result["skipped_operations"]
    assert result["operation_results"][downstream_id] == "downstream ran"


@pytest.mark.asyncio
async def test_repeated_escalations_chain_and_the_dependent_gates_on_its_predecessor():
    """Two escalations of one node form emitter -> retry 1 -> retry 2 ->
    dependent, and the dependent's condition reads the result of the node
    that actually precedes it in the graph."""
    order: list[str] = []

    async def cheap(instruction: str = "", **kw: Any):
        if instruction.startswith("[escalation] first"):
            await asyncio.sleep(0.3)
            order.append("retry 1 done")
            return "retry-1"
        if instruction.startswith("[escalation] second"):
            order.append("retry 2 done")
            return "retry-2"
        await asyncio.sleep(0.05)  # the dependent is already waiting on this node
        return [
            EscalationRequest(reason="first", context={"route": "higher_tier"}),
            EscalationRequest(reason="second", context={"route": "higher_tier"}),
        ]

    async def downstream(**kw: Any):
        order.append("downstream ran")
        return "downstream ran"

    session = _session(cheap=cheap, downstream=downstream)
    builder = OperationGraphBuilder()
    emitter_id = builder.add_operation("cheap", node_id="cheap", depends_on=[], instruction="x")
    downstream_id = builder.add_operation("downstream", node_id="downstream", depends_on=[])
    graph = builder.get_graph()
    graph.add_edge(Edge(head=emitter_id, tail=downstream_id, condition=_ResultEquals("retry-2")))

    result = await flow(session, graph, reactive=True)

    pred_ids = [
        graph.internal_edges[eid].head for eid in graph.node_edge_mapping[downstream_id]["in"]
    ]
    assert len(pred_ids) == 1
    assert result["operation_results"][pred_ids[0]] == "retry-2"
    assert downstream_id not in result["skipped_operations"]
    assert order.index("downstream ran") > order.index("retry 2 done")
    # the second retry follows the first, not the emitter
    (retry_2,) = pred_ids
    retry_2_preds = [
        graph.internal_edges[eid].head for eid in graph.node_edge_mapping[retry_2]["in"]
    ]
    assert retry_2_preds != [emitter_id]
    assert result["operation_results"][retry_2_preds[0]] == "retry-1"


# give_up: signals without re-spawning


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


# Default route (no explicit route key) → higher_tier


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


# Deduplication: same EscalationRequest object only processed once


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


# NodeEscalated.escalation_request is stored in a named field, not Signal.data


def test_node_escalated_request_not_in_data_field():
    """escalation_request lives in a named field, not Signal.data, so it can't re-trigger the bus."""
    req = EscalationRequest(reason="test")
    sig = NodeEscalated(op_id="x", name="x", reason="test", route="give_up", escalation_request=req)
    assert not isinstance(sig.data, EscalationRequest)
    assert sig.escalation_request is req


# SpawnRequest and EscalationRequest coexist without interference


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


# Bus-based emission path (observe fires _on_bus_escalation)


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


# Fire-and-forget signal delivery: observer failure never changes the flow
# result, and the executor's detached-task set drains after the run.


# Help-signal urgency: urgency="fyi" -> "notify" route, non-hanging


@pytest.mark.asyncio
async def test_help_signal_fyi_defaults_to_notify_route():
    """A soft ('fyi') EscalationRequest defaults to route='notify': NodeEscalated
    still fires for visibility, but the op is neither retried nor marked
    escalated — the help-signal channel is orthogonal to the op's own
    completion (an unanswered help signal must never hang the worker)."""
    escalated_signals: list[NodeEscalated] = []

    async def chatty(**kw):
        return EscalationRequest(reason="fyi, just letting you know", urgency="fyi")

    session = _session(chatty=chatty)
    session.observe(NodeEscalated, handler=lambda s, _: escalated_signals.append(s))

    builder = OperationGraphBuilder()
    builder.add_operation("chatty")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True)
    await asyncio.sleep(0)

    assert len(escalated_signals) >= 1
    assert escalated_signals[0].route == "notify"
    assert result["spawned_operations"] == 0
    assert result["escalated_operations"] == []


@pytest.mark.asyncio
async def test_help_signal_blocked_urgency_preserves_higher_tier_default():
    """Back-compat: urgency='blocked' (the default) preserves the historical
    higher_tier retry when no explicit route is given."""

    async def stuck(**kw):
        return EscalationRequest(reason="blocked", urgency="blocked")

    session = _session(stuck=stuck)

    builder = OperationGraphBuilder()
    builder.add_operation("stuck")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True)

    assert result["spawned_operations"] >= 1


@pytest.mark.asyncio
async def test_help_signal_explicit_route_overrides_urgency_default():
    """An explicit context['route'] always wins over the urgency-derived default."""

    async def soft_but_explicit_giveup(**kw):
        return EscalationRequest(
            reason="fyi but give up anyway",
            urgency="fyi",
            context={"route": "give_up"},
        )

    session = _session(soft_but_explicit_giveup=soft_but_explicit_giveup)

    builder = OperationGraphBuilder()
    builder.add_operation("soft_but_explicit_giveup")
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True)

    assert result["spawned_operations"] == 0
    assert len(result["escalated_operations"]) >= 1


@pytest.mark.asyncio
async def test_escalation_observer_failure_does_not_change_flow_result(caplog, monkeypatch):
    """A raising session.emit() (NodeEscalated observer) is consumed and logged;
    the escalation routing outcome is unaffected."""

    async def failing_emit(self, event):
        raise RuntimeError("observer boom")

    async def cheap(**kw):
        return EscalationRequest(reason="stuck", context={"route": "higher_tier"})

    session = _session(cheap=cheap)
    monkeypatch.setattr(Session, "emit", failing_emit)

    builder = OperationGraphBuilder()
    builder.add_operation("cheap")
    graph = builder.get_graph()

    executor_ref: dict[str, Any] = {}
    with caplog.at_level(logging.WARNING, logger="lionagi.operations.flow"):
        result = await flow(session, graph, reactive=True, executor_ref=executor_ref)
        # Give the detached emission task(s) a moment to run and clean up.
        for _ in range(50):
            if not executor_ref["executor"]._signal_tasks:
                break
            await asyncio.sleep(0.01)

    assert len(result["escalated_operations"]) >= 1
    assert result["spawned_operations"] >= 1
    assert executor_ref["executor"]._signal_tasks == set()
    assert any("emission failed" in r.message for r in caplog.records)
