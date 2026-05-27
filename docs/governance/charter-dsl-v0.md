# Charter DSL v0 — Canonical Specification

**Version**: 0.1 | **Status**: Accepted | **Date**: 2026-05-27

This document is the authoritative Charter DSL v0 reference. It supersedes the P6 exploration
drafts (`charter-dsl-v0.md`, `charter_dsl_refined.yaml`) as canonical authoring guidance.
Implementers write charters in this syntax; the compiler produces typed runtime targets;
CLI and IDE tooling consume the same schema.

Related: [standards/dsl-style.md](standards/dsl-style.md),
[standards/trace-naming.md](standards/trace-naming.md),
[standards/adr-style.md](standards/adr-style.md),
ADR-0047 (Agent Charter), ADR-0051 (Tool Registry Allowlists), ADR-0052 (Policy Resolution)

---

## 1. Design Principles

1. **Exact identity, no wildcards.** Tool names, registry values, and grant targets must be
   exact strings. Wildcards are invalid in v0.
2. **Default deny, deterministic resolution.** `permissions.default` is always `deny`.
   Most-specific scope wins; ties deny. Declaration order does not change authorization.
3. **Compiler-time activation.** A charter is not active until all gate references and hook
   references are resolved, the normalized document is hashed, and the hash matches
   `metadata.ratification.hash`.
4. **Evidence and trace contracts are first-class.** Every constraint declares the evidence
   types it requires. The trace block declares the spans the runtime must emit.
5. **No embedded logic.** No boolean formula language, no placeholder substitution, no
   executable tokens. Conditions are represented by typed gate or hook bindings only.

---

## 2. Canonical Top-Level Shape

```yaml
charter_dsl: "0.1"
kind: agent_charter | session_charter

metadata:
  charter_id: ...
  version: ...
  status: ...
  policy_release: ...
  authored_by: ...
  implemented_by: ...
  ratification:
    hash: sha256:<hex>     # required when status: accepted
    signed_at: ...

agents: []                 # >=1 for agent_charter, >=2 for session_charter

registry:
  snapshot: ratification_time
  entries: []

constraints: []

sod:
  active: true
  rules: []

permissions:
  default: deny
  resolution:
    specificity_order: [resource, role, tenant, global]
    tie: deny
  allow: []
  deny: []

break_glass: null          # optional — omit when no elevated mode is needed

trace:
  stamp: []
  require_spans: []
  require_evidence: []
```

---

## 3. Grammar Reference (EBNF Sketch)

```ebnf
document          ::= version kind metadata agents registry constraints sod permissions break_glass? trace

version           ::= "charter_dsl" ":" version_string
kind              ::= "kind" ":" ("agent_charter" | "session_charter")

metadata          ::= "metadata" ":" metadata_body
metadata_body     ::= charter_id version_field status policy_release authored_by implemented_by ratification
status            ::= "draft" | "proposed" | "accepted" | "superseded"

agents            ::= "agents" ":" agent+
agent             ::= agent_id actor_id_source role allowed_models allowed_tools allowed_operations?
actor_id_source   ::= "branch_id" | "session_actor_id"

registry          ::= "registry" ":" snapshot entries
snapshot          ::= "snapshot" ":" "ratification_time"
entries           ::= "entries" ":" registry_entry*
registry_entry    ::= category value scope scope_id reason evidence_refs
category          ::= "tool" | "model" | "mcp_endpoint" | "url" | "path_prefix"

constraints       ::= "constraints" ":" constraint+
constraint        ::= constraint_id description binding manager_surface enforcement attach evidence
binding           ::= gate_binding | hook_binding
gate_binding      ::= "gate_id" ":" identifier
hook_binding      ::= "hook_name" ":" identifier "hook_phase" ":" identifier
manager_surface   ::= "ActionManager" | "MessageManager" | "iModelManager" | "DataLogger"
enforcement       ::= "hard" | "soft" | "advisory"
attach            ::= class_attach | action_attach
class_attach      ::= "attach" ":" "level" ":" "class" "tool_class" ":" identifier
action_attach     ::= "attach" ":" "level" ":" "action" "action" ":" identifier tools?

sod               ::= "sod" ":" active sod_rules
sod_rules         ::= "rules" ":" sod_rule*
sod_rule          ::= rule_id conflict_type roles scope because
conflict_type     ::= "transaction_dual_control" | "record_custody" | "audit_independence"
                    | "approval_chain" | "access_control"
sod_scope         ::= "session" | "task" | "global"

permissions       ::= "permissions" ":" default resolution allow deny
default           ::= "default" ":" "deny"
resolution        ::= specificity_order tie
specificity_order ::= "[" "resource" "," "role" "," "tenant" "," "global" "]"
tie               ::= "tie" ":" "deny"
allow             ::= "allow" ":" permission_rule*
deny              ::= "deny" ":" permission_rule*
permission_rule   ::= rule_id scope roles action (tools | resources) requires_evidence? because

break_glass       ::= "break_glass" ":" break_glass_body
break_glass_body  ::= enabled expires_after attestation temporary_grants notifications evidence

trace             ::= "trace" ":" stamp require_spans require_evidence
```

