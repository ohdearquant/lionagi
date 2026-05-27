# Governance ADR Cross-Reference

Status: revised
Date: 2026-05-27
Revised: P12

This table is the P12 ownership index for governance ADRs. It prevents duplicate runtime type
definitions and binds each ADR to the implementation play that will build or consume it.

## Governance Type Ownership

| Governance type or artifact | Canonical ADR owner | Notes |
|-----------------------------|---------------------|-------|
| `ImmutableEvidenceNode` | ADR-0041 | Survives P12 unchanged. |
| `EvidenceRef` | ADR-0041 | ADR-0042 and other ADRs reference, not redefine. |
| `EvidenceChain` | ADR-0041 | Certificate and trace records embed chain hashes. |
| `TaskCertificate` | ADR-0042 | Minting in P18; verification/replay in P19. |
| `CertificateState` | ADR-0042 | Includes `BREAK_GLASS` and supersession states. |
| `Defensibility` | ADR-0042 | `FULL`, `PARTIAL`, `DEGRADED`, `FAILED`. |
| certificate grade formula | ADR-0042 | Deterministic P12 formula. |
| `GovernedToolMeta` | ADR-0043 | Declaration metadata only. |
| `governed_tool` decorator | ADR-0043 | Attaches metadata; does not decide policy. |
| governed wrapper bypass rules | ADR-0043 | Runtime path enforced by ActionManager in P17. |
| `GateResult` | ADR-0044 | Single canonical owner; removed from ADR-0043 and ADR-0050. |
| `GateEnforcement` | ADR-0044 | `HARD`, `SOFT`, `ADVISORY`. |
| `GateVerdict` | ADR-0044 | `ALLOW`, `DENY`, `SKIP`. |
| `ToolGate` | ADR-0044 | Gate registration and evaluation contract. |
| `BreakGlassReason` | ADR-0045 | Emergency reason taxonomy. |
| `BreakGlassRequest` | ADR-0045 | Emergency request and attestation. |
| `BreakGlassWindow` | ADR-0045 | Active emergency lifecycle window. |
| `BreakGlassEvent` | ADR-0045 | Open, use, close, expire, revoke, notify evidence. |
| `PermitToken` | ADR-0046 | Single-use elevated permit. |
| `JITGrant` | ADR-0046 | Grant lifecycle and permit binding. |
| `AgentCharter` | ADR-0047 | Runtime compiler output, not Python-first source. |
| `SessionCharter` | ADR-0047 | Runtime compiler output. |
| `CharterConstraint` | ADR-0047 | Emitted from DSL constraints. |
| `CharterActivationEvidence` | ADR-0047 | Activation proof and ratification hash evidence. |
| Charter DSL syntax | docs/governance/charter-dsl-v0.md | ADR-0047 consumes this authoritative spec. |
| `SoDPolicy` | ADR-0048 | Survives P12 unchanged. |
| `RoleAssignment` | ADR-0048 | Survives P12 unchanged. |
| `SoDCheckEvidence` | ADR-0048 | Consumed by charter and certificate flows. |
| `LogTier` | ADR-0049 | Survives P12 unchanged. |
| `ServiceContext` | ADR-0050 | Session/branch governance binding. |
| `OperationContext` | ADR-0050 | Explicit per-operation active assertion. |
| `OperationActiveAssertion` | ADR-0050 | Evidence embedding projection. |
| `EvidenceEmission` | ADR-0050 | Operation-local emitted evidence reference. |
| `GateResultProjection` | ADR-0050 | Reference to ADR-0044 result; not a duplicate result type. |
| `RegistryCategory` | ADR-0051 | Exact categories from Charter DSL v0. |
| `RegistryScope` | ADR-0051 | Runtime lookup scope. |
| `RegistryEntry` | ADR-0051 | Compiled DSL target. |
| `ToolRegistryPolicy` | ADR-0051 | Runtime registry artifact. |
| `RegistryLookupResult` | ADR-0051 | Lookup evidence payload. |
| `PolicyBundle` | ADR-0052 | Survives P12 unchanged. |
| `PolicyRelease` | ADR-0052 | Survives P12 unchanged. |
| `PolicyResolver` | ADR-0052 | Most-specific-wins and deny-on-tie owner. |
| permission resolution semantics | ADR-0052 | DSL permissions compile into this resolver. |

## ADR To Implementation Play

