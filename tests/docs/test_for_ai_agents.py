"""Tests for for-ai-agents documentation examples.

Covers code patterns from:
- orchestration-guide.md
- self-improvement.md
- pattern-selection.md
- claude-code-usage.md
"""

import inspect

import pytest

from lionagi.testing import LionAGIMockFactory

# =============================================================================
# Orchestration Guide (orchestration-guide.md)
# =============================================================================


class TestOrchestrationGuide:
    """Tests derived from orchestration-guide.md examples."""

    def test_branch_construction_full_params(self):
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

    def test_session_and_builder_workflow_construction(self):
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

    def test_clear_messages_works(self):
        from lionagi import Branch

        branch = Branch(system="System prompt.")
        initial_count = len(branch.messages)
        assert initial_count >= 1

        branch.msgs.clear_messages()
        # After clearing, only system message (if any) remains
        remaining = len(branch.messages)
        assert remaining <= 1

    def test_message_type_imports_resolve(self):
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

    def test_all_branch_operations_are_coroutines(self):
        from lionagi import Branch

        branch = Branch()
        for method_name in self.EXPECTED_METHODS:
            method = getattr(branch, method_name)
            assert inspect.iscoroutinefunction(method), (
                f"Branch.{method_name} is not a coroutine function"
            )

    def test_communicate_signature(self):
        from lionagi import Branch

        sig = inspect.signature(Branch.communicate)
        params = list(sig.parameters.keys())
        assert "instruction" in params

    def test_chat_signature(self):
        from lionagi import Branch

        sig = inspect.signature(Branch.chat)
        params = list(sig.parameters.keys())
        assert "instruction" in params

    def test_operate_signature(self):
        from lionagi import Branch

        sig = inspect.signature(Branch.operate)
        params = list(sig.parameters.keys())
        assert "instruction" in params

    def test_parse_signature(self):
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
        from lionagi import iModel

        model = iModel(provider="claude_code")
        assert model is not None

    def test_cli_provider_gemini_code(self):
        from lionagi import iModel

        model = iModel(provider="gemini_code")
        assert model is not None

    def test_cli_provider_codex(self):
        from lionagi import iModel

        model = iModel(provider="codex")
        assert model is not None

    def test_multi_branch_orchestration_pattern(self):
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
        from lionagi import Session

        session = Session()
        branch = session.new_branch(
            system="New branch system.",
            name="custom_branch",
        )

        assert isinstance(branch, type(session.default_branch))
        assert branch in session.branches
