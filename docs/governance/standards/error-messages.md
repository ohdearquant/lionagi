# Governance Error Message Standard

**Purpose**: Error code taxonomy, dual human-readable and machine-routable format, context
inclusion rules, and severity mapping for all governance failures.

Cross-references: [trace-naming.md](trace-naming.md), [test-style.md](test-style.md),
[dsl-style.md](dsl-style.md)

---

## 1. Error Code Taxonomy

All governance errors use the `GOV-XXXX` prefix.

| Range | Owner |
|-------|-------|
| `GOV-0XXX` | General governance runtime errors |
| `GOV-1XXX` | Charter DSL parse, schema, validation, and activation |
| `GOV-2XXX` | Tool registry, governed tool declaration, gates, and raw bypass |
| `GOV-3XXX` | Evidence, log tiers, certificates, and trace projection |
| `GOV-4XXX` | Policy resolution, SoD, JIT permits, and break-glass |
| `GOV-5XXX` | Branch, Session, CLI orchestration, and provider adapters |
| `GOV-9XXX` | Internal consistency errors and invariant violations |

### Defined Codes

| Code | Meaning |
|------|---------|
| `GOV-1100` | Parse error |
| `GOV-1101` | Missing required block |
| `GOV-1102` | Invalid enum value |
| `GOV-1103` | Invalid constraint binding |
| `GOV-1104` | Executable content rejected |
| `GOV-2100` | Raw tool call blocked |
| `GOV-2101` | Registry lookup denied |
| `GOV-2200` | Gate evaluation failed closed |
| `GOV-2201` | Soft gate justification missing |
| `GOV-3100` | Evidence hash mismatch |
| `GOV-3200` | Immutable deletion blocked |
| `GOV-3300` | Certificate mint refused |
| `GOV-4100` | Policy no match |
| `GOV-4101` | Policy tie denied |
| `GOV-4200` | SoD conflict |
| `GOV-4300` | Permit missing, expired, or consumed |
| `GOV-4400` | Break-glass attestation invalid |
| `GOV-5100` | Flow charter validation failed |
| `GOV-5200` | Adapter boundary governance incomplete |

---

## 2. Structured Error Object

Every governance error has this shape:

```json
{
  "code": "GOV-2101",
  "severity": "ERROR",
  "message": "Human-readable message.",
  "agent_message": "machine_routable_short_token",
  "remediation": "Concrete next action.",
  "retryable": false,
  "redaction": "safe",
  "context": {
    "session_id": "sess_01",
    "operation_id": "op_42",
    "actor_id": "agent:reader",
    "actor_role": "reader",
    "charter_id": "charter.reader.basic",
    "policy_version": "policy.gov.v1",
    "trace_id": "trace_abc",
    "span_id": "span_def",
    "evidence_hash": "sha256:9ac1"
  },
  "details": {},
  "cause": null
}
```

---

## 3. Human-Readable Message Rules

- State what failed, which object was involved, and what to do next.
- Include exact IDs and hashes when safe to expose.
- Use present tense, active voice.
- Do not include: secrets, full prompts, credentials, raw model outputs, unredacted file contents.
- Do not use blame language. Messages diagnose state, not intent.

**Good**:

```text
GOV-2101 ERROR: Tool `tool.write_file` is not allowed for role `reader` under charter
`charter.reader.basic`. Use an allowed read-only tool or revise the charter registry and
policy release before retrying.
```

**Bad** — vague, no actionable ID, assigns blame:

```text
ERROR: You tried to use a tool you're not allowed to use. Please fix this.
```

---

## 4. Agent-Readable Message Rules

- `agent_message`: stable short token, no spaces: `registry_denied_tool`, `policy_tie_denied`,
  `evidence_hash_mismatch`.
- Include `retryable` and `severity`.
- Include enough context for safe routing; never expose hidden policy text or secrets.
- If the agent can fix the issue, `remediation` must be executable and bounded.
- If the agent must stop, set `retryable: false` and point to human approval or charter revision.

---

## 5. Context Inclusion Rules

**Include when available**:

```text
session_id  operation_id  flow_id  actor_id  actor_role
charter_id  policy_version  gate_id  tool_name
permit_id  certificate_id  trace_id  span_id  evidence_hash
```

**Exclude by default**:

```text
raw_prompts  api_keys  secrets  full_file_bodies
full_stack_traces (in user-facing payload)  personal_data
```

Stack traces may be stored in protected diagnostics. User-facing error payloads carry only
sanitized `cause.type` and `cause.message`.

---

## 6. Severity Mapping

| Severity | When |
|----------|------|
| `INFO` | Successful validation with warnings already recorded elsewhere |
| `WARN` | Advisory failure, soft gate warning, non-blocking export failure |
| `ERROR` | Blocked operation, parse failure, validation failure, denied policy, failed hard gate |
| `CRITICAL` | Evidence tamper, certificate replay, break-glass activation failure, raw bypass under governed mode |

---

## 7. Example A: Registry Denial

Human message:

```text
GOV-2101 ERROR: Tool `tool.write_file` is not allowed for role `reader` under charter
`charter.reader.basic`. Use an allowed read-only tool or revise the charter registry and
policy release before retrying.
```

Agent payload:

```json
{
  "code": "GOV-2101",
  "severity": "ERROR",
  "message": "Tool `tool.write_file` is not allowed for role `reader` under charter `charter.reader.basic`.",
  "agent_message": "registry_denied_tool",
  "remediation": "Select an allowed tool or request a charter registry revision.",
  "retryable": false,
  "redaction": "safe",
  "context": {
    "session_id": "sess_01",
    "operation_id": "op_42",
    "actor_role": "reader",
    "charter_id": "charter.reader.basic",
    "policy_version": "policy.gov.v1",
    "gate_id": "verify_in_registry",
    "tool_name": "tool.write_file",
    "evidence_hash": "sha256:9ac1000000000000000000000000000000000000000000000000000000000001"
  },
  "details": {
    "registry_lookup_source": "ratification_time"
  },
  "cause": null
}
```

---

## 8. Example B: Soft Gate Missing Justification

Human message:

```text
GOV-2201 ERROR: Soft gate `notify_incident_channel` cannot be overridden without
`justification` and `justification_actor_id`. Add both fields or retry after the
notification succeeds.
```

Agent payload:

```json
{
  "code": "GOV-2201",
  "severity": "ERROR",
  "message": "Soft gate `notify_incident_channel` cannot be overridden without justification fields.",
  "agent_message": "soft_gate_justification_missing",
  "remediation": "Provide justification and justification_actor_id, or rerun after notification succeeds.",
  "retryable": true,
  "redaction": "safe",
  "context": {
    "session_id": "sess_02",
    "operation_id": "op_77",
    "gate_id": "notify_incident_channel",
    "charter_id": "charter.prod.support",
    "policy_version": "policy.gov.v1"
  },
  "details": {
    "missing_fields": ["justification", "justification_actor_id"]
  },
  "cause": null
}
```
