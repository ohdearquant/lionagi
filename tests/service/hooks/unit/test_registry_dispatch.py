# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test registry dispatch and argument forwarding - includes regression guards."""

import pytest

from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.hooks.hook_registry import HookRegistry
from tests.service.hooks.conftest import FakeEvent, FakeEventType


class TestRegistrySelection:
    @pytest.mark.anyio
    async def test_call_fails_when_no_hook_or_chunk_type(self):
        """call() raises when both hook_type and chunk_type are None."""
        registry = HookRegistry()
        with pytest.raises(ValueError, match="Either method or chunk_type must be provided"):
            await registry.call(FakeEvent())

    @pytest.mark.anyio
    async def test_internal_call_fails_when_both_none(self):
        registry = HookRegistry()
        with pytest.raises(
            RuntimeError,
            match="Either hook_type or chunk_type must be provided",
        ):
            await registry._call(None, None, None, FakeEvent())

    @pytest.mark.anyio
    async def test_internal_call_fails_when_hook_missing_and_no_chunk_type(
        self,
    ):
        registry = HookRegistry()
        with pytest.raises(
            RuntimeError,
            match="Hook type is required when chunk_type is not provided",
        ):
            await registry._call(HookEventTypes.PreInvocation, None, None, FakeEvent())


class TestArgumentForwardingRegression:
    @pytest.mark.anyio
    async def test_post_invocation_forwards_event_and_exit_and_meta(self):
        """REGRESSION GUARD: PostInvocation must receive event and exit parameters.

        This test catches the original bug where post_invocation was called
        without the event argument, causing TypeError and unexpected should_exit=True.
        """
        captured = {}

        async def post_hook(ev, *, exit=False, **kw):
            captured["got_ev"] = ev
            captured["exit"] = exit
            captured["kw"] = kw
            return "done"

        event = FakeEvent(eid="E123", created_at=42.0)
        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: post_hook})

        (res, se, st), meta = await registry.call(
            event,
            hook_type=HookEventTypes.PostInvocation,
            exit=True,
            custom_param="test_value",
        )

        # Verify event forwarded correctly
        assert captured["got_ev"] is event
        assert captured["got_ev"].id == "E123"

        # Verify exit flag forwarded correctly
        assert captured["exit"] is True

        # Verify custom parameters forwarded
        assert captured["kw"]["custom_param"] == "test_value"

        # Verify normal completion
        assert se is False
        assert st.name == "COMPLETED"

        # CRITICAL: Verify meta conforms to AssociatedEventInfo contract
        assert meta["lion_class"] == "tests.service.hooks.conftest.FakeEvent"
        assert meta["event_id"] == "E123"
        assert meta["event_created_at"] == 42.0

    @pytest.mark.anyio
    async def test_pre_invocation_forwards_event_and_exit_and_meta(self):
        captured = {}

        async def pre_hook(ev, *, exit=False, **kw):
            captured["got_ev"] = ev
            captured["exit"] = exit
            return "pre_done"

        event = FakeEvent(eid="E456", created_at=99.0)
        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: pre_hook})

        (res, se, st), meta = await registry.call(
            event, hook_type=HookEventTypes.PreInvocation, exit=False
        )

        assert captured["got_ev"] is event
        assert captured["exit"] is False
        assert se is False
        assert st.name == "COMPLETED"

        # Check meta for pre_invocation
        assert meta["lion_class"] == "tests.service.hooks.conftest.FakeEvent"
        assert meta["event_id"] == "E456"
        assert meta["event_created_at"] == 99.0

    @pytest.mark.anyio
    async def test_pre_event_create_forwards_event_type_and_exit_and_meta(
        self,
    ):
        captured = {}

        async def pre_create_hook(ev_type, *, exit=False, **kw):
            captured["got_ev_type"] = ev_type
            captured["exit"] = exit
            return FakeEvent("created", 555.0)

        registry = HookRegistry(hooks={HookEventTypes.PreEventCreate: pre_create_hook})

        (res, se, st), meta = await registry.call(
            FakeEventType, hook_type=HookEventTypes.PreEventCreate, exit=True
        )

        assert captured["got_ev_type"] is FakeEventType
        assert captured["exit"] is True
        assert se is False
        assert st.name == "COMPLETED"

        # Check meta for pre_event_create (only has lion_class, no id/created_at)
        assert meta["lion_class"] == "tests.service.hooks.conftest.FakeEventType"
        assert "event_id" not in meta
        assert "event_created_at" not in meta

    @pytest.mark.anyio
    async def test_stream_handler_forwards_exit(self):
        captured = {}

        async def stream_handler(ev, ct, ch, *, exit=False, **kw):
            captured["exit"] = exit
            captured["chunk_type"] = ct
            captured["chunk"] = ch
            return "stream_done"

        registry = HookRegistry(stream_handlers={"test_chunk": stream_handler})

        res, se, st = await registry.call(
            None,  # event_like not used for streaming
            chunk_type="test_chunk",
            chunk="test_data",
            exit=True,
        )

        assert captured["exit"] is True
        assert captured["chunk_type"] == "test_chunk"
        assert captured["chunk"] == "test_data"
        assert se is False
        assert st is None  # streaming handlers return None for status


