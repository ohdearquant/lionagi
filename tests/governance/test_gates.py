# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from datetime import datetime

import pytest

from lionagi.governance.context import OperationContext
from lionagi.governance.context import PolicyPin as ContextPolicyPin
from lionagi.governance.dsl import Enforcement
from lionagi.governance.evidence import EvidenceChain
from lionagi.governance.gates import (
    GateExecutor,
    GateResult,
    GateVerdict,
    GovernanceViolationError,
)
from lionagi.governance.governed_tool import governed_tool
from lionagi.governance.targets import GateRegistration
from lionagi.protocols.action.manager import ActionManager

# ---------------------------------------------------------------------------
# Module-level tool callables — NO type annotations.
# ActionManager calls Tool(func_callable=fn) at registration time, which
# triggers function_to_schema. With `from __future__ import annotations`,
# param.annotation is a string ("int") rather than the actual type, causing
# `param.annotation.__name__` to raise AttributeError. Functions defined
# here (module scope, no annotations) are safe to pass to ActionManager.
# ---------------------------------------------------------------------------


def _tool_add(x, y):
    return x + y


def _tool_multiply(a, b):
    return a * b


def _tool_op(x):
    return x * 2


def _tool_secret_op(x):
    return x


def _tool_governed_func(x):
    return str(x)


def _tool_no_params():
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx() -> OperationContext:
    pin = ContextPolicyPin(
        charter_id="charter-001",
        charter_version="1.0",
        charter_hash="abc123def456",
        pinned_at=datetime(2025, 1, 1, 0, 0, 0),
    )
    return OperationContext(
        actor_id="test-actor",
        actor_role="admin",
        policy_pin=pin,
        trace_id="trace-001",
        span_id="span-001",
    )


def _make_reg(
    tool: str,
    gate_fn: str = "check_permission",
    enforcement: Enforcement = Enforcement.HARD,
    charter_ref: str = "charter-001",
) -> GateRegistration:
    return GateRegistration(
        target_tool=tool,
        gate_function=gate_fn,
        enforcement=enforcement,
        charter_ref=charter_ref,
    )


# ---------------------------------------------------------------------------
# 1. GateResult serialization round-trip
# ---------------------------------------------------------------------------


class TestGateResultSerializationRoundTrip:
    def test_gate_result_serialization_round_trip(self):
        """All fields survive to_dict() -> from_dict()."""
        original = GateResult(
            verdict=GateVerdict.DENY,
            justification="permission denied for tool",
            gate_id="perm_gate_v1",
            policy_ref="policy-xyz-123",
            evidence_ref="ev-abc-456",
            elapsed_ms=42.5,
        )
        d = original.to_dict()
        restored = GateResult.from_dict(d)
        assert restored.verdict == original.verdict
        assert restored.justification == original.justification
        assert restored.gate_id == original.gate_id
        assert restored.policy_ref == original.policy_ref
        assert restored.evidence_ref == original.evidence_ref
        assert restored.elapsed_ms == original.elapsed_ms

    def test_serialization_stores_verdict_as_lowercase_string(self):
        """to_dict() stores verdict as the enum's .value string."""
        for verdict, expected in [
            (GateVerdict.ALLOW, "allow"),
            (GateVerdict.DENY, "deny"),
            (GateVerdict.ADVISORY, "advisory"),
        ]:
            r = GateResult(verdict=verdict, justification="", gate_id="g")
            assert r.to_dict()["verdict"] == expected

    def test_from_dict_absent_optional_fields_use_defaults(self):
        """from_dict() with missing optional fields defaults to None / 0.0."""
        d = {"verdict": "allow", "justification": "ok", "gate_id": "g1"}
        r = GateResult.from_dict(d)
        assert r.policy_ref is None
        assert r.evidence_ref is None
        assert r.elapsed_ms == 0.0

    def test_round_trip_advisory_verdict(self):
        original = GateResult(
            verdict=GateVerdict.ADVISORY,
            justification="advisory flagged",
            gate_id="adv_gate",
        )
        restored = GateResult.from_dict(original.to_dict())
        assert restored.verdict == GateVerdict.ADVISORY


# ---------------------------------------------------------------------------
# 2. GateExecutor - ALLOW
# ---------------------------------------------------------------------------


class TestGateExecutorAllow:
    def test_gate_executor_allow_empty_registrations(self):
        """No registrations -> ALLOW."""
        result = GateExecutor([]).evaluate("any_tool", _make_ctx())
        assert result.verdict == GateVerdict.ALLOW

    def test_gate_executor_allow_no_matching_tool(self):
        """Registrations exist but none match the tool -> ALLOW."""
        result = GateExecutor([_make_reg("other_tool")]).evaluate("my_tool", _make_ctx())
        assert result.verdict == GateVerdict.ALLOW

    def test_allow_justification_mentions_tool(self):
        result = GateExecutor([]).evaluate("special_tool", _make_ctx())
        assert "special_tool" in result.justification


