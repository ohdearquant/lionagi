# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the canonical per-node lifecycle signal contract (ADR-0077 / #1251).

Covers:
- lane_for() projection from signal sequences → NodeLifecycleState
- New signal constructors (NodeQueued, NodeAwaitingApproval, NodeEscalated)
- Terminal-state stickiness and retry-reset semantics
- awaiting_approval and escalated edges (required by spec)
- StructuredOutput(data=EscalationRequest) → escalated projection
- RunStart/RunEnd run-scoped fallback paths
"""

from __future__ import annotations

import pytest

from lionagi.casts.emission import EscalationRequest
from lionagi.session.signal import (
    GateDenied,
    MessageAdded,
    NodeAwaitingApproval,
    NodeCompleted,
    NodeEscalated,
    NodeFailed,
    NodeQueued,
    NodeStarted,
    RunEnd,
    RunFailed,
    RunStart,
    StructuredOutput,
    lane_for,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _queued(op_id: str = "op1") -> NodeQueued:
    return NodeQueued(op_id=op_id, name="test")


def _started(op_id: str = "op1") -> NodeStarted:
    return NodeStarted(op_id=op_id, name="test")


def _completed(op_id: str = "op1") -> NodeCompleted:
    return NodeCompleted(op_id=op_id, name="test")


def _failed(op_id: str = "op1") -> NodeFailed:
    return NodeFailed(op_id=op_id, name="test")


def _awaiting(op_id: str = "op1", reason: str = "needs approval") -> NodeAwaitingApproval:
    return NodeAwaitingApproval(op_id=op_id, name="test", reason=reason)


def _escalated(
    op_id: str = "op1", reason: str = "low confidence", route: str = "give_up"
) -> NodeEscalated:
    return NodeEscalated(op_id=op_id, name="test", reason=reason, route=route)


def _escalation_req(reason: str = "out of depth") -> EscalationRequest:
    return EscalationRequest(
        reason=reason, context={"trigger": "low_confidence", "confidence": 0.3}
    )


# ---------------------------------------------------------------------------
# Default state
# ---------------------------------------------------------------------------


def test_empty_stream_is_queued():
    assert lane_for([]) == "queued"


# ---------------------------------------------------------------------------
# Happy path: queued → running → succeeded
# ---------------------------------------------------------------------------


def test_full_happy_path():
    assert lane_for([_queued(), _started(), _completed()]) == "succeeded"


def test_started_then_completed():
    assert lane_for([_started(), _completed()]) == "succeeded"


def test_started_is_running():
    assert lane_for([_started()]) == "running"


def test_queued_is_queued():
    assert lane_for([_queued()]) == "queued"


# ---------------------------------------------------------------------------
# Failed edge
# ---------------------------------------------------------------------------


def test_started_then_failed():
    assert lane_for([_started(), _failed()]) == "failed"


def test_run_failed_maps_to_failed():
    assert lane_for([RunStart(), RunFailed(data=ValueError("boom"))]) == "failed"


# ---------------------------------------------------------------------------
# awaiting_approval edge (required by #1251)
# ---------------------------------------------------------------------------


def test_awaiting_approval_edge():
    assert lane_for([_started(), _awaiting()]) == "awaiting_approval"


def test_awaiting_then_completed():
    # Approval can be granted; node proceeds to succeed.
    assert lane_for([_queued(), _started(), _awaiting(), _started(), _completed()]) == "succeeded"


# ---------------------------------------------------------------------------
# escalated edge (required by #1251)
# ---------------------------------------------------------------------------


def test_escalated_edge_via_node_escalated():
    assert lane_for([_started(), _escalated()]) == "escalated"


def test_escalated_via_structured_output():
    # StructuredOutput carrying an EscalationRequest must project to escalated.
    req = _escalation_req()
    sig = StructuredOutput(data=req)
    assert lane_for([_started(), sig]) == "escalated"


def test_escalated_higher_tier_route():
    sig = _escalated(route="higher_tier")
    assert lane_for([_started(), sig]) == "escalated"


# ---------------------------------------------------------------------------
# Terminal-state stickiness
# ---------------------------------------------------------------------------


def test_succeeded_is_sticky_against_failed():
    # A NodeFailed after NodeCompleted must NOT override the terminal state.
    assert lane_for([_completed(), _failed()]) == "succeeded"


def test_failed_is_sticky_against_completed():
    assert lane_for([_failed(), _completed()]) == "failed"


def test_escalated_is_sticky_against_completed():
    assert lane_for([_escalated(), _completed()]) == "escalated"


def test_escalated_is_sticky_against_awaiting():
    assert lane_for([_escalated(), _awaiting()]) == "escalated"


# ---------------------------------------------------------------------------
# Terminal reset by new attempt
# ---------------------------------------------------------------------------


def test_terminal_reset_by_node_queued():
    # A new NodeQueued after a terminal state marks a retry attempt.
    assert lane_for([_completed(), _queued()]) == "queued"


def test_terminal_reset_by_node_started():
    # NodeStarted also marks a new attempt.
    assert lane_for([_failed(), _started()]) == "running"


def test_terminal_resets_then_completes():
    assert lane_for([_failed(), _queued(), _started(), _completed()]) == "succeeded"


# ---------------------------------------------------------------------------
# Non-state-bearing signals are ignored
# ---------------------------------------------------------------------------


def test_gate_denied_is_ignored_not_terminal():
    # GateDenied is a governance detail, not a lifecycle lane by itself.
    assert lane_for([_started(), GateDenied(data="denied")]) == "running"


def test_message_added_is_ignored():
    assert lane_for([_started(), MessageAdded(data="msg"), _completed()]) == "succeeded"


def test_none_data_signal_is_ignored():
    from lionagi.session.signal import Signal

    assert lane_for([_started(), Signal(data=None), _completed()]) == "succeeded"


# ---------------------------------------------------------------------------
# RunStart / RunEnd fallbacks
# ---------------------------------------------------------------------------


def test_run_start_maps_to_running():
    assert lane_for([RunStart()]) == "running"


def test_run_end_maps_to_succeeded():
    assert lane_for([RunStart(), RunEnd(data="result")]) == "succeeded"


# ---------------------------------------------------------------------------
# Mixed fine-grained and coarse signals
# ---------------------------------------------------------------------------


def test_node_signals_override_run_signals():
    # Node-level signals interleaved with run-scoped fallbacks — node wins.
    assert lane_for([RunStart(), _queued(), _started(), _completed()]) == "succeeded"


# ---------------------------------------------------------------------------
# New signal constructors — field defaults and type identity
# ---------------------------------------------------------------------------


def test_node_queued_defaults():
    sig = NodeQueued()
    assert sig.op_id == ""
    assert sig.name == ""
    assert sig.elapsed == 0.0


def test_node_awaiting_approval_defaults():
    sig = NodeAwaitingApproval()
    assert sig.op_id == ""
    assert sig.reason is None


def test_node_escalated_fields():
    sig = NodeEscalated(op_id="x", name="n", reason="low conf", route="give_up")
    assert sig.op_id == "x"
    assert sig.route == "give_up"


def test_signals_are_elements():
    """New signals inherit Element identity (have .id) for Flow/Pile storage."""
    from lionagi.protocols.generic.element import Element

    assert isinstance(NodeQueued(), Element)
    assert isinstance(NodeAwaitingApproval(), Element)
    assert isinstance(NodeEscalated(reason="r", route="give_up"), Element)
    # Each instance has a unique id
    a, b = NodeQueued(), NodeQueued()
    assert a.id != b.id
