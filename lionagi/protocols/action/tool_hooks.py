# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tool-event hook contract at the ``ActionManager.invoke`` chokepoint.

Mutation-capable layer around every tool call, outermost and distinct from
``HookBus`` (audit-only) and the spec-level pre/postprocessor chain
(innermost). See docs/internals/core.md for the full pre/post verdict shape.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from lionagi.ln.concurrency import maybe_await

__all__ = (
    "ToolHookDeniedError",
    "ToolPostDecision",
    "ToolPostHook",
    "ToolPreDecision",
    "ToolPreHook",
    "run_tool_post_hooks",
    "run_tool_pre_hooks",
)

logger = logging.getLogger(__name__)

_ALLOW = "allow"
_DENY = "deny"
_ASK = "ask"
_SNAPSHOT_FAILED = object()


@dataclass(frozen=True, slots=True)
class ToolPreDecision:
    """One pre-hook's verdict: ``decision`` in ``allow | deny | ask``; any
    other value fails closed the same as ``deny``."""

    decision: str = _ALLOW
    reason: str = ""
    updated_input: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ToolPostDecision:
    """One post-hook's advisory note on a completed tool call."""

    reason: str | None = None


ToolPreHook = Callable[
    [str, dict[str, Any]],
    "Awaitable[ToolPreDecision | dict[str, Any] | None] | ToolPreDecision | dict[str, Any] | None",
]

ToolPostHook = Callable[
    [str, dict[str, Any], Any, BaseException | None],
    "Awaitable[ToolPostDecision | None] | ToolPostDecision | None",
]


class ToolHookDeniedError(PermissionError):
    """Raised when a tool-pre hook denies (or fails closed on) a call."""

    def __init__(self, hook_name: str, reason: str) -> None:
        super().__init__(f"{hook_name}: {reason}" if hook_name else reason)
        self.hook_name = hook_name
        self.reason = reason

    def __reduce__(self):
        # BaseException's default __reduce__ replays via `self.args`, which
        # here is the single formatted message string -- not the two
        # positional args this __init__ requires. That mismatch makes
        # deepcopy() (used by run_tool_post_hooks' evidence isolation) raise
        # and silently skip post hooks on the deny path. Reconstruct from
        # the named fields instead so deepcopy/pickle round-trip correctly.
        return (self.__class__, (self.hook_name, self.reason))


def _hook_name(hook: Callable) -> str:
    return getattr(hook, "__name__", None) or type(hook).__name__


def _snapshot(value: Any) -> Any:
    """Return an isolated copy, or a sentinel when isolation is impossible."""
    try:
        return deepcopy(value)
    except Exception:  # noqa: BLE001 - an uncopyable value is rare and non-fatal
        return _SNAPSHOT_FAILED


async def run_tool_pre_hooks(
    hooks: list[ToolPreHook],
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run pre hooks in order; return the (possibly rewritten) arguments.

    Raises ``ToolHookDeniedError`` on the first ``deny``/``ask``/unrecognized
    decision, or when a hook itself raises (fail closed).
    """
    for hook_handler in hooks:
        name = _hook_name(hook_handler)
        try:
            raw = await maybe_await(hook_handler(tool_name, arguments))
        except PermissionError as e:
            raise ToolHookDeniedError(name, str(e)) from e
        except Exception as e:  # noqa: BLE001 - fail closed on any hook error
            logger.warning("tool pre hook %r raised %s; failing closed", name, type(e).__name__)
            raise ToolHookDeniedError(name, f"hook error: {e}") from e

        if raw is None:
            continue
        if isinstance(raw, dict):
            arguments = raw
            continue
        if not isinstance(raw, ToolPreDecision):
            raise ToolHookDeniedError(
                name,
                f"hook returned unsupported type {type(raw).__name__}; failing closed",
            )

        if raw.decision == _ALLOW:
            if raw.updated_input is not None:
                arguments = raw.updated_input
            continue
        if raw.decision == _DENY:
            raise ToolHookDeniedError(name, raw.reason or "denied")
        if raw.decision == _ASK:
            raise ToolHookDeniedError(
                name,
                "hook requested interactive approval ('ask'); no interactive "
                "approval surface exists in this runtime -- failing closed",
            )
        raise ToolHookDeniedError(name, f"unrecognized decision {raw.decision!r}; failing closed")

    return arguments


async def run_tool_post_hooks(
    hooks: list[ToolPostHook],
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    error: BaseException | None,
) -> list[str]:
    """Run post hooks in order; advisory only, never affects the outcome.

    A raising hook is logged and skipped. Returns the non-empty ``reason``
    strings collected from hooks that returned a :class:`ToolPostDecision`.
    """
    canonical_arguments = _snapshot(arguments)
    canonical_result = _snapshot(result)
    canonical_error = _snapshot(error)
    if any(
        value is _SNAPSHOT_FAILED
        for value in (canonical_arguments, canonical_result, canonical_error)
    ):
        logger.warning("tool post hooks skipped: completed call state could not be isolated")
        return []

    notes: list[str] = []
    for hook_handler in hooks:
        name = _hook_name(hook_handler)
        snapshot_arguments = _snapshot(canonical_arguments)
        snapshot_result = _snapshot(canonical_result)
        snapshot_error = _snapshot(canonical_error)
        if any(
            value is _SNAPSHOT_FAILED
            for value in (snapshot_arguments, snapshot_result, snapshot_error)
        ):
            logger.warning("tool post hook %r skipped: evidence could not be isolated", name)
            continue
        try:
            raw = await maybe_await(
                hook_handler(
                    tool_name,
                    snapshot_arguments,
                    snapshot_result,
                    snapshot_error,
                )
            )
        except Exception:  # noqa: BLE001 - advisory hooks must not break the caller
            logger.exception("tool post hook %r failed", name)
            continue
        if isinstance(raw, ToolPostDecision) and raw.reason:
            notes.append(raw.reason)
    return notes
