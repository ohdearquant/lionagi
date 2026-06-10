# Lion Studio

Local web interface for Lion. FastAPI backend + Next.js 16 frontend. Lets you
inspect shows, sessions, runs, playbooks, agents, skills, and marketplace metadata.

## Project Layout

```
apps/studio/
├── server/                 FastAPI application
│   ├── app.py              App factory, router mounting, auth middleware, /api/stats
│   ├── config.py           Host, port, data roots, CORS — all env-var driven
│   ├── __main__.py         `python -m apps.studio.server` entrypoint
│   ├── routers/            HTTP route handlers (shows, runs, agents, playbooks, …)
│   └── services/           Filesystem, SQLite, and domain helpers
└── frontend/               Next.js 16 app
    ├── app/                App-router pages
    ├── components/         Reusable UI components
    └── lib/                API client and shared TypeScript types
```

## Environment Variables

All variables are optional; defaults are shown.

| Variable | Default | Purpose |
|---|---|---|
| `LIONAGI_STUDIO_HOST` | `127.0.0.1` | FastAPI bind host |
| `LIONAGI_STUDIO_PORT` | `8765` | FastAPI bind port |
| `LIONAGI_STUDIO_AUTH_TOKEN` | *(unset)* | Bearer token for mutating `/api/*` routes |
| `LIONAGI_DATA_ROOT` | `~/.lionagi` | Base LionAGI data directory |
| `LIONAGI_SHOWS_ROOT` | `~/khive-work/shows` | Show artifact root — set to your shows directory |
| `CORS_ORIGINS` | `localhost:5173,localhost:3000` | Comma-separated allowed browser origins |

## Authentication

When `LIONAGI_STUDIO_AUTH_TOKEN` is unset, all local API routes are open.

When set, mutating `/api/*` requests must include:
```
Authorization: Bearer <token>
```

Read-only requests and `/health` remain open regardless.

## Database

Studio uses the LionAGI state database at `$LIONAGI_DATA_ROOT/state.db`
(default `~/.lionagi/state.db`). Do not point a dev instance at production
state unless intentional.

## Development

**Backend** (auto-reloads on file changes):
```bash
uv run uvicorn apps.studio.server.app:app --reload --host 127.0.0.1 --port 8765
```

**Frontend** (separate terminal):
```bash
cd apps/studio/frontend
npm install
npm run dev
```

Open the URL printed by Next.js (default `http://localhost:3000`).

## Production-Like Local Run

```bash
LIONAGI_STUDIO_AUTH_TOKEN=change-me \
LIONAGI_SHOWS_ROOT=/path/to/your/shows \
uv run uvicorn apps.studio.server.app:app --host 127.0.0.1 --port 8765
```

## Testing

Server tests only (no frontend tests in CI):
```bash
# Full suite
uv run pytest tests/apps_studio_server/ -x

# Skip slow integration tests
uv run pytest tests/apps_studio_server/ -m "not (integration or network)" -x

# Strict warnings (CI gate)
uv run pytest tests/apps_studio_server/ -W error
```

## Notes for Contributors

- Server changes go under `apps/studio/server/`.
- Frontend changes go under `apps/studio/frontend/`.
- Tests go under `tests/apps_studio_server/`.
- Do not modify `lionagi/cli/` or other core SDK paths from Studio code.
- Agent frontmatter uses `effort` (not `reasoning_effort`). The service normalises
  legacy files on read.
