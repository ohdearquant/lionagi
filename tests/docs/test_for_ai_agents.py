"""Tests for for-ai-agents documentation examples."""

import inspect

import pytest

from lionagi.testing import LionAGIMockFactory

# =============================================================================
# Orchestration Guide (orchestration-guide.md)
# =============================================================================


class TestOrchestrationGuide:
    """Tests derived from orchestration-guide.md examples."""

    def test_branch_construction_minimal(self):
        """Branch() with no arguments creates a valid instance."""
        from lionagi import Branch

        branch = Branch()
        assert branch is not None
        assert branch.id is not None

    def test_branch_construction_with_system(self):
        """Branch(system=...) sets the system message."""
        from lionagi import Branch

        branch = Branch(system="You are a code reviewer.")
        assert branch.system is not None
        assert len(branch.messages) > 0

    def test_branch_construction_with_name_and_user(self):
        """Branch(name=..., user=...) stores name and user."""
        from lionagi import Branch

        branch = Branch(name="reviewer", user="agent_1")
        assert branch.name == "reviewer"

    def test_branch_construction_with_tools(self):
        """Branch(tools=[...]) registers callable tools."""
        from lionagi import Branch

        def search(query: str) -> str:
            """Search for information."""
            return f"results for {query}"

        branch = Branch(tools=[search])
        assert "search" in branch.tools

    def test_branch_construction_with_chat_model(self):
        """Branch(chat_model=iModel(...)) sets the chat model."""
        from lionagi import Branch, iModel

        model = iModel(provider="openai", model="gpt-4o", api_key="test-key")
        branch = Branch(chat_model=model)
        assert branch.chat_model is model

    def test_branch_construction_full_params(self):
        """Branch with system, name, tools, and chat_model all at once."""
        from lionagi import Branch, iModel

        def helper(x: str) -> str:
            """A helper tool."""
            return x

        model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        branch = Branch(
            system="You are an assistant.",
            name="full_branch",
            user="tester",
            tools=[helper],
            chat_model=model,
        )
        assert branch.name == "full_branch"
        assert branch.system is not None
        assert "helper" in branch.tools
        assert branch.chat_model is model

    @pytest.mark.asyncio
    async def test_communicate_is_async_callable(self):
        """branch.communicate is an async callable."""
        branch = LionAGIMockFactory.create_mocked_branch()
        assert callable(branch.communicate)
        assert inspect.iscoroutinefunction(branch.communicate)

    @pytest.mark.asyncio
    async def test_chat_is_async_callable(self):
        """branch.chat is an async callable."""
        branch = LionAGIMockFactory.create_mocked_branch()
        assert callable(branch.chat)
        assert inspect.iscoroutinefunction(branch.chat)

    @pytest.mark.asyncio
    async def test_operate_is_async_callable(self):
        """branch.operate is an async callable."""
        branch = LionAGIMockFactory.create_mocked_branch()
        assert callable(branch.operate)
        assert inspect.iscoroutinefunction(branch.operate)

    @pytest.mark.asyncio
    async def test_react_is_async_callable(self):
        """branch.ReAct is an async callable."""
        branch = LionAGIMockFactory.create_mocked_branch()
        assert callable(branch.ReAct)
        assert inspect.iscoroutinefunction(branch.ReAct)

    def test_session_and_builder_workflow_construction(self):
        """Session + Builder can construct a workflow graph without execution."""
        from lionagi import Builder, Session

        session = Session()
        assert session.default_branch is not None

        builder = Builder("test_workflow")
        node_id = builder.add_operation(
            "operate",
            instruction="Analyze this text",
        )
        assert node_id is not None

        graph = builder.get_graph()
        assert graph is not None

    def test_multiple_branches_with_different_imodels(self):
        """Multiple branches can use different iModels."""
        from lionagi import Branch, iModel

        model_a = iModel(provider="openai", model="gpt-4o", api_key="test-key-a")
        model_b = iModel(
            provider="anthropic",
            model="claude-3-5-sonnet-20241022",
            api_key="test-key-b",
        )

        branch_a = Branch(name="openai_branch", chat_model=model_a)
        branch_b = Branch(name="anthropic_branch", chat_model=model_b)

        assert branch_a.chat_model is model_a
        assert branch_b.chat_model is model_b
        assert branch_a.chat_model is not branch_b.chat_model


