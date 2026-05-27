# Governance Direction: lionagi Governed Orchestration

**Version**: 1.0 | **Phase**: 2 — Synthesis and Standards
**Status**: Accepted — blocking all Phase 3-7 implementation

> **Note on phase and play numbering**: This document uses P-numbers (P11, P12–P24) to refer to
> sequential implementation phases in the governed orchestration build-out. P11 is the synthesis
> phase that produced this document; P12–P24 are the 13 implementation phases described in
> Section 7. Each P-number corresponds to a bounded scope of work (ADR revision, substrate
> implementation, adapters, or docs) that feeds into the next.

This document is the master blueprint for lionagi's governed orchestration build-out (implementation
phases P12–P24). It records the strategic direction, architecture decisions, ADR verdicts, the full
phase list, integration plan, and risk register. Developers beginning implementation must read this
document in full before writing any code.

---

## 1. Strategic Direction

### 1.1 Thesis

Governed orchestration is the product. lionagi's differentiator is not model integration or
orchestration convenience — it is the ability to certify that an automated run followed a declared
charter: enforceable permissions, tamper-evident evidence, task certificates, and segregation-of-duties
constraints. Competitors (LangGraph, LlamaIndex, CrewAI, AutoGen) provide orchestration. None provide
orchestration-time governance with verifiable compliance artifacts.

The build strategy is build-over-adopt for governance primitives (hash chains, policy resolution,
log tiers, certificates) and wrap-over-rewrite for external frameworks (LangGraph, LlamaIndex,
CrewAI). lionagi governs at the boundary; it does not replace the user's existing tooling.

### 1.2 What We Are Building

The smallest coherent governed orchestration surface, end-to-end. Every component listed below
must integrate before any adapter is meaningful.

**Charter DSL v0** — YAML-shaped governance contract language. Strict schema, no wildcards, no
prose-only rules, deterministic policy resolution, exact registry entries, deny-by-default
permissions. Compiled to runtime targets; ratified with a reproducible SHA-256 hash. IDE hints
generated from the same Pydantic schema used by the CLI validator.
See: [`docs/governance/standards/dsl-style.md`](standards/dsl-style.md)

**Runtime Substrate** — the engine that Charter contracts compile into and execute against:

- Immutable evidence nodes (SHA-256 hash chains, append-only audit storage)
- `OperationContext` — per-operation actor identity, policy pin, trace IDs, propagated explicitly
- Log tier governance — MUTABLE / PROTECTED / IMMUTABLE tiers, application-layer backends
- Gate framework — hard, soft, advisory gate results; fail-closed exceptions; single canonical `GateResult`
- Policy release pinning — session-bound snapshot, most-specific-wins resolver, deny-on-tie
- Task certificates — minted at run completion, consuming evidence chain head, gate outcomes,
  break-glass state, and policy version

**Orchestration Integration** — governance wired into existing lionagi execution paths:

- `Session.flow()` enforces role allowlists, op limits, artifact contracts, and charter checks
- Per-operation evidence sidecars (`Branch.operate(middle=...)`)
- CLI `FlowPlan` validation: charter activation, op budgets, artifact contracts
- Run-end certificate minting after artifact verification

**Governance Layer Objects** — the safety control surface:

- Break-glass protocol: emergency elevated mode with immutable justification evidence and lifecycle spans
- JIT tool grants: no-standing-capability, single-use permit semantics resolved through the registry
- Tool registry allowlists: exact-match, compiled DSL targets, policy-resolved privileged tier
- Segregation of duties: assignment-time conflict matrix, bidirectional role independence

**OTel Governance Tracing** — projection over typed governance records:

- Typed records are created first; trace/span IDs are embedded in evidence
- OTel spans carry evidence hashes for enterprise correlation
- Span names follow the governance span taxonomy (see [`docs/governance/standards/trace-naming.md`](standards/trace-naming.md))
- Enterprise readiness path: retention tiers, redaction, SIEM export, cost tracking, audit/ops separation

**Provider Adapters — Two Waves**:

- G1 SDK-native: PydanticAI, OpenAI Agents SDK, Anthropic Agent SDK (~200 LOC each, native event hooks)
- G2 Framework: LangGraph, LlamaIndex, CrewAI (zero-rewrite, coarse boundary governance)

### 1.3 What We Are Not Building

The following scope is explicitly cut from the current implementation phases. These items may reopen in a future revision.

| Cut Item | Rationale |
|----------|-----------|
| G3 edge adapters (smolagents, OpenCode, HuggingFace) | Provider research marks these as smaller targets; core substrate is the constraint |
| External policy engine in critical path (OPA/Rego, Cedar) | Build stdlib+Pydantic path first; adopt only after v0 ships |
| Fine-grained LangGraph `ToolNode` internal governance | Depends on internal API stability; coarse graph-boundary governance ships first |
| LlamaIndex multi-agent handoff translation | Deferred until single-agent tool translation is stable |
| CrewAI Flow (hierarchical delegation) translation | Deferred until Crew preflight and coarse wrappers are validated |
| REST evidence API | Authenticated, redacted, tenant-aware evidence reads are unresolved in v0 |
| Database-backed tamper-proof storage | v0 is library-mode tamper-evidence; Merkle packaging and Ed25519 signing are future |
| Permissive DSL features | Prose-only constraints, wildcards, probabilistic auth, boolean expression language, inheritance, auto-SoD, model-id actors — all killed for v0 |
| Dashboards and metrics UI | Not a v0 concern; SIEM export is the enterprise readiness path |

### 1.4 Market Positioning

lionagi enters a crowded orchestration market and must carve a distinct position. The governance
layer is that position: users who need auditable, certifiable automated runs (regulated industries,
enterprise compliance teams, high-stakes automation) cannot get this from existing frameworks.
The charter model (declare constraints, compile them, enforce at runtime, certify outcomes) is the
product. The adapters are the distribution mechanism.

---

## 2. Architecture Overview

Governance is an additive runtime layer — it does not replace existing lionagi execution infrastructure.

