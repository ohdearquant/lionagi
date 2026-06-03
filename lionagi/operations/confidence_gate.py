# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Confidence-gated completion operation (#1254).

A reusable async operation: the agent self-rates across categories toward a
target confidence (default 0.95).  Below target it must seek more evidence or
emit ``EscalationRequest``; it may NOT declare done below target.  At target
it emits ``ConfidenceGatePassed`` and allows completion.

Usage::

    rating, result = await confidence_gated_completion(
        branch,
        work_result=my_result,
        rater=my_async_rater,   # async callable -> ConfidenceRating
        target=0.95,
    )

Learning hook: ``ConfidenceGatePassed`` is emitted on the session bus so
``session.observe(ConfidenceGatePassed, handler=propagate_learnings)`` can
ride the existing observer transport (ADR-0077 Follow-up: full cross-task
propagation deferred; the hook point is wired here).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lionagi.session.signal import Signal

if TYPE_CHECKING:
    from lionagi.session.branch import Branch

__all__ = (
    "ConfidenceGatePassed",
    "ConfidenceGateEscalated",
    "ConfidenceRating",
    "confidence_gated_completion",
)


# ---------------------------------------------------------------------------
# Signal: gate passed
# ---------------------------------------------------------------------------


class ConfidenceGatePassed(Signal):
    """Emitted when an agent's self-rating reaches the target confidence.

    ``session.observe(ConfidenceGatePassed, handler=propagate_learnings)``
    hooks learning propagation onto the existing observer transport.
    """

    target: float = 0.95
    overall: float = 0.0
    category_scores: dict = {}  # noqa: RUF012 — intentional mutable default on Signal field


# ---------------------------------------------------------------------------
# Value object: immutable self-assessment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceRating:
    """Immutable per-category self-assessment produced by the rater callable.

    Overall confidence is ``min(category_scores)`` — conservative by design
    so a single weak dimension cannot be masked by averaging.
    """

    correctness: float  # 0.0–1.0: is the result factually/logically correct?
    completeness: float  # 0.0–1.0: does it cover all required aspects?
    evidence: float  # 0.0–1.0: is the result well-supported by evidence?
    risk: float  # 0.0–1.0: how low is the risk of undetected error?

    @property
    def overall(self) -> float:
        """Conservative aggregate: minimum across all four categories."""
        return min(self.correctness, self.completeness, self.evidence, self.risk)

    def as_dict(self) -> dict[str, float]:
        return {
            "correctness": self.correctness,
            "completeness": self.completeness,
            "evidence": self.evidence,
            "risk": self.risk,
        }


# ---------------------------------------------------------------------------
# Exception: gate not reached
# ---------------------------------------------------------------------------


class ConfidenceGateEscalated(Exception):  # noqa: N818
    """Raised when ``confidence_gated_completion`` cannot reach target confidence.

    Enforces the contract that a caller may NOT declare completion when
    confidence is below target — the normal return path is blocked and this
    exception carries the escalation request and final rating for inspection.
    """

    def __init__(self, escalation_request: Any, rating: ConfidenceRating) -> None:
        reason = getattr(escalation_request, "reason", str(escalation_request))
        super().__init__(reason)
        self.escalation_request = escalation_request
        self.rating = rating


# ---------------------------------------------------------------------------
# Operation
# ---------------------------------------------------------------------------


async def confidence_gated_completion(
    branch: Branch,
    *,
    work_result: Any,
    rater: Callable,
    target: float = 0.95,
    max_attempts: int = 3,
    evidence_seeker: Callable | None = None,
) -> tuple[ConfidenceRating, Any]:
    """Self-rate work toward a target confidence; block completion when below.

    Args:
        branch: The branch whose observer transport is used to emit signals.
        work_result: The artifact or answer to assess.
        rater: Async callable ``(work_result) -> ConfidenceRating``.  In
            production, calls ``branch.operate(response_format=ConfidenceRating)``.
            Must be mockable for unit tests.
        target: Minimum ``ConfidenceRating.overall`` to allow completion
            (default 0.95).
        max_attempts: Maximum self-assessment + evidence-seeking iterations
            before giving up (default 3).
        evidence_seeker: Optional async callable
            ``(work_result, rating) -> new_work_result`` that attempts to
            close the confidence gap (e.g. fetch additional sources, rerun
            a sub-check).  When ``None``, a below-target rating immediately
            escalates.

    Returns:
        ``(final_rating, work_result)`` — only reachable when
        ``final_rating.overall >= target``.

    Raises:
        ConfidenceGateEscalated: When the target cannot be reached within
            ``max_attempts`` and no evidence path is available.  The exception
            also emits an ``EscalationRequest`` onto the session bus via the
            branch observer so ``_on_bus_escalation`` can route it.
    """
    from lionagi.casts.emission import EscalationRequest  # noqa: PLC0415

    attempts: list[ConfidenceRating] = []

    for attempt_n in range(max_attempts):
        import inspect  # noqa: PLC0415

        rating_result = rater(work_result)
        if inspect.isawaitable(rating_result):
            rating_result = await rating_result
        rating: ConfidenceRating = rating_result
        attempts.append(rating)

        if rating.overall >= target:
            # Gate passed: emit the learning hook signal and allow completion.
            gate_signal = ConfidenceGatePassed(
                target=target,
                overall=rating.overall,
                category_scores=rating.as_dict(),
            )
            _observer = getattr(branch, "_observer", None)
            if _observer is not None:
                await branch.emit(gate_signal)
            return rating, work_result

        # Below target: seek more evidence if a seeker is provided and
        # attempts remain, else escalate.
        can_seek = evidence_seeker is not None and attempt_n < max_attempts - 1
        if can_seek:
            seek_result = evidence_seeker(work_result, rating)
            if inspect.isawaitable(seek_result):
                seek_result = await seek_result
            work_result = seek_result
            continue

        # No evidence path or last attempt — escalate.
        break

    # Build and emit the escalation request; this fires _on_bus_escalation
    # in any running ReactiveExecutor that registered the handler.
    req = EscalationRequest(
        reason=(
            f"Confidence {attempts[-1].overall:.3f} below target {target} "
            f"after {len(attempts)} attempt(s)"
        ),
        context={
            "trigger": "low_confidence",
            "confidence": attempts[-1].overall,
            "category_scores": attempts[-1].as_dict(),
            "attempts": [r.as_dict() for r in attempts],
        },
    )
    _observer = getattr(branch, "_observer", None)
    if _observer is not None:
        from lionagi.session.signal import StructuredOutput  # noqa: PLC0415

        await branch.emit(StructuredOutput(data=req))

    raise ConfidenceGateEscalated(req, attempts[-1])
