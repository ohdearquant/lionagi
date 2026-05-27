# Governance Trace Naming Standard

**Purpose**: Canonical span names, attribute keys, retention tiers, severity levels, and
sampling policy for governance OTel projections. Spans are projections over typed evidence
records — the authoritative audit record is always the typed evidence, not the span.

Cross-references: [dsl-style.md](dsl-style.md), [error-messages.md](error-messages.md),
[test-style.md](test-style.md)

---

## 1. Span Naming Pattern

```text
{domain}.{operation}[.{detail}]
```

- `domain`: lower-case governance subsystem.
- `operation`: lower-case verb or lifecycle noun.
- `detail`: optional qualifier for lifecycle events.
- Existing two-part names are canonical when unambiguous: `gate.evaluate`, `evidence.emit`,
  `policy.resolve`.

Allowed domains in v0:

```text
governance  gate  breakglass  evidence  certificate  charter  sod  registry  policy  permit
```

---

## 2. Common Required Attributes

Attach to every governance span where the value is known:

| Attribute | Type | Requirement |
|-----------|------|-------------|
| `governance.schema.version` | string | Required on every governance span |
| `governance.retention.tier` | enum | Required on every governance span |
| `governance.session.id` | string | Required on every governance span |
| `governance.operation.id` | string | Required except session root |
| `governance.flow.id` | string | Required for flow spans, optional otherwise |
| `governance.actor.id` | string | Required when actor is known |
| `governance.actor.role` | string | Required when role affects governance |
| `governance.policy.version` | string | Required for governed operations |
| `governance.charter.id` | string | Required when charter is active |
| `governance.evidence.hash` | string | Required when span projects a durable evidence record |

---

## 3. Retention Tiers

| Tier | Description | Sample Rate |
|------|-------------|-------------|
| `MUTABLE` | Debug-only operational spans; not valid for required governance evidence | May be sampled |
| `PROTECTED` | Session and flow lifecycle summaries | 100% for governed sessions |
| `IMMUTABLE` | Gate decisions, evidence, certificates, break-glass, permits, charter events, SoD checks, registry decisions, policy resolutions | 100% always |

Spans are telemetry projections. Authoritative records live in typed evidence and tier-aware logs.

---

## 4. Span Registry

| Span | Tier | Required Event-Specific Attributes |
|------|------|-------------------------------------|
| `governance.session` | PROTECTED | `governance.session.name`, `governance.session.team`, `governance.session.branch.count` |
| `governance.operation` | IMMUTABLE | `governance.operation.name`, `governance.operation.parent.id`, `governance.evidence.hash` |
| `governance.flow` | PROTECTED | `governance.flow.name`, `governance.flow.plan.hash`, `governance.flow.node.count` |
| `gate.evaluate` | IMMUTABLE | `gate.id`, `gate.tool.name`, `gate.verdict`, `gate.enforcement`, `gate.policy.version`, `gate.charter.id`, `gate.evidence.hash`, `gate.reason` |
| `gate.justify` | IMMUTABLE | `gate.id`, `gate.verdict`, `gate.enforcement`, `gate.justification`, `gate.justification.actor.id`, `gate.evidence.hash` |
| `gate.bypass` | IMMUTABLE | `gate.id`, `gate.tool.name`, `gate.bypass.reason`, `gate.bypass.authority`, `gate.bypass.expiry`, `gate.bypass.window.id`, `gate.evidence.hash` |
| `breakglass.open` | IMMUTABLE | `breakglass.window.id`, `breakglass.activated.at`, `breakglass.requested.by`, `breakglass.approved.by`, `breakglass.reason`, `breakglass.max.duration`, `breakglass.evidence.hash` |
| `breakglass.expire` | IMMUTABLE | `breakglass.window.id`, `breakglass.activated.at`, `breakglass.expired.at`, `breakglass.authority`, `breakglass.tools.used.count`, `breakglass.expiry.reason` |
| `breakglass.close` | IMMUTABLE | `breakglass.window.id`, `breakglass.closed.at`, `breakglass.close.reason`, `breakglass.tool.call.count`, `breakglass.certificate.id` |
| `breakglass.notify` | IMMUTABLE | `breakglass.window.id`, `breakglass.notification.target`, `breakglass.notification.kind`, `breakglass.notification.result` |
| `evidence.emit` | IMMUTABLE or PROTECTED | `evidence.id`, `evidence.chain.hash`, `evidence.previous.hash`, `evidence.tier`, `evidence.kind`, `evidence.payload.hash` |
| `evidence.verify` | IMMUTABLE or PROTECTED | `evidence.chain.hash`, `evidence.chain.length`, `evidence.verification.result`, `evidence.failure.reason` |
| `certificate.state` | IMMUTABLE | `certificate.id`, `certificate.task.id`, `certificate.previous.state`, `certificate.state`, `certificate.defensibility`, `certificate.evidence.chain.hash` |
| `certificate.mint` | IMMUTABLE | `certificate.id`, `certificate.task.id`, `certificate.gates.passed`, `certificate.gates.failed`, `certificate.grade`, `certificate.defensibility`, `certificate.evidence.chain.hash`, `certificate.break.glass` |
| `certificate.verify` | IMMUTABLE when trust-affecting | `certificate.id`, `certificate.verification.result`, `certificate.superseded.by`, `certificate.evidence.chain.hash` |
| `permit.issue` | IMMUTABLE | `permit.id`, `permit.scope`, `permit.tool.name`, `permit.issuer.id`, `permit.subject.id`, `permit.expires.at`, `permit.evidence.hash` |
| `permit.consume` | IMMUTABLE | `permit.id`, `permit.tool.name`, `permit.subject.id`, `permit.consumed.at`, `permit.consume.result`, `permit.evidence.hash` |
| `permit.revoke` | IMMUTABLE | `permit.id`, `permit.revoked.by`, `permit.revoked.at`, `permit.revoke.reason`, `permit.evidence.hash` |
| `charter.load` | IMMUTABLE when activating | `charter.id`, `charter.version`, `charter.source.uri`, `charter.source.hash`, `charter.rule.count`, `charter.load.result` |
| `charter.evaluate` | IMMUTABLE | `charter.id`, `charter.version`, `charter.constraint.id`, `charter.verdict`, `charter.severity`, `charter.evidence.hash` |
| `charter.violation` | IMMUTABLE | `charter.id`, `charter.version`, `charter.constraint.id`, `charter.violation.severity`, `charter.violation.reason`, `charter.evidence.hash` |
| `sod.check` | IMMUTABLE | `sod.role`, `sod.capability`, `sod.verdict`, `sod.policy.version`, `sod.evidence.hash` |
| `registry.lookup` | IMMUTABLE | `registry.tool.name`, `registry.role`, `registry.allowed`, `registry.policy.version`, `registry.lookup.source`, `registry.evidence.hash` |
| `policy.resolve` | IMMUTABLE | `policy.count`, `policy.strategy`, `policy.winner`, `policy.version`, `policy.conflict.count`, `policy.evidence.hash` |

