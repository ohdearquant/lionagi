# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test contracts for hook types and enums."""

from collections.abc import Callable
from typing import Any, get_args, get_origin, get_type_hints

import pytest

from lionagi.service.connections.api_calling import APICalling
from lionagi.service.hooks._types import (
    ALLOWED_HOOKS_TYPES,
    AssociatedEventInfo,
    HookDict,
    HookEventTypes,
    StreamHandlers,
)
from lionagi.service.hooks.hook_registry import HookRegistry
from lionagi.service.imodel import iModel


class TestHookEventTypes:
    def test_hook_event_types_enum_values(self):
        """Test that HookEventTypes contains the expected values."""
        assert HookEventTypes.PreEventCreate == "pre_event_create"
        assert HookEventTypes.PreInvocation == "pre_invocation"
        assert HookEventTypes.PostInvocation == "post_invocation"

    def test_allowed_hooks_types_contains_all(self):
        """Test that ALLOWED_HOOKS_TYPES contains all HookEventTypes."""
        expected = {
            HookEventTypes.PreEventCreate,
            HookEventTypes.PreInvocation,
            HookEventTypes.PostInvocation,
        }
        assert set(ALLOWED_HOOKS_TYPES) == expected


class TestAssociatedEventInfo:
    def test_associated_event_info_structure(self):
        """Test AssociatedEventInfo TypedDict structure."""
        # Test that we can create instances with expected keys
        info = AssociatedEventInfo(
            lion_class="test.Module.Event",
            event_id="E123",
            event_created_at=42.0,
        )
        assert info["lion_class"] == "test.Module.Event"
        assert info["event_id"] == "E123"
        assert info["event_created_at"] == 42.0

    def test_associated_event_info_partial(self):
        """Test AssociatedEventInfo works with partial data (total=False)."""
        info = AssociatedEventInfo(lion_class="test.Event")
        assert info["lion_class"] == "test.Event"
        # Should not have other keys
        assert "event_id" not in info
        assert "event_created_at" not in info


class TestHookDict:
    def test_hook_dict_structure(self):
        """Test HookDict TypedDict structure."""
        hook_dict = HookDict(
            pre_event_create=lambda: None,
            pre_invocation=lambda: None,
            post_invocation=lambda: None,
        )
        assert callable(hook_dict["pre_event_create"])
        assert callable(hook_dict["pre_invocation"])
        assert callable(hook_dict["post_invocation"])

    def test_hook_dict_with_none_values(self):
        """Test HookDict allows None values."""
        hook_dict = HookDict(
            pre_event_create=None,
            pre_invocation=lambda: None,
            post_invocation=None,
        )
        assert hook_dict["pre_event_create"] is None
        assert callable(hook_dict["pre_invocation"])
        assert hook_dict["post_invocation"] is None


class TestRuntimeTypeConformance:
    @pytest.mark.anyio
    async def test_stream_handler_type_matches_runtime_arguments(self):
        _, handler_type = get_args(StreamHandlers)
        assert get_origin(handler_type) is Callable
        positional, _ = get_args(handler_type)
        assert len(positional) == 3
        assert positional[0] is Any
        assert positional[1] == str | type

        captured = {}

        async def handler(event, chunk_type, chunk, **kwargs):
            captured.update(
                event=event,
                chunk_type=chunk_type,
                chunk=chunk,
                kwargs=kwargs,
            )

        handlers: StreamHandlers = {"text": handler}
        registry = HookRegistry(stream_handlers=handlers)

        await registry.handle_streaming_chunk("text", "payload", marker="value")

        assert captured == {
            "event": None,
            "chunk_type": "text",
            "chunk": "payload",
            "kwargs": {"exit": False, "marker": "value"},
        }

    @pytest.mark.anyio
    async def test_create_event_annotation_matches_runtime_value(self):
        assert get_type_hints(iModel.create_event)["return"] is APICalling

        model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
        event = await model.create_event(messages=[{"role": "user", "content": "hello"}])

        assert isinstance(event, APICalling)
