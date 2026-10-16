# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""One iModel can stream and then invoke.

A streamed call is not sent by the executor's queue, so it must leave the
queue and the pile consistent once the stream ends; otherwise the next call
the queue forwards is one the pile no longer holds.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

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


def _model(**kw) -> iModel:
    return iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key", **kw)


def _chunks(n: int):
    async def gen(*_args, **_kwargs):
        for i in range(n):
            yield {"choices": [{"delta": {"content": f"chunk {i}"}}]}

    return gen


async def _stream_once(model: iModel) -> list:
    return [
        chunk
        async for chunk in model.stream(messages=MESSAGES)
        if not isinstance(chunk, APICalling)
    ]


async def _invoke_once(model: iModel) -> APICalling:
    async def call(*_args, **_kwargs):
        return dict(RESPONSE)

    with patch.object(model.endpoint, "call", side_effect=call):
        return await model.invoke(messages=MESSAGES)


@pytest.mark.asyncio
async def test_invoke_after_one_stream_returns_the_invoke_response():
    model = _model()
    with patch.object(model.endpoint, "stream", side_effect=_chunks(2)):
        assert len(await _stream_once(model)) == 2

    result = await _invoke_once(model)

    assert result.response == RESPONSE


@pytest.mark.asyncio
async def test_invoke_after_two_streams_returns_the_invoke_response():
    model = _model()
    with patch.object(model.endpoint, "stream", side_effect=_chunks(2)):
        assert len(await _stream_once(model)) == 2
        assert len(await _stream_once(model)) == 2

    result = await _invoke_once(model)

    assert result.response == RESPONSE


@pytest.mark.asyncio
async def test_invoke_stream_invoke_all_use_the_same_model():
    model = _model()
    first = await _invoke_once(model)
    assert first.response == RESPONSE

    with patch.object(model.endpoint, "stream", side_effect=_chunks(3)):
        assert len(await _stream_once(model)) == 3

    second = await _invoke_once(model)
    assert second.response == RESPONSE


@pytest.mark.parametrize("concurrency_limit", [None, 2])
@pytest.mark.asyncio
async def test_a_streamed_call_leaves_the_executor_queue_and_pile_consistent(concurrency_limit):
    model = _model(concurrency_limit=concurrency_limit)
    with patch.object(model.endpoint, "stream", side_effect=_chunks(2)):
        assert len(await _stream_once(model)) == 2

    assert len(model.executor.pending) == 0
    assert len(model.executor.pile) == 0


@pytest.mark.asyncio
async def test_a_call_appended_before_it_is_streamed_leaves_the_queue_usable():
    # A caller may append its own call and then stream it by passing it in.
    model = _model()
    api_call = await model.create_event(messages=MESSAGES)
    await model.executor.append(api_call)

    with patch.object(model.endpoint, "stream", side_effect=_chunks(2)):
        chunks = [
            chunk
            async for chunk in model.stream(api_call=api_call)
            if not isinstance(chunk, APICalling)
        ]

    assert len(chunks) == 2
    assert len(model.executor.pending) == 0
    assert len(model.executor.pile) == 0

    result = await _invoke_once(model)
    assert result.response == RESPONSE


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
        await queued.wait()
        with patch.object(model.endpoint, "stream", side_effect=_chunks(2)):
            chunks = await _stream_once(model)
        release.set()
        result = await invoke_task

    assert result.response == RESPONSE
    assert len(chunks) == 2
