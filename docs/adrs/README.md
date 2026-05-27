# Architecture Decision Records

ADRs capture significant decisions that affect lionagi's public API, dependencies, architecture
layout, distribution model, or process patterns that future contributors should follow.

## Naming and Location

Files live in `docs/adrs/` and follow the pattern:

```text
ADR-NNNN-kebab-case-slug.md
```

`NNNN` is a 4-digit zero-padded integer assigned sequentially (0001, 0002, …).

## Lifecycle

```text
Proposed → Accepted → Superseded (→ Deprecated, optional)
```

- **Proposed**: decision drafted, not yet ratified.
- **Accepted**: decision is in effect.
- **Superseded by ADR-NNNN**: replaced; the old record stays for history.
- **Deprecated**: no longer relevant; not superseded by a newer decision.

## When to Write an ADR

Write one when the decision affects any of:

- Public API surface (`from lionagi import …` or CLI flags)
- New required or optional package dependencies
- Repository/architecture layout (new top-level directories, package boundaries)
- Distribution model (PyPI extras, install entrypoints, versioning policy)
- Process patterns future contributors must follow (test strategy, branching model)

## When NOT to Write an ADR

Skip it for:

- Internal implementation changes that don't alter public contracts
- Refactors that preserve existing behaviour and signatures
- Dependency version bumps (unless changing a major boundary)

## Cross-Referencing

