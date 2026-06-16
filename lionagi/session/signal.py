# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Signal types and per-node lifecycle projection for the reactive bus (ADR-0083)."""

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
    """A DAG node escalated; route is "higher_tier" or "give_up" (see docs/reference/testing-state-session.md)."""

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
    """Project a pre-filtered single-node signal stream to its current lifecycle lane; terminal states are sticky."""
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
