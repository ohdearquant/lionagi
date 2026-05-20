# ADR-0005: Workers-to-Playbooks Rename Strategy

**Status**: Accepted
**Date**: 2026-05-19

## Context

The source codebase used the term "workers" for the units of work that users define and execute.
Lion Studio's public-facing identity uses "playbooks" — a term that better reflects the
orchestration-script nature of these units and avoids internal naming that leaks into the UI.

A complete atomic rename (all files, routes, TypeScript symbols, and copy) is one option. A
staged rename scoped to the user-visible surface is another.

## Decision

The rename is applied in two stages, only stage one ships in v1:

**Stage 1 (v1 — shipped)**: URL paths, API routes, filesystem paths, and all user-visible copy
rename `workers` → `playbooks`. Specifically:

- Route directory `workers/` → `playbooks/`
- API path `GET /api/workers` → `GET /api/playbooks`
- Filesystem root `~/.lionagi/workers/` → `~/.lionagi/playbooks/`
- All UI copy: page titles, labels, empty-state text

**Stage 2 (deferred)**: Internal TypeScript symbol names (`WorkerFormData`, `listWorkers`,
`WorkersPage`, and ~20 related identifiers) are intentionally left unchanged. These are accepted
tech debt.

## Consequences

**Positive**
- Zero user-facing "Workers" copy remains after stage 1; the public model is consistent.
- Stage 1 risk is bounded to route and path changes, which are testable end-to-end.

**Negative**
- `grep "Workers"` still hits ~20 TypeScript identifiers in the frontend source.
- A future sweeper performing a mechanical symbol rename risks breaking call sites; they MUST
  read [ADR-0002 Appendix B](ADR-0002-studio-tech-stack.md#appendix-b) before touching symbols.
- The split between "renamed" and "not renamed" is a latent confusion point for new contributors.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Full atomic rename (symbols + routes + copy in one pass) | Too much churn for v1; risk of typo regressions across ~20 call sites; unblocks no user-facing feature |
| No rename (keep "workers" everywhere) | "Workers" does not match the playbook model; creates brand inconsistency from day one |

## References

- `brand_swaps.md:31-35` — code_id table: `workers.py` → `playbooks.py`, route dir, API path
- `lift-frontend/_intent.md:39-40` — route rename + in-file API reference updates
- `lift-backend/lift_summary.md:61-63` — Route Remap: `GET /api/workers` → `GET /api/playbooks`
- `frontend-finalize/_intent.md:68` — F1 closes UI copy gap
- [ADR-0002 Appendix B](ADR-0002-studio-tech-stack.md#appendix-b) — symbol retention rationale