When a new ADR supersedes an old one, set the old ADR's status line to
`Superseded by ADR-NNNN` and reference the old ADR in the new one's body.
Example: `_Supersedes [ADR-0001](ADR-0001-lion-studio-internal-app.md)._`

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0001](ADR-0001-lion-studio-internal-app.md) | Lion Studio as internal monorepo app | Accepted |
| [ADR-0002](ADR-0002-studio-tech-stack.md) | Lion Studio tech stack | Accepted |
| [ADR-0003](ADR-0003-claude-code-marketplace.md) | Claude Code marketplace | Amended (v2 catalog) |
| [ADR-0004](ADR-0004-filesystem-data-layer.md) | Data layer — filesystem + SQLite hybrid | Accepted |
| [ADR-0005](ADR-0005-workers-playbooks-rename.md) | Playbooks naming convention | Accepted |
| [ADR-0006](ADR-0006-sse-live-streaming.md) | Live update transport — SSE + interval refresh | Accepted |
| [ADR-0007](ADR-0007-plugin-auto-discovery.md) | Plugin manifest auto-discovery convention | Accepted |
| [ADR-0008](ADR-0008-studio-v1-scope.md) | Studio scope — CLI-primary, definition-editable, localhost | Accepted |
| [ADR-0009](ADR-0009-sqlite-state-layer.md) | SQLite state layer for core data model | Accepted |
| [ADR-0010](ADR-0010-plugin-aware-studio.md) | Plugin-aware Studio UI | Accepted |
| [ADR-0011](ADR-0011-shows-data-model.md) | Shows data model — hybrid SQLite + filesystem | Accepted |
| [ADR-0012](ADR-0012-studio-execution-lineage.md) | Studio execution lineage and UX redesign | Accepted |
| [ADR-0013](ADR-0013-zero-dependency-ui.md) | Zero component-library UI | Accepted |
| [ADR-0014](ADR-0014-cli-primary-studio-secondary.md) | CLI-primary, Studio-secondary | Accepted |
| [ADR-0015](ADR-0015-runs-list-design.md) | Runs list design — identity, filters, pagination | Accepted |
| [ADR-0016](ADR-0016-definitions-write-path.md) | Definition write path and versioning | Accepted |
| [ADR-0017](ADR-0017-session-lifecycle-status.md) | Session lifecycle and status derivation | Accepted |
| [ADR-0018](ADR-0018-studio-distribution.md) | Studio distribution and local access | Accepted |
| [ADR-0019](ADR-0019-teams-db-and-run-lifecycle.md) | Teams DB migration and run lifecycle management | Proposed |
| [ADR-0020](ADR-0020-skill-invocations.md) | Skill invocations — tracking the orchestration layer | Proposed |
| [ADR-0021](ADR-0021-skill-artifacts-and-reactive-chaining.md) | Skill artifacts, structured output, and reactive chaining | Proposed |
| [ADR-0022](ADR-0022-run-step-provenance.md) | Run step provenance — model, agent, and provider disclosure | Proposed |
| [ADR-0023](ADR-0023-unified-hook-system.md) | Unified hook system and agent-level configuration | Proposed |
| [ADR-0024](ADR-0024-session-health-and-admin-surface.md) | Session health classification and admin surface | Proposed |
| [ADR-0025](ADR-0025-session-status-vocabulary.md) | Session status vocabulary | Proposed |
| [ADR-0026](ADR-0026-project-detection.md) | Project detection for session organization | Accepted |
| [ADR-0027](ADR-0027-scheduled-runs.md) | Scheduled runs and event-triggered invocations | Proposed |
| [ADR-0028](ADR-0028-status-reason-model.md) | Status reason model | Proposed |
| [ADR-0029](ADR-0029-artifact-contract.md) | Artifact contract | Proposed |
| [ADR-0030](ADR-0030-attention-queue.md) | Attention queue | Proposed |
| [ADR-0031](ADR-0031-entity-header-pattern.md) | Entity header pattern | Proposed |
| [ADR-0032](ADR-0032-navigation-reorganization.md) | Navigation reorganization | Proposed |
| [ADR-0033](ADR-0033-unified-entity-state-model.md) | Unified entity state model | Proposed |
| [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md) | Frontend data and state architecture | Proposed |
| [ADR-0035](ADR-0035-design-system-and-component-library.md) | Design system and component library | Proposed |
| [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) | Knowledge substrate minimal interface | Proposed |
| [ADR-0041](ADR-0041-immutable-evidence-nodes.md) | Immutable evidence nodes | Proposed |
| [ADR-0042](ADR-0042-task-certificate.md) | Task certificate — signed proof of process adherence | Proposed |
| [ADR-0043](ADR-0043-governed-tool-declaration.md) | Governed tool declaration | Proposed |
| [ADR-0044](ADR-0044-tool-gates.md) | Tool gates — three-tier binary enforcement | Proposed |
| [ADR-0045](ADR-0045-break-glass-protocol.md) | Break-glass protocol — DEGRADED-defensibility override | Proposed |
| [ADR-0046](ADR-0046-jit-tool-grant.md) | JIT tool grant — no standing capability for high-risk tools | Proposed |
| [ADR-0047](ADR-0047-agent-charter.md) | Agent charter — enforceable governance document | Proposed |
| [ADR-0048](ADR-0048-agent-segregation-of-duties.md) | Agent segregation of duties | Proposed |
| [ADR-0049](ADR-0049-log-tier-governance.md) | Log tier governance | Proposed |
| [ADR-0050](ADR-0050-operation-context.md) | Operation context — active assertion in evidence | Proposed |
| [ADR-0051](ADR-0051-tool-registry-allowlists.md) | Tool registry allowlists | Proposed |
| [ADR-0052](ADR-0052-policy-resolution.md) | Policy resolution and staged release | Proposed |
| [ADR-0053](ADR-0053-artifact-persistence.md) | Artifact persistence in state database | Proposed |
| [ADR-0054](ADR-0054-local-state-cleanup.md) | Local state file cleanup and DB migration completion | Proposed |
| [ADR-0055](ADR-0055-studio-artifact-viewer.md) | Studio artifact viewer and file reference resolution | Proposed |
| [ADR-0056](ADR-0056-play-control-api.md) | Play control API — runner control plane | Proposed |
| [ADR-0057](ADR-0057-remote-sandbox-execution.md) | Remote sandbox execution behind PlayRunner | Proposed |
| [ADR-0058](ADR-0058-play-cost-tracking.md) | Play cost tracking | Proposed |
| [ADR-0059](ADR-0059-postgres-state-backend.md) | Postgres state backend | Proposed |
| [ADR-0060](ADR-0060-unified-config-resolution.md) | Unified config resolution | Proposed |
| [ADR-0061](ADR-0061-universal-scheduler.md) | Universal scheduler — `li schedule` for any flow | Proposed |
| [ADR-0062](ADR-0062-state-machine-spec.md) | Scheduled item state machine | Proposed |
| [ADR-0063](ADR-0063-task-board-work-center.md) | Task board — operator work center for Lion Studio | Proposed |
| [ADR-0064](ADR-0064-work-system-integration.md) | Work system integration | Accepted |
| [ADR-0065](ADR-0065-task-board-schema.md) | Task board schema | Proposed |
| [ADR-0066](ADR-0066-unified-execution-viewer.md) | Unified execution viewer | Proposed |
| [ADR-0067](ADR-0067-studio-command-chat.md) | Studio command chat — universal AI-powered control panel | Proposed |
| [ADR-0068](ADR-0068-governed-adapter-protocol.md) | Zero-rewrite governed adapter protocol | Accepted |
| [ADR-0069](ADR-0069-tenant-scope-boundary.md) | Tenant scope boundary | Accepted |
| [ADR-0070](ADR-0070-governance-tracing.md) | Governance tracing and observability | Accepted |

See also [Decision Log](DECISION_LOG.md) for lightweight decisions that don't warrant a full ADR.
