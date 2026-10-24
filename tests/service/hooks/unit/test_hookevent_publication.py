# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Hook completion publishes a coherent result; set observers are instrumented explicitly."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest
import pytest_asyncio

from lionagi.protocols.types import Event, EventStatus
from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.hooks.hook_event import HookEvent
from lionagi.service.hooks.hook_registry import HookRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("not-a-status", "Invalid status: not-a-status"),
        (None, "Invalid status type: <class 'NoneType'>. Expected EventStatus or str."),
    ],
)
@pytest.mark.parametrize("response_kind", ["value", "tuple_error", "tuple_cancel"])
async def test_invalid_registry_status_retains_captured_failure(
    monkeypatch, status, message, response_kind
):
    registry = HookRegistry()

    response = "discarded response"
    if response_kind == "tuple_error":
        response = (None, RuntimeError("tuple error must not override invalid status"))
    elif response_kind == "tuple_cancel":
        response = (None, asyncio.CancelledError("invalid status wins before cancellation"))

    async def call(*_args, **_kwargs):
        return (response, False, status), {"lion_class": "source"}

    monkeypatch.setattr(registry, "call", call)
    event = HookEvent(
        registry=registry,
        hook_type=HookEventTypes.PreInvocation,
        event_like=Event(),
        exit=True,
    )
    await event.invoke()

    assert event.status is EventStatus.FAILED
    assert event.execution.response is None
    assert event.execution.error == message
    assert isinstance(event._exit_cause, ValueError)
    assert str(event._exit_cause) == message
    assert event._should_exit is True


@pytest.mark.asyncio
async def test_valid_registry_status_string_is_normalized(monkeypatch):
    registry = HookRegistry()
    response = object()

    async def call(*_args, **_kwargs):
        return (response, False, "completed"), {"lion_class": "source"}

    monkeypatch.setattr(registry, "call", call)
    event = HookEvent(
        registry=registry,
        hook_type=HookEventTypes.PreInvocation,
        event_like=Event(),
    )
    await event.invoke()

    assert event.status is EventStatus.COMPLETED
    assert event.execution.response is response
    assert event.execution.error is None
    assert event._exit_cause is None
    assert event._should_exit is False


def _snapshot(event):
    return {
        "status": event.status,
        "response": event.execution.response,
        "error": event.execution.error,
        "duration": event.execution.duration,
        "cause": event._exit_cause,
        "should_exit": event._should_exit,
    }


@pytest_asyncio.fixture
async def watch_publication(monkeypatch):
    import lionagi.service.hooks.hook_event as module

    clock = iter((10.0, 12.0))
    monkeypatch.setattr(module, "current_time", lambda: next(clock))
    waiters = []

    async def watch(event):
        done = event.completion_event
        publications = []
        original_set = done.set

        def record_publication():
            original_set()
            publications.append(_snapshot(event))

        # This synchronous recorder observes set(), not an asyncio scheduling race.
        # Assertions stay outside it so they cannot become captured hook failures.
        monkeypatch.setattr(done, "set", record_publication)
        ready = asyncio.Event()

        async def wait_for_result():
            ready.set()
            await done.wait()
            return _snapshot(event)

        waiter = asyncio.create_task(wait_for_result())
        waiters.append(waiter)
        await ready.wait()
        assert not done.is_set()
        return publications, waiter

    yield watch
    for waiter in waiters:
        if not waiter.done():
            waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter


async def _assert_publication(event, publications, waiter, *, status, response, error, cause, exit):
    expected = {
        "status": status,
        "response": response,
        "error": error,
        "duration": 2.0,
        "cause": cause,
        "should_exit": exit,
    }
    assert _snapshot(event) == expected
    assert publications == [expected]
    assert await asyncio.wait_for(waiter, timeout=1) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hook_type", "outcome", "exit_on_error", "expected_status", "expected_exit"),
    [
        (HookEventTypes.PreInvocation, "success", True, EventStatus.COMPLETED, False),
        (HookEventTypes.PreInvocation, "raise", True, EventStatus.CANCELLED, True),
        (HookEventTypes.PostInvocation, "raise", False, EventStatus.ABORTED, False),
        (HookEventTypes.PreInvocation, "return_error", True, EventStatus.COMPLETED, False),
        (HookEventTypes.PreInvocation, "tuple", True, EventStatus.FAILED, True),
        (HookEventTypes.PreInvocation, "tuple_cancel", True, EventStatus.COMPLETED, False),
    ],
)
async def test_publication_contains_final_registry_result(
    watch_publication, hook_type, outcome, exit_on_error, expected_status, expected_exit
):
    error = (
        asyncio.CancelledError("returned cancellation")
        if outcome == "tuple_cancel"
        else RuntimeError("hook failed")
    )
    response = object()

    async def handler(_event, **_kwargs):
        if outcome == "raise":
            raise error
        if outcome in ("tuple", "tuple_cancel"):
            return None, error
        if outcome == "return_error":
            return error
        return response

    event = HookEvent(
        registry=HookRegistry(hooks={hook_type: handler}),
        hook_type=hook_type,
        event_like=Event(),
        exit=exit_on_error,
    )
    publications, waiter = await watch_publication(event)

    if outcome == "tuple_cancel":
        with pytest.raises(asyncio.CancelledError):
            await event.invoke()
    else:
        await event.invoke()

    await _assert_publication(
        event,
        publications,
        waiter,
        status=expected_status,
        response=response if outcome == "success" else None,
        error=None if outcome == "success" else str(error),
        cause=None if outcome == "success" else error,
        exit=expected_exit,
    )


