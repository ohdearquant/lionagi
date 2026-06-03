# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for #1253 (EscalationRequest routing) and #1254 (confidence-gated completion).

#1253 contract:
  - EscalationRequest emitted on the bus while a ReactiveExecutor is running →
    observer routes to higher_tier re-dispatch (when escalation_tier is set)
    OR surfaces NodeEscalated(route="give_up") when no tier is configured /
    human_required=True.

#1254 contract:
  - below-target self-rating → ConfidenceGateEscalated raised (NOT completion)
  - at/above-target rating → (rating, result) returned normally
  - evidence_seeker given a second chance before escalation
"""

from __future__ import annotations

import pytest

from lionagi.casts.emission import EscalationRequest
from lionagi.operations.confidence_gate import (
    ConfidenceGateEscalated,
    ConfidenceGatePassed,
    ConfidenceRating,
    confidence_gated_completion,
)
from lionagi.operations.flow import _CURRENT_OP, ReactiveExecutor
from lionagi.operations.node import create_operation
from lionagi.protocols.graph.graph import Graph
from lionagi.session.session import Session
from lionagi.session.signal import NodeEscalated

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_executor(session: Session, escalation_tier: str | None = None) -> ReactiveExecutor:
    """Minimal ReactiveExecutor with an empty graph (no ops to run)."""
    return ReactiveExecutor(
        session=session,
        graph=Graph(),
        escalation_tier=escalation_tier,
    )


def _make_req(reason: str = "out of depth", **ctx_kwargs) -> EscalationRequest:
    return EscalationRequest(reason=reason, context=ctx_kwargs)


async def _call_handler(executor: ReactiveExecutor, req: EscalationRequest) -> None:
    """Set executor into 'running' state, install a mock task group, call handler."""
    executor._running = True

    spawn_calls: list = []

    class _MockTG:
        def start_soon(self, fn, *args):  # noqa: D401
            spawn_calls.append((fn, args))

    executor._tg = _MockTG()
    executor._spawn_calls = spawn_calls  # expose for assertions
    try:
        await executor._on_bus_escalation(req, executor.session)
    finally:
        executor._running = False
        executor._tg = None


# ---------------------------------------------------------------------------
# #1253 — give_up path (no tier configured)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_no_tier_emits_give_up():
    """Without escalation_tier, EscalationRequest → NodeEscalated(route='give_up')."""
    s = Session()
    captured: list[NodeEscalated] = []
    s.observe(NodeEscalated, handler=lambda sig, _: captured.append(sig))

    executor = _make_executor(s, escalation_tier=None)
    req = _make_req("low confidence")
    await _call_handler(executor, req)

    assert len(captured) == 1
    assert captured[0].route == "give_up"
    assert captured[0].reason == "low confidence"


@pytest.mark.asyncio
async def test_escalation_human_required_always_give_up():
    """human_required=True in context → give_up even when escalation_tier is set."""
    s = Session()
    captured: list[NodeEscalated] = []
    s.observe(NodeEscalated, handler=lambda sig, _: captured.append(sig))

    executor = _make_executor(s, escalation_tier="claude-opus-4-8")
    req = _make_req("need human", human_required=True)
    await _call_handler(executor, req)

    assert len(captured) == 1
    assert captured[0].route == "give_up"


@pytest.mark.asyncio
async def test_escalation_not_running_is_no_op():
    """Handler is a no-op when the executor is not running."""
    s = Session()
    captured: list = []
    s.observe(NodeEscalated, handler=lambda sig, _: captured.append(sig))

    executor = _make_executor(s, escalation_tier="claude-opus-4-8")
    # Do NOT set _running=True — call the handler directly without the helper.
    req = _make_req("test")
    await executor._on_bus_escalation(req, s)

    assert captured == []


# ---------------------------------------------------------------------------
# #1253 — higher_tier path (tier configured, emitter present)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_with_tier_emits_higher_tier():
    """With escalation_tier set, EscalationRequest → NodeEscalated(route='higher_tier')
    and a new op is scheduled on the task group."""
    s = Session()
    captured: list[NodeEscalated] = []
    s.observe(NodeEscalated, handler=lambda sig, _: captured.append(sig))

    executor = _make_executor(s, escalation_tier="claude-opus-4-8")

    # Set a fake current-op so the emitter is not None.
    emitter_op = create_operation("operate", parameters={"instruction": "original task"})
    token = _CURRENT_OP.set(emitter_op)
    try:
        await _call_handler(executor, _make_req("repeated failure"))
    finally:
        _CURRENT_OP.reset(token)

    assert len(captured) == 1
    assert captured[0].route == "higher_tier"
    # A new op should have been scheduled on the mock task group.
    assert len(executor._spawn_calls) >= 1


@pytest.mark.asyncio
async def test_escalation_with_tier_no_emitter_uses_generic_op():
    """escalation_tier set but no current emitter (None) → still schedules and emits higher_tier."""
    s = Session()
    captured: list[NodeEscalated] = []
    s.observe(NodeEscalated, handler=lambda sig, _: captured.append(sig))

    executor = _make_executor(s, escalation_tier="claude-opus-4-8")
    # _CURRENT_OP is not set — emitter will be None.
    await _call_handler(executor, _make_req("explicit cannot"))

    assert len(captured) == 1
    # Without an emitter, the child is still injected as an independent op.
    assert captured[0].route == "higher_tier"


# ---------------------------------------------------------------------------
# #1253 — NodeEscalated is stored in the session observer flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_signal_stored_in_observer_flow():
    """NodeEscalated lands in the observer flow (ADR-0077 audit trail)."""
    s = Session()
    executor = _make_executor(s, escalation_tier=None)
    await _call_handler(executor, _make_req("audit test"))

    stored = s.observer.by_type(NodeEscalated)
    assert len(stored) == 1


# ---------------------------------------------------------------------------
# #1254 — ConfidenceRating value object
# ---------------------------------------------------------------------------


def test_rating_overall_is_min():
    """ConfidenceRating.overall == min(category_scores)."""
    r = ConfidenceRating(correctness=0.9, completeness=0.7, evidence=0.8, risk=0.95)
    assert r.overall == 0.7


def test_rating_frozen():
    """ConfidenceRating is immutable."""
    r = ConfidenceRating(correctness=0.8, completeness=0.8, evidence=0.8, risk=0.8)
    with pytest.raises((AttributeError, TypeError)):
        r.correctness = 0.1  # type: ignore[misc]


def test_rating_as_dict():
    r = ConfidenceRating(correctness=0.9, completeness=0.8, evidence=0.85, risk=0.95)
    d = r.as_dict()
    assert set(d.keys()) == {"correctness", "completeness", "evidence", "risk"}
    assert d["completeness"] == 0.8


# ---------------------------------------------------------------------------
# #1254 — below-target → ConfidenceGateEscalated (NOT completion)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_below_target_raises_not_completes():
    """Below-target self-rating MUST raise ConfidenceGateEscalated, not return."""
    s = Session()
    branch = s.default_branch

    low_rating = ConfidenceRating(correctness=0.5, completeness=0.5, evidence=0.5, risk=0.5)

    async def _rater(_result):
        return low_rating

    with pytest.raises(ConfidenceGateEscalated) as exc_info:
        await confidence_gated_completion(
            branch,
            work_result="some result",
            rater=_rater,
            target=0.95,
        )

    exc = exc_info.value
    assert exc.rating.overall < 0.95
    assert exc.escalation_request is not None
    assert "0.500" in str(exc.escalation_request.reason)


@pytest.mark.asyncio
async def test_confidence_below_target_escalation_request_context():
    """EscalationRequest context includes trigger, confidence, and category_scores."""
    s = Session()
    branch = s.default_branch

    async def _rater(_result):
        return ConfidenceRating(correctness=0.4, completeness=0.9, evidence=0.9, risk=0.9)

    with pytest.raises(ConfidenceGateEscalated) as exc_info:
        await confidence_gated_completion(branch, work_result="x", rater=_rater, target=0.95)

    ctx = exc_info.value.escalation_request.context
    assert ctx["trigger"] == "low_confidence"
    assert ctx["confidence"] == pytest.approx(0.4)
    assert "correctness" in ctx["category_scores"]


# ---------------------------------------------------------------------------
# #1254 — at/above target → completion allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_at_target_returns_result():
    """At-target rating → (rating, work_result) returned; no exception raised."""
    s = Session()
    branch = s.default_branch

    high_rating = ConfidenceRating(correctness=0.96, completeness=0.97, evidence=0.98, risk=0.99)

    async def _rater(_result):
        return high_rating

    rating, result = await confidence_gated_completion(
        branch,
        work_result="final answer",
        rater=_rater,
        target=0.95,
    )

    assert rating.overall >= 0.95
    assert result == "final answer"


@pytest.mark.asyncio
async def test_confidence_at_target_emits_gate_passed():
    """ConfidenceGatePassed is emitted on the session bus when target is reached."""
    s = Session()
    branch = s.default_branch
    # Wire the branch to the session observer so signals propagate.
    branch._observer = s.observer

    gate_passed: list[ConfidenceGatePassed] = []
    s.observe(ConfidenceGatePassed, handler=lambda sig, _: gate_passed.append(sig))

    async def _rater(_result):
        return ConfidenceRating(correctness=0.99, completeness=0.99, evidence=0.99, risk=0.99)

    await confidence_gated_completion(branch, work_result="x", rater=_rater, target=0.95)

    assert len(gate_passed) == 1
    assert gate_passed[0].overall >= 0.95
    assert gate_passed[0].target == 0.95


# ---------------------------------------------------------------------------
# #1254 — evidence seeker closes the gap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_evidence_seeker_closes_gap():
    """evidence_seeker raises rating from below to above target → completion."""
    s = Session()
    branch = s.default_branch

    call_count = {"n": 0}

    async def _rater(_result):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ConfidenceRating(correctness=0.7, completeness=0.7, evidence=0.7, risk=0.7)
        # Second call — after evidence was sought.
        return ConfidenceRating(correctness=0.97, completeness=0.97, evidence=0.97, risk=0.97)

    async def _seeker(result, _rating):
        return result + " + extra evidence"

    rating, result = await confidence_gated_completion(
        branch,
        work_result="initial",
        rater=_rater,
        target=0.95,
        evidence_seeker=_seeker,
    )

    assert rating.overall >= 0.95
    assert "extra evidence" in result
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_confidence_exhausted_seeker_escalates():
    """evidence_seeker cannot close the gap within max_attempts → ConfidenceGateEscalated."""
    s = Session()
    branch = s.default_branch

    async def _rater(_result):
        return ConfidenceRating(correctness=0.5, completeness=0.5, evidence=0.5, risk=0.5)

    async def _seeker(result, _rating):
        return result  # no improvement

    with pytest.raises(ConfidenceGateEscalated):
        await confidence_gated_completion(
            branch,
            work_result="x",
            rater=_rater,
            target=0.95,
            max_attempts=2,
            evidence_seeker=_seeker,
        )


# ---------------------------------------------------------------------------
# #1254 — sync rater is also supported
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_sync_rater_works():
    """rater may be synchronous (not a coroutine) — both forms accepted."""
    s = Session()
    branch = s.default_branch

    def _sync_rater(_result):
        return ConfidenceRating(correctness=0.96, completeness=0.96, evidence=0.96, risk=0.96)

    rating, result = await confidence_gated_completion(
        branch, work_result="data", rater=_sync_rater, target=0.95
    )
    assert rating.overall >= 0.95
