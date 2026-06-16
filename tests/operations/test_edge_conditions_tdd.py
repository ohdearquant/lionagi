# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""TDD suite for edge conditions in flow execution — expected behavior specs and regression guards."""

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from lionagi.operations.flow import flow
from lionagi.operations.node import Operation
from lionagi.protocols.generic.event import EventStatus
from lionagi.protocols.graph.edge import Edge, EdgeCondition
from lionagi.protocols.graph.graph import Graph
from lionagi.session.branch import Branch
from lionagi.session.session import Session

# ============================================================================
# TEST FIXTURES AND HELPERS
# ============================================================================


class ConditionalEdge(EdgeCondition):
    """Edge condition that checks for a specific value in context."""

    def __init__(self, key: str, expected_value: Any):
        super().__init__()
        self.key = key
        self.expected_value = expected_value

    async def apply(self, context: dict) -> bool:
        """Check if context[key] equals expected_value."""
        # Handle both result context and execution context
        if "result" in context:
            # This is from a predecessor operation
            result = context.get("result", {})
            if isinstance(result, dict):
                return result.get(self.key) == self.expected_value

        # Check execution context
        exec_context = context.get("context", {})
        return exec_context.get(self.key) == self.expected_value


class AlwaysTrueCondition(EdgeCondition):
    """Condition that always returns True."""

    async def apply(self, context: dict) -> bool:
        return True


class AlwaysFalseCondition(EdgeCondition):
    """Condition that always returns False."""

    async def apply(self, context: dict) -> bool:
        return False


def create_mock_branch(name: str = "TestBranch") -> Branch:
    """Branch with execution-history tracking on every chat call.

    Uses ``LionAGIMockFactory.create_api_calling_mock`` for the canned response
    plumbing; only the instruction-tracking wrapper stays local to this test.
    """
    from lionagi.protocols.generic.event import EventStatus
    from lionagi.providers.openai.chat.models import OpenAIChatCompletionsRequest
    from lionagi.testing import LionAGIMockFactory

    branch = Branch(user="test_user", name=name)
    branch.metadata["execution_history"] = []

    async def _fake_invoke(**kwargs):
        instruction = kwargs.get("messages", [{}])[0].get("content", "")
        branch.metadata["execution_history"].append(("chat", instruction))
        return LionAGIMockFactory.create_api_calling_mock(
            response_data=f"Result for: {instruction}",
            status=EventStatus.COMPLETED,
            model="gpt-4-mini",
        )

    mock_chat_model = LionAGIMockFactory.create_mocked_imodel(
        provider="openai", model="gpt-4-mini", response="overridden-below"
    )
    mock_chat_model.invoke = AsyncMock(side_effect=_fake_invoke)
    branch.chat_model = mock_chat_model
    return branch


# ============================================================================
# SPECIFICATION TESTS - Define Expected Behavior
# ============================================================================


@pytest.mark.asyncio
async def test_spec_edge_condition_controls_traversal():
    """False edge condition prevents the downstream operation from executing."""
    # Setup
    start = Operation(operation="chat", parameters={"instruction": "Start"})
    path_a = Operation(operation="chat", parameters={"instruction": "Path A"})
    path_b = Operation(operation="chat", parameters={"instruction": "Path B"})

    graph = Graph()
    graph.add_node(start)
    graph.add_node(path_a)
    graph.add_node(path_b)

    # Path A: condition will be True
    graph.add_edge(
        Edge(
            head=start.id,
            tail=path_a.id,
            condition=ConditionalEdge("choice", "A"),
        )
    )

    # Path B: condition will be False
    graph.add_edge(
        Edge(
            head=start.id,
            tail=path_b.id,
            condition=ConditionalEdge("choice", "B"),
        )
    )

    session = Session()
    branch = create_mock_branch()
    session.branches.include(branch)
    session.default_branch = branch

    # Execute with choice="A"
    result = await flow(session, graph, context={"choice": "A"}, verbose=False)

    # ASSERTIONS - Expected Behavior
    # 1. Start should execute
    assert start.id in result["completed_operations"], "Start operation should complete"

    # 2. Path A should execute (condition True)
    assert path_a.id in result["completed_operations"], (
        "Path A should execute when condition is True"
    )

    # 3. Path B should NOT execute (condition False)
    assert path_b.id not in result["completed_operations"], (
        "Path B should NOT execute when condition is False"
    )

    # 4. Path B should be marked as SKIPPED, not FAILED
    if hasattr(EventStatus, "SKIPPED"):
        assert path_b.execution.status == EventStatus.SKIPPED, (
            "Path B should be SKIPPED, not FAILED"
        )

    # 5. No error should be recorded for Path B
    if path_b.id in result["operation_results"]:
        path_b_result = result["operation_results"][path_b.id]
        assert not isinstance(path_b_result, dict) or "error" not in path_b_result, (
            "Skipped operations should not have error results"
        )


