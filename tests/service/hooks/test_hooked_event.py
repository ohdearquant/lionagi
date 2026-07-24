# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.service.hooks.hooked_event — HookedEvent._invoke() and _stream()."""

from types import SimpleNamespace

import pytest

from lionagi.protocols.types import EventStatus
from lionagi.service.hooks.hooked_event import HookedEvent

# ---------------------------------------------------------------------------
# Minimal concrete HookedEvent subclasses for testing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Minimal fake HookEvent — avoids setting up a real HookRegistry
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# HookedEvent._invoke() — no hooks
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# HookedEvent._invoke() — pre-invoke hook paths (lines 80-90)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# HookedEvent._invoke() — post-invoke hook paths (lines 101-122)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# HookedEvent._stream() — pre-hook paths (lines 145-155)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# HookedEvent._stream() — post-hook (lines 163-168)
# ---------------------------------------------------------------------------


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
    # Should NOT raise — stream data already sent
    chunks = [c async for c in h._stream()]
    assert chunks == ["chunk1", "chunk2"]


@pytest.mark.asyncio
async def test_stream_post_hook_recorded_failure_is_logged_at_warning(caplog):
    """A post-stream hook that fails is captured into the hook event's status
    rather than raised, so the failure must be surfaced by an explicit status
    check — otherwise it is invisible."""
    h = SimpleHooked()

    class RecordedFailurePostHook:
        def __init__(self):
            self.execution = SimpleNamespace(status=EventStatus.PENDING, error="post hook raised")
            self._should_exit = False
            self._exit_cause = None

        async def invoke(self):
            self.execution.status = EventStatus.ABORTED

    h._post_invoke_hook_event = RecordedFailurePostHook()
    with caplog.at_level("WARNING", logger="lionagi.service.hooks.hooked_event"):
        chunks = [c async for c in h._stream()]

    assert chunks == ["chunk1", "chunk2"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("post hook raised" in m for m in warnings), warnings


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [EventStatus.FAILED, EventStatus.CANCELLED, EventStatus.ABORTED])
async def test_stream_post_hook_warns_for_every_recorded_failure_status(status, caplog):
    h = SimpleHooked()
    h._post_invoke_hook_event = _fake_hook(status)
    h._post_invoke_hook_event.execution.error = f"hook ended {status.value}"

    with caplog.at_level("WARNING", logger="lionagi.service.hooks.hooked_event"):
        chunks = [c async for c in h._stream()]

    assert chunks == ["chunk1", "chunk2"]
    assert any(r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.asyncio
async def test_stream_post_hook_success_logs_no_warning(caplog):
    h = SimpleHooked()
    h._post_invoke_hook_event = _fake_hook(EventStatus.COMPLETED)

    with caplog.at_level("WARNING", logger="lionagi.service.hooks.hooked_event"):
        chunks = [c async for c in h._stream()]

    assert chunks == ["chunk1", "chunk2"]
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


@pytest.mark.asyncio
async def test_invoke_post_hook_aborted_is_logged_at_warning(caplog):
    """The non-streaming path must surface a recorded post-hook failure too.

    ``_invoke`` and ``_stream`` run the same post-hook through the same registry,
    and an ordinary hook exception is recorded there as ABORTED. On this path
    ABORTED matches neither the failure check that raises nor the exit-request
    branch, so it reached the fall-through and was written as event data only.
    The core result has already been produced by then, so the failure is not
    fatal here, but a caller watching warnings had no way to learn of it.
    """
    h = SimpleHooked()
    h._post_invoke_hook_event = _fake_hook(EventStatus.ABORTED)
    h._post_invoke_hook_event.execution.error = "post hook raised"

    with caplog.at_level("WARNING", logger="lionagi.service.hooks.hooked_event"):
        result = await h._invoke()

    assert result == "core_result"  # the core result still comes back
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("post hook raised" in m for m in warnings), warnings


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [EventStatus.FAILED, EventStatus.CANCELLED])
async def test_invoke_post_hook_failed_and_cancelled_still_raise(status):
    """Warning on ABORTED must not soften the statuses that already raise.

    This is the half of the contract a logging change is most likely to break:
    FAILED and CANCELLED are fatal on this path and must stay fatal.
    """
    h = SimpleHooked()
    h._post_invoke_hook_event = _fake_hook(status)

    with pytest.raises(RuntimeError, match=status.value):
        await h._invoke()


@pytest.mark.asyncio
async def test_invoke_post_hook_success_logs_no_warning(caplog):
    h = SimpleHooked()
    h._post_invoke_hook_event = _fake_hook(EventStatus.COMPLETED)

    with caplog.at_level("WARNING", logger="lionagi.service.hooks.hooked_event"):
        result = await h._invoke()

    assert result == "core_result"
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
