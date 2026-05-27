# ADR-0070: Governance Tracing and Observability

**Status**: accepted
**Date**: 2026-05-27
**Depends on**: [ADR-0041](ADR-0041-immutable-evidence-nodes.md) (EvidenceChain), [ADR-0042](ADR-0042-task-certificate.md) (TaskCertificate), [ADR-0044](ADR-0044-tool-gates.md) (GateResult), [ADR-0045](ADR-0045-break-glass-protocol.md) (BreakGlassWindow)
**Related**: [ADR-0048](ADR-0048-agent-segregation-of-duties.md) (SoD), [ADR-0049](ADR-0049-log-tier-governance.md) (LogTier), [ADR-0050](ADR-0050-operation-context.md) (OperationContext), [ADR-0052](ADR-0052-policy-resolution.md) (PolicyResolution)

## Context

Every governance decision in a governed lionagi session — gate evaluations, certificate minting,
break-glass activations, segregation-of-duties checks, and policy resolutions — must be auditable
after the fact. Today the authoritative records for these events are `ImmutableEvidenceNode`
instances (ADR-0041), `GateResult` objects (ADR-0044), and `TaskCertificate` records (ADR-0042).
Those typed records answer "what happened and can it be verified?" but they do not integrate with
the distributed tracing vocabulary that enterprise observability stacks speak: Datadog, Grafana
Tempo, Jaeger, Honeycomb, and OpenTelemetry-compliant collectors all ingest spans with a defined
structure. Without a span layer, operations teams must build bespoke bridges between lionagi's
evidence records and their existing dashboards.

The gap becomes concrete in three ways:

1. **Alert latency.** A security engineer who needs "alert me when any hard gate denial fires"
   cannot write a Datadog monitor against raw Python objects. Spans with structured attributes can
   be indexed and alerted on without custom exporters.

2. **Distributed trace correlation.** When a governed session runs inside a larger request trace
   (HTTP service, orchestrator pipeline), there is no standard mechanism to attach governance
   sub-spans to the parent trace. A span emitted with a W3C-compatible `trace_id` can be joined to
   the parent trace by any OTel-compatible backend.

3. **In-process testing.** Governance unit tests need a way to assert "this gate was evaluated
   with these attributes." Asserting against spans in memory is far easier than asserting against
   raw evidence node content-hashes.

The constraint that shapes every design option is **zero external dependencies**. lionagi is a
library installed into diverse Python environments. Requiring `opentelemetry-sdk` as a hard
dependency would conflict with users' existing OTel configurations, add ~5 MB to the install
footprint, and introduce transitive version constraints. The tracing layer must work without any
external package.

The secondary constraint is **span authority**. Governance spans are not general application
performance traces. They carry governance decisions with legal and compliance significance. The
authoritative record for any governance decision is always the typed evidence node, not the span.
Spans are projections — they make the evidence observable to external tools. They must be
consistent with the evidence but must never substitute for it.

## Decision

Introduce `GovernanceTracer` as a lightweight in-process span recorder that satisfies both the
zero-dependency constraint and the OTel-compatibility requirement. The tracer records
`GovernanceSpan` objects in memory and exports them in OTel-compatible JSON format on demand.

The design has five load-bearing choices:

**1. OTel-compatible but not OTel-dependent.** `GovernanceSpan.to_otel_dict()` produces a dict
that mirrors the OTel SDK `ReadableSpan` JSON structure — including `traceId`, `spanId`,
`startTimeUnixNano`, `endTimeUnixNano`, `attributes` as key-value pairs, and a `status` object.
A user who installs `opentelemetry-sdk` can feed this dict directly into a
`BatchSpanProcessor` without any translation layer. A user who does not install it still gets
structured span data they can serialize to JSONL and forward with any HTTP client.

**2. One tracer per logical trace.** `GovernanceTracer` takes an optional `trace_id` at
construction. All spans produced by one tracer share that `trace_id`. When a governed session
spans multiple operations, the session can create one tracer at the start and pass it through,
producing a single coherent trace tree. Alternatively, each operation can create its own tracer
for isolated in-process collection.

