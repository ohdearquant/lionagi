# ADR-0005: Playbooks Naming Convention

**Status**: Accepted
**Date**: 2026-05-19

## Context

The initial Studio codebase used "workers" for the units of work that users define
and execute. "Playbooks" better reflects the orchestration-script nature of these
units and aligns with the product vocabulary.

## Decision

All user-visible surfaces, routes, filesystem paths, and API endpoints use
"playbooks" consistently:

- Route: `/playbooks`, `/playbooks/{name}`
- API: `GET /api/playbooks`
- Filesystem: `~/.lionagi/playbooks/`
- UI copy: page titles, labels, badges

The term "worker" does not appear in lionagi's Studio vocabulary. Internal
TypeScript types (e.g., `WorkerStepNode`, `WorkerLinkEdge`, `WorkerCanvas`)
retain "Worker" only where they refer to the graph-format playbook step/link
data model — these are technical type names, not user-facing labels.

## Consequences

**Positive**

- Consistent vocabulary across CLI, Studio, and documentation.
- "Playbook" communicates orchestration intent better than "worker."

**Negative**

- Some TypeScript graph types retain "Worker" prefix — accepted tech debt for
  types that describe the graph editing canvas, not user-facing concepts.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Keep "workers" everywhere | Does not match the playbook orchestration model |
| Rename all TypeScript symbols too | Churn for internal types that don't surface in UI |
