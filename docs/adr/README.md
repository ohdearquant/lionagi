# Architecture Decision Records

This directory is the canonical ADR corpus for lionagi. It replaces the earlier corpus now
preserved at `docs/_archive/v0/` (moved there intact, original filenames kept). Every archived
record has an explicit disposition — carried forward into a new ADR, merged into one, or
retired — recorded in `dispositions.yaml` in this directory (one row per archived record).

Each ADR follows `TEMPLATE.md` and is exactly one of two kinds:

- **Retrospective** — records what the code does today, honestly, including a
  current-vs-ideal delta table whose rows are phrased to lift directly into issues.
- **Aspirational** — records a target state that is decided but not yet implemented.

A gap between a retrospective truth and an aspirational target is an issue, never a blurred
document.

## Architecture-quality figures (κ and τ)

Several ADRs report two figures in their Consequences sections. κ is a coupling density:
directed dependencies divided by n × (n − 1) over the components that ADR's own design diagram
names, where a component is a named box and an edge is a direct dependency drawn in that
diagram. τ is an estimated fraction of components testable in isolation through their declared
seams. Both are the author's structural reading of the recorded design: arithmetically
consistent within each ADR, not independently re-derived from source, and not comparable
across ADRs whose component maps differ in granularity. Read them as review aids for the
specific component map shown, not as corpus-wide measurements.

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

ADR-0094 and ADR-0095 are documented numbering exceptions: both retain numbers from the
substrates block (0090-0095) despite declaring `scheduling-control-plane` as their area.
They landed in that range before the area mismatch was identified; retaining the numbers
avoids collisions with other ADR work and preserves existing inbound references.

ADR-0104 is a documented numbering exception for the same reason: it declares `cli-surface`
but carries a number far outside that block (0062-0067), which was allocated before the
per-area scheme was applied to it. It is indexed under cli-surface and keeps its number, so
inbound references stay valid.

## Index

### core-data-model (0001-0005)

- [ADR-0001](ADR-0001-element-identity-and-polymorphic-serialization-envelope.md) — Element
  identity and polymorphic serialization envelope
- [ADR-0002](ADR-0002-uuid-keyed-ordered-collection-model.md) — UUID-keyed ordered collection
  model
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
- [ADR-0008](ADR-0008-pre-turn-context-provider-execution-and-attribution.md) — Pre-turn context-
  provider execution and attribution
- 0009-0010 — unused (intentional gaps)

### actions-tools (0011-0015)

- [ADR-0011](ADR-0011-function-tool-descriptor-and-branch-registry.md) — Function tool descriptor
  and Branch registry
- [ADR-0012](ADR-0012-branch-action-execution-and-event-lifecycle.md) — Branch action execution
  and event lifecycle
- [ADR-0013](ADR-0013-built-in-tool-provider-and-branch-binding.md) — Built-in tool provider and
  Branch binding
- 0014-0015 — unused (intentional gaps)

### session-branch (0016-0020)

- [ADR-0016](ADR-0016-branch-conversation-aggregate-and-attachment-boundary.md) — Branch
  conversation aggregate and attachment boundary
- [ADR-0017](ADR-0017-session-membership-and-coordination-boundary.md) — Session membership and
  coordination boundary
- [ADR-0018](ADR-0018-turn-scoped-branch-execution-state.md) — Turn-scoped Branch execution state
- 0019-0020 — unused (intentional gaps)

### operations (0021-0026)

- [ADR-0021](ADR-0021-branch-operation-facade-and-turn-adapters.md) — Branch operation facade and
  turn-adapter contract
- [ADR-0022](ADR-0022-composed-branch-operation-pipeline.md) — Composed branch operation pipeline
- [ADR-0023](ADR-0023-dependency-aware-operation-graph-execution-kernel.md) — Dependency-aware
  operation-graph execution kernel
- [ADR-0024](ADR-0024-lndl-operate-integration-adapter.md) — LNDL operate integration adapter
- 0025-0026 — unused (intentional gaps)

### service-providers (0027-0032)

- [ADR-0027](ADR-0027-model-service-facade-and-endpoint-resolution.md) — Model-service facade and
  endpoint resolution
