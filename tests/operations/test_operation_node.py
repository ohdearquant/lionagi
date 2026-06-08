# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from anyio import get_cancelled_exc_class
from pydantic import BaseModel

from lionagi.operations.node import Operation
from lionagi.protocols.generic.event import EventStatus
from lionagi.session.branch import Branch


# Test fixtures and utilities
class OpParams(BaseModel):
    instruction: str
    count: int = 1
    enabled: bool = True


def _set_branch(op: Operation, branch) -> None:
    """Helper to set branch on operation (new invoke pattern)."""
    op._branch = branch


# Test Operation creation and properties
def test_operation_creation():
    # Test with dict parameters
    op1 = Operation(
        operation="chat",
        parameters={"instruction": "Hello", "temperature": 0.7},
    )
    assert op1.operation == "chat"
    assert op1.parameters["instruction"] == "Hello"
    assert op1.parameters["temperature"] == 0.7

    # Test with BaseModel parameters
    params = OpParams(instruction="Test instruction", count=5)
    op2 = Operation(operation="operate", parameters=params)
    assert op2.operation == "operate"
    assert isinstance(op2.parameters, OpParams)
    assert op2.parameters.instruction == "Test instruction"

    # Test with default parameters
    op3 = Operation(operation="parse")
    assert op3.operation == "parse"
    assert op3.parameters == {}


# Test async operations
@pytest.mark.asyncio
async def test_operation_invoke_chat():
    op = Operation(operation="chat", parameters={"instruction": "Hello, how are you?"})

    # Create a mock branch
    branch = MagicMock()
    branch.id = "12345678-1234-4678-9234-567812345678"

    # Mock the chat method
    async def mock_chat(**kwargs):
        return f"chat_response: {kwargs.get('instruction', 'default')}"

    branch.chat = AsyncMock(side_effect=mock_chat)

    # Mock get_operation to return the correct async method
    def mock_get_operation(operation: str):
        if operation == "chat":
            return branch.chat
        return None

    branch.get_operation = MagicMock(side_effect=mock_get_operation)

    _set_branch(op, branch)
    await op.invoke()

    # Verify operation was called
    branch.chat.assert_called_once_with(instruction="Hello, how are you?")

    # Verify execution status
    assert op.execution.status == EventStatus.COMPLETED
    assert op.response == "chat_response: Hello, how are you?"
    assert str(op.branch_id) == branch.id
    assert op.execution.duration > 0


@pytest.mark.asyncio
async def test_operation_invoke_with_basemodel_params():
    params = OpParams(instruction="Complex task", count=3, enabled=False)
    op = Operation(operation="operate", parameters=params)

    # Create a mock branch
    branch = MagicMock()
    branch.id = "12345678-1234-4678-9234-567812345678"

    async def mock_operate(**kwargs):
        return {"operation": "operate", "result": "success"}

    branch.operate = AsyncMock(side_effect=mock_operate)

    # Mock get_operation to return the correct async method
    def mock_get_operation(operation: str):
        if operation == "operate":
            return branch.operate
        return None

    branch.get_operation = MagicMock(side_effect=mock_get_operation)

    _set_branch(op, branch)
    await op.invoke()

    # Verify the method was called with unpacked parameters
    branch.operate.assert_called_once_with(instruction="Complex task", count=3, enabled=False)

    # Verify response
    assert op.response == {"operation": "operate", "result": "success"}


@pytest.mark.asyncio
async def test_operation_invoke_streaming():
    op = Operation(operation="ReActStream", parameters={"query": "stream test"})

    # Create a mock branch
    branch = MagicMock()
    branch.id = "12345678-1234-4678-9234-567812345678"

    async def mock_stream(**kwargs):
        for i in range(3):
            yield f"stream_chunk_{i}"

    branch.ReActStream = mock_stream

    # Mock get_operation to return the correct async method
    def mock_get_operation(operation: str):
        if operation == "ReActStream":
            return branch.ReActStream
        return None

    branch.get_operation = MagicMock(side_effect=mock_get_operation)

    _set_branch(op, branch)
    await op.invoke()

    # Verify response is a list of streamed chunks
    assert op.response == [
        "stream_chunk_0",
        "stream_chunk_1",
        "stream_chunk_2",
    ]
    assert op.execution.status == EventStatus.COMPLETED


@pytest.mark.asyncio
async def test_operation_invoke_all_operations():
    # Create a mock branch
    branch = MagicMock()
    branch.id = "12345678-1234-4678-9234-567812345678"

    # Set up all mock methods
    branch.chat = AsyncMock(return_value="chat_response: test")
    branch.operate = AsyncMock(return_value={"operation": "operate", "result": "success"})
    branch.communicate = AsyncMock(return_value="communicate_response")
    branch.parse = AsyncMock(return_value={"parsed": True})
    branch.ReAct = AsyncMock(return_value={"react": "result"})
    branch.select = AsyncMock(return_value="selected_option")
    branch.translate = AsyncMock(return_value="translated_text")
    branch.interpret = AsyncMock(return_value={"interpretation": "complete"})
    branch.act = AsyncMock(return_value={"action": "taken"})

    # Mock get_operation to return the correct async method
    def mock_get_operation(operation: str):
        operation_map = {
            "chat": branch.chat,
            "operate": branch.operate,
            "communicate": branch.communicate,
            "parse": branch.parse,
            "ReAct": branch.ReAct,
            "select": branch.select,
            "translate": branch.translate,
            "interpret": branch.interpret,
            "act": branch.act,
        }
        return operation_map.get(operation)

    branch.get_operation = MagicMock(side_effect=mock_get_operation)

    operations_and_expected = [
        ("chat", "chat_response: test"),
        ("operate", {"operation": "operate", "result": "success"}),
        ("communicate", "communicate_response"),
        ("parse", {"parsed": True}),
        ("ReAct", {"react": "result"}),
        ("select", "selected_option"),
        ("translate", "translated_text"),
        ("interpret", {"interpretation": "complete"}),
        ("act", {"action": "taken"}),
    ]

    for op_type, expected_response in operations_and_expected:
        op = Operation(operation=op_type, parameters={"instruction": "test"})
        _set_branch(op, branch)
        await op.invoke()
        assert op.response == expected_response
        assert op.execution.status == EventStatus.COMPLETED


