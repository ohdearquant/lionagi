# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.service.hooks.hooked_event — HookedEvent._invoke() and _stream()."""

import asyncio
import contextvars
import gc
import logging
from types import SimpleNamespace

import anyio
import pytest
from pydantic import PrivateAttr

from lionagi.ln.concurrency import get_cancelled_exc_class
from lionagi.protocols.types import EventStatus
from lionagi.service.hooks import hooked_event
from lionagi.service.hooks._types import StreamTerminalState
from lionagi.service.hooks.hooked_event import HookedEvent


class SimpleHooked(HookedEvent):
    async def _core_invoke(self):
        return "core_result"

    async def _core_stream(self):
        yield "chunk1"
        yield "chunk2"


class FailingHooked(HookedEvent):
    async def _core_invoke(self):
        raise ValueError("core_failed")

    async def _core_stream(self):
        raise ValueError("core_stream_failed")
        yield  # make it an async generator


def _fake_hook(
    status: EventStatus = EventStatus.COMPLETED,
    should_exit: bool = False,
    exit_cause: BaseException | None = None,
):
    class _FakeHookEvent:
        def __init__(self):
            self.execution = SimpleNamespace(status=status, error=None)
            self._should_exit = should_exit
            self._exit_cause = exit_cause

        async def invoke(self):
            pass

    return _FakeHookEvent()


@pytest.mark.asyncio
async def test_invoke_no_hooks_returns_core_result():
    """With no hooks attached, _invoke returns _core_invoke result."""
    h = SimpleHooked()
    result = await h._invoke()
    assert result == "core_result"


@pytest.mark.asyncio
async def test_invoke_no_hooks_core_error_propagates():
    """Core errors propagate when no hooks are set."""
    h = FailingHooked()
    with pytest.raises(ValueError, match="core_failed"):
        await h._invoke()


@pytest.mark.asyncio
async def test_public_invoke_signals_precreated_completion_event_on_success():
    event = SimpleHooked()
    done = event.completion_event

    await event.invoke()

    assert event.status is EventStatus.COMPLETED
    assert done.is_set()


@pytest.mark.asyncio
async def test_public_invoke_late_completion_event_access_is_already_signalled():
    event = SimpleHooked()

    await event.invoke()

    assert event.status is EventStatus.COMPLETED
    assert event.completion_event.is_set()


@pytest.mark.parametrize(
    ("status", "signals_completion"),
    (
        (EventStatus.PENDING, False),
        (EventStatus.PROCESSING, False),
        (EventStatus.COMPLETED, True),
        (EventStatus.FAILED, True),
        (EventStatus.SKIPPED, True),
        (EventStatus.CANCELLED, True),
        (EventStatus.ABORTED, True),
    ),
)
def test_completion_event_terminal_status_vocabulary_is_exact(status, signals_completion):
    event = SimpleHooked()
    done = event.completion_event

    event.status = status

    assert done.is_set() is signals_completion


@pytest.mark.asyncio
async def test_public_invoke_signals_precreated_completion_event_on_failure():
    event = FailingHooked()
    done = event.completion_event

    await event.invoke()

    assert event.status is EventStatus.FAILED
    assert done.is_set()


@pytest.mark.asyncio
async def test_exhausted_public_stream_signals_precreated_completion_event():
    event = SimpleHooked()
    done = event.completion_event

    chunks = [chunk async for chunk in event.stream()]

    assert chunks == ["chunk1", "chunk2"]
    assert event.status is EventStatus.COMPLETED
    assert done.is_set()


@pytest.mark.asyncio
async def test_closed_public_stream_signals_precreated_completion_event():
    event = SlowStream()
    done = event.completion_event
    stream = event.stream()

    assert await anext(stream) == "chunk1"
    await stream.aclose()

    assert event.status is EventStatus.CANCELLED
    assert done.is_set()


@pytest.mark.asyncio
async def test_invoke_pre_hook_completed_runs_core():
    """COMPLETED pre-hook runs; core result is returned."""
    h = SimpleHooked()
    h._pre_invoke_hook_event = _fake_hook(EventStatus.COMPLETED)
    result = await h._invoke()
    assert result == "core_result"