```text
┌──────────────────────────────────────────────────────────────────┐
│  Charter DSL v0                                                  │
│  YAML-shaped source → CLI validate → compile → ratification hash │
└────────────────────────────┬─────────────────────────────────────┘
                             │ compilation targets
        ┌────────────────────┼──────────────────────────────┐
        ▼                    ▼                              ▼
┌──────────────┐  ┌──────────────────────┐  ┌─────────────────────┐
│ Gate         │  │ Registry + Policy     │  │ SoD + Charter       │
│ Registry     │  │ Release Pin          │  │ Runtime Objects      │
│ (ADR-0051)   │  │ (ADR-0052)           │  │ (ADR-0047/0048)     │
└──────┬───────┘  └──────────┬───────────┘  └─────────┬───────────┘
       │                     │                         │
       └─────────────────────┼─────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  ActionManager.execute_governed()                                │
│  Phase: pre-gate → execution → post-gate → evidence sidecar      │
│  (ADR-0043/0044 resolved: ActionManager owns pipeline)           │
└────────────────────────────┬─────────────────────────────────────┘
                             │ emits
       ┌─────────────────────┼────────────────────────────┐
       ▼                     ▼                            ▼
┌────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
│ Evidence   │  │ OperationContext      │  │ Log Tiers            │
│ Chain      │  │ (actor, policy pin,  │  │ MUTABLE /            │
│ (ADR-0041) │  │  trace IDs)          │  │ PROTECTED /          │
│            │  │ (ADR-0050)           │  │ IMMUTABLE            │
│            │  │                      │  │ (ADR-0049)           │
└─────┬──────┘  └──────────┬───────────┘  └──────────┬───────────┘
      │                    │                          │
      └────────────────────┼──────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│  OTel Projection                                                 │
│  Typed records → span with evidence hash → SIEM / collector      │
│  Spans are projection; evidence chain is authoritative storage   │
└──────────────────────────────────────────────────────────────────┘
                           │ consumed by
┌──────────────────────────┼───────────────────────────────────────┐
│  Session.flow() / FlowPlan                                       │
│  Charter activation → op limits → artifact contracts             │
│  → run-end certificate minting (ADR-0042)                        │
└──────────────────────────┬───────────────────────────────────────┘
                           │ wraps
┌──────────────────────────┼───────────────────────────────────────┐
│  Provider Adapters                                               │
│  G1: PydanticAI | OpenAI Agents SDK | Anthropic Agent SDK        │
│  G2: LangGraph | LlamaIndex | CrewAI                             │
│  Zero-rewrite: accept user's existing objects at the boundary    │
└──────────────────────────────────────────────────────────────────┘
```

**Layering rules**:

1. Evidence chain is the source of truth — OTel spans are projections, not the record.
2. `ActionManager` owns the single governed execution pipeline — gates plug into phases, not around it.
3. `OperationContext` is created and propagated explicitly — `contextvars` is a bridge only.
4. Charter compilation must resolve all targets before activation; unresolved targets fail closed.
5. Adapters claim only what they govern — coarse boundary wrappers emit boundary evidence only.

---

## 3. Provider Adapter Strategy

### 3.1 Zero-Rewrite Principle

Users bring their existing objects. lionagi wraps the user's existing object and governs its
execution boundary. The adapter does not rewrite the user's framework code. A governed adapter
has the shape:

```python
GovernedAdapter(user_object=their_existing_thing, charter=loaded_charter)
```

The adapter intercepts invocation, enforces the charter at the boundary, emits evidence, and returns
results. Internal framework calls (LangGraph node transitions, LlamaIndex retrieval steps, CrewAI
task delegation) are observed evidence, not governed lionagi tool calls, unless the user explicitly
translates framework tools into lionagi `Tool` objects.

### 3.2 G1 — SDK-Native Adapters

These adapters receive first implementation because they expose typed event streams and native hooks.

| Target | Hook Mechanism | Governed Events | Coarse/Fine |
|--------|---------------|-----------------|-------------|
| PydanticAI | `instrument_*`, run-level hooks | Tool calls, model requests, output validation | Both supported |
| OpenAI Agents SDK | Tracing hooks, `AgentHook` events | Tool calls, handoffs, model outputs, run lifecycle | Both supported |
| Anthropic Agent SDK | Native event stream, tool blocks | Tool use blocks, model turns, result extraction | Both supported |

Each G1 adapter is approximately 200 LOC. Fine-grained claims require the framework tool to be
translated into a registered lionagi `Tool` object. Coarse-only claims govern invocation boundaries.

### 3.3 G2 — Framework Adapters

These adapters accept existing user objects and apply coarse governance at the boundary.

| Target | Accepted Objects | Coarse Boundary | Fine Governance |
|--------|-----------------|-----------------|-----------------|
| LangGraph | `CompiledStateGraph` | Graph invoke entry/exit | Requires tool translation; fine mode explicitly labeled |
| LlamaIndex | `AgentWorkflow`, `FunctionAgent`, `ReActAgent`, `QueryEngineTool`, `FunctionTool` | Agent run entry/exit | Tool translation supported for `FunctionTool` |
| CrewAI | `Crew`, `CrewPlan` (preflight) | Crew kickoff entry/exit | Hierarchical delegation not governed in v0 |

Internal framework state transitions (LangGraph node routing, LlamaIndex retrieval pipelines,
CrewAI task delegation chains) are observed boundary evidence, not governed lionagi actions, in
coarse mode. Any adapter claiming fine governance for internal steps must register those steps as
translated lionagi `Tool` objects with registry entries.

### 3.4 G3 — Edge Adapters (Not In Scope for Current Implementation)

smolagents, OpenCode, and HuggingFace inference suites remain optional. They reopen only if
earlier implementation phases complete ahead of schedule with clean adversarial test results.

### 3.5 Adapter Claim Standards

Every adapter ships with an explicit claim matrix in its docstring and test fixture. The matrix
declares: which events are governed (gate + evidence), which events are observed-only (boundary
evidence, no gate enforcement), and which events are not captured. Overclaiming governance for
internal framework calls is a defect that fails the adversarial test suite.

---

## 4. DSL Strategy

### 4.1 Charter DSL v0 Overview

Charter DSL v0 is YAML-shaped, not YAML-arbitrary. It has a fixed top-level block set, strict
Pydantic schema, and deterministic compilation. It is not a general-purpose policy language.
Unsupported features are rejected at parse time with actionable error messages.

**Canonical top-level blocks** (required unless marked optional):

