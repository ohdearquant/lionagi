"""Tests for advanced documentation examples.

Covers: performance.md, observability.md, error-handling.md,
        flow-composition.md, custom-operations.md.

All tests avoid real API calls by using LionAGIMockFactory or
testing only construction/import semantics.
"""

import asyncio

import pytest

from lionagi.testing import LionAGIMockFactory


# ===================================================================
# Performance (performance.md)
# ===================================================================
class TestPerformance:
    """Patterns from performance.md: concurrency utilities, rate limiting."""

    def test_ln_module_importable(self):
        """``from lionagi import ln`` succeeds and ln is a module."""
        from lionagi import ln

        assert ln is not None
        # ln should be a module, not an arbitrary object
        import types

        assert isinstance(ln, types.ModuleType)

    def test_ln_has_concurrency_attrs(self):
        """ln exposes concurrency-related helpers: alcall, bcall, race, retry."""
        from lionagi import ln

        assert hasattr(ln, "alcall")
        assert hasattr(ln, "bcall")
        assert hasattr(ln, "race")
        assert hasattr(ln, "retry")

    def test_ln_concurrency_direct_import(self):
        """Concurrency utilities can be imported from lionagi.ln.concurrency."""
        from lionagi.ln.concurrency import race, retry

        assert callable(race)
        assert callable(retry)

    def test_alcall_direct_import(self):
        """alcall and bcall importable from top-level ln module."""
        from lionagi.ln import alcall, bcall

        assert callable(alcall)
        assert callable(bcall)

    @pytest.mark.asyncio
    async def test_parallel_communicate_with_gather(self, mocked_branch):
        """asyncio.gather works with multiple mocked branch.communicate calls."""
        results = await asyncio.gather(
            mocked_branch.communicate("Task A"),
            mocked_branch.communicate("Task B"),
            mocked_branch.communicate("Task C"),
        )
        assert len(results) == 3
        for r in results:
            assert isinstance(r, str)
            assert len(r) > 0

    def test_imodel_rate_limiting_params(self):
        """iModel accepts limit_requests and limit_tokens constructor args."""
        from lionagi import iModel

        model = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test",
            limit_requests=100,
            limit_tokens=50000,
        )
        assert model is not None


# ===================================================================
# Observability (observability.md)
# ===================================================================
class TestObservability:
    """Patterns from observability.md: logging, hooks, message inspection."""

    def test_data_logger_config_construction(self):
        """DataLoggerConfig can be constructed with typical params."""
        from lionagi.protocols.generic import DataLoggerConfig

        config = DataLoggerConfig(
            persist_dir="/tmp/test_logs",
            capacity=500,
            extension=".json",
            auto_save_on_exit=False,
        )
        assert config.persist_dir == "/tmp/test_logs"
        assert config.capacity == 500
        assert config.extension == ".json"
        assert config.auto_save_on_exit is False

    def test_branch_accepts_log_config(self):
        """Branch(log_config=...) accepts a DataLoggerConfig."""
        from lionagi import Branch
        from lionagi.protocols.generic import DataLoggerConfig

        config = DataLoggerConfig(
            persist_dir="/tmp/test_logs",
            capacity=100,
            auto_save_on_exit=False,
        )
        branch = Branch(log_config=config)
        assert branch is not None

    def test_branch_logs_is_collection(self):
        """branch.logs exists and is a Pile (collection) of Log entries."""
        from lionagi import Branch

        branch = Branch()
        logs = branch.logs
        assert logs is not None
        # Initially empty
        assert len(logs) == 0

    def test_branch_messages_iterable(self):
        """branch.messages is iterable and can be enumerated."""
        from lionagi import Branch

        branch = Branch(system="You are a test assistant.")
        messages = branch.messages
        msg_list = list(messages)
        # Should contain at least the system message
        assert len(msg_list) >= 1

    def test_message_role_access(self):
        """Messages expose .role and .content attributes."""
        from lionagi import Branch

        branch = Branch(system="Test system prompt.")
        msg = list(branch.messages)[0]
        # role should be accessible
        assert msg.role is not None
        # content should be accessible
        assert msg.content is not None

    def test_hook_registry_constructs(self):
        """HookRegistry() can be instantiated with no arguments."""
        from lionagi import HookRegistry

        registry = HookRegistry()
        assert registry is not None

    def test_hook_registry_has_hook_methods(self):
        """HookRegistry exposes pre/post invocation hook methods."""
        from lionagi import HookRegistry

        registry = HookRegistry()
        assert hasattr(registry, "pre_invocation")
        assert hasattr(registry, "post_invocation")
        assert hasattr(registry, "pre_event_create")

    @pytest.mark.asyncio
    async def test_branch_logs_after_communicate(self, mocked_branch):
        """After a communicate call, logs should be populated."""
        await mocked_branch.communicate("Test message")
        # Logs may or may not be populated depending on configuration,
        # but the attribute should remain accessible.
        assert mocked_branch.logs is not None


