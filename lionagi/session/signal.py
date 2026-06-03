# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Signal — a lightweight Observable envelope for the reactive bus.

A ``Signal`` carries an arbitrary payload (``data``) into the session
observer. Observers key off the *payload* type, not the Signal subclass:
``session.observe(MyModel)`` fires for any Signal whose ``data`` is a
``MyModel`` instance. The id comes for free from :class:`Element`, so the
envelope lives in a Pile/Flow like any other element.

``StructuredOutput`` is the typed case: its payload is a structured model.
It is the realization of "capabilities = structured output event" — an agent
exercises a capability by emitting a typed value; an observer reacting to
that type is the capability being honored.

``NodeLifecycleState`` and ``lane_for`` provide the canonical per-node
lifecycle projection (ADR-0077). Callers pre-filter signals to one node id
and call ``lane_for`` to derive the current lane without bespoke parsing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from ..protocols.generic.element import Element

if TYPE_CHECKING:
    pass

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
    """An Observable envelope carrying a payload into the reactive bus."""

    data: Any = None
    emitter_role: str | None = None
    """Role name of the emitting agent, set at emit time for ``RoleFilter`` routing."""


class StructuredOutput(Signal):
    """A Signal whose payload is a structured (typed) model."""

    data: BaseModel


# Tool-use / tool-result are observed off the universal ``MessageAdded`` stream
# (below): ``session.observe(ActionRequest)`` fires for every tool invocation by
# matching the unwrapped ``ActionRequest`` payload of its ``MessageAdded``
# envelope. No dedicated ActionRequestSignal/ActionResponseSignal — a second
# signal carrying the same message would double-fire those data-type observers.


# -- Run lifecycle ------------------------------------------------------------
# Lifecycle signals report the *fact* that a run began / ended / failed — a
# concern orthogonal to capability emission (which requires a grant). They fire
# whenever a session observer is attached, regardless of grant; a standalone
# branch (no observer) emits nothing, so its behavior is unchanged. Observed by
# their own envelope type (``session.observe(RunEnd)``); ``RunEnd.data`` also
# unwraps so ``session.observe(MyModel)`` fires on the final result.


class RunStart(Signal):
    """A run is beginning. ``data`` is unset (a lifecycle marker)."""


class RunEnd(Signal):
    """A run completed. ``data`` is the final result (model, dict, or text)."""


class RunFailed(Signal):
    """A run raised. ``data`` is the exception that aborted it."""


# -- Node lifecycle (DAG execution) -------------------------------------------
# Emitted by ``EngineRun.run_dag`` as each operation node of a DAG starts and
# finishes. They turn the executor's ``on_progress(op_id, name, status, elapsed)``
# callback into bus events, so persistence, Studio segments, and progress
# display subscribe with ``session.observe(NodeCompleted)`` instead of threading
# a bespoke callback. Observed by their own envelope type. ``op_id`` is the
# operation node id; ``name`` is the executing branch's name; ``elapsed`` is
# wall-seconds at the event (0 at start).


class NodeStarted(Signal):
    """A DAG operation node began executing."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0


class NodeCompleted(Signal):
    """A DAG operation node finished successfully."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0


class NodeFailed(Signal):
    """A DAG operation node raised during execution."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0


# -- Governance ---------------------------------------------------------------
# Emitted when the session's pre-invoke gate denies a proposed action (ADR-0076
# Follow-up 1). The gate gates the *operation* (e.g. a tool call), unlike the
# post-record gate inside ``emit`` which gates observer dispatch. Recorded onto
# the Flow so denials are audit-visible; ``session.observe(GateDenied)`` reacts.


class GateDenied(Signal):
    """The governance gate denied a proposed action. ``data`` is the denied payload."""


# -- Message lifecycle --------------------------------------------------------
# Emitted for EVERY message added to a branch (system, instruction, assistant,
# action — not just the capability-bearing subset). It puts the full message
# stream on the one transport so ``session.observe(MessageAdded)`` sees every
# turn and the Flow is a complete record. ``data`` is the ``RoledMessage``.
# This is the foundation for routing persistence onto the bus (ADR-0023b):
# a persistence handler can subscribe to MessageAdded instead of registering a
# parallel ``on_message_added`` callback.


class MessageAdded(Signal):
    """A message was added to a branch. ``data`` is the ``RoledMessage``."""


# -- Extended node lifecycle (ADR-0077) ---------------------------------------
# Three signals that complete the canonical per-node lifecycle:
#   queued → running → awaiting_approval → succeeded | failed | escalated
#
# ``NodeStarted / NodeCompleted / NodeFailed`` (above) cover running/succeeded/
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
    ``"give_up"`` when no escalation path is configured / ``human_required``.
    ``escalation_request`` carries the original ``EscalationRequest`` payload when
    available.  It is stored in a named field rather than ``Signal.data`` to
    prevent the observer's payload-matching from re-firing the escalation handler
    when this signal is emitted (``Signal.data`` is payload-matched; a named
    field is not).
    """

    op_id: str = ""
    name: str = ""
    reason: str = ""
    route: str = ""  # "higher_tier" | "give_up"
    escalation_request: Any = None  # original EscalationRequest for audit trail


# -- Lifecycle projection (ADR-0077) ------------------------------------------

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
        # Lazy import avoids a circular dependency with casts.emission.
        from lionagi.casts.emission import EscalationRequest  # noqa: PLC0415

        if isinstance(sig.data, EscalationRequest):
            return "escalated"
    return None


def lane_for(signals: Iterable[Signal | Any]) -> NodeLifecycleState:
    """Project an ordered, single-node signal stream into its current lifecycle lane.

    Callers must pre-filter *signals* to one operation/node id.  The default
    state (empty stream) is ``"queued"``.  Terminal states (``succeeded``,
    ``failed``, ``escalated``) are sticky: later non-retry signals cannot
    override them.  A subsequent ``NodeQueued`` or ``NodeStarted`` signal
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
