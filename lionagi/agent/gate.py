# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unified security-control verdict contract (ADR-0086 delta row 1).

``GateResult`` is the one immutable shape every security control produces;
see docs/internals/runtime.md for the full contract.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lionagi.ln.concurrency import maybe_await

__all__ = (
    "GateDeniedError",
    "GateEvaluator",
    "GateResult",
    "adapt_legacy_hook",
    "adapt_session_gate",
    "run_gate_pass",
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GateResult:
    """One immutable verdict from a single security control evaluation."""

    allowed: bool
    control: str
    tool_name: str
    action: str
    reason: str
    mutated_args: dict | None = None
    errored: bool = False


GateEvaluator = Callable[[str, str, dict], Awaitable[GateResult]]


class GateDeniedError(PermissionError):
    """Raised when a gate evaluation pass denies a call; carries the verdict."""

    def __init__(self, result: GateResult) -> None:
        super().__init__(f"{result.control}: {result.reason}")
        self.result = result


def adapt_legacy_hook(control: str, hook: Callable) -> GateEvaluator:
    """Adapt a legacy ``(tool_name, action, args) -> dict | None`` pre-hook.

    Denial = raise ``PermissionError``; rewrite = return a ``dict``; any
    other exception fails closed as a deny GateResult. See docs/internals/runtime.md.
    """

    async def evaluate(tool_name: str, action: str, args: dict) -> GateResult:
        try:
            result = await maybe_await(hook(tool_name, action, args))
        except PermissionError as e:
            return GateResult(False, control, tool_name, action, str(e))
        except Exception as e:  # noqa: BLE001 - fail-closed on any evaluator error
            logger.warning("gate control %r raised %s; failing closed", control, type(e).__name__)
            return GateResult(
                False,
                control,
                tool_name,
                action,
                f"evaluator error: {e}",
                errored=True,
            )
        if isinstance(result, dict):
            return GateResult(True, control, tool_name, action, "allow", mutated_args=result)
        return GateResult(True, control, tool_name, action, "allow")

    return evaluate


def adapt_session_gate(
    check: Callable[[Any], Any],
) -> Callable[[Any], Awaitable[GateResult]]:
    """Adapt a ``SessionObserver`` gate callable (``check(action) -> bool``)."""

    async def evaluate(action: Any) -> GateResult:
        tool_name = str(getattr(action, "function", "") or "")
        try:
            allowed = bool(await maybe_await(check(action)))
        except Exception as e:  # noqa: BLE001 - fail-closed on any evaluator error
            logger.warning("session gate raised %s; failing closed", type(e).__name__)
            return GateResult(
                False,
                "session_gate",
                tool_name,
                "authorize",
                f"evaluator error: {e}",
                errored=True,
            )
        if allowed:
            return GateResult(True, "session_gate", tool_name, "authorize", "allowed")
        return GateResult(False, "session_gate", tool_name, "authorize", "denied by session gate")

    return evaluate


async def run_gate_pass(
    evaluators: list[GateEvaluator],
    tool_name: str,
    action: str,
    args: dict,
) -> tuple[dict, GateResult | None]:
    """Evaluate each control exactly once against ``args``; stop at first deny."""
    for evaluate in evaluators:
        result = await evaluate(tool_name, action, args)
        if not result.allowed:
            return args, result
        if result.mutated_args is not None:
            args = result.mutated_args
    return args, None
