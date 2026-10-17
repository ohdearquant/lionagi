# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""One iModel can stream and then invoke.

A call `stream()` creates is consumed by the generator, never by the executor's
queue, while a call the caller appended with `Executor.append` and then passed
in was queued at that append. Either way the stream must leave the queue and the
pile consistent however it ends, otherwise the next call the queue forwards is
one the pile no longer holds.
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


def _response(tag: str) -> dict:
    """One distinct response per call, so a test that reads the wrong call's
    response fails instead of matching a shared value."""
    return {
        **RESPONSE,
        "id": f"resp-{tag}",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"pong {tag}"}}],
    }


def _chunks(n: int, tag: str = "stream"):
    async def gen(*_args, **_kwargs):
        for i in range(n):
            yield {"choices": [{"delta": {"content": f"{tag} chunk {i}"}}]}

    return gen


async def _drain(agen) -> list:
    return [chunk async for chunk in agen if not isinstance(chunk, APICalling)]


async def _stream_once(model: iModel, tag: str = "stream") -> list:
    with patch.object(model.endpoint, "stream", side_effect=_chunks(2, tag)) as fake_stream:
        with anyio.fail_after(WAIT):
            chunks = await _drain(model.stream(messages=MESSAGES))

    assert fake_stream.call_count == 1, (
        f"the fake transport served {fake_stream.call_count} stream request(s), expected 1"
    )
    return chunks


async def _invoke_once(model: iModel, tag: str = "invoke") -> APICalling:
    async def call(*_args, **_kwargs):
        return _response(tag)

    with patch.object(model.endpoint, "call", side_effect=call) as fake_call:
        result = await model.invoke(messages=MESSAGES)

    assert fake_call.call_count == 1, (
        f"the fake transport served {fake_call.call_count} invoke request(s), expected 1"
    )
    return result


@pytest.mark.asyncio
async def test_invoke_after_one_stream_returns_the_invoke_response():
    model = _model()
    assert len(await _stream_once(model, "one-stream")) == 2

    result = await _invoke_once(model, "one-invoke")

    assert result.response == _response("one-invoke")


@pytest.mark.asyncio
async def test_invoke_after_two_streams_returns_the_invoke_response():
    model = _model()
    with patch.object(model.endpoint, "stream", side_effect=_chunks(2)) as fake_stream:
        assert len(await _drain(model.stream(messages=MESSAGES))) == 2
        assert len(await _drain(model.stream(messages=MESSAGES))) == 2

    assert fake_stream.call_count == 2
    result = await _invoke_once(model, "after-two-streams")

    assert result.response == _response("after-two-streams")


@pytest.mark.asyncio
async def test_invoke_stream_invoke_all_use_the_same_model():
    model = _model()
    first = await _invoke_once(model, "before-the-stream")
    assert first.response == _response("before-the-stream")

    assert len(await _stream_once(model, "between-the-invokes")) == 2

    second = await _invoke_once(model, "after-the-stream")
    assert second.response == _response("after-the-stream")


@pytest.mark.parametrize("concurrency_limit", [None, 2])
@pytest.mark.asyncio
async def test_a_streamed_call_leaves_the_executor_queue_and_pile_consistent(concurrency_limit):
    model = _model(concurrency_limit=concurrency_limit)
    assert len(await _stream_once(model, f"limit-{concurrency_limit}")) == 2

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

    result = await _invoke_once(model, "after-a-queued-stream")
    assert result.response == _response("after-a-queued-stream")


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
            # invoke runs while the call is still being consumed. Its content
            # is its own, so a crossed request cannot pass for its chunk.
            yield {"choices": [{"delta": {"content": "the stream's own first chunk"}}]}
            mid_stream.set()
            await release.wait()
        yield {"choices": [{"delta": {"content": "the stream's own second chunk"}}]}

    async def call(*_args, **_kwargs):
        return _response("during-an-open-stream")

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
        assert result.response == _response("during-an-open-stream")

        release.set()
        chunks = await asyncio.wait_for(stream_task, timeout=WAIT)

    assert len(chunks) == 2
    assert [c["choices"][0]["delta"]["content"] for c in chunks] == [
        "the stream's own first chunk",
        "the stream's own second chunk",
    ]


