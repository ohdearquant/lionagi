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
| [ADR-0001](ADR-0001-lion-studio-internal-app.md) | lion-studio as internal monorepo app | Accepted |
| [ADR-0002](ADR-0002-studio-tech-stack.md) | Lion Studio tech stack | Accepted |
| [ADR-0003](ADR-0003-claude-code-marketplace.md) | Claude Code marketplace | Accepted |
| [ADR-0004](ADR-0004-filesystem-data-layer.md) | Filesystem-backed data layer | Accepted |
| [ADR-0005](ADR-0005-workers-playbooks-rename.md) | Workers-to-playbooks rename strategy | Accepted |
| [ADR-0006](ADR-0006-sse-live-streaming.md) | SSE for live streaming | Accepted |
| [ADR-0007](ADR-0007-plugin-auto-discovery.md) | Plugin manifest auto-discovery convention | Accepted |
| [ADR-0008](ADR-0008-studio-v1-scope.md) | Lion Studio v1 scope | Accepted |
