# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Lightweight in-process governance span recorder.

Emits structured spans for every governance event — gate evaluations,
evidence appends, certificate mints, break-glass activations, SoD checks,
policy resolutions, and flow governance steps.

Design principles:
  - ZERO external dependencies (no opentelemetry-sdk).
  - OTel-compatible JSON output — pipe to any OTel collector via the
    ``to_otel_compatible()`` export method.
  - The tracer is the observability hook point: it records what happened,
    not where to send it.

Span naming follows the governance trace naming standard:
  docs/governance/standards/trace-naming.md
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .certificate import TaskCertificate

__all__ = [
    "GovernanceSpan",
    "SpanName",
    "SpanStatus",
    "GovernanceTracer",
    "trace_gate_evaluation",
    "trace_certificate_mint",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Schema version for governance spans — follows trace-naming.md §2 convention.
_SCHEMA_VERSION = "2026-05-27.v1"


class SpanStatus(str):
    """Conventional span status values."""

    OK = "ok"
    ERROR = "error"
    UNSET = "unset"


class SpanName:
    """Canonical span names from docs/governance/standards/trace-naming.md.

    Pattern: ``{domain}.{operation}[.{detail}]``
    """

    # Core governance lifecycle
    GOVERNANCE_SESSION = "governance.session"
    GOVERNANCE_OPERATION = "governance.operation"
    GOVERNANCE_FLOW = "governance.flow"

    # Gate evaluation family
    GATE_EVALUATION = "gate.evaluate"
    GATE_JUSTIFY = "gate.justify"
    GATE_BYPASS = "gate.bypass"

    # Break-glass family
    BREAK_GLASS_OPEN = "breakglass.open"
    BREAK_GLASS_EXPIRE = "breakglass.expire"
    BREAK_GLASS_CLOSE = "breakglass.close"
    BREAK_GLASS_NOTIFY = "breakglass.notify"

    # Evidence family
    EVIDENCE_EMIT = "evidence.emit"
    EVIDENCE_VERIFY = "evidence.verify"

    # Certificate family
    CERTIFICATE_MINT = "certificate.mint"
    CERTIFICATE_STATE = "certificate.state"
    CERTIFICATE_VERIFY = "certificate.verify"

    # Permit family
    PERMIT_ISSUE = "permit.issue"
    PERMIT_CONSUME = "permit.consume"
    PERMIT_REVOKE = "permit.revoke"

    # Charter family
    CHARTER_LOAD = "charter.load"
    CHARTER_EVALUATE = "charter.evaluate"
    CHARTER_VIOLATION = "charter.violation"

    # SoD / registry / policy
    SOD_CHECK = "sod.check"
    REGISTRY_LOOKUP = "registry.lookup"
    POLICY_RESOLVE = "policy.resolve"

    # Aliases kept for backward compatibility with task spec
    EVIDENCE_APPEND = "evidence.emit"
    BREAK_GLASS_ACTIVATE = "breakglass.open"
    FLOW_GOVERNANCE = "governance.flow"


# ---------------------------------------------------------------------------
# SpanEvent: a timed point-in-time event inside a span
# ---------------------------------------------------------------------------


class SpanEvent(BaseModel):
    """A named event recorded at a point in time within a span.

    Attributes:
        name:        Short event label, e.g. ``"gate.fired"`` or ``"evidence.appended"``.
        timestamp:   Unix epoch seconds (float) when the event occurred.
        attributes:  Arbitrary key-value metadata associated with the event.
    """

    name: str
    timestamp: float = Field(default_factory=time.time)
    attributes: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to plain dict with ISO-formatted timestamp."""
        return {
            "name": self.name,
            "timestamp": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
            "attributes": dict(self.attributes),
        }


# ---------------------------------------------------------------------------
# GovernanceSpan
# ---------------------------------------------------------------------------


class GovernanceSpan(BaseModel):
    """A single governance span.

    Spans are created by ``GovernanceTracer.start_span()`` and completed by
    ``GovernanceTracer.end_span()``.  An ended span is immutable — further
    attribute writes via ``end_span`` are applied before the span is marked
    done.

    Attributes:
        span_id:        Unique hex identifier for this span.
        parent_span_id: Hex identifier of the parent span, or ``None`` for roots.
        trace_id:       Identifier shared by all spans in one logical trace.
        name:           Span name, e.g. ``"gate.evaluate"``.
        start_time:     Unix epoch seconds (float) when the span was started.
        end_time:       Unix epoch seconds (float) when the span was ended,
                        or ``None`` while the span is still open.
        attributes:     Key-value metadata attached to this span.
        events:         Ordered list of point-in-time events within this span.
        status:         Completion status: ``"ok"``, ``"error"``, or ``"unset"``.
    """

    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    parent_span_id: str | None = None
    trace_id: str
    name: str
    start_time: float = Field(default_factory=time.time)
    end_time: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[SpanEvent] = Field(default_factory=list)
    status: str = SpanStatus.UNSET

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    @property
    def duration_ms(self) -> float | None:
        """Wall-clock duration in milliseconds, or ``None`` if still open."""
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time) * 1000.0

    @property
    def is_ended(self) -> bool:
        """True once ``end_span`` has been called."""
        return self.end_time is not None

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict with ISO-formatted timestamps."""
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "trace_id": self.trace_id,
            "name": self.name,
            "start_time": datetime.fromtimestamp(self.start_time, tz=timezone.utc).isoformat(),
            "end_time": (
                datetime.fromtimestamp(self.end_time, tz=timezone.utc).isoformat()
                if self.end_time is not None
                else None
            ),
            "duration_ms": self.duration_ms,
            "attributes": dict(self.attributes),
            "events": [e.to_dict() for e in self.events],
            "status": self.status,
        }

    def to_otel_dict(self) -> dict[str, Any]:
        """Return an OTel-compatible span representation.

        The structure mirrors the OTel SDK ``ReadableSpan`` JSON format so
        users can forward it directly to an OTel collector endpoint::

            POST /v1/traces
            Content-Type: application/json

        No opentelemetry-sdk dependency is required.
        """
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "startTimeUnixNano": int(self.start_time * 1_000_000_000),
            "endTimeUnixNano": (
                int(self.end_time * 1_000_000_000) if self.end_time is not None else None
            ),
            "attributes": [
                {"key": k, "value": {"stringValue": str(v)}} for k, v in self.attributes.items()
            ],
            "events": [
                {
                    "name": ev.name,
                    "timeUnixNano": int(ev.timestamp * 1_000_000_000),
                    "attributes": [
                        {"key": k, "value": {"stringValue": str(v)}}
                        for k, v in ev.attributes.items()
                    ],
                }
                for ev in self.events
            ],
            "status": {"code": self.status},
        }