# ---------------------------------------------------------------------------
# 3. GateExecutor - DENY short-circuits
# ---------------------------------------------------------------------------


class TestGateExecutorDenyShortCircuits:
    def test_gate_executor_deny_short_circuits(self):
        """First HARD gate short-circuits; second HARD gate not evaluated.

        Proof: result.gate_id == 'gate_first' means the loop returned on the
        first HARD match and never reached gate_second.
        """
        regs = [
            _make_reg("my_tool", gate_fn="gate_first", enforcement=Enforcement.HARD),
            _make_reg("my_tool", gate_fn="gate_second", enforcement=Enforcement.HARD),
        ]
        result = GateExecutor(regs).evaluate("my_tool", _make_ctx())
        assert result.verdict == GateVerdict.DENY
        assert result.gate_id == "gate_first", (
            "gate_id must match the first HARD gate, proving the second was not evaluated"
        )

    def test_soft_before_hard_still_denies_at_hard(self):
        """SOFT gate collects advisory but HARD gate short-circuits with DENY."""
        regs = [
            _make_reg("my_tool", gate_fn="soft_1", enforcement=Enforcement.SOFT),
            _make_reg("my_tool", gate_fn="hard_1", enforcement=Enforcement.HARD),
            _make_reg("my_tool", gate_fn="soft_2", enforcement=Enforcement.SOFT),
        ]
        result = GateExecutor(regs).evaluate("my_tool", _make_ctx())
        assert result.verdict == GateVerdict.DENY
        assert result.gate_id == "hard_1"


# ---------------------------------------------------------------------------
# 4. GateExecutor - ADVISORY
# ---------------------------------------------------------------------------


class TestGateExecutorAdvisory:
    def test_gate_executor_advisory_soft_enforcement(self):
        """SOFT enforcement -> ADVISORY verdict; execution not blocked."""
        result = GateExecutor(
            [_make_reg("my_tool", gate_fn="soft_gate", enforcement=Enforcement.SOFT)]
        ).evaluate("my_tool", _make_ctx())
        assert result.verdict == GateVerdict.ADVISORY

    def test_gate_executor_advisory_enforcement_enum(self):
        """Enforcement.ADVISORY also produces ADVISORY verdict."""
        result = GateExecutor(
            [_make_reg("my_tool", gate_fn="adv_gate", enforcement=Enforcement.ADVISORY)]
        ).evaluate("my_tool", _make_ctx())
        assert result.verdict == GateVerdict.ADVISORY

    def test_multiple_advisories_aggregated_in_justification(self):
        regs = [
            _make_reg("my_tool", gate_fn="adv_1", enforcement=Enforcement.SOFT),
            _make_reg("my_tool", gate_fn="adv_2", enforcement=Enforcement.SOFT),
        ]
        result = GateExecutor(regs).evaluate("my_tool", _make_ctx())
        assert result.verdict == GateVerdict.ADVISORY
        assert "2 advisory" in result.justification


# ---------------------------------------------------------------------------
# 5. execute_governed - full pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_governed_full_pipeline():
    """Full pipeline: ADVISORY gate + execution + evidence sidecar recorded."""
    manager = ActionManager(_tool_add)
    manager.governance_artifacts = [
        _make_reg("_tool_add", gate_fn="audit_gate", enforcement=Enforcement.SOFT)
    ]
    chain = EvidenceChain()
    manager.evidence_chain = chain
    ctx = _make_ctx()

    result = await manager.execute_governed("_tool_add", {"x": 3, "y": 4}, ctx)

    assert result == 7
    # advisory node (e) + post-execution sidecar (g) = at least 2 nodes
    assert chain.node_count >= 2
    last_node = list(chain.nodes)[-1]
    assert last_node.content["tool_name"] == "_tool_add"


# ---------------------------------------------------------------------------
# 6. execute_governed - no ctx fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_governed_no_ctx_fallback():
    """ctx=None -> execute_governed falls back to execute(), tool runs without error."""
    manager = ActionManager(_tool_multiply)
    manager.governance_artifacts = [_make_reg("_tool_multiply", enforcement=Enforcement.HARD)]

    result = await manager.execute_governed("_tool_multiply", {"a": 6, "b": 7}, ctx=None)
    assert result == 42


