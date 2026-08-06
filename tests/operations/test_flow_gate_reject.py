# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Gate-reject short-circuit (issue #2860): a gate node's REJECT verdict must
stop its dependent subtree from running against the rejected baseline,
without touching independent siblings or non-gate flows."""

from __future__ import annotations

import pytest

from lionagi.operations import flow
from lionagi.operations.builder import OperationGraphBuilder
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


def _build_gate_graph(builder: OperationGraphBuilder) -> dict[str, str]:
    """producer -> gate(is_gate) -> dep1 -> dep2, plus an unrelated
    sibling -> sibling_child chain that never touches the gate."""
    producer_id = builder.add_operation("producer", depends_on=[])
    gate_id = builder.add_operation("gate", node_id="gate", depends_on=[producer_id], is_gate=True)
    dep1_id = builder.add_operation("dep1", depends_on=[gate_id])
    dep2_id = builder.add_operation("dep2", depends_on=[dep1_id])
    sibling_id = builder.add_operation("sibling", depends_on=[])
    sibling_child_id = builder.add_operation("sibling_child", depends_on=[sibling_id])
    return {
        "producer": producer_id,
        "gate": gate_id,
        "dep1": dep1_id,
        "dep2": dep2_id,
        "sibling": sibling_id,
        "sibling_child": sibling_child_id,
    }


def _make_ops(executed: list[str], *, verdict: str = "reject"):
    async def producer(**kw):
        executed.append("producer")
        return {"baseline": "v1"}

    async def gate(**kw):
        executed.append("gate")
        return {"gate_verdict": verdict, "findings": "design flaw at step 10"}

    async def dep1(**kw):
        executed.append("dep1")
        return "dep1 ran against the baseline"

    async def dep2(**kw):
        executed.append("dep2")
        return "dep2 ran against the baseline"

    async def sibling(**kw):
        executed.append("sibling")
        return "sibling ran"

    async def sibling_child(**kw):
        executed.append("sibling_child")
        return "sibling_child ran"

    return {
        "producer": producer,
        "gate": gate,
        "dep1": dep1,
        "dep2": dep2,
        "sibling": sibling,
        "sibling_child": sibling_child,
    }


@pytest.mark.asyncio
async def test_gate_reject_short_circuits_dependents_not_siblings():
    """(i) downstream nodes never execute, (ii) they carry the skip reason,
    (iii) independent siblings still execute."""
    executed: list[str] = []
    session = _session_with_ops(**_make_ops(executed))
    builder = OperationGraphBuilder()
    ids = _build_gate_graph(builder)
    graph = builder.get_graph()

    result = await flow(session, graph, parallel=False, verbose=False)

    # (i) never executed
    assert "dep1" not in executed
    assert "dep2" not in executed
    assert ids["dep1"] not in result["completed_operations"]
    assert ids["dep2"] not in result["completed_operations"]
    assert ids["dep1"] in result["skipped_operations"]
    assert ids["dep2"] in result["skipped_operations"]

    # the gate itself ran and produced its verdict
    assert "producer" in executed
    assert "gate" in executed
    assert ids["gate"] in result["completed_operations"]
    assert result["gate_rejected_operations"] == [str(ids["gate"])]
    assert sorted(result["gate_short_circuited_operations"]) == sorted(
        [str(ids["dep1"]), str(ids["dep2"])]
    )

    # (ii) skip reason surfaces on both the metadata and the result entry,
    # and the transitively-skipped dep2 still points at the ORIGINAL gate.
    for dep_key in ("dep1", "dep2"):
        node = graph.internal_nodes[ids[dep_key]]
        assert node.metadata["skip_reason_code"] == "upstream_gate_reject"
        assert node.metadata["skip_reason_gate_id"] == str(ids["gate"])
        assert node.metadata["skip_reason_gate_name"] == "gate"

        res_entry = result["operation_results"][ids[dep_key]]
        assert res_entry["skipped"] is True
        assert res_entry["reason_code"] == "upstream_gate_reject"
        assert res_entry["gate_id"] == str(ids["gate"])

    # (iii) independent siblings unaffected
    assert "sibling" in executed
    assert "sibling_child" in executed
    assert ids["sibling"] in result["completed_operations"]
    assert ids["sibling_child"] in result["completed_operations"]


@pytest.mark.asyncio
async def test_gate_reject_short_circuits_reactive_executor():
    """Same short-circuit behavior under the reactive (self-expanding) executor."""
    executed: list[str] = []
    session = _session_with_ops(**_make_ops(executed))
    builder = OperationGraphBuilder()
    ids = _build_gate_graph(builder)
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True, verbose=False)

    assert "dep1" not in executed
    assert "dep2" not in executed
    assert ids["dep1"] in result["skipped_operations"]
    assert ids["dep2"] in result["skipped_operations"]
    assert result["gate_rejected_operations"] == [str(ids["gate"])]

    assert "sibling" in executed
    assert "sibling_child" in executed


@pytest.mark.asyncio
async def test_gate_reject_is_case_insensitive():
    """The contract's reject marker tolerates case/whitespace variance."""
    executed: list[str] = []
    session = _session_with_ops(**_make_ops(executed, verdict="  REJECT  "))
    builder = OperationGraphBuilder()
    ids = _build_gate_graph(builder)
    graph = builder.get_graph()

    result = await flow(session, graph, parallel=False, verbose=False)

    assert "dep1" not in executed
    assert ids["dep1"] in result["skipped_operations"]