Expression language is intentionally minimal in v0:

- No arbitrary boolean expressions.
- Conditions use typed gate or hook bindings.
- Matching is exact role, tool, resource, scope, and registry entry comparison.
- Path matching is prefix-only via `category: path_prefix` registry entries.
- Policy resolution is structural: most-specific scope wins, ties deny.

---

## 4. Validation Rules

### Required Fields

Document top-level: `charter_dsl`, `kind`, `metadata`, `agents`, `registry`, `constraints`,
`sod`, `permissions`, `trace`.

Metadata: `charter_id`, `version`, `status`, `policy_release`, `authored_by`,
`implemented_by`, `ratification`.

Agent: `agent_id`, `actor_id_source`, `role`, `allowed_models`, `allowed_tools`.

Registry entry: `category`, `value`, `scope`, `scope_id`, `reason`, `evidence_refs`.

Constraint: `constraint_id`, `description`, exactly one of `gate_id`/`hook_name`,
`manager_surface`, `enforcement`, `attach`, `evidence.required`.

Permissions: `default`, `resolution.specificity_order`, `resolution.tie`, `allow`, `deny`.

Trace: `stamp`, `require_spans`, `require_evidence`.

### Type And Invariant Checks

- `charter_dsl` must be `"0.1"`.
- `metadata.version` is a semver string.
- `metadata.status` is one of `draft`, `proposed`, `accepted`, `superseded`.
- `metadata.ratification.hash` is required unless status is `draft` or `proposed`.
- `agents` length is exactly 1 for `agent_charter`, at least 2 for `session_charter`.
- `registry.entries[*].value` has no wildcards.
- `path_prefix` values start and end with `/`.
- `break_glass.expires_after` is at most `30m` in v0.
- Hash fields use `sha256:<64-hex-chars>` after real compilation.
- `sod.rules[*].roles` has exactly two distinct roles declared in `agents`.

### Semantic Checks

- Reject unknown top-level keys.
- Reject duplicate `charter_id`, `agent_id`, `constraint_id`, `rule_id` within one document.
- Reject tabs.
- Reject executable tokens in scalar strings: `__import__`, `eval(`, `exec(`, `lambda`,
  `subprocess`.
- Reject wildcards in tool names, registry values, and temporary grants.
- Reject constraints with both `gate_id` and `hook_name` present.
- Reject constraints with neither binding.
- Reject `hook_name` without `hook_phase`.
- Reject `attach.level: class` without `tool_class`.
- Reject `attach.level: action` without `action`.
- Reject accepted charters whose normalized hash does not match the ratification hash.
- Every `agents[*].allowed_tools` entry must appear in registry entries.
- `permissions.default` must be `deny`.
- `permissions.resolution.specificity_order` must be exactly `[resource, role, tenant, global]`.
- `permissions.resolution.tie` must be `deny`.
- If `sod.active` is false, accepted charters are invalid unless a policy release explicitly
  allows disabled SoD.