@pytest.mark.asyncio
async def test_invoke_pre_hook_failed_raises_runtime_error():
    """FAILED pre-hook raises RuntimeError before core runs (line 83)."""
    h = SimpleHooked()
    h._pre_invoke_hook_event = _fake_hook(EventStatus.FAILED)
    with pytest.raises(RuntimeError, match="Pre-invoke hook"):
        await h._invoke()


@pytest.mark.asyncio
async def test_invoke_pre_hook_cancelled_raises_runtime_error():
    """CANCELLED pre-hook raises RuntimeError (line 83)."""
    h = SimpleHooked()
    h._pre_invoke_hook_event = _fake_hook(EventStatus.CANCELLED)
    with pytest.raises(RuntimeError, match="Pre-invoke hook"):
        await h._invoke()


@pytest.mark.asyncio
async def test_invoke_pre_hook_should_exit_raises_exit_cause():
    """Pre-hook _should_exit=True with a cause raises that cause (lines 87-88)."""
    h = SimpleHooked()
    h._pre_invoke_hook_event = _fake_hook(
        EventStatus.COMPLETED,
        should_exit=True,
        exit_cause=RuntimeError("abort by hook"),
    )
    with pytest.raises(RuntimeError, match="abort by hook"):
        await h._invoke()


@pytest.mark.asyncio
async def test_invoke_pre_hook_should_exit_no_cause_raises_generic():
    """Pre-hook _should_exit=True with no cause raises generic RuntimeError (line 88)."""
    h = SimpleHooked()
    h._pre_invoke_hook_event = _fake_hook(EventStatus.COMPLETED, should_exit=True, exit_cause=None)
    with pytest.raises(RuntimeError, match="requested exit"):
        await h._invoke()


@pytest.mark.asyncio
async def test_invoke_post_hook_completed_returns_core_result():
    """COMPLETED post-hook; core result is returned."""
    h = SimpleHooked()
    h._post_invoke_hook_event = _fake_hook(EventStatus.COMPLETED)
    result = await h._invoke()
    assert result == "core_result"


@pytest.mark.asyncio
async def test_invoke_post_hook_failed_raises_when_core_succeeded():
    """FAILED post-hook raises RuntimeError when core succeeded (lines 108-110)."""
    h = SimpleHooked()
    h._post_invoke_hook_event = _fake_hook(EventStatus.FAILED)
    with pytest.raises(RuntimeError, match="Post-invoke hook"):
        await h._invoke()


@pytest.mark.asyncio
async def test_invoke_post_hook_failed_silenced_when_core_failed():
    """FAILED post-hook is silenced when core already raised (lines 118-120)."""
    h = FailingHooked()
    h._post_invoke_hook_event = _fake_hook(EventStatus.FAILED)
    # Only the core error should surface
    with pytest.raises(ValueError, match="core_failed"):
        await h._invoke()


@pytest.mark.asyncio
async def test_invoke_post_hook_should_exit_raises_when_core_succeeded():
    """Post-hook _should_exit=True raises exit cause when core succeeded (lines 112-115)."""
    h = SimpleHooked()
    h._post_invoke_hook_event = _fake_hook(
        EventStatus.COMPLETED,
        should_exit=True,
        exit_cause=RuntimeError("post exit"),
    )
    with pytest.raises(RuntimeError, match="post exit"):
        await h._invoke()


@pytest.mark.asyncio
async def test_invoke_post_hook_should_exit_silenced_when_core_failed():
    """Post-hook _should_exit is ignored when core already failed."""
    h = FailingHooked()
    h._post_invoke_hook_event = _fake_hook(
        EventStatus.COMPLETED,
        should_exit=True,
        exit_cause=RuntimeError("post exit"),
    )
    # Core error wins
    with pytest.raises(ValueError, match="core_failed"):
        await h._invoke()


@pytest.mark.asyncio
async def test_stream_no_hooks_yields_chunks():
    """_stream() with no hooks yields all _core_stream chunks."""
    h = SimpleHooked()
    chunks = [c async for c in h._stream()]
    assert chunks == ["chunk1", "chunk2"]


@pytest.mark.asyncio
async def test_stream_pre_hook_completed_yields_chunks():
    """COMPLETED pre-hook; all chunks yielded."""
    h = SimpleHooked()
    h._pre_invoke_hook_event = _fake_hook(EventStatus.COMPLETED)
    chunks = [c async for c in h._stream()]
    assert chunks == ["chunk1", "chunk2"]


