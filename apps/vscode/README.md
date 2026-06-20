# Lion Studio VS Code Extension

A native VS Code client for the Lion Studio FastAPI backend. Manage and observe
`lionagi` agent runs directly from your editor, without leaving the IDE.

## What It Is

This extension talks to the public Lion Studio API (`/api/*` + SSE) over a
local HTTP connection. It does **not** embed the Studio web SPA. All UI is
native VS Code: tree views, commands, webviews, codicons. The backend stays on
your machine; no data leaves your workstation.

## Requirements

- VS Code 1.90 or later (or Cursor / VSCodium / Windsurf with Open VSX)
- Python 3.10+ with `lionagi[studio]` installed:

```bash
pip install 'lionagi[studio]'
```

## Quick Start

1. Install the extension from the Marketplace (or Open VSX).
2. Open the Lion Studio panel in the activity bar (left sidebar).
3. The extension auto-starts the backend on activation (configurable).
4. Use **Lion Studio: Run Agent...** to trigger a run, then watch it live in
   the Runs tree.

## Settings

| Setting | Default | Description |
|---|---|---|
| `lionStudio.url` | `""` | Attach URL for an already-running backend. Leave empty to auto-spawn. |
| `lionStudio.pythonPath` | `"python3"` | Python interpreter path. |
| `lionStudio.port` | `8765` | Backend port when spawning. |
| `lionStudio.host` | `"127.0.0.1"` | Backend host when spawning. |
| `lionStudio.autoStart` | `true` | Spawn the backend on extension activation. |
| `lionStudio.authToken` | `""` | Bearer token (`LIONAGI_STUDIO_AUTH_TOKEN`). |

## v0 Features

- **Backend lifecycle**: auto-spawns `python -m lionagi.studio` on activation,
  health-checks `/health`, surfaces state (stopped / starting / running / error)
  in the status bar.
- **Attach mode**: set `lionStudio.url` to skip spawning and connect to an
  existing instance.
- **Runs explorer**: tree view over `GET /api/runs/` (paginated).
- **Run Agent**: command palette / toolbar button that POSTs to
  `POST /api/launches/` with `action_kind: "agent"`.
- **Live streaming**: run detail view subscribes to the session SSE
  (`GET /api/sessions/{id}/stream`) for live output.

## License

Apache 2.0
