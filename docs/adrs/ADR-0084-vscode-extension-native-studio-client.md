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
tool that is real friction, and a hosted deployment would move per-user run data
and local workspace access off the user's machine, which is the opposite of what
the product is.

The audience already lives in an editor. A VS Code extension installs in one
click from the Marketplace (and Open VSX, which covers Cursor / VSCodium /
Windsurf), runs inside an environment the user already has open, and can manage
the backend lifecycle on the user's behalf.

The studio backend already exposes a **read** API over the same local StateDB
the web app reads, and it lives in the **public** `lionagi` repository. The read
model is `$LIONAGI_HOME/state.db` (a SQLite StateDB; `LIONAGI_HOME` defaults to
`~/.lionagi`); the HTTP surface is a projection of it:

- Observe: `GET /api/runs/` (paginated), `GET /api/runs/{run_id}` — served from
  StateDB.
- Live: `GET /api/sessions/{id}/stream` — Server-Sent Events (`data: {json}\n\n`,
  with `{type:"heartbeat"}` / `{type:"done"}` framing), the live projection of
  the same rows.
- Health: `GET /health` (unauthenticated) for liveness probing.
- Auth: optional `LIONAGI_STUDIO_AUTH_TOKEN` bearer on `/api/*`.

The same StateDB also receives mirrored **Claude Code** sessions (a background
tail that ingests local Claude Code transcripts), so a single dashboard spans
lionagi runs and Claude Code sessions across projects and agents.

The Vite SPA, by contrast, is the heavyweight cockpit and is developed
separately.

## Decision

Build the VS Code extension ("Den") as a **native TypeScript, read-only
observability client over the public studio `/api` + SSE**, not as a webview
that embeds the SPA.

1. **Native UI, not embedded SPA.** Tree views, commands, and purpose-built
   webviews rendered by the extension. The extension talks only to the public
   FastAPI surface. It does not import, bundle, or depend on the SPA. This keeps
   the extension a fully public, marketplace-distributable artifact with no
   private-repo coupling, and lets the UI feel native to the editor (activity
   bar, codicons, theme-matched webviews).

2. **Read-only observability, no mutating surface.** The extension observes and
   inspects existing StateDB-backed data: it reads runs, streams sessions, and
   renders the run/flow tree. It does not launch agents, run playbooks, edit, or
   schedule. This keeps Den inside ADR-0008's read-only contract and avoids
   competing with the agent tooling already in the editor. (A launch/control
   surface was prototyped and deliberately cut; see Alternatives.)

3. **The extension owns the backend lifecycle.** On activation it discovers a
   Python interpreter and spawns the **bare** studio API as
   `python -m lionagi.studio` (the `lionagi/studio/__main__.py` entry point; it
   calls `uvicorn.run(app)` and serves the API only by default, because it does
   not set `LIONAGI_STUDIO_FRONTEND_DIST`, the variable that would otherwise
   mount the SPA) on a loopback port, health-checks `GET /health`, and surfaces
   state in the status bar. It deliberately does
   **not** use Docker: Den is the UI, and a VS Code extension must not require a
   container runtime. It can attach to an already-running backend instead of
   spawning, via the `den.url` setting (point it at a running `li studio` on
   :8765). Configuration is the `den.*` namespace: `den.url`, `den.pythonPath`,
   `den.port`, `den.host`, `den.autoStart`, `den.authToken`. The Python
   dependency is inherent to lionagi and acceptable for this audience; the
   extension manages it rather than asking the user to.

4. **Everything reduces to one primitive: browse + stream.** A tree over an API
   collection, with a detail webview that subscribes to the session SSE and
   renders progress. Every feature is a view of this; build it once and reuse.

### Scope

- **v0 — spine** (this ADR's deliverable): backend lifecycle + status bar; a
  **Runs explorer** tree (`GET /api/runs/`) with a run-detail webview that
  streams live via the sessions SSE; a **run tree** view; and the **Claude Code
  mirror** so lionagi runs and Claude Code sessions share one cross-project
  dashboard. Proves spawn-backend → REST + SSE → native UI end to end.
- **Later**: editor-native, still-read-only integrations (open files an agent
  changed, sandbox-diff review). A Den MCP server exposing the same
  observability surface is a possible follow-on.

### Location & distribution

- Lives at `apps/vscode/` in the public repo, alongside `apps/studio/`.
- Packaged with `@vscode/vsce`; published to the VS Code Marketplace and Open
  VSX. Marketplace publishing requires a registered publisher + PAT (owner
  action), out of scope for the code itself.

## Consequences

- **Positive**: solves distribution (one-click install, no hosting); the
  extension is fully public; live observability is a *subscribe* because the SSE
  already exists; zero backend rebuild; native editor feel; the same StateDB
  read model already powers the SPA, so there is one source of truth, not a
  second data store.
- **Negative / costs**: the extension must manage a Python child process
  (interpreter discovery, spawn, health, teardown) — real lifecycle complexity.
  The backend remains a Python dependency. Two UI surfaces (SPA + extension) now
  consume the same API and must not drift; the API contract is the seam.

## Alternatives Considered

- **Embed the SPA in a webview** — rejected. Re-imports the distribution problem
  (still needs the server), couples a public artifact to a separately-developed
  SPA, and does not feel native. The API is the integration boundary, not the
  rendered SPA.
- **Docker-managed backend** — rejected. Requiring a container runtime inside a
  VS Code extension is unacceptable friction for the audience; the bare
  `python -m lionagi.studio` process is enough and uses the interpreter the user
  already has.
- **Direct StateDB access from the extension** — rejected. Reading the SQLite
  file directly from TypeScript would duplicate the studio read model and bypass
  auth and the SSE projection. The public `/api` + SSE is the contract; StateDB
  stays behind it.
- **Launch / control scope (trigger agents, run playbooks, schedule)** —
  rejected for v0. A mutating surface was prototyped and cut: it duplicated
  entry points the editor's agent tooling already offers and pulled Den outside
  ADR-0008's read-only contract. Den's differentiated value is the live
  cross-project, cross-agent dashboard, so v0 is observability only.
