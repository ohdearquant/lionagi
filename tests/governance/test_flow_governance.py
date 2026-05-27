# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for P18 flow-governance integration.

Covers:
- TaskCertificate serialisation round-trip
- CertificateGrade computation (FULL / PARTIAL / FAILED)
- BudgetExceededError structured constructor
- GovernedFlowController with and without charter
- pre_op_check ALLOW and DENY
- post_op_record evidence recording
- mint_certificate
"""

from __future__ import annotations

import textwrap
import uuid
from datetime import datetime, timezone

import pytest

from lionagi.protocols.governance.certificate import CertificateGrade, TaskCertificate
from lionagi.protocols.governance.context import (
    BudgetExceededError,
    OperationBudget,
)
from lionagi.protocols.governance.flow_integration import GovernedFlowController
from lionagi.protocols.governance.gates import GateVerdict, GovernanceViolationError

# ──────────────────────────────────────────────────────────────────────────────
# Charter YAML fixtures
# ──────────────────────────────────────────────────────────────────────────────

# A charter with a HARD gate on "tool.risky" and a SOFT (advisory) gate on
# "tool.watched". "tool.safe" is registered but has no gate constraint.
GOVERNED_CHARTER = textwrap.dedent("""\
    charter_dsl: "0.1"
    kind: agent_charter
    metadata:
      charter_id: charter.test.flow
      version: "1.0.0"
      status: draft
      policy_release: policy.gov.v1
      authored_by: human:governance
      implemented_by: agent:implementer
      ratification: {}
    agents:
      - agent_id: agent.worker
        actor_id_source: branch_id
        role: worker
        allowed_models: [openai:gpt-5.4]
        allowed_tools: [tool.safe, tool.watched, tool.risky]
    registry:
      snapshot: ratification_time
      entries:
        - category: tool
          value: tool.safe
          scope: agent
          scope_id: agent.worker
          reason: "Safe tool."
          evidence_refs: [ev.safe]
        - category: tool
          value: tool.watched
          scope: agent
          scope_id: agent.worker
          reason: "Watched tool."
          evidence_refs: [ev.watched]
        - category: tool
          value: tool.risky
          scope: agent
          scope_id: agent.worker
          reason: "Risky tool."
          evidence_refs: [ev.risky]
    constraints:
      - constraint_id: gate.hard.risky
        description: "Block risky tool."
        gate_id: block_risky
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: action
          action: tool_call
          tools: [tool.risky]
        evidence:
          required: [GateResult]
      - constraint_id: gate.soft.watched
        description: "Warn on watched tool."
        gate_id: warn_watched
        manager_surface: ActionManager
        enforcement: soft
        attach:
          level: action
          action: tool_call
          tools: [tool.watched]
        evidence:
          required: [GateResult]
    sod:
      active: true
      rules: []
    permissions:
      default: deny
      resolution:
        specificity_order: [resource, role, tenant, global]
        tie: deny
      allow:
        - rule_id: allow.worker.tools
          scope: role
          roles: [worker]
          action: tool_call
          tools: [tool.safe, tool.watched, tool.risky]
          requires_evidence: [GateResult]
          because: "Worker needs tools."
      deny: []
    trace:
      stamp: [charter_id, policy_release, agent_id, role]
      require_spans:
        - governance.operation
      require_evidence:
        - GateResult
