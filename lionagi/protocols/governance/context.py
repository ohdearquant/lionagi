# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from lionagi.protocols.generic.element import Element

if TYPE_CHECKING:
    from lionagi.protocols.governance.evidence import EvidenceChain


class BudgetExceededError(Exception):
    """Raised when an OperationBudget limit is breached.

    Accepts either the legacy string-message form::

        BudgetExceededError("some message")

    or the structured form used by GovernedFlowController::

        BudgetExceededError(budget=op_budget, requested=1)
    """

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


class PolicyPin(BaseModel):
    charter_id: str
    charter_version: str
    charter_hash: str
    pinned_at: datetime


class OperationBudget(Element):
    max_tokens: int | None = None
    max_calls: int | None = None
    max_duration_seconds: float | None = None
    calls_used: int = 0
    tokens_used: int = 0

    def check_budget(self) -> bool:
        if self.max_calls is not None and self.calls_used > self.max_calls:
            return False
        if self.max_tokens is not None and self.tokens_used > self.max_tokens:
            return False
        return True

    def record_usage(self, tokens: int = 0, calls: int = 1) -> None:
        self.calls_used += calls
        self.tokens_used += tokens
        if not self.check_budget():
            raise BudgetExceededError(self, calls)


class OperationContext(Element):
    actor_id: str
    actor_role: str
    policy_pin: PolicyPin
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    operation_budget: OperationBudget | None = None
    evidence_chain_ref: str | None = None

    def embed_evidence(self, chain: EvidenceChain) -> None:
        self.evidence_chain_ref = chain.head_hash()


_operation_context_var: ContextVar[OperationContext | None] = ContextVar(
    "operation_context", default=None
)


def get_operation_context() -> OperationContext | None:
    return _operation_context_var.get()


def set_operation_context(ctx: OperationContext) -> Token:
    return _operation_context_var.set(ctx)


__all__ = (
    "BudgetExceededError",
    "GovernanceMissingContextError",
    "OperationBudget",
    "OperationContext",
    "PolicyPin",
    "PolicyPinMismatchError",
    "get_operation_context",
    "set_operation_context",
)
