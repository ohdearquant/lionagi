# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for P18 Session.flow() governance integration.

Exercises governed_flow() with:
- charter provided → certificate minted
- no charter → no governance overhead, None certificate
- DENY verdict → stops op execution (raise / skip modes)
- ADVISORY verdict → execution continues, grade downgrades to PARTIAL
- certificate op_count matches executed operations
- evidence chain has an entry per recorded op
"""

from __future__ import annotations

import textwrap
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi.protocols.governance.certificate import CertificateGrade, TaskCertificate
from lionagi.protocols.governance.gates import GateVerdict, GovernanceViolationError
from lionagi.protocols.governance.session_integration import governed_flow
from lionagi.protocols.types import EventStatus

CHARTER_FULL = textwrap.dedent("""\
    charter_dsl: "0.1"
    kind: agent_charter
    metadata:
      charter_id: charter.session.test
      version: "1.0.0"
      status: draft
      policy_release: policy.test.v1
      authored_by: human:test
      implemented_by: agent:implementer
      ratification: {}
    agents:
      - agent_id: agent.flow
        actor_id_source: branch_id
        role: flow_worker
        allowed_models: [openai:gpt-5.4]
        allowed_tools: [op.safe, op.watched, op.blocked]
    registry:
      snapshot: ratification_time
      entries:
        - category: tool
          value: op.safe
          scope: agent
          scope_id: agent.flow
          reason: safe
          evidence_refs: [ev.1]
        - category: tool
          value: op.watched
          scope: agent
          scope_id: agent.flow
          reason: watched
          evidence_refs: [ev.2]
        - category: tool
          value: op.blocked
          scope: agent
          scope_id: agent.flow
          reason: blocked
          evidence_refs: [ev.3]
    constraints:
      - constraint_id: gate.hard.blocked
        description: "Block op.blocked."
        gate_id: block_op
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: action
          action: tool_call
          tools: [op.blocked]
        evidence:
          required: [GateResult]
      - constraint_id: gate.soft.watched
        description: "Warn on op.watched."
        gate_id: warn_op
        manager_surface: ActionManager
        enforcement: soft
        attach:
          level: action
          action: tool_call
          tools: [op.watched]
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
        - rule_id: allow.flow.tools
          scope: role
          roles: [flow_worker]
          action: tool_call
          tools: [op.safe, op.watched, op.blocked]
          requires_evidence: [GateResult]
          because: "Flow worker needs ops."
      deny: []
    trace:
      stamp: [charter_id, policy_release, agent_id, role]
      require_spans:
        - governance.operation
      require_evidence:
        - GateResult
