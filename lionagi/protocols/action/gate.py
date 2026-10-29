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
    "ControlUnavailableError",
    "GateDeniedError",
    "GateEvaluator",
    "GateResult",
    "adapt_legacy_hook",
    "adapt_session_gate",
    "run_gate_pass",
)

logger = logging.getLogger("lionagi.agent.gate")


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


class ControlUnavailableError(PermissionError):
    """A control could not reach a verdict — its own configuration is the
    problem (missing escalation handler, unreachable backend, a rule set
    that failed to load), not anything about the call. Still refuses the
    call, since a control that cannot answer must not be read as approval.

    Subclasses ``PermissionError`` so existing fail-closed handling is
    unchanged; distinguishes "configuration cannot answer this" from "the
    answer is no" for operators who act on the two differently.
    """


class GateDeniedError(PermissionError):
    """Raised when a gate evaluation pass denies a call; carries the verdict."""

    def __init__(self, result: GateResult) -> None:
        prefix = f"{result.control} could not evaluate" if result.errored else result.control
        super().__init__(f"{prefix}: {result.reason}")
        self.result = result

    def __reduce__(self):
        # The default replays ``self.args``, the formatted message, into ``__init__``, which takes
        # the GateResult, so copy.deepcopy and pickle raise. Tool post hooks deepcopy a failed
        # call's error and are skipped when it cannot be copied, so a denied call reached none of
        # them. Rebuild from the GateResult and keep any other instance state.
        return (self.__class__, (self.result,), self.__dict__)


def adapt_legacy_hook(control: str, hook: Callable) -> GateEvaluator:
    """Adapt a ``(tool_name, action, args) -> GateResult | dict | None`` pre-hook.

    Legacy denial raises ``PermissionError``; a dict rewrites arguments and
    None allows them unchanged. A GateResult retains its verdict. Unknown or
    malformed results and other exceptions fail closed as control faults.
    """

    async def evaluate(tool_name: str, action: str, args: dict) -> GateResult:
        try:
            result = await maybe_await(hook(tool_name, action, args))
        except ControlUnavailableError as e:
            # Caught before PermissionError, which it subclasses: a control
            # saying it cannot answer is a fault, and reporting it as a
            # verdict is what makes a misconfiguration look like policy.
            logger.warning("gate control %r could not evaluate: %s", control, e)
            return GateResult(False, control, tool_name, action, str(e), errored=True)
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
        if isinstance(result, GateResult):
            # Dataclass annotations are not runtime validation: do not treat a
            # truthy non-boolean decision or an inconsistent fault as approval.
            try:
                valid = (
                    type(result.allowed) is bool
                    and type(result.errored) is bool
                    and not (result.allowed and result.errored)
                    and (result.mutated_args is None or isinstance(result.mutated_args, dict))
                    and all(
                        isinstance(value, str)
                        for value in (
                            result.control,
                            result.tool_name,
                            result.action,
                            result.reason,
                        )
                    )
                )
            except Exception:  # noqa: BLE001 - unreadable verdicts must fail closed
                valid = False
            if valid:
                return result
            return GateResult(False, control, tool_name, action, "invalid GateResult", errored=True)
        if isinstance(result, dict):
            return GateResult(True, control, tool_name, action, "allow", mutated_args=result)
        if result is None:
            return GateResult(True, control, tool_name, action, "allow")
        # Avoid repr(result): a hook may return sensitive or unprintable data.
        return GateResult(
            False,
            control,
            tool_name,
            action,
            f"unsupported hook result type: {type(result).__name__}",
            errored=True,
        )

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
            if result.errored:
                # The pass is where a refusal becomes final, so it is where the
                # difference between a broken control and a working one has to
                # be visible. A denial is routine and logs nothing; a control
                # that could not answer is an operator's problem.
                logger.error(
                    "gate control %r refused %s.%s without reaching a verdict: %s",
                    result.control,
                    tool_name,
                    action,
                    result.reason,
                )
            return args, result
        if result.mutated_args is not None:
            args = result.mutated_args
            action = args.get("action", action)
    return args, None
