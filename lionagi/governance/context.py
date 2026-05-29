# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from lionagi.governance.evidence import EvidenceChain


from lionagi.governance.errors import (
    BudgetExceededError,
    GovernanceMissingContextError,
    PolicyPinMismatchError,
)


class PolicyPin(BaseModel):
    charter_id: str
    charter_version: str
    charter_hash: str
    pinned_at: datetime


class OperationBudget(BaseModel):
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


class OperationContext(BaseModel):
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