@pytest.mark.asyncio
async def test_operation_invoke_invalid_operation():
    # Create a proper Branch instance so getattr works correctly
    branch = Branch(user="test_user", name="TestBranch")

    # Create operation with valid type first
    op = Operation(operation="chat")
    # Then change to invalid type (bypassing validation)
    op.operation = "invalid_operation"

    # invoke() is total: the unsupported-operation error is captured as FAILED state.
    _set_branch(op, branch)
    await op.invoke()
    assert op.execution.status == EventStatus.FAILED
    assert "Unsupported operation type" in str(op.execution.error)


@pytest.mark.asyncio
async def test_operation_invoke_exception_handling():
    # Create a mock branch
    branch = MagicMock()
    branch.id = "12345678-1234-4678-9234-567812345678"

    # Mock method to raise exception
    async def failing_method(**kwargs):
        raise RuntimeError("Test error occurred")

    branch.chat = AsyncMock(side_effect=failing_method)

    # Mock get_operation to return the correct async method
    def mock_get_operation(operation: str):
        if operation == "chat":
            return branch.chat
        return None

    branch.get_operation = MagicMock(side_effect=mock_get_operation)

    op = Operation(operation="chat", parameters={"instruction": "This will fail"})
    _set_branch(op, branch)
    await op.invoke()  # total: the error is captured as FAILED state, not re-raised

    # Verify error handling — exception is recorded via add_error, not re-raised
    assert op.execution.status == EventStatus.FAILED
    assert isinstance(op.execution.error, RuntimeError)
    assert str(op.execution.error) == "Test error occurred"
    assert op.response is None


@pytest.mark.slow
@pytest.mark.asyncio
async def test_operation_invoke_cancellation():
    branch = MagicMock()
    branch.id = "12345678-1234-4678-9234-567812345678"

    started = asyncio.Event()

    async def slow_method(**kwargs):
        started.set()
        await asyncio.sleep(100)  # Never completes on its own
        return "should_not_reach_here"

    branch.chat = AsyncMock(side_effect=slow_method)

    def mock_get_operation(operation: str):
        if operation == "chat":
            return branch.chat
        return None

    branch.get_operation = MagicMock(side_effect=mock_get_operation)

    op = Operation(operation="chat")

    _set_branch(op, branch)
    task = asyncio.create_task(op.invoke())
    await asyncio.wait_for(started.wait(), timeout=2.0)  # wait for task to start
    task.cancel()

    with pytest.raises(get_cancelled_exc_class()):
        await task

    # Verify cancellation was handled by Event.invoke()'s BaseException handler
    assert op.execution.status == EventStatus.CANCELLED


@pytest.mark.asyncio
async def test_operation_concurrent_invocations():
    # Create a mock branch
    branch = MagicMock()
    branch.id = "12345678-1234-4678-9234-567812345678"

    # Mock chat method
    async def mock_chat(**kwargs):
        await asyncio.sleep(0.01)  # Small delay
        return f"chat_response: {kwargs.get('instruction', 'default')}"

    branch.chat = AsyncMock(side_effect=mock_chat)

    # Mock get_operation to return the correct async method
    def mock_get_operation(operation: str):
        if operation == "chat":
            return branch.chat
        return None

    branch.get_operation = MagicMock(side_effect=mock_get_operation)

    # Create multiple operations
    ops = [Operation(operation="chat", parameters={"instruction": f"Task {i}"}) for i in range(5)]

    # Invoke all operations concurrently
    for op in ops:
        _set_branch(op, branch)
    tasks = [op.invoke() for op in ops]
    await asyncio.gather(*tasks)

    # Verify all completed successfully
    for i, op in enumerate(ops):
        assert op.execution.status == EventStatus.COMPLETED
        assert op.response == f"chat_response: Task {i}"


@pytest.mark.asyncio
async def test_operation_idempotent_invoke():
    branch = MagicMock()
    branch.id = "12345678-1234-4678-9234-567812345678"

    call_count = 0

    async def mock_chat(**kwargs):
        nonlocal call_count
        call_count += 1
        return "response"

    branch.chat = AsyncMock(side_effect=mock_chat)
    branch.get_operation = MagicMock(return_value=branch.chat)

    op = Operation(operation="chat", parameters={"instruction": "test"})
    _set_branch(op, branch)

    # First invoke
    await op.invoke()
    assert op.execution.status == EventStatus.COMPLETED
    assert call_count == 1

    # Second invoke should be a no-op (idempotent)
    await op.invoke()
    assert call_count == 1  # Not called again