- [ADR-0028](ADR-0028-validated-provider-adapter-catalog.md) — Validated provider-adapter catalog
- [ADR-0029](ADR-0029-unified-request-admission-deadline-and-resilience-policy.md) — Unified
  request admission, deadline, and resilience policy
- [ADR-0030](ADR-0030-agentic-provider-adapter-boundary.md) — Agentic provider-adapter boundary
- 0031-0032 — unused (intentional gaps)

### orchestration (0033-0040)

- [ADR-0033](ADR-0033-operation-graph-orchestration-boundary.md) — Operation-graph orchestration
  boundary
- [ADR-0034](ADR-0034-domain-engine-coordination-and-autonomy-safeguards.md) — Domain-engine
  coordination and autonomy safeguards
- [ADR-0035](ADR-0035-persisted-run-completion-contract.md) — Persisted run-completion contract
- [ADR-0036](ADR-0036-casts-role-palettes-as-playstyle.md) — Casts role palettes as playstyle
- [ADR-0037](ADR-0037-resident-engine-host-and-task-queue.md) — Resident engine host and task
  queue
- [ADR-0038](ADR-0038-escalation-tier-routing.md) — Escalation tier routing
- 0039-0040 — unused (intentional gaps)

### agent-roles (0041-0046)

- [ADR-0041](ADR-0041-agent-specification-and-branch-construction.md) — Agent specification and
  Branch construction boundary
- [ADR-0042](ADR-0042-casts-pattern-catalog-and-typed-role-authoring.md) — Casts pattern catalog
  and typed role authoring
- [ADR-0043](ADR-0043-per-role-configuration-resolution.md) — Per-role configuration resolution
- [ADR-0044](ADR-0044-agent-prompt-directives-and-executable-permissions.md) — Agent prompt
  directives and executable permissions
- 0045-0046 — unused (intentional gaps)

### hooks (0047-0049)

- [ADR-0047](ADR-0047-hook-mechanism-scopes-and-canonical-ownership.md) — Hook mechanism scopes
  and canonical ownership
- [ADR-0048](ADR-0048-interoperable-external-hooks.md) — Interoperable external hooks
  (Claude Code / Codex hook contract)
- 0049 — unused (intentional gap)

### utilities (0050-0054)

- [ADR-0050](ADR-0050-foundational-utility-and-typed-adaptation-strata.md) — Foundational utility
  and typed adaptation strata
- [ADR-0051](ADR-0051-lndl-language-and-operations-boundary.md) — LNDL language and operations
  boundary
- [ADR-0052](ADR-0052-supported-validation-and-testing-surfaces.md) — Supported validation and
  testing surfaces
- 0053-0054 — unused (intentional gaps)

### persistence-state (0055-0061)

- [ADR-0055](ADR-0055-operational-state-persistence-boundary.md) — Operational state persistence
  boundary
- [ADR-0056](ADR-0056-statedb-sqlalchemy-core-backend.md) — StateDB SQLAlchemy Core backend
- [ADR-0057](ADR-0057-operational-lifecycle-and-transition-audit.md) — Operational lifecycle and
  transition audit
- [ADR-0058](ADR-0058-unified-lifecycle-transition-service.md) — Unified lifecycle transition
  service
- [ADR-0059](ADR-0059-durable-dispatch-outbox.md) — Durable dispatch outbox
- [ADR-0060](ADR-0060-run-supervision-terminal-callback-and-orphan-detection.md) — Run
  supervision: generic terminal callback and two-stage orphan detection (superseded by ADR-0095)
- 0061 — unused (intentional gap)

### cli-surface (0062-0067)

- [ADR-0062](ADR-0062-cli-command-surface-ownership.md) — CLI command-surface ownership
- [ADR-0063](ADR-0063-project-attribution-cascade.md) — Project attribution cascade
- [ADR-0064](ADR-0064-cli-execution-outcome-and-completion-record.md) — CLI execution outcome and
  completion record
- [ADR-0065](ADR-0065-marketplace-catalog-and-directory-discovery.md) — Marketplace catalog and
  directory discovery
