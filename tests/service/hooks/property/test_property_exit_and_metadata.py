# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Property-based tests for hook exit propagation and metadata invariants."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lionagi.protocols.types import EventStatus
from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.hooks.hook_registry import HookRegistry
from tests.service.hooks.conftest import MyCancelled


@pytest.fixture(autouse=True)
def patch_cancel(monkeypatch):
    """Auto-patch cancellation class for all tests in this module."""
    from lionagi.service.hooks import hook_registry

    monkeypatch.setattr(hook_registry, "get_cancelled_exc_class", lambda: MyCancelled)


class FakeEventProperty:
    """Property test event with arbitrary id and created_at."""

    def __init__(self, event_id, created_at):
        self.id = event_id
        self.created_at = created_at

    @classmethod
    def class_name(cls, full: bool = False):
        return f"{cls.__module__}.{cls.__name__}" if full else cls.__name__


class FakeEventTypeProperty:
    """Property test event type."""

    @classmethod
    def class_name(cls, full: bool = False):
        return f"{cls.__module__}.{cls.__name__}" if full else cls.__name__


class TestExitPropagationInvariants:
    """Property-based tests for exit propagation invariants."""

    @given(
        exit_policy=st.booleans(),
        hook_type=st.sampled_from(
            [
                HookEventTypes.PreEventCreate,
                HookEventTypes.PreInvocation,
                HookEventTypes.PostInvocation,
            ]
        ),
    )
    @pytest.mark.anyio
    async def test_normal_completion_always_no_exit(self, exit_policy, hook_type):
        """Property: Normal completion always sets should_exit=False regardless of exit policy."""

        async def normal_hook(*args, **kw):
            return "success"

        registry = HookRegistry(hooks={hook_type: normal_hook})

        if hook_type == HookEventTypes.PreEventCreate:
            event_like = FakeEventTypeProperty
        else:
            event_like = FakeEventProperty("test", 42.0)

        (res, should_exit, status), _ = await registry.call(
            event_like, hook_type=hook_type, exit=exit_policy
        )

        # Invariant: Normal completion always no exit
        assert should_exit is False
        assert status == EventStatus.COMPLETED
        assert res == "success"

    @given(
        exit_policy=st.booleans(),
        hook_type=st.sampled_from(
            [
                HookEventTypes.PreEventCreate,
                HookEventTypes.PreInvocation,
                HookEventTypes.PostInvocation,
            ]
        ),
    )
    @pytest.mark.anyio
    async def test_cancellation_always_exits_true(self, exit_policy, hook_type):
        """Property: Cancellation always sets should_exit=True regardless of exit policy."""

        async def cancelling_hook(*args, **kw):
            raise MyCancelled("test cancellation")

        registry = HookRegistry(hooks={hook_type: cancelling_hook})

        if hook_type == HookEventTypes.PreEventCreate:
            event_like = FakeEventTypeProperty
        else:
            event_like = FakeEventProperty("test", 42.0)

        (res, should_exit, status), _ = await registry.call(
            event_like, hook_type=hook_type, exit=exit_policy
        )

        # Invariant: Cancellation always exits true
        assert should_exit is True
        assert status == EventStatus.CANCELLED

    @given(
        exit_policy=st.booleans(),
        hook_type=st.sampled_from(
            [
                HookEventTypes.PreEventCreate,
                HookEventTypes.PreInvocation,
            ]
        ),
    )
    @pytest.mark.anyio
    async def test_pre_hook_exceptions_respect_exit_policy(self, exit_policy, hook_type):
        """Property: Pre-hook non-cancellation exceptions respect exit policy."""

        async def failing_hook(*args, **kw):
            raise RuntimeError("test error")

        registry = HookRegistry(hooks={hook_type: failing_hook})

        if hook_type == HookEventTypes.PreEventCreate:
            event_like = FakeEventTypeProperty
        else:
            event_like = FakeEventProperty("test", 42.0)

        (res, should_exit, status), _ = await registry.call(
            event_like, hook_type=hook_type, exit=exit_policy
        )

        # Invariant: Non-cancellation exceptions respect exit policy
        assert should_exit == exit_policy
        assert status == EventStatus.CANCELLED  # Pre-hooks use CANCELLED

    @given(exit_policy=st.booleans())
    @pytest.mark.anyio
    async def test_post_hook_exceptions_respect_exit_policy(self, exit_policy):
        """Property: Post-hook non-cancellation exceptions respect exit policy and use ABORTED."""

        async def failing_hook(*args, **kw):
            raise RuntimeError("test error")

        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: failing_hook})
        event_like = FakeEventProperty("test", 42.0)

        (res, should_exit, status), _ = await registry.call(
            event_like,
            hook_type=HookEventTypes.PostInvocation,
            exit=exit_policy,
        )

        # Invariant: Post-hook exceptions respect exit policy and use ABORTED
        assert should_exit == exit_policy
        assert status == EventStatus.ABORTED  # Post-hooks use ABORTED

    @given(
        exit_policy=st.booleans(),
        chunk_type=st.text(min_size=1, max_size=20).filter(lambda x: x.strip()),
    )
    @pytest.mark.anyio
    async def test_stream_handler_exceptions_respect_exit_policy(self, exit_policy, chunk_type):
        """Property: Stream handler exceptions respect exit policy."""

        async def failing_handler(*args, **kw):
            raise RuntimeError("stream error")

        registry = HookRegistry(stream_handlers={chunk_type: failing_handler})

        res, should_exit, status = await registry.handle_streaming_chunk(
            chunk_type, "test_chunk", exit=exit_policy
        )

        # Invariant: Stream handler exceptions respect exit policy
        assert should_exit == exit_policy
        assert status == EventStatus.ABORTED