**3. SpanName constants as a controlled vocabulary.** Hard-coded string span names scatter naming
decisions across call sites and produce inconsistently-named traces. `SpanName` is a class of
class-level string constants whose values follow the `{domain}.{operation}[.{detail}]` pattern
documented in `docs/governance/standards/trace-naming.md`. Every call site imports from
`SpanName`; the naming standard is enforced at the import level, not by convention.

**4. Convenience functions for common patterns.** The two highest-volume governance spans —
`gate.evaluate` and `certificate.mint` — each have a dedicated convenience function
(`trace_gate_evaluation`, `trace_certificate_mint`) that open, populate, and close a span in a
single call. This eliminates the start/end boilerplate and ensures the standard required
attributes for those span types are always present.

**5. Export is pull-based.** The tracer accumulates spans in a list. Callers export them via
`tracer.export()` (plain dicts) or `tracer.to_otel_compatible()` (OTel format) on demand.
There is no push, no background thread, and no network I/O in the tracer itself. The caller is
responsible for routing exported spans to any external system.

## Span Schema

### GovernanceSpan Fields

| Field | Type | Description |
|-------|------|-------------|
| `span_id` | `str` (hex UUID) | Unique identifier for this span. |
| `trace_id` | `str` (hex UUID) | Shared identifier for all spans in this logical trace. |
| `parent_span_id` | `str \| None` | Hex span ID of the parent span, or `None` for root spans. |
| `name` | `str` | Span name — must be a `SpanName` constant. |
| `start_time` | `float` | Unix epoch seconds (float) at span start. |
| `end_time` | `float \| None` | Unix epoch seconds at span close, or `None` while open. |
| `attributes` | `dict[str, Any]` | Structured key-value metadata. See attribute schemas below. |
| `events` | `list[SpanEvent]` | Ordered list of point-in-time events within the span. |
| `status` | `str` | Completion status: `"ok"`, `"error"`, or `"unset"`. |

`duration_ms` is a derived property: `(end_time - start_time) * 1000`. It is included in
`to_dict()` output but is not a stored field.

Every span receives `governance.schema.version` in its attributes at construction time. The
current schema version is `"2026-05-27.v1"`.

### SpanEvent Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Short event label, e.g. `"gate.fired"` or `"evidence.appended"`. |
| `timestamp` | `float` | Unix epoch seconds when the event occurred. |
| `attributes` | `dict[str, Any]` | Key-value metadata for the event. |

### Attribute Schemas by Span Type

The following tables list the standard attributes for each governance span type. These attributes
align with the Span Registry in `docs/governance/standards/trace-naming.md`. Attributes not
listed here are permitted but must not conflict with the listed keys.

#### gate.evaluate

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `governance.schema.version` | string | Yes | Schema version constant. |
| `governance.retention.tier` | string | Yes | Always `"IMMUTABLE"` for gate evaluations. |
| `gate.id` | string | Yes | Gate function identifier, e.g. `"verify_in_registry"`. |
| `gate.tool.name` | string | Yes | Tool name being guarded, e.g. `"file.write"`. |
| `gate.verdict` | string | Yes | Uppercase: `"ALLOW"`, `"DENY"`, or `"ADVISORY"`. |
| `gate.enforcement` | string | Yes | `"HARD"`, `"SOFT"`, or `"ADVISORY"`. |
| `gate.elapsed_ms` | float | Yes | Wall-clock gate evaluation time in milliseconds. |
| `gate.evidence.hash` | string | Yes | SHA-256 hash of the corresponding `GateResult` node. |
| `gate.reason` | string | Yes | Human-readable explanation of the verdict. |
| `gate.policy.version` | string | Yes | Policy release identifier active at evaluation time. |
| `gate.charter.id` | string | Yes | Charter identifier active at evaluation time. |
| `governance.severity` | string | Recommended | `"INFO"` for ALLOW, `"ERROR"` for DENY, `"WARN"` for soft override. |