# =============================================================================
# Self-Improvement (self-improvement.md)
# =============================================================================


class TestSelfImprovement:
    """Tests derived from self-improvement.md examples."""

    def test_messages_iteration(self):
        """branch.messages is iterable."""
        from lionagi import Branch

        branch = Branch(system="You are helpful.")
        # Should have at least the system message
        count = 0
        for msg in branch.messages:
            count += 1
        assert count >= 1

    def test_msgs_last_response_on_fresh_branch(self):
        """branch.msgs.last_response is None on a fresh branch."""
        from lionagi import Branch

        branch = Branch()
        assert branch.msgs.last_response is None

    def test_msgs_last_instruction_on_fresh_branch(self):
        """branch.msgs.last_instruction is None on a fresh branch."""
        from lionagi import Branch

        branch = Branch()
        assert branch.msgs.last_instruction is None

    def test_to_dict_returns_dict_with_expected_keys(self):
        """branch.to_dict() returns a dict with core serialization keys."""
        from lionagi import Branch

        branch = Branch(system="Test system.")
        data = branch.to_dict()

        assert isinstance(data, dict)
        assert "messages" in data
        assert "chat_model" in data

    def test_from_dict_is_classmethod(self):
        """Branch.from_dict exists as a classmethod."""
        from lionagi import Branch

        assert hasattr(Branch, "from_dict")
        assert isinstance(inspect.getattr_static(Branch, "from_dict"), classmethod)

    def test_clone_returns_branch_instance(self):
        """branch.clone() returns a new Branch instance."""
        from lionagi import Branch

        branch = Branch(system="Original system.")
        cloned = branch.clone()

        assert isinstance(cloned, Branch)
        assert cloned.id != branch.id

    def test_to_chat_msgs_returns_list(self):
        """branch.msgs.to_chat_msgs() returns a list."""
        from lionagi import Branch

        branch = Branch(system="Test system.")
        chat_msgs = branch.msgs.to_chat_msgs()

        assert isinstance(chat_msgs, list)
        assert len(chat_msgs) >= 1  # at least the system message

    def test_clear_messages_works(self):
        """branch.msgs.clear_messages() clears non-system messages."""
        from lionagi import Branch

        branch = Branch(system="System prompt.")
        initial_count = len(branch.messages)
        assert initial_count >= 1

        branch.msgs.clear_messages()
        # After clearing, only system message (if any) remains
        remaining = len(branch.messages)
        assert remaining <= 1

    def test_logs_exists_as_collection(self):
        """branch.logs exists and is a Pile collection."""
        from lionagi import Branch
        from lionagi.protocols.generic.pile import Pile

        branch = Branch()
        assert hasattr(branch, "logs")
        assert isinstance(branch.logs, Pile)

    def test_message_type_imports_resolve(self):
        """All documented message types can be imported."""
        from lionagi.protocols.messages import (  # noqa: F401
            ActionRequest,
            ActionResponse,
            AssistantResponse,
            Instruction,
            MessageRole,
            RoledMessage,
            System,
        )

        assert RoledMessage is not None
        assert System is not None
        assert Instruction is not None
        assert AssistantResponse is not None
        assert ActionRequest is not None
        assert ActionResponse is not None
        assert MessageRole is not None


# =============================================================================
# Pattern Selection (pattern-selection.md)
# =============================================================================


