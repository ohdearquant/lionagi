# lionhub.serve

The processes that serve lionagi to other programs and to people: the MCP server, the local daemon (host canonicalization, pairing, reconnect, scheduler start-up, health), the daemon's command-line entry points, and the HTTP server that carries them. Everything here is a front end: it parses input and calls `lionhub.orchestration`, and no orchestration logic stays in it. It is not the Studio application (operator, routes, app shell), which lives in `apps/studio/backend` and uses this package.

Import path: `lionhub.serve` (`lionhub/src/lionhub/serve/`). On the development line the draft's single `mcp.py` slot is the package `lionhub.serve.mcp`.

| Slot | Holds | Source |
|---|---|---|
| `__init__.py` | package docstring: the processes served from here | `lionhub/src/lionhub/serve/__init__.py` |
| `mcp.py` | the MCP server: verb catalog, background jobs, dispatch, run projections, roster, config | `lionhub/src/lionhub/serve/mcp/` (`server.py`, `verbs.py`, `jobs.py`, `dispatch.py`, `projection.py`, `roster.py`, `config.py`) |
| `daemon.py` | the local daemon: host canonicalization, daemon log, same-user reconnect channel, one-time pairing code, scheduler start-up; split from the Studio app shell | `lionagi/studio/_host.py`, `lionagi/studio/daemon_log.py`, `lionagi/studio/reconnect.py`, `lionagi/studio/launch_access.py`, the daemon part of `lionagi/studio/app.py` |
| `cli.py` | the `li studio` and `li schedule` commands and HTTP client helpers | `lionagi/studio/cli.py` |
| `server.py` | the HTTP server: a loopback-only app over a service object that holds offers and runs and knows no HTTP | new code, from the concept in `lion1/hub/serve/transport.py` and `lion1/hub/serve/service.py` |

## Who uses it

- `lionagi/cli/mcp.py` starts the MCP server; `lionagi/cli/_mcp_resolve.py` and `lionagi/cli/_code_identity.py` import its error type and verb catalog.
- `lionagi/cli/agent.py` imports its config and terminal-cause helpers; `lionagi/studio/services/admin.py` imports its config.
- `lionagi/cli/main.py` loads `lionagi.studio.cli` lazily; `lionagi/cli/machine_schedule.py` imports its `_base_url` helper.
- The Studio application starts the daemon pieces from `lionagi/studio/app.py`.

## Status

`mcp` is already moved (dev path `lionhub/src/lionhub/serve/mcp/`); the old `lionagi.mcp` path keeps working through 1.x with a `DeprecationWarning`. `daemon.py` and `cli.py` move next; a separate record decides the CLI's public path and who owns `li` at 2.0 before `cli.py` moves. `server.py` is new code.
