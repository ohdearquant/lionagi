# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Processor backpressure features: try_enqueue, queue_full, max_queue_size."""

import asyncio

import pytest

from lionagi.protocols.generic.event import Event
from lionagi.protocols.generic.processor import Processor


class DummyEvent(Event):
    """Minimal Event subclass for testing."""

    async def _invoke(self):
        self.execution.response = "done"


class DummyProcessor(Processor):
    event_type = DummyEvent


class TestProcessorBackpressure:
    def test_queue_full_unlimited(self):
        """max_queue_size=0 means unlimited — queue_full always False."""
        p = DummyProcessor(
            queue_capacity=10,
            capacity_refresh_time=1.0,
            concurrency_limit=5,
            max_queue_size=0,
        )
        assert p.queue_full is False
        assert p.max_queue_size == 0

    @pytest.mark.anyio
    async def test_queue_full_with_limit(self):
        """queue_full reflects actual queue state."""
        p = DummyProcessor(
            queue_capacity=10,
            capacity_refresh_time=1.0,
            concurrency_limit=5,
            max_queue_size=2,
        )
        assert p.queue_full is False

        e1 = DummyEvent()
        e2 = DummyEvent()
        await p.enqueue(e1)
        assert p.queue_full is False  # 1/2

        await p.enqueue(e2)
        assert p.queue_full is True  # 2/2

    @pytest.mark.anyio
    async def test_try_enqueue_success(self):
        """try_enqueue returns True when queue has space."""
        p = DummyProcessor(
            queue_capacity=10,
            capacity_refresh_time=1.0,
            concurrency_limit=5,
            max_queue_size=5,
        )
        e = DummyEvent()
        assert p.try_enqueue(e) is True
        assert p.queue.qsize() == 1

    @pytest.mark.anyio
    async def test_try_enqueue_full(self):
        """try_enqueue returns False when queue is at capacity."""
        p = DummyProcessor(
            queue_capacity=10,
            capacity_refresh_time=1.0,
            concurrency_limit=5,
            max_queue_size=2,
        )
        e1 = DummyEvent()
        e2 = DummyEvent()
        e3 = DummyEvent()

        assert p.try_enqueue(e1) is True
        assert p.try_enqueue(e2) is True
        assert p.try_enqueue(e3) is False  # full
        assert p.queue.qsize() == 2

    @pytest.mark.anyio
    async def test_try_enqueue_after_dequeue(self):
        """After dequeuing, try_enqueue succeeds again."""
        p = DummyProcessor(
            queue_capacity=10,
            capacity_refresh_time=1.0,
            concurrency_limit=5,
            max_queue_size=1,
        )
        e1 = DummyEvent()
        e2 = DummyEvent()

        assert p.try_enqueue(e1) is True
        assert p.try_enqueue(e2) is False  # full

        await p.dequeue()  # remove e1
        assert p.queue_full is False
        assert p.try_enqueue(e2) is True

    @pytest.mark.anyio
    async def test_enqueue_blocks_when_full(self):
        """enqueue() blocks when queue is full, unblocks after dequeue."""
        p = DummyProcessor(
            queue_capacity=10,
            capacity_refresh_time=1.0,
            concurrency_limit=5,
            max_queue_size=1,
        )
        e1 = DummyEvent()
        e2 = DummyEvent()

        await p.enqueue(e1)  # fills the queue

        enqueued = asyncio.Event()

        async def delayed_enqueue():
            await p.enqueue(e2)
            enqueued.set()

        task = asyncio.create_task(delayed_enqueue())

        # Give the task a chance to start (it should block)
        await asyncio.sleep(0.05)
        assert not enqueued.is_set()

        # Dequeue to free space
        await p.dequeue()
        await asyncio.sleep(0.05)
        assert enqueued.is_set()

        # Task already completed — just await it
        await task

    def test_max_queue_size_zero_unlimited(self):
        """max_queue_size=0 creates an unbounded asyncio.Queue."""
        p = DummyProcessor(
            queue_capacity=5,
            capacity_refresh_time=1.0,
            concurrency_limit=5,
            max_queue_size=0,
        )
        # Unbounded queue should accept many items via try_enqueue
        for _ in range(50):
            assert p.try_enqueue(DummyEvent()) is True
        assert p.queue.qsize() == 50
        assert p.queue_full is False
