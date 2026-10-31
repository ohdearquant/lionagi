# lionhub

`lionhub` is the layer built on the lionagi core. It holds what is not a core primitive: orchestration (flows, spawning, jobs, schedules), the processes that serve lionagi (MCP server, daemon, command line), model providers, tools, agent roles, hooks, the LNDL output language and the governance concepts. It is not the core itself (`Branch`, sessions, the `iModel` class and the run store stay in `lionagi`) and not the Studio application, which lives in `apps/studio/backend`.

## Layering and versions

- `lionhub` ships in the same distribution as `lionagi` through 1.x.
- `lionhub` imports the core freely. The core reaches `lionhub` only lazily, through a module path in a lookup table or an import inside a function, never at module import time, so `import lionagi` alone loads nothing from `lionhub`.
- 1.x stays fully backward compatible with 0.x. Every moved `lionagi.*` import path keeps working through 1.x, as the same module object, with a `DeprecationWarning` naming the new path. 2.0 removes the old paths.
- The orchestration nouns the CLI and Studio use today (flow, spawn, job, wait, kill, team, notify, schedule, task, escalate, engine, patterns) have their logic in `lionhub.orchestration`. `li` commands, MCP verbs and Studio routes are thin front ends: they parse their input and call orchestration, and no orchestration logic stays in a front end.
- The Studio application (operator, routes, app shell) is not part of `lionhub`; it goes to `apps/studio/backend`. The daemon and its scheduler are `lionhub` (`serve/daemon.py`, `orchestration/scheduler/`).

## Import paths

Every folder in this draft becomes `lionhub/src/lionhub/<folder>` and is imported as `lionhub.<folder>`. The folder `integerations` is spelled `integrations` in its final path, and the slot `spwan.py` is spelled `spawn.py`; each README says so where it applies.

The layout and the compatibility plan are recorded in `docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md`.

## Folders

Each README gives a table of the folder's slots, where the code comes from, who uses it, and its status (already moved, moves next, or new code). `orchestration` (patterns, prompts, engines, dispatch, work) and `serve/mcp` are already moved from the core.

- [`agent`](src/agent/README.md): the agent specification, the factory that builds a branch from it, permission policy, and the roles an agent can take.
- [`canon`](src/canon/README.md): the governance concepts as new code: charter, policy, evidence, enforcement and authorization, with `runtime/`, `dsl/` and `services/` beneath.
- [`hooks`](src/hooks/README.md): the external-hook executor: it runs a configured command on session and tool events and turns the result into a decision, with `scripts/` beneath.
- [`integrations`](src/integerations/README.md): optional adapters to external storage, memory and collaboration systems (the draft folder is `integerations`).
- [`lndl`](src/lndl/README.md): LNDL, the structured output language for model responses: lexer, parser, assembler and prompt.
- [`orchestration`](src/orchestration/README.md): flows, spawn, jobs, wait, kill, teams, notifications, schedules, engines and patterns, with the `scheduler/` beneath.
- [`providers`](src/providers/README.md): vendor endpoints for model and search providers.
- [`resources`](src/resources/README.md): reserved for resource code beyond the plugin runtime.
- [`serve`](src/serve/README.md): the MCP server, the daemon, the command-line entry points and the HTTP server.
- [`tools`](src/tools/README.md): tools a model can call beyond the core's file and shell tools: context, skill, memory, web, watch and computer.
