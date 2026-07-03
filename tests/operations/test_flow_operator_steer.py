# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Operator-message render slot tests (ADR-0088 slice 1, Mode A).

Covers: the labeled [OPERATOR STEER] block renders into the next op's
instruction and is lifted out of the raw context dict; consume-once so a
steer renders into exactly one downstream op; and an acceptance check that
the rendered text reaches the provider-bound payload, not just an internal
field.
"""

from __future__ import annotations

import json

import pytest

from lionagi.operations.flow import DependencyAwareExecutor
from lionagi.operations.node import Operation, create_operation
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.graph.graph import Graph
from lionagi.session.session import Session
from lionagi.testing import TestBranch

# ---------------------------------------------------------------------------
# Unit: render + lift-out (operates directly on _prepare_operation)
# ---------------------------------------------------------------------------


def test_operator_message_renders_labeled_block_into_instruction():
    session = Session()
    graph = Graph()
    op = Operation(operation="operate", parameters={"instruction": "do the task"})
    graph.add_node(op)

    executor = DependencyAwareExecutor(
        session=session,
        graph=graph,
        context={
            "operator_messages": [{"ts": 1720000000.0, "text": "change target language to Rust"}]
        },
    )
    executor._prepare_operation(op)

    instruction = op.parameters["instruction"]
    assert instruction.startswith("[OPERATOR STEER]")
    assert "change target language to Rust" in instruction
    assert instruction.endswith("do the task")
    assert "[/OPERATOR STEER]" in instruction


def test_operator_message_absent_from_raw_context_after_render():
    session = Session()
    graph = Graph()
    op = Operation(operation="operate", parameters={"instruction": "do the task"})
    graph.add_node(op)

    executor = DependencyAwareExecutor(
        session=session,
        graph=graph,
        context={"operator_messages": [{"ts": 1.0, "text": "steer left"}]},
    )
    executor._prepare_operation(op)

    ctx = op.parameters["context"]
    assert "operator_messages" not in ctx


def test_operator_message_records_rendered_into_op_breadcrumb():
    session = Session()
    graph = Graph()
    op = Operation(operation="operate", parameters={"instruction": "do the task"})
    graph.add_node(op)

    executor = DependencyAwareExecutor(
        session=session,
        graph=graph,
        context={"operator_messages": [{"ts": 1.0, "text": "steer left"}]},
    )
    executor._prepare_operation(op)

    assert op.metadata["rendered_into_op"] == str(op.id)
    entry = executor.context.content["operator_messages"][0]
    assert entry["rendered_into_op"] == str(op.id)


def test_no_operator_messages_leaves_instruction_and_context_untouched():
    session = Session()
    graph = Graph()
    op = Operation(
        operation="operate", parameters={"instruction": "do the task", "context": {"k": "v"}}
    )
    graph.add_node(op)

    executor = DependencyAwareExecutor(session=session, graph=graph)
    executor._prepare_operation(op)

    assert op.parameters["instruction"] == "do the task"
    assert op.parameters["context"] == {"k": "v"}
    assert "rendered_into_op" not in op.metadata


# ---------------------------------------------------------------------------
# Consume-once: a steer renders into exactly one downstream op
# ---------------------------------------------------------------------------


def test_consume_once_second_op_does_not_rerender_first_message():
    session = Session()
    graph = Graph()
    op1 = Operation(operation="operate", parameters={"instruction": "op1 task"})
    op2 = Operation(operation="operate", parameters={"instruction": "op2 task"})
    graph.add_node(op1)
    graph.add_node(op2)

    executor = DependencyAwareExecutor(
        session=session,
        graph=graph,
        context={"operator_messages": [{"ts": 1.0, "text": "first steer"}]},
    )

    executor._prepare_operation(op1)
    assert "first steer" in op1.parameters["instruction"]

    executor._prepare_operation(op2)
    assert "first steer" not in op2.parameters["instruction"]
    assert op2.parameters["instruction"] == "op2 task"
    assert "rendered_into_op" not in op2.metadata


def test_consume_once_new_message_queued_between_ops_renders_into_next_op_only():
    session = Session()
    graph = Graph()
    op1 = Operation(operation="operate", parameters={"instruction": "op1 task"})
    op2 = Operation(operation="operate", parameters={"instruction": "op2 task"})
    graph.add_node(op1)
    graph.add_node(op2)

    executor = DependencyAwareExecutor(
        session=session,
        graph=graph,
        context={"operator_messages": [{"ts": 1.0, "text": "first steer"}]},
    )

    executor._prepare_operation(op1)
    assert "first steer" in op1.parameters["instruction"]

    # Simulate the control poller appending a new message mid-run (ADR-0085
    # part 1 semantics) between op1's prep and op2's prep.
    existing = executor.context.content.get("operator_messages", [])
    executor.context.content["operator_messages"] = [*existing, {"ts": 2.0, "text": "second steer"}]

    executor._prepare_operation(op2)
    instr2 = op2.parameters["instruction"]
    assert "second steer" in instr2
    assert "first steer" not in instr2


# ---------------------------------------------------------------------------
# Acceptance: the rendered block reaches the provider-bound payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_steer_reaches_provider_bound_payload():
    """The scripted endpoint records the actual outbound payload — assert the
    steer block lands there, not just on an internal Operation field."""
    branch = TestBranch.from_text("draft acknowledged", name="worker")
    session = Session()
    session.include_branches(branch)
    session.default_branch = branch

    graph = Graph()
    node = create_operation("operate", parameters={"instruction": "implement feature X in Python"})
    node.branch_id = branch.id
    graph.add_node(node)

    await session.flow(
        graph,
        context={
            "operator_messages": [
                {"ts": 1720000000.0, "text": "change the target language to Rust"}
            ]
        },
        parallel=False,
    )

    calls = TestBranch.calls(branch)
    assert len(calls) == 1
    payload_text = json.dumps(calls[0].payload)
    assert "[OPERATOR STEER]" in payload_text
    assert "change the target language to Rust" in payload_text
    assert "operator_messages" not in payload_text


@pytest.mark.asyncio
async def test_operator_steer_consume_once_across_real_flow_ops():
    """Two dependent real ops: op1 sees the queued steer rendered; op2 (which
    depends on op1) must not see it re-rendered in its own payload."""
    branch = TestBranch.from_text(["op1 done", "op2 done"], name="worker")
    session = Session()
    session.include_branches(branch)
    session.default_branch = branch

    graph = Graph()
    op1 = create_operation("operate", parameters={"instruction": "op1 task"})
    op2 = create_operation("operate", parameters={"instruction": "op2 task"})
    op1.branch_id = branch.id
    op2.branch_id = branch.id
    graph.add_node(op1)
    graph.add_node(op2)
    graph.add_edge(Edge(head=op1.id, tail=op2.id))

    await session.flow(
        graph,
        context={"operator_messages": [{"ts": 1.0, "text": "steer once"}]},
        parallel=False,
    )

    calls = TestBranch.calls(branch)
    assert len(calls) == 2
    assert "[OPERATOR STEER]" in (calls[0].last_user_message or "")
    # op2's own (latest) turn must not re-render the steer; it may still
    # appear earlier in the same branch's conversation history from op1's turn.
    assert "[OPERATOR STEER]" not in (calls[1].last_user_message or "")


# ---------------------------------------------------------------------------
# Regression (issue 1681): a steer landing after prepare, before invoke
# ---------------------------------------------------------------------------


def test_steer_appended_after_prepare_is_caught_by_invoke_time_recheck():
    """Reproduces the race: op2's prepare runs while the queue is still
    empty (nothing to render), then a steer lands in the shared context
    (simulating a control-plane poller landing mid-run). Without an
    invoke-time recheck this steer would be silently dropped for the rest
    of the flow; the recheck must catch it right before the provider call."""
    session = Session()
    graph = Graph()
    op1 = Operation(operation="operate", parameters={"instruction": "op1 task"})
    op2 = Operation(operation="operate", parameters={"instruction": "op2 task"})
    graph.add_node(op1)
    graph.add_node(op2)
    graph.add_edge(Edge(head=op1.id, tail=op2.id))

    executor = DependencyAwareExecutor(session=session, graph=graph)

    executor._prepare_operation(op1)
    assert "rendered_into_op" not in op1.metadata

    # op2 is prepared while the queue is still empty — the current design's
    # single prepare-time render sees nothing.
    executor._prepare_operation(op2)
    assert op2.parameters["instruction"] == "op2 task"
    assert "rendered_into_op" not in op2.metadata

    # A steer lands after op2's prepare already ran, mirroring the control
    # poller appending mid-run (ADR-0085 part 1) after op2's slot passed.
    executor.context.content["operator_messages"] = [{"ts": 1.0, "text": "late steer"}]

    # The invoke-time recheck (called by _execute_operation immediately
    # before `await operation.invoke()`) must still catch it.
    executor._render_pending_operator_steers(op2)

    assert "[OPERATOR STEER]" in op2.parameters["instruction"]
    assert "late steer" in op2.parameters["instruction"]
    assert op2.metadata["rendered_into_op"] == str(op2.id)

    # Consume-once: a third op prepared afterwards must not re-render it.
    op3 = Operation(operation="operate", parameters={"instruction": "op3 task"})
    graph.add_node(op3)
    graph.add_edge(Edge(head=op2.id, tail=op3.id))
    executor._prepare_operation(op3)
    executor._render_pending_operator_steers(op3)
    assert op3.parameters["instruction"] == "op3 task"
    assert "rendered_into_op" not in op3.metadata


def test_no_pending_steer_leaves_recheck_a_noop():
    session = Session()
    graph = Graph()
    op = Operation(operation="operate", parameters={"instruction": "do the task"})
    graph.add_node(op)

    executor = DependencyAwareExecutor(session=session, graph=graph)
    executor._prepare_operation(op)
    executor._render_pending_operator_steers(op)

    assert op.parameters["instruction"] == "do the task"
    assert "rendered_into_op" not in op.metadata


@pytest.mark.asyncio
async def test_operator_steer_injected_at_own_started_callback_still_renders():
    """Acceptance: inject the steer from op2's own ``on_progress("started")``
    callback — which fires right after op2's prepare and right before its
    invoke — deterministically simulating the window a control-plane poller
    can land in. The rendered block must still reach the provider payload."""
    branch = TestBranch.from_text(["op1 done", "op2 done"], name="worker")
    session = Session()
    session.include_branches(branch)
    session.default_branch = branch

    graph = Graph()
    op1 = create_operation("operate", parameters={"instruction": "op1 task"})
    op2 = create_operation("operate", parameters={"instruction": "op2 task"})
    op1.branch_id = branch.id
    op2.branch_id = branch.id
    graph.add_node(op1)
    graph.add_node(op2)
    graph.add_edge(Edge(head=op1.id, tail=op2.id))

    executor_ref: dict = {}
    injected = False

    def on_progress(op_id, name, status, elapsed):
        nonlocal injected
        if injected or status != "started" or op_id != str(op2.id):
            return
        executor = executor_ref.get("executor")
        if executor is not None:
            existing = executor.context.content.get("operator_messages", [])
            executor.context.content["operator_messages"] = [
                *existing,
                {"ts": 1.0, "text": "steer during op2 start"},
            ]
            injected = True

    await session.flow(
        graph,
        on_progress=on_progress,
        executor_ref=executor_ref,
        parallel=False,
    )

    assert injected
    assert op2.metadata.get("rendered_into_op") == str(op2.id)

    calls = TestBranch.calls(branch)
    assert len(calls) == 2
    payload_text = json.dumps(calls[1].payload)
    assert "[OPERATOR STEER]" in payload_text
    assert "steer during op2 start" in payload_text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
