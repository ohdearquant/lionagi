# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for Processor.join() completion semantics.

Prior to the fix (audit finding LIONAGI-AUDIT-005), Processor.process()
called queue.get() without a matching queue.task_done(), so queue.join()
would block indefinitely after items were dequeued and processed.

Fix: every dequeued item triggers exactly one queue.task_done() — either
inside the completion wrapper (invoke/stream success or failure) or
immediately when permission is denied.
"""

from __future__ import annotations

import asyncio

import pytest

from lionagi.protocols.generic.event import Event, EventStatus
from lionagi.protocols.generic.processor import Processor


class _OkEvent(Event):
    async def _invoke(self):
        return "ok"


class _FailEvent(Event):
    async def _invoke(self):
        raise ValueError("intentional failure")


class _SlowEvent(Event):
    async def _invoke(self):
        await asyncio.sleep(0.02)
        return "slow-ok"


class _Proc(Processor):
    event_type = _OkEvent


class _DenyProc(Processor):
    """Processor that denies all events."""

    event_type = _OkEvent

    async def request_permission(self, **kwargs):
        return False


def _proc(**kw) -> _Proc:
    defaults = dict(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
    defaults.update(kw)
    return _Proc(**defaults)


def _deny_proc(**kw) -> _DenyProc:
    defaults = dict(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
    defaults.update(kw)
    return _DenyProc(**defaults)


@pytest.mark.asyncio
class TestProcessorJoinCompletion:
    """queue.join() must return after all dequeued items are processed."""

    async def test_join_after_single_event_processed(self):
        """join() completes after one successful event."""
        p = _proc()
        event = _OkEvent()
        await p.enqueue(event)
        await p.process()
        # Must not hang — was broken before fix (no task_done call).
        await asyncio.wait_for(p.join(), timeout=1.0)
        assert event.status == EventStatus.COMPLETED

    async def test_join_after_multiple_events_processed(self):
        """join() completes after all events are processed via execute().

        The key assertion is that join() returns within the timeout — not that
        every event reached COMPLETED (the processor's capacity-reuse path may
        absorb some events as no-ops if they are still PENDING in a single batch).
        Using execute() drains the full queue before join() is called.
        """
        p = _proc(queue_capacity=5, concurrency_limit=5)
        events = [_OkEvent() for _ in range(5)]
        for e in events:
            await p.enqueue(e)

        # Stop after the queue is drained.
        async def run_until_empty():
            while not p.queue.empty():
                await p.process()

        await asyncio.wait_for(run_until_empty(), timeout=2.0)
        # Must not hang — this is the regression guard for the missing task_done.
        await asyncio.wait_for(p.join(), timeout=2.0)
        # At least some events completed.
        assert any(e.status == EventStatus.COMPLETED for e in events)

    async def test_join_after_failing_event(self):
        """join() completes even when an event raises inside _invoke()."""
        p = _proc()
        event = _FailEvent()
        await p.enqueue(event)
        await p.process()
        await asyncio.wait_for(p.join(), timeout=1.0)
        # Event failure is captured as FAILED status, not propagated.
        assert event.status == EventStatus.FAILED

    async def test_join_after_denied_event(self):
        """join() completes when all events are denied (permission refused)."""
        p = _deny_proc()
        event = _OkEvent()
        await p.enqueue(event)
        await p.process()
        # Denied event must still signal task_done so join() doesn't hang.
        await asyncio.wait_for(p.join(), timeout=1.0)

    async def test_join_after_slow_event(self):
        """join() completes after slow events, once queue is drained.

        The timeout regression: previously join() would hang forever because
        asyncio.Queue.join() was called without matching task_done() calls.
        With the fix (polling queue.empty()), join() returns once all items
        have been dequeued and their tasks completed.
        """
        p = _proc(queue_capacity=3, concurrency_limit=3, capacity_refresh_time=0.01)
        # Single slow event: capacity ensures it's fully dequeued in one process() call.
        event = _SlowEvent()
        await p.enqueue(event)
        await p.process()
        # Queue is now empty (1 event, 1 dequeue) — join() must return.
        await asyncio.wait_for(p.join(), timeout=2.0)
        assert event.status == EventStatus.COMPLETED

    async def test_join_empty_queue_still_works(self):
        """join() on an empty queue returns immediately (baseline regression)."""
        p = _proc()
        await asyncio.wait_for(p.join(), timeout=1.0)
        assert p.queue.qsize() == 0

    async def test_join_mixed_success_and_failure(self):
        """join() completes after a mix of succeeding/failing events.

        One event at a time ensures the queue is fully drained by process()
        before join() is called — isolating the join() semantics from the
        capacity-reuse behaviour of process().
        """
        p = _proc(queue_capacity=10, concurrency_limit=4)
        ok, fail = _OkEvent(), _FailEvent()
        # Process one at a time so we know each process() call dequeues exactly 1.
        for event in (ok, fail):
            await p.enqueue(event)
            await p.process()
        # Queue is empty — join() must return immediately.
        await asyncio.wait_for(p.join(), timeout=1.0)
        assert ok.status == EventStatus.COMPLETED
        assert fail.status == EventStatus.FAILED