The span `status` is set to `"error"` when `gate.verdict == "DENY"` and `"ok"` otherwise.

#### certificate.mint

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `governance.schema.version` | string | Yes | Schema version constant. |
| `governance.retention.tier` | string | Yes | Always `"IMMUTABLE"` for certificate minting. |
| `certificate.id` | string | Yes | `TaskCertificate.certificate_id`. |
| `certificate.task.id` | string | Yes | `TaskCertificate.session_id`. |
| `certificate.grade` | string | Yes | Certificate grade value (e.g. `"A"`, `"DEGRADED"`). |
| `certificate.gates.passed` | int | Yes | Count of gates that returned ALLOW. |
| `certificate.gates.failed` | int | Yes | Count of gates that returned DENY. |
| `certificate.evidence.chain.hash` | string | Yes | Hash of the evidence chain head at minting time. |
| `certificate.duration_ms` | float | Yes | Wall-clock task duration from start to certificate mint. |
| `governance.charter.id` | string | Yes | Charter identifier under which the certificate was minted. |
| `certificate.defensibility` | string | Recommended | `"FULL"`, `"DEGRADED"`, or `"INVALID"`. |
| `certificate.break.glass` | boolean | Recommended | `true` if any break-glass window was active during the task. |

#### breakglass.open

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `governance.schema.version` | string | Yes | Schema version constant. |
| `governance.retention.tier` | string | Yes | Always `"IMMUTABLE"`. |
| `breakglass.window.id` | string | Yes | Unique identifier for this emergency window. |
| `breakglass.activated.at` | string | Yes | ISO-8601 UTC timestamp of activation. |
| `breakglass.requested.by` | string | Yes | Actor ID of the requesting party. |
| `breakglass.approved.by` | string | Yes | Actor ID of the approving authority. |
| `breakglass.reason` | string | Yes | Reason code: `"incident_response"`, `"data_loss_prevention"`, etc. |
| `breakglass.max.duration` | string | Yes | Maximum window lifetime, e.g. `"30m"`. |
| `breakglass.evidence.hash` | string | Yes | Hash of the `BreakGlassEvent` evidence node. |
| `governance.severity` | string | Yes | Always `"CRITICAL"` for break-glass activation. |

#### sod.check

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `governance.schema.version` | string | Yes | Schema version constant. |
| `governance.retention.tier` | string | Yes | Always `"IMMUTABLE"`. |
| `sod.role` | string | Yes | Role being checked against the SoD rule. |
| `sod.capability` | string | Yes | Capability or tool the role is attempting to access. |
| `sod.verdict` | string | Yes | `"PASS"` or `"FAIL"`. |
| `sod.policy.version` | string | Yes | SoD policy release version. |
| `sod.evidence.hash` | string | Recommended | Hash of the `SoDCheckResult` evidence node. |
| `governance.severity` | string | Recommended | `"INFO"` for PASS, `"ERROR"` for FAIL. |

#### policy.resolve

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `governance.schema.version` | string | Yes | Schema version constant. |
| `governance.retention.tier` | string | Yes | Always `"IMMUTABLE"`. |
| `policy.count` | int | Yes | Number of policies evaluated. |
| `policy.strategy` | string | Yes | Resolution strategy used, e.g. `"most_restrictive"`. |
| `policy.winner` | string | Yes | Identifier of the winning policy. |
| `policy.version` | string | Yes | Active policy release version. |
| `policy.conflict.count` | int | Yes | Number of policy conflicts detected before resolution. |
| `policy.evidence.hash` | string | Recommended | Hash of the `PolicyResolutionRecord` evidence node. |

## SpanName Vocabulary

`SpanName` is a class of string constants whose values follow the
`{domain}.{operation}[.{detail}]` naming pattern. All constants are cross-referenced to their
canonical definitions in `docs/governance/standards/trace-naming.md`.

