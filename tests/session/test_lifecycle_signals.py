# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the per-node lifecycle signal contract (ADR-0083): lane_for projection, new signal types, engine bridge, reactive injection."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lionagi.session.signal import (
    GateDenied,
    MessageAdded,
    NodeAwaitingApproval,
    NodeCompleted,
    NodeEscalated,
    NodeFailed,
    NodePaused,
    NodeQueued,
    NodeStarted,
    RunEnd,
    RunFailed,
    RunStart,
    Signal,
    StructuredOutput,
    lane_for,
)

# ---------------------------------------------------------------------------
# lane_for unit tests
# ---------------------------------------------------------------------------


def test_lane_for_empty_stream():
    assert lane_for([]) == "queued"


def test_lane_for_non_state_bearing_only():
    assert lane_for([GateDenied(), MessageAdded()]) == "queued"


def test_lane_for_queued():
    assert lane_for([NodeQueued(op_id="a", name="a")]) == "queued"


def test_lane_for_running_via_node_started():
    assert lane_for([NodeStarted(op_id="a", name="a")]) == "running"


def test_lane_for_running_via_run_start():
    assert lane_for([RunStart()]) == "running"


def test_lane_for_awaiting_approval():
    assert lane_for([NodeStarted(), NodeAwaitingApproval()]) == "awaiting_approval"


def test_lane_for_paused():
    assert lane_for([NodeStarted(), NodePaused()]) == "paused"


def test_lane_for_paused_resets_to_running_on_node_started():
    """NodeStarted after NodePaused resets the lane to running (resume + execution began)."""
    signals = [NodeStarted(), NodePaused(), NodeStarted()]
    assert lane_for(signals) == "running"


def test_lane_for_succeeded_via_node_completed():
    assert lane_for([NodeStarted(), NodeCompleted()]) == "succeeded"


def test_lane_for_succeeded_via_run_end():
    assert lane_for([RunStart(), RunEnd()]) == "succeeded"


def test_lane_for_failed_via_node_failed():
    assert lane_for([NodeStarted(), NodeFailed()]) == "failed"


def test_lane_for_failed_via_run_failed():
    assert lane_for([RunStart(), RunFailed()]) == "failed"


def test_lane_for_escalated_via_node_escalated():
    sig = NodeEscalated(op_id="x", name="x", reason="out of depth", route="give_up")
    assert lane_for([NodeStarted(), sig]) == "escalated"


def test_lane_for_escalated_via_structured_output():
    """StructuredOutput carrying an EscalationRequest projects to 'escalated'."""
    from lionagi.casts.emission import EscalationRequest

    req = EscalationRequest(reason="too hard")
    so = StructuredOutput(data=req)
    assert lane_for([NodeStarted(), so]) == "escalated"


def test_lane_for_terminal_sticky_succeeded():
    signals = [NodeStarted(), NodeCompleted(), NodeFailed()]
    assert lane_for(signals) == "succeeded"


def test_lane_for_terminal_sticky_failed():
    signals = [NodeStarted(), NodeFailed(), NodeCompleted()]
    assert lane_for(signals) == "failed"


def test_lane_for_terminal_sticky_escalated():
    esc = NodeEscalated(op_id="x", name="x", reason="r", route="give_up")
    signals = [NodeStarted(), esc, NodeAwaitingApproval()]
    assert lane_for(signals) == "escalated"


def test_lane_for_terminal_sticky_ignores_paused():
    """A stray NodePaused after a terminal state (e.g. a late-arriving signal) is ignored."""
    signals = [NodeStarted(), NodeCompleted(), NodePaused()]
    assert lane_for(signals) == "succeeded"


def test_lane_for_retry_reset_from_succeeded():
    signals = [NodeStarted(), NodeCompleted(), NodeQueued()]
    assert lane_for(signals) == "queued"


def test_lane_for_retry_reset_from_failed_via_node_started():
    signals = [NodeStarted(), NodeFailed(), NodeStarted()]
    assert lane_for(signals) == "running"


def test_lane_for_latest_wins_in_non_terminal():
    signals = [NodeQueued(), NodeStarted(), NodeAwaitingApproval()]
    assert lane_for(signals) == "awaiting_approval"


def test_lane_for_full_happy_path_sequence():
    """queued → running → succeeded sequence."""
    signals = [NodeQueued(), NodeStarted(), NodeCompleted()]
    for i, (sig, expected) in enumerate(
        zip(
            [signals[:1], signals[:2], signals[:3]],
            ["queued", "running", "succeeded"],
        )
    ):
        assert lane_for(sig) == expected, f"step {i}"


# ---------------------------------------------------------------------------
# New signal type tests
# ---------------------------------------------------------------------------


def test_node_queued_fields():
    sig = NodeQueued(op_id="abc", name="myop")
    assert sig.op_id == "abc"
    assert sig.name == "myop"
    assert sig.elapsed == 0.0


