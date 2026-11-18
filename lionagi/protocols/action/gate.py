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

    def __post_init__(self) -> None:
        broken = self._broken_rule()
        if broken is not None:
            error, message = broken
            raise error(message)

    def _broken_rule(self) -> tuple[type[Exception], str] | None:
        """The first rule of a verdict this one breaks, as the error to raise and its message,
        or None.

        ``allowed`` and ``errored`` are bools and not both true, ``mutated_args`` is None or a
        dict, and the four text fields are strings. Construction enforces these; an instance
        built around the constructor (``object.__new__`` or ``object.__setattr__``) can still
        break them, so the adapters ask again. A message names the field and the type of its
        value, never the value, which can hold arguments a hook was given.
        """
        if type(self.allowed) is not bool:
            return TypeError, f"allowed must be a bool, not {type(self.allowed).__name__}"
        if type(self.errored) is not bool:
            return TypeError, f"errored must be a bool, not {type(self.errored).__name__}"
        if self.allowed and self.errored:
            return ValueError, (
                "allowed and errored are both true: an errored gate result cannot allow the action"
            )
        if self.mutated_args is not None and not isinstance(self.mutated_args, dict):
            return TypeError, (
                f"mutated_args must be a dict or None, not {type(self.mutated_args).__name__}"
            )
        for name in ("control", "tool_name", "action", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str):
                return TypeError, f"{name} must be a str, not {type(value).__name__}"
        return None


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
        malformed = _malformed_call(control, tool_name, action)
        if malformed is not None:
            # A verdict for this call could not be constructed, so refuse it here, with text
            # that is valid, rather than let the construction raise out of the pass.
            logger.warning("gate control %r refused a malformed call: %s", control, malformed)
            return GateResult(
                False,
                _as_text(control),
                _as_text(tool_name),
                _as_text(action),
                malformed,
                errored=True,
            )
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
            # Do not treat a truthy non-boolean decision or an inconsistent fault as approval.
            broken = _broken_rule_of(result)
            if broken is None:
                return result
            return GateResult(False, control, tool_name, action, broken, errored=True)
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


def _malformed_call(control: Any, tool_name: Any, action: Any) -> str | None:
    """Why a call cannot carry a verdict, or None: every verdict names its control, tool and
    action as strings. ``action`` can come from a rewrite an earlier control returned."""
    for name, value in (("control", control), ("tool_name", tool_name), ("action", action)):
        if not isinstance(value, str):
            return f"invalid gate call: {name} must be a str, not {type(value).__name__}"
    return None


def _as_text(value: Any) -> str:
    """*value* when it is a string, otherwise its type name; never the value itself."""
    return value if isinstance(value, str) else f"<{type(value).__name__}>"


def _broken_rule_of(result: GateResult) -> str | None:
    """Why *result* is not a valid verdict, or None. Construction already enforces the rules
    (``GateResult._broken_rule``), so this catches an instance built around the constructor; a
    verdict whose fields cannot even be read is invalid too. The base class's rules are applied
    even when a subclass overrides them."""
    try:
        broken = GateResult._broken_rule(result)
    except Exception:  # noqa: BLE001 - unreadable verdicts must fail closed
        return "invalid GateResult: its fields cannot be read"
    return None if broken is None else f"invalid GateResult: {broken[1]}"


def _session_gate_result(result: Any, tool_name: str = "") -> GateResult:
    """Preserve typed session verdicts and fail closed on invalid decisions."""
    if isinstance(result, GateResult):
        broken = _broken_rule_of(result)
        if broken is not None:
            return GateResult(False, "session_gate", tool_name, "authorize", broken, errored=True)
        if result.allowed and result.mutated_args is not None:
            return GateResult(
                False,
                "session_gate",
                tool_name,
                "authorize",
                "session gate does not apply argument rewrites",
                errored=True,
            )
        return result
    if type(result) is not bool:
        # Avoid repr(result): a gate may return sensitive or unprintable data.
        return GateResult(
            False,
            "session_gate",
            tool_name,
            "authorize",
            f"unsupported session gate result type: {type(result).__name__}",
            errored=True,
        )
    return GateResult(
        result,
        "session_gate",
        tool_name,
        "authorize",
        "allowed" if result else "denied by session gate",
    )


def adapt_session_gate(
    check: Callable[[Any], Any],
) -> Callable[[Any], Awaitable[GateResult]]:
    """Adapt a ``SessionObserver`` gate returning a bool or ``GateResult``."""

    async def evaluate(action: Any) -> GateResult:
        tool_name = str(getattr(action, "function", "") or "")
        try:
            return _session_gate_result(await maybe_await(check(action)), tool_name)
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