### Governance Session and Operation Lifecycle

| Constant | Value | What It Tracks |
|----------|-------|----------------|
| `GOVERNANCE_SESSION` | `"governance.session"` | Root span for an entire governed session. Created once per session; parent of all operation spans. |
| `GOVERNANCE_OPERATION` | `"governance.operation"` | One governed tool invocation (pre-gate through post-gate). Child of `governance.session`. |
| `GOVERNANCE_FLOW` | `"governance.flow"` | A multi-operation `Session.flow()` DAG governed as a unit. Child of `governance.session`. |

### Gate Evaluation Family

| Constant | Value | What It Tracks |
|----------|-------|----------------|
| `GATE_EVALUATION` | `"gate.evaluate"` | Single gate function evaluation against one tool call. The primary enforcement-point span. |
| `GATE_JUSTIFY` | `"gate.justify"` | A soft-gate denial that was overridden with explicit human justification evidence. |
| `GATE_BYPASS` | `"gate.bypass"` | Gate bypassed under a break-glass window; records the window ID and bypass authority. |

### Break-Glass Lifecycle Family

| Constant | Value | What It Tracks |
|----------|-------|----------------|
| `BREAK_GLASS_OPEN` | `"breakglass.open"` | Emergency window activation; CRITICAL severity. |
| `BREAK_GLASS_EXPIRE` | `"breakglass.expire"` | Window reached its maximum duration without explicit close. |
| `BREAK_GLASS_CLOSE` | `"breakglass.close"` | Window closed explicitly by an authorized actor. |
| `BREAK_GLASS_NOTIFY` | `"breakglass.notify"` | Notification dispatched to an on-call target during or after a break-glass event. |

The alias `BREAK_GLASS_ACTIVATE` maps to `BREAK_GLASS_OPEN` for backward compatibility.

### Evidence Family

| Constant | Value | What It Tracks |
|----------|-------|----------------|
| `EVIDENCE_EMIT` | `"evidence.emit"` | A new `EvidenceNode` appended to an `EvidenceChain`. |
| `EVIDENCE_VERIFY` | `"evidence.verify"` | A chain integrity verification run, including the result and failure reason if applicable. |

The alias `EVIDENCE_APPEND` maps to `EVIDENCE_EMIT` for backward compatibility.

### Certificate Family

| Constant | Value | What It Tracks |
|----------|-------|----------------|
| `CERTIFICATE_MINT` | `"certificate.mint"` | Task certificate minted at the end of a governed run. |
| `CERTIFICATE_STATE` | `"certificate.state"` | Certificate state transition (e.g. from `PENDING` to `VALID` or `SUPERSEDED`). |
| `CERTIFICATE_VERIFY` | `"certificate.verify"` | Certificate verification query, including replay-detection result. |

### Permit Family

| Constant | Value | What It Tracks |
|----------|-------|----------------|
| `PERMIT_ISSUE` | `"permit.issue"` | JIT permit issued to an actor for a bounded capability. |
| `PERMIT_CONSUME` | `"permit.consume"` | JIT permit consumed by an actor invoking the permitted tool. |
| `PERMIT_REVOKE` | `"permit.revoke"` | JIT permit revoked before natural expiry. |

### Charter Family

| Constant | Value | What It Tracks |
|----------|-------|----------------|
| `CHARTER_LOAD` | `"charter.load"` | Charter DSL loaded and compiled into active gate configuration. |
| `CHARTER_EVALUATE` | `"charter.evaluate"` | Single charter constraint evaluation against an operation. |
| `CHARTER_VIOLATION` | `"charter.violation"` | Charter constraint violated; carries severity and reason. |

### Registry, SoD, and Policy Family

| Constant | Value | What It Tracks |
|----------|-------|----------------|
| `SOD_CHECK` | `"sod.check"` | Segregation-of-duties check for a role-capability pair. |
| `REGISTRY_LOOKUP` | `"registry.lookup"` | Tool registry allowlist lookup for a (tool, role) pair. |
| `POLICY_RESOLVE` | `"policy.resolve"` | Policy conflict resolution, producing one winning policy. |

