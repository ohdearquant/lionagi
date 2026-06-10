# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Observable envelope carrying payloads into the reactive bus.

``NodeLifecycleState`` and ``lane_for`` provide the canonical per-node
lifecycle projection (ADR-0083). Callers pre-filter signals to one node id
and call ``lane_for`` to derive the current lane without bespoke parsing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel

from ..protocols.generic.element import Element

__all__ = (
    "Signal",
    "StructuredOutput",
    "RunStart",
    "RunEnd",
    "RunFailed",
    "NodeStarted",
    "NodeCompleted",
    "NodeFailed",
    "NodeQueued",
    "NodeAwaitingApproval",
    "NodeEscalated",
    "GateDenied",
    "MessageAdded",
    "NodeLifecycleState",
    "lane_for",
)


class Signal(Element):
    """Observable envelope carrying a payload into the reactive bus."""

    data: Any = None
    emitter_role: str | None = None


class StructuredOutput(Signal):
    """Signal whose payload is a structured (typed) model."""

    data: BaseModel


class RunStart(Signal):
    """Run lifecycle: beginning."""


class RunEnd(Signal):
    """Run lifecycle: completed. data is the result."""


class RunFailed(Signal):
    """Run lifecycle: raised. data is the exception."""


class NodeStarted(Signal):
    """DAG node lifecycle: began executing."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0


class NodeCompleted(Signal):
    """DAG node lifecycle: finished successfully."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0


class NodeFailed(Signal):
    """DAG node lifecycle: raised during execution."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0


class GateDenied(Signal):
    """Governance gate denied a proposed action."""


class MessageAdded(Signal):
    """A message was added to a branch. data is the RoledMessage."""


# -- Extended node lifecycle (ADR-0083) ---------------------------------------
# Three signals completing the canonical per-node lifecycle:
#   queued → running → awaiting_approval → succeeded | failed | escalated
#
# NodeStarted / NodeCompleted / NodeFailed (above) cover running/succeeded/
# failed already; these three cover the remaining states.


class NodeQueued(Signal):
    """A DAG operation node entered the runnable graph, queued for execution."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0


class NodeAwaitingApproval(Signal):
    """A DAG operation node is paused waiting for an external approval decision."""

    op_id: str = ""
    name: str = ""
    reason: str | None = None


class NodeEscalated(Signal):
    """A DAG operation node escalated — out of depth or no higher tier available.

    ``route`` is ``"higher_tier"`` when a re-dispatch is scheduled or
    ``"give_up"`` when no escalation path is configured. The
    ``escalation_request`` field carries the original request payload for
    the audit trail; it is stored in a named field rather than ``Signal.data``
    to prevent the observer's payload-matching from re-firing the escalation
    handler when this signal is emitted.
    """

    op_id: str = ""
    name: str = ""
    reason: str = ""
    route: str = ""  # "higher_tier" | "give_up"
    escalation_request: Any = None


# -- Lifecycle projection (ADR-0083) ------------------------------------------

#: The six canonical per-node lifecycle states.
NodeLifecycleState = Literal[
    "queued", "running", "awaiting_approval", "succeeded", "failed", "escalated"
]

_TERMINAL: frozenset[str] = frozenset({"succeeded", "failed", "escalated"})


def _signal_to_state(sig: Any) -> NodeLifecycleState | None:
    """Return the lifecycle state implied by *sig*, or ``None`` if not state-bearing."""
    if isinstance(sig, NodeQueued):
        return "queued"
    if isinstance(sig, NodeStarted | RunStart):
        return "running"
    if isinstance(sig, NodeAwaitingApproval):
        return "awaiting_approval"
    if isinstance(sig, NodeCompleted | RunEnd):
        return "succeeded"
    if isinstance(sig, NodeFailed | RunFailed):
        return "failed"
    if isinstance(sig, NodeEscalated):
        return "escalated"
    # StructuredOutput carrying an EscalationRequest also projects to escalated.
    if isinstance(sig, StructuredOutput):
        from lionagi.casts.emission import EscalationRequest  # noqa: PLC0415

        if isinstance(sig.data, EscalationRequest):
            return "escalated"
    return None


def lane_for(signals: Iterable[Signal | Any]) -> NodeLifecycleState:
    """Project an ordered, single-node signal stream into its current lifecycle lane.

    Callers must pre-filter *signals* to one operation/node id. The default
    state (empty stream) is ``"queued"``. Terminal states (``succeeded``,
    ``failed``, ``escalated``) are sticky: later non-retry signals cannot
    override them. A subsequent ``NodeQueued`` or ``NodeStarted`` signal
    represents a new attempt and resets the state.

    ``RunStart`` / ``RunEnd`` are run-scoped fallbacks — valid only for
    single-run cards where no node-specific signal is present.
    """
    state: NodeLifecycleState = "queued"
    in_terminal: bool = False
    for sig in signals:
        new = _signal_to_state(sig)
        if new is None:
            continue
        # Terminal is sticky unless a new attempt explicitly resets to queued/running.
        if in_terminal and new not in ("queued", "running"):
            continue
        state = new
        in_terminal = state in _TERMINAL
    return state
