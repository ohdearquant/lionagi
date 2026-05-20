# ADR-0008: Lion Studio v1 Scope — Read-Only, Single-Workspace, Internal

**Status**: Accepted
**Date**: 2026-05-19

## Context

Lion Studio is a monitoring dashboard for lionagi runs, agents, playbooks, and shows. Before
building the backend and frontend, the scope of v1 must be bounded to avoid scope creep and
to make explicit what is NOT supported (so future PRs adding those capabilities do so knowingly).

Three boundary decisions were on the table: write operations, multi-workspace/multi-user support,
and authentication. Each has implementation cost and risk disproportionate to v1 value.

## Decision

Lion Studio v1 is bounded as follows:

- **Read-only**: All write endpoints (create, update, delete) are stubbed with HTTP 501. A
  `# TODO(lion-studio-writes)` comment marks each stub for a future play.
- **Single local workspace**: Data is read from the local machine's filesystem only
  (`~/.lionagi/`, `~/khive-work/`). No remote backends, no multi-user routing.
- **Internal**: Not published as a separate PyPI package or public service. Installed via
  `pip install lionagi[studio]` only (see ADR-0001).
- **No authentication**: The studio serves `localhost` only; authentication is explicitly out
  of scope for v1.

Cross-project monitoring (lionagi issue #967) is a v2 concern.

## Consequences

**Positive**
- 11 GET routes can ship in v1 without any mutation risk to user data.
- No auth layer means no token management, session handling, or RBAC to implement or test.
- Minimal operational surface: one uvicorn process, no credentials, no secrets.

**Negative**
- Any future PR adding writes, auth, multi-workspace, or remote support MUST explicitly
  acknowledge that it is changing the v1 scope decision captured here.
- Write stubs (501) return an error to the client rather than silently ignoring mutations —
  this is intentional, not a bug.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Ship with stub authentication (e.g., static API key) | Premature; localhost-only workload needs no auth; adds config surface for no benefit |
| Enable write endpoints behind a feature flag | Mutations without UX polish (confirmation dialogs, error handling) create data-loss risk; deferred until UX is validated |

## References

- `lift-backend/_intent.md:53-55` — write endpoint stub convention (`# TODO(lift-backend-writes)`)
- `add-shows-pages/_intent.md:50` — authentication explicitly out of scope
- `lift-backend/lift_summary.md:83` — 11 GET routes implemented, 11 write endpoints stubbed (501)
- [ADR-0004](ADR-0004-filesystem-data-layer.md) — filesystem data layer (enables single-workspace)
- lionagi issue #967 — cross-project monitoring (v2)