@pytest.mark.asyncio
async def test_a_stream_that_ends_while_an_invoke_waits_leaves_the_invoke_its_own_result():
    model = _model()
    await model.executor.start()
    queued = asyncio.Event()
    release = asyncio.Event()
    real_process = model.executor.processor.process

    async def gated_process():
        # Hold the invoke's send step open while a stream runs and finishes
        # on the same model. ``queued`` is set once the invoke's call is in the
        # pile and the processor's queue; the stream then runs to completion
        # before the processor sends anything.
        queued.set()
        await release.wait()
        await real_process()

    with patch.object(model.executor.processor, "process", side_effect=gated_process):
        invoke_task = asyncio.create_task(_invoke_once(model, "waiting-invoke"))
        await asyncio.wait_for(queued.wait(), timeout=WAIT)
        chunks = await _stream_once(model, "stream-that-ends")
        release.set()
        result = await asyncio.wait_for(invoke_task, timeout=WAIT)

    assert result.response == _response("waiting-invoke")
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
    # outlasts the timeout ends the run before the stream starts. This test
    # calls the run() streaming path, ``_stream_with_liveness``, directly with
    # an already-expired deadline: the call it creates must not be left for the
    # next invoke() to send.
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
        result = await _invoke_once(model, "after-a-past-deadline-run")
        assert result.response == _response("after-a-past-deadline-run")

    assert fake_stream.call_count == 0


@pytest.mark.asyncio
async def test_a_queued_call_cancelled_before_it_is_registered_leaves_the_queue_empty():
    # A caller may append its own call before streaming it, so the call holds a
    # pending entry from that append. A stream cancelled while the pile
    # registration is still in flight must drop that entry, or the next
    # forward() pops an id the pile no longer holds.
    model = _model()
    api_call = await model.create_event(messages=MESSAGES)
    await model.executor.append(api_call)
    entered = asyncio.Event()

    async def hang(self, item):
        entered.set()
        await asyncio.Event().wait()

    with (
        patch.object(type(model.executor.pile), "ainclude", new=hang),
        patch.object(model.endpoint, "stream", side_effect=_chunks(2)) as fake_stream,
    ):
        consumer = asyncio.create_task(_drain(model.stream(api_call=api_call)))
        await asyncio.wait_for(entered.wait(), timeout=WAIT)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, timeout=WAIT)

    assert fake_stream.call_count == 0
    assert len(model.executor.pending) == 0
    assert len(model.executor.pile) == 0

    result = await _invoke_once(model, "after-a-cancelled-registration")
    assert result.response == _response("after-a-cancelled-registration")


@pytest.mark.asyncio
async def test_a_queued_call_whose_processor_fails_to_start_leaves_the_queue_empty():
    # The caller's own append put the call in the queue before stream() took
    # it, so a processor start that raises must not leave that entry behind.
    model = _model()
    api_call = await model.create_event(messages=MESSAGES)
    await model.executor.append(api_call)
    assert len(model.executor.pending) == 1

    async def refused_start(*_args, **_kwargs):
        raise RuntimeError("processor could not start")

    with (
        patch.object(model.executor, "start", side_effect=refused_start),
        patch.object(model.endpoint, "stream", side_effect=_chunks(2)) as fake_stream,
    ):
        with pytest.raises(RuntimeError, match="processor could not start"):
            await _drain(model.stream(api_call=api_call))

    assert fake_stream.call_count == 0
    assert len(model.executor.pending) == 0
    assert len(model.executor.pile) == 0

    result = await _invoke_once(model, "after-a-failed-start")
    assert result.response == _response("after-a-failed-start")


@pytest.mark.asyncio
async def test_a_queued_call_cancelled_while_waiting_for_a_slot_leaves_the_queue_empty():
    # As above, one concurrency limit and the caller's own append before the
    # stream: the queue entry must not outlive a stream cancelled at the slot.
    model = _model(concurrency_limit=1)
    await model.executor.start()
    real_slot = model.executor.processor._concurrency_sem
    assert real_slot is not None
    await real_slot.acquire()

    api_call = await model.create_event(messages=MESSAGES)
    await model.executor.append(api_call)
    entered_slot_wait = asyncio.Event()

    class ObservedSlot:
        """Takes the real slot, which this test also holds, so the stream parks
        here until the test cancels it."""

        async def __aenter__(self):
            entered_slot_wait.set()
            await real_slot.acquire()
            return self

        async def __aexit__(self, *_exc):
            real_slot.release()

    try:
        with (
            patch.object(model.executor.processor, "_concurrency_sem", ObservedSlot()),
            patch.object(model.endpoint, "stream", side_effect=_chunks(2)) as fake_stream,
        ):
            consumer = asyncio.create_task(_drain(model.stream(api_call=api_call)))
            await asyncio.wait_for(entered_slot_wait.wait(), timeout=WAIT)
            assert fake_stream.call_count == 0, "the stream ran before it held the slot"
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(consumer, timeout=WAIT)
    finally:
        real_slot.release()

    assert fake_stream.call_count == 0
    assert len(model.executor.pending) == 0
    assert len(model.executor.pile) == 0

    result = await _invoke_once(model, "after-a-cancelled-slot-wait")
    assert result.response == _response("after-a-cancelled-slot-wait")
