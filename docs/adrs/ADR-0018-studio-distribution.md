# ADR-0018: Studio Distribution and Local Access

**Status**: Accepted
**Date**: 2026-05-20

## Context

Lion Studio is a web UI (React frontend + Python/FastAPI backend) that needs
to reach the user's local filesystem, CLI tools (`li agent`, `li play`, etc.),
and `~/.lionagi/state.db`. Users need a way to install and run Studio without
frontend toolchain knowledge (no `npm install`, no Node.js requirement).

Docker is the obvious distribution mechanism for web apps. But Studio is not a
typical web app — it is a **local dev tool** whose entire value depends on
unrestricted host access:

- The backend reads/writes `~/.lionagi/` (state.db, runs/, agents/, playbooks/).
- The backend spawns CLI subprocesses (`li agent`, `li play`, `li o flow`).
- SSE streams depend on subprocess stdout/stderr piping.
- The frontend serves on `127.0.0.1` only (ADR-0008 security posture).

Docker Compose would require volume-mounting `~/.lionagi/`, bind-mounting the
host's `$PATH` or socket-forwarding for subprocess spawning, and exposing the
container's port. This is more complex than running the server natively, and
the isolation Docker provides is actively unwanted — Studio *is* the local
environment's UI.

## Decision

### Primary distribution: bundled static assets in the Python package

The React frontend is built at CI/release time. The built static files
(`dist/`) are included in the Python package under
`lionagi/studio/static/`. The FastAPI server serves them directly.

```
pip install lionagi[studio]    # or: pip install lionagi (if studio becomes default)
li studio                      # starts uvicorn, serves frontend + API on 127.0.0.1:8765
```

Users need: Python 3.11+, `pip install lionagi[studio]`. No Node.js, no npm,
no Docker. One command to start.

**Build pipeline** (CI only — users never run this):

```bash
cd apps/studio/frontend && npm ci && npm run build
cp -r dist/ ../../lionagi/studio/static/
```

The `lionagi/studio/static/` directory is `.gitignore`d (built artifact, not
source). The Python package's `pyproject.toml` includes it via
`[tool.hatch.build.targets.wheel] / packages`.

**Server startup** (`li studio`):

```python
from fastapi.staticfiles import StaticFiles

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="studio")
```

API routes (`/api/*`) are registered before the static mount so they take
precedence. The frontend's client-side router handles all non-API paths.

### Development mode

Developers who modify the frontend use the standard dev server:

```bash
cd apps/studio/frontend && npm run dev    # Vite dev server on :5173
cd apps/studio && uv run python -m server  # API server on :8765
```

Vite proxies `/api/*` to `:8765`. This is already configured.

### Optional: Docker Compose for server/team deployment

For a shared Studio instance (e.g., a team server monitoring a shared
`~/.lionagi/` directory on a build machine), Docker Compose is available
but explicitly **not the primary path**:

```yaml
# docker-compose.yml (optional, not shipped in pip package)
services:
  studio:
    build: .
    volumes:
      - ${LIONAGI_HOME:-~/.lionagi}:/home/lion/.lionagi
      - /var/run/docker.sock:/var/run/docker.sock  # if CLI needs Docker
    ports:
      - "127.0.0.1:8765:8765"
    environment:
      - LIONAGI_HOME=/home/lion/.lionagi
```

**Limitations of Docker mode** (document clearly):

- CLI subprocesses run inside the container, not on the host. Tools available
  inside the container may differ from the host.
- Git operations see the container's git config, not the host's.
- SSH keys, API tokens, and shell profiles are not available unless explicitly
  mounted.
- Performance overhead from volume mounts on macOS (osxfs/virtiofs).

Docker mode is a **known-limited deployment option**, not the recommended path.
The README should say: "For local use, `pip install` is simpler and has full
host access. Docker is for shared/server deployments."

### `li studio` extras dependency

The `[studio]` extra adds:

- `uvicorn` — ASGI server
- `fastapi` — already a dependency of lionagi
- `aiosqlite` — already decided as a dependency

No heavyweight additions. The frontend is pre-built static HTML/JS/CSS —
zero runtime frontend dependencies.

### Future: Desktop wrapper (deferred)

Tauri or Electron could wrap Studio as a native app with system tray, auto-start,
and OS-level notifications. This is deferred until Studio is feature-complete.
The bundled-static-assets architecture is compatible — a desktop wrapper would
embed the same uvicorn server and open a webview to `127.0.0.1:8765`.

## Consequences

**Positive**
- One-command install and start (`pip install lionagi[studio] && li studio`).
- Full host access — no Docker isolation getting in the way of local dev tooling.
- No frontend toolchain required for users (Node.js, npm stay in CI only).
- Docker option exists for team/server deployments without blocking the primary path.
- Compatible with future desktop wrapper without architecture changes.

**Negative**
- CI must build the frontend and include it in the wheel. Adds a build step.
- `lionagi[studio]` wheel is larger (~2-5 MB for built React assets).
- Docker mode has documented limitations around host access — users who expect
  full Docker + full host access will be disappointed (this is a fundamental
  Docker constraint, not a lionagi limitation).

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Docker-only distribution | Walls off local filesystem, CLI tools, SSH keys. The primary use case (local dev tool) becomes the hardest path. |
| npm-based install (users run `npm install`) | Adds Node.js as a user requirement. lionagi is a Python tool; requiring a second ecosystem for the UI is friction. |
| Electron desktop app (primary) | Heavy (~100MB+), slow to build, Chromium bundled. Premature for a tool with one primary user. Revisit post-feature-complete. |
| Separate PyPI package (`lionagi-studio`) | Splits the install. Users must coordinate versions. The studio is part of lionagi, not a separate product. |
| Serve frontend from CDN | Requires internet. Studio must work offline (localhost-only, air-gapped environments). |

## References

- [ADR-0008](ADR-0008-studio-v1-scope.md) — Security posture (127.0.0.1 only)
- [ADR-0013](ADR-0013-zero-dependency-ui.md) — Zero-dependency frontend (no component library)
- [ADR-0014](ADR-0014-cli-primary-studio-secondary.md) — CLI-primary, Studio-secondary