# ---------------------------------------------------------------------------
# 7. Bypass adversarial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bypass_adversarial():
    """Document the chosen fallback behavior when ctx is None.

    Implementation choice: execute_governed falls back to execute() when ctx is
    None, even if HARD gate registrations exist. Governance is opt-in via ctx.

    Adversarial validation: with a valid ctx the same gate causes DENY.
    """
    manager = ActionManager(_tool_op)
    manager.governance_artifacts = [
        _make_reg("_tool_op", gate_fn="hard_gate", enforcement=Enforcement.HARD)
    ]

    # Without ctx: falls back gracefully — no GovernanceViolationError
    result_no_ctx = await manager.execute_governed("_tool_op", {"x": 5}, ctx=None)
    assert result_no_ctx == 10

    # With ctx: the HARD gate raises GovernanceViolationError
    manager.evidence_chain = EvidenceChain()
    with pytest.raises(GovernanceViolationError) as exc_info:
        await manager.execute_governed("_tool_op", {"x": 5}, ctx=_make_ctx())
    assert exc_info.value.result.verdict == GateVerdict.DENY


# ---------------------------------------------------------------------------
# 8. GovernanceViolationError
# ---------------------------------------------------------------------------


def test_governance_violation_error():
    """GovernanceViolationError holds .result and str() contains gate_id."""
    gate_result = GateResult(
        verdict=GateVerdict.DENY,
        justification="classified data access blocked",
        gate_id="classified_gate_v2",
    )
    error = GovernanceViolationError(gate_result)
    assert error.result is gate_result
    assert "classified_gate_v2" in str(error)
    assert isinstance(error, Exception)


def test_governance_violation_error_message_contains_justification():
    gate_result = GateResult(
        verdict=GateVerdict.DENY,
        justification="permission denied for this tool",
        gate_id="perm_gate",
    )
    error = GovernanceViolationError(gate_result)
    assert "permission denied for this tool" in str(error)


# ---------------------------------------------------------------------------
# Additional: governed_tool decorator
# ---------------------------------------------------------------------------


def test_governed_tool_produces_tool_with_governance_meta():
    from lionagi.protocols.action.tool import Tool

    my_tool = governed_tool(permissions=["read:code"], gate_ids=["pii_check"])(_tool_governed_func)
    assert isinstance(my_tool, Tool)
    assert my_tool.governance_meta is not None
    assert "read:code" in my_tool.governance_meta["required_permissions"]
    assert "pii_check" in my_tool.governance_meta["gate_ids"]


def test_governed_tool_default_meta_values():
    my_tool = governed_tool()(_tool_no_params)
    meta = my_tool.governance_meta
    assert meta["required_permissions"] == []
    assert meta["gate_ids"] == []
    assert "evidence_level" in meta
    assert meta["evidence_level"] is None
    assert meta["audit_classification"] == "standard"


# ---------------------------------------------------------------------------
# Additional: evidence recorded on deny
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deny_records_evidence_node_before_raising():
    """DENY records a denial EvidenceNode into the chain before raising."""
    manager = ActionManager(_tool_secret_op)
    manager.governance_artifacts = [
        _make_reg("_tool_secret_op", gate_fn="deny_gate", enforcement=Enforcement.HARD)
    ]
    chain = EvidenceChain()
    manager.evidence_chain = chain
    ctx = _make_ctx()

    with pytest.raises(GovernanceViolationError):
        await manager.execute_governed("_tool_secret_op", {"x": 99}, ctx)

    assert chain.node_count == 1
    denial_node = list(chain.nodes)[0]
    assert denial_node.content["event"] == "gate_deny"
    assert denial_node.content["tool_name"] == "_tool_secret_op"


# ---------------------------------------------------------------------------
# Required Scenario 1 (standalone): GateResult serialization round-trip
# ---------------------------------------------------------------------------


def test_gate_result_serialization_round_trip():
    """All GateResult fields survive to_dict() → from_dict() round-trip."""
    original = GateResult(
        verdict=GateVerdict.DENY,
        justification="permission denied for tool",
        gate_id="perm_gate_v1",
        policy_ref="policy-xyz-123",
        evidence_ref="ev-abc-456",
        elapsed_ms=42.5,
    )
    d = original.to_dict()
    restored = GateResult.from_dict(d)

    assert restored.verdict == original.verdict
    assert restored.justification == original.justification
    assert restored.gate_id == original.gate_id
    assert restored.policy_ref == original.policy_ref
    assert restored.evidence_ref == original.evidence_ref
    assert restored.elapsed_ms == original.elapsed_ms


# ---------------------------------------------------------------------------
# Required Scenario 2 (standalone): GateExecutor ALLOW
# ---------------------------------------------------------------------------


