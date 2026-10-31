# lionhub.orchestration

Multi-run orchestration for lionagi: building and running fan-out and DAG flows, spawning child runs, background jobs, waiting on and stopping runs, team inboxes, notifications, schedules, and the engines and patterns that drive them. This is the one place orchestration logic lives. `li` commands, MCP verbs and Studio routes are thin front ends: they parse their input and call this package, and no orchestration logic stays in a front end. It is not the core (`Branch`, sessions and the run store stay in `lionagi`) and not the Studio application.

Import path: `lionhub.orchestration` (`lionhub/src/lionhub/orchestration/`). The draft spells the spawn slot `spwan.py`; its final name is `spawn.py`.

| Slot | Holds | Source |
|---|---|---|
| `__init__.py` | public surface: `spawn_roles`, `plan`, `fanout`, the graph builders, `grant_spawn` | `lionhub/src/lionhub/orchestration/__init__.py` |
| `patterns.py` | fan-out, plan and DAG graph builders, role spawning | `lionhub/src/lionhub/orchestration/patterns.py` |
| `engine.py` | the research, review, coding, hypothesis and planning engines | `lionhub/src/lionhub/orchestration/engines/` (kept as a package); front end `lionagi/cli/engine.py` |
| `flow.py` | flow and fan-out execution: run loop, round records, checkpoints, quiescence, finalize, harvest, manifest | `lionagi/cli/orchestrate/flow.py`, `lionagi/cli/orchestrate/fanout.py`, `lionagi/cli/orchestrate/_orchestration.py` and the helper modules beside them |
| `spwan.py` | spawning a child run; a write-capable child gets its own actor identity (final name `spawn.py`) | spawn path in `lionagi/cli/agent.py`; `lionhub/src/lionhub/serve/mcp/_child_actor.py` |
| `job.py` | background job engine: run lifecycle, terminal-state writers, per-run locking | `lionhub/src/lionhub/serve/mcp/jobs.py`, `lionhub/src/lionhub/serve/mcp/_terminal_cause.py` |
| `wait.py` | waiting on one or more runs, with a fixed stdout line format | `lionagi/cli/wait.py` |
| `kill.py` | stopping runs | `lionagi/cli/kill.py` |
| `team.py` | team inboxes and messages between roles | `lionagi/cli/team.py`, `lionagi/cli/_team_service.py` |
| `notify.py` | outcome notifications and the durable outbox that delivers them | `lionagi/cli/orchestrate/_notify.py`, `lionhub/src/lionhub/serve/mcp/_notify_hook.py`, `lionhub/src/lionhub/orchestration/dispatch/outbox.py` |
| `task.py` | new; no existing code. Reserved for task records; created with its first code | none |
| `escalate.py` | new; no existing code. Reserved for handing a blocked run to a higher authority; created with its first code | none |
| `schedule.py` | schedule declarations (versioned sets), export, scheduler state service, write-boundary validation | `lionagi/studio/services/schedule_declaration.py`, `lionagi/studio/services/schedule_export.py`, `lionagi/studio/services/scheduler_state.py`, `lionagi/studio/services/_schedule_validation.py`, the validation half of `lionagi/studio/services/schedules.py` |
| `scheduler/` | the daemon's scheduler; see `scheduler/README.md` | `lionagi/studio/scheduler/` |

Already moved here with no slot in this draft: `lionhub/src/lionhub/orchestration/prompts.py`, `lionhub/src/lionhub/orchestration/dispatch/` (the outbox) and `lionhub/src/lionhub/orchestration/work/` (work forms and rules).

## Who uses it

- `lionagi/cli/orchestrate/flow.py` and `lionagi/cli/orchestrate/fanout.py` import `plan`, `role_node_builder` and `normalize_dep_indices` from it.
- `lionagi/cli/_engine_definitions.py` names the engine classes; `lionagi/cli/dispatch.py` calls the outbox; `lionagi/cli/agent.py` uses the work forms.
- The MCP verbs, catalogued in `lionhub/src/lionhub/serve/mcp/verbs.py`, become front ends over this package.
- Studio: `lionagi/studio/services/workflow_run.py` uses the flow progress signals; `lionagi/studio/scheduler/worker.py` and `lionagi/studio/scheduler/engine.py` use the outbox.
- `li schedule` reaches the schedule slot through `lionagi/cli/machine_schedule.py`.

## Status

Already moved: `__init__.py`, `patterns.py`, `engines/`, `dispatch/`, `work/` (dev path `lionhub/src/lionhub/orchestration/`); the old `lionagi.orchestration`, `lionagi.engines`, `lionagi.dispatch` and `lionagi.work` paths keep working through 1.x with a `DeprecationWarning`. Moves next: flow, spawn, job, wait, kill, team, notify, schedule, scheduler. New code: task, escalate.