- Break-glass `attestation.approver_role` must not equal the requesting role.
- Every required span in `trace.require_spans` must appear in the canonical registry in
  [standards/trace-naming.md](standards/trace-naming.md).

---

## 5. Charter Examples

### Example A: Simple Read-Only Agent Charter

One reader agent with a single allowed tool, exact registry controls, and path-prefix bounding.

```yaml
charter_dsl: "0.1"
kind: agent_charter

metadata:
  charter_id: charter.simple_reader
  version: "1.0.0"
  status: accepted
  policy_release: policy.gov.v1
  authored_by: human:governance
  implemented_by: agent:implementer
  ratification:
    hash: sha256:1000abcd00000000000000000000000000000000000000000000000000000001
    signed_at: "2026-05-27T00:00:00Z"

agents:
  - agent_id: agent.simple_reader
    actor_id_source: branch_id
    role: reader
    allowed_models: [openai:gpt-5.4]
    allowed_tools: [tool.read_file]

registry:
  snapshot: ratification_time
  entries:
    - category: tool
      value: tool.read_file
      scope: agent
      scope_id: agent.simple_reader
      reason: "The reader can inspect approved workspace files."
      evidence_refs: [ev.registry.simple_reader.read_file]
    - category: path_prefix
      value: /workspace/
      scope: agent
      scope_id: agent.simple_reader
      reason: "File access is bounded to the workspace."
      evidence_refs: [ev.registry.simple_reader.workspace]

constraints:
  - constraint_id: gate.registry.read_file
    description: "Read-file calls must match the ratified registry snapshot."
    gate_id: verify_in_registry
    manager_surface: ActionManager
    enforcement: hard
    attach:
      level: action
      action: tool_call
      tools: [tool.read_file]
    evidence:
      required: [GateResult, ToolCallEvidence]
  - constraint_id: gate.path.workspace
    description: "Read-file paths must stay under the workspace prefix."
    gate_id: enforce_path_prefix
    manager_surface: ActionManager
    enforcement: hard
    attach:
      level: action
      action: tool_call
      tools: [tool.read_file]
    evidence:
      required: [GateResult]

sod:
  active: true
  rules: []

permissions:
  default: deny
  resolution:
    specificity_order: [resource, role, tenant, global]
    tie: deny
  allow:
    - rule_id: allow.reader.read_workspace
      scope: role
      roles: [reader]
      action: tool_call
      tools: [tool.read_file]
      resources: [/workspace/]
      requires_evidence: [GateResult, ToolCallEvidence]
      because: "Reader role requires file inspection only."
  deny:
    - rule_id: deny.reader.write_or_shell
      scope: role
      roles: [reader]
      action: tool_call
      tools: [tool.write_file, tool.exec_command]
      because: "Reader role cannot mutate files or run shell commands."

trace:
  stamp: [charter_id, policy_release, agent_id, role]
  require_spans:
    - governance.operation
    - registry.lookup
    - gate.evaluate
    - evidence.emit
  require_evidence:
    - GateResult
    - ToolCallEvidence
```

**Compile targets**: one `AgentCharter`, two registry entries, two action-level hard gates, one
empty active SoD policy, one deny-default permission policy.

---

### Example B: Multi-Agent Session Charter With SoD

Four roles: orchestrator (coordinates only), researcher, implementer, and reviewer (independent
from implementer). Two SoD rules enforce role separation.

