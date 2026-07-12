# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Signal types and per-node lifecycle projection for the reactive bus (ADR-0083).

Payload contract (schema_version=1):
  RunStart        — no payload fields
  RunEnd          — input_tokens, output_tokens, total_cost_usd, num_turns, duration_ms
  RunFailed       — data: exception
  NodeSpawned     — op_id, parent_id, independent, assignee, instruction
  NodeQueued      — op_id, name, elapsed, parent_id, depends_on
  NodeStarted     — op_id, name, elapsed, parent_id, depends_on
  NodeCompleted   — op_id, name, elapsed, parent_id, depends_on
  NodeFailed      — op_id, name, elapsed, parent_id, depends_on
  NodeAwaitingApproval — op_id, name, reason
  NodeEscalated   — op_id, name, reason, route, escalation_request
  NodePaused      — op_id, name
  GateDenied      — data: any
  MessageAdded    — data: RoledMessage (stored as message_ref in payload)
  DispatchSignal  — dispatch_id, kind, deliver_to, attempt, ack_token, body (ADR-0092)

Version policy: schema_version is bumped on any breaking field removal or rename.
Adding nullable fields is non-breaking and does not bump the version.
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
    "NodeSpawned",
    "NodeStarted",
    "NodeCompleted",
    "NodeFailed",
    "NodeQueued",
    "NodeAwaitingApproval",
    "NodeEscalated",
    "NodePaused",
    "GateDenied",
    "MessageAdded",
    "DispatchSignal",
    "NodeLifecycleState",
    "lane_for",
    "build_run_end",
    "SIGNAL_SCHEMA_VERSION",
)

SIGNAL_SCHEMA_VERSION: int = 1


class Signal(Element):
    """Observable envelope carrying a payload into the reactive bus."""

    data: Any = None
    emitter_role: str | None = None
    schema_version: int = SIGNAL_SCHEMA_VERSION


class StructuredOutput(Signal):
    """Signal whose payload is a structured (typed) model."""

    data: BaseModel


class RunStart(Signal):
    """Run lifecycle: beginning."""


class RunEnd(Signal):
    """Run lifecycle: completed. Usage fields are populated when available."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: float = 0.0


class RunFailed(Signal):
    """Run lifecycle: raised. data is the exception."""


class NodeSpawned(Signal):
    """A DAG node was accepted into the running graph (reactive spawn)."""

    op_id: str = ""
    parent_id: str | None = None
    independent: bool = False
    assignee: str | None = None
    instruction: str | None = None


class NodeStarted(Signal):
    """DAG node lifecycle: began executing."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0
    parent_id: str | None = None
    depends_on: list[str] = []


class NodeCompleted(Signal):
    """DAG node lifecycle: finished successfully."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0
    parent_id: str | None = None
    depends_on: list[str] = []


class NodeFailed(Signal):
    """DAG node lifecycle: raised during execution."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0
    parent_id: str | None = None
    depends_on: list[str] = []


class GateDenied(Signal):
    """Governance gate denied a proposed action."""


class MessageAdded(Signal):
    """A message was added to a branch. data is the RoledMessage."""


class DispatchSignal(Signal):
    """Outbound dispatch payload contract (ADR-0092); schema_version rides Signal.

    One stable envelope (``to_dict(mode="json")``) shared by every dispatch kind,
    so the transport template never churns per-kind.
    """

    dispatch_id: str = ""
    kind: str = ""  # e.g. "revival_ping" | "terminal_notify"
    deliver_to: str = ""
    attempt: int = 0
    ack_token: str | None = None
    body: dict = {}


# -- Extended node lifecycle (ADR-0083) ---------------------------------------
# queued → running → awaiting_approval → succeeded | failed | escalated
# NodeStarted/NodeCompleted/NodeFailed (above) cover running/succeeded/failed;
# these three cover the remaining states.