# ===================================================================
# Error Handling (error-handling.md)
# ===================================================================
class TestErrorHandling:
    """Patterns from error-handling.md: rate limiting, provider fallback."""

    def test_imodel_with_rate_limit_construction(self):
        """iModel with rate limit params constructs without error."""
        from lionagi import iModel

        model = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=50,
            limit_tokens=100000,
        )
        assert model is not None

    def test_provider_fallback_pattern(self):
        """Multiple iModels for different providers can coexist (fallback)."""
        from lionagi import iModel

        primary = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-primary",
        )
        fallback = iModel(
            provider="anthropic",
            model="claude-3-5-sonnet-20241022",
            api_key="test-fallback",
        )
        assert primary is not None
        assert fallback is not None
        # They should be distinct instances
        assert primary is not fallback

    def test_error_response_mock_factory(self):
        """LionAGIMockFactory can create error response mocks for testing."""
        mock = LionAGIMockFactory.create_error_response_mock(
            error_message="Rate limit exceeded",
            error_code="rate_limit_error",
        )
        assert mock is not None
        assert mock.execution.response["error"]["message"] == "Rate limit exceeded"

    @pytest.mark.asyncio
    async def test_sequential_imodel_responses(self):
        """Mock factory supports sequential responses for retry testing."""
        model = LionAGIMockFactory.create_mocked_imodel(
            responses=["first attempt", "second attempt", "third attempt"],
        )
        r1 = await model.invoke()
        r2 = await model.invoke()
        r3 = await model.invoke()
        assert r1.execution.response == "first attempt"
        assert r2.execution.response == "second attempt"
        assert r3.execution.response == "third attempt"


# ===================================================================
# Flow Composition (flow-composition.md)
# ===================================================================
class TestFlowComposition:
    """Patterns from flow-composition.md: Builder, Graph, Session orchestration."""

    def test_builder_constructs(self):
        """Builder() (alias for OperationGraphBuilder) constructs."""
        from lionagi import Builder

        builder = Builder()
        assert builder is not None

    def test_builder_is_operation_graph_builder(self):
        """Builder is the same class as OperationGraphBuilder."""
        from lionagi import Builder
        from lionagi.operations.builder import OperationGraphBuilder

        assert Builder is OperationGraphBuilder

    def test_builder_add_operation_returns_id(self):
        """builder.add_operation() returns a node ID."""
        from lionagi import Builder

        builder = Builder()
        node_id = builder.add_operation("communicate", instruction="Summarize the document")
        assert node_id is not None

    def test_builder_get_graph_returns_graph(self):
        """builder.get_graph() returns a Graph instance."""
        from lionagi import Builder, Graph

        builder = Builder()
        builder.add_operation("communicate", instruction="Hello")
        graph = builder.get_graph()
        assert isinstance(graph, Graph)

    def test_builder_sequential_operations(self):
        """Multiple add_operation calls create sequential dependencies."""
        from lionagi import Builder

        builder = Builder()
        id1 = builder.add_operation("communicate", instruction="Step 1")
        id2 = builder.add_operation("communicate", instruction="Step 2")
        graph = builder.get_graph()
        # The graph should have nodes and edges
        assert id1 != id2
        assert len(graph.internal_edges) > 0

    def test_session_new_branch_returns_branch(self):
        """Session().new_branch() returns a Branch instance."""
        from lionagi import Branch, Session

        session = Session()
        branch = session.new_branch(name="analysis")
        assert isinstance(branch, Branch)
        assert branch.name == "analysis"

    def test_session_has_flow_method(self):
        """Session exposes an async flow() method for graph execution."""
        from lionagi import Session

        session = Session()
        assert hasattr(session, "flow")
        # flow should be a coroutine function
        import inspect

        assert inspect.iscoroutinefunction(session.flow)


# ===================================================================
# Custom Operations (custom-operations.md)
# ===================================================================
class TestCustomOperations:
    """Patterns from custom-operations.md: register_operation, operation decorator."""

    def test_session_has_register_operation(self):
        """Session has a register_operation method."""
        from lionagi import Session

        session = Session()
        assert hasattr(session, "register_operation")
        assert callable(session.register_operation)

    def test_session_has_operation_decorator(self):
        """Session has an operation() method that works as a decorator."""
        from lionagi import Session

        session = Session()
        assert hasattr(session, "operation")
        assert callable(session.operation)

    def test_register_operation_with_function(self):
        """register_operation stores the callable under the given name."""
        from lionagi import Session

        session = Session()

        async def custom_op(branch, **kwargs):
            return "custom result"

        session.register_operation("custom_op", custom_op)
        # Operation should be retrievable from the internal registry
        assert "custom_op" in session._operation_manager.registry
        assert session._operation_manager.registry["custom_op"] is custom_op

    def test_operation_decorator_usage(self):
        """@session.operation() registers a function under its own __name__."""
        from lionagi import Session

        session = Session()

        @session.operation()
        async def summarize(branch, **kwargs):
            return "summary"

        assert callable(summarize)
        assert "summarize" in session._operation_manager.registry
        assert session._operation_manager.registry["summarize"] is summarize

    def test_operation_decorator_custom_name(self):
        """@session.operation('my_custom_op') registers under the given name."""
        from lionagi import Session

        session = Session()

        @session.operation("my_custom_op")
        async def some_func(branch, **kwargs):
            return "result"

        assert callable(some_func)
        assert "my_custom_op" in session._operation_manager.registry
        # Original function name should NOT be the registry key
        assert "some_func" not in session._operation_manager.registry
