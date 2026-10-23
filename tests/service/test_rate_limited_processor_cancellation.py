# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Cancellation of replenishment leaves callers able to finish shutdown."""

import asyncio

import pytest

from lionagi.service import rate_limited_processor as module


def _processor():
    return module.RateLimitedAPIProcessor(queue_capacity=1, capacity_refresh_time=1)


@pytest.mark.asyncio
async def test_replenisher_accepts_cancellation_while_waiting(monkeypatch):
    started = asyncio.Event()
    processor = _processor()

    async def wait_for_refresh(_delay):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "sleep", wait_for_refresh)
    task = asyncio.create_task(processor.start_replenishing())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        task.cancel()
        assert await task is None

    assert not task.cancelled()


@pytest.mark.asyncio
async def test_stop_settles_cancelled_task_and_clears_its_handle():
    started = asyncio.Event()
    processor = _processor()

    async def pending_replenishment():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(pending_replenishment())
    processor._rate_limit_replenisher_task = task
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await processor.stop()
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert task.cancelled()
    assert processor.is_stopped()
    assert processor._rate_limit_replenisher_task is None
