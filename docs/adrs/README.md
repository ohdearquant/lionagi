# Architecture Decision Records

ADRs capture significant decisions that affect lionagi's public API, dependencies, architecture
layout, or distribution model, or process patterns that future contributors should follow.

New readers should start with [reader's-guide.md](reader's-guide.md) for a navigated entry point
into the ADR set.

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

### Foundation (0001–0010): Studio architecture, tech stack, data layer

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0001](ADR-0001-lion-studio-internal-app.md) | Lion Studio as an internal monorepo app, not a separate repo | Accepted |
| [ADR-0002](ADR-0002-studio-tech-stack.md) | Lion Studio tech stack (Next.js, FastAPI, SQLite) | Accepted |
| [ADR-0003](ADR-0003-claude-code-marketplace.md) | Claude Code marketplace integration and v2 catalog design | Amended (v2 catalog) |
| [ADR-0004](ADR-0004-filesystem-data-layer.md) | Filesystem + SQLite hybrid as the primary data layer | Accepted |
| [ADR-0005](ADR-0005-workers-playbooks-rename.md) | Rename "workers/playbooks" to the canonical "skills" vocabulary | Accepted |
| [ADR-0006](ADR-0006-sse-live-streaming.md) | SSE + interval refresh as the live-update transport | Accepted |
| [ADR-0007](ADR-0007-plugin-auto-discovery.md) | Plugin manifest auto-discovery convention and resolution rules | Accepted |
| [ADR-0008](ADR-0008-studio-v1-scope.md) | Studio v1 scope: CLI-primary, definition-editable, localhost-only | Accepted |
| [ADR-0009](ADR-0009-sqlite-state-layer.md) | SQLite as the authoritative state layer for core runtime data | Accepted |
| [ADR-0010](ADR-0010-plugin-aware-studio.md) | Plugin-aware Studio UI with per-plugin route registration | Accepted |

### Operational primitives (0011–0023): shows, teams, hooks, lineage

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0011](ADR-0011-shows-data-model.md) | Shows data model: hybrid SQLite metadata + filesystem artifacts | Accepted |
| [ADR-0012](ADR-0012-studio-execution-lineage.md) | Studio execution lineage tree and UX redesign | Accepted |
| [ADR-0013](ADR-0013-zero-dependency-ui.md) | Zero component-library UI (hand-rolled Tailwind only) | Superseded by [ADR-0035](ADR-0035-design-system-and-component-library.md) |
| [ADR-0014](ADR-0014-cli-primary-studio-secondary.md) | CLI is the primary interface; Studio is a secondary observer | Accepted |
| [ADR-0015](ADR-0015-runs-list-design.md) | Runs list design: identity columns, filters, and pagination | Accepted |
| [ADR-0016](ADR-0016-definitions-write-path.md) | Definition write path, versioning, and rollback semantics | Accepted |
| [ADR-0017](ADR-0017-session-lifecycle-status.md) | Session lifecycle and status derivation rules | Partially superseded by [ADR-0033](ADR-0033-unified-entity-state-model.md) |
| [ADR-0018](ADR-0018-studio-distribution.md) | Studio distribution model and local-access packaging | Accepted |
| [ADR-0019](ADR-0019-teams-db-and-run-lifecycle.md) | Teams DB schema and per-run lifecycle state machine | Accepted |
| [ADR-0020](ADR-0020-skill-invocations.md) | Skill invocation contract: request/response envelope and routing | Accepted |
| [ADR-0021](ADR-0021-skill-artifacts-and-reactive-chaining.md) | Skill artifact model and reactive chaining between skills | Accepted |
| [ADR-0022](ADR-0022-run-step-provenance.md) | Run-step provenance: linking output artifacts back to source steps | Accepted |
| [ADR-0023](ADR-0023-unified-hook-system.md) | Unified hook system for pre/post tool-call interception | Accepted |

### State refinement (0024–0032): health, vocab, reasons, attention, navigation

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0024](ADR-0024-session-health-and-admin-surface.md) | Session health signals and admin surface for operator visibility | Extended by [ADR-0033](ADR-0033-unified-entity-state-model.md) |
| [ADR-0025](ADR-0025-session-status-vocabulary.md) | Session status vocabulary (running, completed, failed, …) | Superseded by [ADR-0033](ADR-0033-unified-entity-state-model.md) |
| [ADR-0026](ADR-0026-project-detection.md) | Project detection cascade for CLI session context | Accepted |
| [ADR-0027](ADR-0027-scheduled-runs.md) | Scheduled runs: cron/interval triggers and missed-fire policy | Accepted |
| [ADR-0028](ADR-0028-status-reason-model.md) | Status reason model: attaching structured reasons to state transitions | Extended by [ADR-0033](ADR-0033-unified-entity-state-model.md) |
| [ADR-0029](ADR-0029-artifact-contract.md) | Artifact contract: storage, MIME types, and lifecycle guarantees | Extended by [ADR-0033](ADR-0033-unified-entity-state-model.md) |
| [ADR-0030](ADR-0030-attention-queue.md) | Attention queue: surfacing entities that need operator action | Extended by [ADR-0033](ADR-0033-unified-entity-state-model.md) / [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) |
| [ADR-0031](ADR-0031-entity-header-pattern.md) | Entity header pattern: canonical top-of-page summary block | Accepted |
| [ADR-0032](ADR-0032-navigation-reorganization.md) | Navigation reorganization: sidebar hierarchy and route ownership | Accepted |

### KHive product evolution (0033–0035, 0039): unified state, frontend, components, knowledge

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0033](ADR-0033-unified-entity-state-model.md) | Unified Entity State Model: single authoritative state semantic across all entity types | Accepted |
| [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md) | Frontend data and state architecture: TanStack Query, SSE, and URL-as-state | Accepted |
| [ADR-0035](ADR-0035-design-system-and-component-library.md) | Design system and component library: shadcn/ui + Radix primitives | Accepted |
| [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) | Knowledge substrate minimal interface: claims with evidence | Accepted |

See also [Decision Log](DECISION_LOG.md) for lightweight decisions that don't warrant a full ADR.
