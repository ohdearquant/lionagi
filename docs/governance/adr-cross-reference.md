# Governance ADR Cross-Reference

Status: revised
Date: 2026-05-27

> **Historical note**: this document predates the ADR corpus restructure. All bare `ADR-00NN`
> references below have been re-anchored to `v0-00NN` because those numbers now collide with
> unrelated ADRs in the current corpus at `docs/adr/`. Every `v0-00NN` here is a record archived
> at `docs/_archive/v0/ADR-00NN-*.md`; see `docs/adr/dispositions.yaml` for the authoritative
> mapping. Per that ledger, `v0-0039` and `v0-0041`–`v0-0052` were all merged into
> [ADR-0087 — Evidence-backed governed execution](../adr/ADR-0087-evidence-backed-governed-execution.md)
> (its own Relations line confirms: "supersedes v0-0039, v0-0041, ..., v0-0052; extends
> ADR-0086"), and `v0-0068`–`v0-0070` were merged into
> [ADR-0086 — Local tool controls and session authorization observation](../adr/ADR-0086-local-tool-controls-and-session-authorization.md).
> The per-type/per-phase detail below is the P12-era design intent, not a live ownership index —
> read ADR-0087 and ADR-0086 for what actually shipped.

This table is the ownership index for governance ADRs (produced during the ADR consolidation
phase, P12). It prevents duplicate runtime type definitions and binds each ADR to the
implementation phase that will build or consume it.

## Governance Type Ownership

| Governance type or artifact | Canonical v0 ADR owner | Notes |
|-----------------------------|---------------------|-------|
| `ImmutableEvidenceNode` | v0-0041 | Survives P12 unchanged. |
| `EvidenceRef` | v0-0041 | v0-0042 and other ADRs reference, not redefine. |
| `EvidenceChain` | v0-0041 | Certificate and trace records embed chain hashes. |
| `TaskCertificate` | v0-0042 | Minting in P18; verification/replay in P19. |
| `CertificateState` | v0-0042 | Includes `BREAK_GLASS` and supersession states. |
| `Defensibility` | v0-0042 | `FULL`, `PARTIAL`, `DEGRADED`, `FAILED`. |
| certificate grade formula | v0-0042 | Deterministic P12 formula. |
| `GovernedToolMeta` | v0-0043 | Declaration metadata only. |
| `governed_tool` decorator | v0-0043 | Attaches metadata; does not decide policy. |
| governed wrapper bypass rules | v0-0043 | Runtime path enforced by ActionManager in P17. |
| `GateResult` | v0-0044 | Single canonical owner; removed from v0-0043 and v0-0050. |
| `GateEnforcement` | v0-0044 | `HARD`, `SOFT`, `ADVISORY`. |
| `GateVerdict` | v0-0044 | `ALLOW`, `DENY`, `ADVISORY`. |
| `ToolGate` | v0-0044 | Gate registration and evaluation contract. |
| `BreakGlassReason` | v0-0045 | Emergency reason taxonomy. |
| `BreakGlassRequest` | v0-0045 | Emergency request and attestation. |
| `BreakGlassWindow` | v0-0045 | Active emergency lifecycle window. |
| `BreakGlassEvent` | v0-0045 | Open, use, close, expire, revoke, notify evidence. |
| `PermitToken` | v0-0046 | Single-use elevated permit. |
| `JITGrant` | v0-0046 | Grant lifecycle and permit binding. |
| `AgentCharter` | v0-0047 | Runtime compiler output, not Python-first source. |
| `SessionCharter` | v0-0047 | Runtime compiler output. |
| `CharterConstraint` | v0-0047 | Emitted from DSL constraints. |
| `CharterActivationEvidence` | v0-0047 | Activation proof and ratification hash evidence. |
| Charter DSL syntax | docs/governance/charter-dsl-v0.md | v0-0047 consumes this authoritative spec. |
| `SoDPolicy` | v0-0048 | Survives P12 unchanged. |
| `RoleAssignment` | v0-0048 | Survives P12 unchanged. |
| `SoDCheckEvidence` | v0-0048 | Consumed by charter and certificate flows. |
| `LogTier` | v0-0049 | Survives P12 unchanged. |
| `ServiceContext` | v0-0050 | Session/branch governance binding. |
| `OperationContext` | v0-0050 | Explicit per-operation active assertion. |
| `OperationActiveAssertion` | v0-0050 | Evidence embedding projection. |
| `EvidenceEmission` | v0-0050 | Operation-local emitted evidence reference. |
| `GateResultProjection` | v0-0050 | Reference to v0-0044 result; not a duplicate result type. |
| `RegistryCategory` | v0-0051 | Exact categories from Charter DSL v0. |
| `RegistryScope` | v0-0051 | Runtime lookup scope. |
| `RegistryEntry` | v0-0051 | Compiled DSL target. |
| `ToolRegistryPolicy` | v0-0051 | Runtime registry artifact. |
| `RegistryLookupResult` | v0-0051 | Lookup evidence payload. |
| `PolicyBundle` | v0-0052 | Survives P12 unchanged. |
| `PolicyRelease` | v0-0052 | Survives P12 unchanged. |
| `PolicyResolver` | v0-0052 | Most-specific-wins and deny-on-tie owner. |
| permission resolution semantics | v0-0052 | DSL permissions compile into this resolver. |

## ADR To Implementation Phase

| v0 ADR | Title | Consolidation verdict | Implementation phase | Implementation responsibility |
|-----|-------|-----------------------|----------------------|-------------------------------|
| v0-0041 | Immutable Evidence Nodes | SURVIVE | Evidence chain and log tiers (P15) | Evidence chain, hash verification, append-only audit pile, tier-aware log integration. |
| v0-0042 | Task Certificate | REVISE | Flow governance integration (P18); governance layer objects (P19) | P18 mints certificates; P19 verifies, detects replay, and queries certificate store. |
| v0-0043 | Governed Tool Wrapper | REVISE | Governed ActionManager gates (P17) | Tool metadata, decorator, governed ActionManager route, raw bypass controls. |
| v0-0044 | Tool Gates | REVISE | Governed ActionManager gates (P17) | Canonical `GateResult`, gate executor, hard/soft/advisory behavior. |
| v0-0045 | Break-Glass Protocol | REVISE | Governance layer objects (P19); OTel tracing (P20) | Lifecycle evidence and degraded certificate path in P19; spans in P20. |
| v0-0046 | JIT Tool Grant | REVISE | Governance layer objects (P19); OTel tracing (P20) | Single-use permits and registry/policy `requires_jit` in P19; permit spans in P20. |
| v0-0047 | Agent Charter | REVISE | Charter DSL parser and schema (P13); Charter compiler and runtime targets (P14) | DSL parser/schema in P13; compiler, runtime targets, activation evidence in P14. |
| v0-0048 | Agent SoD | SURVIVE | Governance layer objects (P19) | SoD matrix, assignment-time checks, bidirectional conflict tests. |
| v0-0049 | Log Tier Governance | SURVIVE | Evidence chain and log tiers (P15); OTel tracing (P20) | Runtime log tiers in P15; trace retention alignment in P20. |
| v0-0050 | Operation Context | REVISE | OperationContext and policy release pinning (P16) | Explicit context propagation, policy pin, active assertion evidence embedding. |
| v0-0051 | Tool Registry Allowlists | REVISE | Charter compiler and runtime targets (P14); governance layer objects (P19) | Compile registry entries in P14; runtime exact-match registry in P19. |
| v0-0052 | Policy Resolution | SURVIVE | Governance layer objects (P19) | Policy resolver, release pinning, most-specific-wins, deny-on-tie. |

Additional ADRs adopted during the governance build-out:

| v0 ADR | Title | Status | Implementation phase | Implementation responsibility |
|-----|-------|--------|----------------------|-------------------------------|
| v0-0068 | Zero-rewrite governed adapter protocol | Accepted | Framework governed endpoints (P22) | Base class `GovernedAdapter` in `lionagi/adapters/governed_base.py`; each concrete adapter subclasses it. |
| v0-0069 | Tenant scope boundary | Accepted | Governance layer objects (P19) | Tenant-scoped registry and policy resolution; scope boundary enforcement in `ToolRegistryPolicy`. |
| v0-0070 | Governance tracing and observability | Accepted | OTel tracing (P20) | OTel span projection, evidence hash embedding, backpressure-safe export, retention tier alignment. |

Provider adapter phases consume these primitives after substrate completion:

| Phase | Consumes | Constraint |
|-------|----------|------------|
| SDK-native governed endpoints (P21) | v0-0043, v0-0044, v0-0050, v0-0051, v0-0052 | SDK-native adapters must govern only events backed by runtime evidence. |
| Framework governed endpoints (P22) | v0-0042, v0-0043, v0-0044, v0-0050, v0-0051, v0-0052 | Framework adapters are coarse-boundary by default; fine claims require translated lionagi tools. |

## Convergence Groups

| Group | Member v0 ADRs | P12 resolution |
|-------|-------------|----------------|
| ActionManager Pipeline | v0-0043, v0-0044, v0-0050 | v0-0043 owns wrapper metadata and the governed execution route; v0-0044 owns gate execution and `GateResult`; v0-0050 owns explicit `OperationContext` propagation and active assertion embedding. |
| Charter DSL Compilation | v0-0047, v0-0051, v0-0052 | v0-0047 owns DSL-to-runtime charter activation; v0-0051 registry entries are compiled DSL targets; v0-0052 remains the policy resolution algorithm. |
| Emergency And Elevated Paths | v0-0045, v0-0046 | v0-0045 owns emergency break-glass lifecycle evidence and degraded certificates; v0-0046 owns planned, single-use JIT permits resolved through registry and policy. |
| Evidence And Certification | v0-0041, v0-0042, v0-0049, v0-0050 | v0-0041 is the immutable evidence substrate; v0-0042 certifies run-level process adherence; v0-0049 classifies retention tiers; v0-0050 embeds active assertions. |

## Cross-ADR Ownership Rules

- `GateResult` is defined only by v0-0044.
- `OperationContext` may store gate result IDs and `GateResultProjection`, but never a second gate
  result schema.
- `GovernedToolMeta.requires_jit_hint` is diagnostic only; v0-0051 and v0-0052 decide whether
  v0-0046 JIT is required.
- Runtime `AgentCharter` and `SessionCharter` objects are compiler outputs from v0-0047, not
  hand-authored accepted governance sources.
- Registry entries in accepted charters are compiled DSL targets from v0-0047 and owned at
  runtime by v0-0051.
- Break-glass is not a hard-gate override; it is an emergency lifecycle owned by v0-0045 and
  reflected as degraded defensibility by v0-0042.

## Evidence Inventory Index

The revised ADRs cite the governance direction document, the Charter DSL v0 specification,
the governance standards, and the prior governance research that the original ADRs already
referenced.

| Evidence source | Used for |
|-----------------|----------|
| docs/governance/direction.md section 5 | Governance span taxonomy and span additions. |
| docs/governance/direction.md section 6 | ADR slate verdicts and convergence groups. |
| docs/governance/direction.md section 7 | Implementation phase mapping. |
| docs/governance/charter-dsl-v0.md | DSL-first charter and compiled registry target ownership. |
| docs/governance/standards/adr-style.md | Required ADR format and type ownership rule. |
| docs/governance/standards/trace-naming.md | Required governance span names and attributes. |
| Prior governance research — task certificate pattern | Task certificate pattern. |
| Prior governance research — policy gates pattern | Gate tier pattern. |
| Prior governance research — governed action declaration pattern | Governed action declaration pattern. |
| Prior governance research — service context pattern | Active assertion and context propagation pattern. |
| Prior governance research — JIT no-standing-capability pattern | JIT no-standing-capability pattern. |
| Prior governance research — break-glass degraded lifecycle pattern | Break-glass degraded lifecycle pattern. |
| Prior governance research — charter ratification pattern | Charter ratification and enforcement binding pattern. |
| Prior governance research — registry allowlists pattern | Scoped registry allowlist pattern. |
