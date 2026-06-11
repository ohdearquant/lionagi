# Lion Studio — macOS Desktop Shell

Tauri 2 shell that wraps the `li studio` FastAPI backend and the Vite SPA into a
native macOS app. The shell locates the `li` CLI, spawns the backend on a free
port, and loads the SPA with `window.__STUDIO_API_BASE__` already set before any
page scripts run.

## Layout

```
apps/studio/desktop/
├── .gitignore
├── README.md                       This file
└── src-tauri/
    ├── Cargo.toml                  lion-studio crate
    ├── Cargo.lock
    ├── build.rs                    tauri-build
    ├── tauri.conf.json             Tauri 2 config
    ├── icons/                      Generated icon set (tauri icon)
    │   ├── icon.icns
    │   ├── icon.ico
    │   ├── 32x32.png
    │   ├── 128x128.png
    │   ├── 128x128@2x.png
    │   └── app-icon.svg            Source SVG (amber L on near-black)
    └── src/
        ├── main.rs                 Binary entry point
        ├── lib.rs                  App setup, window creation, lifecycle
        ├── backend.rs              CLI detection, process spawn, health poll
        ├── port.rs                 Free-port finding + CLI location (tested)
        ├── commands.rs             Tauri commands (retry_backend_launch, get_api_base)
        └── setup_html.rs           Built-in error/loading screen (embedded HTML)
```

## Prerequisites

- Rust stable (tested with 1.77+)
- `lionagi[studio]` installed and `li` on PATH (or `LIONAGI_CLI` env var)
- Built SPA: `apps/studio/frontend/dist/` must exist

## Running in dev mode

**Step 1** — build the SPA (once, or whenever frontend changes):

```bash
cd apps/studio/frontend
npm install
npm run build
```

**Step 2** — run the shell against the built dist:

```bash
cd apps/studio/desktop/src-tauri
cargo run
```

**Dev mode with Vite hot-reload**:

The `devUrl` in `tauri.conf.json` is set to `http://localhost:5173`. When running
via `cargo tauri dev` (tauri-cli), the shell loads the SPA from the Vite dev
server instead of `dist/`:

```bash
# Terminal 1 — Vite dev server
cd apps/studio/frontend && npm run dev

# Terminal 2 — Tauri dev (requires cargo-tauri installed)
cd apps/studio/desktop/src-tauri && cargo tauri dev
```

Note: `cargo tauri dev` requires the Vite dev server to be running first.

## Building the .app bundle

```bash
cd apps/studio/frontend && npm run build        # build SPA first
cd ../desktop/src-tauri
cargo tauri build                               # codesign + DMG
# or skip bundling (just the binary):
cargo build --release
```

The unsigned binary lands at `src-tauri/target/release/lion-studio`.
The signed `.app` bundle lands at `src-tauri/target/release/bundle/macos/`.

## Architecture

### Initialization script injection (window.__STUDIO_API_BASE__)

The main window is created with `WebviewWindowBuilder::initialization_script()`
(Tauri 2.5+ API). This script runs synchronously in every new document, before
any page scripts. It reads the port from the URL hash fragment (`#port=N`) and
sets `window.__STUDIO_API_BASE__`. The SPA's `lib/api.ts::resolveApiBase()` reads
this global at module-evaluation time.

Navigation sequence:
1. Window opens on `index.html` with `visible: false`
2. Shell writes the loading screen HTML via `document.write()` and shows window
3. Backend launches; health poll waits up to 30 s
4. On success: `win.navigate("tauri://localhost/index.html#port=N")` — INIT_SCRIPT
   fires again in the new document, sets `__STUDIO_API_BASE__` before SPA loads
5. On failure: shell evals `window.__showSetupScreen()` — error + Retry button appear

### Process management

`li studio --no-frontend --port N` is spawned with `.process_group(0)` (unix),
making the child the leader of a new process group. On app exit (or window close),
`BackendHandle::terminate()` sends `SIGTERM` to the group (`kill(-pgid, SIGTERM)`),
waits 5 s, then `SIGKILL`s the group. This kills `uvicorn` workers and any other
grandchild processes.