## Integration Patterns

### Pattern 1: In-Process Collection for Testing

The simplest integration requires no external dependencies. Create a tracer before the operation
under test and assert against its spans afterward:

```python
from lionagi.protocols.governance.tracing import GovernanceTracer, SpanName

tracer = GovernanceTracer()

# ... execute governed operation ...

spans = tracer.export()
gate_spans = [s for s in spans if s["name"] == SpanName.GATE_EVALUATION]
assert len(gate_spans) == 1
assert gate_spans[0]["attributes"]["gate.verdict"] == "ALLOW"
assert gate_spans[0]["status"] == "ok"
```

This pattern works without any installed observability packages and is the recommended approach
for governance unit tests.

### Pattern 2: OTel Export to a Collector

Users who have `opentelemetry-sdk` installed can feed exported spans directly into an OTel SDK
span processor:

```python
from opentelemetry.sdk.trace.export import BatchSpanExporter
from opentelemetry.sdk.trace import ReadableSpan

from lionagi.protocols.governance.tracing import GovernanceTracer

tracer = GovernanceTracer()

# ... governed operations ...

otel_dicts = tracer.to_otel_compatible()

# Convert dicts to ReadableSpan objects via OTel SDK (no custom adapter needed
# when the dict structure matches the SDK's internal JSON representation).
# Alternatively, POST otel_dicts directly to a collector HTTP endpoint:
#
#   import httpx
#   httpx.post("https://otel-collector/v1/traces", json={"resourceSpans": [...]})
```

The `to_otel_compatible()` method returns a list of dicts whose keys (`traceId`, `spanId`,
`parentSpanId`, `startTimeUnixNano`, `endTimeUnixNano`, `attributes`, `events`, `status`) match
the OTel Protocol JSON representation. No translation layer is required.

### Pattern 3: Custom JSON/JSONL Export

For deployments that ingest spans via log forwarding (Filebeat, Fluentd, Loki) rather than OTel
collectors:

```python
import json
from lionagi.protocols.governance.tracing import GovernanceTracer

tracer = GovernanceTracer()

# ... governed operations ...

with open("governance-spans.jsonl", "a") as fh:
    for span in tracer.export():
        fh.write(json.dumps(span) + "\n")
```

`tracer.export()` returns plain Python dicts with ISO-8601 timestamps. The output is suitable
for any JSON-based log ingestion pipeline.

### Pattern 4: Evidence Chain Correlation

Governance spans are projections over typed evidence records. They are correlated to their
authoritative evidence nodes via hash attributes. When a gate evaluation fires:

1. The gate creates a `GateResult` evidence node (ADR-0044) and appends it to the session's
   `EvidenceChain` (ADR-0041). The node carries a `node_hash`.
2. `trace_gate_evaluation()` records a `gate.evaluate` span. The caller passes
   `extra_attributes={"gate.evidence.hash": gate_result.node_hash}` to embed the correlation
   pointer in the span.
3. An external auditor who receives the span can locate the authoritative `GateResult` record by
   looking up the hash in the evidence store.

This one-way reference (span points to evidence; evidence does not point to spans) keeps the
evidence chain independent of the observability layer. Evidence correctness does not depend on
span completeness.

### Pattern 5: Parent Span Linkage

Governance sub-spans can be nested under a parent trace span for distributed correlation:

```python
from lionagi.protocols.governance.tracing import (
    GovernanceTracer, SpanName, trace_gate_evaluation
)

tracer = GovernanceTracer(trace_id="<w3c-trace-id-from-parent>")

# Create a root operation span
op_span = tracer.start_span(
    SpanName.GOVERNANCE_OPERATION,
    attributes={"governance.operation.name": "invoke_tool"},
)

# Gate evaluation is a child of the operation span
gate_span = trace_gate_evaluation(
    tracer=tracer,
    gate_id="check_registry",
    tool_name="file.write",
    verdict="ALLOW",
    elapsed_ms=1.2,
    parent_span_id=op_span.span_id,
)

tracer.end_span(op_span)
```

