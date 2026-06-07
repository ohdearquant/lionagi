# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test HookEvent behavior including timeout, cancellation, and exit policy."""

import pytest

from lionagi.protocols.types import EventStatus
from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.hooks.hook_event import HookEvent
from lionagi.service.hooks.hook_registry import HookRegistry
from tests.service.hooks.conftest import FakeEvent, MyCancelled


class TestHookEventBasicBehavior:
    """Test basic HookEvent behavior and state management."""

    @pytest.mark.anyio
    async def test_normal_hook_execution_sets_correct_state(self, patch_cancellation):
        """Test that normal hook execution sets correct execution state."""

        async def successful_hook(ev, **kw):
            return "hook_result"

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: successful_hook})
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=30,
            params={"custom": "param"},
            event_like=FakeEvent("test", 42.0),
        )

        await hook_event.invoke()

        # Check execution state
        assert hook_event.execution.status == EventStatus.COMPLETED
        assert hook_event.execution.response == "hook_result"
        assert hook_event.execution.error is None
        assert hook_event.execution.duration >= 0

        # Check hook-specific state
        assert hook_event._should_exit is False
        assert hook_event._exit_cause is None

        # Check associated event info
        assert hook_event.associated_event_info is not None
        assert (
            hook_event.associated_event_info["lion_class"]
            == "tests.service.hooks.conftest.FakeEvent"
        )
        assert hook_event.associated_event_info["event_id"] == "test"
        assert hook_event.associated_event_info["event_created_at"] == 42.0

    @pytest.mark.anyio
    async def test_hook_exception_sets_error_state(self, patch_cancellation):
        """Test that hook exceptions set proper error state."""

        async def failing_hook(ev, **kw):
            raise RuntimeError("hook failed")

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: failing_hook})
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=30,
            params={},
            event_like=FakeEvent(),
        )

        await hook_event.invoke()

        # Check that hook exception is recorded
        assert hook_event.execution.status == EventStatus.CANCELLED  # From registry
        assert hook_event.execution.response is None
        assert "hook failed" in hook_event.execution.error
        assert hook_event._exit_cause is not None
        assert isinstance(hook_event._exit_cause, RuntimeError)

        # Check exit behavior - should respect exit policy (False in this case)
        assert hook_event._should_exit is False  # exit=False, so should not exit

    @pytest.mark.anyio
    async def test_hook_exception_with_exit_true_sets_should_exit(self, patch_cancellation):
        """Test that hook exceptions with exit=True set should_exit=True."""

        async def failing_hook(ev, **kw):
            raise RuntimeError("hook failed")

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: failing_hook})
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=True,  # This should make it exit on error
            timeout=30,
            params={},
            event_like=FakeEvent(),
        )

        await hook_event.invoke()

        # Should exit because exit=True and hook failed
        assert hook_event._should_exit is True

    @pytest.mark.anyio
    async def test_hook_returns_exception_object(self, patch_cancellation):
        """Test handling when hook returns an Exception object."""
        test_exception = RuntimeError("returned exception")

        async def hook_returning_exception(ev, **kw):
            return test_exception

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: hook_returning_exception})
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=30,
            params={},
            event_like=FakeEvent(),
        )

        await hook_event.invoke()

        # Hook event should detect returned exception and set error state
        assert hook_event.execution.response is None
        assert hook_event.execution.error == "returned exception"
        assert hook_event._exit_cause is test_exception

    @pytest.mark.anyio
    async def test_hook_returns_tuple_with_exception(self, patch_cancellation):
        """Test handling when hook returns a tuple containing an exception."""
        test_exception = RuntimeError("tuple exception")

        async def hook_returning_tuple(ev, **kw):
            return ("UNDEFINED", test_exception)

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: hook_returning_tuple})
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=30,
            params={},
            event_like=FakeEvent(),
        )

        # Should handle the tuple exception and set error state
        await hook_event.invoke()

        assert hook_event.execution.status == EventStatus.FAILED
        assert hook_event.execution.error == "tuple exception"
        assert hook_event._exit_cause is test_exception


class TestHookEventCancellation:
    """Test HookEvent cancellation and timeout behavior."""

    @pytest.mark.anyio
    async def test_cancellation_propagates(self, patch_cancellation):
        """Test that cancellation exceptions propagate correctly."""

        async def cancelling_hook(ev, **kw):
            raise MyCancelled("test cancellation")

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: cancelling_hook})
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=30,
            params={},
            event_like=FakeEvent(),
        )

        # Cancellation should propagate out of invoke()
        with pytest.raises(MyCancelled, match="test cancellation"):
            await hook_event.invoke()

    @pytest.mark.anyio
    async def test_timeout_cancellation(self, patch_cancellation, patch_timeout):
        """Test that timeouts cause cancellation."""

        async def slow_hook(ev, **kw):
            # This won't actually run because patch_timeout immediately raises
            return "should not reach this"

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: slow_hook})
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=1,  # Short timeout in seconds
            params={},
            event_like=FakeEvent(),
        )

        # Should raise cancellation due to timeout
        with pytest.raises(MyCancelled, match="Timeout"):
            await hook_event.invoke()