""")


def _make_session() -> MagicMock:
    session = MagicMock()
    session.id = uuid.uuid4()

    branch = MagicMock()
    branch.id = uuid.uuid4()
    branch.name = "default"
    branch.metadata = {}
    branch.clone = MagicMock(return_value=branch)

    branches = MagicMock()
    branches.__contains__ = MagicMock(return_value=True)
    branches.__iter__ = MagicMock(return_value=iter([branch]))
    branches.collections = {}
    branches.progression = []
    branches.async_lock = MagicMock()
    branches.async_lock.__aenter__ = AsyncMock(return_value=None)
    branches.async_lock.__aexit__ = AsyncMock(return_value=None)

    session.branches = branches
    session.default_branch = branch
    return session


def _make_op(reference_id: str, result: Any = "done") -> MagicMock:
    """Build a mock Operation that completes with *result*."""
    op = MagicMock()
    op.id = uuid.uuid4()
    op.operation = "operate"
    op.metadata = {"reference_id": reference_id}
    op.parameters = {}
    op.response = result
    op.branch_id = None

    execution = MagicMock()
    execution.status = EventStatus.PENDING
    execution.response = result
    execution.error = None
    op.execution = execution

    return op


def _make_graph(ops: list[MagicMock]) -> MagicMock:
    graph = MagicMock()
    graph.is_acyclic = MagicMock(return_value=True)
    graph.internal_nodes = {op.id: op for op in ops}
    graph.internal_edges = {}
    graph.get_predecessors = MagicMock(return_value=[])
    return graph


class _FakeCapacityLimiter:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


async def _run_governed(
    session,
    graph,
    charter=CHARTER_FULL,
    on_deny="raise",
    **kw,
):
    return await governed_flow(
        session,
        graph,
        charter=charter,
        on_deny=on_deny,
        **kw,
    )


# ── Helpers: patch the executor's inner machinery ─────────────────────────────


def _patch_executor(ops: list[MagicMock]):
    """
    Patch DependencyAwareExecutor so it bypasses the real concurrency engine.
    We manually call _execute_operation to test governance wrapping.
    """
    pass  # patching happens inside each test with monkeypatch / patch


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_charter_returns_none_certificate():
    """Without a charter, governed_flow returns (result, None) and bypasses governance."""
    session = _make_session()
    op = _make_op("op.safe", result="ok")
    graph = _make_graph([op])

    expected = {"completed_operations": [op.id], "operation_results": {op.id: "ok"}}

    with patch(
        "lionagi.protocols.governance.session_integration._ungoverned_flow",
        new=AsyncMock(return_value=expected),
    ):
        result, cert = await governed_flow(session, graph, charter=None)

    assert cert is None
    assert result == expected


@pytest.mark.asyncio
async def test_flow_with_charter_mints_certificate():
    """A charter-governed flow always mints a TaskCertificate."""
    session = _make_session()
    op = _make_op("op.safe", result="value")
    graph = _make_graph([op])

    from lionagi.protocols.governance.session_integration import _GovernedExecutor

    with patch.object(
        _GovernedExecutor,
        "execute",
        new_callable=AsyncMock,
        return_value={
            "completed_operations": [op.id],
            "operation_results": {op.id: "value"},
            "final_context": {},
            "skipped_operations": [],
        },
    ):
        result, cert = await _run_governed(session, graph)

    assert isinstance(cert, TaskCertificate)
    assert cert.session_id == str(session.id)
    assert cert.charter_id == "charter.session.test"


@pytest.mark.asyncio
async def test_certificate_grade_full_for_safe_ops():
    """Operations with no matching gate yield FULL certificate."""
    session = _make_session()
    op = _make_op("op.safe")
    graph = _make_graph([op])

    from lionagi.protocols.governance.session_integration import _GovernedExecutor

    async def fake_execute(self):
        gate = self._controller.pre_op_check("op.safe", ctx=None)
        assert gate.verdict == GateVerdict.ALLOW
        self._controller.post_op_record("op.safe", "ah", "rh", gate, 1.0)
        return {
            "completed_operations": [op.id],
            "operation_results": {op.id: "ok"},
            "final_context": {},
            "skipped_operations": [],
        }

    with patch.object(_GovernedExecutor, "execute", new=fake_execute):
        result, cert = await _run_governed(session, graph)

    assert cert is not None
    assert cert.grade == CertificateGrade.FULL


@pytest.mark.asyncio
async def test_deny_raises_governance_violation_error():
    """DENY verdict with on_deny='raise' propagates GovernanceViolationError."""
    session = _make_session()
    op = _make_op("op.blocked")
    graph = _make_graph([op])

    from lionagi.protocols.governance.flow_integration import GovernedFlowController
    from lionagi.protocols.governance.session_integration import (
        _RAISE,
        _GovernedExecutor,
    )

    async def fake_execute(self):
        gate = self._controller.pre_op_check("op.blocked", ctx=None)
        assert gate.verdict == GateVerdict.DENY
        if self._on_deny == _RAISE:
            raise GovernanceViolationError(gate)
        return {
            "completed_operations": [],
            "operation_results": {},
            "final_context": {},
            "skipped_operations": [],
        }

    with patch.object(_GovernedExecutor, "execute", new=fake_execute):
        with pytest.raises(GovernanceViolationError) as exc_info:
            await _run_governed(session, graph, on_deny="raise")

    assert exc_info.value.result.verdict == GateVerdict.DENY


@pytest.mark.asyncio
async def test_deny_skip_marks_op_skipped_not_raises():
    """DENY verdict with on_deny='skip' skips the op without raising."""
    session = _make_session()
    op = _make_op("op.blocked")
    graph = _make_graph([op])

    from lionagi.protocols.governance.session_integration import _SKIP, _GovernedExecutor

    async def fake_execute(self):
        gate = self._controller.pre_op_check("op.blocked", ctx=None)
        assert gate.verdict == GateVerdict.DENY
        self._controller.post_op_record("op.blocked", "", "", gate, 0.0)
        return {
            "completed_operations": [],
            "operation_results": {},
            "final_context": {},
            "skipped_operations": [op.id],
        }

    with patch.object(_GovernedExecutor, "execute", new=fake_execute):
        result, cert = await _run_governed(session, graph, on_deny="skip")

    assert op.id in result["skipped_operations"]
    assert cert is not None
    assert cert.grade == CertificateGrade.FAILED


@pytest.mark.asyncio
async def test_advisory_allows_execution_downgrades_to_partial():
    """ADVISORY (soft gate) allows execution but downgrades certificate to PARTIAL."""
    session = _make_session()
    op = _make_op("op.watched")
    graph = _make_graph([op])

    from lionagi.protocols.governance.session_integration import _GovernedExecutor

    async def fake_execute(self):
        gate = self._controller.pre_op_check("op.watched", ctx=None)
        assert gate.verdict == GateVerdict.ADVISORY
        self._controller.post_op_record("op.watched", "ah", "rh", gate, 1.5)
        return {
            "completed_operations": [op.id],
            "operation_results": {op.id: "watched-result"},
            "final_context": {},
            "skipped_operations": [],
        }

    with patch.object(_GovernedExecutor, "execute", new=fake_execute):
        result, cert = await _run_governed(session, graph)

    assert cert is not None
    assert cert.grade == CertificateGrade.PARTIAL
    assert cert.gate_results_summary.get("advisory", 0) > 0
    assert cert.gate_results_summary.get("deny", 0) == 0


@pytest.mark.asyncio
async def test_certificate_op_count_matches_recorded_ops():
    """Certificate op_count reflects the number of post_op_record calls."""
    session = _make_session()
    ops = [_make_op(f"op.safe_{i}") for i in range(3)]
    graph = _make_graph(ops)

    from lionagi.protocols.governance.session_integration import _GovernedExecutor

    async def fake_execute(self):
        for i in range(3):
            gate = self._controller.pre_op_check("op.safe", ctx=None)
            self._controller.post_op_record(f"op.safe_{i}", "ah", "rh", gate, float(i))
        return {
            "completed_operations": [o.id for o in ops],
            "operation_results": {o.id: f"res_{i}" for i, o in enumerate(ops)},
            "final_context": {},
            "skipped_operations": [],
        }

    with patch.object(_GovernedExecutor, "execute", new=fake_execute):
        result, cert = await _run_governed(session, graph)

    assert cert is not None
    assert cert.op_count == 3
    assert cert.ops_allowed == 3
    assert cert.grade == CertificateGrade.FULL


@pytest.mark.asyncio
async def test_evidence_chain_has_entry_per_op():
    """Evidence chain accumulates one node per post_op_record call."""
    session = _make_session()
    ops = [_make_op(f"op.safe_{i}") for i in range(2)]
    graph = _make_graph(ops)

    from lionagi.protocols.governance.flow_integration import GovernedFlowController
    from lionagi.protocols.governance.session_integration import _GovernedExecutor

    captured_controller: list[GovernedFlowController] = []

    original_init = GovernedFlowController.__init__

    def recording_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured_controller.append(self)

    async def fake_execute(self):
        for i in range(2):
            gate = self._controller.pre_op_check("op.safe", ctx=None)
            self._controller.post_op_record(f"op.safe_{i}", "ah", "rh", gate, 1.0)
        return {
            "completed_operations": [o.id for o in ops],
            "operation_results": {},
            "final_context": {},
            "skipped_operations": [],
        }

    with (
        patch.object(GovernedFlowController, "__init__", new=recording_init),
        patch.object(_GovernedExecutor, "execute", new=fake_execute),
    ):
        result, cert = await _run_governed(session, graph)

    assert captured_controller
    ctrl = captured_controller[0]
    assert ctrl._evidence_chain is not None
    assert ctrl._evidence_chain.node_count == 2


@pytest.mark.asyncio
async def test_certificate_contains_correct_charter_id():
    """Minted certificate embeds the charter_id from the YAML."""
    session = _make_session()
    op = _make_op("op.safe")
    graph = _make_graph([op])

    from lionagi.protocols.governance.session_integration import _GovernedExecutor

    async def fake_execute(self):
        return {
            "completed_operations": [],
            "operation_results": {},
            "final_context": {},
            "skipped_operations": [],
        }

    with patch.object(_GovernedExecutor, "execute", new=fake_execute):
        _, cert = await _run_governed(session, graph)

    assert cert is not None
    assert cert.charter_id == "charter.session.test"
    assert len(cert.charter_hash) == 64


@pytest.mark.asyncio
async def test_no_charter_delegates_to_flow_unchanged():
    """Without a charter, governed_flow calls the standard flow() function."""
    session = _make_session()
    op = _make_op("op.safe")
    graph = _make_graph([op])

    call_log: list = []

    async def fake_flow(**kw):
        call_log.append(kw)
        return {
            "completed_operations": [],
            "operation_results": {},
            "final_context": {},
            "skipped_operations": [],
        }

    with patch(
        "lionagi.protocols.governance.session_integration._ungoverned_flow",
        new=AsyncMock(side_effect=fake_flow),
    ):
        result, cert = await governed_flow(
            session,
            graph,
            charter=None,
            context={"x": 1},
        )

    assert cert is None
    assert len(call_log) == 1
    assert call_log[0]["context"] == {"x": 1}


@pytest.mark.asyncio
async def test_certificate_started_before_completed():
    """Certificate timestamps are ordered: started_at < completed_at."""
    session = _make_session()
    op = _make_op("op.safe")
    graph = _make_graph([op])

    from lionagi.protocols.governance.session_integration import _GovernedExecutor

    async def fake_execute(self):
        return {
            "completed_operations": [],
            "operation_results": {},
            "final_context": {},
            "skipped_operations": [],
        }

    with patch.object(_GovernedExecutor, "execute", new=fake_execute):
        _, cert = await _run_governed(session, graph)

    assert cert is not None
    assert cert.started_at <= cert.completed_at
