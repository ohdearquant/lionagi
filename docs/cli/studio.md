# li studio

Launch Lion Studio — the web UI for inspecting sessions, invocations, and team channels.

## Synopsis

```
li studio [start] [options]
```

## Description

`li studio` starts a local web server running the Studio application. The bare command `li studio` is equivalent to `li studio start`.

The backend is served via uvicorn (`apps.studio.server.app:app`). The frontend is a separate process; use `--frontend-mode` to launch it alongside the backend, or `--no-frontend` to skip it.

## Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--port PORT` | int | `LIONAGI_STUDIO_PORT` env, or `8765` | Port to listen on. |
| `--host HOST` | string | `127.0.0.1` | Host address to bind. |
| `--frontend-mode {dev,start,none}` | choice | `none` | Frontend launch mode. `dev` starts the dev server; `start` serves a production build; `none` skips the frontend. |
| `--no-frontend` | flag | `false` | Do not launch the frontend. Implied by `--frontend-mode none`. |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `LIONAGI_STUDIO_PORT` | Default port (fallback `8765` if unset). |

## Examples

```bash
# Start Studio on the default port (8765)
li studio

# Custom port
li studio --port 9000

# Bind to all interfaces (e.g. in a container)
li studio --host 0.0.0.0 --port 8765

# Skip frontend entirely
li studio --no-frontend
```

## Notes

- Frontend modes other than `none` print a warning if the frontend package is not installed.
- Studio reads session data from the state database at the path returned by `lionagi.state.db.DEFAULT_DB_PATH`. Run `li state import` first to backfill runs from disk if you have existing sessions.