@pytest.mark.asyncio
async def test_non_reject_gate_verdict_changes_nothing():
    """A gate that approves (or any verdict != 'reject') must not skip anything."""
    executed: list[str] = []
    session = _session_with_ops(**_make_ops(executed, verdict="approve"))
    builder = OperationGraphBuilder()
    ids = _build_gate_graph(builder)
    graph = builder.get_graph()

    result = await flow(session, graph, parallel=False, verbose=False)

    assert "dep1" in executed
    assert "dep2" in executed
    assert result["skipped_operations"] == []
    assert result["gate_rejected_operations"] == []
    assert result["gate_short_circuited_operations"] == []


@pytest.mark.asyncio
async def test_gate_without_verdict_field_changes_nothing():
    """Design constraint #2: a gate whose result has no gate_verdict key at
    all (missing field, not just a non-reject value) leaves the flow untouched."""
    executed: list[str] = []

    async def gate_no_verdict(**kw):
        executed.append("gate")
        return {"summary": "looks fine, no structured verdict emitted"}

    ops = _make_ops(executed)
    ops["gate"] = gate_no_verdict
    session = _session_with_ops(**ops)
    builder = OperationGraphBuilder()
    ids = _build_gate_graph(builder)
    graph = builder.get_graph()

    result = await flow(session, graph, parallel=False, verbose=False)

    assert "dep1" in executed
    assert "dep2" in executed
    assert result["skipped_operations"] == []
    assert result["gate_rejected_operations"] == []


@pytest.mark.asyncio
async def test_unmarked_node_with_reject_shaped_result_is_not_a_gate():
    """A node that is NOT flagged is_gate must not trigger short-circuiting
    even if its result happens to contain a gate_verdict='reject' key --
    detection is the explicit is_gate flag, never string-sniffing the result."""
    executed: list[str] = []

    async def producer(**kw):
        executed.append("producer")
        # Coincidentally shaped like a gate reject, but this node was never
        # marked is_gate, so it must not be treated as one.
        return {"gate_verdict": "reject", "baseline": "v1"}

    async def dep1(**kw):
        executed.append("dep1")
        return "dep1 ran"

    session = _session_with_ops(producer=producer, dep1=dep1)
    builder = OperationGraphBuilder()
    producer_id = builder.add_operation("producer", depends_on=[])
    dep1_id = builder.add_operation("dep1", depends_on=[producer_id])  # NOT is_gate
    graph = builder.get_graph()

    result = await flow(session, graph, parallel=False, verbose=False)

    assert "dep1" in executed
    assert dep1_id in result["completed_operations"]
    assert result["skipped_operations"] == []
    assert result["gate_rejected_operations"] == []


@pytest.mark.asyncio
async def test_flow_with_no_gate_nodes_is_unaffected():
    """A flow with no is_gate nodes at all behaves exactly as before: the new
    result keys are present but empty, nothing is skipped."""
    executed: list[str] = []

    async def step_a(**kw):
        executed.append("a")
        return "a"

    async def step_b(**kw):
        executed.append("b")
        return "b"

    session = _session_with_ops(step_a=step_a, step_b=step_b)
    builder = OperationGraphBuilder()
    a_id = builder.add_operation("step_a", depends_on=[])
    b_id = builder.add_operation("step_b", depends_on=[a_id])
    graph = builder.get_graph()

    result = await flow(session, graph, parallel=False, verbose=False)

    assert executed == ["a", "b"]
    assert sorted(result["completed_operations"]) == sorted([a_id, b_id])
    assert result["skipped_operations"] == []
    assert result["gate_rejected_operations"] == []
    assert result["gate_short_circuited_operations"] == []
