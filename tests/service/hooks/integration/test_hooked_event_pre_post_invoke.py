# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test HookedEvent integration with pre/post hooks."""

from typing import Any

import pytest
from pydantic import ConfigDict, Field

from lionagi.protocols.types import EventStatus
from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.hooks.hook_registry import HookRegistry
from lionagi.service.hooks.hooked_event import HookedEvent
from tests.service.hooks.conftest import MyCancelled


class MockHookedEvent(HookedEvent):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    invoke_result: Any = Field(default="test_invoke_result")
    invoke_error: Exception | None = Field(default=None)
    invoke_called: bool = Field(default=False)

    async def _core_invoke(self):
        self.invoke_called = True
        if self.invoke_error:
            raise self.invoke_error
        return self.invoke_result

    async def _core_stream(self):
        yield "test_chunk"


class TestHookedEventPreHookIntegration:
    @pytest.mark.anyio
    async def test_pre_hook_normal_allows_invoke(self, patch_cancellation, patch_logger):
        async def pre_hook(ev, **kw):
            return "pre_ok"

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: pre_hook})
        event = MockHookedEvent(invoke_result="main_result")
        event.create_pre_invoke_hook(hook_registry=registry, exit_hook=False)

        await event.invoke()

        # Pre-hook should have run and allowed main invoke
        assert event.invoke_called is True
        assert event.execution.status == EventStatus.COMPLETED
        assert event.execution.response == "main_result"
        assert event.execution.error is None

        # Logger should have been called once for the pre-hook
        assert len(patch_logger) == 1

    @pytest.mark.anyio
    async def test_pre_hook_exit_aborts_invoke_and_propagates(
        self, patch_cancellation, patch_logger
    ):
        """A pre-hook raising Exception (here MyCancelled) is captured as FAILED; the
        action is still blocked (core never runs). Raise BaseException to hard-abort."""

        async def pre_hook(ev, **kw):
            raise MyCancelled("pre-hook denied")

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: pre_hook})
        event = MockHookedEvent(invoke_result="SHOULD_NOT_HAPPEN")
        event.create_pre_invoke_hook(hook_registry=registry, exit_hook=True)

        await event.invoke()  # total: hook Exception captured as FAILED, not raised

        # Main _core_invoke() should NOT have been called
        assert event.invoke_called is False
        assert event.execution.status == EventStatus.FAILED
        assert isinstance(event.execution.error, MyCancelled)

    @pytest.mark.anyio
    async def test_pre_hook_error_aborts_invoke_regardless_of_exit_flag(
        self, patch_cancellation, patch_logger
    ):
        """A failing pre-hook (RuntimeError, exit=False) aborts _core_invoke; the error is
        captured as FAILED, not re-raised."""

        async def pre_hook(ev, **kw):
            raise RuntimeError("pre-hook error")

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: pre_hook})
        event = MockHookedEvent(invoke_result="main_result")
        event.create_pre_invoke_hook(hook_registry=registry, exit_hook=False)

        await event.invoke()  # total: hook error captured as FAILED, not raised

        # _core_invoke is not reached because the pre-hook failed
        assert event.invoke_called is False
        assert event.execution.status == EventStatus.FAILED

    @pytest.mark.anyio
    async def test_pre_hook_error_with_exit_true_aborts(self, patch_cancellation, patch_logger):
        """A failing pre-hook (RuntimeError, exit=True) aborts execution; captured as FAILED."""

        async def pre_hook(ev, **kw):
            raise RuntimeError("pre-hook critical error")

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: pre_hook})
        event = MockHookedEvent(invoke_result="SHOULD_NOT_HAPPEN")
        event.create_pre_invoke_hook(hook_registry=registry, exit_hook=True)

        await event.invoke()  # total: hook error captured as FAILED, not raised

        assert event.invoke_called is False
        assert event.execution.status == EventStatus.FAILED


