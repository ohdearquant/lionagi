# Charter DSL Style Standard

**Purpose**: Canonical syntax conventions for authoring Charter DSL v0 documents. Any charter
file that passes `li charter validate` must conform to these rules.

Cross-references: [adr-style.md](adr-style.md), [trace-naming.md](trace-naming.md),
[../charter-dsl-v0.md](../charter-dsl-v0.md)

---

## 1. Document Structure

A v0 charter has exactly these top-level keys, in this order:

```text
charter_dsl  kind  metadata  agents  registry  constraints  sod  permissions  [break_glass]  trace
```

`break_glass` is optional. All other blocks are required even when empty (e.g., `sod.rules: []`).

**Good**:

```yaml
charter_dsl: "0.1"
kind: agent_charter
metadata: { ... }
agents: [ ... ]
registry: { ... }
constraints: [ ... ]
sod: { active: true, rules: [] }
permissions: { ... }
trace: { ... }
```

**Bad** — missing block, wrong order:

```yaml
charter_dsl: "0.1"
kind: agent_charter
agents: [ ... ]
metadata: { ... }
# sod missing — invalid
```

---

## 2. Key Naming

- YAML keys use `lower_snake_case` only.
- Identifier values use dot-separated lower-snake segments: `agent.reader`, `role.reviewer`,
  `gate.registry.exact_tool`, `allow.reader.read_files`.
- Rule IDs carry action prefixes: `allow.*`, `deny.*`, `gate.*`, `sod.*`, `trace.*`.
- Enforcement values are lowercase in source: `hard`, `soft`, `advisory`. Span output maps to
  uppercase (`HARD`, `SOFT`, `ADVISORY`) — see [trace-naming.md](trace-naming.md).
- Registry categories are exactly: `tool`, `model`, `mcp_endpoint`, `url`, `path_prefix`.
- `path_prefix` values must begin and end with `/`.
- Tool names are exact canonical identifiers. Wildcards (`*`, `?`) are invalid in v0.

**Good**:

```yaml
- category: path_prefix
  value: /workspace/reports/
  scope: agent
  scope_id: agent.analyst
```

**Bad** — wildcard, wrong category name, wrong casing:

```yaml
- category: PATH_PREFIX
  value: /workspace/*
  scope: agent
  scope_id: agent.analyst
```

---

## 3. Indentation And Formatting

- Two spaces per indentation level. Tabs are invalid.
- Sequence items align under their key.
- Top-level blocks follow canonical order.
- One logical object per list item.
- Prefer block lists over inline maps when an entry has more than two fields.
- Line length target is 100 characters for prose fields; do not wrap IDs or hashes.
- Strings containing `:`, `#`, `{`, `}`, `[`, or `]` must be quoted.
- Use ISO 8601 UTC for fixed timestamps: `"2026-05-27T00:00:00Z"`.
- Durations use compact strings: `15m`, `30m`, `1h`.

**Good**:

```yaml
constraints:
  - constraint_id: gate.registry.exact_tool
    description: "Every tool call must match the ratified registry snapshot."
    gate_id: verify_in_registry
    manager_surface: ActionManager
    enforcement: hard
    attach:
      level: action
      action: tool_call
      tools: [tool.read_file]
    evidence:
      required: [GateResult, ToolCallEvidence]
```

**Bad** — tabs, inline map with many fields:

```yaml
constraints:
 - {constraint_id: gate.x, gate_id: y, enforcement: hard, attach: {level: action, action: tool_call}}
```

---

## 4. Comments

- Use YAML `#` comments above blocks or list items, not inside scalar values.
- Comments are not semantic. The compiler ignores them and excludes them from the ratification hash.
- A comment must explain review intent or a non-obvious constraint. Do not restate the field name.

**Good**:

```yaml
# Reviewer must never approve code they authored in the same task.
- rule_id: sod.implementer_reviewer.independent
  conflict_type: audit_independence
  roles: [implementer, reviewer]
  scope: task
```

**Bad** — restates the field:

```yaml
# rule_id for SoD rule
- rule_id: sod.implementer_reviewer.independent
```

---

## 5. Required Metadata Fields

`metadata` requires all of: `charter_id`, `version`, `status`, `policy_release`, `authored_by`,
`implemented_by`, `ratification`.

- `status`: `draft` | `proposed` | `accepted` | `superseded`.
- `authored_by` and `implemented_by` must identify different actors unless status is `draft`.
- `ratification.hash` is required for accepted charters; omit or set `null` for drafts.
- Hash format: `sha256:<64-hex-chars>`.

**Good**:

```yaml
metadata:
  charter_id: charter.reader.basic
  version: "1.0.0"
  status: accepted
  policy_release: policy.gov.v1
  authored_by: human:governance
  implemented_by: agent:implementer
  ratification:
    hash: sha256:1d6fabcd000000000000000000000000000000000000000000000000000000001
    signed_at: "2026-05-27T00:00:00Z"
```

**Bad** — same actor for both roles, missing ratification hash on accepted charter:

```yaml
metadata:
  charter_id: charter.reader.basic
  version: "1.0.0"
  status: accepted
  authored_by: agent:implementer
  implemented_by: agent:implementer
  ratification: null
```

---

## 6. Constraints Binding Rule

Each constraint must contain **exactly one** of `gate_id` or `hook_name`.

- Both present → invalid.
- Neither present → invalid.
- `hook_name` requires a companion `hook_phase` field.
- `attach.level: class` requires `tool_class`.
- `attach.level: action` requires `action`; `tools` is optional for scoping.

**Good**:

```yaml
- constraint_id: gate.sod.review_independence
  gate_id: assert_sod_independence
  manager_surface: ActionManager
  enforcement: hard
  attach:
    level: action
    action: certificate_mint
  evidence:
    required: [SoDCheckEvidence, GateResult]
```

**Bad** — both bindings present:

```yaml
- constraint_id: gate.x
  gate_id: verify_in_registry
  hook_name: pre_tool_call
  enforcement: hard
  attach:
    level: action
    action: tool_call
```

---

## 7. Permissions Default-Deny

`permissions.default` must be `deny`. Resolution must be:

```yaml
resolution:
  specificity_order: [resource, role, tenant, global]
  tie: deny
```

Allow rules that permit tool execution must include `requires_evidence`. Deny rules must include
a specific `because`; vague text such as "not allowed" is invalid.

---

## 8. Cross-References

- Span names in `trace.require_spans` must match the canonical registry in
  [trace-naming.md](trace-naming.md).
- Error codes emitted by the compiler and runtime follow [error-messages.md](error-messages.md).
- ADR governance for type ownership of `GateResult`, `AgentCharter`, etc. is described in
  [adr-style.md](adr-style.md).
- Full grammar, validation rules, and runtime integration are in
  [../charter-dsl-v0.md](../charter-dsl-v0.md).
