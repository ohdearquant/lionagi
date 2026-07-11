# Architecture Decision Records (v0 archive)

This directory is a frozen, historical corpus. It is **not** the current ADR record —
the canonical, actively maintained ADR corpus lives at [`docs/adr/`](../../adr/README.md).
These files were moved here intact, original filenames and numbering kept, when the
corpus was restructured.

**The authoritative v0 → current mapping is [`docs/adr/dispositions.yaml`](../../adr/dispositions.yaml)**,
one row per archived record, recording whether each v0 decision was carried forward as a
named current ADR, merged into one alongside other v0 records, or retired outright. Start
there, not here, if you're trying to find where a v0 decision lives today.

Nothing in this directory is edited going forward, beyond one-time hygiene repairs such as annotating references to never-published numbers. Where content below describes v0-era
conventions (naming, lifecycle, status taxonomy), it is preserved as a historical record of
how the v0 corpus operated, not as guidance for new work — see
[`docs/adr/README.md`](../../adr/README.md) for the conventions actually in effect.

This directory also retains `DECISION_LOG.md`, `glossary.md`, `TEMPLATE.md`, and
`reader's-guide.md` from the v0 corpus, kept for historical reference alongside the ADR files.

## Naming and Location (historical)

Files in this archive follow the pattern:

```text
ADR-NNNN-kebab-case-slug.md
```

`NNNN` is a 4-digit zero-padded integer assigned sequentially (0001, 0002, …). At the time
this corpus was frozen, the files lived at `docs/adrs/`; they have since been relocated to
`docs/_archive/v0/` without renaming.

## Lifecycle (historical)

```text
Proposed → Accepted → Phase-N Shipped → Superseded (→ Deprecated, optional)
```

## Status Taxonomy (historical)

An ADR's status answered two distinct questions: *is the decision ratified?* and
*how much of it is built?* A design could be ratified but unbuilt, or built ahead
of formal ratification.

| Status | Meaning | Implementation |
|--------|---------|----------------|
| **Proposed** | Design draft, under discussion. Not yet ratified. | None started. |
| **Accepted** | Design locked and in effect. Phase N may be planned. | None-to-partial; the decision governs new code. |
| **Phase N Shipped** | Implementation through phase N has landed on `main`. | Phases 1..N shipped; later phases may remain planned. |
| **Superseded by ADR-NNNN** | Replaced by a newer decision. Record kept for history. | Frozen; see the superseding ADR. |
| **Deprecated** | No longer relevant; not replaced by a newer decision. | Abandoned or removed. |

Notes:

- **Amended (...)** is a free-form annotation a few early ADRs use to note a later revision
  that did not warrant a new ADR number; treat it as a flavour of Accepted.
- `Superseded by ADR-NNNN` and any other in-file cross-reference to an `ADR-NNNN` number
  refers to the **v0 namespace** (another file in this same directory), never to the current
  `docs/adr/` corpus, which restarted its own numbering.

## Skipped Numbers

Numbers 0036–0038, 0040, and 0073 were reserved for future use and have no corresponding
files in this archive.

## Index

All 98 archived ADR files (0001–0103, with the gaps above). Title is taken from each file's
top-level heading.

