# ADR-0002: Lion Studio Tech Stack

**Status**: Accepted
**Date**: 2026-05-19

## Context

Lion Studio (see [ADR-0001](ADR-0001-lion-studio-internal-app.md)) needs a frontend dashboard and
a Python API backend. The selection criteria are: DAG visualisation support, language alignment
with lionagi (Python), SSE streaming compatibility, and minimal net-new code for problems with
existing open-source solutions.

Two frontend approaches were evaluated: a custom React 19 + Vite setup, and Next.js with a
curated set of UI libraries. For the backend, FastAPI on a fixed port was compared against Flask
and a raw Starlette app.

## Decision

Lion Studio uses:

- **Frontend**: Next.js 16 + React 19 + TypeScript + Tailwind CSS + ReactFlow + dagre
- **Backend**: Python + FastAPI + uvicorn, port 8765

ReactFlow with dagre layout is the industry-standard solution for interactive DAG visualisation
in React; reproducing this from scratch offers no benefit. Next.js 16 provides SSR, API routes,
and a mature build system. Tailwind keeps styling co-located without a CSS build step.

FastAPI with Starlette's `StreamingResponse` is the natural SSE backend for a Python codebase:
async-first, Pydantic-native, and avoids the "async-in-sync" friction that Flask introduces.
Port 8765 is unreserved by IANA and is the studio's established default; no conflicts have been
found in common development environments.

The alternative (Vite + React 19 + custom DAG canvas) solves no unique problem for Lion Studio
and introduces redundant engineering for capabilities the chosen stack provides out of the box.

## Consequences

**Positive**

- DAG visualisation, runs polling loop, and agent editor all have proven library support.
- Python backend stays idiomatic with lionagi's own codebase (FastAPI + Pydantic).
- SSE support is first-class via Starlette `StreamingResponse` — no third-party adapter required.
- Port 8765 is consistent across all Studio documentation and tooling.

**Negative**

- `npm install` requires `--legacy-peer-deps` due to an ESLint 9 / `@eslint/js` 10 peer conflict
  in the dependency tree (see Appendix A).
- TypeScript symbol names (`WorkerFormData`, `listWorkers`) reflect an earlier naming layer;
  they are accepted tech debt for v1 (see Appendix B).

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Vite + React 19 (build from scratch) | DAG viz, runs polling, and agent editor are already solved problems — reimplementing them yields no capability advantage. Next.js 16 provides SSR, mature build tooling, and the same React 19 runtime without the bespoke setup. |
| Lift lionag2's AG-UI + Vite stack | lionag2 has no CLI-provider concept; lionagi's claude/codex/gemini CLI providers are load-bearing for the daily-driver use case |
| Flask instead of FastAPI | Sync-first; wrapping async SSE streaming adds friction; Pydantic integration is not native |

### Stack upgrades since v1

- **(2026-05-20)**: Migrated from Next.js 14 → 16 and React 18 → 19. The upgrade was deliberate:
  Next.js 16 is the stable current release, and React 19 ships with it; deferral was no longer
  necessary. The `--legacy-peer-deps` requirement still applies (ESLint peer conflict is not
  resolved by the Next.js version bump).

## References

- [ADR-0001](ADR-0001-lion-studio-internal-app.md) — establishes `apps/studio/` as the home
- [ADR-0006](ADR-0006-sse-live-streaming.md) — SSE protocol decision (uses this stack)

---

## Appendix A — `npm install --legacy-peer-deps`

The frontend dependency tree has a pre-existing peer conflict: `@eslint/js@10` (required by
`eslint-config-next`) conflicts with `eslint@9`'s peer expectations. Running `npm install`
without `--legacy-peer-deps` fails with a peer resolution error. This conflict was not resolved
by the Next.js 14 → 16 upgrade; the flag remains the accepted workaround. Every `npm install`
in CI and local setup MUST include the flag.

Source: `_show.md:148-149`

## Appendix B — TypeScript Symbol Names

Some internal TypeScript types retain `Worker` prefix (`WorkerStepNode`, `WorkerLinkEdge`,
`WorkerCanvas`) where they refer to graph-format playbook step/link data models. These are
technical type names for the graph editing canvas, not user-facing labels. All user-facing
surfaces use "playbooks." See [ADR-0005](ADR-0005-workers-playbooks-rename.md).

## Appendix C — Run Detail: Execution Graph from Playbook Context

The backend session API does not include a `steps` field. Execution graph visualization
(`ExecutionDag.tsx`) derives its structure from the playbook's graph metadata (steps + links),
not from the session. The graph is rendered only when playbook context is known — either by
navigating from the playbook detail page or when the session's `playbook_name` field matches
a graph-format playbook. See ADR-0012.

Source: `frontend-finalize/impl1/summary.md:88-97`; `visual-walkthrough/walkthrough_findings.md:98-103`
