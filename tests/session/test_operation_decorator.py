# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the Session.operation() decorator functionality.
"""

import pytest

from lionagi.operations.builder import OperationGraphBuilder
from lionagi.session.branch import Branch
from lionagi.session.session import Session


@pytest.mark.asyncio
async def test_operation_decorator_basic():
    session = Session()

    # Register operation using decorator
    @session.operation()
    async def test_op(**kwargs):
        return {"result": "success"}

    # Check that operation was registered
    assert "test_op" in session._operation_manager.registry
    assert session._operation_manager.registry["test_op"] == test_op

    # Test that function can still be called directly
    result = await test_op()
    assert result == {"result": "success"}


@pytest.mark.asyncio
async def test_operation_decorator_custom_name():
    session = Session()

    @session.operation("custom_operation")
    async def some_function(**kwargs):
        return {"custom": True}

    # Check that operation was registered with custom name
    assert "custom_operation" in session._operation_manager.registry
    assert "some_function" not in session._operation_manager.registry
    assert session._operation_manager.registry["custom_operation"] == some_function


@pytest.mark.asyncio
async def test_operation_decorator_with_branches():
    session = Session()

    # Create and include branches to sync operation manager
    branch1 = Branch(name="branch1")
    branch2 = Branch(name="branch2")
    session.include_branches([branch1, branch2])

    # Register operations using decorator
    @session.operation()
    async def op1(**kwargs):
        return {"op": "1"}

    @session.operation()
    async def op2(**kwargs):
        return {"op": "2"}

    # Verify operations are synced to branches
    assert "op1" in branch1._operation_manager.registry
    assert "op2" in branch1._operation_manager.registry
    assert "op1" in branch2._operation_manager.registry
    assert "op2" in branch2._operation_manager.registry


@pytest.mark.asyncio
async def test_operation_decorator_in_flow():
    session = Session()

    # Create branches
    branch1 = Branch(name="branch1")
    branch2 = Branch(name="branch2")
    session.include_branches([branch1, branch2])

    # Register operations with decorator
    @session.operation()
    async def first_op(**kwargs):
        return {"step": 1, "data": "from_first"}

    @session.operation()
    async def second_op(**kwargs):
        context = kwargs.get("context", {})
        return {"step": 2, "received": context}

    # Build and execute flow
    builder = OperationGraphBuilder("DecoratorFlow")
    a = builder.add_operation("first_op", branch=branch1)
    b = builder.add_operation("second_op", branch=branch2, depends_on=[a])

    result = await session.flow(builder.get_graph())

    # Verify flow executed successfully
    assert len(result["completed_operations"]) == 2

    # Check results
    results = result["operation_results"]
    first_result = None
    second_result = None

    for op_result in results.values():
        if op_result and "step" in op_result:
            if op_result["step"] == 1:
                first_result = op_result
            elif op_result["step"] == 2:
                second_result = op_result

    assert first_result is not None
    assert first_result == {"step": 1, "data": "from_first"}
    assert second_result is not None
    assert "received" in second_result


@pytest.mark.asyncio
async def test_operation_decorator_update_flag():
    session = Session()

    # Register first operation
    @session.operation()
    async def test_op(**kwargs):
        return {"version": 1}

    # Try to register again without update flag (should raise error)
    with pytest.raises(ValueError, match="Operation 'test_op' is already registered"):

        @session.operation(update=False)
        async def test_op(**kwargs):
            return {"version": 2}

    # Original function should still be registered
    result = await session._operation_manager.registry["test_op"]()
    assert result == {"version": 1}

    # Now update with update=True
    @session.operation(update=True)
    async def test_op(**kwargs):
        return {"version": 3}

    # Should now have the updated function
    result = await session._operation_manager.registry["test_op"]()
    assert result == {"version": 3}


def test_operation_decorator_preserves_function():
    session = Session()

    @session.operation()
    async def original_func(**kwargs):
        """Original docstring"""
        return {"original": True}

    # Function should be preserved
    assert original_func.__name__ == "original_func"
    assert "Original docstring" in original_func.__doc__

    # And should be registered
    assert session._operation_manager.registry["original_func"] == original_func
