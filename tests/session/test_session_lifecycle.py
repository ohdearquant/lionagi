# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Session lifecycle: basic flow execution and branch management."""

from unittest.mock import AsyncMock

import pytest

from lionagi.operations.node import Operation
from lionagi.protocols.generic.event import EventStatus
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.graph.graph import Graph
from lionagi.providers.openai.chat.models import OpenAIChatCompletionsRequest
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


class TestBasicFlowExecution:
    @pytest.mark.asyncio
    async def test_flow_single_branch_linear_graph(self):
        session = Session()
        branch = make_mock_branch("MainBranch")
        session.include_branches(branch)

        graph, ops = make_simple_graph(3)

        result = await session.flow(graph, parallel=False, verbose=False)

        assert len(result["completed_operations"]) == 3
        assert all(op.id in result["completed_operations"] for op in ops)
        assert len(result["operation_results"]) == 3

    @pytest.mark.asyncio
    async def test_flow_multiple_branches_parallel(self):
        session = Session()

        branch1 = make_mock_branch("Branch1")
        branch2 = make_mock_branch("Branch2")
        branch3 = make_mock_branch("Branch3")
        session.include_branches([branch1, branch2, branch3])

        graph, ops = make_parallel_graph()

        result = await session.flow(graph, parallel=True, max_concurrent=3, verbose=False)

        assert len(result["completed_operations"]) == 4
        assert all(op.id in result["completed_operations"] for op in ops.values())

    @pytest.mark.asyncio
    async def test_flow_context_passing_between_operations(self):
        session = Session()
        branch = make_mock_branch()
        session.include_branches(branch)

        op1 = Operation(
            operation="chat",
            parameters={
                "instruction": "Task 1",
                "context": {"key1": "value1"},
            },
        )
        op2 = Operation(operation="chat", parameters={"instruction": "Task 2"})

        graph = Graph()
        graph.add_node(op1)
        graph.add_node(op2)
        graph.add_edge(Edge(head=op1.id, tail=op2.id))

        initial_context = {"global_key": "global_value"}
        result = await session.flow(graph, context=initial_context, parallel=False)

        assert "global_key" in result["final_context"]
        assert op2.parameters.get("context") is not None

    @pytest.mark.asyncio
    async def test_flow_with_empty_graph(self):
        session = Session()
        branch = make_mock_branch()
        session.include_branches(branch)

        graph = Graph()

        result = await session.flow(graph, parallel=False, verbose=False)

        assert result["completed_operations"] == []
        assert result["operation_results"] == {}
        assert result["final_context"] == {}

    @pytest.mark.asyncio
    async def test_flow_with_single_operation(self):
        session = Session()
        branch = make_mock_branch()
        session.include_branches(branch)

        op = Operation(operation="chat", parameters={"instruction": "Solo task"})
        graph = Graph()
        graph.add_node(op)

        result = await session.flow(graph, parallel=False, verbose=False)

        assert result["completed_operations"] == [op.id]
        assert op.id in result["operation_results"]


# ============================================================================
# 2. Branch Management Tests
# ============================================================================