```text
charter_dsl      — version pin ("0.1")
kind             — agent_charter | session_charter
metadata         — identity, release, ratification
agents           — actor bindings (one per agent_charter, two or more per session_charter)
registry         — ratified tool/model/path snapshot with evidence
constraints      — executable gate or hook bindings with enforcement levels
sod              — segregation-of-duties conflict matrix (required, rules: [] if inactive)
permissions      — explicit allow/deny rules with specificity resolution
break_glass      — (optional) emergency elevated mode with attestation and time bounds
trace            — span and evidence requirements for runtime verification
```

Full syntax specification: [`docs/governance/standards/dsl-style.md`](standards/dsl-style.md)

Three concrete charter examples are in: [`docs/governance/charter-dsl-v0.md`](charter-dsl-v0.md)

### 4.2 Compilation Pipeline

Charter compilation follows six ordered phases. Each phase is a gate — failure fails closed.

```text
Phase 1: Parse
  YAML source → AST nodes
  Validates: top-level block presence, key spelling, value types
  Rejects: unknown keys, wrong types, missing required blocks

Phase 2: Validate
  Schema validation via Pydantic models
  Checks: constraint binding exclusivity (exactly one of gate_id/hook_name),
          SoD rule structure, permission specificity, trace span name set

Phase 3: Bind
  Resolves gate_id → registered GateExecutor
  Resolves hook_name + hook_phase → registered hook
  Resolves registry entry values → canonical identifiers
  Fails closed on any unresolved target

Phase 4: Normalize
  Produces canonical JSON representation
  Comments stripped; field order fixed; enum values lowercased
  Output is deterministic given the same input

Phase 5: Emit
  Produces typed runtime target objects:
    - GateRegistration objects (one per constraint)
    - RegistryEntry objects (one per registry entry)
    - SoDRule objects (bidirectional, one per sod.rules entry)
    - EvidenceRequirement objects (from constraints and trace)
    - TraceExpectation objects (from trace.require_spans)
    - PermissionPolicy object (from permissions block)
    - PolicyPin record (from metadata.policy_release)

Phase 6: Activate
  Computes SHA-256 hash of normalized JSON
  Compares against metadata.ratification.hash (must match for accepted charters)
  Records activation evidence (charter_id, hash, policy_pin, activated_at)
  Registers all emitted targets with the runtime
  Returns typed AgentCharter or SessionCharter runtime object
```

### 4.3 Runtime Binding

At runtime, the compiled charter binding is the source of authority for:

- Gate enforcement: `ActionManager.execute_governed()` consults registered gates by `constraint_id`
- Registry enforcement: tool calls check against ratified registry snapshots
- Policy resolution: ADR-0052 resolver uses the policy pin from the charter
- SoD checks: role conflict matrix is evaluated at assignment time
- Trace verification: required spans are validated post-run against the OTel export

### 4.4 IDE Hints and CLI Integration

The same Pydantic schema that validates charters at runtime is exported as JSON Schema for IDE
integration. Editor hints cannot drift from runtime validation because they share the same source.

CLI commands (planned — not yet implemented; implementation is tracked in the charter parser and
flow governance phases):

- `li charter validate <file>` — parse + validate, report errors with line numbers
- `li charter compile <file>` — full compilation through Phase 5, output runtime target summary
- `li charter activate <file>` — full compilation through Phase 6, register targets in session

---

## 5. Tracing Strategy

### 5.1 Architecture: Records First, Spans Second

The governance tracing architecture has a strict hierarchy:

1. The runtime creates **typed governance records** first (evidence nodes, gate results, certificates)
2. `trace_id` and `span_id` are **embedded into evidence** at record creation time
3. OTel spans are **projected** from the same records, carrying evidence hashes for correlation
4. Spans are **never** the authoritative storage for governance decisions

This means: an auditor verifying a run reads the evidence chain, not the OTel export. OTel
is the enterprise observability interface (SIEM, alerting, dashboards), not the compliance record.

### 5.2 Span Taxonomy Reference

The canonical span registry is defined in
[`docs/governance/standards/trace-naming.md`](standards/trace-naming.md) (section 4).
That file is the authoritative source for span names, required attributes, and retention tiers.
The summary below uses the same attribute names as trace-naming.md.

**Base spans**:

| Span Name | Trigger | Key Required Attributes |
|-----------|---------|-------------------------|
| `governance.operation` | Operation start | `governance.operation.name`, `governance.charter.id`, `governance.policy.version`, `governance.actor.id`, `governance.actor.role`, `governance.evidence.hash` |
| `gate.evaluate` | Gate execution | `gate.id`, `gate.tool.name`, `gate.verdict`, `gate.enforcement`, `gate.policy.version`, `gate.charter.id`, `gate.evidence.hash`, `gate.reason` |
| `evidence.emit` | Evidence node creation | `evidence.id`, `evidence.kind`, `evidence.chain.hash`, `evidence.previous.hash`, `evidence.tier`, `evidence.payload.hash` |
| `registry.lookup` | Registry entry check | `registry.tool.name`, `registry.role`, `registry.allowed`, `registry.policy.version`, `registry.lookup.source`, `registry.evidence.hash` |
| `policy.resolve` | Policy resolution | `policy.count`, `policy.strategy`, `policy.winner`, `policy.version`, `policy.conflict.count`, `policy.evidence.hash` |
| `certificate.mint` | Certificate creation | `certificate.id`, `certificate.task.id`, `certificate.gates.passed`, `certificate.gates.failed`, `certificate.grade`, `certificate.defensibility`, `certificate.evidence.chain.hash`, `certificate.break.glass` |
| `sod.check` | SoD matrix evaluation | `sod.role`, `sod.capability`, `sod.verdict`, `sod.policy.version`, `sod.evidence.hash` |

**Additional spans (permit, certificate, and break-glass lifecycle — implemented in the OTel tracing phase)**:

