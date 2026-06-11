# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for edge condition handling in flow execution.

These tests ensure that edge conditions control path traversal correctly:
- Operations with false edge conditions are SKIPPED, not FAILED
- Skipped operations don't appear in completed_operations
- Edge conditions use edge.check_condition() for consistent behavior
"""

import pytest

from lionagi.operations.flow import flow
from lionagi.operations.node import Operation
from lionagi.protocols.generic.event import EventStatus
from lionagi.protocols.graph.edge import Edge, EdgeCondition
from lionagi.protocols.graph.graph import Graph
from lionagi.session.branch import Branch
from lionagi.session.session import Session


class CustomCondition(EdgeCondition):
    """Custom condition that checks for a specific value."""

    def __init__(self, expected_value):
        super().__init__()
        self.expected_value = expected_value

    async def apply(self, context: dict) -> bool:
        """Check if context matches expected value."""
        exec_context = context.get("context", {})
        return exec_context.get("test_value") == self.expected_value


@pytest.mark.asyncio
async def test_edge_condition_controls_traversal():
    """
    REGRESSION TEST: Edge conditions should control path traversal.

    Previously, false edge conditions would cause operations to fail with error.
    Now, they should be skipped without executing.
    """
    # Create operations
    start = Operation(operation="chat", parameters={"instruction": "Start"})
    path_true = Operation(operation="chat", parameters={"instruction": "True path"})
    path_false = Operation(operation="chat", parameters={"instruction": "False path"})

    # Build graph with conditional edges
    graph = Graph()
    graph.add_node(start)
    graph.add_node(path_true)
    graph.add_node(path_false)

    # Path with true condition
    graph.add_edge(Edge(head=start.id, tail=path_true.id, condition=CustomCondition(True)))

    # Path with false condition
    graph.add_edge(Edge(head=start.id, tail=path_false.id, condition=CustomCondition(False)))

    # Create session
    session = Session()
    branch = Branch(user="test", name="test")
    session.branches.include(branch)
    session.default_branch = branch

    # Execute flow
    result = await flow(session, graph, context={"test_value": True}, verbose=False)

    # Verify correct behavior
    assert start.id in result["completed_operations"], "Start should complete"
    assert path_true.id in result["completed_operations"], "True path should complete"
    assert path_false.id not in result["completed_operations"], (
        "False path should NOT be in completed"
    )

    # Key regression check: false path should be skipped, not failed
    assert path_false.id in result.get("skipped_operations", []), "False path should be skipped"

    # Verify no error for skipped operation
    if path_false.id in result["operation_results"]:
        result_value = result["operation_results"][path_false.id]
        assert not isinstance(result_value, dict) or "error" not in result_value, (
            "Skipped operation should not have error result"
        )

    # Verify status is SKIPPED
    assert path_false.execution.status == EventStatus.SKIPPED, (
        "False path should have SKIPPED status"
    )


@pytest.mark.asyncio
async def test_no_overlap_completed_skipped():
    """
    REGRESSION TEST: Operations cannot be both completed and skipped.

    The validation should ensure no operation appears in both lists.
    """
    # Create simple conditional graph
    op1 = Operation(operation="chat", parameters={"instruction": "Op1"})
    op2 = Operation(operation="chat", parameters={"instruction": "Op2"})

    graph = Graph()
    graph.add_node(op1)
    graph.add_node(op2)

    # Always false condition
    class AlwaysFalse(EdgeCondition):
        async def apply(self, context: dict) -> bool:
            return False

    graph.add_edge(Edge(head=op1.id, tail=op2.id, condition=AlwaysFalse()))

    session = Session()
    branch = Branch(user="test", name="test")
    session.branches.include(branch)
    session.default_branch = branch

    result = await flow(session, graph, verbose=False)

    # Check no overlap
    completed = set(result["completed_operations"])
    skipped = set(result.get("skipped_operations", []))

    overlap = completed & skipped
    assert not overlap, f"Operations {overlap} appear in both completed and skipped!"


@pytest.mark.asyncio
async def test_cascading_skip():
    """
    REGRESSION TEST: Skipped operations should cascade to their dependents.

    If A->B->C and A->B has false condition, both B and C should be skipped.
    """
    op_a = Operation(operation="chat", parameters={"instruction": "A"})
    op_b = Operation(operation="chat", parameters={"instruction": "B"})
    op_c = Operation(operation="chat", parameters={"instruction": "C"})

    graph = Graph()
    graph.add_node(op_a)
    graph.add_node(op_b)
    graph.add_node(op_c)

    # A->B with false condition
    class AlwaysFalse(EdgeCondition):
        async def apply(self, context: dict) -> bool:
            return False

    graph.add_edge(Edge(head=op_a.id, tail=op_b.id, condition=AlwaysFalse()))

    # B->C with no condition
    graph.add_edge(Edge(head=op_b.id, tail=op_c.id))

    session = Session()
    branch = Branch(user="test", name="test")
    session.branches.include(branch)
    session.default_branch = branch

    result = await flow(session, graph, verbose=False)

    # A should complete
    assert op_a.id in result["completed_operations"]

    # B should be skipped due to false condition
    assert op_b.id not in result["completed_operations"]
    assert op_b.id in result.get("skipped_operations", [])

    # C should also be skipped (cascade)
    assert op_c.id not in result["completed_operations"]
    assert op_c.id in result.get("skipped_operations", [])


@pytest.mark.asyncio
async def test_none_condition_always_traverses():
    """
    REGRESSION TEST: Edges with None condition should always be traversed.

    Using edge.check_condition() ensures None conditions return True.
    """
    op1 = Operation(operation="chat", parameters={"instruction": "Op1"})
    op2 = Operation(operation="chat", parameters={"instruction": "Op2"})

    graph = Graph()
    graph.add_node(op1)
    graph.add_node(op2)

    # Edge with None condition (should always traverse)
    graph.add_edge(Edge(head=op1.id, tail=op2.id, condition=None))

    session = Session()
    branch = Branch(user="test", name="test")
    session.branches.include(branch)
    session.default_branch = branch

    result = await flow(session, graph, verbose=False)

    # Both operations should complete
    assert op1.id in result["completed_operations"]
    assert op2.id in result["completed_operations"]
    assert op2.id not in result.get("skipped_operations", [])


@pytest.mark.asyncio
async def test_validation_catches_invalid_conditions():
    """
    REGRESSION TEST: Invalid edge conditions should be caught during validation.
    """
    op1 = Operation(operation="chat", parameters={"instruction": "Op1"})
    op2 = Operation(operation="chat", parameters={"instruction": "Op2"})

    graph = Graph()
    graph.add_node(op1)
    graph.add_node(op2)

    # Create edge with invalid condition type
    edge = Edge(head=op1.id, tail=op2.id)
    # Manually set invalid condition (bypassing constructor validation)
    edge.properties["condition"] = "not_a_condition"  # Invalid type
    graph.add_edge(edge)

    session = Session()
    branch = Branch(user="test", name="test")
    session.branches.include(branch)
    session.default_branch = branch

    # Should raise TypeError during validation
    with pytest.raises(TypeError, match="invalid condition type"):
        await flow(session, graph, verbose=False)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