```yaml
charter_dsl: "0.1"
kind: session_charter

metadata:
  charter_id: charter.gov_orchestration
  version: "1.0.0"
  status: accepted
  policy_release: policy.gov.v1
  authored_by: human:governance
  implemented_by: agent:implementer
  ratification:
    hash: sha256:2000abcd00000000000000000000000000000000000000000000000000000002
    signed_at: "2026-05-27T00:00:00Z"

agents:
  - agent_id: agent.orchestrator
    actor_id_source: branch_id
    role: orchestrator
    allowed_models: [openai:gpt-5.4]
    allowed_tools: []
    allowed_operations: [delegate, assign_role]
  - agent_id: agent.researcher
    actor_id_source: branch_id
    role: researcher
    allowed_models: [openai:gpt-5.4]
    allowed_tools: [tool.search, tool.read_file]
  - agent_id: agent.implementer
    actor_id_source: branch_id
    role: implementer
    allowed_models: [openai:gpt-5.4]
    allowed_tools: [tool.read_file, tool.write_file]
  - agent_id: agent.reviewer
    actor_id_source: branch_id
    role: reviewer
    allowed_models: [openai:gpt-5.4]
    allowed_tools: [tool.read_file]

registry:
  snapshot: ratification_time
  entries:
    - category: tool
      value: tool.search
      scope: role
      scope_id: researcher
      reason: "Researcher must locate source and documentation."
      evidence_refs: [ev.registry.orchestration.search]
    - category: tool
      value: tool.read_file
      scope: session
      scope_id: session.gov_orchestration
      reason: "Researcher, implementer, and reviewer inspect files."
      evidence_refs: [ev.registry.orchestration.read]
    - category: tool
      value: tool.write_file
      scope: role
      scope_id: implementer
      reason: "Only implementer applies code changes."
      evidence_refs: [ev.registry.orchestration.write]

constraints:
  - constraint_id: gate.registry.all_effects
    description: "Every external effect must resolve through registry policy."
    gate_id: verify_in_registry
    manager_surface: ActionManager
    enforcement: hard
    attach:
      level: class
      tool_class: external_effect
    evidence:
      required: [GateResult]
  - constraint_id: gate.sod.review_independence
    description: "Reviewer must be independent from implementer for the same task."
    gate_id: assert_sod_independence
    manager_surface: ActionManager
    enforcement: hard
    attach:
      level: action
      action: certificate_mint
    evidence:
      required: [SoDCheckEvidence, GateResult]
  - constraint_id: gate.orchestrator.no_task_tools
    description: "Orchestrator coordinates work but does not invoke task tools directly."
    gate_id: deny_direct_tool_call_for_role
    manager_surface: ActionManager
    enforcement: hard
    attach:
      level: action
      action: tool_call
    evidence:
      required: [GateResult]

sod:
  active: true
  rules:
    - rule_id: sod.implementer_reviewer.independent
      conflict_type: audit_independence
      roles: [implementer, reviewer]
      scope: task
      because: "The same actor cannot both implement and approve the same task."
    - rule_id: sod.grant_requester_approver.split
      conflict_type: approval_chain
      roles: [implementer, orchestrator]
      scope: session
      because: "Grant requester and approver must be distinct actors."

permissions:
  default: deny
  resolution:
    specificity_order: [resource, role, tenant, global]
    tie: deny
  allow:
    - rule_id: allow.orchestrator.delegate
      scope: role
      roles: [orchestrator]
      action: delegate
      resources: [session.gov_orchestration]
      requires_evidence: [DelegationEvidence]
      because: "Orchestrator manages work assignment only."
    - rule_id: allow.researcher.search_read
      scope: role
      roles: [researcher]
      action: tool_call
      tools: [tool.search, tool.read_file]
      requires_evidence: [GateResult, ToolCallEvidence]
      because: "Researcher may gather source evidence."
    - rule_id: allow.implementer.read_write
      scope: role
      roles: [implementer]
      action: tool_call
      tools: [tool.read_file, tool.write_file]
      requires_evidence: [GateResult, ToolCallEvidence]
      because: "Implementer may inspect and edit files."
    - rule_id: allow.reviewer.read
      scope: role
      roles: [reviewer]
      action: tool_call
      tools: [tool.read_file]
      requires_evidence: [GateResult, ToolCallEvidence]
      because: "Reviewer may inspect results without editing."
  deny:
    - rule_id: deny.orchestrator.task_tools
      scope: role
      roles: [orchestrator]
      action: tool_call
      tools: [tool.search, tool.read_file, tool.write_file]
      because: "Coordination authority is separate from task execution authority."
    - rule_id: deny.reviewer.write
      scope: role
      roles: [reviewer]
      action: tool_call
      tools: [tool.write_file]
      because: "Reviewer role must remain read-only."

trace:
  stamp: [charter_id, policy_release, agent_id, role, flow_id]
  require_spans:
    - governance.session
    - governance.flow
    - governance.operation
    - sod.check
    - registry.lookup
    - gate.evaluate
    - evidence.emit
    - certificate.state
    - certificate.mint
  require_evidence:
    - GateResult
    - ToolCallEvidence
    - SoDCheckEvidence
    - DelegationEvidence
    - TaskCertificate
```

