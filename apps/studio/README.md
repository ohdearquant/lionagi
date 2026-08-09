# Lion Studio

Web interface for Lion. The default experience is zero-install: the hosted
client-side SPA at <https://lion-studio.khive.ai> connects to your local
daemon at `http://127.0.0.1:8765`. Studio state remains in that local daemon;
model requests follow the data-handling terms of the provider you use. The
same-origin FastAPI-served build remains available for Docker, source, and dev
modes.

## Project Layout

```text
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
| `LIONAGI_STUDIO_OPERATOR_CWD` | *(unset)* | Absolute execution root for Operator CLI providers |
| `LIONAGI_HOME` | `~/.lionagi` | Base LionAGI data directory (holds `state.db`) |
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

## Operator quickstart

Operator uses the locally installed Claude Code CLI and its own authenticated
session by default. The Studio extra does not install or sign in to Claude
Code.

Set `LIONAGI_STUDIO_OPERATOR_CWD` to an existing absolute directory to pin all
Operator turns to one execution root. When it is unset, Operator uses the
selected project's registered path. If neither path is usable, or if the
explicit setting is invalid, the turn fails with a configuration error instead
of inheriting whichever directory launched the Studio daemon. Browser-created
conversations currently have no project selection, so configure this variable
for normal Operator use in container deployments.

On a fresh machine:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install "lionagi[studio]"

npm install -g @anthropic-ai/claude-code
claude --version
claude auth login

li studio
```

See [Claude Code setup](https://code.claude.com/docs/en/setup) for other
supported installation and authentication methods.

In Studio, use the speech-bubble button at the top of the left rail, press
<kbd>Cmd</kbd>/<kbd>Ctrl</kbd>+<kbd>J</kbd>, or choose **Toggle Operator** from
the command palette. The dock can close while a turn runs; closing it does not
stop the turn.

- Conversation history is stored by the daemon. Reloading the page or reopening
  the dock restores the selected conversation and its earlier activity.
- **Stop** requests cancellation of the active turn. It does not undo tool work
  that already completed.
- Disk writes, commands, and other gated work pause on a **Permission required**
  card. **Allow** permits that request; **Deny** blocks it. The engine waits for
  the decision.
- Every Operator turn has an **Open run** link into Fleet's Runs view. Run
  detail also offers **Continue this run** for live or terminal runs that still
  have a persisted branch. A follow-up submitted while the current leg is
  still live is queued and starts after that leg reaches a terminal state.
- The run detail CLI escape hatch is equivalent to:

  ```bash
  li agent -r '<branch-id>' --prompt '<follow-up instruction>'
  ```

For permission behavior, resume details, real-provider verification, and the
CI stub boundary, see the [Studio guide](../../docs/guides/studio.md).

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

```text
Authorization: Bearer <token>
```

`/health` remains open regardless.

## Database

Studio uses the LionAGI state database at `$LIONAGI_HOME/state.db`
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
