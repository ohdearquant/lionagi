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
