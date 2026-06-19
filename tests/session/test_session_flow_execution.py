# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Session multi-branch flow execution, edge cases, and async behaviour."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.operations.builder import OperationGraphBuilder
from lionagi.operations.node import Operation
from lionagi.protocols.generic.event import EventStatus
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.graph.graph import Graph
from lionagi.protocols.messages import Instruction
from lionagi.providers.openai.chat import OpenAIChatCompletionsRequest
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from lionagi.session.session import Session


def _get_oai_config(
    name: str = "openai_chat/completions",
    endpoint: str = "chat/completions",
    request_options=None,
    kwargs: dict | None = None,
) -> EndpointConfig:
    return EndpointConfig(
        name=name,
        provider="openai",
        base_url="https://api.openai.com/v1",
        endpoint=endpoint,
        api_key="dummy-key-for-testing",
        request_options=request_options,
        auth_type="bearer",
        content_type="application/json",
        method="POST",
        requires_tokens=True,
        kwargs=kwargs or {},
    )


# ============================================================================
# Test Fixtures and Helpers
# ============================================================================


def make_mock_branch(name: str = "TestBranch") -> Branch:
    branch = Branch(user="test_user", name=name)

    async def _fake_invoke(**kwargs):
        config = _get_oai_config(
            name="oai_chat",
            endpoint="chat/completions",
            request_options=OpenAIChatCompletionsRequest,
            kwargs={"model": "gpt-4.1-mini"},
        )
        endpoint = Endpoint(config=config)
        fake_call = APICalling(
            payload={"model": "gpt-4.1-mini", "messages": []},
            headers={"Authorization": "Bearer test"},
            endpoint=endpoint,
        )
        fake_call.execution.response = "mocked_response"
        fake_call.execution.status = EventStatus.COMPLETED
        return fake_call

    mock_invoke = AsyncMock(side_effect=_fake_invoke)
    mock_chat_model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    mock_chat_model.invoke = mock_invoke

    branch.chat_model = mock_chat_model
    return branch


def make_simple_graph(num_nodes: int = 3) -> tuple[Graph, list[Operation]]:
    ops = [
        Operation(operation="chat", parameters={"instruction": f"Task {i}"})
        for i in range(num_nodes)
    ]

    graph = Graph()
    for op in ops:
        graph.add_node(op)

    for i in range(len(ops) - 1):
        graph.add_edge(Edge(head=ops[i].id, tail=ops[i + 1].id))

    return graph, ops


def make_parallel_graph() -> tuple[Graph, dict[str, Operation]]:
    ops = {
        "start": Operation(operation="chat", parameters={"instruction": "Start"}),
        "branch_a": Operation(operation="chat", parameters={"instruction": "Branch A"}),
        "branch_b": Operation(operation="chat", parameters={"instruction": "Branch B"}),
        "merge": Operation(operation="chat", parameters={"instruction": "Merge"}),
    }

    graph = Graph()
    for op in ops.values():
        graph.add_node(op)

    graph.add_edge(Edge(head=ops["start"].id, tail=ops["branch_a"].id))
    graph.add_edge(Edge(head=ops["start"].id, tail=ops["branch_b"].id))
    graph.add_edge(Edge(head=ops["branch_a"].id, tail=ops["merge"].id))
    graph.add_edge(Edge(head=ops["branch_b"].id, tail=ops["merge"].id))

    return graph, ops


# ============================================================================
# 1. Basic Flow Execution Tests
# ============================================================================


