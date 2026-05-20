# ADR-0004: Filesystem-Backed Data Layer for Lion Studio

**Status**: Accepted
**Date**: 2026-05-19

## Context

Lion Studio's backend must serve runs, agents, playbooks, and show data to the dashboard. The
data already exists on the local filesystem as output from lionagi's CLI and show tooling:
`~/.lionagi/runs/`, `~/.lionagi/agents/`, `~/.lionagi/playbooks/`, and `~/khive-work/shows/`.

A persistent database, ORM, or caching layer would require schema definition, migration tooling,
and a sync process to keep the DB consistent with the filesystem that the CLI writes to directly.

## Decision

The Lion Studio backend reads all data directly from the local filesystem on each request. No
database, ORM, or caching layer is introduced. Each service scans its designated directory tree
and returns the results. The configuration mapping is:

| Route | Filesystem root |
|-------|----------------|
| `/api/runs` | `~/.lionagi/runs/` |
| `/api/agents` | `~/.lionagi/agents/` |
| `/api/playbooks` | `~/.lionagi/playbooks/` |
| `/api/shows` | `~/khive-work/shows/` |

## Consequences

**Positive**
- Zero schema migrations; the filesystem is the schema.
- Instant consistency: CLI writes are visible to the dashboard on the next poll cycle.
- Trivial deployment — no database process to start or configure.
- No ORM impedance mismatch; directory scan logic is plain Python.

**Negative**
- Directory scans on every request do not scale beyond a single local workspace.
- Multi-user, remote, or persistent-state features are ruled out without revisiting this decision.
- No query capabilities (filtering, sorting) beyond what Python list comprehensions provide.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| SQLite for query speed | Adds schema management and a sync process; the single-user local workload does not justify the complexity |
| Redis cache layer | Single-user workload; cache invalidation adds complexity with no throughput benefit |

## References

- `_show.md:8` — shows root at `~/khive-work/shows/<topic>/`
- `lift-backend/_intent.md:44-48` — services scan filesystem directories
- `lift-backend/lift_summary.md:40-45` — services/*.py descriptions
- `brand_swaps.md:41-55` — config mapping (directories)
- [ADR-0008](ADR-0008-studio-v1-scope.md) — v1 scope decision (single-workspace, read-only)