Setting `trace_id` to a W3C-compatible hex string allows governance spans to be joined to a
parent distributed trace in any OTel-compatible backend.

## Sampling and Performance

### Sampling Policy

The retention tier of each span type governs its sampling obligation:

| Tier | Obligation | Rationale |
|------|------------|-----------|
| `IMMUTABLE` | 100% — never drop | Gate decisions, certificates, break-glass, evidence, permits, charter events, SoD checks, registry decisions, policy resolutions carry compliance significance. Dropping any of these is an audit gap. |
| `PROTECTED` | 100% for governed sessions | Session and flow lifecycle summaries; dropped only for ungoverned dev sessions where governance guarantees are explicitly not claimed. |
| `MUTABLE` | May be sampled | Debug-only operational spans that do not carry required governance evidence. |

The `GovernanceTracer` itself does not implement sampling — it records all spans passed to it.
Sampling decisions belong to the export layer. When export is backpressured, write local evidence
first and mark the export failure with a protected operational evidence record rather than
silently dropping the span.

The following span types must never be sampled away regardless of export pressure:

- All `gate.*` spans with verdict `"DENY"`
- All `breakglass.*` spans
- All `certificate.*` spans
- All `permit.*` spans
- All `sod.check` spans with verdict `"FAIL"`
- All `charter.violation` spans

### Performance Characteristics

`GovernanceSpan` construction is O(1): it allocates a `GovernanceSpan` Pydantic model, calls
`time.time()` once, generates a `uuid.uuid4().hex` span ID, and appends to a list.

`GovernanceTracer.export()` is O(n) in the number of recorded spans, where n is bounded by the
number of governance events in one session. Governance events are low-frequency relative to
application request volume — a typical governed session with 10 tool calls produces on the order
of 10–50 spans, not thousands.

`to_otel_compatible()` is O(n) and allocates one dict per span. For large sessions or batch
export, callers can iterate `tracer._spans` directly and serialize in chunks:

```python
for span in tracer._spans:
    process(span.to_otel_dict())
```

There is no background thread, no lock, and no I/O in the tracer core. It is safe to create and
use from any coroutine context.

## Redaction Before External Export

Governance spans may contain decision rationale, actor identifiers, and tool names. Before
exporting spans to external systems, callers must apply redaction appropriate to their
deployment's data governance policy:

```python
import copy
from lionagi.protocols.governance.tracing import GovernanceTracer

REDACT_KEYS = {"gate.reason", "breakglass.reason", "breakglass.requested.by"}

def redacted_export(tracer: GovernanceTracer) -> list[dict]:
    result = []
    for span in tracer.export():
        span_copy = copy.deepcopy(span)
        for key in REDACT_KEYS:
            if key in span_copy["attributes"]:
                span_copy["attributes"][key] = "<redacted>"
        result.append(span_copy)
    return result
```

Hashes (`gate.evidence.hash`, `breakglass.evidence.hash`) do not require redaction because they
are SHA-256 digests that reveal nothing about the underlying content.

## Non-Goals

The following concerns are explicitly out of scope for this ADR and for `GovernanceTracer`:

**No distributed span propagation.** `GovernanceTracer` does not implement W3C TraceContext
injection or extraction. In-process span correlation via `trace_id` and `parent_span_id` is
supported.

**No automatic instrumentation of non-governance code.** `GovernanceTracer` only records spans
for governance events. Application-level performance tracing (HTTP request durations, database
query times) is the responsibility of the application's chosen OTel SDK integration.

**No OTel SDK dependency.** The `opentelemetry-sdk` package must not appear in lionagi's
dependencies. `GovernanceSpan.to_otel_dict()` produces an OTel-compatible dict; wiring that
dict into OTel SDK types is the caller's responsibility.

