# Den

Watch your agents work. Den is a native VS Code client that shows your
`lionagi` agent runs and Claude Code sessions live in the editor, as they
happen.

## What It Is

Den runs a small local backend (`python -m lionagi.studio`) and talks to it
over `localhost` (`/api/*` + SSE). Everything you see is native VS Code: a tree
of runs in the activity bar, commands, and webview panels that stream output
live. There is no web app and no SPA embedded. The backend stays on your
machine and nothing leaves your workstation.

Two things show up in the Runs tree:

- **lionagi runs** — anything you launch with `li agent`, `li o`, or the
  **Den: Run Agent…** command, grouped by project and streamed live.
- **Claude Code sessions** — your local Claude Code transcripts, mirrored into
  the same tree so every agent you run lands in one place.

## The three surfaces

Den is built around three things you do with a run, all without leaving the editor:

1. **Launch** — start an Agent or an orchestrated Flow from **Den: Run**; the live
   output attaches in a panel as the run starts.
2. **Observe** — open **Den: View Run Tree** on any run to see its branch/agent
   DAG with typed nodes and per-run cost, refreshed as it progresses.
3. **Control** — cancel an in-flight run, or retry a finished one with its original
   parameters, straight from the inline buttons on each run row.

## Requirements

- VS Code 1.90 or later (or Cursor / VSCodium / Windsurf with Open VSX)
- Python 3.10+ with `lionagi[studio]` installed:

```bash
pip install 'lionagi[studio]'
```

## Quick Start

1. Install Den from the Marketplace (or Open VSX).
2. Open the **Den** panel in the activity bar (left sidebar).
3. Den auto-starts the local backend on activation (configurable).
4. Use **Den: Run Agent…** to trigger a run, then watch it live in the Runs
   tree. Claude Code sessions appear automatically as you use them.

## Settings

| Setting | Default | Description |
|---|---|---|
| `den.url` | `""` | Attach URL for an already-running backend. Leave empty to auto-spawn. |
| `den.pythonPath` | `""` | Python interpreter path. Leave empty to auto-detect (workspace `.venv`, then `uv`, then `python3`). |
| `den.port` | `8765` | Backend port when spawning. |
| `den.host` | `"127.0.0.1"` | Backend host when spawning. |
| `den.autoStart` | `true` | Spawn the backend on extension activation. |
| `den.authToken` | `""` | Bearer token (`LIONAGI_STUDIO_AUTH_TOKEN`). |

## Features

- **Backend lifecycle**: auto-spawns `python -m lionagi.studio` on activation,
  health-checks `/health`, surfaces state (stopped / starting / running / error)
  in the status bar.
- **Attach mode**: set `den.url` to skip spawning and connect to an existing
  instance.
- **Runs explorer**: tree view over `GET /api/runs/`, grouped by project with a
  pinned **Active** group for everything currently running.
- **Launch**: **Den: Run** POSTs to `POST /api/launches/` with
  `action_kind: "agent"` or `"flow"`, then attaches the live panel.
- **Live streaming**: the run detail panel subscribes to the session SSE
  (`GET /api/sessions/{id}/stream`) and streams output as it arrives.
- **Run Tree**: **Den: View Run Tree** subscribes to the session signal stream
  (`GET /api/sessions/{id}/signals`) and renders the run's branch/agent DAG with
  typed nodes and per-run cost.
- **Cancel / Retry**: inline buttons on each run row — cancel an in-flight run via
  `POST /api/invocations/{id}/cancel`, or retry a finished one by re-POSTing its
  original launch request. Retry parameters are cached per session.
- **Claude Code mirror**: local Claude Code sessions are mirrored into the Runs
  tree and reconciled live.

## License

Apache 2.0

Den is part of [lionagi](https://github.com/ohdearquant/lionagi). If it is
useful to you, a ⭐ on the repo helps.
