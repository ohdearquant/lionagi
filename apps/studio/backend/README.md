# Studio backend

The Studio application lives here, outside the library wheels: the operator feature, the HTTP routes, the app shell and the connection to the Studio frontend. It uses `lionhub` (the daemon, the scheduler and orchestration) and the core. Nothing in `lionhub` imports it, and the core never depends on it.

It is not the daemon or the scheduler (`lionhub.serve`, `lionhub.orchestration.scheduler`), and it holds no orchestration logic: a route parses its request and calls `lionhub.orchestration`.

Source: `lionagi/studio/` minus the scheduler and daemon parts.

| Part | Holds | Source |
|---|---|---|
| `src/operator/` | the operator feature: durable operator turns, cancellation and permission proposals, the model catalog ([README](src/operator/README.md)) | `lionagi/studio/operator/` |
| routes | the HTTP route handlers, grouped by area, each registered with the `studio_route` decorator | `lionagi/studio/registry.py`, `lionagi/studio/services/` |
| app shell | the web application: route loading, CORS, launch access, and the static assets of the built frontend | `lionagi/studio/app.py` |
| settings and entry | host, port and CORS settings; the module entry point that serves the app; an opt-in traceback dump for debugging | `lionagi/studio/config.py`, `lionagi/studio/__main__.py`, `lionagi/studio/_traceback_dump.py` |
| playbooks | the built-in playbook definitions shipped with the application | `lionagi/studio/builtin_playbooks/` |

## What stays in lionhub

- The daemon pieces (host canonicalization, daemon log, same-user reconnect channel, pairing code) are `lionhub.serve`, from `lionagi/studio/_host.py`, `lionagi/studio/daemon_log.py`, `lionagi/studio/reconnect.py` and `lionagi/studio/launch_access.py`. The app shell calls them.
- The scheduler is `lionhub.orchestration.scheduler`, from `lionagi/studio/scheduler/`.
- The schedule service logic (`lionagi/studio/services/schedules.py`, `lionagi/studio/services/schedule_declaration.py`, `lionagi/studio/services/schedule_export.py`, `lionagi/studio/services/scheduler_state.py`) is the orchestration `schedule` slot; the routes here call it.
- The `li studio` and `li schedule` commands (`lionagi/studio/cli.py`) are `lionhub.serve.cli`.

## Frontend connection

The frontend in `apps/studio/frontend` talks to the routes registered here, and the app shell serves its built static assets.

## Status

Not moved yet. The application code is still under `lionagi/studio/` in the single distribution through 1.x, with its current import paths; it moves here at 2.0, outside both library wheels. How the backend attaches to the daemon is decided in `docs/adr/ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md`, which is still proposed.