**No tenant-specific trace routing.** All spans are emitted to the same in-process list.

**No span persistence.** `GovernanceTracer` holds spans in memory for the lifetime of the tracer
object. Persistence to a durable store is out of scope; callers who need persistence export
spans to JSONL (Pattern 3 above) or feed them to a collector.

**No trace context injection into outbound HTTP.** The tracer does not intercept requests made by
governed tools and does not inject trace headers. This is the application's concern, not
governance's.

## Security Considerations

### Span Sensitivity

Break-glass activation spans are IMMUTABLE-tier governance evidence. The `breakglass.open` span
records activation timestamp, requester identity, approver identity, and reason. These attributes
must be handled with the same access controls as `BreakGlassEvent` evidence nodes. Before
forwarding break-glass spans to external collectors, operators should confirm the collector's
data residency and access controls meet their compliance requirements.

Gate denial spans (`gate.verdict == "DENY"`) record the name of the tool that was blocked. In
some deployments, the set of tools an agent attempted to invoke is sensitive information.
Operators should apply redaction to `gate.tool.name` if tool-name disclosure is a concern.

### Attribute Hashing

Tool argument values must not appear in span attributes. Tool arguments may contain secrets,
PII, or proprietary data. The correct correlation mechanism is a hash reference:
`extra_attributes={"gate.evidence.hash": sha256_of_gate_result}`. The underlying content
stays in the evidence store; the span carries only the pointer.

This rule is explicit in `trace_gate_evaluation()` and `trace_certificate_mint()`: neither
convenience function accepts raw tool argument parameters. Only structured metadata (IDs,
verdicts, counts, durations) is recorded in the span attributes.

### Immutability of Break-Glass Spans

Once a `breakglass.open` span is closed, its attributes reflect an IMMUTABLE-tier governance
event. Applications must not mutate span attributes after `end_span()` is called. The
`GovernanceSpan.is_ended` property allows callers to check whether a span has been closed.
Calling `end_span()` on an already-ended span is a no-op (idempotent), but attribute values
recorded at close time are final.

### Trace ID Predictability

`GovernanceTracer` generates `trace_id` via `uuid.uuid4().hex`. This provides 122 bits of
randomness, which is sufficient to prevent trace ID collision in any single deployment. Callers
who need to join governance spans to an existing distributed trace should pass their parent
system's trace ID at construction time rather than allowing the tracer to generate one.

## Consequences

**Positive**

- Enterprise observability stacks (Datadog, Grafana Tempo, Jaeger) can ingest governance spans
  without custom adapters. The `to_otel_compatible()` export is a direct feed to any OTel
  collector endpoint.
- Governance unit tests can assert on span attributes rather than evidence node hashes, which
  is dramatically simpler. `tracer.export()` returns plain Python dicts — no special assertion
  helpers required.
- The zero-dependency constraint is enforced at the architecture level: no import of
  `opentelemetry.*` appears in the tracer module. A future dependency audit cannot break this
  guarantee without modifying the tracer source.
- `SpanName` constants make span-name typos a static analysis problem rather than a runtime
  mystery. A misspelled `SpanName.GATE_EVALUAION` fails at import time.
- The convenience functions `trace_gate_evaluation()` and `trace_certificate_mint()` enforce
  required attribute presence at the call site. A gate that forgets to record `gate.verdict`
  is impossible when the only supported path is through the convenience function.
- Pull-based export gives callers full control over batching, retries, and backpressure handling.
  The tracer never blocks on I/O.

**Negative**

- The tracer holds all spans in memory for its lifetime. For extremely long-lived sessions with
  thousands of governance events, this adds heap pressure. Callers are responsible for
  periodically exporting and clearing the tracer.
- There is no built-in span deduplication. If `trace_gate_evaluation()` is called twice for
  the same gate (e.g., due to retry logic), two spans are recorded. Deduplication is the caller's
  responsibility at export time.