Backend stdout/stderr are appended to rotating log files in the Tauri log
directory (`~/Library/Logs/ai.lionagi.studio/studio-backend-{stdout,stderr}.log`).

### CLI search order

1. `LIONAGI_CLI` env var
2. `which li` via PATH
3. `~/.local/bin/li`
4. `~/.cargo/bin/li`
5. `/opt/homebrew/bin/li`
6. `/usr/local/bin/li`

### macOS window configuration (DESIGN.md §5)

- `titleBarStyle: Overlay` — traffic lights float; top edge draggable
- `hiddenTitle: true`
- Min size: 1100 × 720; default: 1440 × 900
- Background color `#0C0D10` — eliminates flash-of-white on resize
- `macOSPrivateApi: true` — required for `titleBarStyle: Overlay`
- Note: the SPA must add `-webkit-app-region: drag` CSS to the top rail

## Security model

### Loopback-only binding

The shell forces the backend to bind on `127.0.0.1` via `LIONAGI_STUDIO_HOST`. The
API is not reachable from other machines on the network.  A browser tab from an
external origin cannot read responses from `http://127.0.0.1:<port>` due to the
Same-Origin Policy — provided CORS is not misconfigured on the backend.

### Per-launch bearer token

At startup the shell generates a 32-hex-char token from `/dev/urandom` (16 bytes
of OS-level CSPRNG entropy).  The token is:

- Passed to the child process as `LIONAGI_STUDIO_AUTH_TOKEN`.  The FastAPI server
  enforces bearer auth on all API routes when this env var is present.
- Injected into the SPA via the Tauri initialization script as
  `window.__STUDIO_AUTH_TOKEN__` before any page scripts run.
- Attached by `lib/api.ts::fetchJson` as `Authorization: Bearer <token>` on every
  request.

A new token is generated for each app launch.  Restarting the app rotates the token.

### What a malicious local process can and cannot do

**Can**: observe that port `N` is bound on loopback (e.g. via `ss` / `lsof`), and
attempt to connect to `http://127.0.0.1:N/api/...`.  Without the bearer token every
such request will be rejected with HTTP 401.

**Cannot**: obtain the bearer token from disk — it is never persisted; it lives in
the child process environment and the Tauri webview JS heap.  Note the limit of this
model: a malicious process running *as the same user* can ultimately inspect process
state (e.g. `ps -E` on its own user's processes), so the token raises the bar and
blocks other-user/local-network access — it does not defend against an attacker
already running with your privileges.

### CORS and live streams

The webview's origin is `tauri://localhost`, so SPA→backend calls are cross-origin;
the shell spawns the backend with `CORS_ORIGINS=tauri://localhost` to allow exactly
that origin.  Live streams (session/show/signal SSE) use fetch-based subscriptions
rather than `EventSource` so the `Authorization` header rides on them too.

### Residual port-race window

`find_free_port` binds port 0, obtains the OS-assigned port, and releases the
listener.  The backend binds that port when it starts.  In the window between these
two events, another local process could claim the port.  The shell mitigates this
with two checks after health 2xx:

1. `child.try_wait()` — if the intended backend exited, the health reply came from
   an unrelated process and we fail with `ProcessExited`.
2. Authenticated `GET /api/stats` — any squatter must present the correct bearer
   token to pass this check; an unrelated service that happens to speak HTTP will
   return 401 or a non-200 response and we fail with `IdentityCheckFailed`.

The remaining attack surface requires a malicious process to (a) claim the port in
the short race window, (b) speak HTTP on `/health` returning 2xx, (c) also speak
HTTP on `/api/stats` returning 2xx with a valid bearer, which requires knowing the
token before the app starts.  This is not a realistic threat model for a local
development tool.

`--port 0` CLI-level socket hand-off (which would eliminate the race entirely) is
left for a future release.

## Tests

```bash
cd apps/studio/desktop/src-tauri
cargo test
```

Tests cover free-port finding, CLI location logic, and backend lifecycle state
machine transitions including the health-timeout leak reproducer.
