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
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..protocols.generic.element import Element
from ..protocols.messages import ActionRequest, ActionResponse

__all__ = (
    "Signal",
    "StructuredOutput",
    "ActionRequestSignal",
    "ActionResponseSignal",
    "RunStart",
    "RunEnd",
    "RunFailed",
    "NodeStarted",
    "NodeCompleted",
    "NodeFailed",
    "GateDenied",
    "MessageAdded",
)


class Signal(Element):
    """An Observable envelope carrying a payload into the reactive bus."""

    data: Any = None
    emitter_role: str | None = None
    """Role name of the emitting agent, set at emit time for ``RoleFilter`` routing."""


class StructuredOutput(Signal):
    """A Signal whose payload is a structured (typed) model."""

    data: BaseModel


class ActionRequestSignal(Signal):
    """A tool-use emission. ``data`` is the originating ``ActionRequest``.

    Lets observers react to tool calls and track per-tool usage:
    ``session.observe(ActionRequest)`` fires for every tool invocation.
    """

    data: ActionRequest


class ActionResponseSignal(Signal):
    """A tool-result emission. ``data`` is the resolved ``ActionResponse``."""

    data: ActionResponse


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