- Pull-based export means spans can be lost if a session terminates abnormally before the caller
  calls `export()`. Sessions that require durable span delivery should export spans to JSONL on
  every span creation rather than batching at session end.
- `to_otel_compatible()` produces dicts, not OTel SDK `ReadableSpan` objects. Callers who want
  to use OTel SDK features (custom attribute processors, SDK-level sampling) must bridge the
  dict representation to SDK types themselves.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Require `opentelemetry-sdk` as a hard dependency | Conflicts with users' existing OTel configurations; adds ~5 MB install footprint; introduces transitive SDK version constraints that break pinned environments. Zero-dep constraint is non-negotiable for a library. |
| Emit spans directly to `logging.Logger` with structured JSON | Logging is already used for operational output. Mixing governance spans into the log stream makes it harder to route governance data to compliance-specific sinks. Spans and logs have different lifecycle semantics: logs are fire-and-forget; spans have start/end time and parent relationships. |
| Subclass `opentelemetry.sdk.trace.Span` | Requires importing the OTel SDK, which violates the zero-dep constraint. Also locks the internal data model to OTel SDK internals that change across minor versions. |
| Store spans in the `EvidenceChain` directly | Evidence nodes are designed for authoritative audit records, not telemetry projections. Embedding spans in the chain conflates two concerns: tamper-evident correctness (evidence) and real-time observability (spans). Evidence nodes are immutable after construction; spans need to accumulate attributes while open. |
| Custom binary protocol (MessagePack, protobuf) | Adds complexity without benefit for in-process collection. Plain Python dicts and ISO-8601 timestamps are debuggable with zero tooling. Binary formats are only beneficial when serialization throughput is the bottleneck, which it is not for low-volume governance events. |
| Push-based export via background thread | Adds a threading dependency and makes the tracer's lifecycle non-trivial (start/stop, exception handling in background thread). Pull-based export keeps the tracer stateless and synchronous, which is simpler and sufficient for governance event volumes. |

## References

- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — Immutable Evidence Nodes; `EvidenceChain`
  and `EvidenceNode` that governance spans correlate to via hash attributes.
- [ADR-0042](ADR-0042-task-certificate.md) — Task Certificate; `trace_certificate_mint()`
  records the certificate minting event in the trace.
- [ADR-0044](ADR-0044-tool-gates.md) — Tool Gates; `trace_gate_evaluation()` records every
  gate evaluation, including the verdict and enforcement tier.
- [ADR-0045](ADR-0045-break-glass-protocol.md) — Break-Glass Protocol; `BREAK_GLASS_OPEN`,
  `BREAK_GLASS_EXPIRE`, `BREAK_GLASS_CLOSE`, and `BREAK_GLASS_NOTIFY` spans record the
  emergency lifecycle.
- [ADR-0048](ADR-0048-agent-segregation-of-duties.md) — Agent Segregation of Duties;
  `SOD_CHECK` spans record SoD check verdicts.
- [ADR-0049](ADR-0049-log-tier-governance.md) — Log Tier Governance; the `IMMUTABLE`,
  `PROTECTED`, and `MUTABLE` tier classification used in span attribute values and sampling policy.
- [ADR-0052](ADR-0052-policy-resolution.md) — Policy Resolution; `POLICY_RESOLVE` spans record
  the resolution strategy and winning policy.
- `docs/governance/standards/trace-naming.md` — Canonical span name vocabulary, required
  attribute sets, retention tier definitions, and value conventions. This ADR is the architectural
  specification; the naming standard is the operational reference.
- OpenTelemetry Protocol (OTLP) specification — the JSON representation that
  `GovernanceSpan.to_otel_dict()` targets; no SDK dependency required to read this spec.
- `lionagi/protocols/governance/tracing.py` — Implementation of `GovernanceTracer`,
  `GovernanceSpan`, `SpanName`, `trace_gate_evaluation()`, and `trace_certificate_mint()`.