@pytest.mark.asyncio
async def test_stream_pre_hook_failed_raises_before_chunks():
    """FAILED pre-hook raises RuntimeError before any chunks are yielded (line 148)."""
    h = SimpleHooked()
    h._pre_invoke_hook_event = _fake_hook(EventStatus.FAILED)
    with pytest.raises(RuntimeError, match="Pre-invoke hook"):
        async for _ in h._stream():
            pass


@pytest.mark.asyncio
async def test_stream_pre_hook_should_exit_raises():
    """Pre-hook _should_exit raises in _stream() (line 152)."""
    h = SimpleHooked()
    h._pre_invoke_hook_event = _fake_hook(
        EventStatus.COMPLETED,
        should_exit=True,
        exit_cause=RuntimeError("stream exit"),
    )
    with pytest.raises(RuntimeError, match="stream exit"):
        async for _ in h._stream():
            pass


@pytest.mark.asyncio
async def test_stream_post_hook_does_not_affect_chunks():
    """Post-hook runs after stream; chunks are not affected (lines 163-168)."""
    h = SimpleHooked()
    h._post_invoke_hook_event = _fake_hook(EventStatus.COMPLETED)
    chunks = [c async for c in h._stream()]
    assert chunks == ["chunk1", "chunk2"]


@pytest.mark.asyncio
async def test_stream_post_hook_failure_silenced():
    """Post-hook failure after stream is silenced (line 166-167)."""
    h = SimpleHooked()

    class ExplodingPostHook:
        execution = SimpleNamespace(status=EventStatus.COMPLETED, error=None)
        _should_exit = False
        _exit_cause = None

        async def invoke(self):
            raise RuntimeError("post hook explodes after stream")

    h._post_invoke_hook_event = ExplodingPostHook()
    # Must not raise: the stream data was already sent before the post-hook ran.
    chunks = [c async for c in h._stream()]
    assert chunks == ["chunk1", "chunk2"]


@pytest.mark.asyncio
async def test_stream_post_hook_aborted_status_logs_warning(caplog):
    """A normal (non-raising) post-hook failure — the shape HookRegistry.
    post_invocation() actually produces, recording the error on the
    HookEvent with status ABORTED instead of raising out of invoke() — must
    still log the promised warning, not be silently dropped."""
    h = SimpleHooked()
    h._post_invoke_hook_event = _fake_hook(EventStatus.ABORTED)
    h._post_invoke_hook_event.execution.error = "post hook probe failure"

    with caplog.at_level("WARNING", logger="lionagi.service.hooks.hooked_event"):
        chunks = [c async for c in h._stream()]

    assert chunks == ["chunk1", "chunk2"]
    assert any("Post-stream hook failed" in r.message for r in caplog.records)


class SlowStream(HookedEvent):
    """Yields one chunk, then waits long enough to be cancelled from outside."""

    async def _core_stream(self):
        yield "chunk1"
        await anyio.sleep(30)
        yield "chunk2"


class RaisingStream(HookedEvent):
    """Yields one chunk, then raises the exception object handed to it.

    Raising a caller-held object is what makes identity assertable: the test can
    compare what the consumer caught against what the source actually raised.
    """

    _to_raise: BaseException = PrivateAttr(None)

    async def _core_stream(self):
        yield "chunk1"
        raise self._to_raise


class CancelCapturingStream(HookedEvent):
    """Waits to be cancelled from outside, keeping the cancellation it was handed."""

    _delivered: list = PrivateAttr(default_factory=list)

    async def _core_stream(self):
        yield "chunk1"
        try:
            await anyio.sleep(30)
        except get_cancelled_exc_class() as e:
            self._delivered.append(e)
            raise
        yield "chunk2"


def _post_hook_raising(exc: BaseException):
    """A post-hook that raises ``exc`` out of invoke()."""

    class _RaisingHookEvent:
        execution = SimpleNamespace(status=EventStatus.COMPLETED, error=None)
        _should_exit = False
        _exit_cause = None

        async def invoke(self):
            raise exc

    return _RaisingHookEvent()


