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

Remaining areas land here as their records are accepted.