class NodeQueued(Signal):
    """A DAG operation node entered the runnable graph, queued for execution."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0
    parent_id: str | None = None
    depends_on: list[str] = []


class NodeAwaitingApproval(Signal):
    """A DAG operation node is paused waiting for an external approval decision."""

    op_id: str = ""
    name: str = ""
    reason: str | None = None


class NodeEscalated(Signal):
    """A DAG node escalated or sent a help signal.

    route is "higher_tier" (retry), "give_up" (terminal), or "notify" (soft
    help signal — informational only, the node's own lifecycle is
    unaffected). See docs/reference/testing-state-session.md.
    """

    op_id: str = ""
    name: str = ""
    reason: str = ""
    route: str = ""  # "higher_tier" | "give_up" | "notify"
    escalation_request: Any = None


class NodePaused(Signal):
    """A DAG operation node is blocked at an operation boundary, awaiting resume()."""

    op_id: str = ""
    name: str = ""


# -- Lifecycle projection (ADR-0083) ------------------------------------------

#: The seven canonical per-node lifecycle states.
NodeLifecycleState = Literal[
    "queued", "running", "awaiting_approval", "paused", "succeeded", "failed", "escalated"
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
    if isinstance(sig, NodePaused):
        return "paused"
    if isinstance(sig, NodeCompleted | RunEnd):
        return "succeeded"
    if isinstance(sig, NodeFailed | RunFailed):
        return "failed"
    if isinstance(sig, NodeEscalated):
        req = sig.escalation_request
        # A soft ("fyi") help signal is informational only — the emitting
        # node keeps working toward its own terminal state, so it must not
        # get pinned into the terminal "escalated" lane. Only a "blocked"
        # urgency (the default, matching historical give_up/higher_tier
        # behavior) or an unaccompanied signal (no request attached, e.g.
        # a bare NodeEscalated built directly) is treated as escalated.
        if getattr(req, "urgency", "blocked") == "fyi":
            return None
        return "escalated"
    # StructuredOutput carrying an EscalationRequest also projects to escalated,
    # unless it is a soft ("fyi") help signal.
    if isinstance(sig, StructuredOutput):
        from lionagi.casts.emission import EscalationRequest  # noqa: PLC0415

        if isinstance(sig.data, EscalationRequest) and sig.data.urgency != "fyi":
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


def _collect_branch_usage(branch: Any) -> dict[str, Any]:
    """Sum provider-reported usage across all AssistantResponse messages on branch.
    Keys: input_tokens, output_tokens, total_cost_usd, num_turns; all zero when
    no provider data is available (subscription runs, tests)."""
    input_tokens = 0
    output_tokens = 0
    total_cost_usd = 0.0
    num_turns = 0

    try:
        messages = list(branch.msgs.messages)
    except Exception:  # noqa: BLE001
        return {"input_tokens": 0, "output_tokens": 0, "total_cost_usd": 0.0, "num_turns": 0}

    for msg in messages:
        mr = (
            getattr(msg, "metadata", {}).get("model_response") if hasattr(msg, "metadata") else None
        )
        if not isinstance(mr, dict):
            continue
        usage = mr.get("usage") if isinstance(mr.get("usage"), dict) else mr
        input_tokens += int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
        output_tokens += int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
        cost = mr.get("total_cost_usd") or mr.get("cost")
        if isinstance(cost, (int, float)):
            total_cost_usd += float(cost)
        num_turns += int(mr.get("num_turns", 0) or 0)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_cost_usd": total_cost_usd,
        "num_turns": num_turns,
    }


def _collect_multi_branch_usage(branches: Iterable[Any]) -> dict[str, Any]:
    """Sum _collect_branch_usage across multiple branches (multi-leg DAG runs).

    Same keys as _collect_branch_usage. duration_ms is deliberately excluded —
    wall-clock across parallel legs isn't simply summable.
    """
    input_tokens = 0
    output_tokens = 0
    total_cost_usd = 0.0
    num_turns = 0

    for branch in branches:
        usage = _collect_branch_usage(branch)
        input_tokens += usage["input_tokens"]
        output_tokens += usage["output_tokens"]
        total_cost_usd += usage["total_cost_usd"]
        num_turns += usage["num_turns"]

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_cost_usd": total_cost_usd,
        "num_turns": num_turns,
    }


def build_run_end(branch: Any, *, duration_ms: float = 0.0, result: Any = None) -> RunEnd:
    """Build a RunEnd signal with usage populated from branch message history."""
    usage = _collect_branch_usage(branch)
    return RunEnd(data=result, duration_ms=duration_ms, **usage)
