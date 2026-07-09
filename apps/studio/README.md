# Lion Studio

Web interface for Lion. The default experience is zero-install: the hosted
client-side SPA at <https://lion-studio.khive.ai> connects to your local
daemon at `http://127.0.0.1:8765` — data never leaves your machine. The
same-origin FastAPI-served build remains available for Docker, source, and
dev modes.

## Project Layout

```
apps/studio/
└── frontend/               Vite + React SPA
    ├── src/                Source (routes, components, lib)
    ├── dist/               Built output (served by FastAPI)
    └── vite.config.mts     Dev server proxies /api → localhost:8765
```

The backend lives at `lionagi/studio/` (installed as part of the `lionagi[studio]`
package). Studio routers are mounted under `/api`, and the built `dist/` is
mounted as a static SPA fallback on all other paths.

## Environment Variables

All variables are optional; defaults are shown.

| Variable | Default | Purpose |
|---|---|---|
| `LIONAGI_STUDIO_HOST` | `127.0.0.1` | FastAPI bind host |
| `LIONAGI_STUDIO_PORT` | `8765` | FastAPI bind port |
| `LIONAGI_STUDIO_AUTH_TOKEN` | *(unset)* | Bearer token for `/api/*` routes |
| `LIONAGI_STUDIO_FRONTEND_DIST` | `apps/studio/frontend/dist` | Path to built SPA dist/ |
| `LIONAGI_DATA_ROOT` | `~/.lionagi` | Base LionAGI data directory |
| `LIONAGI_SHOWS_ROOT` | `~/khive-work/shows` | Show artifact root |
| `CORS_ORIGINS` | `localhost:5173,localhost:3000` | Comma-separated allowed browser origins |

## Running

**Default (hosted UI + local daemon)**:
```bash
li studio          # starts the local daemon and opens https://lion-studio.khive.ai;
                   # nothing is built locally (pass --no-open to skip the browser)
```

**Self-contained local build (Docker or same-origin serve)**:
```bash
li studio --docker # auto-pulls ghcr.io/ohdearquant/lion-studio; UI + API on :8765
```

**Dev mode (hot-reload)**:
```bash
li studio --dev    # Vite dev server on :3000 + uvicorn on :8765; Vite proxies /api
```

**Backend only** (e.g. desktop shell):
```bash
li studio --no-frontend
```

## Development

**Backend** (auto-reloads):
```bash
uv run uvicorn lionagi.studio.app:app --reload --host 127.0.0.1 --port 8765
```

**Frontend** (separate terminal):
```bash
cd apps/studio/frontend
npm install
npm run dev        # http://localhost:5173 — proxies /api → :8765
```

## Authentication

When `LIONAGI_STUDIO_AUTH_TOKEN` is unset, all local API routes are open.

When set, all `/api/*` requests must include:
```
Authorization: Bearer <token>
```

`/health` remains open regardless.

## Database

Studio uses the LionAGI state database at `$LIONAGI_DATA_ROOT/state.db`
(default `~/.lionagi/state.db`).

## Testing

```bash
# Full suite
uv run pytest tests/apps_studio_server/ -x

# Skip slow integration tests
uv run pytest tests/apps_studio_server/ -m "not (integration or network)" -x

# Strict warnings (CI gate)
uv run pytest tests/apps_studio_server/ -W error
```

## Desktop App (macOS)

See [`desktop/README.md`](desktop/README.md) for the full guide.

**Quick start:**

```bash
# 1. Build the SPA (required before running the shell)
cd apps/studio/frontend && npm install && npm run build

# 2. Run the shell
cd ../desktop/src-tauri && cargo run

# 3. Dev mode (Vite hot-reload + Tauri)
#    Terminal 1: cd apps/studio/frontend && npm run dev
#    Terminal 2: cd apps/studio/desktop/src-tauri && cargo tauri dev

# 4. Build .app bundle
cargo tauri build         # signed + DMG
cargo build --release     # binary only, no signing
```

The shell finds the `li` CLI automatically (searches PATH,
`~/.local/bin/li`, `~/.cargo/bin/li`, `/opt/homebrew/bin/li`), spawns
`li studio --no-frontend --port <free-port>`, and loads the SPA with
`window.__STUDIO_API_BASE__` pre-set via Tauri's initialization script API.