class TestEdgeCasesAndErrors:
    @pytest.mark.asyncio
    async def test_flow_with_operation_error(self):
        """Operations that fail are still marked completed; error recorded in execution."""
        session = Session()
        branch = make_mock_branch("ErrorBranch")

        original_invoke = branch.chat_model.invoke

        async def failing_invoke(**kwargs):
            raise ValueError("Simulated operation failure")

        branch.chat_model.invoke = failing_invoke

        session.include_branches(branch)
        session.default_branch = branch

        op = Operation(operation="chat", parameters={"instruction": "Will fail"})
        graph = Graph()
        graph.add_node(op)

        result = await session.flow(graph, parallel=False, verbose=False)

        assert op.id in result["completed_operations"]
        assert op.execution.error is not None
        assert "Simulated operation failure" in str(op.execution.error)

    @pytest.mark.asyncio
    async def test_flow_max_concurrent_limit(self):
        session = Session()
        branch = make_mock_branch()
        session.include_branches(branch)

        ops = [
            Operation(operation="chat", parameters={"instruction": f"Task {i}"}) for i in range(5)
        ]

        graph = Graph()
        for op in ops:
            graph.add_node(op)

        result = await session.flow(graph, parallel=True, max_concurrent=2, verbose=False)

        assert len(result["completed_operations"]) == 5

    @pytest.mark.asyncio
    async def test_flow_context_inheritance(self):
        session = Session()
        branch = make_mock_branch()
        session.include_branches(branch)

        op1 = Operation(operation="chat", parameters={"instruction": "First"})
        op2 = Operation(
            operation="chat",
            parameters={"instruction": "Second"},
            metadata={"inherit_context": True},
        )

        graph = Graph()
        graph.add_node(op1)
        graph.add_node(op2)
        graph.add_edge(Edge(head=op1.id, tail=op2.id))

        result = await session.flow(graph, context={"initial": "context"}, parallel=False)

        assert op2.parameters.get("context") is not None

    @pytest.mark.asyncio
    async def test_flow_context_isolation_between_branches(self):
        session = Session()

        branch1 = make_mock_branch("Branch1")
        branch2 = make_mock_branch("Branch2")
        session.include_branches([branch1, branch2])

        op1 = Operation(
            operation="chat",
            parameters={"instruction": "Task 1"},
        )
        op1.branch_id = branch1.id

        op2 = Operation(
            operation="chat",
            parameters={"instruction": "Task 2"},
        )
        op2.branch_id = branch2.id

        graph = Graph()
        graph.add_node(op1)
        graph.add_node(op2)

        result = await session.flow(graph, parallel=True, verbose=False)

        assert op1.id in result["completed_operations"]
        assert op2.id in result["completed_operations"]

    def test_concat_messages_single_branch(self):
        session = Session()
        branch = make_mock_branch("TestBranch")

        msg1 = Instruction(
            content={"instruction": "Message 1"},
            sender="user",
            recipient=branch.id,
        )
        msg2 = Instruction(
            content={"instruction": "Message 2"},
            sender="user",
            recipient=branch.id,
        )
        branch.messages.include([msg1, msg2])
        session.include_branches(branch)

        messages = session.concat_messages([branch.id])

        assert len(messages) >= 2

    def test_concat_messages_multiple_branches(self):
        session = Session()
        branch1 = make_mock_branch("Branch1")
        branch2 = make_mock_branch("Branch2")

        msg1 = Instruction(
            content={"instruction": "Branch1 Message"},
            sender="user",
            recipient=branch1.id,
        )
        msg2 = Instruction(
            content={"instruction": "Branch2 Message"},
            sender="user",
            recipient=branch2.id,
        )
        branch1.messages.include(msg1)
        branch2.messages.include(msg2)

        session.include_branches([branch1, branch2])

        messages = session.concat_messages([branch1.id, branch2.id])

        assert len(messages) >= 2

    def test_concat_messages_deduplication(self):
        session = Session()
        branch1 = make_mock_branch("Branch1")
        branch2 = make_mock_branch("Branch2")

        msg = Instruction(
            content={"instruction": "Shared Message"},
            sender="user",
            recipient=branch1.id,
        )
        branch1.messages.include(msg)
        branch2.messages.include(msg)

        session.include_branches([branch1, branch2])

        messages = session.concat_messages([branch1.id, branch2.id])

        message_ids = [m.id for m in messages]
        assert len(message_ids) == len(set(message_ids))

    def test_to_df_conversion(self):
        session = Session()
        branch = make_mock_branch("TestBranch")

        msg = Instruction(
            content={"instruction": "Test"},
            sender="user",
            recipient=branch.id,
        )
        branch.messages.include(msg)
        session.include_branches(branch)

        df = session.to_df([branch.id])

        assert df is not None
        assert len(df) >= 1

    def test_operation_manager_shared_across_branches(self):
        session = Session()

        @session.operation("shared_op")
        async def shared_operation(**kwargs):
            return {"result": "success"}

        branch1 = make_mock_branch("Branch1")
        branch2 = make_mock_branch("Branch2")
        session.include_branches([branch1, branch2])

        assert "shared_op" in branch1._operation_manager.registry
        assert "shared_op" in branch2._operation_manager.registry
        assert (
            branch1._operation_manager.registry["shared_op"]
            is branch2._operation_manager.registry["shared_op"]
        )


# ============================================================================
# 4. Integration Tests
# ============================================================================