@pytest.mark.asyncio
async def test_spec_multiple_conditions_any_satisfied():
    """An operation executes if ANY incoming edge condition is satisfied (OR logic)."""
    source_a = Operation(operation="chat", parameters={"instruction": "Source A"})
    source_b = Operation(operation="chat", parameters={"instruction": "Source B"})
    target = Operation(operation="chat", parameters={"instruction": "Target"})

    graph = Graph()
    for op in [source_a, source_b, target]:
        graph.add_node(op)

    # Two paths to target with different conditions
    graph.add_edge(
        Edge(
            head=source_a.id,
            tail=target.id,
            condition=AlwaysTrueCondition(),  # This path is valid
        )
    )
    graph.add_edge(
        Edge(
            head=source_b.id,
            tail=target.id,
            condition=AlwaysFalseCondition(),  # This path is invalid
        )
    )

    session = Session()
    branch = create_mock_branch()
    session.branches.include(branch)
    session.default_branch = branch

    result = await flow(session, graph, verbose=False)

    # ASSERTIONS
    # Target should execute because at least one path is valid
    assert target.id in result["completed_operations"], (
        "Target should execute when at least one incoming edge condition is True"
    )

    # Both sources should execute (they have no conditions)
    assert source_a.id in result["completed_operations"]
    assert source_b.id in result["completed_operations"]


@pytest.mark.asyncio
async def test_spec_all_conditions_must_fail_to_skip():
    """An operation is skipped only when ALL incoming edge conditions are False."""
    source = Operation(operation="chat", parameters={"instruction": "Source"})
    target = Operation(operation="chat", parameters={"instruction": "Target"})

    graph = Graph()
    graph.add_node(source)
    graph.add_node(target)

    # All paths have False conditions
    graph.add_edge(Edge(head=source.id, tail=target.id, condition=AlwaysFalseCondition()))

    session = Session()
    branch = create_mock_branch()
    session.branches.include(branch)
    session.default_branch = branch

    result = await flow(session, graph, verbose=False)

    # ASSERTIONS
    assert source.id in result["completed_operations"], "Source should execute"
    assert target.id not in result["completed_operations"], (
        "Target should be skipped when all edge conditions are False"
    )


@pytest.mark.asyncio
async def test_spec_no_condition_means_always_traverse():
    source = Operation(operation="chat", parameters={"instruction": "Source"})
    target = Operation(operation="chat", parameters={"instruction": "Target"})

    graph = Graph()
    graph.add_node(source)
    graph.add_node(target)

    # Edge with no condition
    graph.add_edge(Edge(head=source.id, tail=target.id))

    session = Session()
    branch = create_mock_branch()
    session.branches.include(branch)
    session.default_branch = branch

    result = await flow(session, graph, verbose=False)

    # ASSERTIONS
    assert source.id in result["completed_operations"]
    assert target.id in result["completed_operations"], (
        "Operations should execute when edges have no conditions"
    )


@pytest.mark.asyncio
async def test_spec_operation_state_reset_between_executions():
    """Operations do not retain execution state between flow runs."""
    op = Operation(operation="chat", parameters={"instruction": "Stateless Op"})

    graph = Graph()
    graph.add_node(op)

    session = Session()
    branch = create_mock_branch()
    session.branches.include(branch)
    session.default_branch = branch

    # First execution
    result1 = await flow(session, graph, verbose=False)
    assert op.id in result1["completed_operations"]

    # Reset operation state (this should happen automatically)
    # Currently this is a problem - operations retain state

    # Second execution should work identically
    branch2 = create_mock_branch("Branch2")
    session.branches.include(branch2)
    session.default_branch = branch2

    result2 = await flow(session, graph, verbose=False)
    assert op.id in result2["completed_operations"], (
        "Operations should execute in second flow run without state carryover"
    )


