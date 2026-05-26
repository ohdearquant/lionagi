# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Tests for specific flow execution patterns and edge cases.

These tests ensure complex patterns work correctly:
1. Dynamic fan-out based on results
2. Context inheritance between operations
3. Branch management without locking
4. Multi-phase execution patterns
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from lionagi.operations.builder import OperationGraphBuilder
from lionagi.operations.fields import Instruct
from lionagi.operations.flow import flow
from lionagi.session.session import Session
from lionagi.testing import MockClaudeCode


def create_mock_branch(branch_id: str, **operation_mocks):
    """Create a properly configured mock branch with all required attributes."""
    branch = MagicMock()
    branch.id = branch_id

    # Set up operation mocks
    for op_name, op_func in operation_mocks.items():
        setattr(branch, op_name, AsyncMock(side_effect=op_func))

    # Set up message manager
    branch._message_manager = MagicMock()
    branch._message_manager.pile = MagicMock()
    branch._message_manager.pile.clear = MagicMock()
    branch._message_manager.pile.append = MagicMock()
    branch._message_manager.pile.__iter__ = MagicMock(return_value=iter([]))

    # Set up metadata
    branch.metadata = {}

    # Set up clone method
    def clone_func(sender=None):
        cloned = create_mock_branch(f"cloned_{branch_id}", **operation_mocks)
        return cloned

    branch.clone = MagicMock(side_effect=clone_func)

    return branch


"""Tests for flow edge cases and complex execution patterns."""


@pytest.mark.asyncio
async def test_flow_with_existing_graph():
    """Test flow execution with a pre-existing complex graph."""

    # Create a graph that simulates a real-world scenario
    builder = OperationGraphBuilder("RealWorldScenario")

    # Phase 1: Initial analysis
    analyze = builder.add_operation(
        "operate",
        instruct=Instruct(instruction="Analyze the codebase", context="lionagi"),
    )

    # Phase 2: Parallel investigations based on analysis
    investigate_ops = []
    for area in ["concurrency", "operations", "protocols"]:
        op = builder.add_operation(
            "operate",
            depends_on=[analyze],
            instruction=f"Investigate {area} module",
            context={"area": area, "depth": "detailed"},
        )
        investigate_ops.append(op)

    # Phase 3: Deep dive into specific issues
    issues_ops = []
    for inv_op in investigate_ops[:2]:  # Only first 2 investigations yield issues
        for i in range(2):
            op = builder.add_operation(
                "operate",
                depends_on=[inv_op],
                instruction=f"Fix issue {i} in module",
                metadata={
                    "inherit_context": True,
                    "primary_dependency": inv_op,
                },
            )
            issues_ops.append(op)

    # Phase 4: Synthesis
    synthesis = builder.add_aggregation(
        "communicate",
        source_node_ids=investigate_ops + issues_ops,
        instruction="Synthesize all findings and fixes",
    )

    # Mock branch
    async def mock_operate(**kwargs):
        # Debug: print what we received
        # print(f"mock_operate called with: {kwargs}")

        # Get instruction from various possible locations
        instruction = kwargs.get("instruction", "")

        # Check if we have an instruct parameter
        if not instruction and "instruct" in kwargs:
            # If instruct object was passed
            instruct_obj = kwargs["instruct"]
            if hasattr(instruct_obj, "instruction"):
                instruction = instruct_obj.instruction
            elif isinstance(instruct_obj, dict):
                instruction = instruct_obj.get("instruction", "")

        # Also check in the parameters
        if not instruction:
            # When Instruct object is converted to dict, it goes into kwargs directly
            instruction = kwargs.get("instruction", "")
            if not instruction and hasattr(kwargs.get("context"), "instruction"):
                instruction = kwargs["context"].instruction

        if "Analyze" in instruction:
            return {"areas_found": ["concurrency", "operations", "protocols"]}
        elif "Investigate" in instruction:
            # Context might be nested or at top level
            context = kwargs.get("context", {})
            if isinstance(context, str):
                # If context is a string, check kwargs for area
                area = "unknown"
            else:
                area = context.get("area", "unknown") if isinstance(context, dict) else "unknown"
            return {"issues_found": 2 if area != "protocols" else 0}
        elif "Fix issue" in instruction:
            return {"fix_applied": True}
        return {"result": "generic"}

    branch = MagicMock()
    branch.id = str(uuid4())  # Use proper UUID
    branch.operate = AsyncMock(side_effect=mock_operate)
    branch.communicate = AsyncMock(return_value="Synthesis complete")
    branch._message_manager = MagicMock()
    branch._message_manager.pile = MagicMock()
    branch._message_manager.pile.clear = MagicMock()
    branch._message_manager.pile.__iter__ = MagicMock(return_value=iter([]))
    branch.metadata = {}

    # Mock get_operation to return the correct async method
    def mock_get_operation(operation: str):
        operation_map = {
            "operate": branch.operate,
            "communicate": branch.communicate,
        }
        return operation_map.get(operation)

    branch.get_operation = MagicMock(side_effect=mock_get_operation)

    # Mock clone method
    def mock_clone(sender=None):
        cloned = MagicMock()
        cloned.id = str(uuid4())
        cloned.operate = AsyncMock(side_effect=mock_operate)
        cloned.communicate = AsyncMock(return_value="Synthesis complete")
        cloned.clone = MagicMock(side_effect=mock_clone)
        cloned._message_manager = MagicMock()
        cloned._message_manager.pile = MagicMock()
        cloned._message_manager.pile.clear = MagicMock()
        cloned._message_manager.pile.__iter__ = MagicMock(return_value=iter([]))
        cloned.metadata = {}

        # Mock get_operation for cloned branch too
        def cloned_get_operation(operation: str):
            operation_map = {
                "operate": cloned.operate,
                "communicate": cloned.communicate,
            }
            return operation_map.get(operation)

        cloned.get_operation = MagicMock(side_effect=cloned_get_operation)
        return cloned

    branch.clone = MagicMock(side_effect=mock_clone)

    session = Session()
    session.branches.include(branch)
    session.default_branch = branch

    # Execute the complete flow
    result = await flow(session, builder.get_graph(), max_concurrent=5, verbose=False)

    # Verify execution
    assert len(result["completed_operations"]) == 9  # 1 + 3 + 4 + 1
    assert synthesis in result["operation_results"]

    # Since branches are pre-allocated and cloned, we can't check call counts on the original branch
    # Instead, verify that all operations completed successfully

    # Check that all expected operations have results
    assert analyze in result["operation_results"]
    for op in investigate_ops:
        assert op in result["operation_results"]
    for op in issues_ops:
        assert op in result["operation_results"]

    # Verify the synthesis result
    synthesis_result = result["operation_results"][synthesis]
    assert synthesis_result == "Synthesis complete"

    # Verify analyze result
    analyze_result = result["operation_results"][analyze]
    assert "areas_found" in analyze_result
    assert analyze_result["areas_found"] == [
        "concurrency",
        "operations",
        "protocols",
    ]

    # Verify investigate results
    for op in investigate_ops[:2]:  # First two should find issues
        result_data = result["operation_results"][op]
        assert "issues_found" in result_data
        assert result_data["issues_found"] == 2

    # Last investigate should find no issues
    protocols_result = result["operation_results"][investigate_ops[2]]
    assert protocols_result["issues_found"] == 0

    # Verify all fix operations completed
    for op in issues_ops:
        fix_result = result["operation_results"][op]
        assert "fix_applied" in fix_result
        assert fix_result["fix_applied"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