| Span Name | Trigger | Key Required Attributes |
|-----------|---------|-------------------------|
| `permit.issue` | JIT permit issued | `permit.id`, `permit.scope`, `permit.tool.name`, `permit.issuer.id`, `permit.subject.id`, `permit.expires.at`, `permit.evidence.hash` |
| `permit.consume` | JIT permit consumed on tool call | `permit.id`, `permit.tool.name`, `permit.subject.id`, `permit.consumed.at`, `permit.consume.result`, `permit.evidence.hash` |
| `permit.revoke` | JIT permit revoked before expiry | `permit.id`, `permit.revoked.by`, `permit.revoked.at`, `permit.revoke.reason`, `permit.evidence.hash` |
| `certificate.verify` | Certificate re-verification | `certificate.id`, `certificate.verification.result`, `certificate.superseded.by`, `certificate.evidence.chain.hash` |
| `breakglass.open` | Break-glass activation | `breakglass.window.id`, `breakglass.activated.at`, `breakglass.requested.by`, `breakglass.approved.by`, `breakglass.reason`, `breakglass.max.duration`, `breakglass.evidence.hash` |
| `breakglass.expire` | Break-glass window expired | `breakglass.window.id`, `breakglass.activated.at`, `breakglass.expired.at`, `breakglass.authority`, `breakglass.tools.used.count`, `breakglass.expiry.reason` |
| `breakglass.close` | Break-glass closed or revoked | `breakglass.window.id`, `breakglass.closed.at`, `breakglass.close.reason`, `breakglass.tool.call.count`, `breakglass.certificate.id` |
| `breakglass.notify` | Emergency notification sent | `breakglass.window.id`, `breakglass.notification.target`, `breakglass.notification.kind`, `breakglass.notification.result` |
| `gate.justify` | SOFT gate override with justification | `gate.id`, `gate.verdict`, `gate.enforcement`, `gate.justification`, `gate.justification.actor.id`, `gate.evidence.hash` |

See trace-naming.md section 4 for the complete attribute list for every span type.

### 5.3 Enterprise Readiness Path

The span schema is designed for enterprise readiness from the start. Retrofitting observability
attributes onto opaque spans is expensive — declaring them in the schema creates the migration path.

| Capability | Design Element | Implementation phase |
|------------|---------------|----------------------|
| Retention tiers | Log tier governance aligned with span retention fields | Evidence chain phase; OTel tracing phase |
| Sensitive data redaction | Evidence record field exclusion rules documented at emit time | Evidence chain phase |
| SIEM export | OTel collector-compatible span format; attribute naming follows OTel semconv | OTel tracing phase |
| Backpressure | Span export failure must not block evidence chain writes | OTel tracing phase |
| Alerting | `gate.verdict: DENY` and `breakglass.open` are alertable events | OTel tracing phase |
| Cost tracking | `governance.operation` spans carry operation budget attributes | Flow governance integration phase |
| Audit/ops separation | Audit spans go to immutable SIEM sink; ops spans go to mutable collector | OTel tracing phase |

---

## 6. ADR Slate Verdict

All 12 governance ADRs are reviewed. The verdict is five SURVIVE, seven REVISE, zero KILL.
No ADR is correct-as-is across the entire governance design; revisions resolve overlaps and
bind the ADRs to the Charter DSL compilation pipeline and shared runtime types.

| ADR | Title | Verdict | Rationale |
|-----|-------|---------|-----------|
| ADR-0041 | Immutable Evidence Nodes | **SURVIVE** | Hash-chain evidence is the correct foundation. Matches P7's stdlib SHA-256 build path and P1's missing chain-tip/canonical hash substrate. Implementation must follow it without modification. |
| ADR-0042 | Task Certificate | **REVISE** | Certificate stays, but must inherit ADR-0041 evidence types rather than redeclaring `Element`-based references. Minting must consume `OperationContext`, gate records, break-glass state, and policy pin. |
| ADR-0043 | Governed Tool Declaration | **REVISE** | Rename to "Governed Tool Wrapper" to match the actual implementation (a wrapper + metadata, not just a declaration). `ActionManager` is the single execution pipeline owner; gates plug into its phases. |
| ADR-0044 | Tool Gates | **REVISE** | Keep binary gates and fail-closed exceptions. Define one canonical `GateResult` type (currently duplicated with ADR-0050). Treat break-glass as a separate emergency lifecycle, not a HARD gate override. |
| ADR-0045 | Break-Glass Protocol | **REVISE** | Keep the emergency path. Add activation/revoke lifecycle spans (`breakglass.open`, `breakglass.close`). Clarify library-mode limits: no hardware HSM, no distributed consensus. Reconcile evidence/certificate path with `OperationContext`. |
| ADR-0046 | JIT Tool Grant | **REVISE** | Keep no-standing-capability and single-use permit semantics. The registry/policy-resolved privileged tier is authoritative for `requires_jit`; decorator metadata is a declaration hint, not the source of truth. |
| ADR-0047 | Agent Charter | **REVISE** | Replace Python-only charter objects with DSL-first compilation. Runtime `AgentCharter` and `SessionCharter` objects are compiler outputs, not hand-constructed. CLI validation and activation evidence are mandatory. |
| ADR-0048 | Agent SoD | **SURVIVE** | Assignment-time conflict matrices and role independence are correct and required. Must be implemented before CrewAI hierarchical/delegation paths can be claimed governed. |
| ADR-0049 | Log Tier Governance | **SURVIVE** | MUTABLE/PROTECTED/IMMUTABLE model is correct. Application-layer backend deferral is the right call for v0. Complements evidence chain rather than replacing it. |
| ADR-0050 | Operation Context | **REVISE** | Keep active assertion, explicit propagation, and evidence embedding. Remove the duplicated `GateResult` type (canonical home is ADR-0044). Settle pipeline phase ownership with ADR-0043. Allow `contextvars` as a propagation bridge only, never as the authoritative source of context state. |
| ADR-0051 | Tool Registry Allowlists | **REVISE** | Keep append-only exact registry semantics. Make registry entries compiled DSL targets, not a parallel policy plane. The registry is a runtime enforcement artifact of Charter compilation, not an independently authored store. |
| ADR-0052 | Policy Resolution | **SURVIVE** | Most-specific-wins, deny-on-tie, staged releases, and session pinning are the canonical resolver algorithm. Wire it into Charter DSL compilation (the `permissions` block compiles to policy rules) and registry scope. |

**Revision convergence groups**:

- ADR-0043 + ADR-0044 + ADR-0050 → one governed execution pipeline in `ActionManager`
- ADR-0047 + ADR-0051 + ADR-0052 → unified under Charter DSL compilation
- ADR-0045 + ADR-0046 → explicit lifecycle evidence and OTel spans for emergency/elevated paths

---

## 7. Implementation Phase List

