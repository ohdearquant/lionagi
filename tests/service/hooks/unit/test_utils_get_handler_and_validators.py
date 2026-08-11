# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test contracts for hook utilities including validators and get_handler."""

import pytest

from lionagi._errors import ValidationError
from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.hooks._utils import (
    get_handler,
    validate_hooks,
    validate_stream_handlers,
)


class TestValidateHooks:
    def test_validate_hooks_accepts_correct_dict(self):
        """validate_hooks accepts a well-formed dict and returns None."""
        valid_hooks = {
            HookEventTypes.PreInvocation: lambda e, **kw: None,
            HookEventTypes.PostInvocation: lambda e, **kw: None,
        }
        result = validate_hooks(valid_hooks)
        assert result is None, "validate_hooks should return None for a valid dict"

    def test_validate_hooks_accepts_empty_dict(self):
        """validate_hooks accepts an empty dict and returns None."""
        result = validate_hooks({})
        assert result is None

    def test_validate_hooks_rejects_non_dict(self):
        with pytest.raises(ValidationError):
            validate_hooks("not a dict")

        with pytest.raises(ValidationError):
            validate_hooks([HookEventTypes.PreInvocation])

    def test_validate_hooks_rejects_bad_key_type(self):
        with pytest.raises(ValidationError):
            validate_hooks({"pre_invocation": lambda e: None})  # string instead of enum

        with pytest.raises(ValidationError):
            validate_hooks({42: lambda e: None})  # int key

    def test_validate_hooks_rejects_non_callable_values(self):
        with pytest.raises(ValidationError):
            validate_hooks({HookEventTypes.PreInvocation: "not callable"})

        with pytest.raises(ValidationError):
            validate_hooks({HookEventTypes.PreInvocation: 42})


class TestValidateStreamHandlers:
    def test_validate_stream_handlers_accepts_correct_dict(self):
        valid_handlers = {
            "chunk_type": lambda ev, ct, ch, **kw: None,
            str: lambda ev, ct, ch, **kw: None,
        }
        result = validate_stream_handlers(valid_handlers)
        assert result is None

    def test_validate_stream_handlers_accepts_empty_dict(self):
        result = validate_stream_handlers({})
        assert result is None

    def test_validate_stream_handlers_rejects_non_dict(self):
        with pytest.raises(ValidationError):
            validate_stream_handlers("not a dict")

        with pytest.raises(ValidationError):
            validate_stream_handlers(["chunk_type"])

    def test_validate_stream_handlers_rejects_bad_key_type(self):
        with pytest.raises(ValidationError):
            validate_stream_handlers({42: lambda ev, ct, ch: None})  # int key

        with pytest.raises(ValidationError):
            validate_stream_handlers({None: lambda ev, ct, ch: None})  # None key

    def test_validate_stream_handlers_accepts_string_and_type_keys(self):
        valid_handlers = {
            "string_key": lambda ev, ct, ch, **kw: None,
            str: lambda ev, ct, ch, **kw: None,
            int: lambda ev, ct, ch, **kw: None,
        }
        result = validate_stream_handlers(valid_handlers)
        assert result is None

    def test_validate_stream_handlers_rejects_non_callable_values(self):
        with pytest.raises(ValidationError):
            validate_stream_handlers({"chunk_type": "not callable"})

        with pytest.raises(ValidationError):
            validate_stream_handlers({"chunk_type": 42})


class TestGetHandler:
    @pytest.mark.anyio
    async def test_get_handler_wraps_sync_to_async(self):
        def sync_handler(ev, **kw):
            return ("sync_result", kw.get("x"))

        handlers = {"test_key": sync_handler}
        wrapped = get_handler(handlers, "test_key", True)

        result = await wrapped("event", x=42)
        assert result == ("sync_result", 42)

    @pytest.mark.anyio
    async def test_get_handler_returns_async_unchanged(self):
        async def async_handler(ev, **kw):
            return ("async_result", kw.get("x"))

        handlers = {"test_key": async_handler}
        wrapped = get_handler(handlers, "test_key", True)

        assert wrapped is async_handler
        result = await wrapped("event", x=42)
        assert result == ("async_result", 42)

    @pytest.mark.anyio
    async def test_get_handler_missing_key_with_get_false_returns_none(self):
        handlers = {}
        result = get_handler(handlers, "missing_key", False)
        assert result is None

    @pytest.mark.anyio
    async def test_get_handler_missing_key_with_get_true_returns_passthrough(
        self,
    ):
        handlers = {}
        wrapped = get_handler(handlers, "missing_key", True)

        result = await wrapped("input")
        assert result == "input"

    @pytest.mark.anyio
    async def test_get_handler_default_get_false(self):
        handlers = {}
        result = get_handler(handlers, "missing_key")
        assert result is None

    @pytest.mark.anyio
    async def test_get_handler_with_none_value(self):
        handlers = {"test_key": None}

        result = get_handler(handlers, "test_key", False)
        assert result is None

        wrapped = get_handler(handlers, "test_key", True)
        result = await wrapped("input")
        assert result == "input"