""")


def _session_id() -> str:
    return uuid.uuid4().hex


# ──────────────────────────────────────────────────────────────────────────────
# 1. TaskCertificate serialisation
# ──────────────────────────────────────────────────────────────────────────────


def test_certificate_round_trip_serialization():
    now = datetime.now(tz=timezone.utc)
    cert = TaskCertificate(
        certificate_id="abc123",
        session_id="sess-001",
        charter_id="charter.test",
        charter_hash="deadbeef" * 8,
        grade=CertificateGrade.FULL,
        evidence_chain_head="0" * 64,
        started_at=now,
        completed_at=now,
        op_count=5,
        ops_allowed=5,
        gate_results_summary={"allow": 5},
    )
    d = cert.to_dict()
    assert d["grade"] == "full"
    assert d["op_count"] == 5
    assert d["ops_allowed"] == 5
    assert d["gate_results_summary"] == {"allow": 5}

    restored = TaskCertificate.from_dict(d)
    assert restored.certificate_id == cert.certificate_id
    assert restored.grade == CertificateGrade.FULL
    assert restored.op_count == 5
    assert restored.gate_results_summary == {"allow": 5}
    # Datetime round-trip preserves the value (possibly with or without tzinfo)
    assert restored.started_at.isoformat() == cert.started_at.isoformat()


# ──────────────────────────────────────────────────────────────────────────────
# 2. Grade computation
# ──────────────────────────────────────────────────────────────────────────────


def test_certificate_grade_full():
    """No denials and no advisories → FULL."""
    controller = GovernedFlowController(GOVERNED_CHARTER, session_id=_session_id())
    # Only access safe tool (no gate on it).
    result = controller.pre_op_check("tool.safe", ctx=None)
    assert result.verdict == GateVerdict.ALLOW
    controller.post_op_record("tool.safe", "args", "res", result, 1.0)

    cert = controller.mint_certificate()
    assert cert.grade == CertificateGrade.FULL
    assert cert.op_count == 1
    assert cert.ops_allowed == 1


def test_certificate_grade_partial():
    """Advisory-only gate fires → PARTIAL."""
    controller = GovernedFlowController(GOVERNED_CHARTER, session_id=_session_id())
    # tool.watched has a SOFT gate → ADVISORY verdict.
    result = controller.pre_op_check("tool.watched", ctx=None)
    assert result.verdict == GateVerdict.ADVISORY
    controller.post_op_record("tool.watched", "args", "res", result, 1.0)

    cert = controller.mint_certificate()
    assert cert.grade == CertificateGrade.PARTIAL
    assert cert.gate_results_summary.get("advisory", 0) > 0
    assert cert.gate_results_summary.get("deny", 0) == 0


def test_certificate_grade_failed():
    """Hard denial gate fires → FAILED."""
    controller = GovernedFlowController(GOVERNED_CHARTER, session_id=_session_id())
    # tool.risky has a HARD gate → DENY verdict.
    result = controller.pre_op_check("tool.risky", ctx=None)
    assert result.verdict == GateVerdict.DENY
    controller.post_op_record("tool.risky", "args", "res", result, 0.5)

    cert = controller.mint_certificate()
    assert cert.grade == CertificateGrade.FAILED
    assert cert.gate_results_summary.get("deny", 0) > 0


# ──────────────────────────────────────────────────────────────────────────────
# 3. BudgetExceededError
# ──────────────────────────────────────────────────────────────────────────────


def test_budget_exceeded_error():
    """BudgetExceededError carries budget and requested."""
    budget = OperationBudget(max_calls=2)
    err = BudgetExceededError(budget, 3)
    assert err.budget is budget
    assert err.requested == 3
    assert "remaining" in str(err) or "exceeded" in str(err).lower()


def test_budget_exceeded_error_no_budget():
    """BudgetExceededError with no budget still constructs."""
    err = BudgetExceededError()
    assert err.budget is None
    assert err.requested == 1


def test_budget_exceeded_error_raised_by_record_usage():
    """OperationBudget.record_usage raises BudgetExceededError on breach."""
    budget = OperationBudget(max_calls=1)
    budget.record_usage(calls=1)  # calls_used == 1, max_calls == 1 → still ok (> not >=)
    with pytest.raises(BudgetExceededError) as exc_info:
        budget.record_usage(calls=1)  # calls_used becomes 2 > max_calls=1
    # record_usage passes self as budget_or_msg positional arg.
    assert exc_info.value.budget is budget


# ──────────────────────────────────────────────────────────────────────────────
# 4. GovernedFlowController — pre_op_check
# ──────────────────────────────────────────────────────────────────────────────


def test_governed_flow_controller_pre_op_allows():
    """A tool with no matching gate receives ALLOW."""
    controller = GovernedFlowController(GOVERNED_CHARTER, session_id=_session_id())
    result = controller.pre_op_check("tool.safe", ctx=None)
    assert result.verdict == GateVerdict.ALLOW


def test_governed_flow_controller_pre_op_denies_hard():
    """A tool matching a HARD gate receives DENY."""
    controller = GovernedFlowController(GOVERNED_CHARTER, session_id=_session_id())
    result = controller.pre_op_check("tool.risky", ctx=None)
    assert result.verdict == GateVerdict.DENY
    # Callers can raise GovernanceViolationError from the result.
    with pytest.raises(GovernanceViolationError):
        raise GovernanceViolationError(result)


# ──────────────────────────────────────────────────────────────────────────────
# 5. GovernedFlowController — post_op_record
# ──────────────────────────────────────────────────────────────────────────────


def test_governed_flow_controller_post_op_records_evidence():
    """post_op_record increments counters and adds a node to the chain."""
    controller = GovernedFlowController(GOVERNED_CHARTER, session_id=_session_id())
    result = controller.pre_op_check("tool.safe", ctx=None)
    controller.post_op_record("tool.safe", "argshash", "reshash", result, 2.5)

    assert controller._op_count == 1
    assert controller._ops_allowed == 1
    # Evidence chain has one node.
    assert controller._evidence_chain is not None
    assert controller._evidence_chain.node_count == 1


def test_governed_flow_controller_post_op_multiple():
    """Multiple ops accumulate correctly in counters."""
    controller = GovernedFlowController(GOVERNED_CHARTER, session_id=_session_id())

    r1 = controller.pre_op_check("tool.safe", ctx=None)
    controller.post_op_record("tool.safe", "a1", "r1", r1, 1.0)

    r2 = controller.pre_op_check("tool.watched", ctx=None)
    controller.post_op_record("tool.watched", "a2", "r2", r2, 2.0)

    assert controller._op_count == 2
    assert controller._ops_allowed == 1  # only safe was ALLOW; watched was ADVISORY
    assert controller._evidence_chain.node_count == 2


# ──────────────────────────────────────────────────────────────────────────────
# 6. GovernedFlowController — mint_certificate
# ──────────────────────────────────────────────────────────────────────────────


def test_governed_flow_controller_mints_certificate():
    """mint_certificate returns a valid TaskCertificate."""
    sid = _session_id()
    controller = GovernedFlowController(GOVERNED_CHARTER, session_id=sid)
    result = controller.pre_op_check("tool.safe", ctx=None)
    controller.post_op_record("tool.safe", "a", "r", result, 1.0)

    cert = controller.mint_certificate()
    assert isinstance(cert, TaskCertificate)
    assert cert.session_id == sid
    assert cert.charter_id == "charter.test.flow"
    assert len(cert.charter_hash) == 64  # sha256 hex
    assert cert.grade == CertificateGrade.FULL
    assert cert.op_count == 1
    assert cert.ops_allowed == 1
    assert cert.evidence_chain_head != "0" * 64  # chain was appended to


# ──────────────────────────────────────────────────────────────────────────────
# 7. Backward compatibility — no charter
# ──────────────────────────────────────────────────────────────────────────────


def test_no_charter_skips_governance():
    """With charter=None the controller is a pass-through."""
    controller = GovernedFlowController(charter=None, session_id=_session_id())

    result = controller.pre_op_check("any.tool", ctx=None)
    assert result.verdict == GateVerdict.ALLOW

    controller.post_op_record("any.tool", "ah", "rh", result, 0.5)

    cert = controller.mint_certificate()
    assert isinstance(cert, TaskCertificate)
    # Without a charter the grade defaults to FULL (no denials).
    assert cert.grade == CertificateGrade.FULL
    assert cert.charter_id == ""
    assert cert.charter_hash == ""
    # Evidence chain is None; head is empty string.
    assert cert.evidence_chain_head == ""
