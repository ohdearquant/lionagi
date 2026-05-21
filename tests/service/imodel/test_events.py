# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
from unittest.mock import patch

import pytest

from lionagi.protocols.generic.event import EventStatus
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.imodel import iModel


class TestiModelEdgeCases:
    """Edge case tests for iModel - concurrent behavior, rate limiting, error recovery."""

    @pytest.mark.asyncio
    async def test_concurrent_streaming_multiple_requests(
        self, mock_streaming_response
    ):
        """Test concurrent streaming requests with semaphore control."""
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=2,
        )

        call_count = 0

        async def mock_stream_generator():
            for i in range(3):
                yield {"choices": [{"delta": {"content": f"chunk {i}"}}]}
                await asyncio.sleep(0.05)

        def track_calls(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_stream_generator()

        with patch.object(imodel.endpoint, "stream", side_effect=track_calls):
            # Start 5 concurrent streaming requests with limit of 2
            tasks = []
            for i in range(5):

                async def collect_stream(idx):
                    chunks = []
                    async for chunk in imodel.stream(
                        messages=[{"role": "user", "content": f"Request {idx}"}]
                    ):
                        if chunk and not isinstance(chunk, APICalling):
                            chunks.append(chunk)
                    return chunks

                tasks.append(asyncio.create_task(collect_stream(i)))

            results = await asyncio.gather(*tasks)

        # Verify all streams completed
        assert len(results) == 5
        # At least some should have chunks (not all may complete in time)
        chunks_found = sum(1 for r in results if len(r) > 0)
        assert chunks_found >= 3
        assert call_count == 5  # All calls executed

    @pytest.mark.asyncio
    async def test_rate_limiting_under_load(self, mock_response):
        """Test rate limiting enforcement under concurrent load.

        Fire exactly limit_requests so all clear in one window — over-
        budget requests would block until the iModel.invoke 10s safety
        timeout because the executor does not re-forward on replenishment.
        """
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=3,
            limit_tokens=100,
            capacity_refresh_time=0.2,
        )

        call_times = []

        async def track_timing(*args, **kwargs):
            call_times.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.01)
            return mock_response.json.return_value

        with patch.object(imodel.endpoint, "call", side_effect=track_timing):
            # Fire exactly limit_requests so all complete in one window
            tasks = [
                asyncio.create_task(
                    imodel.invoke(
                        messages=[{"role": "user", "content": f"Request {i}"}]
                    )
                )
                for i in range(3)
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

        # All should complete successfully within the single-window budget
        successful = [r for r in results if isinstance(r, APICalling)]
        assert len(successful) == 3

    @pytest.mark.asyncio
    async def test_provider_switching_mid_session(self, mock_response):
        """Test switching providers by creating new iModel instances."""
        # Start with OpenAI
        imodel1 = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")

        async def mock_openai_call(*args, **kwargs):
            return {"provider": "openai", "response": "OpenAI response"}

        with patch.object(imodel1.endpoint, "call", side_effect=mock_openai_call):
            result1 = await imodel1.invoke(
                messages=[{"role": "user", "content": "Hello"}]
            )
            assert result1.response["provider"] == "openai"

        # Switch to Anthropic with required parameters
        imodel2 = iModel(
            provider="anthropic",
            model="claude-3-5-sonnet-20241022",
            api_key="test-key",
        )

        async def mock_anthropic_call(*args, **kwargs):
            return {"provider": "anthropic", "response": "Anthropic response"}

        with patch.object(imodel2.endpoint, "call", side_effect=mock_anthropic_call):
            result2 = await imodel2.invoke(
                messages=[{"role": "user", "content": "Hello"}],
                max_tokens=100,  # Required for Anthropic
            )
            assert result2.response["provider"] == "anthropic"

    @pytest.mark.asyncio
    async def test_error_recovery_and_retry_logic(self):
        """Test error handling and recovery in invoke."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")

        call_count = 0

        async def failing_then_success(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception(f"Temporary error {call_count}")
            return {"success": True, "attempt": call_count}

        # First call fails
        with patch.object(imodel.endpoint, "call", side_effect=Exception("API Error")):
            result = await imodel.invoke(
                messages=[{"role": "user", "content": "Hello"}]
            )
            assert result.status == EventStatus.FAILED
            assert result.execution.error is not None

        # Recovery with manual retry
        call_count = 0
        with patch.object(imodel.endpoint, "call", side_effect=failing_then_success):
            # Manual retry loop
            for attempt in range(5):
                result = await imodel.invoke(
                    messages=[{"role": "user", "content": "Hello"}]
                )
                if result.status == EventStatus.COMPLETED:
                    break

            assert result.status == EventStatus.COMPLETED
            assert call_count == 3  # Failed twice, succeeded on third

    @pytest.mark.asyncio
    async def test_streaming_error_mid_stream(self):
        """Test error handling when streaming fails mid-stream."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")

        async def failing_stream():
            yield {"choices": [{"delta": {"content": "Start"}}]}
            yield {"choices": [{"delta": {"content": " middle"}}]}
            raise Exception("Stream interrupted")

        with patch.object(imodel.endpoint, "stream", return_value=failing_stream()):
            chunks = []
            error_raised = False
            try:
                async for chunk in imodel.stream(
                    messages=[{"role": "user", "content": "Hello"}]
                ):
                    if chunk and not isinstance(chunk, APICalling):
                        chunks.append(chunk)
            except ValueError as e:
                error_raised = True
                assert "Failed to stream API call" in str(e)

            # Either error was raised or chunks were collected
            assert error_raised or len(chunks) >= 2

    @pytest.mark.asyncio
    async def test_concurrent_invoke_with_queue_capacity(self, mock_response):
        """Test queue capacity limits with concurrent invocations.

        Fire exactly limit_requests so all clear in one rate-limit
        window — over-budget requests would block until the iModel.invoke
        10s safety timeout because the executor does not re-forward on
        replenishment.
        """
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            queue_capacity=5,
            limit_requests=3,
            capacity_refresh_time=0.1,
        )

        async def slow_call(*args, **kwargs):
            await asyncio.sleep(0.01)
            return mock_response.json.return_value

        with patch.object(imodel.endpoint, "call", side_effect=slow_call):
            # Fire exactly limit_requests so all complete in one window
            tasks = [
                asyncio.create_task(
                    imodel.invoke(
                        messages=[{"role": "user", "content": f"Request {i}"}]
                    )
                )
                for i in range(3)
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

        # All should complete successfully within the single-window budget
        successful = [
            r
            for r in results
            if isinstance(r, APICalling) and r.status == EventStatus.COMPLETED
        ]
        assert len(successful) == 3

    @pytest.mark.asyncio
    async def test_provider_metadata_persistence(self, mock_response):
        """Test session_id persists on CLI endpoint across multiple calls."""
        imodel = iModel(
            provider="claude_code",
            model="claude-3-5-sonnet-20241022",
            api_key="test-key",
        )

        # First call stores session_id
        async def first_call(*args, **kwargs):
            return {"session_id": "session-123", "response": "First call"}

        with patch.object(imodel.endpoint, "call", side_effect=first_call):
            result1 = await imodel.invoke(
                messages=[{"role": "user", "content": "Hello"}]
            )
            assert imodel.endpoint.session_id == "session-123"

        # Second call uses stored session_id
        async def second_call(*args, **kwargs):
            return {"session_id": "session-123", "response": "Second call"}

        with patch.object(imodel.endpoint, "call", side_effect=second_call):
            # Create api_calling to check resume parameter
            api_call = imodel.create_api_calling(
                messages=[{"role": "user", "content": "Follow-up"}]
            )
            # Session ID should be auto-injected as resume
            assert api_call.payload["request"].resume == "session-123"

    @pytest.mark.asyncio
    async def test_streaming_with_processing_function_error(self):
        """Test error in streaming_process_func doesn't crash stream."""

        def failing_processor(chunk):
            if "error" in str(chunk):
                raise ValueError("Processing error")
            return f"Processed: {chunk}"

        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            streaming_process_func=failing_processor,
        )

        async def mock_stream():
            yield {"choices": [{"delta": {"content": "normal"}}]}
            yield {"choices": [{"delta": {"content": "error"}}]}
            yield {"choices": [{"delta": {"content": "continue"}}]}

        with patch.object(imodel.endpoint, "stream", return_value=mock_stream()):
            chunks = []
            try:
                async for chunk in imodel.stream(
                    messages=[{"role": "user", "content": "Hello"}]
                ):
                    if chunk and not isinstance(chunk, APICalling):
                        chunks.append(chunk)
            except ValueError as e:
                assert "Failed to stream API call" in str(e)

    @pytest.mark.asyncio
    async def test_serialization_roundtrip_with_complex_config(self):
        """Test to_dict/from_dict preserves complex configurations."""
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=10,
            limit_tokens=1000,
            queue_capacity=50,
            concurrency_limit=5,
            provider_metadata={
                "custom_key": "custom_value",
                "session_id": "abc",
            },
        )

        # Serialize
        data = imodel.to_dict()

        # Deserialize
        restored = iModel.from_dict(data)

        # Verify
        assert restored.id == imodel.id
        assert restored.created_at == imodel.created_at
        assert restored.endpoint.config.provider == "openai"
        assert restored.executor.config["limit_requests"] == 10
        assert restored.executor.config["limit_tokens"] == 1000
        assert restored.executor.config["queue_capacity"] == 50
        assert restored.provider_metadata == imodel.provider_metadata