def _recording_post_hook(event: HookedEvent):
    """A post-hook that records the terminal state visible to it when it runs."""
    seen: list[StreamTerminalState | None] = []

    class _RecordingHookEvent:
        def __init__(self):
            self.execution = SimpleNamespace(status=EventStatus.COMPLETED, error=None)
            self._should_exit = False
            self._exit_cause = None

        async def invoke(self):
            seen.append(event.stream_terminal_state)

    event._post_invoke_hook_event = _RecordingHookEvent()
    return seen


@pytest.mark.asyncio
async def test_stream_post_hook_runs_when_core_stream_raises():
    """A source error must not skip teardown, and must reach the caller unchanged.

    Unchanged means the same object: a teardown that replaced it with an equivalent
    one would still satisfy a type-and-message check while losing the original
    traceback and any state the caller attached to it.
    """
    source = ValueError("core_stream_failed")
    h = RaisingStream()
    h._to_raise = source
    seen = _recording_post_hook(h)

    with pytest.raises(ValueError) as caught:
        async for _ in h._stream():
            pass

    assert caught.value is source
    assert seen == [StreamTerminalState.Failed]
    assert h.stream_terminal_state is StreamTerminalState.Failed


@pytest.mark.asyncio
async def test_stream_post_hook_runs_when_consumer_breaks_early():
    """A consumer that stops after the first chunk still gets teardown."""
    h = SimpleHooked()
    seen = _recording_post_hook(h)

    stream = h._stream()
    async for chunk in stream:
        assert chunk == "chunk1"
        break
    await stream.aclose()

    assert seen == [StreamTerminalState.Closed]
    assert h.stream_terminal_state is StreamTerminalState.Closed


@pytest.mark.asyncio
async def test_stream_post_hook_runs_when_consuming_task_is_cancelled():
    """Cancellation still propagates, and teardown runs shielded from it."""
    h = SlowStream()
    seen = _recording_post_hook(h)
    first_chunk = asyncio.Event()

    async def consume():
        async for _ in h._stream():
            first_chunk.set()

    task = asyncio.create_task(consume())
    await first_chunk.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert seen == [StreamTerminalState.Cancelled]
    assert h.stream_terminal_state is StreamTerminalState.Cancelled


@pytest.mark.asyncio
async def test_stream_post_hook_runs_when_an_enclosing_timeout_fires():
    """A timeout around the consumer reaches the stream as cancellation."""
    h = SlowStream()
    seen = _recording_post_hook(h)

    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.05):
            async for _ in h._stream():
                pass

    assert seen == [StreamTerminalState.Cancelled]
    assert h.stream_terminal_state is StreamTerminalState.Cancelled


@pytest.mark.asyncio
async def test_stream_terminal_state_is_completed_on_exhaustion():
    """The ordinary path keeps reporting itself as a completed stream."""
    h = SimpleHooked()
    seen = _recording_post_hook(h)

    chunks = [c async for c in h._stream()]

    assert chunks == ["chunk1", "chunk2"]
    assert seen == [StreamTerminalState.Completed]
    assert h.stream_terminal_state is StreamTerminalState.Completed


@pytest.mark.asyncio
async def test_concurrent_stream_hooks_see_their_own_terminal_state():
    """Each concurrent stream's hook observes how that stream ended."""
    current_stream = contextvars.ContextVar("current_stream")
    first_hook_started = asyncio.Event()
    release_first_hook = asyncio.Event()
    seen: dict[str, StreamTerminalState | None] = {}

    class ConcurrentStream(HookedEvent):
        async def _core_stream(self):
            if current_stream.get() == "first":
                raise ValueError("first stream failed")
            yield "second stream completed"

    class RecordingPostHook:
        execution = SimpleNamespace(status=EventStatus.COMPLETED, error=None)
        _should_exit = False
        _exit_cause = None

        async def invoke(self):
            stream = current_stream.get()
            if stream == "first":
                first_hook_started.set()
                await release_first_hook.wait()
            seen[stream] = event.stream_terminal_state

    event = ConcurrentStream()
    event._post_invoke_hook_event = RecordingPostHook()

    async def consume(stream: str):
        current_stream.set(stream)
        return [chunk async for chunk in event._stream()]

    first = asyncio.create_task(consume("first"))
    await first_hook_started.wait()
    assert await consume("second") == ["second stream completed"]
    release_first_hook.set()
    with pytest.raises(ValueError, match="first stream failed"):
        await first

    assert seen == {
        "first": StreamTerminalState.Failed,
        "second": StreamTerminalState.Completed,
    }
    assert event.stream_terminal_state is StreamTerminalState.Failed