class TestSessionFlowIntegration:
    @pytest.mark.asyncio
    async def test_full_multi_branch_workflow(self):
        session = Session()

        research_branch = make_mock_branch("Research")
        analysis_branch = make_mock_branch("Analysis")
        summary_branch = make_mock_branch("Summary")

        session.include_branches([research_branch, analysis_branch, summary_branch])

        # Create workflow graph
        op_research = Operation(
            operation="chat",
            parameters={"instruction": "Research topic"},
        )
        op_research.branch_id = research_branch.id

        op_analyze = Operation(
            operation="chat",
            parameters={"instruction": "Analyze findings"},
        )
        op_analyze.branch_id = analysis_branch.id

        op_summarize = Operation(
            operation="chat",
            parameters={"instruction": "Create summary"},
        )
        op_summarize.branch_id = summary_branch.id

        graph = Graph()
        graph.add_node(op_research)
        graph.add_node(op_analyze)
        graph.add_node(op_summarize)
        graph.add_edge(Edge(head=op_research.id, tail=op_analyze.id))
        graph.add_edge(Edge(head=op_analyze.id, tail=op_summarize.id))

        result = await session.flow(
            graph,
            context={"topic": "AI orchestration"},
            parallel=False,
            verbose=False,
        )

        assert len(result["completed_operations"]) == 3
        assert all(
            op.id in result["completed_operations"]
            for op in [op_research, op_analyze, op_summarize]
        )

    @pytest.mark.asyncio
    async def test_flow_with_builder_pattern(self):
        session = Session()

        branch1 = make_mock_branch("Branch1")
        branch2 = make_mock_branch("Branch2")
        session.include_branches([branch1, branch2])

        @session.operation()
        async def process_data(**kwargs):
            return {"processed": True}

        @session.operation()
        async def validate_data(**kwargs):
            return {"validated": True}

        builder = OperationGraphBuilder("TestWorkflow")
        op1 = builder.add_operation("process_data", branch=branch1)
        op2 = builder.add_operation("validate_data", branch=branch2, depends_on=[op1])

        result = await session.flow(builder.get_graph(), parallel=False, verbose=False)

        assert len(result["completed_operations"]) == 2

    @pytest.mark.asyncio
    async def test_session_resilience_to_branch_errors(self):
        """Operations that fail are still marked completed; error recorded in execution."""
        session = Session()

        working_branch = make_mock_branch("WorkingBranch")
        failing_branch = make_mock_branch("FailingBranch")

        async def failing_invoke(**kwargs):
            raise RuntimeError("Branch failure")

        failing_branch.chat_model.invoke = failing_invoke

        session.include_branches([working_branch, failing_branch])
        session.default_branch = working_branch

        op_working = Operation(
            operation="chat",
            parameters={"instruction": "Should work"},
        )
        op_working.branch_id = working_branch.id

        op_failing = Operation(
            operation="chat",
            parameters={"instruction": "Will fail"},
        )
        op_failing.branch_id = failing_branch.id

        graph = Graph()
        graph.add_node(op_working)
        graph.add_node(op_failing)

        result = await session.flow(graph, parallel=True, verbose=False)

        assert op_working.id in result["completed_operations"]
        assert op_failing.id in result["completed_operations"]

        assert op_working.id in result["operation_results"]
        assert op_failing.id in result["operation_results"]

        failing_result = result["operation_results"][op_failing.id]
        has_error = (
            isinstance(failing_result, dict) and "error" in failing_result
        ) or op_failing.execution.error is not None
        assert has_error


# ============================================================================
# 6. Async Edge Cases: Cancellation, Timeout, Error Propagation
# ============================================================================


