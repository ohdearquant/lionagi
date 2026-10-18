# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
from unittest.mock import patch

import pytest

from lionagi.protocols.generic.event import EventStatus
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.imodel import iModel


class TestiModelRateLimitingEdgeCases:
    """Tests for rate limiting edge cases and boundary conditions."""

    def test_zero_rate_limits(self):
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
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=-10,
        )
        # Negative values may be treated as unlimited
        assert imodel.executor.config["limit_requests"] == -10

    def test_extremely_high_rate_limits(self):
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=1000000,
            limit_tokens=100000000,
        )
        assert imodel.executor.config["limit_requests"] == 1000000

    def test_zero_queue_capacity(self):
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            queue_capacity=0,
        )
        # Zero may mean unlimited queue
        assert imodel.executor.config["queue_capacity"] == 0

    def test_capacity_refresh_time_boundary(self):
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
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=0,
        )
        # Zero gets converted to default (100)
        assert imodel.executor.concurrency_limit == 100

    def test_single_concurrency_limit(self):
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            concurrency_limit=1,
        )
        assert imodel.executor.concurrency_limit == 1

    @pytest.mark.asyncio
    async def test_rate_limit_token_counting(self, mock_response):
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
        """Fire exactly limit_requests so the burst all clears in one rate-limit window."""
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
                    imodel.invoke(messages=[{"role": "user", "content": f"Request {i}"}])
                )
                for i in range(5)
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

        # All requests should complete within the single-window budget
        successful = [r for r in results if isinstance(r, APICalling)]
        assert len(successful) == 5
        assert call_count == 5

    @pytest.mark.asyncio
    async def test_over_budget_burst_completes_not_dropped(self, mock_response):
        """Regression: denied event was dequeued and left PENDING outside the queue — over-budget calls lost until 10s timeout.

        Fix re-enqueues deferred events; replenisher re-drives process() on each refresh so every over-budget call completes.
        """
        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            limit_requests=3,
            capacity_refresh_time=0.1,
        )

        call_count = 0

        async def count_calls(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.005)
            return mock_response.json.return_value

        with patch.object(imodel.endpoint, "call", side_effect=count_calls):
            # Fire well over the per-window budget (3) in one burst.
            tasks = [
                asyncio.create_task(
                    imodel.invoke(messages=[{"role": "user", "content": f"Request {i}"}])
                )
                for i in range(8)
            ]
            # Comfortably within invoke()'s 10s safety timeout, but spanning
            # several 0.1s refresh windows so deferred work is re-driven.
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=8.0
            )

        successful = [r for r in results if isinstance(r, APICalling)]
        completed = [r for r in successful if r.status == EventStatus.COMPLETED]
        # None dropped: all 8 over-budget calls eventually completed.
        assert len(completed) == 8
        assert call_count == 8


@pytest.mark.asyncio
async def test_unlimited_processor_does_not_deny_on_queue_occupancy():
    """An unlimited processor drains a queue larger than its capacity.

    ``queue_capacity`` bounds how many events one ``process()`` pass starts;
    it is not the queue's size. An unlimited processor must not refuse an
    event merely because more than that many events are waiting, or every
    event is re-enqueued and the first pass starts nothing.
    """
    from lionagi.protocols.generic.event import Event
    from lionagi.service.rate_limited_processor import RateLimitedAPIProcessor

    class _Call(Event):
        async def _invoke(self):
            return "ok"

    processor = RateLimitedAPIProcessor(
        queue_capacity=1,
        capacity_refresh_time=60,
    )
    events = [_Call() for _ in range(3)]
    for event in events:
        await processor.enqueue(event)

    await processor.process()
    assert events[0].status == EventStatus.COMPLETED, (
        "an unlimited processor denied every queued event"
    )

    for _ in range(len(events)):
        if processor.queue.empty():
            break
        await processor.process()

    assert processor.queue.empty()
    assert all(event.status == EventStatus.COMPLETED for event in events), (
        f"an unlimited processor left work pending: {[e.status for e in events]}"
    )