13 phases (P12–P24). No phase may be added without explicit scope cut elsewhere. G3 adapters,
REST evidence API, dashboards, and fine-grained framework internals are the first cut targets if
budget is exceeded. P-numbers are sequential identifiers; each number corresponds to one bounded
scope of work described below.

### P12 — ADR Slate Consolidation (the governance consolidation phase)

| Field | Value |
|-------|-------|
| **Category** | A — ADR revision |
| **Playbook** | feature |
| **Effort** | medium |
| **Scope** | Revise ADR-0042, 0043, 0044, 0045, 0046, 0047, 0050, 0051 to remove duplicated types and bind DSL/runtime ownership. Produce cross-reference table: which ADR owns each type. |
| **Dependencies** | This document |
| **Measurement** | All revised ADRs cite evidence inventory; no duplicated `GateResult`; cross-reference table complete; ADR doc links to tests and implementation path |
| **Files Modified** | `docs/adrs/ADR-0042-*.md`, `ADR-0043-*.md`, `ADR-0044-*.md`, `ADR-0045-*.md`, `ADR-0046-*.md`, `ADR-0047-*.md`, `ADR-0050-*.md`, `ADR-0051-*.md` |

### P13 — Charter DSL Parser and Schema

| Field | Value |
|-------|-------|
| **Category** | B — DSL implementation |
| **Playbook** | feature |
| **Effort** | high |
| **Scope** | Implement canonical DSL Pydantic models, YAML parser, schema validation, legacy variant rejection with migration diagnostics, and JSON Schema export for IDE hints. Must handle all required blocks, optional `break_glass`, enforcement enum normalization, and constraint binding exclusivity check. |
| **Dependencies** | P12 |
| **Measurement** | Unit tests for parser; property tests for schema round-trips; fixtures cover three canonical charter examples; wildcards and legacy `apiVersion` keys are rejected with line-specific error messages; JSON Schema output passes VS Code schema validation |
| **Files Modified** | `lionagi/protocols/governance/charter.py`, `lionagi/protocols/governance/dsl.py`, `lionagi/cli/orchestrate/charter.py`, `tests/governance/test_charter_dsl.py` |

### P14 — Charter Compiler and Runtime Targets

| Field | Value |
|-------|-------|
| **Category** | B — DSL implementation |
| **Playbook** | feature |
| **Effort** | high |
| **Scope** | Compile DSL through all six phases. Emit `GateRegistration`, `RegistryEntry`, `SoDRule`, `EvidenceRequirement`, `TraceExpectation`, `PermissionPolicy`, and `PolicyPin` objects. Activation must verify SHA-256 hash against `metadata.ratification.hash`; unresolved targets fail closed. |
| **Dependencies** | P13 |
| **Measurement** | Golden compile snapshots stable across Python versions; activation fails closed on hash mismatch; activation fails closed on unresolved gate or hook target; coverage across all seven runtime target families; adversarial test: tampered charter hash is rejected |
| **Files Modified** | `lionagi/protocols/governance/compiler.py`, `lionagi/protocols/governance/runtime.py`, `lionagi/cli/orchestrate/charter.py`, `tests/governance/test_charter_compile.py` |

### P15 — Evidence Chain and Log Tiers

| Field | Value |
|-------|-------|
| **Category** | C — Substrate |
| **Playbook** | feature |
| **Effort** | high |
| **Scope** | Canonical JSON evidence nodes with SHA-256 chain hashing (stdlib `hashlib`, zero external dependency). Append-only audit pile behavior (existing `Pile` gets append-only mode). Tier-aware `DataLogger` emitting MUTABLE, PROTECTED, and IMMUTABLE records. Evidence chain `verify()` that detects tampering, reordering, and deletion. |
| **Dependencies** | P12 |
| **Measurement** | Tamper, reorder, delete tests pass; property tests for SHA-256 determinism; microbenchmark confirms synchronous hashing stays under 1ms for typical evidence payloads; sensitive field exclusion is documented at emit time |
| **Files Modified** | `lionagi/protocols/governance/evidence.py`, `lionagi/protocols/generic/log.py`, `lionagi/protocols/generic/pile.py`, `lionagi/session/branch.py`, `tests/governance/test_evidence_chain.py` |

### P16 — OperationContext and Policy Release Pinning

| Field | Value |
|-------|-------|
| **Category** | C — Substrate |
| **Playbook** | feature |
| **Effort** | high |
| **Scope** | `OperationContext` with actor identity, policy release pin, trace IDs, operation budget, and evidence embedding helpers. Explicit propagation via function arguments. `contextvars` bridge: set at async boundary for tools that cannot accept explicit context, but never used as the authoritative store. Policy release pin validated against the session charter at `Session.flow()` entry. |
| **Dependencies** | P15 |
| **Measurement** | Async propagation tests show context is correctly available in governed tool calls; missing context fails closed in governed mode; `contextvars` contains only a reference to the explicit context, not the state itself; policy pin mismatch fails the flow before execution |
| **Files Modified** | `lionagi/protocols/governance/context.py`, `lionagi/session/branch.py`, `lionagi/session/session.py`, `lionagi/operations/operate/operate.py`, `tests/governance/test_operation_context.py` |

### P17 — Governed ActionManager Gates

| Field | Value |
|-------|-------|
| **Category** | C — Substrate |
| **Playbook** | feature |
| **Effort** | high |
| **Scope** | `Tool.governance_meta` descriptor, `@governed_tool` decorator, `ActionManager.execute_governed()` pipeline (pre-gate → execution → post-gate → evidence sidecar), gate registry executor, and no-bypass controls on raw tool execution. Single canonical `GateResult` type (ADR-0044 owner). Hard gates raise `GovernanceViolationError` on deny; soft gates emit advisory evidence; advisory gates record without blocking. |
| **Dependencies** | P14, P16 |
| **Measurement** | Happy-path test (ALLOW result, evidence emitted); deny test (DENY raises error, evidence immutable); soft gate test (SOFT allows with justification evidence); advisory test (ADVISORY records without blocking); raw-tool bypass adversarial test fails closed; exactly one `GateResult` import path |
| **Files Modified** | `lionagi/protocols/action/tool.py`, `lionagi/protocols/action/manager.py`, `lionagi/protocols/action/function_calling.py`, `lionagi/protocols/governance/gates.py`, `tests/governance/test_tool_gates.py` |

