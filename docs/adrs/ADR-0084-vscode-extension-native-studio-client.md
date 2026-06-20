# ADR-0084: VS Code Extension as a Native Client over the Studio API

**Status**: Accepted
**Date**: 2026-06-20
**Builds on**: ADR-0008 (runs read-only / sessions stream for live monitoring) · ADR-0026 (project detection) · ADR-0083 (lifecycle signal contract)

## Context

Lion Studio ships today as a standalone web app: a FastAPI backend
(`lionagi.studio.app:app`, uvicorn) plus a Vite SPA served from
`LIONAGI_STUDIO_FRONTEND_DIST`. Distribution is the weak link. To use it a
developer must `pip install 'lionagi[studio]'`, run `li studio` (or pull a
Docker image), manage a local port, and open a browser tab. For a developer
tool that is real friction, and a hosted deployment moves per-user run data and
local workspace access off the user's machine, which is the opposite of what
the product is.

The audience already lives in an editor. A VS Code extension installs in one
click from the Marketplace (and Open VSX, which covers Cursor / VSCodium /
Windsurf), runs inside an environment the user already has open, and can manage
the backend lifecycle on the user's behalf.

The backend is already a complete control + observability surface, and it is in
the **public** `lionagi` repository:

- Trigger: `POST /api/launches/` (202) with `LaunchRequest` →
  `{invocation_id, action_kind}`. Valid kinds: `agent`, `flow`, `fanout`,
  `play`, `engine`.
- Observe: `GET /api/runs/` (paginated), `GET /api/runs/{run_id}`.
- Live: `GET /api/sessions/{id}/stream` — Server-Sent Events
  (`data: {json}\n\n`, with `{type:"heartbeat"}` / `{type:"done"}` framing).
  (The `/runs/{id}/events` SSE route was removed in ADR-0008; sessions stream
  is the live channel.)
- Playbooks: full CRUD + `POST /api/playbooks/{name}/run`.
- Scheduling: `schedules` / `scheduler_state` services.
- Auth: optional `LIONAGI_STUDIO_AUTH_TOKEN` bearer on `/api/*`.

The Vite SPA, by contrast, is the heavyweight cockpit and carries a separate
(private) development line.

## Decision

Build the VS Code extension as a **native TypeScript client over the public
studio `/api` + SSE**, not as a webview that embeds the SPA.

1. **Native UI, not embedded SPA.** Tree views, commands, and purpose-built
   webviews rendered by the extension. The extension talks only to the public
   FastAPI surface. It does not import, bundle, or depend on the SPA. This keeps
   the extension a fully public, marketplace-distributable artifact with no
   private-repo coupling, and it lets the UI feel native to the editor (activity
   bar, codicons, theme-matched webviews, editor-context actions).

2. **The extension owns the backend lifecycle.** On activation it discovers a
   Python interpreter, spawns the backend (`li studio --no-frontend`, or
   `python -m uvicorn lionagi.studio.app:app`) on a loopback port, health-checks
   `GET /health`, and surfaces state in the status bar. It can also attach to an
   already-running backend (`lionagi.studio.url` setting) instead of spawning.
   The Python dependency is inherent to lionagi and acceptable for this
   audience; the extension manages it rather than asking the user to.

3. **Everything reduces to two primitives.** *Browse* (a tree over an API
   collection) and *trigger + stream* (`POST` to launch, subscribe to the
   session SSE, render progress). Every feature is one of these two; build them
   once and reuse.

### Scope

- **v0 — spine** (this ADR's deliverable): backend lifecycle + status bar; a
  **Runs explorer** tree (`GET /api/runs/`) with a run-detail webview that
  streams live via the sessions SSE; **trigger `li agent`**
  (`action_kind:"agent"` → `POST /api/launches/`) that lands as a run and
  streams its output. Proves spawn-backend → REST + SSE → native UI end to end.
- **v1 — product**: **flow DAG observability** (the live graph the terminal
  cannot draw — the signature feature) and a **playbooks browser** (list / run /
  edit). Reuses v0's browse + trigger + stream primitives.
- **Later**: scheduled runs (the only feature that forces an always-on resident
  backend), and editor-native integrations (open files an agent changed,
  sandbox-diff review, review-lane approvals per #1410).

### Location & distribution

- Lives at `apps/vscode/` in the public repo, alongside `apps/studio/`.
- Packaged with `@vscode/vsce`; published to the VS Code Marketplace and Open
  VSX. Marketplace publishing requires a registered publisher + PAT (owner
  action), out of scope for the code itself.

## Consequences

- **Positive**: solves distribution (one-click install, no hosting); the
  extension is fully public; live observability is a *subscribe* because the SSE
  already exists; zero backend rebuild; native editor feel and editor-context
  triggering that a web app cannot offer.
- **Negative / costs**: the extension must manage a Python child process
  (interpreter discovery, spawn, health, teardown) — real lifecycle complexity.
  The backend remains a Python dependency. Two UI surfaces (SPA + extension) now
  consume the same API and must not drift; the API contract is the seam.
- **Rejected — embed the SPA in a webview**: re-imports the distribution
  problem (still needs the server), couples a public artifact to the private SPA
  line, and does not feel native. The API is the integration boundary, not the
  rendered SPA.