class TestSessionFlowAsyncEdgeCases:
    @pytest.mark.asyncio
    async def test_flow_cancellation_mid_execution(self):
        session = Session()

        branch = MagicMock()
        branch.id = "test-branch-cancel-id"

        started = asyncio.Event()

        async def slow_chat(**kwargs):
            started.set()
            await asyncio.sleep(30)
            return (MagicMock(), MagicMock())

        branch.chat = AsyncMock(side_effect=slow_chat)

        def mock_get_operation(operation: str):
            if operation == "chat":
                return branch.chat
            return None

        branch.get_operation = MagicMock(side_effect=mock_get_operation)

        session.branches.include(branch)
        session.default_branch = branch

        graph, ops = make_simple_graph(3)

        task = asyncio.create_task(session.flow(graph, parallel=True, verbose=False))

        # Wait until at least one operation has entered slow_chat before cancelling
        await asyncio.wait_for(started.wait(), timeout=5.0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_flow_timeout_behavior(self):
        session = Session()

        branch = MagicMock()
        branch.id = "test-branch-id"

        async def very_slow_chat(**kwargs):
            await asyncio.sleep(10)  # sleep longer than the timeout so the timeout fires
            return "mocked_response"

        branch.chat = AsyncMock(side_effect=very_slow_chat)

        def mock_get_operation(operation: str):
            if operation == "chat":
                return branch.chat
            return None

        branch.get_operation = MagicMock(side_effect=mock_get_operation)

        session.branches.include(branch)
        session.default_branch = branch

        graph, ops = make_simple_graph(2)

        # Apply timeout to flow execution - should raise TimeoutError
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(session.flow(graph, parallel=False, verbose=False), timeout=0.5)

    @pytest.mark.asyncio
    async def test_error_propagation_across_parallel_branches(self):
        session = Session()

        working_branch = make_mock_branch("WorkingBranch")
        failing_branch = make_mock_branch("FailingBranch")

        async def failing_invoke(**kwargs):
            raise RuntimeError("Branch-specific error")

        failing_branch.chat_model.invoke = failing_invoke

        session.include_branches([working_branch, failing_branch])

        op_working = Operation(
            operation="chat",
            parameters={"instruction": "Should succeed"},
        )
        op_working.branch_id = working_branch.id

        op_failing = Operation(
            operation="chat",
            parameters={"instruction": "Will fail"},
        )
        op_failing.branch_id = failing_branch.id

        graph = Graph()
        graph.add_node(op_working)
        graph.add_node(op_failing)

        result = await session.flow(graph, parallel=True, verbose=False)

        assert op_working.id in result["completed_operations"]
        assert op_failing.id in result["completed_operations"]

        assert op_working.execution.error is None

        # Failing operation should have recorded error
        assert op_failing.execution.error is not None
        assert "Branch-specific error" in str(op_failing.execution.error)

    @pytest.mark.asyncio
    async def test_flow_continues_after_operation_failure(self):
        """Test that flow continues processing after one operation fails."""
        session = Session()

        # Create two branches - one will fail, one will succeed
        working_branch = make_mock_branch("WorkingBranch")
        failing_branch = make_mock_branch("FailingBranch")

        # Override invoke to fail for failing_branch
        async def failing_invoke(**kwargs):
            raise ValueError("Operation failure")

        failing_branch.chat_model.invoke = failing_invoke

        session.include_branches([working_branch, failing_branch])

        # Create sequential operations with mixed success/failure
        op1 = Operation(
            operation="chat",
            parameters={"instruction": "First (success)"},
        )
        op1.branch_id = working_branch.id

        op2 = Operation(
            operation="chat",
            parameters={"instruction": "Second (fail)"},
        )
        op2.branch_id = failing_branch.id

        op3 = Operation(
            operation="chat",
            parameters={"instruction": "Third (success)"},
        )
        op3.branch_id = working_branch.id

        graph = Graph()
        graph.add_node(op1)
        graph.add_node(op2)
        graph.add_node(op3)
        graph.add_edge(Edge(head=op1.id, tail=op2.id))
        graph.add_edge(Edge(head=op2.id, tail=op3.id))

        result = await session.flow(graph, parallel=False, verbose=False)

        # All operations should complete
        assert len(result["completed_operations"]) == 3
        # Verify first and third succeeded
        assert op1.execution.error is None
        assert op3.execution.error is None
        # Second should have failed
        assert op2.execution.error is not None

    @pytest.mark.asyncio
    async def test_concurrent_flow_with_mixed_timings(self):
        """Test concurrent flow with operations of varying speeds doesn't deadlock."""
        session = Session()
        branch = make_mock_branch()
        session.include_branches(branch)

        # Create operations with varying IDs
        op_fast = Operation(
            operation="chat",
            parameters={"instruction": "fast task"},
        )
        op_medium = Operation(
            operation="chat",
            parameters={"instruction": "medium task"},
        )
        op_slow = Operation(
            operation="chat",
            parameters={"instruction": "slow task"},
        )

        graph = Graph()
        for op in [op_fast, op_medium, op_slow]:
            graph.add_node(op)

        result = await session.flow(graph, parallel=True, max_concurrent=3, verbose=False)

        # All operations should complete despite potential timing differences
        assert len(result["completed_operations"]) == 3
        assert op_fast.id in result["completed_operations"]
        assert op_medium.id in result["completed_operations"]
        assert op_slow.id in result["completed_operations"]