### P18 — Flow Governance Integration

| Field | Value |
|-------|-------|
| **Category** | D — Orchestration integration |
| **Playbook** | feature |
| **Effort** | high |
| **Scope** | Define and implement `TaskCertificate` type (minting, grade computation, evidence chain head embedding). Wire charter activation into `Session.flow()` entry; enforce role allowlists and op limits from compiled charter; validate artifact contracts in `FlowPlan`; attach per-operation evidence sidecars via `Branch.operate(middle=...)`; mint run-end `TaskCertificate` after artifact verification; expose `li charter activate` and `li flow run --charter` CLI paths. |
| **Dependencies** | P17 |
| **Measurement** | Integration fixture proves: op limit exceeded → run blocked; role not in allowlist → op blocked; artifact missing → certificate not minted; break-glass event recorded in immutable evidence; run certificate contains evidence chain head hash and policy version |
| **Files Modified** | `lionagi/session/session.py`, `lionagi/operations/flow.py`, `lionagi/operations/types.py`, `lionagi/cli/orchestrate/flow.py`, `lionagi/cli/orchestrate/_orchestration.py`, `tests/governance/test_flow_governance.py` |

### P19 — Certificates, Break-Glass, JIT, Registry, SoD

| Field | Value |
|-------|-------|
| **Category** | E — Layer |
| **Playbook** | feature |
| **Effort** | high |
| **Scope** | Full governance layer object implementations: `TaskCertificate` (verification, replay detection, certificate store querying — minting type is defined in P18), `BreakGlassProtocol` (activation with attestation evidence, lifecycle spans, revocation, DEGRADED certificate path), `JITGrant` (single-use permit, registry/policy-resolved `requires_jit`, expiry), `ToolRegistry` (compiled DSL targets, exact-match, policy-resolved scope), `PolicyResolver` (ADR-0052 most-specific-wins), `SoDMatrix` (bidirectional conflict, assignment-time check). |
| **Dependencies** | P18 |
| **Measurement** | Certificate replay is blocked; break-glass audit evidence is immutable and non-exportable via normal paths; JIT grant is consumed exactly once; deny-on-tie test case; SoD matrix property test for bidirectional conflicts; adversarial test: elevated tool call outside break-glass window is rejected |
| **Files Modified** | `lionagi/protocols/governance/certificate.py`, `lionagi/protocols/governance/break_glass.py`, `lionagi/protocols/governance/jit.py`, `lionagi/protocols/governance/registry.py`, `lionagi/protocols/governance/policy.py`, `lionagi/protocols/governance/sod.py`, `lionagi/session/branch.py`, `tests/governance/test_layer_controls.py` |

### P20 — OTel Governance Tracing

| Field | Value |
|-------|-------|
| **Category** | F — Tracing |
| **Playbook** | feature |
| **Effort** | medium |
| **Scope** | Project typed governance records onto OTel spans using the full governance span registry defined in `standards/trace-naming.md` (base spans plus permit lifecycle, certificate lifecycle, break-glass open/close/notify, and SOFT gate justification spans). Embed evidence hashes and trace/span IDs bidirectionally. Span export failure must not block evidence chain writes (backpressure). Retention tier alignment with log tier governance. |
| **Dependencies** | P19 |
| **Measurement** | Every governance record type has a corresponding span; all span attribute names are dot-separated; `evidence.emit` span carries chain tip hash; `breakglass.open` span is alertable; evidence chain verification passes without OTel export; backpressure test confirms evidence writes complete when span export is blocked |
| **Files Modified** | `lionagi/protocols/governance/tracing.py`, `lionagi/protocols/generic/log.py`, `tests/governance/test_tracing.py` |

### P21 — SDK-Native Governed Endpoints (G1)

| Field | Value |
|-------|-------|
| **Category** | G1 — SDK-native adapters |
| **Playbook** | feature |
| **Effort** | medium |
| **Scope** | Zero-rewrite governed endpoints for PydanticAI (`instrument_*` hooks), OpenAI Agents SDK (`AgentHook` event stream), and Anthropic Agent SDK (native tool-use block handling). Each adapter: ~200 LOC, accepts the user's existing agent/runner object, emits boundary evidence, enforces the compiled charter at the invocation boundary. Explicit claim matrix in docstring and test. |
| **Dependencies** | P17, P20 |
| **Measurement** | Contract tests with mocked typed event streams; package-shape validation confirms no framework internals are imported unconditionally; coarse/fine claim matrix documented; adversarial test: overclaiming internal tool call governance fails the claim matrix check |
| **Files Modified** | `lionagi/adapters/openai_agents.py`, `lionagi/adapters/anthropic_agents.py`, `tests/adapters/test_governed_sdk_adapters.py` |

### P22 — Framework Governed Endpoints (G2)

| Field | Value |
|-------|-------|
| **Category** | G2 — Framework adapters |
| **Playbook** | feature |
| **Effort** | medium |
| **Scope** | Zero-rewrite governed wrappers for LangGraph (`CompiledStateGraph`), LlamaIndex (`AgentWorkflow`, `FunctionAgent`, `ReActAgent`, `QueryEngineTool`, `FunctionTool`), and CrewAI (`Crew`, `CrewPlan` preflight). Coarse boundary governance only. Fine mode available only for translated lionagi `Tool` objects. CrewAI hierarchical delegation is not governed in v0. |
| **Dependencies** | P18, P19 |
| **Measurement** | Contract tests for graph invoke, LlamaIndex tool translation, CrewPlan preflight; claim matrix documents what is and is not governed; unsupported delegation call fails closed with a descriptive `GovernanceViolationError`; coarse-only adapters emit boundary evidence correctly |
| **Files Modified** | `lionagi/adapters/langgraph.py`, `lionagi/adapters/crewai.py`, `tests/adapters/test_governed_framework_adapters.py` |

### P23 — Governance Test and Adversarial Fixture Pack