- [ADR-0066](ADR-0066-li-mcp-v2-verb-surface.md) — `li mcp` v2 verb surface (discrete core
  plus one parser-generated dispatch verb)
- 0067 — unused (intentional gap)
- [ADR-0104](ADR-0104-li-kill-detached-play-reaping-and-terminal-notify.md) — `li kill` reaping of
  detached-play workers and terminal-notify on kill (numbering exception; see Numbering)

### scheduling-control-plane (0068-0075)

- [ADR-0068](ADR-0068-three-public-orchestration-lanes.md) — Three public orchestration lanes
- [ADR-0069](ADR-0069-reactive-flow-steering-and-recovery.md) — Reactive flow steering and
  recovery
- [ADR-0070](ADR-0070-studio-scheduling-and-dispatch-delivery.md) — Studio scheduling and dispatch
  delivery
- [ADR-0071](ADR-0071-durable-ad-hoc-task-queue.md) — Durable ad-hoc task queue
- [ADR-0072](ADR-0072-unified-task-admission-and-lifecycle.md) — Unified task admission and
  lifecycle
- [ADR-0073](ADR-0073-fixed-workflow-definition-execution.md) — Fixed workflow-definition
  execution
- 0074-0075 — unused (intentional gaps)

### studio (0076-0085)

- [ADR-0076](ADR-0076-studio-daemon-route-registry-and-local-control-plane.md) — Studio daemon
  route registry and local control plane
- [ADR-0077](ADR-0077-studio-state-and-filesystem-boundary.md) — Studio state and filesystem
  boundary
- [ADR-0078](ADR-0078-studio-application-service-boundary.md) — Studio application-service
  boundary
- [ADR-0079](ADR-0079-studio-web-client-architecture-and-deployment.md) — Studio web client
  architecture and deployment
- [ADR-0080](ADR-0080-studio-six-space-cockpit-information-architecture.md) — Studio six-space
  cockpit information architecture
- [ADR-0081](ADR-0081-studio-execution-and-artifact-workspace-target.md) — Studio execution and
  artifact workspace target
- [ADR-0082](ADR-0082-vscode-studio-observability-client.md) — VS Code Studio observability client
- [ADR-0083](ADR-0083-studio-operator-command-protocol.md) — Studio operator-command protocol
- 0084-0085 — unused (intentional gaps)

### governance (0086-0089)

- [ADR-0086](ADR-0086-local-tool-controls-and-session-authorization.md) — Local tool controls and
  session authorization observation
- [ADR-0087](ADR-0087-evidence-backed-governed-execution.md) — Evidence-backed governed execution
- 0088-0089 — unused (0088 allocated to a substrates record from this gap; see below)

### substrates (0090-0095)

- [ADR-0090](ADR-0090-local-sandbox-and-measured-cell-backend-seams.md) — Local sandbox and
  measured-cell backend seams
- [ADR-0091](ADR-0091-per-worker-worktree-execution-isolation.md) — Per-worker worktree execution
  isolation
- [ADR-0092](ADR-0092-minimal-branch-and-session-memory-store.md) — Minimal Branch and session
  memory store
- [ADR-0093](ADR-0093-external-memory-adapter-fidelity-contract.md) — External memory adapter
  fidelity contract
- [ADR-0094](ADR-0094-automated-pr-review-pipeline.md) — Automated PR-review pipeline over
  github-poll schedules (area: scheduling-control-plane; numbering exception)
- [ADR-0095](ADR-0095-run-terminal-callbacks-and-orphan-recovery.md) — Run-terminal
  callbacks and orphan recovery (area: scheduling-control-plane; numbering exception;
  supersedes ADR-0060)
- [ADR-0088](ADR-0088-plugin-system.md) — Plugin system (directory-bundle manifest with
  lazy activation; number from the adjacent free gap — the substrates block is exhausted)

Remaining areas land here as their records are accepted.

Status-set literals quoted in these records (terminal sets, valid-status vocabularies) are
checked against the lifecycle policy registry in CI (`scripts/check_adr_status_sets.py`);
a registry change that stale-ifies a quoted set fails the docs job.