def test_gate_executor_allow():
    """GateRegistrations that do not match the target tool yield ALLOW verdict."""
    regs = [
        _make_reg("other_tool", gate_fn="irrelevant_gate", enforcement=Enforcement.HARD),
    ]
    result = GateExecutor(regs).evaluate("my_tool", _make_ctx())
    assert result.verdict == GateVerdict.ALLOW


# ---------------------------------------------------------------------------
# Required Scenario 3 (standalone): GateExecutor DENY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_executor_deny():
    """HARD gate returns GateResult(DENY) from evaluate(); execute_governed raises.

    GateExecutor.evaluate() returns DENY (does not raise).
    execute_governed() raises GovernanceViolationError carrying .result.
    """
    deny_reg = _make_reg("_tool_op", gate_fn="block_gate", enforcement=Enforcement.HARD)

    # evaluate() returns DENY — does not raise by itself
    deny_result = GateExecutor([deny_reg]).evaluate("_tool_op", {})
    assert deny_result.verdict == GateVerdict.DENY
    assert deny_result.gate_id == "block_gate"

    # execute_governed() raises GovernanceViolationError carrying .result
    manager = ActionManager(_tool_op)
    manager.governance_artifacts = [deny_reg]
    manager.evidence_chain = EvidenceChain()

    with pytest.raises(GovernanceViolationError) as exc_info:
        await manager.execute_governed("_tool_op", {"x": 1}, _make_ctx())

    assert exc_info.value.result.verdict == GateVerdict.DENY
    assert exc_info.value.result.gate_id == "block_gate"


# ---------------------------------------------------------------------------
# Required Scenario 4 (standalone): GateExecutor ADVISORY
# ---------------------------------------------------------------------------


def test_gate_executor_advisory():
    """SOFT/ADVISORY enforcement yields ADVISORY verdict; no exception raised."""
    result = GateExecutor(
        [_make_reg("my_tool", gate_fn="audit_gate", enforcement=Enforcement.SOFT)]
    ).evaluate("my_tool", {})

    assert result.verdict == GateVerdict.ADVISORY


# ---------------------------------------------------------------------------
# Required Scenario 6: execute_governed fails closed on DENY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_governed_fails_closed_on_deny():
    """Charter DENY gate propagates as GovernanceViolationError, never silently swallowed.

    # Fails-closed invariant: a charter DENY must propagate as exception, never be swallowed.
    """
    manager = ActionManager(_tool_secret_op)
    manager.governance_artifacts = [
        _make_reg("_tool_secret_op", gate_fn="deny_gate", enforcement=Enforcement.HARD)
    ]
    manager.evidence_chain = EvidenceChain()

    with pytest.raises(GovernanceViolationError):
        await manager.execute_governed("_tool_secret_op", {"x": 42}, _make_ctx())


# ---------------------------------------------------------------------------
# Required Scenario 7: Evidence sidecar attached after governed execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_sidecar_attached_after_governed_execution():
    """After successful execute_governed, the chain gains sidecar node(s) referencing tool.

    With SOFT enforcement: advisory node + execution sidecar = 2 new nodes.
    The execution sidecar (last node) contains tool_name in its content.
    """
    manager = ActionManager(_tool_multiply)
    manager.governance_artifacts = [
        _make_reg("_tool_multiply", gate_fn="audit_gate", enforcement=Enforcement.SOFT)
    ]
    chain = EvidenceChain()
    manager.evidence_chain = chain
    ctx = _make_ctx()

    count_before = chain.node_count
    await manager.execute_governed("_tool_multiply", {"a": 3, "b": 4}, ctx)
    count_after = chain.node_count

    assert count_after > count_before
    last_node = list(chain.nodes)[-1]
    assert last_node.content["tool_name"] == "_tool_multiply"


# ---------------------------------------------------------------------------
# Additional: governed_tool on async callable
# ---------------------------------------------------------------------------


async def _async_tool_double(x):
    return x * 2


@pytest.mark.asyncio
async def test_governed_tool_async_callable():
    """governed_tool works on async callables; execute_governed returns the result."""
    from lionagi.protocols.action.tool import Tool

    my_tool = governed_tool(
        permissions=["exec:async"],
        gate_ids=["async_gate"],
        audit_classification="high",
    )(_async_tool_double)
    assert isinstance(my_tool, Tool)
    meta = my_tool.governance_meta
    assert meta["required_permissions"] == ["exec:async"]
    assert meta["gate_ids"] == ["async_gate"]
    assert meta["audit_classification"] == "high"

    manager = ActionManager(my_tool)
    # ctx=None fallback — executes without governance overhead
    result = await manager.execute_governed("_async_tool_double", {"x": 21}, ctx=None)
    assert result == 42