| Field | Value |
|-------|-------|
| **Category** | H — Hygiene and docs |
| **Playbook** | feature |
| **Effort** | medium |
| **Scope** | Shared `conftest.py` and fixture library for all governance tests. Marker lanes: `@pytest.mark.governance`, `@pytest.mark.adversarial`, `@pytest.mark.property`. Deterministic adversarial suites for: charter parser (injection, malformed, wildcard), gate executor (bypass, type mismatch), evidence chain (tamper, reorder), registry (exact-match bypass), policy resolver (tie, ambiguity), SoD (conflict evasion), JIT (replay, expiry), break-glass (window exhaustion). Mutation and fuzz entrypoints for parser and evidence chain. |
| **Dependencies** | P13-P22 |
| **Measurement** | PR adversarial lane deterministic and passing; property tests cover chain/SoD/policy invariants with Hypothesis; adversarial bypass suite passes for all nine threat categories; mutation tests achieve ≥80% kill rate |
| **Files Modified** | `tests/governance/conftest.py`, `tests/governance/fixtures/`, `pyproject.toml`, `tests/governance/test_adversarial_charter.py`, `tests/governance/test_adversarial_gates.py`, `tests/governance/test_adversarial_evidence.py`, `tests/governance/test_adversarial_policy.py` |

### P24 — Docs, Migration Guide, and Public API Polish

| Field | Value |
|-------|-------|
| **Category** | H — Hygiene and docs |
| **Playbook** | fix |
| **Effort** | medium |
| **Scope** | User-facing governed orchestration guide, CLI reference (`li charter`, `li flow`), adapter claim matrix (table: adapter × event × governance level), migration path from ungoverned lionagi usage, standards cleanup pass, and `lionagi/governance/__init__.py` ergonomic public API with typed imports. Documentation examples must run or have snapshot-validated output. |
| **Dependencies** | P21, P22, P23 |
| **Measurement** | Documentation examples run or snapshot-validated; adapter claim matrix matches adversarial test fixture labels; migration guide covers the three most common ungoverned patterns; no banned token matches; public API imports resolve in <100ms |
| **Files Modified** | `docs/governance/`, `README.md`, `docs/adrs/README.md`, `lionagi/governance/__init__.py`, `tests/docs/test_governance_examples.py` |

---

## 8. Integration Plan

The governance build is additive. After implementation, existing lionagi code will be modified at
well-defined extension points only. New modules will live in `lionagi/protocols/governance/`
(proposed — not yet created). Existing modules will receive narrow additions.

### 8.1 `lionagi/protocols/`

**New package: `lionagi/protocols/governance/`**

| Module | Contents |
|--------|----------|
| `evidence.py` | `EvidenceNode`, `EvidenceChain`, `ChainVerifier`, SHA-256 hash utilities |
| `context.py` | `OperationContext`, `contextvars` bridge, policy pin, actor identity helpers |
| `gates.py` | `GateResult`, `GateExecutor`, `GateRegistry`, `GovernanceViolationError` |
| `charter.py` | `AgentCharter`, `SessionCharter`, `CharterId`, `RatificationRecord` |
| `dsl.py` | `CharterDSL` Pydantic models, YAML parser, JSON Schema exporter |
| `compiler.py` | Six-phase compiler: parse → validate → bind → normalize → emit → activate |
| `runtime.py` | `GateRegistration`, `RegistryEntry`, `SoDRule`, `EvidenceRequirement`, `TraceExpectation` |
| `certificate.py` | `TaskCertificate`, minting, verification, replay registry |
| `break_glass.py` | `BreakGlassProtocol`, attestation, lifecycle spans, `DEGRADED` certificate path |
| `jit.py` | `JITGrant`, single-use permit, expiry, `requires_jit` resolution |
| `registry.py` | `ToolRegistry`, exact-match, policy-resolved scope, compiled DSL target store |
| `policy.py` | `PolicyResolver`, most-specific-wins, deny-on-tie, staged release pinning |
| `sod.py` | `SoDMatrix`, bidirectional conflict, assignment-time check |
| `tracing.py` | OTel span projections, evidence hash embedding, backpressure-safe export |

**Modified: `lionagi/protocols/generic/`**

| File | Change |
|------|--------|
| `log.py` | Add tier-aware emission: `MUTABLE`, `PROTECTED`, `IMMUTABLE` record routing |
| `pile.py` | Add append-only mode for audit records |

**Modified: `lionagi/protocols/action/`**

| File | Change |
|------|--------|
| `tool.py` | Add `governance_meta` descriptor and `@governed_tool` decorator |
| `manager.py` | Add `execute_governed()` pipeline; raw execution path marked as non-governed |
| `function_calling.py` | Thread `OperationContext` through tool execution call stack |

### 8.2 `lionagi/session/`

| File | Change |
|------|--------|
| `branch.py` | Mount: evidence chain, policy pin, charter binding, role identity, JIT grant store |
| `session.py` | Mount: charter activation at flow entry, certificate store, break-glass window tracking |

Branch gains governance state that is initialized when a charter is activated. Ungoverned branches
remain unchanged — governance is opt-in at the `Session.flow()` or `Branch.activate_charter()` call.

### 8.3 `lionagi/cli/orchestrate/`

| File | Change |
|------|--------|
| `charter.py` | New: `li charter validate`, `li charter compile`, `li charter activate` commands |
| `flow.py` | Add: `--charter` flag, flow-plan governance validation, certificate reporting |
| `_orchestration.py` | Add: artifact evidence sidecars, run-end certificate hook |

### 8.4 `lionagi/adapters/`

New adapter modules (P21, P22). The base class is `GovernedAdapter` in
`lionagi/adapters/governed_base.py` (ADR-0068). Each concrete adapter module contains
the framework-specific `GovernedAdapter` subclass and its claim matrix.

| Module | Exported Class | Target | Play |
|--------|----------------|--------|------|
| `lionagi/adapters/openai_agents.py` | `GovernedOpenAIAgent` | OpenAI Agents SDK | P21 |
| `lionagi/adapters/anthropic_agents.py` | `GovernedAnthropicAgent` | Anthropic Agent SDK | P21 |
| `lionagi/adapters/langgraph.py` | `GovernedLangGraph` | LangGraph `CompiledStateGraph` | P22 |
| `lionagi/adapters/crewai.py` | `GovernedCrew` | CrewAI `Crew` | P22 |

Each adapter module exports exactly one concrete `GovernedAdapter` subclass. The claim matrix
is documented in the class docstring and mirrored in the test fixture (`tests/adapters/`).

No adapter module is added until the substrate (P15-P17) and flow governance (P18) are complete
and verified. An adapter claiming governance before the substrate exists is a false claim.

