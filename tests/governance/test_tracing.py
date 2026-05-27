# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.protocols.governance.tracing (P19).

Covers:
  - GovernanceSpan: creation, start/end timing, attributes, events, status
  - GovernanceTracer: lifecycle, span recording, isolation
  - SpanName constants
  - Export methods: export() and to_otel_compatible()
  - Convenience functions: trace_gate_evaluation, trace_certificate_mint
  - Edge cases: empty tracer, multiple tracers, already-ended span noop
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from lionagi.protocols.governance.certificate import CertificateGrade, TaskCertificate
from lionagi.protocols.governance.tracing import (
    GovernanceSpan,
    GovernanceTracer,
    SpanEvent,
    SpanName,
    SpanStatus,
    trace_certificate_mint,
    trace_gate_evaluation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tracer(trace_id: str | None = None) -> GovernanceTracer:
    return GovernanceTracer(trace_id=trace_id)


def _make_certificate(grade: CertificateGrade = CertificateGrade.FULL) -> TaskCertificate:
    now = datetime.now(tz=timezone.utc)
    return TaskCertificate(
        session_id="sess-001",
        charter_id="charter-test",
        charter_hash="abc123",
        grade=grade,
        evidence_chain_head="sha256:" + "0" * 64,
        started_at=now,
        completed_at=now + timedelta(milliseconds=250),
        op_count=5,
        ops_allowed=5,
        gate_results_summary={"allow": 5},
    )


# ---------------------------------------------------------------------------
# SpanName constants
# ---------------------------------------------------------------------------


class TestSpanNameConstants:
    def test_gate_evaluation_value(self):
        assert SpanName.GATE_EVALUATION == "gate.evaluate"

    def test_evidence_append_alias(self):
        # backward-compat alias matches canonical name
        assert SpanName.EVIDENCE_APPEND == SpanName.EVIDENCE_EMIT

    def test_break_glass_activate_alias(self):
        assert SpanName.BREAK_GLASS_ACTIVATE == SpanName.BREAK_GLASS_OPEN

    def test_flow_governance_alias(self):
        assert SpanName.FLOW_GOVERNANCE == SpanName.GOVERNANCE_FLOW

    def test_certificate_mint_value(self):
        assert SpanName.CERTIFICATE_MINT == "certificate.mint"

    def test_sod_check_value(self):
        assert SpanName.SOD_CHECK == "sod.check"

    def test_policy_resolve_value(self):
        assert SpanName.POLICY_RESOLVE == "policy.resolve"


# ---------------------------------------------------------------------------
# GovernanceSpan unit tests
# ---------------------------------------------------------------------------


class TestGovernanceSpan:
    def test_span_defaults(self):
        span = GovernanceSpan(trace_id="trace-1", name="gate.evaluate")
        assert span.span_id is not None
        assert len(span.span_id) == 32  # uuid4 hex
        assert span.parent_span_id is None
        assert span.end_time is None
        assert span.status == SpanStatus.UNSET
        assert span.events == []

    def test_start_time_is_recorded(self):
        before = time.time()
        span = GovernanceSpan(trace_id="t", name="test")
        after = time.time()
        assert before <= span.start_time <= after

    def test_is_ended_false_when_open(self):
        span = GovernanceSpan(trace_id="t", name="test")
        assert span.is_ended is False

    def test_is_ended_true_after_end_time_set(self):
        span = GovernanceSpan(trace_id="t", name="test")
        span.end_time = time.time()
        assert span.is_ended is True

    def test_duration_ms_none_while_open(self):
        span = GovernanceSpan(trace_id="t", name="test")
        assert span.duration_ms is None

    def test_duration_ms_calculated_after_end(self):
        span = GovernanceSpan(trace_id="t", name="test")
        span.start_time = 1000.0
        span.end_time = 1000.5
        assert abs(span.duration_ms - 500.0) < 0.001

    def test_to_dict_contains_required_fields(self):
        span = GovernanceSpan(trace_id="trace-xyz", name="gate.evaluate")
        span.end_time = time.time()
        span.status = "ok"
        d = span.to_dict()
        assert d["span_id"] == span.span_id
        assert d["trace_id"] == "trace-xyz"
        assert d["name"] == "gate.evaluate"
        assert d["parent_span_id"] is None
        assert d["start_time"] is not None
        assert d["end_time"] is not None
        assert d["status"] == "ok"
        assert "duration_ms" in d
        assert "attributes" in d
        assert "events" in d

    def test_to_dict_timestamps_are_iso(self):
        span = GovernanceSpan(trace_id="t", name="test")
        span.end_time = time.time()
        d = span.to_dict()
        # ISO strings should parse without error
        datetime.fromisoformat(d["start_time"])
        datetime.fromisoformat(d["end_time"])

    def test_to_dict_end_time_none_while_open(self):
        span = GovernanceSpan(trace_id="t", name="test")
        d = span.to_dict()
        assert d["end_time"] is None

    def test_to_otel_dict_required_fields(self):
        span = GovernanceSpan(trace_id="trace-abc", name="gate.evaluate")
        span.end_time = time.time()
        span.status = "ok"
        otel = span.to_otel_dict()
        assert otel["traceId"] == "trace-abc"
        assert otel["spanId"] == span.span_id
        assert otel["parentSpanId"] is None
        assert otel["name"] == "gate.evaluate"
        assert isinstance(otel["startTimeUnixNano"], int)
        assert isinstance(otel["endTimeUnixNano"], int)
        assert isinstance(otel["attributes"], list)
        assert isinstance(otel["events"], list)
        assert otel["status"] == {"code": "ok"}

    def test_to_otel_dict_end_time_none_while_open(self):
        span = GovernanceSpan(trace_id="t", name="test")
        otel = span.to_otel_dict()
        assert otel["endTimeUnixNano"] is None

    def test_to_otel_dict_attributes_format(self):
        span = GovernanceSpan(
            trace_id="t",
            name="gate.evaluate",
            attributes={"gate.id": "check_registry", "gate.verdict": "ALLOW"},
        )
        otel = span.to_otel_dict()
        keys = {attr["key"] for attr in otel["attributes"]}
        assert "gate.id" in keys
        assert "gate.verdict" in keys

    def test_to_otel_dict_parent_span_id(self):
        span = GovernanceSpan(trace_id="t", name="gate.evaluate", parent_span_id="parent-hex")
        otel = span.to_otel_dict()
        assert otel["parentSpanId"] == "parent-hex"


# ---------------------------------------------------------------------------
# SpanEvent unit tests
# ---------------------------------------------------------------------------


class TestSpanEvent:
    def test_event_timestamp_is_set(self):
        before = time.time()
        event = SpanEvent(name="gate.fired")
        after = time.time()
        assert before <= event.timestamp <= after

    def test_event_to_dict(self):
        event = SpanEvent(name="policy.checked", attributes={"policy.id": "p1"})
        d = event.to_dict()
        assert d["name"] == "policy.checked"
        assert "timestamp" in d
        assert d["attributes"]["policy.id"] == "p1"
        datetime.fromisoformat(d["timestamp"])  # must be ISO

    def test_event_empty_attributes(self):
        event = SpanEvent(name="test")
        assert event.attributes == {}


# ---------------------------------------------------------------------------
# GovernanceTracer tests
# ---------------------------------------------------------------------------


class TestGovernanceTracerInit:
    def test_auto_generates_trace_id(self):
        tracer = GovernanceTracer()
        assert tracer.trace_id is not None
        assert len(tracer.trace_id) == 32  # uuid4 hex

    def test_accepts_explicit_trace_id(self):
        tracer = GovernanceTracer(trace_id="my-trace-001")
        assert tracer.trace_id == "my-trace-001"

    def test_empty_tracer_has_zero_spans(self):
        tracer = GovernanceTracer()
        assert tracer.span_count == 0

    def test_empty_export(self):
        tracer = GovernanceTracer()
        assert tracer.export() == []

    def test_empty_otel_export(self):
        tracer = GovernanceTracer()
        assert tracer.to_otel_compatible() == []


class TestGovernanceTracerStartSpan:
    def test_start_span_returns_governance_span(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        assert isinstance(span, GovernanceSpan)

    def test_span_has_correct_trace_id(self):
        tracer = _make_tracer("tid-123")
        span = tracer.start_span("gate.evaluate")
        assert span.trace_id == "tid-123"

    def test_span_has_correct_name(self):
        tracer = _make_tracer()
        span = tracer.start_span(SpanName.GATE_EVALUATION)
        assert span.name == "gate.evaluate"

    def test_span_increments_span_count(self):
        tracer = _make_tracer()
        tracer.start_span("gate.evaluate")
        assert tracer.span_count == 1
        tracer.start_span("certificate.mint")
        assert tracer.span_count == 2

    def test_span_inherits_schema_version_attribute(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        assert "governance.schema.version" in span.attributes

    def test_span_attributes_are_merged(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate", attributes={"gate.id": "check_registry"})
        assert span.attributes["gate.id"] == "check_registry"
        # schema version also present
        assert "governance.schema.version" in span.attributes

    def test_span_parent_span_id_is_set(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate", parent_span_id="parent-001")
        assert span.parent_span_id == "parent-001"

    def test_span_without_parent_has_none(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        assert span.parent_span_id is None

    def test_span_is_open_after_start(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        assert not span.is_ended


class TestGovernanceTracerEndSpan:
    def test_end_span_sets_end_time(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        before = time.time()
        tracer.end_span(span)
        after = time.time()
        assert before <= span.end_time <= after

    def test_end_span_marks_span_as_ended(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        tracer.end_span(span)
        assert span.is_ended

    def test_end_span_sets_status_ok_default(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        tracer.end_span(span)
        assert span.status == SpanStatus.OK

    def test_end_span_sets_error_status(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        tracer.end_span(span, status=SpanStatus.ERROR)
        assert span.status == SpanStatus.ERROR

    def test_end_span_merges_attributes(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        tracer.end_span(span, attributes={"gate.verdict": "DENY"})
        assert span.attributes["gate.verdict"] == "DENY"

    def test_end_span_noop_when_already_ended(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        tracer.end_span(span, status="ok")
        first_end_time = span.end_time
        tracer.end_span(span, status="error")  # should be no-op
        # Status must not change and end_time must not change
        assert span.status == "ok"
        assert span.end_time == first_end_time

    def test_start_end_timing(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        time.sleep(0.01)  # 10ms sleep
        tracer.end_span(span)
        assert span.duration_ms is not None
        assert span.duration_ms >= 5.0  # at least 5ms to account for variance


class TestGovernanceTracerAddEvent:
    def test_add_event_appends_to_span(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        event = tracer.add_event(span, "policy.checked")
        assert len(span.events) == 1
        assert span.events[0] is event

    def test_add_event_returns_span_event(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        event = tracer.add_event(span, "gate.fired", attributes={"gate.id": "g1"})
        assert isinstance(event, SpanEvent)
        assert event.name == "gate.fired"
        assert event.attributes["gate.id"] == "g1"

    def test_add_multiple_events_in_order(self):
        tracer = _make_tracer()
        span = tracer.start_span("governance.operation")
        tracer.add_event(span, "first")
        tracer.add_event(span, "second")
        tracer.add_event(span, "third")
        names = [e.name for e in span.events]
        assert names == ["first", "second", "third"]

    def test_event_timestamp_within_span_duration(self):
        tracer = _make_tracer()
        span = tracer.start_span("test")
        event = tracer.add_event(span, "midpoint")
        tracer.end_span(span)
        assert span.start_time <= event.timestamp <= span.end_time


class TestGovernanceTracerNested:
    def test_nested_spans_have_correct_parent_linkage(self):
        tracer = _make_tracer()

        root = tracer.start_span(SpanName.GOVERNANCE_OPERATION)
        child = tracer.start_span(SpanName.GATE_EVALUATION, parent_span_id=root.span_id)
        grandchild = tracer.start_span(SpanName.EVIDENCE_EMIT, parent_span_id=child.span_id)

        assert root.parent_span_id is None
        assert child.parent_span_id == root.span_id
        assert grandchild.parent_span_id == child.span_id

    def test_all_nested_spans_share_trace_id(self):
        tracer = _make_tracer("shared-trace")

        root = tracer.start_span("governance.operation")
        child = tracer.start_span("gate.evaluate", parent_span_id=root.span_id)
        leaf = tracer.start_span("sod.check", parent_span_id=child.span_id)

        assert root.trace_id == child.trace_id == leaf.trace_id == "shared-trace"

    def test_nested_span_count(self):
        tracer = _make_tracer()
        for _ in range(5):
            tracer.start_span("gate.evaluate")
        assert tracer.span_count == 5


class TestGovernanceTracerExport:
    def test_export_returns_list_of_dicts(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        tracer.end_span(span)
        result = tracer.export()
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], dict)

    def test_export_contains_all_spans(self):
        tracer = _make_tracer()
        for i in range(3):
            s = tracer.start_span(f"span-{i}")
            tracer.end_span(s)
        result = tracer.export()
        assert len(result) == 3

    def test_export_includes_open_spans(self):
        tracer = _make_tracer()
        tracer.start_span("gate.evaluate")  # not ended
        result = tracer.export()
        assert len(result) == 1
        assert result[0]["end_time"] is None

    def test_export_span_has_required_fields(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate", attributes={"k": "v"})
        tracer.end_span(span)
        d = tracer.export()[0]
        assert "span_id" in d
        assert "trace_id" in d
        assert "name" in d
        assert "start_time" in d
        assert "end_time" in d
        assert "attributes" in d
        assert "events" in d
        assert "status" in d

    def test_export_events_are_serialised(self):
        tracer = _make_tracer()
        span = tracer.start_span("governance.operation")
        tracer.add_event(span, "gate.evaluated", attributes={"gate.id": "g1"})
        tracer.end_span(span)
        d = tracer.export()[0]
        assert len(d["events"]) == 1
        assert d["events"][0]["name"] == "gate.evaluated"


class TestGovernanceTracerOtelExport:
    def test_to_otel_compatible_returns_list(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        tracer.end_span(span)
        result = tracer.to_otel_compatible()
        assert isinstance(result, list)

    def test_otel_span_has_required_fields(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        tracer.end_span(span)
        otel = tracer.to_otel_compatible()[0]
        for field in (
            "traceId",
            "spanId",
            "name",
            "startTimeUnixNano",
            "attributes",
            "events",
            "status",
        ):
            assert field in otel

    def test_otel_trace_id_matches_tracer(self):
        tracer = _make_tracer("my-trace")
        span = tracer.start_span("gate.evaluate")
        tracer.end_span(span)
        otel = tracer.to_otel_compatible()[0]
        assert otel["traceId"] == "my-trace"

    def test_otel_span_id_matches_span(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        tracer.end_span(span)
        otel = tracer.to_otel_compatible()[0]
        assert otel["spanId"] == span.span_id

    def test_otel_start_time_is_nanoseconds(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        otel = tracer.to_otel_compatible()[0]
        # Nanoseconds since epoch should be a large integer
        assert otel["startTimeUnixNano"] > 1_000_000_000_000_000_000

    def test_otel_events_have_time_unix_nano(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate")
        tracer.add_event(span, "policy.resolved")
        tracer.end_span(span)
        otel = tracer.to_otel_compatible()[0]
        assert len(otel["events"]) == 1
        assert "timeUnixNano" in otel["events"][0]

    def test_otel_attributes_are_key_value_pairs(self):
        tracer = _make_tracer()
        span = tracer.start_span("gate.evaluate", attributes={"gate.id": "g1"})
        tracer.end_span(span)
        otel = tracer.to_otel_compatible()[0]
        attr_keys = {a["key"] for a in otel["attributes"]}
        assert "gate.id" in attr_keys


# ---------------------------------------------------------------------------
# Trace isolation (multiple tracers)
# ---------------------------------------------------------------------------


class TestTracerIsolation:
    def test_two_tracers_have_different_trace_ids(self):
        t1 = GovernanceTracer()
        t2 = GovernanceTracer()
        assert t1.trace_id != t2.trace_id

    def test_spans_from_different_tracers_do_not_share(self):
        t1 = GovernanceTracer(trace_id="trace-A")
        t2 = GovernanceTracer(trace_id="trace-B")

        t1.start_span("gate.evaluate")
        t2.start_span("sod.check")

        assert t1.span_count == 1
        assert t2.span_count == 1
        assert t1.export()[0]["trace_id"] == "trace-A"
        assert t2.export()[0]["trace_id"] == "trace-B"

    def test_tracers_with_explicit_trace_ids_are_independent(self):
        t1 = _make_tracer("same-trace")
        t2 = _make_tracer("same-trace")

        t1.start_span("span-a")
        assert t2.span_count == 0  # t2 is isolated even with the same trace_id

    def test_end_span_on_one_tracer_does_not_affect_other(self):
        t1 = _make_tracer()
        t2 = _make_tracer()

        s1 = t1.start_span("gate.evaluate")
        t2.start_span("gate.evaluate")  # open in t2

        tracer_2_export = t2.export()
        assert tracer_2_export[0]["end_time"] is None  # still open

        t1.end_span(s1)
        # t2 span remains open
        assert t2.export()[0]["end_time"] is None


# ---------------------------------------------------------------------------
# Convenience function: trace_gate_evaluation
# ---------------------------------------------------------------------------


class TestTraceGateEvaluation:
    def test_returns_ended_span(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(tracer, "check_registry", "file.write", "ALLOW", 12.5)
        assert span.is_ended

    def test_span_name_is_gate_evaluate(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(tracer, "g1", "tool.op", "ALLOW", 0.0)
        assert span.name == SpanName.GATE_EVALUATION

    def test_span_has_gate_id_attribute(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(tracer, "check_registry", "file.write", "ALLOW", 5.0)
        assert span.attributes["gate.id"] == "check_registry"

    def test_span_has_tool_name_attribute(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(tracer, "g1", "file.write", "ALLOW", 5.0)
        assert span.attributes["gate.tool.name"] == "file.write"

    def test_span_verdict_uppercased(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(tracer, "g1", "tool", "allow", 0.0)
        assert span.attributes["gate.verdict"] == "ALLOW"

    def test_deny_verdict_gives_error_status(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(tracer, "g1", "tool", "DENY", 0.0)
        assert span.status == SpanStatus.ERROR

    def test_allow_verdict_gives_ok_status(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(tracer, "g1", "tool", "ALLOW", 0.0)
        assert span.status == SpanStatus.OK

    def test_advisory_verdict_gives_ok_status(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(tracer, "g1", "tool", "ADVISORY", 0.0)
        assert span.status == SpanStatus.OK

    def test_elapsed_ms_stored(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(tracer, "g1", "tool", "ALLOW", 42.7)
        assert span.attributes["gate.elapsed_ms"] == pytest.approx(42.7)

    def test_parent_span_id_propagated(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(tracer, "g1", "tool", "ALLOW", 0.0, parent_span_id="root-span")
        assert span.parent_span_id == "root-span"

    def test_extra_attributes_merged(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(
            tracer,
            "g1",
            "tool",
            "ALLOW",
            0.0,
            extra_attributes={"gate.charter.id": "charter-001"},
        )
        assert span.attributes["gate.charter.id"] == "charter-001"

    def test_retention_tier_is_immutable(self):
        tracer = _make_tracer()
        span = trace_gate_evaluation(tracer, "g1", "tool", "ALLOW", 0.0)
        assert span.attributes["governance.retention.tier"] == "IMMUTABLE"

    def test_span_is_added_to_tracer(self):
        tracer = _make_tracer()
        trace_gate_evaluation(tracer, "g1", "tool", "ALLOW", 0.0)
        assert tracer.span_count == 1


# ---------------------------------------------------------------------------
# Convenience function: trace_certificate_mint
# ---------------------------------------------------------------------------


class TestTraceCertificateMint:
    def test_returns_ended_span(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        span = trace_certificate_mint(tracer, cert)
        assert span.is_ended

    def test_span_name_is_certificate_mint(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        span = trace_certificate_mint(tracer, cert)
        assert span.name == SpanName.CERTIFICATE_MINT

    def test_span_has_certificate_id(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        span = trace_certificate_mint(tracer, cert)
        assert span.attributes["certificate.id"] == cert.certificate_id

    def test_span_has_grade_attribute(self):
        tracer = _make_tracer()
        cert = _make_certificate(CertificateGrade.PARTIAL)
        span = trace_certificate_mint(tracer, cert)
        assert span.attributes["certificate.grade"] == "partial"

    def test_span_has_duration_ms(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        span = trace_certificate_mint(tracer, cert)
        assert span.attributes["certificate.duration_ms"] == pytest.approx(250.0, abs=1.0)

    def test_span_has_charter_id(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        span = trace_certificate_mint(tracer, cert)
        assert span.attributes["governance.charter.id"] == "charter-test"

    def test_span_has_evidence_chain_hash(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        span = trace_certificate_mint(tracer, cert)
        assert span.attributes["certificate.evidence.chain.hash"] == cert.evidence_chain_head

    def test_retention_tier_is_immutable(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        span = trace_certificate_mint(tracer, cert)
        assert span.attributes["governance.retention.tier"] == "IMMUTABLE"

    def test_status_is_ok(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        span = trace_certificate_mint(tracer, cert)
        assert span.status == SpanStatus.OK

    def test_extra_attributes_merged(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        span = trace_certificate_mint(tracer, cert, extra_attributes={"custom.key": "custom_val"})
        assert span.attributes["custom.key"] == "custom_val"

    def test_parent_span_id_propagated(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        span = trace_certificate_mint(tracer, cert, parent_span_id="flow-span-001")
        assert span.parent_span_id == "flow-span-001"

    def test_span_is_added_to_tracer(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        trace_certificate_mint(tracer, cert)
        assert tracer.span_count == 1

    def test_gates_passed_failed_counts(self):
        tracer = _make_tracer()
        cert = _make_certificate()
        span = trace_certificate_mint(tracer, cert)
        assert span.attributes["certificate.gates.passed"] == 5
        assert span.attributes["certificate.gates.failed"] == 0


# ---------------------------------------------------------------------------
# Full pipeline integration
# ---------------------------------------------------------------------------


class TestFullTracePipeline:
    def test_full_governed_operation_trace(self):
        """Simulate a governed operation trace with nested spans."""
        tracer = _make_tracer()

        # Root operation span
        op_span = tracer.start_span(
            SpanName.GOVERNANCE_OPERATION,
            attributes={
                "governance.actor.id": "agent:worker",
                "governance.actor.role": "worker",
                "governance.session.id": "sess-42",
            },
        )

        # Gate evaluation (child of op_span)
        gate_span = trace_gate_evaluation(
            tracer,
            gate_id="check_registry",
            tool_name="file.read",
            verdict="ALLOW",
            elapsed_ms=3.2,
            parent_span_id=op_span.span_id,
        )

        # Record mid-operation event
        tracer.add_event(op_span, "tool.executed", attributes={"tool": "file.read"})

        # Certificate (child of op_span)
        cert = _make_certificate()
        cert_span = trace_certificate_mint(tracer, cert, parent_span_id=op_span.span_id)

        # Close root span
        tracer.end_span(op_span, status="ok")

        # Verify structure
        assert tracer.span_count == 3
        assert op_span.parent_span_id is None
        assert gate_span.parent_span_id == op_span.span_id
        assert cert_span.parent_span_id == op_span.span_id

        # All share same trace_id
        spans = tracer.export()
        trace_ids = {s["trace_id"] for s in spans}
        assert len(trace_ids) == 1  # single trace

        # All have schema version
        for s in spans:
            assert "governance.schema.version" in s["attributes"]

        # OTel export is non-empty
        otel = tracer.to_otel_compatible()
        assert len(otel) == 3

    def test_export_and_otel_have_same_span_count(self):
        tracer = _make_tracer()
        for i in range(4):
            s = tracer.start_span(f"span-{i}")
            tracer.end_span(s)
        assert len(tracer.export()) == len(tracer.to_otel_compatible()) == 4
