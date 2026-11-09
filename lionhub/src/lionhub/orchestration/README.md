# lionhub.orchestration

Multi-run orchestration for lionagi: fan-out and DAG flows, spawning child runs, background jobs,
stopping runs, team inboxes, the dispatch outbox, the engines, and the schedule services. `li`
commands, MCP verbs and Studio routes are front ends that parse their input and call this package.
The core (`Branch`, sessions and the run store) stays in `lionagi`.

Importing `lionhub.orchestration` loads none of its submodules. The parent package resolves its
exported functions on first use, and `lionhub/tests/lionhub_tests/orchestration/test_parent_laziness.py`
keeps it that way. Import the module you need by its full path.

## Modules

| Module | Holds |
| --- | --- |
| `patterns` | `spawn_roles`, `plan`, `fanout` and the DAG and fan-out graph builders |
| `prompts` | prompt fragments for planning, synthesis, spawn guidance and decomposition |
| `engines/` | the research, review, coding, hypothesis and planning engines |
| `engine_definitions` | the engine kind registry and class loader |
| `flow/` | flow argument parsing (`flow.parsing`) and checkpoints for cross-process resume (`flow.checkpoint`) |
| `spawn` | model construction helpers for spawned runs |
| `mcp_config` | which MCP servers a spawned run is given, resolved at submit time |
| `runs` | the run directory layout: state under `LIONAGI_HOME/runs/{run_id}/`, artifacts beside it |
| `job` | exit-code mapping, exception classification, process liveness and entity resolution |
| `kill` | process identity, signalling and cleanup for stopping runs |
| `inspection` | lookups shared by the read-only lifecycle commands |
| `team` | team files and the messages between roles |
| `project` | project detection: config file, global overrides, git remote |
| `project_store` | `get_project`: reads a stored project record from the run store |
| `state_store` | the run store file: `store_path`, `store_exists`, `require_file_store`, `open_db`, `table_columns` |
| `dispatch/` | the durable dispatch outbox and its revival heartbeat |
| `work/` | `WorkForm` and its declarative validation rules |
| `arguments`, `console` | argparse type callables and the named console output channels |
| `schedule/` | the schedule services, below |

## schedule

The schedule services hold everything about a schedule that does not need the HTTP server or the
scheduler daemon: parsing `li schedule` arguments, validating declarations and actions, resolving
the scheduler's timezone and an action's working directory, and declaring, diffing, applying and
exporting versioned schedule sets. The Studio routes, the `li schedule` commands and the scheduler
engine all call these functions.

| Module | Holds |
| --- | --- |
| `schedule.parsing` | `li schedule` argument parsing and request-body building |
| `schedule.validation` | shared validation for schedule declarations and service requests |
| `schedule.action_validation` | validation of scheduled action fields and launch configuration |
| `schedule.timing` | the scheduler's timezone resolution and cron timing |
| `schedule._timezone_rules` | bounded TZif evidence for identifying the host timezone (private, `__all__ = ()`) |
| `schedule.execution_root` | the working directory a scheduled action runs in |
| `schedule.declaration` | `ScheduleSet` models and the validate, diff and apply service |
| `schedule.export` | read-only conversion of stored schedule rows into a `ScheduleSet` |

Three moved modules keep the logger names they had before the move, so existing log filters keep
matching: `state_store` logs as `lionagi.studio.services._db`, `schedule.timing` as
`lionagi.studio.config`, and `schedule.timing` and `schedule.execution_root` log scheduler decisions
as `lionagi.studio.scheduler.engine`.

## Moved here

These old import paths still work through 1.x. Importing one emits a `DeprecationWarning` and
returns the module at its new path. Each old path is removed in 2.0.0. The rows are the entries in
`lionagi/_moved.py` whose new path is in this package.

| Old import | Current import | Deprecated in |
| --- | --- | --- |
| `lionagi.orchestration` | `lionhub.orchestration` | 0.37.0 |
| `lionagi.engines` | `lionhub.orchestration.engines` | 0.37.0 |
| `lionagi.dispatch` | `lionhub.orchestration.dispatch` | 0.37.0 |
| `lionagi.work` | `lionhub.orchestration.work` | 0.37.0 |
| `lionagi.cli._argtypes` | `lionhub.orchestration.arguments` | 0.38.0 |
| `lionagi.cli._logging` | `lionhub.orchestration.console` | 0.38.0 |
| `lionagi.cli._project` | `lionhub.orchestration.project` | 0.38.0 |
| `lionagi.cli._mcp_resolve` | `lionhub.orchestration.mcp_config` | 0.38.0 |
| `lionagi.cli._team_service` | `lionhub.orchestration.team` | 0.38.0 |
| `lionagi.cli._engine_definitions` | `lionhub.orchestration.engine_definitions` | 0.38.0 |
| `lionagi.cli._util` | `lionhub.orchestration.job` | 0.38.0 |
| `lionagi.cli._inspection` | `lionhub.orchestration.inspection` | 0.38.0 |
| `lionagi.cli._providers` | `lionhub.orchestration.spawn` | 0.38.0 |
| `lionagi.cli._orchestrate_parsing` | `lionhub.orchestration.flow.parsing` | 0.38.0 |
| `lionagi.cli._schedule_parsing` | `lionhub.orchestration.schedule.parsing` | 0.38.0 |
| `lionagi.cli._runs` | `lionhub.orchestration.runs` | 0.38.0 |
| `lionagi.cli._kill_service` | `lionhub.orchestration.kill` | 0.38.0 |
| `lionagi.cli.orchestrate._checkpoint` | `lionhub.orchestration.flow.checkpoint` | 0.38.0 |
| `lionagi.studio.services._schedule_validation` | `lionhub.orchestration.schedule.validation` | 0.38.0 |
| `lionagi.studio.services.schedule_action_validation` | `lionhub.orchestration.schedule.action_validation` | 0.38.0 |
| `lionagi.studio.services.schedule_timing` | `lionhub.orchestration.schedule.timing` | 0.38.0 |
| `lionagi.studio._timezone_rules` | `lionhub.orchestration.schedule._timezone_rules` | 0.38.0 |
| `lionagi.studio.services.schedule_execution_root` | `lionhub.orchestration.schedule.execution_root` | 0.38.0 |
| `lionagi.studio.services.schedule_declaration` | `lionhub.orchestration.schedule.declaration` | 0.38.0 |
| `lionagi.studio.services.schedule_export` | `lionhub.orchestration.schedule.export` | 0.38.0 |
| `lionagi.studio.services.project_lookup` | `lionhub.orchestration.project_store` | 0.38.0 |
| `lionagi.studio.services._db` | `lionhub.orchestration.state_store` | 0.38.0 |

## Tests

Tests of this package live in `lionhub/tests/lionhub_tests/orchestration/`, with the schedule
services under its `schedule/` directory and the run store in `test_state_store.py`. Tests that
also exercise the Studio application, its HTTP routes, the `li` commands, the scheduler engine or
its subprocess runner stay under `tests/`.