@pytest.mark.asyncio
async def test_spec_cascading_skip_propagation():
    """Skipped operations cascade: dependents without other valid paths are also skipped."""
    start = Operation(operation="chat", parameters={"instruction": "Start"})
    middle = Operation(operation="chat", parameters={"instruction": "Middle"})
    end = Operation(operation="chat", parameters={"instruction": "End"})

    graph = Graph()
    for op in [start, middle, end]:
        graph.add_node(op)

    # Start -> Middle with False condition
    graph.add_edge(Edge(head=start.id, tail=middle.id, condition=AlwaysFalseCondition()))

    # Middle -> End with no condition
    graph.add_edge(Edge(head=middle.id, tail=end.id))

    session = Session()
    branch = create_mock_branch()
    session.branches.include(branch)
    session.default_branch = branch

    result = await flow(session, graph, verbose=False)

    # ASSERTIONS
    assert start.id in result["completed_operations"], "Start should execute"
    assert middle.id not in result["completed_operations"], (
        "Middle should be skipped due to False condition"
    )
    assert end.id not in result["completed_operations"], (
        "End should be skipped because Middle (its only predecessor) was skipped"
    )


# ============================================================================
# GUARD TESTS - Prevent Specific Regressions
# ============================================================================


@pytest.mark.asyncio
async def test_guard_against_error_on_false_condition():
    """False edge conditions must not raise ValueError; previously raised 'Edge condition not satisfied'."""
    source = Operation(operation="chat", parameters={"instruction": "Source"})
    target = Operation(operation="chat", parameters={"instruction": "Target"})

    graph = Graph()
    graph.add_node(source)
    graph.add_node(target)

    graph.add_edge(Edge(head=source.id, tail=target.id, condition=AlwaysFalseCondition()))

    session = Session()
    branch = create_mock_branch()
    session.branches.include(branch)
    session.default_branch = branch

    # This should NOT raise an exception
    try:
        result = await flow(session, graph, verbose=False)
        # If we get here, no exception was raised - good!
    except ValueError as e:
        if "Edge condition not satisfied" in str(e):
            pytest.fail(f"False edge conditions should not raise ValueError: {e}")
        raise  # Re-raise other ValueErrors

    # Additional check: target should not have an error result
    if target.id in result.get("operation_results", {}):
        target_result = result["operation_results"][target.id]
        if isinstance(target_result, dict):
            assert "error" not in target_result, "Skipped operations should not have error results"


@pytest.mark.asyncio
async def test_guard_edge_check_condition_usage():
    """edge.check_condition() handles None conditions (returns True); direct apply() does not."""
    source = Operation(operation="chat", parameters={"instruction": "Source"})
    target = Operation(operation="chat", parameters={"instruction": "Target"})

    graph = Graph()
    graph.add_node(source)
    graph.add_node(target)

    # Edge with None condition (should default to True)
    edge = Edge(head=source.id, tail=target.id, condition=None)
    graph.add_edge(edge)

    session = Session()
    branch = create_mock_branch()
    session.branches.include(branch)
    session.default_branch = branch

    result = await flow(session, graph, verbose=False)

    # Target should execute (None condition = always traverse)
    assert target.id in result["completed_operations"], (
        "Operations should execute when edge condition is None"
    )


@pytest.mark.asyncio
async def test_guard_conditional_aggregation():
    """Aggregation with some conditionally-excluded inputs still completes on available sources."""
    source_a = Operation(operation="chat", parameters={"instruction": "Source A"})
    source_b = Operation(operation="chat", parameters={"instruction": "Source B"})
    source_c = Operation(operation="chat", parameters={"instruction": "Source C"})

    # Aggregation operation
    aggregator = Operation(
        operation="chat",
        parameters={
            "instruction": "Aggregate results",
            "aggregation_sources": [source_a.id, source_b.id, source_c.id],
        },
        metadata={"aggregation": True},
    )

    graph = Graph()
    for op in [source_a, source_b, source_c, aggregator]:
        graph.add_node(op)

    # Source A -> Aggregator (always)
    graph.add_edge(Edge(head=source_a.id, tail=aggregator.id))

    # Source B -> Aggregator (conditional - will be False)
    graph.add_edge(
        Edge(
            head=source_b.id,
            tail=aggregator.id,
            condition=AlwaysFalseCondition(),
        )
    )

    # Source C -> Aggregator (always)
    graph.add_edge(Edge(head=source_c.id, tail=aggregator.id))

    session = Session()
    branch = create_mock_branch()
    session.branches.include(branch)
    session.default_branch = branch

    result = await flow(session, graph, context={"test": "value"}, verbose=False)

    # Source B might be skipped or might execute but not connect to aggregator
    # The aggregator should still work with available sources
    assert aggregator.id in result["completed_operations"], (
        "Aggregator should work even if some sources are conditionally excluded"
    )


