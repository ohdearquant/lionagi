# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The service hook registry as one iModel drives it: the hook points and their
order around a call and a stream, the exception a denial surfaces as, stream
teardown when a hook denies or raises and on cancellation, and hook sharing when
one iModel serves several branches."""

from __future__ import annotations

import asyncio
import gc
from unittest.mock import patch

import anyio
import pytest

from lionagi.protocols.generic.event import EventStatus
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.hooks import HookRegistry, StreamTerminalState
from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch

MESSAGES = [{"role": "user", "content": "hello"}]
RESPONSE = {
    "id": "r1",
    "object": "chat.completion",
    "model": "gpt-4.1-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _imodel(hooks=None, stream_handlers=None, **kw) -> iModel:
    registry = HookRegistry(hooks=hooks, stream_handlers=stream_handlers)
    return iModel(
        provider="openai",
        model="gpt-4.1-mini",
        api_key="test-key",
        hook_registry=registry,
        **kw,
    )


def _chunks(n: int):
    async def gen(*args, **kwargs):
        for i in range(n):
            yield {"i": i}

    return gen


async def _drain(imodel: iModel) -> list:
    # iModel.stream() yields whatever process_chunk returns, and a stream handler may
    # return an event object; keep only the payload chunks the assertions are about.
    return [c async for c in imodel.stream(messages=MESSAGES) if not isinstance(c, APICalling)]


@pytest.mark.asyncio
async def test_invoke_runs_the_three_hook_points_in_order_around_the_endpoint_call():
    order = []
    seen = {}

    async def pre_create(event_type, **kw):
        order.append("pre_event_create")
        seen["create_arg"] = event_type

    async def pre_invoke(event, **kw):
        order.append("pre_invocation")
        seen["pre"] = (event, event.status)

    async def post_invoke(event, **kw):
        order.append("post_invocation")
        seen["post"] = (event, event.status, event.response)

    async def call(*args, **kwargs):
        order.append("endpoint_call")
        return RESPONSE

    imodel = _imodel(
        {
            HookEventTypes.PreEventCreate: pre_create,
            HookEventTypes.PreInvocation: pre_invoke,
            HookEventTypes.PostInvocation: post_invoke,
        }
    )
    with patch.object(imodel.endpoint, "call", side_effect=call):
        result = await imodel.invoke(messages=MESSAGES)

    assert order == ["pre_event_create", "pre_invocation", "endpoint_call", "post_invocation"]
    assert seen["create_arg"] is APICalling
    assert seen["pre"][0] is result and seen["post"][0] is result
    assert seen["pre"][1] is EventStatus.PROCESSING
    # the post hook runs after _core_invoke() has returned, but before Event.invoke()
    # marks the event terminal: the status is still PROCESSING and there is no
    # response yet, and only afterwards does it become COMPLETED
    assert seen["post"][1] is EventStatus.PROCESSING and seen["post"][2] is None
    assert result.status is EventStatus.COMPLETED and result.response == RESPONSE


@pytest.mark.asyncio
async def test_event_carries_hook_events_only_for_registered_points():
    async def post_invoke(event, **kw):
        return None

    imodel = _imodel({HookEventTypes.PostInvocation: post_invoke})
    api_call = await imodel.create_event(messages=MESSAGES)

    assert api_call._pre_invoke_hook_event is None
    assert api_call._post_invoke_hook_event is not None


@pytest.mark.asyncio
async def test_stream_runs_hooks_around_the_chunks_and_post_hook_follows_the_last_chunk():
    order = []

    async def pre_create(event_type, **kw):
        order.append("pre_event_create")

    async def pre_invoke(event, **kw):
        order.append("pre_invocation")

    async def post_invoke(event, **kw):
        order.append("post_invocation")

    async def on_chunk(event, chunk_type, chunk, **kw):
        order.append(f"handler:{chunk['i']}")

    imodel = _imodel(
        {
            HookEventTypes.PreEventCreate: pre_create,
            HookEventTypes.PreInvocation: pre_invoke,
            HookEventTypes.PostInvocation: post_invoke,
        },
        {dict: on_chunk},
    )
    with patch.object(imodel.endpoint, "stream", side_effect=_chunks(2)):
        async for chunk in imodel.stream(messages=MESSAGES):
            order.append(f"consumed:{chunk['i']}")

    assert order == [
        "pre_event_create",
        "pre_invocation",
        "handler:0",
        "consumed:0",
        "handler:1",
        "consumed:1",
        "post_invocation",
    ]


@pytest.mark.asyncio
async def test_stream_handler_result_replaces_the_chunk_and_none_keeps_it():
    async def replace_even(event, chunk_type, chunk, **kw):
        return {"replaced": chunk["i"]} if chunk["i"] % 2 == 0 else None

    imodel = _imodel(stream_handlers={dict: replace_even})
    with patch.object(imodel.endpoint, "stream", side_effect=_chunks(3)):
        got = await _drain(imodel)

    assert got == [{"replaced": 0}, {"i": 1}, {"replaced": 2}]


@pytest.mark.asyncio
async def test_stream_handler_is_found_by_type_name_when_the_type_is_not_registered():
    async def tag(event, chunk_type, chunk, **kw):
        return {"via": chunk_type}

    imodel = _imodel(stream_handlers={"dict": tag})
    with patch.object(imodel.endpoint, "stream", side_effect=_chunks(1)):
        got = await _drain(imodel)

    assert got == [{"via": "dict"}]


@pytest.mark.asyncio
async def test_pre_event_create_denial_with_exit_raises_the_hook_exception_from_create_event():
    denial = PermissionError("create denied")

    async def deny(event_type, **kw):
        raise denial

    imodel = _imodel({HookEventTypes.PreEventCreate: deny}, exit_hook=True)

    with pytest.raises(PermissionError) as caught:
        await imodel.create_event(messages=MESSAGES)

    assert caught.value is denial


@pytest.mark.asyncio
async def test_pre_event_create_denial_without_exit_does_not_stop_the_call():
    calls = []

    async def deny(event_type, **kw):
        calls.append(event_type)
        raise PermissionError("create denied")

    imodel = _imodel({HookEventTypes.PreEventCreate: deny})
    with patch.object(imodel.endpoint, "call", return_value=RESPONSE):
        result = await imodel.invoke(messages=MESSAGES)

    assert calls == [APICalling]
    assert result.status is EventStatus.COMPLETED


@pytest.mark.asyncio
async def test_invoke_wraps_a_pre_event_create_denial_in_value_error_chained_to_the_original():
    denial = PermissionError("create denied")

    async def deny(event_type, **kw):
        raise denial

    imodel = _imodel({HookEventTypes.PreEventCreate: deny}, exit_hook=True)

    with patch.object(imodel.endpoint, "call", return_value=RESPONSE) as call:
        with pytest.raises(ValueError, match="Failed to invoke API call: create denied") as caught:
            await imodel.invoke(messages=MESSAGES)

    call.assert_not_called()
    assert caught.value.__cause__ is denial


@pytest.mark.asyncio
async def test_stream_raises_a_pre_event_create_denial_unwrapped():
    denial = PermissionError("create denied")

    async def deny(event_type, **kw):
        raise denial

    imodel = _imodel({HookEventTypes.PreEventCreate: deny}, exit_hook=True)

    with patch.object(imodel.endpoint, "stream", side_effect=_chunks(2)) as stream:
        with pytest.raises(PermissionError) as caught:
            await _drain(imodel)

    stream.assert_not_called()
    assert caught.value is denial


@pytest.mark.parametrize("exit_hook", [False, True])
@pytest.mark.asyncio
async def test_invoke_pre_invocation_denial_is_recorded_on_the_event_not_raised(exit_hook):
    async def deny(event, **kw):
        raise PermissionError("pre denied")

    imodel = _imodel({HookEventTypes.PreInvocation: deny}, exit_hook=exit_hook)
    with patch.object(imodel.endpoint, "call", return_value=RESPONSE) as call:
        result = await imodel.invoke(messages=MESSAGES)

    call.assert_not_called()
    assert result.status is EventStatus.FAILED
    # the original exception type is replaced by a RuntimeError carrying its text
    assert type(result.execution.error) is RuntimeError
    assert str(result.execution.error) == "Pre-invoke hook cancelled: pre denied"


@pytest.mark.asyncio
async def test_stream_pre_invocation_denial_raises_value_error_chained_to_runtime_error():
    async def deny(event, **kw):
        raise PermissionError("pre denied")

    imodel = _imodel({HookEventTypes.PreInvocation: deny})
    with patch.object(imodel.endpoint, "stream", side_effect=_chunks(2)) as stream:
        with pytest.raises(ValueError, match="Failed to stream API call: Pre-invoke") as caught:
            await _drain(imodel)

    stream.assert_not_called()
    assert type(caught.value.__cause__) is RuntimeError
    assert "pre denied" in str(caught.value.__cause__)


@pytest.mark.asyncio
async def test_invoke_post_invocation_failure_is_swallowed_without_exit():
    calls = []

    async def boom(event, **kw):
        calls.append(event.id)
        raise PermissionError("post failed")

    imodel = _imodel({HookEventTypes.PostInvocation: boom})
    with patch.object(imodel.endpoint, "call", return_value=RESPONSE):
        result = await imodel.invoke(messages=MESSAGES)

    assert calls == [result.id]
    assert result.status is EventStatus.COMPLETED
    assert result.response == RESPONSE


@pytest.mark.asyncio
async def test_invoke_post_invocation_failure_with_exit_fails_the_event_with_the_hook_exception():
    failure = PermissionError("post failed")

    async def boom(event, **kw):
        raise failure

    imodel = _imodel({HookEventTypes.PostInvocation: boom}, exit_hook=True)
    with patch.object(imodel.endpoint, "call", return_value=RESPONSE):
        result = await imodel.invoke(messages=MESSAGES)

    assert result.status is EventStatus.FAILED
    assert result.execution.error is failure


@pytest.mark.asyncio
async def test_stream_post_invocation_failure_never_reaches_the_consumer():
    calls = []

    async def boom(event, **kw):
        calls.append(event.id)
        raise PermissionError("post failed")

    imodel = _imodel({HookEventTypes.PostInvocation: boom}, exit_hook=True)
    with patch.object(imodel.endpoint, "stream", side_effect=_chunks(2)):
        got = await _drain(imodel)

    assert len(calls) == 1
    assert got == [{"i": 0}, {"i": 1}]


@pytest.mark.asyncio
async def test_stream_handler_denial_without_exit_passes_the_chunk_through():
    calls = []

    async def deny(event, chunk_type, chunk, **kw):
        calls.append(chunk["i"])
        raise PermissionError("chunk denied")

    imodel = _imodel(stream_handlers={dict: deny})
    with patch.object(imodel.endpoint, "stream", side_effect=_chunks(2)):
        got = await _drain(imodel)

    assert calls == [0, 1]
    assert got == [{"i": 0}, {"i": 1}]


@pytest.mark.asyncio
async def test_stream_handler_denial_with_exit_raises_value_error_chained_to_the_original():
    denial = PermissionError("chunk denied")
    delivered = []

    async def deny(event, chunk_type, chunk, **kw):
        if chunk["i"] == 1:
            raise denial
        return None

    imodel = _imodel(stream_handlers={dict: deny}, exit_hook=True)
    with patch.object(imodel.endpoint, "stream", side_effect=_chunks(3)):
        with pytest.raises(ValueError, match="Failed to stream API call: chunk denied") as caught:
            async for chunk in imodel.stream(messages=MESSAGES):
                delivered.append(chunk)

    assert caught.value.__cause__ is denial
    assert delivered == [{"i": 0}]


@pytest.mark.asyncio
async def test_stream_handler_denial_runs_post_hook_only_when_the_abandoned_stream_is_finalized():
    """The denial is raised in the consumer's loop, so the hooked stream is not closed
    at that point: the post-invocation hook runs later, as a closed stream."""
    seen = []
    held = {}

    async def deny(event, chunk_type, chunk, **kw):
        raise PermissionError("chunk denied")

    async def post(event, **kw):
        seen.append(event.stream_terminal_state)

    imodel = _imodel(
        {HookEventTypes.PostInvocation: post},
        {dict: deny},
        exit_hook=True,
    )
    real_stream = APICalling.stream

    def hold(self, *args, **kwargs):
        held["stream"] = gen = real_stream(self, *args, **kwargs)
        return gen

    with patch.object(APICalling, "stream", hold):
        with patch.object(imodel.endpoint, "stream", side_effect=_chunks(2)):
            with pytest.raises(ValueError, match="chunk denied"):
                await _drain(imodel)

            # the abandoned inner stream is held alive, so the post hook cannot have
            # run yet even after the loop has had turns to schedule it
            assert seen == []
            with anyio.fail_after(5):
                for _ in range(20):
                    await anyio.sleep(0)
            assert seen == []

    held.clear()
    gc.collect()
    with anyio.fail_after(5):
        while not seen:
            await anyio.sleep(0.01)

    assert seen == [StreamTerminalState.Closed]


@pytest.mark.asyncio
async def test_stream_source_failure_runs_post_hook_as_failed_and_raises_value_error():
    seen = []

    async def post(event, **kw):
        seen.append(event.stream_terminal_state)

    async def failing(*args, **kwargs):
        yield {"i": 0}
        raise ConnectionError("dropped")

    imodel = _imodel({HookEventTypes.PostInvocation: post})
    with patch.object(imodel.endpoint, "stream", side_effect=failing):
        with pytest.raises(ValueError, match="dropped"):
            await _drain(imodel)

    assert seen == [StreamTerminalState.Failed]


@pytest.mark.asyncio
async def test_stream_closed_early_clears_the_executor_pile_and_tears_down_as_closed():
    """Closing the iModel stream early pops the event from the executor pile at the
    close; the post-invocation hook runs as closed once the abandoned inner stream
    is finalized, not at the close itself."""
    seen = []
    held = {}

    async def post(event, **kw):
        seen.append(event.stream_terminal_state)

    imodel = _imodel({HookEventTypes.PostInvocation: post})
    real_stream = APICalling.stream

    def hold(self, *args, **kwargs):
        held["stream"] = gen = real_stream(self, *args, **kwargs)
        return gen

    with patch.object(APICalling, "stream", hold):
        with patch.object(imodel.endpoint, "stream", side_effect=_chunks(5)):
            stream = imodel.stream(messages=MESSAGES)
            first = await stream.__anext__()
            await stream.aclose()

            # the pile is cleared at the close, and the abandoned inner stream is
            # held alive, so the post hook cannot have run yet even after the loop
            # has had turns to schedule it
            assert first == {"i": 0}
            assert len(imodel.executor.pile) == 0
            assert seen == []
            with anyio.fail_after(5):
                for _ in range(20):
                    await anyio.sleep(0)
            assert seen == []

    held.clear()
    gc.collect()
    with anyio.fail_after(5):
        while not seen:
            await anyio.sleep(0.01)

    assert seen == [StreamTerminalState.Closed]


@pytest.mark.asyncio
async def test_stream_consumer_cancellation_runs_post_hook_as_cancelled_and_propagates():
    seen = []
    first_chunk = asyncio.Event()

    async def post(event, **kw):
        seen.append(event.stream_terminal_state)

    async def slow(*args, **kwargs):
        yield {"i": 0}
        with anyio.fail_after(10):
            await anyio.sleep(30)
        yield {"i": 1}

    imodel = _imodel({HookEventTypes.PostInvocation: post})

    async def consume():
        async for _ in imodel.stream(messages=MESSAGES):
            first_chunk.set()

    with patch.object(imodel.endpoint, "stream", side_effect=slow):
        task = asyncio.create_task(consume())
        with anyio.fail_after(5):
            await first_chunk.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert task.cancelled()
    assert seen == [StreamTerminalState.Cancelled]
    assert len(imodel.executor.pile) == 0


@pytest.mark.asyncio
async def test_one_imodel_serves_several_branches_with_one_set_of_hooks():
    shared = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    first = Branch(chat_model=shared)
    second = Branch(chat_model=shared)

    assert first.chat_model is second.chat_model is shared
    assert first.chat_model.hook_registry is shared.hook_registry

    calls = []

    async def pre_invoke(event, **kw):
        calls.append(event.id)

    first.chat_model.hook_registry.pre_invoke(pre_invoke)
    with patch.object(shared.endpoint, "call", return_value=RESPONSE):
        first_result = await first.chat_model.invoke(messages=MESSAGES)
        second_result = await second.chat_model.invoke(messages=MESSAGES)

    assert first_result.status is EventStatus.COMPLETED
    assert second_result.status is EventStatus.COMPLETED
    assert calls == [first_result.id, second_result.id]


@pytest.mark.asyncio
async def test_a_cloned_branch_keeps_the_shared_imodels_registry():
    shared = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    branch = Branch(chat_model=shared)

    clone = branch.clone()

    assert clone.chat_model is shared
    assert clone.chat_model.hook_registry is shared.hook_registry


def test_separate_imodels_get_separate_empty_registries():
    first = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    second = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")

    assert isinstance(first.hook_registry, HookRegistry)
    assert first.hook_registry is not second.hook_registry
    assert first.hook_registry._hooks == {}
    assert first.hook_registry._stream_handlers == {}


def test_imodel_accepts_a_registry_supplied_as_a_dict():
    async def pre_invoke(event, **kw):
        return None

    imodel = iModel(
        provider="openai",
        model="gpt-4.1-mini",
        api_key="test-key",
        hook_registry={"hooks": {"pre_invoke": pre_invoke}},
    )

    assert isinstance(imodel.hook_registry, HookRegistry)
    assert imodel.hook_registry._hooks[HookEventTypes.PreInvocation] is pre_invoke


def test_a_flat_hook_mapping_is_rejected():
    async def pre_invoke(event, **kw):
        return None

    with pytest.raises(TypeError, match="unexpected keyword argument 'pre_invoke'"):
        iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            hook_registry={"pre_invoke": pre_invoke},
        )
