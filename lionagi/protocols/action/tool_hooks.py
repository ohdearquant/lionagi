# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tool-event hook contract at the ``ActionManager.invoke`` chokepoint.

This is the mutation-capable layer that sits outermost around every tool
call routed through ``ActionManager`` (plain function tools, ``Tool``
objects, and MCP-discovered tools alike). It is deliberately separate from
``lionagi.hooks.bus.HookBus`` (the summary-payload audit plane) and from the
per-``Tool`` ``preprocessor``/``postprocessor`` chain wired by
``lionagi.agent.spec.HooksMixin`` (the spec-level security/user chain,
which keeps running innermost, closest to the tool).

A pre hook receives the tool name and the current argument dict and returns
a verdict:

- ``None`` -- allow, arguments unchanged.
- a plain ``dict`` -- allow, replace the arguments with this dict.
- a :class:`ToolPreDecision` -- full control: ``"allow"`` (optionally with
  ``updated_input``), ``"deny"``, ``"ask"`` (fails closed -- no interactive
  approval surface exists), or any other value (fails closed with a
  diagnostic naming the unrecognized value).

A post hook receives the tool name, the final arguments, the result (or
``None`` on failure), and the error (or ``None`` on success). Post hooks are
advisory only: the action has already happened, so a post hook cannot deny
or rewrite anything, matching the harness convention that ``block`` on a
post-invocation event cannot un-run the call.
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


@dataclass(frozen=True, slots=True)
class ToolPreDecision:
    """One pre-hook's verdict on a pending tool call.

    ``decision`` follows the cross-harness intersection vocabulary
    (``allow | deny | ask``); any other value is treated as unrecognized and
    fails closed the same as ``deny``.
    """

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


def _hook_name(hook: Callable) -> str:
    return getattr(hook, "__name__", None) or type(hook).__name__


def _snapshot(value: Any) -> Any:
    """Best-effort isolated copy; falls back to the original if uncopyable."""
    try:
        return deepcopy(value)
    except Exception:  # noqa: BLE001 - an uncopyable value is rare and non-fatal
        return value


async def run_tool_pre_hooks(
    hooks: list[ToolPreHook],
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run pre hooks in config order; return the (possibly rewritten) arguments.

    Raises ``ToolHookDeniedError`` -- a ``PermissionError`` subtype -- on the
    first ``deny``, ``ask``, or unrecognized decision (fail closed), or when
    a hook itself raises. ``ask`` has no interactive-approval surface in this
    runtime, so it is treated exactly like ``deny``.
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

    A post hook that raises is logged and skipped -- an observer must not be
    able to take down a call that already completed. Returns the non-empty
    ``reason`` strings collected from hooks that returned a
    :class:`ToolPostDecision`.
    """
    # Deep-copied once, shared read-only across hooks -- a hook mutating its
    # copy (top-level or nested) must never reach the live event.
    snapshot_arguments = _snapshot(arguments)
    snapshot_result = _snapshot(result)

    notes: list[str] = []
    for hook_handler in hooks:
        name = _hook_name(hook_handler)
        try:
            raw = await maybe_await(
                hook_handler(tool_name, snapshot_arguments, snapshot_result, error)
            )
        except Exception:  # noqa: BLE001 - advisory hooks must not break the caller
            logger.exception("tool post hook %r failed", name)
            continue
        if isinstance(raw, ToolPostDecision) and raw.reason:
            notes.append(raw.reason)
    return notes