# ============================================================================
# VALIDATION TESTS - Ensure Proper Error Handling
# ============================================================================


@pytest.mark.asyncio
async def test_validation_invalid_edge_condition_type():
    """Non-EdgeCondition objects raise ValueError at edge creation time."""

    # Use valid node IDs
    node1_id = uuid4()
    node2_id = uuid4()

    with pytest.raises(ValueError, match="Condition subclass"):
        edge = Edge(
            head=node1_id,
            tail=node2_id,
            condition="not_a_condition",  # Invalid type
        )


@pytest.mark.asyncio
async def test_validation_circular_conditional_dependency():
    """Circular edges with conditions raise ValueError (graph must be acyclic)."""
    op_a = Operation(operation="chat", parameters={"instruction": "A"})
    op_b = Operation(operation="chat", parameters={"instruction": "B"})

    graph = Graph()
    graph.add_node(op_a)
    graph.add_node(op_b)

    # Create a cycle with conditions
    graph.add_edge(Edge(head=op_a.id, tail=op_b.id, condition=AlwaysTrueCondition()))
    graph.add_edge(Edge(head=op_b.id, tail=op_a.id, condition=AlwaysTrueCondition()))

    session = Session()
    branch = create_mock_branch()
    session.branches.include(branch)
    session.default_branch = branch

    # Should detect cycle
    with pytest.raises(ValueError, match="Graph must be acyclic"):
        await flow(session, graph, verbose=False)


# ============================================================================
# BEHAVIORAL TESTS - Complex Scenarios
# ============================================================================


@pytest.mark.asyncio
async def test_behavior_diamond_pattern_with_conditions():
    """Diamond graph (START -> A|B -> END) routes correctly based on edge conditions."""
    start = Operation(operation="chat", parameters={"instruction": "Start"})
    path_a = Operation(operation="chat", parameters={"instruction": "Path A"})
    path_b = Operation(operation="chat", parameters={"instruction": "Path B"})
    end = Operation(operation="chat", parameters={"instruction": "End"})

    graph = Graph()
    for op in [start, path_a, path_b, end]:
        graph.add_node(op)

    # Conditional branches
    graph.add_edge(
        Edge(
            head=start.id,
            tail=path_a.id,
            condition=ConditionalEdge("path", "A"),
        )
    )
    graph.add_edge(
        Edge(
            head=start.id,
            tail=path_b.id,
            condition=ConditionalEdge("path", "B"),
        )
    )

    # Convergence
    graph.add_edge(Edge(head=path_a.id, tail=end.id))
    graph.add_edge(Edge(head=path_b.id, tail=end.id))

    session = Session()

    # Test with path A
    branch_a = create_mock_branch("BranchA")
    session.branches.include(branch_a)
    session.default_branch = branch_a

    result_a = await flow(session, graph, context={"path": "A"}, verbose=False)

    assert start.id in result_a["completed_operations"]
    assert path_a.id in result_a["completed_operations"], "Path A should execute"
    assert path_b.id not in result_a["completed_operations"], "Path B should be skipped"
    assert end.id in result_a["completed_operations"], "End should execute after Path A"

    # Test with path B
    branch_b = create_mock_branch("BranchB")
    session.branches.include(branch_b)
    session.default_branch = branch_b

    # Reset operation states (this is the issue we're trying to fix)
    for op in [start, path_a, path_b, end]:
        op.execution.status = EventStatus.PENDING

    result_b = await flow(session, graph, context={"path": "B"}, verbose=False)

    assert start.id in result_b["completed_operations"]
    assert path_a.id not in result_b["completed_operations"], "Path A should be skipped"
    assert path_b.id in result_b["completed_operations"], "Path B should execute"
    assert end.id in result_b["completed_operations"], "End should execute after Path B"


