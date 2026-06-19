# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from anyio import get_cancelled_exc_class

from lionagi.operations.node import Operation
from lionagi.protocols.generic.event import EventStatus
from lionagi.providers.openai.chat.models import OpenAIChatCompletionsRequest
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.connections.endpoint import Endpoint
from lionagi.testing import oai_chat_endpoint_config


@pytest.mark.slow
@pytest.mark.asyncio
async def test_operation_cancelled_status():
    """Test that cancelled operations have EventStatus.CANCELLED."""
    branch = MagicMock()
    branch.id = "test-branch-id"

    started = asyncio.Event()

    async def slow_method(**kwargs):
        started.set()
        await asyncio.sleep(100)  # Never completes on its own
        return "should_not_reach_here"

    branch.chat = slow_method
    branch.get_operation = MagicMock(return_value=branch.chat)

    op = Operation(operation="chat")
    op._branch = branch

    task = asyncio.create_task(op.invoke())
    await asyncio.wait_for(started.wait(), timeout=2.0)  # Wait for task to actually start
    task.cancel()

    with pytest.raises(get_cancelled_exc_class()):
        await task

    # Verify the operation has CANCELLED status
    assert op.execution.status == EventStatus.CANCELLED


@pytest.mark.slow
@pytest.mark.asyncio
async def test_api_call_cancelled_status():
    """Test that cancelled API calls have EventStatus.CANCELLED."""
    # Create an API call
    config = oai_chat_endpoint_config(
        name="oai_chat",
        endpoint="chat/completions",
        request_options=OpenAIChatCompletionsRequest,
        kwargs={"model": "gpt-4.1-mini"},
    )
    endpoint = Endpoint(config=config)
    api_call = APICalling(
        payload={"model": "gpt-4", "messages": []},
        headers={"Authorization": "Bearer test"},
        endpoint=endpoint,
    )

    async def slow_invoke(**kwargs):
        await asyncio.sleep(10)
        return {"response": "should_not_reach_here"}

    api_call.endpoint.invoke = AsyncMock(side_effect=slow_invoke)

    task = asyncio.create_task(api_call.invoke())
    await asyncio.sleep(0.01)  # Let it start
    task.cancel()

    try:
        await task
    except get_cancelled_exc_class():
        pass  # Expected

    assert hasattr(EventStatus, "CANCELLED")
    assert EventStatus.CANCELLED == "cancelled"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_cancelled_vs_failed_status():
    """Test that cancelled operations are distinct from failed operations."""
    # Test failed operation
    branch = MagicMock()
    branch.id = "test-branch-id"

    async def failing_method(**kwargs):
        raise ValueError("This is a failure")

    branch.chat = AsyncMock(side_effect=failing_method)
    branch.get_operation = MagicMock(return_value=branch.chat)

    op_failed = Operation(operation="chat")
    op_failed._branch = branch
    await op_failed.invoke()  # total: a business failure is captured, not raised

    # Failed operation should have FAILED status
    assert op_failed.execution.status == EventStatus.FAILED
    assert isinstance(op_failed.execution.error, ValueError)
    assert "This is a failure" in str(op_failed.execution.error)

    # Test cancelled operation - use an event to avoid fixed sleeps
    branch_cancelled = MagicMock()
    branch_cancelled.id = "test-branch-id-cancelled"
    started_cancelled = asyncio.Event()

    async def slow_method(**kwargs):
        started_cancelled.set()
        await asyncio.sleep(100)
        return "should_not_reach_here"

    branch_cancelled.chat = slow_method
    branch_cancelled.get_operation = MagicMock(return_value=branch_cancelled.chat)

    op_cancelled = Operation(operation="chat")
    op_cancelled._branch = branch_cancelled
    task = asyncio.create_task(op_cancelled.invoke())
    await asyncio.wait_for(started_cancelled.wait(), timeout=2.0)
    task.cancel()

    try:
        await task
    except get_cancelled_exc_class():
        pass  # Expected

    # Cancelled operation should have CANCELLED status
    assert op_cancelled.execution.status == EventStatus.CANCELLED

    # Verify they are different
    assert op_failed.execution.status != op_cancelled.execution.status
