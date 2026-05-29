# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""All governance exception types."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lionagi.governance.context import OperationBudget
    from lionagi.governance.gates import GateResult

__all__ = [
    "BreakGlassDisabledError",
    "BreakGlassInactiveError",
    "BreakGlassMissingAttestationError",
    "BudgetExceededError",
    "CharterActivationError",
    "CharterParseError",
    "GovernanceMissingContextError",
    "GovernanceViolationError",
    "PolicyPinMismatchError",
]


class BudgetExceededError(Exception):
    def __init__(
        self,
        budget_or_msg: OperationBudget | str | None = None,
        requested: int = 1,
        message: str = "",
    ) -> None:
        if isinstance(budget_or_msg, str):
            self.budget = None
            self.requested = requested
            super().__init__(budget_or_msg)
        elif budget_or_msg is not None:
            self.budget = budget_or_msg
            self.requested = requested
            remaining = (
                (budget_or_msg.max_calls - budget_or_msg.calls_used)
                if budget_or_msg.max_calls is not None
                else None
            )
            super().__init__(
                message or f"Budget exceeded: {remaining} remaining, {requested} requested"
            )
        else:
            self.budget = None
            self.requested = requested
            super().__init__(message or "Budget exceeded")


class GovernanceMissingContextError(Exception):
    pass


class PolicyPinMismatchError(Exception):
    pass


class GovernanceViolationError(Exception):
    def __init__(self, result: GateResult) -> None:
        self.result = result
        super().__init__(f"Gate {result.gate_id} denied: {result.justification}")


class BreakGlassDisabledError(Exception):
    pass


class BreakGlassInactiveError(Exception):
    pass


class BreakGlassMissingAttestationError(ValueError):
    pass


class CharterParseError(Exception):
    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        prefix = f"[line {line}] " if line is not None else ""
        super().__init__(f"{prefix}{message}")


class CharterActivationError(Exception):
    pass