class TestHookedEventPostHookIntegration:
    @pytest.mark.anyio
    async def test_post_hook_normal_completion(self, patch_cancellation, patch_logger):
        async def post_hook(ev, **kw):
            return "post_logged"

        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: post_hook})
        event = MockHookedEvent(invoke_result="main_result")
        event.create_post_invoke_hook(hook_registry=registry, exit_hook=False)

        await event.invoke()

        # Both main invoke and post-hook should have run
        assert event.invoke_called is True
        assert event.execution.status == EventStatus.COMPLETED
        assert event.execution.response == "main_result"

        # Post-hook should have been logged once
        assert len(patch_logger) == 1

    @pytest.mark.anyio
    async def test_post_hook_cancellation_propagates_after_main(
        self, patch_cancellation, patch_logger
    ):
        """A post-hook raising Exception (exit=True) after main ran is captured as FAILED."""

        async def post_hook(ev, **kw):
            raise MyCancelled("post-hook failed")

        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: post_hook})
        event = MockHookedEvent(invoke_result="main_result")
        event.create_post_invoke_hook(hook_registry=registry, exit_hook=True)

        await event.invoke()  # total: post-hook Exception captured as FAILED, not raised

        # Main invoke DID run; the post-hook failure is recorded as FAILED
        assert event.invoke_called is True
        assert event.execution.status == EventStatus.FAILED

    @pytest.mark.anyio
    async def test_post_hook_error_with_exit_false_keeps_result(
        self, patch_cancellation, patch_logger
    ):
        """Test that post-hook error with exit=False keeps main result."""

        async def post_hook(ev, **kw):
            raise RuntimeError("post-hook error")

        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: post_hook})
        event = MockHookedEvent(invoke_result="main_result")
        event.create_post_invoke_hook(hook_registry=registry, exit_hook=False)

        await event.invoke()

        # Main result should be preserved because exit_hook=False
        assert event.invoke_called is True
        assert event.execution.status == EventStatus.COMPLETED
        assert event.execution.response == "main_result"

        # Post-hook should have been logged
        assert len(patch_logger) == 1


class TestHookedEventBothHooks:
    @pytest.mark.anyio
    async def test_both_hooks_normal_execution_order(self, patch_cancellation, patch_logger):
        execution_order = []

        async def pre_hook(ev, **kw):
            execution_order.append("pre")
            return "pre_ok"

        async def post_hook(ev, **kw):
            execution_order.append("post")
            return "post_ok"

        class OrderTestEvent(MockHookedEvent):
            async def _core_invoke(self):
                execution_order.append("main")
                return await super()._core_invoke()

        registry = HookRegistry(
            hooks={
                HookEventTypes.PreInvocation: pre_hook,
                HookEventTypes.PostInvocation: post_hook,
            }
        )
        event = OrderTestEvent(invoke_result="main_result")
        event.create_pre_invoke_hook(hook_registry=registry, exit_hook=False)
        event.create_post_invoke_hook(hook_registry=registry, exit_hook=False)

        await event.invoke()

        # Check execution order
        assert execution_order == ["pre", "main", "post"]
        assert event.execution.status == EventStatus.COMPLETED
        assert event.execution.response == "main_result"

        # Both hooks should have been logged
        assert len(patch_logger) == 2

    @pytest.mark.anyio
    async def test_pre_hook_exit_prevents_post_hook(self, patch_cancellation, patch_logger):
        """A pre-hook exit (Exception) is captured as FAILED; neither core nor post hook runs."""
        hooks_called = []

        async def pre_hook(ev, **kw):
            hooks_called.append("pre")
            raise MyCancelled("pre exit")

        async def post_hook(ev, **kw):
            hooks_called.append("post")
            return "post_ok"

        registry = HookRegistry(
            hooks={
                HookEventTypes.PreInvocation: pre_hook,
                HookEventTypes.PostInvocation: post_hook,
            }
        )
        event = MockHookedEvent(invoke_result="SHOULD_NOT_HAPPEN")
        event.create_pre_invoke_hook(hook_registry=registry, exit_hook=True)
        event.create_post_invoke_hook(hook_registry=registry, exit_hook=False)

        await event.invoke()  # total: pre-hook Exception captured as FAILED, not raised

        # Only pre-hook ran; post-hook and core never reached
        assert hooks_called == ["pre"]
        assert event.invoke_called is False
        assert event.execution.status == EventStatus.FAILED

    @pytest.mark.anyio
    async def test_main_invoke_error_still_runs_post_hook(self, patch_cancellation, patch_logger):
        hooks_called = []

        async def pre_hook(ev, **kw):
            hooks_called.append("pre")
            return "pre_ok"

        async def post_hook(ev, **kw):
            hooks_called.append("post")
            return "post_ok"

        registry = HookRegistry(
            hooks={
                HookEventTypes.PreInvocation: pre_hook,
                HookEventTypes.PostInvocation: post_hook,
            }
        )
        event = MockHookedEvent(invoke_error=RuntimeError("main invoke failed"))
        event.create_pre_invoke_hook(hook_registry=registry, exit_hook=False)
        event.create_post_invoke_hook(hook_registry=registry, exit_hook=False)

        # core error is captured as FAILED after the post-hook runs (post runs even on core error)
        await event.invoke()  # total: core error captured as FAILED, not raised

        # Both hooks ran despite the core error
        assert hooks_called == ["pre", "post"]
        assert event.invoke_called is True
        assert event.execution.status == EventStatus.FAILED

        # Both hooks should have been logged
        assert len(patch_logger) == 2


