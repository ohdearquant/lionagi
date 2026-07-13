# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Event/signal schema drift gate (ADR-0033, ADR-0003).

Regression class this guards against: the reactive-bus signal payloads
(``lionagi.session.signal``), the in-process event execution status
vocabulary (``lionagi.protocols.generic.event.EventStatus``), and the flow
executor's own completion event (``lionagi.operations.flow.FlowEvent``)
change shape silently -- a field gets renamed, removed, or an enum member is
dropped -- and nothing in the suite notices until a downstream consumer (the
Studio bus renderer, the CLI progress projector, a persisted signal replay,
``flow_signals.py``'s ``NodeQueued(...)`` construction call) breaks on a
``KeyError``/``AttributeError``/``ValidationError`` far away from the actual
change.

These tests pin the CURRENT, empirically-observed shape of every exported
signal class, the ``EventStatus`` enum, the per-node ``NodeLifecycleState``
vocabulary, and ``FlowEvent`` so any drift fails loudly, in this file, at the
point of change -- and constructively instantiate every signal from its
documented fields so a construction-site break (e.g. inside
``lionagi/engines/flow_signals.py``) is caught here too.

An intentional schema change bumps ``SIGNAL_SCHEMA_VERSION`` in
``lionagi/session/signal.py`` and updates the golden tables below in the same
change.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import BaseModel

from lionagi.operations.flow import FlowEvent
from lionagi.protocols.generic.event import Event, EventStatus
from lionagi.session import signal as signal_mod
from lionagi.session.signal import (
    DispatchSignal,
    GateDenied,
    MessageAdded,
    NodeAwaitingApproval,
    NodeCompleted,
    NodeEscalated,
    NodeFailed,
    NodeLifecycleState,
    NodePaused,
    NodeQueued,
    NodeSpawned,
    NodeStarted,
    RunEnd,
    RunFailed,
    RunStart,
    Signal,
    StructuredOutput,
)

# ---------------------------------------------------------------------------
# 1. Signal module export surface
# ---------------------------------------------------------------------------

# The full exported name set from lionagi.session.signal (ADR-0033). Adding
# or removing a signal type -- or renaming lane_for/build_run_end -- is a
# schema change in its own right and must be a deliberate edit here too.
EXPECTED_SIGNAL_EXPORTS = (
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


def test_signal_module_export_surface_golden():
    assert signal_mod.__all__ == EXPECTED_SIGNAL_EXPORTS


def test_signal_schema_version_golden():
    """schema_version=1 rides every Signal payload (per Signal.schema_version
    default). A bump here is a deliberate, versioned breaking change -- this
    test forces that bump to be conscious, not accidental."""
    assert signal_mod.SIGNAL_SCHEMA_VERSION == 1
    assert Signal().schema_version == 1


# ---------------------------------------------------------------------------
# 2. Per-class field-set goldens (sorted field-name shape)
# ---------------------------------------------------------------------------

# Sorted field-name set per pydantic Signal subclass, empirically pinned via
# `cls.model_fields.keys()`. Inherited Element/Signal fields (id, created_at,
# metadata, data, emitter_role, schema_version) are included -- a change to
# an inherited field is just as much a drift as a change to an own field.
EXPECTED_SIGNAL_FIELDS: dict[str, tuple[str, ...]] = {
    "Signal": ("created_at", "data", "emitter_role", "id", "metadata", "schema_version"),
    "StructuredOutput": (
        "created_at",
        "data",
        "emitter_role",
        "id",
        "metadata",
        "schema_version",
    ),
    "RunStart": ("created_at", "data", "emitter_role", "id", "metadata", "schema_version"),
    "RunEnd": (
        "created_at",
        "data",
        "duration_ms",
        "emitter_role",
        "id",
        "input_tokens",
        "metadata",
        "num_turns",
        "output_tokens",
        "schema_version",
        "total_cost_usd",
    ),
    "RunFailed": ("created_at", "data", "emitter_role", "id", "metadata", "schema_version"),
    "NodeSpawned": (
        "assignee",
        "created_at",
        "data",
        "emitter_role",
        "id",
        "independent",
        "instruction",
        "metadata",
        "op_id",
        "parent_id",
        "schema_version",
    ),
    "NodeStarted": (
        "created_at",
        "data",
        "depends_on",
        "elapsed",
        "emitter_role",
        "id",
        "metadata",
        "name",
        "op_id",
        "parent_id",
        "schema_version",
    ),
    "NodeCompleted": (
        "created_at",
        "data",
        "depends_on",
        "elapsed",
        "emitter_role",
        "id",
        "metadata",
        "name",
        "op_id",
        "parent_id",
        "schema_version",
    ),
    "NodeFailed": (
        "created_at",
        "data",
        "depends_on",
        "elapsed",
        "emitter_role",
        "id",
        "metadata",
        "name",
        "op_id",
        "parent_id",
        "schema_version",
    ),
    "NodeQueued": (
        "created_at",
        "data",
        "depends_on",
        "elapsed",
        "emitter_role",
        "id",
        "metadata",
        "name",
        "op_id",
        "parent_id",
        "schema_version",
    ),
    "NodeAwaitingApproval": (
        "created_at",
        "data",
        "emitter_role",
        "id",
        "metadata",
        "name",
        "op_id",
        "reason",
        "schema_version",
    ),
    "NodeEscalated": (
        "created_at",
        "data",
        "emitter_role",
        "escalation_request",
        "id",
        "metadata",
        "name",
        "op_id",
        "reason",
        "route",
        "schema_version",
    ),
    "NodePaused": (
        "created_at",
        "data",
        "emitter_role",
        "id",
        "metadata",
        "name",
        "op_id",
        "schema_version",
    ),
    "GateDenied": ("created_at", "data", "emitter_role", "id", "metadata", "schema_version"),
    "MessageAdded": ("created_at", "data", "emitter_role", "id", "metadata", "schema_version"),
    "DispatchSignal": (
        "ack_token",
        "attempt",
        "body",
        "created_at",
        "data",
        "deliver_to",
        "dispatch_id",
        "emitter_role",
        "id",
        "kind",
        "metadata",
        "schema_version",
    ),
}

_SIGNAL_CLASSES: dict[str, type[Signal]] = {
    "Signal": Signal,
    "StructuredOutput": StructuredOutput,
    "RunStart": RunStart,
    "RunEnd": RunEnd,
    "RunFailed": RunFailed,
    "NodeSpawned": NodeSpawned,
    "NodeStarted": NodeStarted,
    "NodeCompleted": NodeCompleted,
    "NodeFailed": NodeFailed,
    "NodeQueued": NodeQueued,
    "NodeAwaitingApproval": NodeAwaitingApproval,
    "NodeEscalated": NodeEscalated,
    "NodePaused": NodePaused,
    "GateDenied": GateDenied,
    "MessageAdded": MessageAdded,
    "DispatchSignal": DispatchSignal,
}


@pytest.mark.parametrize("name", sorted(EXPECTED_SIGNAL_FIELDS))
def test_signal_payload_field_set_golden(name):
    cls = _SIGNAL_CLASSES[name]
    actual = tuple(sorted(cls.model_fields.keys()))
    expected = tuple(sorted(EXPECTED_SIGNAL_FIELDS[name]))
    assert actual == expected, (
        f"{name} field set drifted: expected {expected}, got {actual}. "
        "If this is an intentional schema change, bump SIGNAL_SCHEMA_VERSION "
        "in lionagi/session/signal.py and update the golden table in "
        "tests/protocols/test_event_schema_drift.py alongside it."
    )


def test_signal_payload_goldens_cover_every_exported_signal_class():
    """Every Signal subclass in __all__ has a pinned golden -- a newly added
    signal type must be added to EXPECTED_SIGNAL_FIELDS in the same change."""
    exported_signal_classes = {
        name
        for name in signal_mod.__all__
        if isinstance(getattr(signal_mod, name), type)
        and issubclass(getattr(signal_mod, name), Signal)
    }
    assert exported_signal_classes == set(EXPECTED_SIGNAL_FIELDS)
    assert exported_signal_classes == set(_SIGNAL_CLASSES)


# ---------------------------------------------------------------------------
# 3. Constructive: every signal is instantiable from its documented fields
# ---------------------------------------------------------------------------

# Field -> representative sample value, keyed by the exact field names used
# across the payload-contract table in the lionagi/session/signal.py module
# docstring. Only own (non-inherited-default) fields need a sample here.
_SAMPLE_VALUES: dict[str, object] = {
    "op_id": "op-1",
    "parent_id": "op-0",
    "independent": True,
    "assignee": "worker",
    "instruction": "do the thing",
    "name": "my-op",
    "elapsed": 1.5,
    "depends_on": ["op-0"],
    "reason": "needs review",
    "route": "higher_tier",
    "input_tokens": 10,
    "output_tokens": 20,
    "total_cost_usd": 0.01,
    "num_turns": 2,
    "duration_ms": 123.0,
    "dispatch_id": "d-1",
    "kind": "revival_ping",
    "deliver_to": "worker-1",
    "attempt": 1,
    "ack_token": "tok",
    "body": {"k": "v"},
}


class _DummyStructuredModel(BaseModel):
    x: int = 1


def _construct_from_documented_fields(name: str) -> Signal:
    cls = _SIGNAL_CLASSES[name]
    own_fields = set(EXPECTED_SIGNAL_FIELDS[name]) - set(EXPECTED_SIGNAL_FIELDS["Signal"])
    kwargs = {f: _SAMPLE_VALUES[f] for f in own_fields if f in _SAMPLE_VALUES}
    if name == "StructuredOutput":
        # StructuredOutput narrows `data` from `Any = None` to a required
        # BaseModel -- it has no *new* field name but a required override,
        # so it needs an explicit sample to be constructible at all.
        kwargs["data"] = _DummyStructuredModel()
    return cls(**kwargs)


@pytest.mark.parametrize("name", sorted(EXPECTED_SIGNAL_FIELDS))
def test_signal_constructible_from_documented_fields(name):
    """Instantiate each signal using only its own documented fields -- pins
    that real construction call sites (lionagi/engines/flow_signals.py's
    NodeQueued/NodeStarted/NodeCompleted/NodeFailed(...) calls, DispatchSignal
    producers under ADR-0059) stay valid against the current schema."""
    instance = _construct_from_documented_fields(name)
    assert isinstance(instance, Signal)
    assert instance.schema_version == signal_mod.SIGNAL_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 4. EventStatus vocabulary + transition invariants (ADR-0003)
# ---------------------------------------------------------------------------

EXPECTED_EVENT_STATUS_VALUES = (
    "pending",
    "processing",
    "completed",
    "failed",
    "skipped",
    "cancelled",
    "aborted",
)

EXPECTED_EVENT_STATUS_TERMINAL = frozenset(
    {"completed", "failed", "cancelled", "aborted", "skipped"}
)


def test_event_status_vocabulary_golden():
    assert tuple(EventStatus.allowed()) == EXPECTED_EVENT_STATUS_VALUES


def test_event_status_terminal_set_golden():
    actual = frozenset(s.value for s in Event._TERMINAL_STATUSES)
    assert actual == EXPECTED_EVENT_STATUS_TERMINAL


class _DummyEvent(Event):
    async def _invoke(self):
        # Observed from inside the running body: invoke() must have already
        # flipped PENDING -> PROCESSING before dispatching to _invoke().
        assert self.status == EventStatus.PROCESSING
        return "ok"


class _FailingEvent(Event):
    async def _invoke(self):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_event_status_transition_pending_to_processing_to_completed():
    ev = _DummyEvent()
    assert ev.status == EventStatus.PENDING
    await ev.invoke()
    assert ev.status == EventStatus.COMPLETED
    assert ev.response == "ok"


@pytest.mark.asyncio
async def test_event_status_transition_pending_to_processing_to_failed():
    ev = _FailingEvent()
    await ev.invoke()
    assert ev.status == EventStatus.FAILED
    assert ev.execution.error is not None


@pytest.mark.asyncio
async def test_event_invoke_is_noop_once_non_pending():
    """The transition rule enforced in Event.invoke() --
    `if self.execution.status != PENDING: return` -- means invoke() only
    runs _invoke() once. Pinning this so a refactor that drops the guard
    (e.g. allowing silent re-execution / re-emission of terminal signals)
    is caught here."""
    calls = []

    class _CountingEvent(Event):
        async def _invoke(self):
            calls.append(1)
            return "first"

    ev = _CountingEvent()
    await ev.invoke()
    await ev.invoke()
    assert len(calls) == 1
    assert ev.status == EventStatus.COMPLETED


def test_event_status_setter_rejects_unknown_value():
    ev = _DummyEvent()
    with pytest.raises(ValueError):
        ev.status = "not-a-real-status"


# ---------------------------------------------------------------------------
# 5. NodeLifecycleState vocabulary + terminal-lane invariants (ADR-0033)
# ---------------------------------------------------------------------------

EXPECTED_NODE_LIFECYCLE_STATES = (
    "queued",
    "running",
    "awaiting_approval",
    "paused",
    "succeeded",
    "failed",
    "escalated",
)

EXPECTED_NODE_LIFECYCLE_TERMINAL = frozenset({"succeeded", "failed", "escalated"})


def test_node_lifecycle_state_vocabulary_golden():
    assert NodeLifecycleState.__args__ == EXPECTED_NODE_LIFECYCLE_STATES


def test_node_lifecycle_terminal_lane_set_golden():
    """The sticky-terminal lane set backing lane_for()'s transition rule
    (queued -> running -> {awaiting_approval, paused} -> one of these three,
    then sticky unless explicitly reset to queued/running)."""
    assert signal_mod._TERMINAL == EXPECTED_NODE_LIFECYCLE_TERMINAL


# ---------------------------------------------------------------------------
# 6. FlowEvent shape (lionagi/operations/flow.py)
# ---------------------------------------------------------------------------

EXPECTED_FLOW_EVENT_FIELDS = ("operation_id", "name", "status", "result", "spawned")
EXPECTED_FLOW_EVENT_STATUS_VALUES = frozenset({"completed", "failed", "skipped"})


def test_flow_event_field_set_golden():
    assert dataclasses.is_dataclass(FlowEvent)
    actual = tuple(f.name for f in dataclasses.fields(FlowEvent))
    assert actual == EXPECTED_FLOW_EVENT_FIELDS


@pytest.mark.parametrize("status", sorted(EXPECTED_FLOW_EVENT_STATUS_VALUES))
def test_flow_event_constructible_per_documented_status(status):
    ev = FlowEvent(operation_id="op-1", name="op", status=status, result=None)
    assert ev.status == status
    assert ev.ok == (status == "completed")