@pytest.mark.asyncio
async def test_behavior_multi_level_conditions():
    """Multi-level gate conditions pass only when context level meets each threshold."""
    start = Operation(operation="chat", parameters={"instruction": "Start"})
    gate1 = Operation(operation="chat", parameters={"instruction": "Gate 1"})
    gate2 = Operation(operation="chat", parameters={"instruction": "Gate 2"})
    gate3 = Operation(operation="chat", parameters={"instruction": "Gate 3"})

    graph = Graph()
    for op in [start, gate1, gate2, gate3]:
        graph.add_node(op)

    # Define level-based conditions
    class LevelCondition(EdgeCondition):
        def __init__(self, min_level: int):
            super().__init__()
            self.min_level = min_level

        async def apply(self, context: dict) -> bool:
            level = context.get("context", {}).get("level", 0)
            return level >= self.min_level

    graph.add_edge(Edge(head=start.id, tail=gate1.id, condition=LevelCondition(1)))
    graph.add_edge(Edge(head=gate1.id, tail=gate2.id, condition=LevelCondition(2)))
    graph.add_edge(Edge(head=gate2.id, tail=gate3.id, condition=LevelCondition(3)))

    session = Session()

    # Test with different levels
    test_cases = [
        (0, [start.id]),  # Only start
        (1, [start.id, gate1.id]),  # Start + Gate1
        (2, [start.id, gate1.id, gate2.id]),  # Start + Gate1 + Gate2
        (3, [start.id, gate1.id, gate2.id, gate3.id]),  # All gates
    ]

    for level, expected_ops in test_cases:
        # Reset operation states
        for op in [start, gate1, gate2, gate3]:
            op.execution.status = EventStatus.PENDING

        branch = create_mock_branch(f"Level{level}")
        session.branches.include(branch)
        session.default_branch = branch

        result = await flow(session, graph, context={"level": level}, verbose=False)

        for op_id in expected_ops:
            assert op_id in result["completed_operations"], (
                f"Level {level}: Operation {op_id} should execute"
            )

        # Check that operations beyond the level are not executed
        all_ops = [start.id, gate1.id, gate2.id, gate3.id]
        for op_id in all_ops:
            if op_id not in expected_ops:
                assert op_id not in result["completed_operations"], (
                    f"Level {level}: Operation {op_id} should NOT execute"
                )


# ============================================================================
# PERFORMANCE TESTS - Ensure Efficiency
# ============================================================================


@pytest.mark.asyncio
async def test_performance_skip_expensive_operations():
    """Operations with a false edge condition are not invoked at all."""
    call_count = {"expensive": 0}

    async def expensive_operation(**kwargs):
        call_count["expensive"] += 1
        # No sleep — we're testing skipping, not timing
        from lionagi.testing import LionAGIMockFactory

        return LionAGIMockFactory.create_api_calling_mock(
            response_data="Expensive result",
            status=EventStatus.COMPLETED,
            model="gpt-4-mini",
        )

    start = Operation(operation="chat", parameters={"instruction": "Start"})
    expensive = Operation(operation="chat", parameters={"instruction": "Expensive"})

    graph = Graph()
    graph.add_node(start)
    graph.add_node(expensive)

    # Expensive operation has False condition
    graph.add_edge(Edge(head=start.id, tail=expensive.id, condition=AlwaysFalseCondition()))

    # Create branch but don't override - the test should verify skipping
    branch = create_mock_branch("ExpensiveBranch")

    # Track if invoke was called on expensive operation
    original_invoke = branch.chat_model.invoke

    async def tracking_invoke(**kwargs):
        instruction = kwargs.get("messages", [{}])[0].get("content", "")
        if "Expensive" in instruction:
            call_count["expensive"] += 1
        return await original_invoke(**kwargs)

    branch.chat_model.invoke = tracking_invoke

    session = Session()
    session.branches.include(branch)
    session.default_branch = branch

    result = await flow(session, graph, verbose=False)

    # The expensive operation should NOT have been called
    assert call_count["expensive"] == 0, "Skipped operations should not execute their logic"
    assert expensive.id not in result["completed_operations"], (
        "Expensive operation should be skipped"
    )


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])