def test_node_awaiting_approval_fields():
    sig = NodeAwaitingApproval(op_id="x", name="gate-op", reason="human review required")
    assert sig.reason == "human review required"


def test_node_paused_fields():
    sig = NodePaused(op_id="z", name="blocked-op")
    assert sig.op_id == "z"
    assert sig.name == "blocked-op"


def test_node_escalated_fields():
    sig = NodeEscalated(op_id="y", name="op", reason="too hard", route="higher_tier")
    assert sig.route == "higher_tier"
    assert sig.escalation_request is None  # optional


def test_node_escalated_with_request():
    from lionagi.casts.emission import EscalationRequest

    req = EscalationRequest(reason="no capacity")
    sig = NodeEscalated(
        op_id="y", name="op", reason="no capacity", route="give_up", escalation_request=req
    )
    assert sig.escalation_request is req


def test_node_escalated_request_not_payload_matched():
    """escalation_request in a named field must NOT re-trigger the bus handler.

    The observer matches on Signal.data (the generic payload field). Storing
    the EscalationRequest in a separate named field prevents re-fire.
    """
    from lionagi.casts.emission import EscalationRequest

    req = EscalationRequest(reason="test")
    sig = NodeEscalated(op_id="z", name="z", reason="test", route="give_up", escalation_request=req)
    # Signal.data must NOT be an EscalationRequest — that would re-match the handler.
    assert not isinstance(sig.data, EscalationRequest)


# ---------------------------------------------------------------------------
# Engine bridge: NodeQueued emitted via run_dag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_dag_emits_node_queued_before_started():
    from lionagi.engines import Engine
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    async def work(**kw):
        return "ok"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("work", work)

    signal_log: list[str] = []
    session.observe(NodeQueued, handler=lambda s, _: signal_log.append(f"queued:{s.op_id}"))
    session.observe(NodeStarted, handler=lambda s, _: signal_log.append(f"started:{s.op_id}"))

    builder = OperationGraphBuilder()
    builder.add_operation("work")
    graph = builder.get_graph()

    run = Engine().new_run(session=session)
    result = await run.run_dag(graph)

    assert len(result["completed_operations"]) == 1

    queued_ops = [e.split(":")[1] for e in signal_log if e.startswith("queued:")]
    started_ops = [e.split(":")[1] for e in signal_log if e.startswith("started:")]
    assert len(queued_ops) >= 1, "NodeQueued must fire"
    assert len(started_ops) >= 1, "NodeStarted must fire"
    assert queued_ops[0] == started_ops[0], "NodeQueued and NodeStarted must share op_id"

    qi = next(i for i, e in enumerate(signal_log) if e.startswith("queued:"))
    si = next(i for i, e in enumerate(signal_log) if e.startswith("started:"))
    assert qi < si, "NodeQueued must precede NodeStarted in the signal log"


# ---------------------------------------------------------------------------
# End-to-end projection: collect signals, project lanes, assert sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projection_contract_end_to_end():
    from lionagi.engines import Engine
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    async def compute(**kw):
        return 42

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("compute", compute)

    collected: dict[str, list[Signal]] = {}

    def _capture(sig: Signal, _ctx: Any) -> None:
        op_id = getattr(sig, "op_id", None) or "run"
        collected.setdefault(op_id, []).append(sig)

    for sig_type in (NodeQueued, NodeStarted, NodeCompleted, NodeFailed):
        session.observe(sig_type, handler=_capture)

    builder = OperationGraphBuilder()
    builder.add_operation("compute")
    graph = builder.get_graph()

    run = Engine().new_run(session=session)
    result = await run.run_dag(graph)

    assert len(result["completed_operations"]) == 1
    op_id = str(result["completed_operations"][0])

    assert op_id in collected, "No signals collected for the completed op"
    op_signals = collected[op_id]

    final_lane = lane_for(op_signals)
    assert final_lane == "succeeded", f"Expected succeeded, got {final_lane}"

    lanes_seen = []
    for i in range(1, len(op_signals) + 1):
        lanes_seen.append(lane_for(op_signals[:i]))

    assert "queued" in lanes_seen, "Should have passed through 'queued'"
    assert "running" in lanes_seen, "Should have passed through 'running'"
    assert lanes_seen[-1] == "succeeded", "Final state must be 'succeeded'"

    # Order: queued must come before running which must come before succeeded
    q_idx = lanes_seen.index("queued")
    r_idx = lanes_seen.index("running")
    s_idx = lanes_seen.index("succeeded")
    assert q_idx < r_idx < s_idx, f"Lane sequence wrong: {lanes_seen}"


