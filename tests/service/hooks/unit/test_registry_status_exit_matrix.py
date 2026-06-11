# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test status/exit matrix for all hook types and exception scenarios."""

import pytest

from lionagi.protocols.types import EventStatus
from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.hooks.hook_registry import HookRegistry
from tests.service.hooks.conftest import FakeEvent, FakeEventType, MyCancelled


@pytest.fixture(autouse=True)
def patch_cancel(monkeypatch):
    """Auto-patch cancellation class for all tests in this module."""
    from lionagi.service.hooks import hook_registry

    monkeypatch.setattr(hook_registry, "get_cancelled_exc_class", lambda: MyCancelled)
    yield
    # auto-unpatch


class TestPreEventCreateMatrix:
    """Test status/exit matrix for pre_event_create hook."""

    @pytest.mark.anyio
    async def test_pre_event_create_normal_completion(self):
        """Normal completion: should_exit=False, status=COMPLETED."""

        async def hook(ev_type, **kw):
            return FakeEvent("created", 1.0)

        registry = HookRegistry(hooks={HookEventTypes.PreEventCreate: hook})
        res, se, st = await registry.pre_event_create(FakeEventType, exit=False)

        assert se is False
        assert st == EventStatus.COMPLETED
        assert isinstance(res, FakeEvent)

    @pytest.mark.anyio
    async def test_pre_event_create_cancelled_should_exit_true(self):
        """Cancelled exception: should_exit=True, status=CANCELLED."""

        async def hook(ev_type, **kw):
            raise MyCancelled("cancelled")

        registry = HookRegistry(hooks={HookEventTypes.PreEventCreate: hook})
        res, se, st = await registry.pre_event_create(FakeEventType, exit=False)

        assert se is True
        assert st == EventStatus.CANCELLED
        assert isinstance(res, tuple) and len(res) == 2  # (UNDEFINED, exception)

    @pytest.mark.anyio
    async def test_pre_event_create_other_exception_respects_exit_policy(self):
        """Other exception: should_exit=exit, status=CANCELLED."""

        async def hook(ev_type, **kw):
            raise RuntimeError("boom")

        registry = HookRegistry(hooks={HookEventTypes.PreEventCreate: hook})

        # exit=False -> should_exit=False
        res, se, st = await registry.pre_event_create(FakeEventType, exit=False)
        assert se is False
        assert st == EventStatus.CANCELLED
        assert isinstance(res, RuntimeError)

        # exit=True -> should_exit=True
        res, se, st = await registry.pre_event_create(FakeEventType, exit=True)
        assert se is True
        assert st == EventStatus.CANCELLED
        assert isinstance(res, RuntimeError)


class TestPreInvocationMatrix:
    """Test status/exit matrix for pre_invocation hook."""

    @pytest.mark.anyio
    async def test_pre_invocation_normal_completion(self):
        """Normal completion: should_exit=False, status=COMPLETED."""

        async def hook(ev, **kw):
            return "permission_granted"

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: hook})
        res, se, st = await registry.pre_invocation(FakeEvent(), exit=False)

        assert se is False
        assert st == EventStatus.COMPLETED
        assert res == "permission_granted"

    @pytest.mark.anyio
    async def test_pre_invocation_cancelled_should_exit_true(self):
        """Cancelled exception: should_exit=True, status=CANCELLED."""

        async def hook(ev, **kw):
            raise MyCancelled("permission denied")

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: hook})
        res, se, st = await registry.pre_invocation(FakeEvent(), exit=False)

        assert se is True
        assert st == EventStatus.CANCELLED
        assert isinstance(res, tuple) and len(res) == 2

    @pytest.mark.anyio
    async def test_pre_invocation_other_exception_respects_exit_policy(self):
        """Other exception: should_exit=exit, status=CANCELLED."""

        async def hook(ev, **kw):
            raise RuntimeError("auth error")

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: hook})

        # exit=False -> should_exit=False
        res, se, st = await registry.pre_invocation(FakeEvent(), exit=False)
        assert se is False
        assert st == EventStatus.CANCELLED
        assert isinstance(res, RuntimeError)

        # exit=True -> should_exit=True
        res, se, st = await registry.pre_invocation(FakeEvent(), exit=True)
        assert se is True
        assert st == EventStatus.CANCELLED
        assert isinstance(res, RuntimeError)


class TestPostInvocationMatrix:
    """Test status/exit matrix for post_invocation hook."""

    @pytest.mark.anyio
    async def test_post_invocation_normal_completion(self):
        """Normal completion: should_exit=False, status=COMPLETED."""

        async def hook(ev, **kw):
            return "logged"

        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: hook})
        res, se, st = await registry.post_invocation(FakeEvent(), exit=False)

        assert se is False
        assert st == EventStatus.COMPLETED
        assert res == "logged"

    @pytest.mark.anyio
    async def test_post_invocation_cancelled_should_exit_true(self):
        """Cancelled exception: should_exit=True, status=CANCELLED."""

        async def hook(ev, **kw):
            raise MyCancelled("log cancelled")

        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: hook})
        res, se, st = await registry.post_invocation(FakeEvent(), exit=False)

        assert se is True
        assert st == EventStatus.CANCELLED
        assert isinstance(res, tuple) and len(res) == 2

    @pytest.mark.anyio
    async def test_post_invocation_other_exception_aborted_respects_exit_policy(
        self,
    ):
        """Other exception: should_exit=exit, status=ABORTED (not CANCELLED for post)."""

        async def hook(ev, **kw):
            raise RuntimeError("log failed")

        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: hook})

        # exit=False -> should_exit=False, status=ABORTED
        res, se, st = await registry.post_invocation(FakeEvent(), exit=False)
        assert se is False
        assert st == EventStatus.ABORTED  # Post uses ABORTED, not CANCELLED
        assert isinstance(res, RuntimeError)

        # exit=True -> should_exit=True, status=ABORTED
        res, se, st = await registry.post_invocation(FakeEvent(), exit=True)
        assert se is True
        assert st == EventStatus.ABORTED
        assert isinstance(res, RuntimeError)