| ADR | Title | P12 status | Implementation play | Implementation responsibility |
|-----|-------|------------|---------------------|-------------------------------|
| ADR-0041 | Immutable Evidence Nodes | SURVIVE | P15 | Evidence chain, hash verification, append-only audit pile, tier-aware log integration. |
| ADR-0042 | Task Certificate | REVISE | P18, P19 | P18 mints certificates; P19 verifies, detects replay, and queries certificate store. |
| ADR-0043 | Governed Tool Wrapper | REVISE | P17 | Tool metadata, decorator, governed ActionManager route, raw bypass controls. |
| ADR-0044 | Tool Gates | REVISE | P17 | Canonical `GateResult`, gate executor, hard/soft/advisory behavior. |
| ADR-0045 | Break-Glass Protocol | REVISE | P19, P20 | Lifecycle evidence and degraded certificate path in P19; spans in P20. |
| ADR-0046 | JIT Tool Grant | REVISE | P19, P20 | Single-use permits and registry/policy `requires_jit` in P19; permit spans in P20. |
| ADR-0047 | Agent Charter | REVISE | P13, P14 | DSL parser/schema in P13; compiler, runtime targets, activation evidence in P14. |
| ADR-0048 | Agent SoD | SURVIVE | P19 | SoD matrix, assignment-time checks, bidirectional conflict tests. |
| ADR-0049 | Log Tier Governance | SURVIVE | P15, P20 | Runtime log tiers in P15; trace retention alignment in P20. |
| ADR-0050 | Operation Context | REVISE | P16 | Explicit context propagation, policy pin, active assertion evidence embedding. |
| ADR-0051 | Tool Registry Allowlists | REVISE | P14, P19 | Compile registry entries in P14; runtime exact-match registry in P19. |
| ADR-0052 | Policy Resolution | SURVIVE | P19 | Policy resolver, release pinning, most-specific-wins, deny-on-tie. |

Provider adapter plays consume these primitives after substrate completion:

| Play | Consumes | Constraint |
|------|----------|------------|
| P21 | ADR-0043, ADR-0044, ADR-0050, ADR-0051, ADR-0052 | SDK-native adapters must govern only events backed by runtime evidence. |
| P22 | ADR-0042, ADR-0043, ADR-0044, ADR-0050, ADR-0051, ADR-0052 | Framework adapters are coarse-boundary by default; fine claims require translated lionagi tools. |

## Convergence Groups

| Group | Member ADRs | P12 resolution |
|-------|-------------|----------------|
| ActionManager Pipeline | ADR-0043, ADR-0044, ADR-0050 | ADR-0043 owns wrapper metadata and the governed execution route; ADR-0044 owns gate execution and `GateResult`; ADR-0050 owns explicit `OperationContext` propagation and active assertion embedding. |
| Charter DSL Compilation | ADR-0047, ADR-0051, ADR-0052 | ADR-0047 owns DSL-to-runtime charter activation; ADR-0051 registry entries are compiled DSL targets; ADR-0052 remains the policy resolution algorithm. |
| Emergency And Elevated Paths | ADR-0045, ADR-0046 | ADR-0045 owns emergency break-glass lifecycle evidence and degraded certificates; ADR-0046 owns planned, single-use JIT permits resolved through registry and policy. |
| Evidence And Certification | ADR-0041, ADR-0042, ADR-0049, ADR-0050 | ADR-0041 is the immutable evidence substrate; ADR-0042 certifies run-level process adherence; ADR-0049 classifies retention tiers; ADR-0050 embeds active assertions. |

## Cross-ADR Ownership Rules

- `GateResult` is defined only by ADR-0044.
- `OperationContext` may store gate result IDs and `GateResultProjection`, but never a second gate
  result schema.
- `GovernedToolMeta.requires_jit_hint` is diagnostic only; ADR-0051 and ADR-0052 decide whether
  ADR-0046 JIT is required.
- Runtime `AgentCharter` and `SessionCharter` objects are compiler outputs from ADR-0047, not
  hand-authored accepted governance sources.
- Registry entries in accepted charters are compiled DSL targets from ADR-0047 and owned at
  runtime by ADR-0051.
- Break-glass is not a hard-gate override; it is an emergency lifecycle owned by ADR-0045 and
  reflected as degraded defensibility by ADR-0042.

## Evidence Inventory Index

P12 source coverage note: standalone P1-P10 deliverable files were not present in this worktree.
The revised ADRs therefore cite the P11 synthesis, the Charter DSL v0 specification, the governance
standards, and the prior governance research paths that the original ADRs already referenced.

| Evidence source | Used for |
|-----------------|----------|
| docs/governance/direction.md section 5 | P9 trace taxonomy and P11 trace additions. |
| docs/governance/direction.md section 6 | ADR slate verdicts and convergence groups. |
| docs/governance/direction.md P13-P22 | Implementation play mapping. |
| docs/governance/charter-dsl-v0.md | DSL-first charter and compiled registry target ownership. |
| docs/governance/standards/adr-style.md | Required ADR format and type ownership rule. |
| docs/governance/standards/trace-naming.md | Required governance span names and attributes. |
| prior governance research `01_design/007-decision-certificate/ADR-007-decision-certificate.md` | Task certificate pattern. |
| prior governance research `01_design/008-policy-gates/ADR-008-policy-gates.md` | Gate tier pattern. |
| prior governance research `01_design/010-action-declaration/ADR-010-action-declaration.md` | Governed action declaration pattern. |
| prior governance research `01_design/013-service-context/ADR-013-service-context.md` | Active assertion and context propagation pattern. |
| prior governance research `01_design/015-jit-role/ADR-015-jit-role.md` | JIT no-standing-capability pattern. |
| prior governance research `01_design/016-break-glass/ADR-016-break-glass.md` | Break-glass degraded lifecycle pattern. |
| prior governance research `01_design/025-charter/ADR-025-charter.md` | Charter ratification and enforcement binding pattern. |
| prior governance research `01_design/031-registry-allowlists/ADR-031-registry-allowlists.md` | Scoped registry allowlist pattern. |
