# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import os
from unittest.mock import patch

import pytest

from lionagi.protocols.generic.event import EventStatus
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.hooks import HookRegistry
from lionagi.service.imodel import iModel


class TestiModelRateLimitingEdgeCases:
    """Tests for rate limiting edge cases and boundary conditions."""

    def test_zero_rate_limits(self):
        """Test iModel accepts zero rate limits (no limiting)."""
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=0,
            limit_tokens=0,
        )
        # Zero means unlimited
        assert imodel.executor.config["limit_requests"] == 0

    def test_negative_rate_limits(self):
        """Test iModel accepts negative rate limits (no limiting)."""
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=-10,
        )
        # Negative values may be treated as unlimited
        assert imodel.executor.config["limit_requests"] == -10

    def test_extremely_high_rate_limits(self):
        """Test iModel with extremely high rate limits."""
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=1000000,
            limit_tokens=100000000,
        )
        assert imodel.executor.config["limit_requests"] == 1000000

    def test_zero_queue_capacity(self):
        """Test iModel accepts zero queue capacity (unlimited queue)."""
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            queue_capacity=0,
        )
        # Zero may mean unlimited queue
        assert imodel.executor.config["queue_capacity"] == 0

    def test_capacity_refresh_time_boundary(self):
        """Test iModel with boundary capacity refresh times."""
        # Very short refresh time
        imodel1 = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            capacity_refresh_time=0.001,
        )
        assert imodel1.executor.config["capacity_refresh_time"] == 0.001

        # Very long refresh time
        imodel2 = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            capacity_refresh_time=3600.0,
        )
        assert imodel2.executor.config["capacity_refresh_time"] == 3600.0

    def test_zero_concurrency_limit(self):
        """Test iModel with zero concurrency limit uses default."""
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=0,
        )
        # Zero gets converted to default (100)
        assert imodel.executor.concurrency_limit == 100

    def test_single_concurrency_limit(self):
        """Test iModel with concurrency limit of 1."""
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=1,
        )
        assert imodel.executor.concurrency_limit == 1

    @pytest.mark.asyncio
    async def test_rate_limit_token_counting(self, mock_response):
        """Test that token counting is tracked for rate limiting."""
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=100,
            limit_tokens=10000,
        )

        # Create response with token usage
        response_with_tokens = {
            "choices": [{"message": {"content": "test"}}],
            "usage": {"total_tokens": 50},
        }

        with patch.object(imodel.endpoint, "call", return_value=response_with_tokens):
            result = await imodel.invoke(
                messages=[{"role": "user", "content": "test"}],
                include_token_usage_to_model=True,
            )

        assert result.status == EventStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_burst_requests_rate_limiting(self, mock_response):
        """Test rate limiting behavior with burst of requests.

        Rate-limited requests over the per-window budget stay PENDING
        until the next refresh, and currently the executor's forward()
        is not re-invoked on replenishment — those calls would block
        until the iModel.invoke 10s safety timeout. Fire exactly
        limit_requests so the burst all clears in one window and the
        assertion runs without paying for that timeout.
        """
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=5,
            capacity_refresh_time=0.2,
        )

        call_count = 0

        async def count_calls(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.005)
            return mock_response.json.return_value

        with patch.object(imodel.endpoint, "call", side_effect=count_calls):
            # Fire exactly limit_requests so all clear in one window
            tasks = [
                asyncio.create_task(
                    imodel.invoke(
                        messages=[{"role": "user", "content": f"Request {i}"}]
                    )
                )
                for i in range(5)
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

        # All requests should complete within the single-window budget
        successful = [r for r in results if isinstance(r, APICalling)]
        assert len(successful) == 5
        assert call_count == 5