class TestStreamHandlerMatrix:
    """Test status/exit matrix for stream handlers."""

    @pytest.mark.anyio
    async def test_stream_handler_normal_completion(self):
        """Normal completion: should_exit=False, status=None."""

        async def handler(ev, ct, ch, **kw):
            return f"handled {ch}"

        registry = HookRegistry(stream_handlers={"test": handler})
        res, se, st = await registry.handle_streaming_chunk("test", "chunk_data", exit=False)

        assert se is False
        assert st is None  # Stream handlers return None for status
        assert res == "handled chunk_data"

    @pytest.mark.anyio
    async def test_stream_handler_cancelled_should_exit_true(self):
        """Cancelled exception: should_exit=True, status=CANCELLED."""

        async def handler(ev, ct, ch, **kw):
            raise MyCancelled("stream cancelled")

        registry = HookRegistry(stream_handlers={"test": handler})
        res, se, st = await registry.handle_streaming_chunk("test", "chunk_data", exit=False)

        assert se is True
        assert st == EventStatus.CANCELLED
        assert isinstance(res, tuple) and len(res) == 2

    @pytest.mark.anyio
    async def test_stream_handler_other_exception_aborted_respects_exit_policy(
        self,
    ):
        """Other exception: should_exit=exit, status=ABORTED."""

        async def handler(ev, ct, ch, **kw):
            raise RuntimeError("stream error")

        registry = HookRegistry(stream_handlers={"test": handler})

        # exit=False -> should_exit=False, status=ABORTED
        res, se, st = await registry.handle_streaming_chunk("test", "chunk_data", exit=False)
        assert se is False
        assert st == EventStatus.ABORTED
        assert isinstance(res, RuntimeError)

        # exit=True -> should_exit=True, status=ABORTED
        res, se, st = await registry.handle_streaming_chunk("test", "chunk_data", exit=True)
        assert se is True
        assert st == EventStatus.ABORTED
        assert isinstance(res, RuntimeError)


class TestMatrixParameterized:
    """Parameterized tests for the complete status/exit matrix."""

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "hook_type",
        [
            HookEventTypes.PreEventCreate,
            HookEventTypes.PreInvocation,
            HookEventTypes.PostInvocation,
        ],
    )
    async def test_cancelled_always_exits_true(self, hook_type):
        """Cancelled exceptions always set should_exit=True regardless of exit policy."""

        async def hook(*args, **kw):
            raise MyCancelled("test cancellation")

        registry = HookRegistry(hooks={hook_type: hook})

        # Test with exit=False
        if hook_type == HookEventTypes.PreEventCreate:
            event_like = FakeEventType
        else:
            event_like = FakeEvent()

        (res, se, st), _ = await registry.call(event_like, hook_type=hook_type, exit=False)
        assert se is True
        assert st == EventStatus.CANCELLED

        # Test with exit=True
        (res, se, st), _ = await registry.call(event_like, hook_type=hook_type, exit=True)
        assert se is True
        assert st == EventStatus.CANCELLED

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "hook_type,expected_status",
        [
            (HookEventTypes.PreEventCreate, EventStatus.CANCELLED),
            (HookEventTypes.PreInvocation, EventStatus.CANCELLED),
            (HookEventTypes.PostInvocation, EventStatus.ABORTED),
        ],
    )
    async def test_non_cancelled_exceptions_respect_exit_policy(self, hook_type, expected_status):
        """Non-cancelled exceptions respect the exit policy and use correct status."""

        async def hook(*args, **kw):
            raise RuntimeError("test error")

        registry = HookRegistry(hooks={hook_type: hook})

        if hook_type == HookEventTypes.PreEventCreate:
            event_like = FakeEventType
        else:
            event_like = FakeEvent()

        # Test with exit=False -> should_exit=False
        (res, se, st), _ = await registry.call(event_like, hook_type=hook_type, exit=False)
        assert se is False
        assert st == expected_status

        # Test with exit=True -> should_exit=True
        (res, se, st), _ = await registry.call(event_like, hook_type=hook_type, exit=True)
        assert se is True
        assert st == expected_status

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "hook_type",
        [
            HookEventTypes.PreEventCreate,
            HookEventTypes.PreInvocation,
            HookEventTypes.PostInvocation,
        ],
    )
    async def test_normal_completion_always_no_exit(self, hook_type):
        """Normal completion always sets should_exit=False and status=COMPLETED."""

        async def hook(*args, **kw):
            return "success"

        registry = HookRegistry(hooks={hook_type: hook})

        if hook_type == HookEventTypes.PreEventCreate:
            event_like = FakeEventType
        else:
            event_like = FakeEvent()

        # Test with different exit policies - normal completion ignores exit
        for exit_val in [False, True]:
            (res, se, st), _ = await registry.call(event_like, hook_type=hook_type, exit=exit_val)
            assert se is False
            assert st == EventStatus.COMPLETED
            assert res == "success"
