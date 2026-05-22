# Studio Setup

## Prerequisites

| Requirement | Notes |
|---|---|
| Python ≥ 3.10 | Required by lionagi (`pyproject.toml:9`) |
| `uv` | Preferred runner — see [install guide](https://docs.astral.sh/uv/) |
| Node.js ≥ 18 + pnpm | Required for the Next.js frontend |
| `lionagi[studio]` installed | Pulls FastAPI, uvicorn, aiosqlite, PyYAML, Starlette |

Install the Studio extras:

```bash
uv pip install -e '.[studio]'
# or if already installed:
uv pip install 'lionagi[studio]'
```

The `[studio]` extra adds: `fastapi>=0.115`, `uvicorn>=0.34`, `starlette>=0.46.2`, `aiosqlite>=0.21.0`, `pyyaml>=6.0`.

## Environment Variables

All variables are optional. The table below shows the name, type, default value, and where each is read in the source.

| Variable | Type | Default | Description |
|---|---|---|---|
| `LIONAGI_STUDIO_PORT` | `int` | `8765` | FastAPI bind port. Read at `apps/studio/server/config.py:6`. |
| `LIONAGI_STUDIO_HOST` | `str` | `127.0.0.1` | FastAPI bind host. `127.0.0.1` keeps the server local-only by default. Set to `0.0.0.0` to expose on the network. Read at `config.py:7`. |
| `LIONAGI_DATA_ROOT` | `Path` | `~/.lionagi` | Base directory for all Lion data (agents, playbooks, skills, runs, teams, and `state.db`). Read at `config.py:8`. |
| `LIONAGI_SHOWS_ROOT` | `Path` | `~/khive-work/shows` | Root directory for show filesystem trees. Set this to wherever your `li show` artifacts land. Read at `config.py:9`. |
| `CORS_ORIGINS` | `str` | *(see below)* | Comma-separated list of allowed browser origins. Defaults to `http://localhost:5173,http://localhost:3000` when unset. Read at `config.py:13–18`. |
| `LIONAGI_STUDIO_AUTH_TOKEN` | `str` | *(unset)* | Bearer token for auth gating. When unset, the API is fully open. Read at `app.py:41`. |
| `NEXT_PUBLIC_STUDIO_API_BASE` | `str` | `http://localhost:8765` | Frontend API base URL. Set in the Next.js environment if the backend runs on a non-default port. Read at `frontend/lib/api.ts:20`. |

!!! tip "Why localhost-only by default"
    `HOST=127.0.0.1` means the Studio API is not reachable from other machines without SSH tunneling. This is intentional — Studio exposes `~/.lionagi/state.db` and your agent profiles. Set `LIONAGI_STUDIO_HOST=0.0.0.0` only on a trusted network, and always set `LIONAGI_STUDIO_AUTH_TOKEN` when you do.

## Database Setup

Studio uses SQLite at `~/.lionagi/state.db` (the path comes from `lionagi/state/db.py:18`: `DEFAULT_DB_PATH = LIONAGI_HOME / "state.db"`).

The database is created automatically when you first run `li agent` or `li play`. You do not need to create it manually.

Relevant SQLite settings applied on every connection (`services/_db.py:34`):

```
PRAGMA journal_mode = WAL;       -- concurrent reads while writes happen
PRAGMA busy_timeout = 5000;      -- wait up to 5 s before SQLITE_BUSY
PRAGMA foreign_keys = ON;        -- referential integrity
```

!!! warning "Dev vs production state"
    Do not point a development Studio instance at your production `state.db`. Use `LIONAGI_DATA_ROOT=/tmp/lion-dev` for isolated testing.

## Development Mode

Run backend and frontend in separate terminals:

**Terminal 1 — backend with hot reload:**

```bash
uv run uvicorn apps.studio.server.app:app \
    --reload \
    --host 127.0.0.1 \
    --port 8765
```

**Terminal 2 — frontend dev server:**

```bash
cd apps/studio/frontend
pnpm install
pnpm dev
```

The frontend dev server starts on port 3000 (`package.json:5`: `next dev -p 3000`). Open `http://localhost:3000`.

The Next.js app proxies API calls to `http://localhost:8765` by default (configured via `NEXT_PUBLIC_STUDIO_API_BASE`).

## Production-Like Local Run

```bash
LIONAGI_STUDIO_AUTH_TOKEN=change-me \
LIONAGI_SHOWS_ROOT=/path/to/your/shows \
uv run uvicorn apps.studio.server.app:app \
    --host 127.0.0.1 \
    --port 8765
```

Then build and start the frontend:

```bash
cd apps/studio/frontend
pnpm build
pnpm start   # starts on port 3000
```

## Authentication

When `LIONAGI_STUDIO_AUTH_TOKEN` is set, the auth middleware (`app.py:39–50`) gates two route classes:

| Route class | Methods gated |
|---|---|
| `/api/admin/*` | **All** methods — GET is also protected for admin routes |
| All other `/api/*` routes | Mutating methods only: `POST`, `PUT`, `PATCH`, `DELETE` |

Read-only `GET` requests on non-admin routes and `/health` are always open.

Send the token in the `Authorization` header:

```bash
curl -H "Authorization: Bearer change-me" http://localhost:8765/api/agents/
```

When `LIONAGI_STUDIO_AUTH_TOKEN` is unset, every route is open — suitable for local development only.

## Running Tests

```bash
# Full server test suite
uv run pytest tests/apps_studio_server/ -x

# Skip slow integration and network tests
uv run pytest tests/apps_studio_server/ -m "not (integration or network)" -x

# CI mode — treat warnings as errors
uv run pytest tests/apps_studio_server/ -W error
```

There are no CI-run frontend tests. Frontend type-checking and linting:

```bash
cd apps/studio/frontend
pnpm typecheck
pnpm lint
pnpm format --check
```

## Troubleshooting

**`uvicorn` not found after `pip install lionagi`**

You installed without the `[studio]` extra. Run:

```bash
uv pip install 'lionagi[studio]'
```

**Frontend shows "Failed to fetch" or blank data**

The frontend cannot reach the backend. Check:

1. Backend is running: `curl http://localhost:8765/health` should return `{"status":"ok"}`
2. `NEXT_PUBLIC_STUDIO_API_BASE` matches the backend URL
3. CORS origins include your frontend URL — set `CORS_ORIGINS=http://localhost:3000`

**Sessions page shows no data**

`~/.lionagi/state.db` does not exist yet. Run any `li agent` command first to create and populate it.

**Shows page is empty**

`LIONAGI_SHOWS_ROOT` is not set or points to the wrong directory. Set it to the directory containing your `{topic}/` show trees:

```bash
LIONAGI_SHOWS_ROOT=~/my-shows li studio
```

**`li studio start --frontend-mode dev` does nothing for the frontend**

Frontend auto-launch is not yet implemented (`cli/studio.py:69`). Start the frontend manually with `pnpm dev`.