@pytest.mark.asyncio
async def test_wiring_failure_publishes_final_error_and_exit(watch_publication):
    async def handler(_event, **_kwargs):
        raise AssertionError("duplicate argument must prevent handler invocation")

    event = HookEvent(
        registry=HookRegistry(hooks={HookEventTypes.PreInvocation: handler}),
        hook_type=HookEventTypes.PreInvocation,
        event_like=Event(),
        exit=True,
        params={"should_exit": False},
    )
    publications, waiter = await watch_publication(event)
    await event.invoke()

    assert isinstance(event._exit_cause, TypeError)
    await _assert_publication(
        event,
        publications,
        waiter,
        status=EventStatus.FAILED,
        response=None,
        error=str(event._exit_cause),
        cause=event._exit_cause,
        exit=True,
    )


@pytest.mark.asyncio
async def test_raised_cancellation_publishes_final_cause_and_propagates(watch_publication):
    entered = asyncio.Event()
    cancellations = []

    async def handler(_event, **_kwargs):
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
    publications, waiter = await watch_publication(event)
    task = asyncio.create_task(event.invoke())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    await _assert_publication(
        event,
        publications,
        waiter,
        status=EventStatus.CANCELLED,
        response=None,
        error=str(cancellations[0]),
        cause=cancellations[0],
        exit=True,
    )


@pytest.mark.asyncio
async def test_pre_result_cancellation_keeps_nonterminal_state(monkeypatch, watch_publication):
    registry = HookRegistry()
    error = asyncio.CancelledError("before registry result")

    async def call(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(registry, "call", call)
    event = HookEvent(
        registry=registry,
        hook_type=HookEventTypes.PreInvocation,
        event_like=Event(),
        exit=True,
    )
    publications, _waiter = await watch_publication(event)
    with pytest.raises(asyncio.CancelledError):
        await event.invoke()

    assert _snapshot(event) == {
        "status": EventStatus.PROCESSING,
        "response": None,
        "error": None,
        "duration": 2.0,
        "cause": None,
        "should_exit": False,
    }
    assert publications == []
    assert not event.completion_event.is_set()


@pytest.mark.asyncio
async def test_timeout_publishes_final_failure(watch_publication):
    async def handler(_event, **_kwargs):
        await asyncio.Event().wait()

    event = HookEvent(
        registry=HookRegistry(hooks={HookEventTypes.PreInvocation: handler}),
        hook_type=HookEventTypes.PreInvocation,
        event_like=Event(),
        timeout=0.01,
    )
    publications, waiter = await watch_publication(event)
    await event.invoke()

    assert isinstance(event._exit_cause, TimeoutError)
    await _assert_publication(
        event,
        publications,
        waiter,
        status=EventStatus.FAILED,
        response=None,
        error=str(event._exit_cause),
        cause=event._exit_cause,
        exit=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["not-a-status", None])
async def test_invalid_status_publishes_captured_validation_failure(
    monkeypatch, watch_publication, status
):
    registry = HookRegistry()

    async def call(*_args, **_kwargs):
        return ("discarded response", False, status), {"lion_class": "source"}

    monkeypatch.setattr(registry, "call", call)
    event = HookEvent(
        registry=registry,
        hook_type=HookEventTypes.PreInvocation,
        event_like=Event(),
        exit=True,
    )
    publications, waiter = await watch_publication(event)
    await event.invoke()

    assert isinstance(event._exit_cause, ValueError)
    await _assert_publication(
        event,
        publications,
        waiter,
        status=EventStatus.FAILED,
        response=None,
        error=str(event._exit_cause),
        cause=event._exit_cause,
        exit=True,
    )


@pytest.mark.asyncio
async def test_publication_setter_failure_is_captured_before_failure_publication(
    monkeypatch, watch_publication
):
    error = RuntimeError("completion publication failed")
    response = object()

    async def handler(_event, **_kwargs):
        return response

    event = HookEvent(
        registry=HookRegistry(hooks={HookEventTypes.PreInvocation: handler}),
        hook_type=HookEventTypes.PreInvocation,
        event_like=Event(),
        exit=True,
    )
    publications, waiter = await watch_publication(event)
    recording_set = event.completion_event.set
    attempts = []

    def fail_first_publication():
        # Explicit one-shot production-boundary fault injection, separate from
        # the nonthrowing snapshot recorder called on the successful attempt.
        attempts.append(None)
        if len(attempts) == 1:
            raise error
        recording_set()

    monkeypatch.setattr(event.completion_event, "set", fail_first_publication)
    await event.invoke()

    assert len(attempts) == 2
    assert event.status is EventStatus.FAILED
    assert event.execution.response is None
    assert event.execution.error == str(error)
    assert event._exit_cause is error
    assert event._should_exit is True
    assert event.completion_event.is_set()
    assert await asyncio.wait_for(waiter, timeout=1) == _snapshot(event)

    await _assert_publication(
        event,
        publications,
        waiter,
        status=EventStatus.FAILED,
        response=None,
        error=str(error),
        cause=error,
        exit=True,
    )