class TestHookEventDispatchErrorPolicy:
    """Test HookEvent behavior on registry/dispatch errors."""

    @pytest.mark.anyio
    async def test_dispatch_error_respects_exit_policy_false(self, patch_cancellation):
        """Test that dispatch errors respect exit=False policy."""
        # Create registry without the hook to cause dispatch error
        registry = HookRegistry()
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,  # Should not exit on dispatch error
            timeout=30,
            params={},
            event_like=FakeEvent(),
        )

        await hook_event.invoke()

        # Should not exit because exit=False
        assert hook_event._should_exit is False
        assert hook_event.execution.status == EventStatus.CANCELLED  # Dispatch errors are CANCELLED
        assert hook_event._exit_cause is not None

    @pytest.mark.anyio
    async def test_dispatch_error_respects_exit_policy_true(self, patch_cancellation):
        """Test that dispatch errors respect exit=True policy."""
        # Create registry without the hook to cause dispatch error
        registry = HookRegistry()
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=True,  # Should exit on dispatch error
            timeout=30,
            params={},
            event_like=FakeEvent(),
        )

        await hook_event.invoke()

        # Should exit because exit=True
        assert hook_event._should_exit is True
        assert hook_event.execution.status == EventStatus.CANCELLED  # Dispatch errors are CANCELLED
        assert hook_event._exit_cause is not None


class TestHookEventMetadata:
    """Test HookEvent metadata handling."""

    @pytest.mark.anyio
    async def test_metadata_populated_correctly(self, patch_cancellation):
        """Test that associated_event_info is populated correctly."""

        async def dummy_hook(ev, **kw):
            return "ok"

        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: dummy_hook})
        event = FakeEvent("meta_test", 999.5)
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PostInvocation,
            exit=False,
            timeout=30,
            params={},
            event_like=event,
        )

        await hook_event.invoke()

        # Check metadata structure
        info = hook_event.associated_event_info
        assert info is not None
        assert isinstance(info, dict)  # Should be AssociatedEventInfo

        # Check specific fields
        assert info["lion_class"] == "tests.service.hooks.conftest.FakeEvent"
        assert info["event_id"] == "meta_test"
        assert info["event_created_at"] == 999.5

    @pytest.mark.anyio
    async def test_metadata_for_pre_event_create_type(self, patch_cancellation):
        """Test metadata for pre_event_create (event type, not instance)."""

        async def dummy_hook(ev_type, **kw):
            return "ok"

        from tests.service.hooks.conftest import FakeEventType

        registry = HookRegistry(hooks={HookEventTypes.PreEventCreate: dummy_hook})
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreEventCreate,
            exit=False,
            timeout=30,
            params={},
            event_like=FakeEventType,
        )

        await hook_event.invoke()

        # Pre event create should only have lion_class
        info = hook_event.associated_event_info
        assert info["lion_class"] == "tests.service.hooks.conftest.FakeEventType"
        assert len(info) == 1  # Only lion_class for event types


class TestHookEventValidation:
    """Test HookEvent validation and edge cases."""

    def test_exit_validation_converts_none_to_false(self):
        """Test that exit field validation converts None to False."""
        registry = HookRegistry()

        # Test with None
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=None,
            timeout=30,
            params={},
            event_like=FakeEvent(),
        )
        assert hook_event.exit is False

        # Test with explicit False
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=30,
            params={},
            event_like=FakeEvent(),
        )
        assert hook_event.exit is False

        # Test with True
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=True,
            timeout=30,
            params={},
            event_like=FakeEvent(),
        )
        assert hook_event.exit is True

    @pytest.mark.anyio
    async def test_params_forwarded_to_hook(self, patch_cancellation):
        """Test that params are correctly forwarded to hooks."""
        captured_params = {}

        async def param_capturing_hook(ev, **kw):
            captured_params.update(kw)
            return "ok"

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: param_capturing_hook})
        hook_event = HookEvent(
            registry=registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=30,
            params={
                "custom_param": "test_value",
                "another_param": 42,
            },
            event_like=FakeEvent(),
        )

        await hook_event.invoke()

        # Check that custom params were forwarded
        assert captured_params["custom_param"] == "test_value"
        assert captured_params["another_param"] == 42
        assert captured_params["exit"] is False  # exit should also be forwarded