# ---------------------------------------------------------------------------
# Reactive injection: injected children also get NodeQueued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reactive_injected_child_receives_node_queued():
    from lionagi.casts.emission import SpawnRequest
    from lionagi.engines import Engine
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.operations.node import create_operation
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    async def spawner(**kw):
        return SpawnRequest(instruction="follow-up", independent=True)

    async def follow_up(**kw):
        return "child done"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("spawner", spawner)
    session.register_operation("follow_up", follow_up)

    queued_ids: list[str] = []
    started_ids: list[str] = []
    session.observe(NodeQueued, handler=lambda s, _: queued_ids.append(s.op_id))
    session.observe(NodeStarted, handler=lambda s, _: started_ids.append(s.op_id))

    def node_builder(req: Any, emitter: Any) -> Any:
        return create_operation("follow_up", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("spawner")
    graph = builder.get_graph()

    run = Engine().new_run(session=session)
    result = await run.run_dag(graph, reactive=True, node_builder=node_builder, max_spawn=1)

    assert result["spawned_operations"] == 1
    assert len(result["completed_operations"]) == 2

    assert len(queued_ids) == 2, f"Expected 2 queued signals, got {len(queued_ids)}"

    for op_id in started_ids:
        assert op_id in queued_ids, f"op {op_id} was started without a prior NodeQueued"


# ---------------------------------------------------------------------------
# Skipped nodes project to 'failed' lane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skipped_node_projects_to_failed_lane():
    """A node skipped by an always-false edge condition emits NodeFailed → lane 'failed'.

    This is a regression guard: before the fix, the skip path did not call
    on_progress, so the node stayed in 'queued' forever.
    """
    from lionagi.engines import Engine
    from lionagi.operations.node import Operation
    from lionagi.protocols.graph.edge import Edge, EdgeCondition
    from lionagi.protocols.graph.graph import Graph
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    class AlwaysFalse(EdgeCondition):
        async def apply(self, context: dict) -> bool:
            return False

    async def root_op(**kw):
        return "root done"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("root_op", root_op)

    collected: dict[str, list[Signal]] = {}

    def _capture(sig: Signal, _ctx: Any) -> None:
        op_id = getattr(sig, "op_id", None) or "run"
        collected.setdefault(op_id, []).append(sig)

    for sig_type in (NodeQueued, NodeStarted, NodeCompleted, NodeFailed):
        session.observe(sig_type, handler=_capture)

    root = Operation(operation="root_op", parameters={})
    skipped = Operation(operation="root_op", parameters={})

    graph = Graph()
    graph.add_node(root)
    graph.add_node(skipped)
    graph.add_edge(Edge(head=root.id, tail=skipped.id, condition=AlwaysFalse()))

    run = Engine().new_run(session=session)
    result = await run.run_dag(graph)

    assert str(root.id) in [str(x) for x in result["completed_operations"]]
    assert str(skipped.id) in [str(x) for x in result.get("skipped_operations", [])]

    skipped_op_id = str(skipped.id)
    assert skipped_op_id in collected, (
        "No signals collected for the skipped op — on_progress was not called in the skip path"
    )
    assert lane_for(collected[skipped_op_id]) == "failed", (
        f"Skipped node must project to 'failed', got {lane_for(collected[skipped_op_id])}"
    )


# ---------------------------------------------------------------------------
# execute_stream subscribes via the public observer property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_stream_subscribes_spawn_via_public_observer(monkeypatch):
    """execute_stream receives SpawnRequests even when _observer was None before run.

    The public observer property is a lazy-init that always returns a SessionObserver.
    If execute_stream used the private _observer attribute it would return None when
    the session was built without prior observer access, silently dropping reactive
    spawns. This test verifies the spawn is received via a behavioral check.
    """
    from lionagi.casts.emission import SpawnRequest
    from lionagi.operations import flow_stream
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.operations.node import Operation, create_operation
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    executed: list[str] = []

    async def spawner(**kw):
        executed.append("spawner")
        return SpawnRequest(instruction="follow-up", independent=True)

    async def follow_up(**kw):
        executed.append("follow_up")
        return "done"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("spawner", spawner)
    session.register_operation("follow_up", follow_up)

    # Forcibly clear _observer to simulate an uninitialised private attr.
    # getattr(session, '_observer', None) would return None here; but
    # session.observer (the property) lazily recreates it — the correct path.
    session._observer = None

    def node_builder(req: SpawnRequest, emitter: Operation) -> Operation:
        return create_operation("follow_up", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("spawner")
    graph = builder.get_graph()

    events = []
    async for ev in flow_stream(session, graph, node_builder=node_builder):
        events.append(ev)

    assert "follow_up" in executed, (
        "execute_stream did not receive the SpawnRequest — "
        "it may have fallen back to _observer (None) instead of the public property"
    )
    assert any(e.spawned for e in events)


# ---------------------------------------------------------------------------
# Export contract
# ---------------------------------------------------------------------------


def test_session_package_exports_new_symbols():
    """New symbols are re-exported from lionagi.session."""
    import lionagi.session as sess

    for name in (
        "NodeQueued",
        "NodeAwaitingApproval",
        "NodeEscalated",
        "NodeLifecycleState",
        "lane_for",
    ):
        assert hasattr(sess, name), f"lionagi.session missing {name}"
