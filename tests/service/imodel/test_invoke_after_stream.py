# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""One iModel can stream and then invoke.

A streamed call is not sent by the executor's queue, so it must leave the
queue and the pile consistent however the stream ends; otherwise the next
call the queue forwards is one the pile no longer holds.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import anyio
import pytest

from lionagi.operations.run.run import _stream_with_liveness
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.imodel import iModel

MESSAGES = [{"role": "user", "content": "hello"}]
RESPONSE = {
    "id": "resp-1",
    "object": "chat.completion",
    "model": "gpt-4.1-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
# Bounds every cross-task wait below, so a regression fails the test instead
# of leaving it hung until the suite's own timeout.
WAIT = 5.0


def _model(**kw) -> iModel:
    return iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key", **kw)


def _chunks(n: int):
    async def gen(*_args, **_kwargs):
        for i in range(n):
            yield {"choices": [{"delta": {"content": f"chunk {i}"}}]}

    return gen


async def _drain(agen) -> list:
    return [chunk async for chunk in agen if not isinstance(chunk, APICalling)]


async def _stream_once(model: iModel) -> list:
    with patch.object(model.endpoint, "stream", side_effect=_chunks(2)) as fake_stream:
        chunks = await _drain(model.stream(messages=MESSAGES))

    assert fake_stream.call_count == 1, (
        f"the fake transport served {fake_stream.call_count} stream request(s), expected 1"
    )
    return chunks


async def _invoke_once(model: iModel) -> APICalling:
    async def call(*_args, **_kwargs):
        return dict(RESPONSE)

    with patch.object(model.endpoint, "call", side_effect=call) as fake_call:
        result = await model.invoke(messages=MESSAGES)

    assert fake_call.call_count == 1, (
        f"the fake transport served {fake_call.call_count} invoke request(s), expected 1"
    )
    return result


@pytest.mark.asyncio
async def test_invoke_after_one_stream_returns_the_invoke_response():
    model = _model()
    assert len(await _stream_once(model)) == 2

    result = await _invoke_once(model)

    assert result.response == RESPONSE


@pytest.mark.asyncio
async def test_invoke_after_two_streams_returns_the_invoke_response():
    model = _model()
    with patch.object(model.endpoint, "stream", side_effect=_chunks(2)) as fake_stream:
        assert len(await _drain(model.stream(messages=MESSAGES))) == 2
        assert len(await _drain(model.stream(messages=MESSAGES))) == 2

    assert fake_stream.call_count == 2
    result = await _invoke_once(model)

    assert result.response == RESPONSE


@pytest.mark.asyncio
async def test_invoke_stream_invoke_all_use_the_same_model():
    model = _model()
    first = await _invoke_once(model)
    assert first.response == RESPONSE

    assert len(await _stream_once(model)) == 2

    second = await _invoke_once(model)
    assert second.response == RESPONSE


@pytest.mark.parametrize("concurrency_limit", [None, 2])
@pytest.mark.asyncio
async def test_a_streamed_call_leaves_the_executor_queue_and_pile_consistent(concurrency_limit):
    model = _model(concurrency_limit=concurrency_limit)
    assert len(await _stream_once(model)) == 2

    assert len(model.executor.pending) == 0
    assert len(model.executor.pile) == 0


@pytest.mark.asyncio
async def test_a_call_appended_before_it_is_streamed_leaves_the_queue_usable():
    # A caller may append its own call and then stream it by passing it in.
    model = _model()
    api_call = await model.create_event(messages=MESSAGES)
    await model.executor.append(api_call)

    with patch.object(model.endpoint, "stream", side_effect=_chunks(2)) as fake_stream:
        chunks = await _drain(model.stream(api_call=api_call))

    assert fake_stream.call_count == 1
    assert len(chunks) == 2
    assert len(model.executor.pending) == 0
    assert len(model.executor.pile) == 0

    result = await _invoke_once(model)
    assert result.response == RESPONSE


@pytest.mark.asyncio
async def test_an_invoke_during_an_open_stream_sends_one_request_for_its_own_call():
    model = _model()
    mid_stream = asyncio.Event()
    release = asyncio.Event()
    stream_requests: list = []

    async def gated_stream(*_args, **_kwargs):
        stream_requests.append(1)
        if len(stream_requests) == 1:
            # The stream the test itself is reading: hold it open so the
            # invoke runs while the call is still being consumed.
            yield {"choices": [{"delta": {"content": "first"}}]}
            mid_stream.set()
            await release.wait()
        yield {"choices": [{"delta": {"content": "second"}}]}

    async def call(*_args, **_kwargs):
        return dict(RESPONSE)

    with (
        patch.object(model.endpoint, "stream", side_effect=gated_stream) as fake_stream,
        patch.object(model.endpoint, "call", side_effect=call) as fake_call,
    ):
        stream_task = asyncio.create_task(_drain(model.stream(messages=MESSAGES)))
        await asyncio.wait_for(mid_stream.wait(), timeout=WAIT)

        result = await asyncio.wait_for(model.invoke(messages=MESSAGES), timeout=WAIT)

        # The invoke ran to completion while the stream was still open. Only
        # the stream's own consumer may have requested a stream: the invoke's
        # call is not a streamed call.
        assert fake_stream.call_count == 1, (
            f"the fake transport served {fake_stream.call_count} stream request(s), expected 1"
        )
        assert fake_call.call_count == 1, (
            f"the fake transport served {fake_call.call_count} invoke request(s), expected 1"
        )
        assert result.response == RESPONSE

        release.set()
        chunks = await asyncio.wait_for(stream_task, timeout=WAIT)

    assert len(chunks) == 2


@pytest.mark.asyncio
async def test_a_stream_that_ends_while_an_invoke_waits_leaves_the_invoke_its_own_result():
    model = _model()
    queued = asyncio.Event()
    release = asyncio.Event()
    real_forward = model.executor.forward

    async def gated_forward():
        # Hold the invoke's queue step open while a stream runs and finishes
        # on the same model. ``queued`` is set once the invoke holds its own
        # pending entry; the stream then runs to completion before the queue
        # forwards anything.
        queued.set()
        await release.wait()
        await real_forward()

    with patch.object(model.executor, "forward", side_effect=gated_forward):
        invoke_task = asyncio.create_task(_invoke_once(model))
        await asyncio.wait_for(queued.wait(), timeout=WAIT)
        chunks = await _stream_once(model)
        release.set()
        result = await asyncio.wait_for(invoke_task, timeout=WAIT)

    assert result.response == RESPONSE
    assert len(chunks) == 2


@pytest.mark.asyncio
async def test_a_cancelled_stream_waiting_for_a_slot_leaves_no_pending_entry():
    model = _model(concurrency_limit=1)
    await model.executor.start()
    slot = model.executor.processor._concurrency_sem
    assert slot is not None
    await slot.acquire()
    try:
        with patch.object(model.endpoint, "stream", side_effect=_chunks(2)) as fake_stream:
            consumer = asyncio.create_task(_drain(model.stream(messages=MESSAGES)))

            async def reached_the_slot_wait() -> None:
                # The call enters the pile when stream() takes it; with this test
                # holding the only slot, the next await in stream() is the slot
                # acquire.
                while len(model.executor.pile) == 0:
                    await asyncio.sleep(0)

            await asyncio.wait_for(reached_the_slot_wait(), timeout=WAIT)
            assert fake_stream.call_count == 0, "the stream ran before it held the slot"
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(consumer, timeout=WAIT)
    finally:
        slot.release()

    assert len(model.executor.pending) == 0
    assert len(model.executor.pile) == 0


@pytest.mark.asyncio
async def test_a_stream_whose_processor_fails_to_start_leaves_no_pending_entry():
    model = _model()

    async def refused_start(*_args, **_kwargs):
        raise RuntimeError("processor could not start")

    with (
        patch.object(model.executor, "start", side_effect=refused_start),
        patch.object(model.endpoint, "stream", side_effect=_chunks(2)) as fake_stream,
    ):
        with pytest.raises(RuntimeError, match="processor could not start"):
            await _drain(model.stream(messages=MESSAGES))

    assert fake_stream.call_count == 0
    assert len(model.executor.pending) == 0
    assert len(model.executor.pile) == 0


@pytest.mark.asyncio
async def test_a_run_stream_past_its_deadline_before_it_starts_leaves_nothing_queued():
    # run() arms its deadline before the pre-call hooks run, so a hook that
    # outlasts the timeout ends the run before stream() starts. The call it
    # created must not be left for the next invoke() to send.
    model = _model()
    with patch.object(model.endpoint, "stream", side_effect=_chunks(1)) as fake_stream:
        with pytest.raises(TimeoutError):
            await _drain(
                _stream_with_liveness(
                    model,
                    {"messages": MESSAGES, "stream": True},
                    anyio.current_time() - 1.0,
                    None,
                    [],
                )
            )
        assert len(model.executor.pending) == 0
        assert len(model.executor.pile) == 0
        await _invoke_once(model)

    assert fake_stream.call_count == 0