class TestMetadataInvariants:
    """Property-based tests for metadata invariants."""

    @given(
        event_id=st.text(min_size=1, max_size=50),
        created_at=st.floats(
            min_value=0.0,
            max_value=1e10,
            allow_nan=False,
            allow_infinity=False,
        ),
        hook_type=st.sampled_from(
            [
                HookEventTypes.PreInvocation,
                HookEventTypes.PostInvocation,
            ]
        ),
    )
    @pytest.mark.anyio
    async def test_metadata_mirrors_event_attributes(self, event_id, created_at, hook_type):
        """Property: Metadata exactly mirrors event attributes for invocation hooks."""

        async def dummy_hook(*args, **kw):
            return "ok"

        registry = HookRegistry(hooks={hook_type: dummy_hook})
        event = FakeEventProperty(event_id, created_at)

        (res, should_exit, status), meta = await registry.call(event, hook_type=hook_type)

        # Invariant: Metadata mirrors event attributes exactly
        assert meta["lion_class"] == "test_property_exit_and_metadata.FakeEventProperty"
        assert meta["event_id"] == str(event_id)  # Should be stringified
        assert meta["event_created_at"] == created_at
        assert len(meta) == 3  # Exactly these three fields

    @pytest.mark.anyio
    async def test_pre_event_create_metadata_has_only_lion_class(self):
        """Property: PreEventCreate metadata only has lion_class."""

        async def dummy_hook(*args, **kw):
            return "ok"

        registry = HookRegistry(hooks={HookEventTypes.PreEventCreate: dummy_hook})

        (res, should_exit, status), meta = await registry.call(
            FakeEventTypeProperty, hook_type=HookEventTypes.PreEventCreate
        )

        # Invariant: PreEventCreate only has lion_class
        assert meta["lion_class"] == "test_property_exit_and_metadata.FakeEventTypeProperty"
        assert len(meta) == 1  # Only lion_class

    @given(
        event_id=st.text(min_size=1, max_size=50),
        created_at=st.floats(
            min_value=0.0,
            max_value=1e10,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    @pytest.mark.anyio
    async def test_metadata_consistency_across_hook_types(self, event_id, created_at):
        """Property: Metadata is consistent across different hook types for same event."""

        async def dummy_hook(*args, **kw):
            return "ok"

        registry = HookRegistry(
            hooks={
                HookEventTypes.PreInvocation: dummy_hook,
                HookEventTypes.PostInvocation: dummy_hook,
            }
        )
        event = FakeEventProperty(event_id, created_at)

        # Get metadata from both hook types
        (_, _, _), pre_meta = await registry.call(event, hook_type=HookEventTypes.PreInvocation)
        (_, _, _), post_meta = await registry.call(event, hook_type=HookEventTypes.PostInvocation)

        # Invariant: Metadata should be identical for same event
        assert pre_meta == post_meta


class TestSyncAsyncVariability:
    """Property-based tests for sync/async handler equivalence."""

    @given(
        exit_policy=st.booleans(),
        return_value=st.text(min_size=1, max_size=20),
    )
    @pytest.mark.anyio
    async def test_sync_async_hooks_equivalent_results(self, exit_policy, return_value):
        """Property: Sync and async hooks produce identical results."""

        def sync_hook(*args, **kw):
            return return_value

        async def async_hook(*args, **kw):
            return return_value

        sync_registry = HookRegistry(hooks={HookEventTypes.PreInvocation: sync_hook})
        async_registry = HookRegistry(hooks={HookEventTypes.PreInvocation: async_hook})

        event = FakeEventProperty("test", 42.0)

        # Get results from both registries
        sync_result, sync_meta = await sync_registry.call(
            event, hook_type=HookEventTypes.PreInvocation, exit=exit_policy
        )
        async_result, async_meta = await async_registry.call(
            event, hook_type=HookEventTypes.PreInvocation, exit=exit_policy
        )

        # Invariant: Results should be identical
        assert sync_result == async_result
        assert sync_meta == async_meta

    @given(
        exit_policy=st.booleans(),
        chunk_type=st.text(min_size=1, max_size=20).filter(lambda x: x.strip()),
        chunk_data=st.text(min_size=0, max_size=50),
    )
    @pytest.mark.anyio
    async def test_sync_async_stream_handlers_equivalent(self, exit_policy, chunk_type, chunk_data):
        """Property: Sync and async stream handlers produce identical results."""

        def sync_handler(ev, ct, ch, **kw):
            return f"sync:{ct}:{ch}"

        async def async_handler(ev, ct, ch, **kw):
            return f"async:{ct}:{ch}"

        sync_registry = HookRegistry(stream_handlers={chunk_type: sync_handler})
        async_registry = HookRegistry(stream_handlers={chunk_type: async_handler})

        # Get results from both registries
        sync_res, sync_se, sync_st = await sync_registry.handle_streaming_chunk(
            chunk_type, chunk_data, exit=exit_policy
        )
        async_res, async_se, async_st = await async_registry.handle_streaming_chunk(
            chunk_type, chunk_data, exit=exit_policy
        )

        # Results should have same structure but different content
        assert sync_se == async_se
        assert sync_st == async_st
        assert sync_res == f"sync:{chunk_type}:{chunk_data}"
        assert async_res == f"async:{chunk_type}:{chunk_data}"


class TestExceptionVariability:
    """Property-based tests for exception handling robustness."""

    @given(
        error_message=st.text(min_size=1, max_size=100),
        exit_policy=st.booleans(),
        hook_type=st.sampled_from(
            [
                HookEventTypes.PreEventCreate,
                HookEventTypes.PreInvocation,
                HookEventTypes.PostInvocation,
            ]
        ),
    )
    @pytest.mark.anyio
    async def test_exception_message_preserved(self, error_message, exit_policy, hook_type):
        """Property: Exception messages are preserved in results."""

        async def failing_hook(*args, **kw):
            raise RuntimeError(error_message)

        registry = HookRegistry(hooks={hook_type: failing_hook})

        if hook_type == HookEventTypes.PreEventCreate:
            event_like = FakeEventTypeProperty
        else:
            event_like = FakeEventProperty("test", 42.0)

        (res, should_exit, status), _ = await registry.call(
            event_like, hook_type=hook_type, exit=exit_policy
        )

        # Invariant: Exception should be preserved in result
        assert isinstance(res, RuntimeError)
        assert str(res) == error_message

    @given(
        custom_params=st.dictionaries(
            keys=st.text(
                min_size=1,
                max_size=20,
                alphabet=st.characters(min_codepoint=97, max_codepoint=122),
            ).filter(lambda x: x not in {"exit", "hook_type", "chunk_type", "chunk"}),
            values=st.one_of(
                st.text(max_size=50),
                st.integers(),
                st.floats(allow_nan=False, allow_infinity=False),
            ),
            min_size=0,
            max_size=10,
        ),
        exit_policy=st.booleans(),
    )
    @pytest.mark.anyio
    async def test_custom_parameters_forwarded(self, custom_params, exit_policy):
        """Property: Custom parameters are correctly forwarded to hooks."""
        captured_params = {}

        async def param_capturing_hook(*args, **kw):
            captured_params.update(kw)
            return "ok"

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: param_capturing_hook})
        event = FakeEventProperty("test", 42.0)

        await registry.call(
            event,
            hook_type=HookEventTypes.PreInvocation,
            exit=exit_policy,
            **custom_params,
        )

        # Invariant: All custom params should be forwarded
        for key, value in custom_params.items():
            assert captured_params[key] == value
        assert captured_params["exit"] == exit_policy