class TestPatternSelection:
    """Tests derived from pattern-selection.md examples."""

    EXPECTED_METHODS = [
        "communicate",
        "chat",
        "operate",
        "parse",
        "ReAct",
        "interpret",
        "act",
    ]

    def test_all_branch_operations_exist(self):
        """All 7 documented Branch operations exist as attributes."""
        from lionagi import Branch

        branch = Branch()
        for method_name in self.EXPECTED_METHODS:
            assert hasattr(branch, method_name), f"Branch missing method: {method_name}"

    def test_all_branch_operations_are_callable(self):
        """All 7 documented Branch operations are callable."""
        from lionagi import Branch

        branch = Branch()
        for method_name in self.EXPECTED_METHODS:
            method = getattr(branch, method_name)
            assert callable(method), f"Branch.{method_name} is not callable"

    def test_all_branch_operations_are_coroutines(self):
        """All 7 documented Branch operations are async (coroutine functions)."""
        from lionagi import Branch

        branch = Branch()
        for method_name in self.EXPECTED_METHODS:
            method = getattr(branch, method_name)
            assert inspect.iscoroutinefunction(method), (
                f"Branch.{method_name} is not a coroutine function"
            )

    def test_communicate_signature(self):
        """branch.communicate accepts instruction as first positional arg."""
        from lionagi import Branch

        sig = inspect.signature(Branch.communicate)
        params = list(sig.parameters.keys())
        assert "instruction" in params

    def test_chat_signature(self):
        """branch.chat accepts instruction parameter."""
        from lionagi import Branch

        sig = inspect.signature(Branch.chat)
        params = list(sig.parameters.keys())
        assert "instruction" in params

    def test_operate_signature(self):
        """branch.operate accepts instruction parameter."""
        from lionagi import Branch

        sig = inspect.signature(Branch.operate)
        params = list(sig.parameters.keys())
        assert "instruction" in params

    def test_parse_signature(self):
        """branch.parse accepts text parameter."""
        from lionagi import Branch

        sig = inspect.signature(Branch.parse)
        params = list(sig.parameters.keys())
        assert "text" in params


# =============================================================================
# Claude Code Usage (claude-code-usage.md)
# =============================================================================


class TestClaudeCodeUsage:
    """Tests derived from claude-code-usage.md examples."""

    def test_cli_provider_claude_code(self):
        """iModel(provider='claude_code') constructs without error."""
        from lionagi import iModel

        model = iModel(provider="claude_code")
        assert model is not None

    def test_cli_provider_gemini_code(self):
        """iModel(provider='gemini_code') constructs without error."""
        from lionagi import iModel

        model = iModel(provider="gemini_code")
        assert model is not None

    def test_cli_provider_codex(self):
        """iModel(provider='codex') constructs without error."""
        from lionagi import iModel

        model = iModel(provider="codex")
        assert model is not None

    def test_multi_branch_orchestration_pattern(self):
        """Multiple branches can model different agent roles."""
        from lionagi import Branch, iModel

        model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")

        researcher = Branch(
            system="You are a research agent.",
            name="researcher",
            chat_model=model,
        )
        writer = Branch(
            system="You are a writing agent.",
            name="writer",
            chat_model=model,
        )
        reviewer = Branch(
            system="You are a review agent.",
            name="reviewer",
            chat_model=model,
        )

        branches = [researcher, writer, reviewer]
        assert len(branches) == 3
        assert all(isinstance(b, Branch) for b in branches)
        names = [b.name for b in branches]
        assert "researcher" in names
        assert "writer" in names
        assert "reviewer" in names

    def test_fan_out_pattern_independent_state(self):
        """Fan-out: multiple branches maintain independent state."""
        from lionagi import Branch, iModel

        model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")

        branches = []
        for i in range(3):
            b = Branch(
                system=f"Worker {i} instructions.",
                name=f"worker_{i}",
                chat_model=model,
            )
            branches.append(b)

        # Each branch has its own independent message state
        assert len(branches) == 3
        ids = [b.id for b in branches]
        assert len(set(ids)) == 3  # all unique IDs

        # Each branch has its own system message
        for i, b in enumerate(branches):
            assert b.system is not None
            assert b.name == f"worker_{i}"

    def test_session_new_branch(self):
        """session.new_branch() creates and includes a branch."""
        from lionagi import Session

        session = Session()
        branch = session.new_branch(
            system="New branch system.",
            name="custom_branch",
        )

        assert isinstance(branch, type(session.default_branch))
        assert branch in session.branches