### 8.5 `lionagi/tools/`

| Change | Details |
|--------|---------|
| Add `governance_meta` to built-in tools | `read_file`, `write_file`, `exec_command`, and other built-ins get governance metadata |
| Safe defaults | Destructive-action tools (write, exec) default to registry-required, hard gate |
| Registry identity | Built-in tool IDs are canonical and match the DSL `tool.*` identifier namespace |

### 8.6 Public API (`lionagi/governance/`)

After P19, expose ergonomic imports over the full protocol path:

```python
from lionagi.governance import (
    AgentCharter,
    SessionCharter,
    TaskCertificate,
    EvidenceChain,
    OperationContext,
    ToolRegistry,
    PolicyResolver,
    SoDMatrix,
    BreakGlassProtocol,
    JITGrant,
    governed_tool,
    GovernanceViolationError,
)
```

The public API is the migration entry point. v1 migration attaches governance to existing branches
via `branch.activate_charter(charter)` without requiring Session-level flow rewrites.

---

## 9. Risk Register

| Risk | Likelihood (1-5) | Impact (1-5) | Mitigation | Recovery Rule |
|------|-----------------|--------------|------------|---------------|
| **DSL/runtime drift**: Charter compiles to targets the runtime does not enforce, creating a false governance claim | 3 | 5 | Golden compile snapshots from the DSL parser and compiler phases; activation fails closed on unresolved targets; each target type has a corresponding integration test in the flow governance phase | ADR-impl drift triggers the "match ADR or revise" recovery: pause implementation, revise the ADR, re-pass the gate |
| **Coarse adapter overclaiming**: Adapter claims governance for internal framework calls that are not enforced | 4 | 5 | Claim matrix per adapter; the adversarial fixture pack phase checks claim matrix against emitted evidence; coarse-only adapters emit boundary evidence only | Later bypass/test gaps queue a substrate patch or ADR amendment; adapter ships with explicit scope limitation in docs |
| **`OperationContext` propagation ambiguity**: `contextvars` used as authoritative store, causing governance gaps under concurrency | 3 | 4 | Explicit propagation is the design; `contextvars` is bridge-only; the OperationContext phase tests prove no authoritative hidden accumulation; async propagation tests cover concurrent operation paths | Substrate bug queues a foundation patch and blocks API widening until the fix is verified |
| **`GateResult` type duplication**: Incompatible `GateResult` types across ADR-0044 and ADR-0050 cause certificate, span, and policy inconsistencies | 4 | 4 | The ADR consolidation phase establishes one canonical `GateResult` before any substrate implementation; cross-reference table in ADR revision locks ownership | ADR contradictions get a binding tie-break from the ADR consolidation phase; no implementation proceeds without resolution |
| **Evidence integrity misrepresented**: Library-mode hash chains mistakenly claimed as tamper-proof storage | 3 | 5 | Documentation is explicit: library-mode tamper-evidence, sensitive-field exclusion, backend immutability is not provided by lionagi core; audit trail is advisory in adversarial custody settings | Any claim of tamper-proof storage triggers an immediate doc correction and governance violation |
| **Break-glass undermines HARD gate semantics**: Emergency path is used to bypass normal governance as a convenience | 3 | 5 | Break-glass has a separate emergency lifecycle distinct from normal gate results; all activations emit immutable justification evidence; DEGRADED certificate signals non-standard run; lifecycle spans are alertable | Behavioral gaps queue a substrate/ADR coverage patch; repeated misuse triggers a policy tightening revision |
| **External package APIs move before adapters land**: LangGraph, LlamaIndex, CrewAI, or SDK-native packages change their event or hook API | 3 | 3 | Runtime package validation at adapter import; contract tests use mocked event shapes with explicit version pins; `claim_matrix.py` documents which API version the claim matrix was verified against | Integration breakage triggers rollback to last verified version, patch, or documented incompatibility until upstream stabilizes |
| **Deny-on-tie creates availability failures**: Common policies accidentally hit the tie-deny rule in production | 3 | 4 | Canary policy tests in the governance layer phase; deny-on-tie test cases in the adversarial fixture pack; clear error messages name the conflicting rule IDs; deny remains the integrity-safe default | Later defects queue a policy patch with specificity adjustments |
| **Trace export becomes treated as compliance storage**: Teams use OTel export instead of the evidence chain for audits | 2 | 4 | Architecture documentation is explicit: spans are projections, evidence chain is authoritative; `certificate.mint` span does not contain the full certificate; evidence hash in spans is for correlation only | Integration breakage blocks downstream until the misuse is corrected and the evidence chain is adopted |
| **Scope exceeds 13 phases**: Implementation finds the 13 phases insufficient and scope creeps | 4 | 4 | G3 adapters, REST evidence API, dashboards, and fine-grained framework internals are the first cut; the adversarial fixture and docs phases are deferrable if substrate phases slip | Budget/time pressure forces a minimum viable cut: G1+substrate+flow is the irreducible core; G2 and layer objects are deferrable |

---

## Cross-References

| Document | Path | Contents |
|----------|------|----------|
| DSL Style | [`docs/governance/standards/dsl-style.md`](standards/dsl-style.md) | Charter DSL syntax conventions, required blocks, formatting rules, two examples |
| ADR Style | [`docs/governance/standards/adr-style.md`](standards/adr-style.md) | ADR structure, status values, revision conventions |
| Test Style | [`docs/governance/standards/test-style.md`](standards/test-style.md) | Test fixture naming, marker lanes, coverage thresholds |
| Trace Naming | [`docs/governance/standards/trace-naming.md`](standards/trace-naming.md) | Span name registry, required attributes, retention tiers |
| Error Messages | [`docs/governance/standards/error-messages.md`](standards/error-messages.md) | Error message conventions for human and agent consumers |
| Commit and PR Style | [`docs/governance/standards/commit-and-pr-style.md`](standards/commit-and-pr-style.md) | Commit message format, PR description structure |
| Charter DSL v0 | [`docs/governance/charter-dsl-v0.md`](charter-dsl-v0.md) | Canonical Charter DSL v0 with three complete examples |

---

*This document is authoritative for implementation phases P12–P24. Changes require a governance ADR
revision or an explicit direction amendment with project maintainers' approval.*