---

## 5. Value Conventions

- Attribute keys: dot-separated lower-case.
- Retention tiers: uppercase `MUTABLE`, `PROTECTED`, `IMMUTABLE`.
- Gate verdict enums (`gate.verdict`): uppercase `ALLOW`, `DENY`, `ADVISORY` (ADR-0044 canonical values).
- Gate enforcement in spans: uppercase `HARD`, `SOFT`, `ADVISORY`.
- DSL enforcement values are lowercase (`hard`, `soft`, `advisory`) — map to uppercase in spans.
- Booleans: native boolean.
- Hashes: `sha256:<64-hex-chars>`.
- IDs: stable strings for the duration of the operation.
- Paths and prompts: excluded unless explicitly redacted and approved by evidence policy.
- `trace_id` and `span_id`: stored in evidence records as correlation fields.
- Evidence hashes: stored in span attributes as correlation pointers to authoritative records.

---

## 6. Severity

Use `governance.severity` when a generic severity field is needed:

| Value | When |
|-------|------|
| `INFO` | Expected lifecycle event or pass |
| `WARN` | Advisory failure, soft override, partial verification, or non-blocking policy issue |
| `ERROR` | Hard denial, parser failure, invalid charter, evidence/certificate verification failure |
| `CRITICAL` | Break-glass activation, evidence tamper signal, certificate replay, or policy conflict that blocks a regulated run |

For charter-specific spans, use `charter.severity` with values `INFO`, `WARN`, `BLOCK`.

---

## 7. Sampling Policy

- Sample 100% of `IMMUTABLE` spans.
- Sample 100% of `PROTECTED` session and flow lifecycle spans for governed sessions.
- `MUTABLE` debug spans may be sampled at lower rates, but sampling must never drop the
  authoritative governance record.
- When telemetry export is backpressured, write local evidence first and mark export failure
  with protected operational evidence.
- Never sample away: failures, break-glass events, permit events, certificate transitions, or
  policy conflicts.

---

## 8. Example A: Hard Gate Denial Span

```json
{
  "name": "gate.evaluate",
  "parent": "governance.operation",
  "attributes": {
    "governance.schema.version": "2026-05-27.v1",
    "governance.retention.tier": "IMMUTABLE",
    "governance.session.id": "sess_01",
    "governance.operation.id": "op_42",
    "governance.actor.id": "agent:reader",
    "governance.actor.role": "reader",
    "governance.policy.version": "policy.gov.v1",
    "governance.charter.id": "charter.reader.basic",
    "gate.id": "verify_in_registry",
    "gate.tool.name": "tool.write_file",
    "gate.verdict": "DENY",
    "gate.enforcement": "HARD",
    "gate.reason": "Tool is not in the ratified registry snapshot.",
    "gate.evidence.hash": "sha256:9ac1000000000000000000000000000000000000000000000000000000000001",
    "governance.severity": "ERROR"
  }
}
```

---

## 9. Example B: Break-Glass Open Span

```json
{
  "name": "breakglass.open",
  "parent": "governance.session",
  "attributes": {
    "governance.schema.version": "2026-05-27.v1",
    "governance.retention.tier": "IMMUTABLE",
    "governance.session.id": "sess_02",
    "governance.operation.id": "op_77",
    "governance.policy.version": "policy.gov.v1",
    "governance.charter.id": "charter.prod.support",
    "breakglass.window.id": "bg_20260527_001",
    "breakglass.activated.at": "2026-05-27T00:12:00Z",
    "breakglass.requested.by": "agent:prod_support",
    "breakglass.approved.by": "human:oncall_lead",
    "breakglass.reason": "incident_response",
    "breakglass.max.duration": "30m",
    "breakglass.evidence.hash": "sha256:1b0e000000000000000000000000000000000000000000000000000000000002",
    "governance.severity": "CRITICAL"
  }
}
```
