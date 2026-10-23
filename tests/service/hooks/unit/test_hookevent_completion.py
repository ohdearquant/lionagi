# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Hook completion signals for waiters attached before invocation."""

from __future__ import annotations

import asyncio

import pytest

from lionagi.protocols.types import Event, EventStatus
from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.hooks.hook_event import HookEvent
from lionagi.service.hooks.hook_registry import HookRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hook_type", "outcome", "exit_on_error", "expected_status", "expected_exit"),
    (
        (HookEventTypes.PreInvocation, "success", True, EventStatus.COMPLETED, False),
        (HookEventTypes.PreInvocation, "raise", True, EventStatus.CANCELLED, True),
        (HookEventTypes.PostInvocation, "raise", False, EventStatus.ABORTED, False),
        (HookEventTypes.PreInvocation, "tuple", True, EventStatus.FAILED, True),
    ),
)
async def test_terminal_hook_signals_precreated_completion_event(
    hook_type, outcome, exit_on_error, expected_status, expected_exit
):
    error = RuntimeError("hook failed")
    response = object()
    calls = []

    async def handler(event, **kwargs):
        calls.append(event)
        if outcome == "raise":
            raise error
        if outcome == "tuple":
            return None, error
        return response

    source = Event()
    event = HookEvent(
        registry=HookRegistry(hooks={hook_type: handler}),
        hook_type=hook_type,
        event_like=source,
        exit=exit_on_error,
    )
    done = event.completion_event
    assert not done.is_set()

    await event.invoke()

    assert calls == [source]
    assert event.status is expected_status
    assert event.execution.response is (response if outcome == "success" else None)
    assert event.execution.error == (None if outcome == "success" else str(error))
    assert event._exit_cause is (None if outcome == "success" else error)
    assert event._should_exit is expected_exit
    assert event.execution.duration >= 0
    assert done.is_set()
    await asyncio.wait_for(done.wait(), timeout=1)


@pytest.mark.asyncio
async def test_hook_wiring_failure_signals_precreated_completion_event():
    calls = []

    async def handler(event, **kwargs):
        calls.append(event)

    event = HookEvent(
        registry=HookRegistry(hooks={HookEventTypes.PreInvocation: handler}),
        hook_type=HookEventTypes.PreInvocation,
        event_like=Event(),
        exit=True,
        params={"should_exit": False},
    )
    done = event.completion_event
    assert not done.is_set()

    await event.invoke()

    assert calls == []
    assert event.status is EventStatus.FAILED
    assert isinstance(event._exit_cause, TypeError)
    assert event.execution.error == str(event._exit_cause)
    assert event.execution.response is None
    assert event._should_exit is True
    assert event.execution.duration >= 0
    assert done.is_set()
    await asyncio.wait_for(done.wait(), timeout=1)


@pytest.mark.asyncio
async def test_cancelled_hook_signals_completion_and_propagates_cancellation():
    entered = asyncio.Event()
    cancellations = []

    async def handler(event, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise

    event = HookEvent(
        registry=HookRegistry(hooks={HookEventTypes.PreInvocation: handler}),
        hook_type=HookEventTypes.PreInvocation,
        event_like=Event(),
    )
    done = event.completion_event
    task = asyncio.create_task(event.invoke())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert event.status is EventStatus.CANCELLED
    assert event._exit_cause is cancellations[0]
    assert event.execution.error == str(cancellations[0])
    assert event.execution.response is None
    assert event._should_exit is True
    assert event.execution.duration >= 0
    assert done.is_set()
    await asyncio.wait_for(done.wait(), timeout=1)
