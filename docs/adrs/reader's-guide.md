# ADR Reader's Guide

> A map for navigating the ADR set. Read this first.

## What is this directory?

Each file here is an Architecture Decision Record — a short document that captures a significant
design choice, the context that forced it, the alternatives considered, and the consequences
accepted. ADRs are the authoritative record of why the system looks the way it does. They are
not design proposals or tutorials; they are binding decisions that have already been made and are
in effect unless marked Superseded.

## Where to start: by your role

### If you're new to Lion Studio / KHive

Read in this order to build a mental model of the system from the ground up:

1. [ADR-0001](ADR-0001-lion-studio-internal-app.md) — why Studio lives inside the monorepo
2. [ADR-0002](ADR-0002-studio-tech-stack.md) — the technology choices (Next.js, FastAPI, SQLite)
3. [ADR-0009](ADR-0009-sqlite-state-layer.md) — why SQLite is the authoritative runtime store
4. [ADR-0014](ADR-0014-cli-primary-studio-secondary.md) — the CLI/Studio relationship; Studio observes, CLI drives
5. [ADR-0033](ADR-0033-unified-entity-state-model.md) — the current unified state model that governs all entity types

### If you're working on backend state semantics

Start with the foundation, then follow the refinement chain:

1. [ADR-0033](ADR-0033-unified-entity-state-model.md) — the unified state model (read this first; it consolidates and supersedes several predecessors)
2. [ADR-0028](ADR-0028-status-reason-model.md) — how reasons attach to state transitions
3. [ADR-0029](ADR-0029-artifact-contract.md) — the artifact lifecycle and its tie-in to state
4. [ADR-0024](ADR-0024-session-health-and-admin-surface.md) — health signals that derive from entity state
5. [ADR-0017](ADR-0017-session-lifecycle-status.md) — the original session lifecycle (partially superseded; read for history)

### If you're working on frontend

1. [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md) — TanStack Query, SSE subscription, and URL-as-state contract
2. [ADR-0035](ADR-0035-design-system-and-component-library.md) — the component library decision (shadcn/Radix; supersedes ADR-0013)
3. [ADR-0033](ADR-0033-unified-entity-state-model.md) — the NormalizedState shape that all frontend components consume
4. [ADR-0031](ADR-0031-entity-header-pattern.md) — the entity header block used on every detail page
5. [ADR-0032](ADR-0032-navigation-reorganization.md) — sidebar hierarchy and route ownership

### If you're working on knowledge / evidence

1. [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) — the minimal claims-with-evidence interface
2. [ADR-0033](ADR-0033-unified-entity-state-model.md) — how knowledge entities participate in the unified state model
3. [ADR-0029](ADR-0029-artifact-contract.md) — artifact storage, which is how evidence payloads are stored
4. [ADR-0030](ADR-0030-attention-queue.md) — how claims surface in the attention queue when they need review

### If you're working on the data layer

1. [ADR-0009](ADR-0009-sqlite-state-layer.md) — SQLite as the canonical state store
2. [ADR-0033](ADR-0033-unified-entity-state-model.md) — the schema contract all tables must conform to
3. [ADR-0011](ADR-0011-shows-data-model.md) — the shows model (SQLite metadata + filesystem artifacts hybrid)
4. [ADR-0019](ADR-0019-teams-db-and-run-lifecycle.md) — teams DB schema and per-run lifecycle
5. [ADR-0004](ADR-0004-filesystem-data-layer.md) — the original filesystem + SQLite layering decision

## How to read an ADR

Each ADR follows the same canonical structure. Here is what to look for in each section:

- **Context**: The situation that forced a decision. Read this to understand what constraints
  existed at the time — some may no longer apply, but they explain why the decision took the shape
  it did.
- **Decision**: The choice made. This is the binding commitment. If you are implementing something
  in this area, the Decision section is what you must honour.
- **Consequences**: What the decision enables, what it forecloses, and what technical debt it
  accepts. The negative consequences are as important as the positive ones.
- **Alternatives considered**: The options that were evaluated and rejected, with the reasons. Read
  this before proposing a change — the rejected alternatives have usually been tried or thought
  through already.

## Cross-reference legend

ADR files use a small set of relationship headers to connect decisions:

- **Supersedes**: The referenced ADR is no longer in effect for the domain covered here. The old
  ADR is retained for history but should not be followed as current policy.
- **Extends**: The referenced ADR remains in effect; this ADR adds new rules on top of it without
  replacing any of the original decisions.
- **Related**: The referenced ADR covers overlapping ground without a strict supersession or
  extension relationship. Read both to understand the full picture.
- **Depends on**: This ADR's decision only makes sense given the referenced ADR's decision. If the
  dependency is ever reconsidered, this ADR must be revisited.

The [README.md](README.md) index notes these relationships inline next to each entry.

## The evidence chain (the unifying principle)

Starting with [ADR-0028](ADR-0028-status-reason-model.md) and consolidated in
[ADR-0033](ADR-0033-unified-entity-state-model.md), a single principle runs through the newer
ADRs: every state transition must carry a structured reason, and every reason that is non-trivial
must carry evidence.

This chain looks like: an entity moves to a new lifecycle state → the transition records a
`StateReason` (machine-readable code + human-readable message) → where the reason is based on an
observation, it links to an `EvidenceRef` (a stored artifact or an external claim) →
[ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) generalises this pattern into the
knowledge substrate, where claims carry confidence scores and evidence kinds independently of
whether they are attached to a state transition.

The attention queue ([ADR-0030](ADR-0030-attention-queue.md)) is the operational surface of this
chain: entities whose reasons are unresolved or whose evidence is stale surface as items requiring
operator action.

Understanding this chain is the fastest route to understanding why the newer ADRs look the way
they do. It is not just an audit trail — it is the mechanism by which the system makes its own
state legible to both humans and downstream agents.

## When to write a new ADR

Write a new ADR when:

- You are changing a public API surface, CLI flag, or schema contract that other code or humans
  depend on.
- You are introducing a new architectural boundary — a new top-level module, a new persistence
  layer, a new transport — that future implementers will need to understand.
- You are rejecting an apparently obvious approach in favour of a less obvious one; the reasoning
  needs to survive the next contributor's first instinct.
- You are making a decision that, if made differently later, would require significant migration
  work — record the cost of reversal explicitly.

Do not write an ADR for internal implementation choices that have no observable effect on
public contracts, or for dependency version bumps unless they change a major boundary.