# ---------------------------------------------------------------------------
# GovernanceTracer
# ---------------------------------------------------------------------------


class GovernanceTracer:
    """Lightweight in-process governance span recorder.

    One tracer per logical trace.  Multiple tracers in the same process are
    fully isolated — each has its own ``trace_id`` and span list.

    Usage::

        tracer = GovernanceTracer()

        span = tracer.start_span(SpanName.GATE_EVALUATION, attributes={
            "gate.id": "check_registry",
            "gate.tool.name": "file.write",
        })
        # ... do work ...
        tracer.end_span(span, status="ok", attributes={"gate.verdict": "ALLOW"})

        spans = tracer.export()
        otel_spans = tracer.to_otel_compatible()

    Args:
        trace_id: Identifier for this trace.  A new UUID hex is minted when
                  omitted.
    """

    def __init__(self, trace_id: str | None = None) -> None:
        self._trace_id: str = trace_id if trace_id is not None else uuid.uuid4().hex
        self._spans: list[GovernanceSpan] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def trace_id(self) -> str:
        """The trace identifier shared by all spans in this tracer."""
        return self._trace_id

    @property
    def span_count(self) -> int:
        """Total number of spans recorded (open or closed)."""
        return len(self._spans)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def start_span(
        self,
        name: str,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> GovernanceSpan:
        """Open a new span and begin timing.

        The span is added to the internal list immediately.  It remains open
        (``is_ended == False``) until ``end_span`` is called.

        Args:
            name:           Span name — use a ``SpanName`` constant.
            parent_span_id: ``span_id`` of the parent span for nested traces.
            attributes:     Initial key-value attributes for this span.

        Returns:
            The newly created ``GovernanceSpan``.
        """
        base_attrs: dict[str, Any] = {
            "governance.schema.version": _SCHEMA_VERSION,
        }
        if attributes:
            base_attrs.update(attributes)

        span = GovernanceSpan(
            trace_id=self._trace_id,
            name=name,
            parent_span_id=parent_span_id,
            attributes=base_attrs,
        )
        self._spans.append(span)
        return span

    def end_span(
        self,
        span: GovernanceSpan,
        status: str = SpanStatus.OK,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Close a span, record its end time, and set the final status.

        Calling ``end_span`` on an already-ended span is a no-op.

        Args:
            span:       The span to close (must have been opened by this tracer).
            status:     Final status — typically ``"ok"`` or ``"error"``.
            attributes: Additional attributes merged into the span at close time.
        """
        if span.is_ended:
            return
        if attributes:
            span.attributes.update(attributes)
        span.end_time = time.time()
        span.status = status

    def add_event(
        self,
        span: GovernanceSpan,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> SpanEvent:
        """Record a named point-in-time event within a span.

        Events can be added to both open and ended spans, though in practice
        they are only meaningful while the span is still open.

        Args:
            span:       The span to attach the event to.
            name:       Short event label.
            attributes: Key-value metadata for the event.

        Returns:
            The recorded ``SpanEvent``.
        """
        event = SpanEvent(name=name, attributes=attributes or {})
        span.events.append(event)
        return event

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export(self) -> list[dict[str, Any]]:
        """Return all recorded spans as plain dicts.

        Both open and ended spans are included.  Each dict is the result of
        ``GovernanceSpan.to_dict()``.

        Returns:
            Ordered list of span dicts, in insertion order.
        """
        return [s.to_dict() for s in self._spans]

    def to_otel_compatible(self) -> list[dict[str, Any]]:
        """Return all spans in OTel-compatible format.

        The format mirrors the OTel SDK ``ReadableSpan`` JSON structure.
        Consumers can pipe this directly into any OTel-compliant collector
        endpoint without installing opentelemetry-sdk.

        Required fields present on every output span:

        - ``traceId`` — hex trace identifier
        - ``spanId`` — hex span identifier
        - ``parentSpanId`` — hex parent or ``None`` for roots
        - ``name`` — span name string
        - ``startTimeUnixNano`` — epoch nanoseconds
        - ``endTimeUnixNano`` — epoch nanoseconds or ``None``
        - ``attributes`` — list of OTel attribute key-value objects
        - ``events`` — list of OTel event objects
        - ``status`` — status object with ``code`` field

        Returns:
            Ordered list of OTel-compatible span dicts.
        """
        return [s.to_otel_dict() for s in self._spans]


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def trace_gate_evaluation(
    tracer: GovernanceTracer,
    gate_id: str,
    tool_name: str,
    verdict: str,
    elapsed_ms: float,
    parent_span_id: str | None = None,
    extra_attributes: dict[str, Any] | None = None,
) -> GovernanceSpan:
    """Record a ``gate.evaluate`` span and immediately close it.

    This is the primary convenience entry point for gate evaluation tracing.
    All gate evaluations should call this function to ensure consistent
    attribute naming per the trace-naming standard.

    Args:
        tracer:           The active ``GovernanceTracer`` for this operation.
        gate_id:          ``gate.id`` attribute — the gate function identifier.
        tool_name:        ``gate.tool.name`` — the tool being guarded.
        verdict:          ``gate.verdict`` — e.g. ``"ALLOW"``, ``"DENY"``, ``"ADVISORY"``.
        elapsed_ms:       Wall-clock gate evaluation time in milliseconds.
        parent_span_id:   Optional parent span linkage.
        extra_attributes: Additional attributes merged into the span.

    Returns:
        The completed (ended) ``GovernanceSpan``.
    """
    attrs: dict[str, Any] = {
        "gate.id": gate_id,
        "gate.tool.name": tool_name,
        "gate.verdict": verdict.upper(),
        "gate.elapsed_ms": elapsed_ms,
        "governance.retention.tier": "IMMUTABLE",
    }
    if extra_attributes:
        attrs.update(extra_attributes)

    span = tracer.start_span(
        SpanName.GATE_EVALUATION,
        parent_span_id=parent_span_id,
        attributes=attrs,
    )
    status = SpanStatus.ERROR if verdict.upper() == "DENY" else SpanStatus.OK
    tracer.end_span(span, status=status)
    return span


def trace_certificate_mint(
    tracer: GovernanceTracer,
    certificate: TaskCertificate,
    parent_span_id: str | None = None,
    extra_attributes: dict[str, Any] | None = None,
) -> GovernanceSpan:
    """Record a ``certificate.mint`` span and immediately close it.

    All certificate minting should call this function so the minting event
    is durably represented in the trace.

    Args:
        tracer:           The active ``GovernanceTracer`` for this operation.
        certificate:      The ``TaskCertificate`` that was just minted.
        parent_span_id:   Optional parent span linkage.
        extra_attributes: Additional attributes merged into the span.

    Returns:
        The completed (ended) ``GovernanceSpan``.
    """
    duration_ms = (certificate.completed_at - certificate.started_at).total_seconds() * 1000.0
    attrs: dict[str, Any] = {
        "certificate.id": str(certificate.id),
        "certificate.task.id": certificate.session_id,
        "certificate.grade": certificate.grade.value,
        "certificate.gates.passed": certificate.ops_allowed,
        "certificate.gates.failed": (certificate.op_count - certificate.ops_allowed),
        "certificate.evidence.chain.hash": certificate.evidence_chain_head,
        "certificate.duration_ms": duration_ms,
        "governance.retention.tier": "IMMUTABLE",
        "governance.charter.id": certificate.charter_id,
    }
    if extra_attributes:
        attrs.update(extra_attributes)

    span = tracer.start_span(
        SpanName.CERTIFICATE_MINT,
        parent_span_id=parent_span_id,
        attributes=attrs,
    )
    tracer.end_span(span, status=SpanStatus.OK)
    return span
