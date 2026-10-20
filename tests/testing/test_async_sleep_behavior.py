# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Observable delay and cancellation behavior on the default asyncio backend."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import anyio
import pytest

from lionagi.service.connections import match_endpoint
from lionagi.service.connections._http import HTTPStatusError
from lionagi.testing import ScriptModel
from lionagi.testing.helpers import AsyncTestHelpers


def _endpoint(*, delay_ms: int, kind: str = "value_error"):
    return match_endpoint(
        "scripted",
        "chat",
        script=ScriptModel.from_responses(
            [
                {
                    "type": "error",
                    "kind": kind,
                    "message": "delayed error",
                    "delay_ms": delay_ms,
                },
                {"type": "text", "content": "next response"},
            ]
        ),
    )


async def _next_chunk(stream):
    return await anext(stream)


def _first_result(endpoint, streaming: bool):
    if streaming:
        stream = endpoint.stream({"messages": []})
        return stream, _next_chunk(stream)
    return None, endpoint._call(payload={"messages": []}, headers={})


async def _finish_owned_task(task, stream):
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError, HTTPStatusError, asyncio.TimeoutError, ValueError):
        await task
    if stream is not None:
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["call", "stream"])
@pytest.mark.parametrize(
    ("kind", "error_type"),
    [
        ("rate_limit", HTTPStatusError),
        ("timeout", asyncio.TimeoutError),
        ("value_error", ValueError),
    ],
)
async def test_delayed_error_records_only_after_wait_and_preserves_error(
    streaming, kind, error_type
):
    endpoint = _endpoint(delay_ms=50, kind=kind)
    stream, pending = _first_result(endpoint, streaming)
    task = asyncio.create_task(pending)
    try:
        with anyio.fail_after(2):
            await asyncio.sleep(0)
            assert not task.done()
            assert endpoint.script.cursor == 1
            assert endpoint.calls == []

            if streaming:
                chunk = await task
                assert chunk.type == "error"
                assert chunk.is_error is True
                assert chunk.content == "delayed error"
                assert len(endpoint.calls) == 1
                with pytest.raises(error_type, match="delayed error") as error:
                    await anext(stream)
            else:
                with pytest.raises(error_type, match="delayed error") as error:
                    await task

            assert len(endpoint.calls) == 1
            record = endpoint.calls[0]
            assert record.index == 0
            assert record.response_type == "error"
            assert record.response is None
            assert record.was_streamed is streaming
            if kind == "rate_limit":
                assert error.value.status == 429
    finally:
        await _finish_owned_task(task, stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["call", "stream"])
async def test_zero_error_delay_does_not_yield_before_recording(streaming):
    endpoint = _endpoint(delay_ms=0)
    yielded = []
    asyncio.get_running_loop().call_soon(yielded.append, True)
    stream, pending = _first_result(endpoint, streaming)
    try:
        if streaming:
            chunk = await pending
            assert chunk.is_error is True
            assert yielded == []
            with pytest.raises(ValueError, match="delayed error"):
                await anext(stream)
        else:
            with pytest.raises(ValueError, match="delayed error"):
                await pending
        assert yielded == []
        assert len(endpoint.calls) == 1
    finally:
        if stream is not None:
            await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["call", "stream"])
async def test_negative_error_delay_still_yields_before_recording(streaming):
    endpoint = _endpoint(delay_ms=-1)
    yielded = []
    asyncio.get_running_loop().call_soon(yielded.append, True)
    stream, pending = _first_result(endpoint, streaming)
    try:
        with anyio.fail_after(2):
            if streaming:
                chunk = await pending
                assert chunk.is_error is True
                with pytest.raises(ValueError, match="delayed error"):
                    await anext(stream)
            else:
                with pytest.raises(ValueError, match="delayed error"):
                    await pending
        assert yielded == [True]
        assert len(endpoint.calls) == 1
    finally:
        if stream is not None:
            await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["call", "stream"])
async def test_cancelling_error_delay_consumes_entry_without_recording(streaming):
    endpoint = _endpoint(delay_ms=10_000)
    stream, pending = _first_result(endpoint, streaming)
    task = asyncio.create_task(pending)
    try:
        with anyio.fail_after(2):
            await asyncio.sleep(0)
            assert not task.done()
            assert endpoint.script.cursor == 1
            assert endpoint.calls == []
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert endpoint.calls == []

            response = await endpoint._call(payload={"messages": []}, headers={})
            assert response["choices"][0]["message"]["content"] == "next response"
            assert len(endpoint.calls) == 1
            assert endpoint.calls[0].index == 0
            assert endpoint.calls[0].response_type == "text"
    finally:
        await _finish_owned_task(task, stream)


@pytest.mark.asyncio
async def test_cleanup_grace_allows_child_to_finish_and_preserves_result(capsys):
    marker = object()
    child = None
    finished = asyncio.Event()

    async def finish_child():
        await asyncio.sleep(0)
        finished.set()

    @AsyncTestHelpers.assert_async_context_cleanup
    async def operation():
        nonlocal child
        child = asyncio.create_task(finish_child())
        return marker

    try:
        with anyio.fail_after(2):
            assert await operation() is marker
            assert finished.is_set()
            assert capsys.readouterr().out == ""
    finally:
        if child is not None:
            await _finish_owned_task(child, None)