@pytest.mark.asyncio
async def test_public_stream_retains_terminal_state_for_parent_after_context_reset():
    """The parent keeps seeing the completed state after child invocation cleanup."""
    event = SimpleHooked()

    async def consume():
        return [chunk async for chunk in event.stream()]

    assert await asyncio.create_task(consume()) == ["chunk1", "chunk2"]
    assert event.stream_terminal_state is StreamTerminalState.Completed


@pytest.mark.asyncio
async def test_public_stream_can_be_advanced_from_different_task_contexts():
    """A timeout task may advance each chunk without corrupting normal exhaustion."""
    event = SimpleHooked()
    stream = event.stream()

    assert await asyncio.wait_for(anext(stream), 1) == "chunk1"
    assert await asyncio.wait_for(anext(stream), 1) == "chunk2"
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), 1)

    assert event.status is EventStatus.COMPLETED
    assert event.execution.error is None
    assert event.stream_terminal_state is StreamTerminalState.Completed


@pytest.mark.asyncio
async def test_stream_teardown_does_not_block_the_close_indefinitely(monkeypatch, caplog):
    """Teardown runs on the close path, but a slow hook must not hold the close open."""
    monkeypatch.setattr(hooked_event, "POST_STREAM_TEARDOWN_GRACE", 0.1)

    class SlowPostHook:
        execution = SimpleNamespace(status=EventStatus.COMPLETED, error=None)
        _should_exit = False
        _exit_cause = None

        async def invoke(self):
            await anyio.sleep(30)

    h = SimpleHooked()
    h._post_invoke_hook_event = SlowPostHook()

    stream = h._stream()
    async for _ in stream:
        break

    started = anyio.current_time()
    with caplog.at_level("WARNING", logger="lionagi.service.hooks.hooked_event"):
        await stream.aclose()
    elapsed = anyio.current_time() - started

    assert elapsed < 5, f"close waited {elapsed}s on a hook it should have given up on"
    assert any("did not finish within" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_public_stream_closes_the_inner_stream_when_closed_early():
    """Closing Event.stream() must close the hooked stream it wraps, so teardown
    happens at the close rather than whenever the interpreter gets around to it."""
    h = SimpleHooked()
    seen = _recording_post_hook(h)

    stream = h.stream()
    async for _ in stream:
        break
    await stream.aclose()

    assert seen == [StreamTerminalState.Closed]


@pytest.mark.asyncio
async def test_bare_break_defers_teardown_to_generator_finalization():
    """A consumer that breaks without closing does not get teardown at the break.

    ``break`` does not close the generator it was iterating, so nothing raises
    GeneratorExit at that point and the post-invocation hook has not run yet. The
    interpreter still finalizes the abandoned generator, and teardown runs then with
    the closed terminal state -- but at a moment the consumer does not choose. This
    is why an early-stopping consumer that needs the hook to have run should close
    the stream itself.
    """
    h = SimpleHooked()
    seen = _recording_post_hook(h)

    stream = h.stream()
    async for _ in stream:
        break

    assert seen == [], "teardown must not be attributed to a break that did not close"
    assert h.stream_terminal_state is None

    del stream
    gc.collect()
    with anyio.fail_after(5):
        while not seen:
            await anyio.sleep(0.01)

    assert seen == [StreamTerminalState.Closed]


# HookedEvent._stream() — a failing teardown must not replace the ending it
# was there to record


@pytest.mark.parametrize(
    "hook_exc",
    [
        pytest.param(asyncio.CancelledError("post hook cancelled itself"), id="cancelled"),
        pytest.param(RuntimeError("post hook exploded"), id="ordinary"),
    ],
)
@pytest.mark.asyncio
async def test_source_error_survives_a_failing_teardown(hook_exc):
    """A source error reaches the caller as the same object, whatever the hook does.

    A cancellation is the interesting half: it is a BaseException, so a teardown
    guard written against Exception lets it out of the finally and the caller gets
    the hook's cancellation instead of the failure that ended the stream.
    """
    source = ValueError("core_stream_failed")
    h = RaisingStream()
    h._to_raise = source
    h._post_invoke_hook_event = _post_hook_raising(hook_exc)

    with pytest.raises(ValueError) as caught:
        async for _ in h._stream():
            pass

    assert caught.value is source
    assert h.stream_terminal_state is StreamTerminalState.Failed


@pytest.mark.parametrize(
    "hook_exc",
    [
        pytest.param(asyncio.CancelledError("post hook cancelled itself"), id="cancelled"),
        pytest.param(RuntimeError("post hook exploded"), id="ordinary"),
    ],
)
@pytest.mark.asyncio
async def test_normal_completion_survives_a_failing_teardown(hook_exc):
    """A stream that ended normally still ends normally when its teardown fails."""
    h = SimpleHooked()
    h._post_invoke_hook_event = _post_hook_raising(hook_exc)

    chunks = [c async for c in h._stream()]

    assert chunks == ["chunk1", "chunk2"]
    assert h.stream_terminal_state is StreamTerminalState.Completed


@pytest.mark.parametrize(
    "hook_exc",
    [
        pytest.param(asyncio.CancelledError("post hook cancelled itself"), id="cancelled"),
        pytest.param(RuntimeError("post hook exploded"), id="ordinary"),
    ],
)
@pytest.mark.asyncio
async def test_early_close_survives_a_failing_teardown(hook_exc):
    """aclose() on an early-stopped stream returns, it does not raise the hook's failure."""
    h = SimpleHooked()
    h._post_invoke_hook_event = _post_hook_raising(hook_exc)

    stream = h._stream()
    async for _ in stream:
        break
    await stream.aclose()

    assert h.stream_terminal_state is StreamTerminalState.Closed


@pytest.mark.asyncio
async def test_outer_cancellation_survives_a_failing_teardown():
    """A cancelled consumer gets back the cancellation it was handed, not the hook's.

    Identity is checked inside the consuming coroutine: asyncio re-wraps a task's
    cancellation at the task boundary, so awaiting the task cannot see it.
    """
    h = CancelCapturingStream()
    h._post_invoke_hook_event = _post_hook_raising(
        asyncio.CancelledError("post hook cancelled itself")
    )
    first_chunk = asyncio.Event()
    caught: list[BaseException] = []

    async def consume():
        try:
            async for _ in h._stream():
                first_chunk.set()
        except asyncio.CancelledError as e:
            caught.append(e)
            raise

    task = asyncio.create_task(consume())
    await first_chunk.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert caught and caught[0] is h._delivered[0]
    assert h.stream_terminal_state is StreamTerminalState.Cancelled


@pytest.mark.asyncio
async def test_consumer_cancelled_during_teardown_is_still_cancelled():
    """The cancellation the fix could swallow: one delivered while teardown awaits.

    The stream itself ended normally, so nothing is in flight to protect, and the
    cancellation belongs to the consuming task. Swallowing it here would leave a
    task that was cancelled from outside running on as if it never had been.
    """
    hook_running = asyncio.Event()

    class BlockingPostHook:
        execution = SimpleNamespace(status=EventStatus.COMPLETED, error=None)
        _should_exit = False
        _exit_cause = None

        async def invoke(self):
            hook_running.set()
            await anyio.sleep(30)

    h = SimpleHooked()
    h._post_invoke_hook_event = BlockingPostHook()

    async def consume():
        async for _ in h._stream():
            pass

    task = asyncio.create_task(consume())
    await hook_running.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled(), "a consumer cancelled from outside must stay cancelled"


@pytest.mark.asyncio
async def test_consumer_cancelled_as_the_hook_ends_is_still_cancelled():
    """A cancellation delivered in the same loop turn the hook finishes in still wins.

    The ordering is forced rather than raced. The hook queues the consumer's
    ``Task.cancel()`` with ``call_soon`` and then raises its own cancellation without
    awaiting, so the cancel callback is already on the ready queue while the hook is
    still running, and the hook's task finishes before that callback is drained. Any
    rule that decides whose cancellation this was by looking at whether the hook has
    finished reads "the hook's" here, every time, and loses a consumer cancellation
    that was delivered from outside.
    """
    holder: dict = {}

    class SelfCancellingPostHook:
        execution = SimpleNamespace(status=EventStatus.COMPLETED, error=None)
        _should_exit = False
        _exit_cause = None

        async def invoke(self):
            asyncio.get_running_loop().call_soon(holder["task"].cancel)
            raise asyncio.CancelledError("post hook cancelled itself")

    h = SimpleHooked()
    h._post_invoke_hook_event = SelfCancellingPostHook()

    async def consume():
        async for _ in h._stream():
            pass

    task = asyncio.create_task(consume())
    holder["task"] = task
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled(), "a consumer cancelled from outside must stay cancelled"


def test_post_hook_runs_on_a_non_asyncio_backend():
    """The hook must actually run on Trio, not fail on an asyncio object.

    An asyncio task or future is not awaitable under Trio, and creating one there does
    not fail where it is created -- it fails at the await, far enough away that the
    teardown guard logs it and the hook never runs at all. So the backend has to be
    settled before any asyncio object exists.
    """
    seen: list[StreamTerminalState | None] = []
    h = SimpleHooked()

    class _RecordingHookEvent:
        execution = SimpleNamespace(status=EventStatus.COMPLETED, error=None)
        _should_exit = False
        _exit_cause = None

        async def invoke(self):
            seen.append(h.stream_terminal_state)

    h._post_invoke_hook_event = _RecordingHookEvent()

    async def main():
        return [c async for c in h._stream()]

    chunks = anyio.run(main, backend="trio")

    assert chunks == ["chunk1", "chunk2"]
    assert seen == [StreamTerminalState.Completed]


def test_cancellation_during_teardown_propagates_on_a_non_asyncio_backend():
    """Off asyncio the two cancellations are indistinguishable, so both propagate.

    That is the documented behaviour on such a backend, and it is what must execute:
    a cancellation delivered while the hook is running reaches the consumer.
    """
    started: list[bool] = []

    class BlockingPostHook:
        execution = SimpleNamespace(status=EventStatus.COMPLETED, error=None)
        _should_exit = False
        _exit_cause = None

        async def invoke(self):
            started.append(True)
            await anyio.sleep(30)

    h = SimpleHooked()
    h._post_invoke_hook_event = BlockingPostHook()

    async def main():
        with anyio.move_on_after(0.1) as scope:
            async for _ in h._stream():
                pass
        return scope.cancelled_caught

    cancelled_caught = anyio.run(main, backend="trio")

    assert started, "the post hook must have run on this backend"
    assert cancelled_caught, "the cancellation must reach the consumer, not be swallowed"


def test_a_hook_that_will_not_stop_is_reported_not_destroyed_pending():
    """A hook that swallows its cancellation is abandoned loudly, not silently.

    It cannot be waited on forever, so after the stop grace it is left running -- but
    reported at WARNING and kept referenced, so the interpreter does not destroy it
    mid-await and announce that itself.
    """
    from lionagi.service.hooks.hooked_event import (
        POST_STREAM_HOOK_STOP_GRACE,
        _abandoned_post_stream_hooks,
    )

    class ResistantPostHook:
        execution = SimpleNamespace(status=EventStatus.COMPLETED, error=None)
        _should_exit = False
        _exit_cause = None

        async def invoke(self):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                pass  # refuses the first cancellation
            await asyncio.sleep(30)

    h = SlowStream()
    h._post_invoke_hook_event = ResistantPostHook()

    async def main():
        started = asyncio.Event()

        async def consume():
            async for _ in h._stream():
                started.set()

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture()
    logging.getLogger("asyncio").addHandler(handler)
    logging.getLogger(hooked_event.__name__).addHandler(handler)
    known = set(_abandoned_post_stream_hooks)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
        logging.getLogger("asyncio").removeHandler(handler)
        logging.getLogger(hooked_event.__name__).removeHandler(handler)

    gc.collect()

    assert set(_abandoned_post_stream_hooks) - known, "the abandoned hook must be retained"
    assert any(f"did not stop within {POST_STREAM_HOOK_STOP_GRACE}s" in r for r in records)
    assert not [r for r in records if "destroyed but it is pending" in r], records
