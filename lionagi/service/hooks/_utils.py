# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi._errors import ValidationError
from lionagi.ln import is_coro_func
from lionagi.ln.concurrency import run_sync

from ._types import ALLOWED_HOOKS_TYPES, HookEventTypes

__all__ = (
    "get_handler",
    "validate_hooks",
    "validate_stream_handlers",
)


def get_handler(d_: dict, k: str | type, get: bool = False, /):
    handler = d_.get(k)
    if handler is None and not get:
        return None

    if handler is not None:
        if not is_coro_func(handler):

            async def _func(*args, **kwargs):
                return await run_sync(handler, *args, **kwargs)

            return _func
        return handler

    async def _func(*args, **_kwargs):
        return args[0] if args else None

    return _func


def validate_hooks(kw):
    """Validate that all hooks are callable."""
    if not isinstance(kw, dict):
        raise ValidationError.from_value(
            kw,
            expected="A dictionary of hooks",
            message="Hooks must be a dictionary of callable functions",
        )
    for k, v in kw.items():
        if not isinstance(k, HookEventTypes) or k not in ALLOWED_HOOKS_TYPES:
            raise ValidationError.from_value(
                k,
                expected=f"One of {ALLOWED_HOOKS_TYPES}",
                message=f"Hook key must be one of {ALLOWED_HOOKS_TYPES}, got {k}",
            )
        if not callable(v):
            raise ValidationError.from_value(
                v,
                expected="A callable function",
                message=f"Hook for {k} must be callable, got {type(v)}",
            )


def validate_stream_handlers(kw):
    """Validate that all stream handlers are callable."""
    if not isinstance(kw, dict):
        raise ValidationError.from_value(
            kw,
            expected="A dictionary of stream handlers",
            message="Stream handlers must be a dictionary of callable functions",
        )
    for k, v in kw.items():
        if not isinstance(k, str | type):
            raise ValidationError.from_value(
                k,
                expected="A name or type of the chunk being handled",
                message=f"Stream handler key must be a string or type, got {type(k)}",
            )
        if not callable(v):
            raise ValidationError.from_value(
                v,
                expected="A callable function",
                message=f"Stream handler for {k} must be callable, got {type(v)}",
            )
