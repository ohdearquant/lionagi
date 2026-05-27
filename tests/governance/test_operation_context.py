# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextvars
from datetime import datetime

import pytest

from lionagi.protocols.governance.context import (
    BudgetExceededError,
    GovernanceMissingContextError,
    OperationBudget,
    OperationContext,
    PolicyPin,
    PolicyPinMismatchError,
    _operation_context_var,
    get_operation_context,
    set_operation_context,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pin() -> PolicyPin:
    return PolicyPin(
        charter_id="charter-001",
        charter_version="1.0.0",
        charter_hash="abc123def456",
        pinned_at=datetime(2026, 1, 1, 0, 0, 0),
    )


def _ctx(**kwargs) -> OperationContext:
    defaults = dict(
        actor_id="agent-001",
        actor_role="executor",
        policy_pin=_pin(),
        trace_id="trace-0001",
        span_id="span-0001",
    )
    defaults.update(kwargs)
    return OperationContext(**defaults)


# ---------------------------------------------------------------------------
# 1. OperationContext fields
# ---------------------------------------------------------------------------


def test_create_context_fields():
    pin = _pin()
    ctx = OperationContext(
        actor_id="agent-001",
        actor_role="executor",
        policy_pin=pin,
        trace_id="trace-abc",
        span_id="span-xyz",
    )
    assert ctx.actor_id == "agent-001"
    assert ctx.actor_role == "executor"
    assert ctx.policy_pin is pin
    assert ctx.trace_id == "trace-abc"
    assert ctx.span_id == "span-xyz"
    assert ctx.parent_span_id is None
    assert ctx.operation_budget is None
    assert ctx.evidence_chain_ref is None


# ---------------------------------------------------------------------------
# 2. PolicyPin fields
# ---------------------------------------------------------------------------


def test_policy_pin_fields():
    pinned_at = datetime(2026, 5, 27, 12, 0, 0)
    pin = PolicyPin(
        charter_id="charter-001",
        charter_version="1.0.0",
        charter_hash="abc123def456",
        pinned_at=pinned_at,
    )
    assert pin.charter_id == "charter-001"
    assert pin.charter_version == "1.0.0"
    assert pin.charter_hash == "abc123def456"
    assert pin.pinned_at == pinned_at


# ---------------------------------------------------------------------------
# 3-7. OperationBudget
# ---------------------------------------------------------------------------


def test_budget_within_limits():
    budget = OperationBudget(max_calls=5)
    budget.record_usage(calls=1)
    budget.record_usage(calls=1)
    budget.record_usage(calls=1)
    assert budget.check_budget() is True


def test_budget_calls_exceeded():
    # max_calls=2 means >2 raises; calls_used hits 3 on the 3rd record_usage
    budget = OperationBudget(max_calls=2)
    budget.record_usage()
    budget.record_usage()
    with pytest.raises(BudgetExceededError):
        budget.record_usage()


def test_budget_tokens_exceeded():
    budget = OperationBudget(max_tokens=100)
    budget.record_usage(tokens=60)  # 60 <= 100, ok
    with pytest.raises(BudgetExceededError):
        budget.record_usage(tokens=60)  # 120 > 100, raises


def test_budget_check_false_when_over():
    budget = OperationBudget(max_calls=5)
    budget.calls_used = 10
    assert budget.check_budget() is False


def test_budget_none_limits_never_exceeded():
    budget = OperationBudget()
    for _ in range(100):
        budget.record_usage(tokens=1000, calls=1)
    assert budget.check_budget() is True


# ---------------------------------------------------------------------------
# 8-11. contextvars bridge
# ---------------------------------------------------------------------------


def test_contextvars_default_none():
    # Run inside a fresh copy to guarantee no prior set from other tests
    result = contextvars.copy_context().run(get_operation_context)
    assert result is None


def test_contextvars_bridge_set_get():
    ctx = _ctx()

    def _run():
        set_operation_context(ctx)
        return get_operation_context()

    result = contextvars.copy_context().run(_run)
    assert result is ctx


def test_contextvars_reset_token():
    ctx = _ctx()

    def _run():
        token = set_operation_context(ctx)
        assert get_operation_context() is ctx
        _operation_context_var.reset(token)
        return get_operation_context()

    result = contextvars.copy_context().run(_run)
    assert result is None


async def test_contextvars_bridge_async():
    ctx = _ctx()
    token = set_operation_context(ctx)
    try:
        await asyncio.sleep(0)
        assert get_operation_context() is ctx
    finally:
        _operation_context_var.reset(token)


# ---------------------------------------------------------------------------
# 12. Evidence embedding
# ---------------------------------------------------------------------------


def test_evidence_embedding():
    class MockEvidenceChain:
        def head_hash(self) -> str:
            return "deadbeef1234"

    ctx = _ctx()
    chain = MockEvidenceChain()
    ctx.embed_evidence(chain)
    assert ctx.evidence_chain_ref == "deadbeef1234"


# ---------------------------------------------------------------------------
# 13-15. Exception hierarchy
# ---------------------------------------------------------------------------


def test_missing_context_governed_error():
    assert issubclass(GovernanceMissingContextError, Exception)
    assert isinstance(GovernanceMissingContextError("test"), Exception)


def test_policy_pin_mismatch_error():
    assert issubclass(PolicyPinMismatchError, Exception)
    assert isinstance(PolicyPinMismatchError("mismatch"), Exception)


def test_budget_exceeded_error():
    assert issubclass(BudgetExceededError, Exception)
    assert isinstance(BudgetExceededError("exceeded"), Exception)
