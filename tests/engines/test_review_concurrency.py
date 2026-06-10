# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Concurrency safety tests for ReviewEngine fan-out.

Verifies that when a dimension reviewer raises, any verifier tasks that were
spawned before the failure are cancelled and do not outlive the run.  No
orphaned background task should be able to mutate shared EngineRun state
after _run has exited.
"""

from __future__ import annotations

import asyncio

import pytest

from lionagi.engines.review import ReviewEngine
from lionagi.ln.concurrency import BaseExceptionGroup  # 3.10 compat shim


class _OKAgent:
    """A reviewer that completes immediately without errors."""

    def __init__(self, name: str):
        self.name = name

    async def operate(self, *, instruction: str):
        await asyncio.sleep(0)
        return None


class _BoomAgent:
    """A reviewer that raises after a short delay."""

    def __init__(self, name: str):
        self.name = name

    async def operate(self, *, instruction: str):
        await asyncio.sleep(0.01)
        raise RuntimeError("reviewer exploded")


@pytest.mark.asyncio
async def test_no_orphaned_tasks_after_dimension_failure():
    """Attack: a dimension reviewer raises mid-flight.

    Before the fix, verifier tasks spawned just before the failure kept running
    in the background and mutated shared EngineRun state after _run raised.
    After the fix, cancel_active() is called and _active must be empty before
    the exception propagates to the caller.
    """
    eng = ReviewEngine(dimensions=("correctness", "security"))
    run = eng.new_run()

    # Inject a verifier-like task into _active to simulate the condition where
    # a verifier was spawned by _on_issue just before the reviewer blew up.
    mutated_after_raise: list[str] = []

    async def lingering_verifier():
        # This task must NOT complete normally if cancel_active() fires correctly.
        await asyncio.sleep(10)  # long enough to outlive the run if not cancelled
        mutated_after_raise.append("orphan mutated state")

    # Set up agents: 'correctness' is fine, 'security' explodes.
    async def fake_make(role, *, name=None, **kw):
        if name and "security" in name:
            return _BoomAgent(name or role)
        return _OKAgent(name or role)

    run.make_agent = fake_make

    # Manually plant a long-running task into _active before calling _run so
    # we can verify it gets cancelled.
    task = asyncio.ensure_future(lingering_verifier())
    run._active.add(task)
    task.add_done_callback(run._active.discard)

    with pytest.raises((RuntimeError, BaseExceptionGroup)):
        await eng._run(run, "ARTIFACT")

    # After _run raises, the orphaned task must have been cancelled — _active
    # must be empty (no background work outlives the run).
    assert not run._active, (
        f"cancel_active() must leave _active empty: found {len(run._active)} task(s) still running"
    )

    # The lingering verifier never got to append because it was cancelled.
    # Give the event loop a moment to process cancellation callbacks.
    await asyncio.sleep(0.05)
    assert mutated_after_raise == [], (
        "Orphaned verifier mutated shared state after _run raised. "
        "Structured concurrency cleanup is broken."
    )


@pytest.mark.asyncio
async def test_no_orphaned_tasks_when_all_dimensions_succeed():
    """Baseline: when all reviewers succeed, _active is drained by wait_quiescence."""
    eng = ReviewEngine(dimensions=("correctness", "security"))
    run = eng.new_run()

    calls: list[str] = []

    async def fake_make(role, *, name=None, **kw):
        return _OKAgent(name or role)

    run.make_agent = fake_make

    # Run succeeds (no LLM call, fake agents emit nothing).
    await eng._run(run, "ARTIFACT")

    # After a successful run there are no pending tasks.
    assert not run._active, "_active must be empty after a successful run"


@pytest.mark.asyncio
async def test_cancel_active_clears_active_set():
    """Unit test for EngineRun.cancel_active() directly.

    Verifies that cancel_active() cancels every task and leaves _active empty,
    regardless of what the tasks are doing.
    """
    eng = ReviewEngine()
    run = eng.new_run()

    ran: list[str] = []

    async def long_task(label: str):
        await asyncio.sleep(10)
        ran.append(label)  # must never reach here if cancelled

    for label in ("a", "b", "c"):
        t = asyncio.ensure_future(long_task(label))
        run._active.add(t)
        t.add_done_callback(run._active.discard)

    assert len(run._active) == 3

    await run.cancel_active()

    assert not run._active, "cancel_active must leave _active empty"

    # Give event loop time to process callbacks.
    await asyncio.sleep(0.01)
    assert ran == [], "Cancelled tasks must not complete their body"


@pytest.mark.asyncio
async def test_cancel_active_is_idempotent_on_empty():
    """cancel_active() on an already-empty _active must not raise."""
    eng = ReviewEngine()
    run = eng.new_run()
    assert not run._active
    await run.cancel_active()  # must not raise
    assert not run._active
