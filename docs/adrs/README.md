# Architecture Decision Records

ADRs capture significant decisions that affect lionagi's public API, dependencies, architecture
layout, distribution model, or process patterns that future contributors should follow.

## Naming and Location

Files live in `docs/adrs/` and follow the pattern:

```
ADR-NNNN-kebab-case-slug.md
```

`NNNN` is a 4-digit zero-padded integer assigned sequentially (0001, 0002, …).

## Lifecycle

```
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
| [ADR-0003](ADR-0003-claude-code-marketplace.md) | Claude Code marketplace | Accepted |
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

See also [Decision Log](DECISION_LOG.md) for lightweight decisions that don't warrant a full ADR.
