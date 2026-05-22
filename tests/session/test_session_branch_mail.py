# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Comprehensive tests for Session class focusing on multi-branch orchestration.

Test Coverage:
1. Basic flow execution (single/multiple branches, context passing)
2. Branch management (creation, registration, selection, iteration)
3. Edge cases (empty graphs, branch lifecycle, error handling, context isolation)
4. Mail system (send/receive, routing, mailbox management)
"""

from unittest.mock import AsyncMock

import pytest

from lionagi._errors import ItemNotFoundError
from lionagi.operations.node import Operation
from lionagi.protocols.generic.event import EventStatus
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.graph.graph import Graph
from lionagi.protocols.messages import Instruction
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
    """Create a Branch with mocked iModel for testing."""
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
    """Create a simple linear graph with specified number of operations."""
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
    """Create a diamond-shaped graph for parallel execution testing."""
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


class TestBranchManagement:
    """Test branch creation, registration, selection, and iteration."""

    def test_session_initialization_with_default_branch(self):
        """Test Session creates default branch on initialization."""
        session = Session()

        assert session.default_branch is not None
        assert session.default_branch in session.branches
        assert len(session.branches) == 1

    def test_session_initialization_with_custom_branch(self):
        """Test Session can be initialized with custom branch."""
        custom_branch = make_mock_branch("CustomBranch")
        session = Session()
        session.include_branches(custom_branch)

        assert custom_branch in session.branches
        # Default branch was created, then custom was added
        assert len(session.branches) >= 2

    def test_include_branches_single(self):
        """Test including single branch."""
        session = Session()
        initial_count = len(session.branches)

        branch = make_mock_branch("NewBranch")
        session.include_branches(branch)

        assert branch in session.branches
        assert len(session.branches) == initial_count + 1
        assert branch.user == session.id  # Branch user set to session ID

    def test_include_branches_multiple(self):
        """Test including multiple branches at once."""
        session = Session()
        initial_count = len(session.branches)

        branches = [make_mock_branch(f"Branch{i}") for i in range(3)]
        session.include_branches(branches)

        assert all(b in session.branches for b in branches)
        assert len(session.branches) == initial_count + 3

    def test_include_branches_idempotent(self):
        """Test that including same branch twice doesn't duplicate."""
        session = Session()
        branch = make_mock_branch("TestBranch")

        session.include_branches(branch)
        initial_count = len(session.branches)

        session.include_branches(branch)  # Include again

        assert len(session.branches) == initial_count  # No duplication

    def test_get_branch_by_id(self):
        """Test retrieving branch by ID."""
        session = Session()
        branch = make_mock_branch("TestBranch")
        session.include_branches(branch)

        retrieved = session.get_branch(branch.id)

        assert retrieved is branch
        assert retrieved.id == branch.id

    def test_get_branch_by_name(self):
        """Test retrieving branch by name."""
        session = Session()
        branch = make_mock_branch("UniqueNameBranch")
        session.include_branches(branch)

        retrieved = session.get_branch("UniqueNameBranch")

        assert retrieved is branch
        assert retrieved.name == "UniqueNameBranch"

    def test_get_branch_not_found_raises_error(self):
        """Test getting non-existent branch raises error."""
        session = Session()

        with pytest.raises(ItemNotFoundError):
            session.get_branch("nonexistent")

    def test_get_branch_with_default_value(self):
        """Test get_branch returns default when branch not found."""
        session = Session()

        default_value = "default"
        result = session.get_branch("nonexistent", default_value)

        assert result == default_value

    def test_remove_branch(self):
        """Test removing branch from session."""
        session = Session()
        branch = make_mock_branch("RemoveBranch")
        session.include_branches(branch)

        assert branch in session.branches

        session.remove_branch(branch.id)

        assert branch not in session.branches

    def test_remove_branch_updates_default_branch(self):
        """Test removing default branch updates to next available."""
        session = Session()
        branch1 = make_mock_branch("Branch1")
        branch2 = make_mock_branch("Branch2")
        session.include_branches([branch1, branch2])

        session.change_default_branch(branch1)
        assert session.default_branch is branch1

        session.remove_branch(branch1.id)

        # Default should now be branch2 or another available branch
        assert session.default_branch is not branch1
        assert session.default_branch in session.branches

    def test_change_default_branch(self):
        """Test changing default branch."""
        session = Session()
        branch1 = make_mock_branch("Branch1")
        branch2 = make_mock_branch("Branch2")
        session.include_branches([branch1, branch2])

        initial_default = session.default_branch
        session.change_default_branch(branch2)

        assert session.default_branch is branch2
        assert session.default_branch is not initial_default

    def test_new_branch_creates_and_includes(self):
        """Test new_branch creates branch and adds to session."""
        session = Session()
        initial_count = len(session.branches)

        new_branch = session.new_branch(name="NewBranch")

        assert new_branch in session.branches
        assert new_branch.name == "NewBranch"
        assert len(session.branches) == initial_count + 1

    def test_new_branch_with_custom_imodel(self):
        """Test creating new branch with custom iModel."""
        session = Session()

        custom_model = iModel(provider="openai", model="gpt-4o", api_key="test")
        new_branch = session.new_branch(name="CustomModelBranch", imodel=custom_model)

        assert new_branch.chat_model.model_name == "gpt-4o"

    def test_new_branch_as_default(self):
        """Test creating new branch and setting as default."""
        session = Session()
        old_default = session.default_branch

        new_branch = session.new_branch(name="NewDefaultBranch", as_default_branch=True)

        assert session.default_branch is new_branch
        assert session.default_branch is not old_default

    def test_split_branch_preserves_messages(self):
        """Test split creates new branch with cloned messages."""
        session = Session()
        branch = make_mock_branch("OriginalBranch")
        session.include_branches(branch)

        # Add message to branch
        msg = Instruction(
            content={"instruction": "Test message"},
            sender=branch.user,
            recipient=branch.id,
        )
        branch.messages.include(msg)

        # Split the branch
        cloned_branch = session.split(branch.id)

        assert cloned_branch in session.branches
        assert len(cloned_branch.messages) == len(branch.messages)
        assert cloned_branch.id != branch.id

    def test_split_branch_clones_tools(self):
        """Test split clones tool manager."""
        session = Session()
        branch = make_mock_branch("OriginalBranch")

        # Register a tool
        def test_tool(x: int) -> int:
            return x * 2

        branch.register_tools(test_tool)
        session.include_branches(branch)

        # Split the branch
        cloned_branch = session.split(branch.id)

        # Verify tool was cloned
        assert "test_tool" in cloned_branch.tools

    @pytest.mark.asyncio
    async def test_asplit_branch(self):
        """Test async split branch."""
        session = Session()
        branch = make_mock_branch("AsyncSplitBranch")
        session.include_branches(branch)

        cloned_branch = await session.asplit(branch.id)

        assert cloned_branch in session.branches
        assert cloned_branch.id != branch.id

    def test_iterate_over_branches(self):
        """Test iterating over session branches."""
        session = Session()
        branches = [make_mock_branch(f"Branch{i}") for i in range(3)]
        session.include_branches(branches)

        branch_list = list(session.branches)

        # Should include default branch + 3 added branches
        assert len(branch_list) >= 3
        assert all(b in branch_list for b in branches)


# ============================================================================
# 3. Edge Cases and Error Handling
# ============================================================================