| ADR | Title |
|-----|-------|
| [ADR-0001](ADR-0001-lion-studio-internal-app.md) | lion-studio as Internal Monorepo App |
| [ADR-0002](ADR-0002-studio-tech-stack.md) | Lion Studio Tech Stack |
| [ADR-0003](ADR-0003-claude-code-marketplace.md) | Claude Code Marketplace |
| [ADR-0004](ADR-0004-filesystem-data-layer.md) | Data Layer — Filesystem + SQLite Hybrid |
| [ADR-0005](ADR-0005-workers-playbooks-rename.md) | Playbooks Naming Convention |
| [ADR-0006](ADR-0006-sse-live-streaming.md) | Live Update Transport — SSE + Interval Refresh |
| [ADR-0007](ADR-0007-plugin-auto-discovery.md) | Plugin Manifest Layout and Auto-Discovery Convention |
| [ADR-0008](ADR-0008-studio-v1-scope.md) | Lion Studio Scope — CLI-Primary, Definition-Editable, Localhost |
| [ADR-0009](ADR-0009-sqlite-state-layer.md) | SQLite State Layer for Core Data Model |
| [ADR-0010](ADR-0010-plugin-aware-studio.md) | Plugin-Aware Studio UI |
| [ADR-0011](ADR-0011-shows-data-model.md) | Shows Data Model — Hybrid SQLite + Filesystem |
| [ADR-0012](ADR-0012-studio-execution-lineage.md) | Studio Execution Lineage & UX Redesign |
| [ADR-0013](ADR-0013-zero-dependency-ui.md) | Zero Component-Library UI |
| [ADR-0014](ADR-0014-cli-primary-studio-secondary.md) | CLI-Primary, Studio-Secondary |
| [ADR-0015](ADR-0015-runs-list-design.md) | Runs List Design — Identity, Filters, Pagination |
| [ADR-0016](ADR-0016-definitions-write-path.md) | Definition Write Path and Versioning |
| [ADR-0017](ADR-0017-session-lifecycle-status.md) | Session Lifecycle and Status Derivation |
| [ADR-0018](ADR-0018-studio-distribution.md) | Studio Distribution and Local Access |
| [ADR-0019](ADR-0019-teams-db-and-run-lifecycle.md) | Teams DB Migration and Run Lifecycle Management |
| [ADR-0020](ADR-0020-skill-invocations.md) | Skill Invocations — Tracking the Orchestration Layer |
| [ADR-0021](ADR-0021-skill-artifacts-and-reactive-chaining.md) | Skill Artifacts, Structured Output, and Reactive Chaining |
| [ADR-0022](ADR-0022-run-step-provenance.md) | Run Step Provenance — Model, Agent, and Provider Disclosure |
| [ADR-0023](ADR-0023-unified-hook-system.md) | Unified Hook System and Agent-Level Configuration |
| [ADR-0024](ADR-0024-session-health-and-admin-surface.md) | Session Health Classification and Admin Surface |
| [ADR-0025](ADR-0025-session-status-vocabulary.md) | Session Status Vocabulary |
| [ADR-0026](ADR-0026-project-detection.md) | Project Detection for Session Organization |
| [ADR-0027](ADR-0027-scheduled-runs.md) | Scheduled Runs and Event-Triggered Invocations |
| [ADR-0028](ADR-0028-status-reason-model.md) | Status Reason Model |
| [ADR-0029](ADR-0029-artifact-contract.md) | Artifact Contract |
| [ADR-0030](ADR-0030-attention-queue.md) | Attention Queue |
| [ADR-0031](ADR-0031-entity-header-pattern.md) | Entity Header Pattern |
| [ADR-0032](ADR-0032-navigation-reorganization.md) | Navigation Reorganization |
| [ADR-0033](ADR-0033-unified-entity-state-model.md) | Unified Entity State Model |
| [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md) | Frontend Data & State Architecture |
| [ADR-0035](ADR-0035-design-system-and-component-library.md) | Design System and Component Library |
| [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) | Knowledge Substrate Minimal Interface |
| [ADR-0041](ADR-0041-immutable-evidence-nodes.md) | Immutable Evidence Nodes |
| [ADR-0042](ADR-0042-task-certificate.md) | Task Certificate — Signed Proof of Process Adherence |
| [ADR-0043](ADR-0043-governed-tool-declaration.md) | Governed Tool Declaration |
| [ADR-0044](ADR-0044-tool-gates.md) | Tool Gates — Three-Tier Binary Enforcement |
| [ADR-0045](ADR-0045-break-glass-protocol.md) | Break-Glass Protocol — DEGRADED-Defensibility Override |
| [ADR-0046](ADR-0046-jit-tool-grant.md) | JIT Tool Grant — No Standing Capability for High-Risk Tools |
| [ADR-0047](ADR-0047-agent-charter.md) | Agent Charter — Enforceable Governance Document |
| [ADR-0048](ADR-0048-agent-segregation-of-duties.md) | Agent Segregation of Duties (SoD) |
| [ADR-0049](ADR-0049-log-tier-governance.md) | Log Tier Governance |
| [ADR-0050](ADR-0050-operation-context.md) | Operation Context — Active Assertion in Evidence |
| [ADR-0051](ADR-0051-tool-registry-allowlists.md) | Tool Registry Allowlists |
| [ADR-0052](ADR-0052-policy-resolution.md) | Policy Resolution and Staged Release |
| [ADR-0053](ADR-0053-artifact-persistence.md) | Artifact Persistence in State Database |
| [ADR-0054](ADR-0054-local-state-cleanup.md) | Local State File Cleanup and DB Migration Completion |
| [ADR-0055](ADR-0055-studio-artifact-viewer.md) | Studio Artifact Viewer and File Reference Resolution |
| [ADR-0056](ADR-0056-play-control-api.md) | Play Control API - Runner Control Plane |
| [ADR-0057](ADR-0057-remote-sandbox-execution.md) | Remote Sandbox Execution Behind PlayRunner |
| [ADR-0058](ADR-0058-play-cost-tracking.md) | Play Cost Tracking |
| [ADR-0059](ADR-0059-postgres-state-backend.md) | Postgres State Backend |
| [ADR-0060](ADR-0060-unified-config-resolution.md) | Unified Config Resolution |
| [ADR-0061](ADR-0061-universal-scheduler.md) | Universal Scheduler - `li schedule` for Any Flow |
| [ADR-0062](ADR-0062-state-machine-spec.md) | Scheduled Item State Machine |
| [ADR-0063](ADR-0063-task-board-work-center.md) | Task Board - Operator Work Center for Lion Studio |
| [ADR-0064](ADR-0064-work-system-integration.md) | Work System Integration |
| [ADR-0065](ADR-0065-task-board-schema.md) | Task Board Schema |
| [ADR-0066](ADR-0066-unified-execution-viewer.md) | Unified Execution Viewer |
| [ADR-0067](ADR-0067-studio-command-chat.md) | Studio Command Chat - Universal AI-Powered Control Panel |
| [ADR-0068](ADR-0068-governed-adapter-protocol.md) | Zero-Rewrite Governed Adapter Protocol |
| [ADR-0069](ADR-0069-tenant-scope-boundary.md) | Tenant Scope Boundary |
| [ADR-0070](ADR-0070-governance-tracing.md) | Governance Tracing and Observability |
| [ADR-0071](ADR-0071-cognitive-mode-model.md) | Cognitive Mode Model |
| [ADR-0072](ADR-0072-reactive-capability-bus.md) | Reactive Capability Bus |
| [ADR-0074](ADR-0074-role-composition-and-pack-config.md) | Role Composition & Pack-Based Per-Role Configuration |
| [ADR-0075](ADR-0075-domain-specific-engines.md) | Domain-Specific Agent Engines |
| [ADR-0076](ADR-0076-observer-as-hook-transport.md) | Observer as the Canonical Hook Transport |
| [ADR-0077](ADR-0077-engine-autonomy-protections.md) | Engine Autonomy Protections and the Hypothesis Engine |
| [ADR-0078](ADR-0078-casts-conceptual-model-and-module-coherence.md) | The Casts Conceptual Model and Module Coherence |
| [ADR-0079](ADR-0079-substrate-executor-provider-interface.md) | Substrate Executor Provider Interface |
| [ADR-0080](ADR-0080-remote-sandbox-substrate-execution.md) | Remote Sandbox Substrate Execution |
| [ADR-0081](ADR-0081-configurable-flow-planning.md) | Configurable Flow Planning |
| [ADR-0082](ADR-0082-role-substrate-routing-policy.md) | Role to Substrate Routing Policy |
| [ADR-0083](ADR-0083-lifecycle-signal-contract.md) | Canonical Per-Node Lifecycle Signal Contract |
| [ADR-0084](ADR-0084-vscode-extension-native-studio-client.md) | VS Code Extension as a Native Client over the Studio API |
| [ADR-0085](ADR-0085-flow-control-plane.md) | Flow Control Plane — Pause/Resume, Message Injection, Checkpoint Resume, Status, Usage, Fallback |
| [ADR-0086](ADR-0086-statedb-sqlalchemy-core-backend-unification.md) | StateDB SQLAlchemy-Core backend unification |
| [ADR-0087](ADR-0087-lndl-operate-seam.md) | LNDL operate() Seam, Scratchpad-as-Tool, and the Measurement Gate |
| [ADR-0088](ADR-0088-flow-steering-mechanisms.md) | Flow-Steering Control-Plane Mechanisms |
| [ADR-0089](ADR-0089-sandbox-backend-seam-and-measurement-loop.md) | Sandbox Backend Seam and Recursive Measurement Loop |
| [ADR-0090](ADR-0090-minimal-memory-contract-and-backend-seam.md) | Minimal Memory Contract and Pluggable Backend Seam |
| [ADR-0091](ADR-0091-khive-pluggable-memorystore-backend.md) | khive as a Pluggable lionagi MemoryStore Backend |
| [ADR-0092](ADR-0092-durable-dispatch-outbox-and-named-resource-gates.md) | Durable Dispatch Outbox and Named Resource Gates |
| [ADR-0093](ADR-0093-studio-three-surface-ia.md) | Studio Three-Surface Information Architecture |
| [ADR-0094](ADR-0094-run-completion-contract.md) | Run Completion Contract and Machine-Consumable Orchestration |
| [ADR-0095](ADR-0095-reactive-spawn-observability-and-dx.md) | Reactive Spawn Observability and Orchestration DX |
| [ADR-0096](ADR-0096-engine-result-and-degradation-contract.md) | Engine Result & Degradation Contract |
| [ADR-0097](ADR-0097-ci-gate-taxonomy.md) | CI Gate Taxonomy And Fail-Closed Aggregation |
| [ADR-0098](ADR-0098-resident-engine-work-queue.md) | Resident Engine Work Queue (Warm Host Loop) |
| [ADR-0099](ADR-0099-escalation-node-builder-tier-bump.md) | Escalation node_builder Tier Bump |
| [ADR-0100](ADR-0100-context-injection-providers.md) | Pre-Turn Context Injection Providers |
| [ADR-0101](ADR-0101-task-application-queue.md) | Task-Application Surface & Durable Queue |
| [ADR-0102](ADR-0102-workflow-library-registry.md) | Workflow Library Registry |
| [ADR-0103](ADR-0103-fixed-flow-workflow-v11-contract.md) | Fixed-flow workflow engine v1.1 contract (li flow run, per-node cwd, per-node model, artifact_dir) |

See also [`DECISION_LOG.md`](DECISION_LOG.md) for the lightweight decisions this corpus
recorded outside of full ADRs, and [`docs/adr/dispositions.yaml`](../../adr/dispositions.yaml)
for what each of these records became.
