# Lion Studio

Local web interface for Lion. FastAPI backend serves a Vite SPA from the same
origin — one URL, one process.

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

**Default (repo present, production mode)**:
```bash
li studio          # builds dist/ if stale, starts uvicorn on http://127.0.0.1:8765
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