class TestMetadataContract:
    @pytest.mark.anyio
    async def test_meta_uses_lion_class_not_event_type(self):
        """REGRESSION GUARD: meta uses 'lion_class' key, not the old 'event_type' key."""

        async def dummy_hook(ev, **kw):
            return "ok"

        event = FakeEvent()
        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: dummy_hook})

        (res, se, st), meta = await registry.call(event, hook_type=HookEventTypes.PreInvocation)

        # Must use 'lion_class' to align with AssociatedEventInfo
        assert "lion_class" in meta
        assert "event_type" not in meta  # Old key should not be present

    @pytest.mark.anyio
    async def test_meta_content_for_all_hook_types(self):

        async def dummy_hook(ev, **kw):
            return "ok"

        event = FakeEvent(eid="TEST", created_at=123.456)
        registry = HookRegistry(
            hooks={
                HookEventTypes.PreEventCreate: dummy_hook,
                HookEventTypes.PreInvocation: dummy_hook,
                HookEventTypes.PostInvocation: dummy_hook,
            }
        )

        # Pre event create - only lion_class
        (res, se, st), meta = await registry.call(
            FakeEventType, hook_type=HookEventTypes.PreEventCreate
        )
        assert meta["lion_class"] == "tests.service.hooks.conftest.FakeEventType"
        assert len(meta) == 1  # Only lion_class

        # Pre invocation - all fields
        (res, se, st), meta = await registry.call(event, hook_type=HookEventTypes.PreInvocation)
        assert meta["lion_class"] == "tests.service.hooks.conftest.FakeEvent"
        assert meta["event_id"] == "TEST"
        assert meta["event_created_at"] == 123.456
        assert len(meta) == 3

        # Post invocation - all fields
        (res, se, st), meta = await registry.call(event, hook_type=HookEventTypes.PostInvocation)
        assert meta["lion_class"] == "tests.service.hooks.conftest.FakeEvent"
        assert meta["event_id"] == "TEST"
        assert meta["event_created_at"] == 123.456
        assert len(meta) == 3


class TestDictConstructorStringKeys:
    """HookRegistry constructor must accept string aliases, not only enum keys."""

    def test_constructor_accepts_documented_string_aliases(self):
        def dummy(*args, **kwargs):
            return None

        registry = HookRegistry(
            {
                "pre_invoke": dummy,
                "post_invoke": dummy,
                "pre_event_create": dummy,
            }
        )
        assert registry._hooks[HookEventTypes.PreInvocation] is dummy
        assert registry._hooks[HookEventTypes.PostInvocation] is dummy
        assert registry._hooks[HookEventTypes.PreEventCreate] is dummy

    def test_normalize_key_pre_event_create_without_hook_suffix(self):

        def dummy(*args, **kwargs):
            return None

        registry = HookRegistry({"pre_event_create": dummy})
        assert registry._hooks[HookEventTypes.PreEventCreate] is dummy

    def test_decorator_overwrite_emits_warning(self):

        def first(*args, **kwargs):
            return "first"

        def second(*args, **kwargs):
            return "second"

        registry = HookRegistry()

        # pre_event_create_hook warns on second registration
        registry.pre_event_create_hook(first)
        with pytest.warns(UserWarning, match="pre_event_create"):
            registry.pre_event_create_hook(second)
        assert registry._hooks[HookEventTypes.PreEventCreate] is second

        # pre_invoke warns on second registration
        registry.pre_invoke(first)
        with pytest.warns(UserWarning, match="pre_invocation"):
            registry.pre_invoke(second)
        assert registry._hooks[HookEventTypes.PreInvocation] is second

        # post_invoke warns on second registration
        registry.post_invoke(first)
        with pytest.warns(UserWarning, match="post_invocation"):
            registry.post_invoke(second)
        assert registry._hooks[HookEventTypes.PostInvocation] is second

    @pytest.mark.anyio
    async def test_stream_handler_missing_key_raises_runtime_error_not_validation(self):
        registry = HookRegistry()
        with pytest.raises(RuntimeError, match="No stream handler registered for missing"):
            await registry._call_stream_handler("missing", "data", None)

    def test_constructor_accepts_raw_enum_values(self):
        def dummy(*args, **kwargs):
            return None

        registry = HookRegistry({HookEventTypes.PreInvocation.value: dummy})
        assert registry._hooks[HookEventTypes.PreInvocation] is dummy

    def test_constructor_still_accepts_enum_keys(self):
        def dummy(*args, **kwargs):
            return None

        registry = HookRegistry({HookEventTypes.PreInvocation: dummy})
        assert registry._hooks[HookEventTypes.PreInvocation] is dummy