**Compile targets**: one session-level runtime plan, one `AgentCharter` per agent, shared SoD
policy with bidirectional conflict pairs, certificate mint precondition requiring SoD evidence.

---

### Example C: Adapter Boundary Charter

User provides an existing LangGraph compiled graph. Lionagi wraps the invocation boundary.
Coarse mode governs graph invocation and emits boundary evidence — internal graph tool calls
are not claimed as individually governed.

```yaml
charter_dsl: "0.1"
kind: agent_charter

metadata:
  charter_id: charter.adapter.langgraph_boundary
  version: "1.0.0"
  status: accepted
  policy_release: policy.gov.v1
  authored_by: human:governance
  implemented_by: agent:adapter_owner
  ratification:
    hash: sha256:3000abcd00000000000000000000000000000000000000000000000000000003
    signed_at: "2026-05-27T00:00:00Z"

agents:
  - agent_id: agent.graph_runner
    actor_id_source: branch_id
    role: adapter_runner
    allowed_models: [openai:gpt-5.4]
    allowed_tools: [adapter.langgraph.invoke]

registry:
  snapshot: ratification_time
  entries:
    - category: tool
      value: adapter.langgraph.invoke
      scope: agent
      scope_id: agent.graph_runner
      reason: "The existing compiled graph is governed at the invocation boundary."
      evidence_refs: [ev.registry.adapter.langgraph.invoke]
    - category: path_prefix
      value: /workspace/
      scope: agent
      scope_id: agent.graph_runner
      reason: "Adapter inputs and artifacts are bounded to the workspace."
      evidence_refs: [ev.registry.adapter.langgraph.workspace]

constraints:
  - constraint_id: gate.adapter.boundary_registry
    description: "The adapter invocation must be registered before execution."
    gate_id: verify_in_registry
    manager_surface: ActionManager
    enforcement: hard
    attach:
      level: action
      action: tool_call
      tools: [adapter.langgraph.invoke]
    evidence:
      required: [GateResult, AdapterInvocationEvidence]
  - constraint_id: gate.adapter.input_contract
    description: "Adapter input must pass the graph boundary schema."
    gate_id: validate_adapter_input
    manager_surface: ActionManager
    enforcement: hard
    attach:
      level: action
      action: tool_call
      tools: [adapter.langgraph.invoke]
    evidence:
      required: [GateResult]
  - constraint_id: gate.adapter.no_internal_claim
    description: "Coarse boundary mode must not emit per-internal-tool governed evidence."
    gate_id: assert_boundary_only_evidence
    manager_surface: DataLogger
    enforcement: hard
    attach:
      level: action
      action: evidence_emit
    evidence:
      required: [AdapterInvocationEvidence]

sod:
  active: true
  rules: []

permissions:
  default: deny
  resolution:
    specificity_order: [resource, role, tenant, global]
    tie: deny
  allow:
    - rule_id: allow.adapter_runner.invoke_graph
      scope: role
      roles: [adapter_runner]
      action: tool_call
      tools: [adapter.langgraph.invoke]
      requires_evidence: [GateResult, AdapterInvocationEvidence]
      because: "Adapter runner may invoke the wrapped graph boundary."
  deny:
    - rule_id: deny.adapter_runner.raw_internal_tools
      scope: role
      roles: [adapter_runner]
      action: tool_call
      tools: [tool.internal_graph_tool]
      because: "Internal graph tools are not governed in coarse boundary mode."

trace:
  stamp: [charter_id, policy_release, agent_id, role, adapter_mode]
  require_spans:
    - governance.operation
    - registry.lookup
    - gate.evaluate
    - evidence.emit
  require_evidence:
    - GateResult
    - AdapterInvocationEvidence
```

