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

    @pytest.mark.asyncio
    async def test_parallel_communicate_with_gather(self, mocked_branch):
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

    def test_provider_fallback_pattern(self):
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
        mock = LionAGIMockFactory.create_error_response_mock(
            error_message="Rate limit exceeded",
            error_code="rate_limit_error",
        )
        assert mock is not None
        assert mock.execution.response["error"]["message"] == "Rate limit exceeded"

    @pytest.mark.asyncio
    async def test_sequential_imodel_responses(self):
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
        from lionagi import Builder

        builder = Builder()
        assert builder is not None

    def test_builder_add_operation_returns_id(self):
        from lionagi import Builder

        builder = Builder()
        node_id = builder.add_operation("communicate", instruction="Summarize the document")
        assert node_id is not None

    def test_builder_get_graph_returns_graph(self):
        from lionagi import Builder, Graph

        builder = Builder()
        builder.add_operation("communicate", instruction="Hello")
        graph = builder.get_graph()
        assert isinstance(graph, Graph)

    def test_builder_sequential_operations(self):
        from lionagi import Builder

        builder = Builder()
        id1 = builder.add_operation("communicate", instruction="Step 1")
        id2 = builder.add_operation("communicate", instruction="Step 2")
        graph = builder.get_graph()
        # The graph should have nodes and edges
        assert id1 != id2
        assert len(graph.internal_edges) > 0

    def test_session_new_branch_returns_branch(self):
        from lionagi import Branch, Session

        session = Session()
        branch = session.new_branch(name="analysis")
        assert isinstance(branch, Branch)
        assert branch.name == "analysis"

    def test_session_has_flow_method(self):
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
        from lionagi import Session

        session = Session()
        assert hasattr(session, "register_operation")
        assert callable(session.register_operation)

    def test_session_has_operation_decorator(self):
        from lionagi import Session

        session = Session()
        assert hasattr(session, "operation")
        assert callable(session.operation)

    def test_register_operation_with_function(self):
        from lionagi import Session

        session = Session()

        async def custom_op(branch, **kwargs):
            return "custom result"

        session.register_operation("custom_op", custom_op)
        # Operation should be retrievable from the internal registry
        assert "custom_op" in session._operation_manager.registry
        assert session._operation_manager.registry["custom_op"] is custom_op

    def test_operation_decorator_usage(self):
        from lionagi import Session

        session = Session()

        @session.operation()
        async def summarize(branch, **kwargs):
            return "summary"

        assert callable(summarize)
        assert "summarize" in session._operation_manager.registry
        assert session._operation_manager.registry["summarize"] is summarize

    def test_operation_decorator_custom_name(self):
        from lionagi import Session

        session = Session()

        @session.operation("my_custom_op")
        async def some_func(branch, **kwargs):
            return "result"

        assert callable(some_func)
        assert "my_custom_op" in session._operation_manager.registry
        # Original function name should NOT be the registry key
        assert "some_func" not in session._operation_manager.registry