class TestHookedEventParameterForwarding:
    @pytest.mark.anyio
    async def test_hook_params_forwarded_to_hook(self, patch_cancellation, patch_logger):
        captured_params = {}

        async def param_hook(ev, **kw):
            captured_params.update(kw)
            return "ok"

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: param_hook})
        event = MockHookedEvent()
        event.create_pre_invoke_hook(
            hook_registry=registry,
            exit_hook=False,
            hook_timeout=60.0,
            hook_params={"custom": "value", "number": 42},
        )

        await event.invoke()

        # Check that custom params were forwarded
        assert captured_params["custom"] == "value"
        assert captured_params["number"] == 42
        assert captured_params["exit"] is False

    @pytest.mark.anyio
    async def test_hook_timeout_configuration(self, patch_cancellation):
        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: lambda ev, **kw: "ok"})
        event = MockHookedEvent()
        event.create_pre_invoke_hook(hook_registry=registry, exit_hook=True, hook_timeout=120.0)

        # Check that the hook event was configured with correct timeout
        assert event._pre_invoke_hook_event.timeout == 120.0
        assert event._pre_invoke_hook_event.exit is True

    @pytest.mark.anyio
    async def test_hook_creation_defaults(self, patch_cancellation):
        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: lambda ev, **kw: "ok"})
        event = MockHookedEvent()
        event.create_post_invoke_hook(hook_registry=registry)

        # Check defaults
        hook_event = event._post_invoke_hook_event
        assert hook_event.exit is False  # Default exit_hook=None -> False
        assert hook_event.timeout == 30.0  # Default timeout
        assert hook_event.params == {}  # Default empty params


class TestHookedEventCancellationPropagation:
    @pytest.mark.anyio
    async def test_main_invoke_error_captured(self, patch_cancellation, patch_logger):
        """Exceptions in _core_invoke() are captured as FAILED state, not propagated."""
        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: lambda ev, **kw: "post"})
        # MyCancelled extends Exception; Event.invoke() catches it → FAILED (no re-raise)
        event = MockHookedEvent(invoke_error=MyCancelled("main cancelled"))
        event.create_post_invoke_hook(hook_registry=registry, exit_hook=False)

        await event.invoke()  # total: Exception captured as FAILED, not raised

        # Event.invoke() catches Exception subclasses → FAILED (not CANCELLED)
        assert event.execution.status == EventStatus.FAILED
        # execution.error holds the exception object
        assert isinstance(event.execution.error, MyCancelled)
        assert "main cancelled" in str(event.execution.error)

        # Post hook DOES run even on core error (design intent: post-hook runs always)
        assert len(patch_logger) == 1