**Adapter claim rule**: boundary mode emits `AdapterInvocationEvidence` for the invocation
boundary and does not emit per-tool governance evidence for internal graph nodes. Fine-grain
mode requires translating each internal external-effect tool into a lionagi `Tool` identity
before activation.

---

## 6. Integration Points

### Runtime Binding

The compiler emits typed runtime targets:

- `AgentCharter` objects per `agents[*]` entry.
- Registry entries for `ToolRegistryPolicy`.
- Gate registrations for each manager surface.
- SoD rules for `SoDPolicy`.
- Permission rules for `PolicyResolver`.
- Evidence requirements for evidence emission and certificate minting.
- Trace expectations for span validation.

`Branch` integration:

- `Branch` carries active `charter_id`, `policy_release`, actor identity, and operation context.
- `Branch.operate(middle=...)` installs governance through pre-gate, operation context creation,
  post-result evidence, and span projection.
- `Branch.act()` and `ActionManager` route governed tools through `execute_governed()`.

`Session` integration:

- `Session` pins a policy release and charter set for governed runs.
- `Session.flow()` validates role assignments, op limits, artifact contracts, and required
  control operations against the session charter before execution.
- Run-end certificate minting occurs after artifact contract verification.

### CLI Commands

```text
li charter validate <path/to/charter.yaml>
li charter compile  <path/to/charter.yaml> --out <path/to/compiled.json>
li governance audit  --session <sess_id>
li governance report --certificate <cert_id>
```

`validate` behavior:

- Performs parse, schema, binding existence, semantic, and safety checks.
- Exits non-zero on any hard error.
- Emits warnings for advisory quality issues; accepted charters cannot have unresolved warnings.

`compile` behavior:

- Refuses dynamic placeholders, wildcards, unresolved gates, unresolved hooks, and missing
  trace expectations.
- Emits normalized JSON plus ratification hash inputs.
- The normalized artifact (not raw YAML) is the hash input; comments and key ordering do not
  affect the hash after normalization.

### IDE Hints

IDE support comes from a generated JSON Schema artifact:

- Autocomplete top-level keys in canonical order.
- Enum completion for `kind`, `status`, `manager_surface`, `enforcement`, `conflict_type`,
  scope values, and canonical trace span names.
- Diagnostics for missing required blocks and tabs.
- Diagnostics for both/neither `gate_id` and `hook_name`.
- Warnings for unused registry entries and unreachable permission rules.
- Hover docs for span names and error codes.

---

## 7. Canonical Normalized Output Shape

```json
{
  "charter_id": "charter.simple_reader",
  "charter_dsl": "0.1",
  "kind": "agent_charter",
  "policy_release": "policy.gov.v1",
  "agents": [],
  "registry_entries": [],
  "gate_bindings": [],
  "sod_policy": {},
  "permission_policy": {},
  "evidence_requirements": [],
  "trace_requirements": [],
  "ratification_hash": "sha256:..."
}
```

The normalized artifact is the hash input. Comments, insignificant whitespace, and key
ordering differences inside maps do not affect the hash after normalization.

---

## 8. Open Issues For Phase 3-7

- ADR-0047 must be revised to define DSL-to-runtime binding, not only the Python `AgentCharter`
  shape (P12).
- ADR-0044 and ADR-0050 must consolidate `GateResult` to a single owner before implementation
  begins (P12).
- P9 trace taxonomy must be extended with permit lifecycle, certificate state transitions,
  break-glass lifecycle, and soft-gate justification spans before P20 (tracked in
  [standards/trace-naming.md](standards/trace-naming.md)).
- `OperationContext` must remain explicit state. A `contextvars` bridge is allowed for async
  propagation but must not become the authoritative governance store (P16).
- Boundary adapters must be documented and marketed honestly: they govern invocation boundaries,
  not hidden internal effects (P22).
