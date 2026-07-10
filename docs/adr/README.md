# Architecture Decision Records

This directory is the canonical ADR corpus for lionagi. It replaces the earlier corpus now
preserved at `docs/_archive/v0/` (moved there intact, original filenames kept). Every archived
record receives an explicit disposition — carried forward into a new ADR, merged into one, or
retired — recorded in `dispositions.yaml` in this directory once the corpus is complete.

Each ADR follows `TEMPLATE.md` and is exactly one of two kinds:

- **Retrospective** — records what the code does today, honestly, including a
  current-vs-ideal delta table whose rows are phrased to lift directly into issues.
- **Aspirational** — records a target state that is decided but not yet implemented.

A gap between a retrospective truth and an aspirational target is an issue, never a blurred
document.

## Numbering

Numbers are allocated in per-area blocks so areas can be authored independently without
collisions. Unused numbers inside a block are intentional gaps, not missing documents.

| Area | Block | Area | Block |
|------|-------|------|-------|
| core-data-model | 0001-0005 | persistence-state | 0055-0061 |
| messages-context | 0006-0010 | cli-surface | 0062-0067 |
| actions-tools | 0011-0015 | scheduling-control-plane | 0068-0075 |
| session-branch | 0016-0020 | studio | 0076-0085 |
| operations | 0021-0026 | governance | 0086-0089 |
| service-providers | 0027-0032 | substrates | 0090-0095 |
| orchestration | 0033-0040 | agent-roles | 0041-0046 |
| hooks | 0047-0049 | utilities | 0050-0054 |

## Index

### core-data-model (0001-0005)

- [ADR-0001](ADR-0001-element-identity-and-polymorphic-serialization-envelope.md) — Element
  identity and the polymorphic serialization envelope
- [ADR-0002](ADR-0002-uuid-keyed-ordered-collection-model.md) — UUID-keyed ordered collection
  model (Pile and Progression)
- [ADR-0003](ADR-0003-in-process-event-execution-lifecycle.md) — In-process Event execution
  lifecycle
- [ADR-0004](ADR-0004-directed-graph-structural-invariants.md) — Directed graph structural
  invariants
- 0005 — unused (intentional gap)

### messages-context (0006-0010)

- [ADR-0006](ADR-0006-conversational-message-envelope-and-ordered-history.md) — Conversational
  message envelope and ordered history
- [ADR-0007](ADR-0007-canonical-turn-request-compilation-boundary.md) — Canonical turn-request
  compilation boundary
- [ADR-0008](ADR-0008-pre-turn-context-provider-execution-and-attribution.md) — Pre-turn
  context-provider execution and attribution
- 0009-0010 — unused (intentional gaps)

### actions-tools (0011-0015)

- [ADR-0011](ADR-0011-function-tool-descriptor-and-branch-registry.md) — Function tool
  descriptor and Branch registry
- [ADR-0012](ADR-0012-branch-action-execution-and-event-lifecycle.md) — Branch action
  execution and event lifecycle
- [ADR-0013](ADR-0013-built-in-tool-provider-and-branch-binding.md) — Built-in tool provider
  and Branch binding
- 0014-0015 — unused (intentional gaps)

### session-branch (0016-0020)

- [ADR-0016](ADR-0016-branch-conversation-aggregate-and-attachment-boundary.md) — Branch
  conversation aggregate and attachment boundary
- [ADR-0017](ADR-0017-session-membership-and-coordination-boundary.md) — Session membership
  and coordination boundary
- [ADR-0018](ADR-0018-turn-scoped-branch-execution-state.md) — Turn-scoped Branch execution
  state
- 0019-0020 — unused (intentional gaps)

### operations (0021-0026)

- [ADR-0021](ADR-0021-branch-operation-facade-and-turn-adapters.md) — Branch operation facade
  and turn adapters
- [ADR-0022](ADR-0022-composed-branch-operation-pipeline.md) — Composed branch operation
  pipeline
- [ADR-0023](ADR-0023-dependency-aware-operation-graph-execution-kernel.md) — Dependency-aware
  operation graph execution kernel
- [ADR-0024](ADR-0024-lndl-operate-integration-adapter.md) — LNDL operate integration adapter
- 0025-0026 — unused (intentional gaps)

### service-providers (0027-0032)

- [ADR-0027](ADR-0027-model-service-facade-and-endpoint-resolution.md) — Model-service facade
  and endpoint resolution
- [ADR-0028](ADR-0028-validated-provider-adapter-catalog.md) — Validated provider-adapter
  catalog
- [ADR-0029](ADR-0029-unified-request-admission-deadline-and-resilience-policy.md) — Unified
  request admission, deadline, and resilience policy
- [ADR-0030](ADR-0030-agentic-provider-adapter-boundary.md) — Agentic provider-adapter
  boundary
- 0031-0032 — unused (intentional gaps)

### orchestration (0033-0040)

- [ADR-0033](ADR-0033-operation-graph-orchestration-boundary.md) — Operation-graph
  orchestration boundary
- [ADR-0034](ADR-0034-domain-engine-coordination-and-autonomy-safeguards.md) — Domain-engine
  coordination and autonomy safeguards
- [ADR-0035](ADR-0035-persisted-run-completion-contract.md) — Persisted run-completion
  contract
- [ADR-0036](ADR-0036-casts-role-palettes-as-playstyle.md) — Casts role palettes as playstyle
- [ADR-0037](ADR-0037-resident-engine-host-and-task-queue.md) — Resident engine host and task
  queue
- [ADR-0038](ADR-0038-escalation-tier-routing.md) — Escalation tier routing
- 0039-0040 — unused (intentional gaps)

### agent-roles (0041-0046)

- [ADR-0041](ADR-0041-agent-specification-and-branch-construction.md) — Agent specification
  and Branch construction boundary
- [ADR-0042](ADR-0042-casts-pattern-catalog-and-typed-role-authoring.md) — Casts pattern
  catalog and typed role authoring
- [ADR-0043](ADR-0043-per-role-configuration-resolution.md) — Per-role configuration
  resolution
- [ADR-0044](ADR-0044-agent-prompt-directives-and-executable-permissions.md) — Agent prompt
  directives and executable permissions
- 0045-0046 — unused (intentional gaps)

### hooks (0047-0049)

- [ADR-0047](ADR-0047-hook-mechanism-scopes-and-canonical-ownership.md) — Hook mechanism
  scopes and canonical ownership
- 0048-0049 — unused (intentional gaps)

Remaining areas land here as their records are accepted.
