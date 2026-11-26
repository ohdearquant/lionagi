# LionHub

LionHub contains packages built on the LionAGI core, including orchestration,
provider integrations, LNDL, and the MCP server. The current LionAGI distribution
includes both `lionagi` and `lionhub`.

## Layout

| Path (from repository root) | Contents |
| --- | --- |
| `lionhub/src/lionhub/agent/` | Agent profiles, permissions, hooks, and construction. |
| `lionhub/src/lionhub/agent/casts/` | Cast patterns, roles, modes, profiles, packs, and emission contracts. |
| `lionhub/src/lionhub/tools/` | Built-in tool interfaces and implementations. |
| `lionhub/src/lionhub/serve/cli/` | The `li` command-line interface: the entry point, the built-in command catalog, lazy command discovery, and every command module. |
| `lionhub/src/lionhub/orchestration/` | Orchestration, engines, dispatch, work forms, and scheduler services and helpers. |
| `lionhub/src/lionhub/orchestration/scheduler/` | Scheduler engine, state service, workers, signals, process execution, GitHub polling, admission, coordination, thresholds, maintenance, lifecycle, and dependency helpers. |
| `lionhub/src/lionhub/providers/` | Provider implementations and shared provider helpers. |
| `lionhub/src/lionhub/lndl/` | LNDL parsing, assembly, and middle layer. |
| `lionhub/src/lionhub/integrations/` | Optional third-party integrations: the RuVector vector store and memory store. |
| `lionhub/src/lionhub/serve/mcp/` | MCP server and its supporting modules. |
| `lionhub/src/lionhub/serve/daemon/` | Studio daemon process services: host admission, the reconnect channel, the scheduler log, stack-dump diagnostics, launch access and pairing, the daemon's route inventory, and the daemon's routes for runs, sessions, invocations, launches, schedules, identity and maintenance. |
| `lionhub/tests/lionhub_tests/` | Tests for packages that have moved into LionHub. |

## Moved imports

The compatibility table in `lionagi._moved.MOVED` maps these old import prefixes
to their current owners. More specific rows take precedence over a parent row.
Old paths are deprecated in 0.37.0 and are scheduled for removal in 2.0.0. The agent and tools paths below are deprecated in 0.38.0.

| Old import | Current import |
| --- | --- |
| `lionagi.orchestration` | `lionhub.orchestration` |
| `lionagi.engines` | `lionhub.orchestration.engines` |
| `lionagi.dispatch` | `lionhub.orchestration.dispatch` |
| `lionagi.work` | `lionhub.orchestration.work` |
| `lionagi.mcp` | `lionhub.serve.mcp` |
| `lionagi.providers` | `lionhub.providers` |
| `lionagi.lndl` | `lionhub.lndl` |
| `lionagi.tools` | `lionhub.tools` |
| `lionagi.agent` | `lionhub.agent` |
| `lionagi.casts` | `lionhub.agent.casts` |
| `lionagi.hooks.external` | `lionhub.hooks.external` |
| `lionagi.operations.lndl_middle` | `lionhub.lndl.middle` |
| `lionagi.providers._provider_errors` | `lionagi._provider_errors` |
| `lionagi.providers._secret_resolution` | `lionagi.service.accounts._secret_resolution` |
| `lionagi.providers._subagent_admission` | `lionagi.service._subagent_admission` |
| `lionagi.providers.openai._chat_schemas` | `lionagi.service.connections._chat_schemas` |

The CLI package and the shared CLI leaf modules below are deprecated at their old
paths in 0.38.0 and are scheduled for removal in 2.0.0. Each leaf module row takes
precedence over the package row.

| Old import | Current import |
| --- | --- |
| `lionagi.cli` | `lionhub.serve.cli` |
| `lionagi.cli._project` | `lionhub.orchestration.project` |
| `lionagi.cli._mcp_resolve` | `lionhub.orchestration.mcp_config` |
| `lionagi.cli._team_service` | `lionhub.orchestration.team` |
| `lionagi.cli._engine_definitions` | `lionhub.orchestration.engine_definitions` |
| `lionagi.cli._util` | `lionhub.orchestration.job` |
| `lionagi.cli._inspection` | `lionhub.orchestration.inspection` |
| `lionagi.cli._providers` | `lionhub.orchestration.spawn` |
| `lionagi.cli._orchestrate_parsing` | `lionhub.orchestration.flow.parsing` |
| `lionagi.cli._schedule_parsing` | `lionhub.orchestration.schedule.parsing` |
| `lionagi.cli._mirror_service` | `lionhub.serve.mirror` |
| `lionagi.cli._runs` | `lionhub.orchestration.runs` |
| `lionagi.cli._kill_service` | `lionhub.orchestration.kill` |
| `lionagi.cli.orchestrate._checkpoint` | `lionhub.orchestration.flow.checkpoint` |

The flow specification and checkpoint modules below are deprecated at their old paths in
0.39.0 and are scheduled for removal in 2.0.0.

| Old import | Current import |
| --- | --- |
| `lionagi._flow_spec` | `lionhub.orchestration.flow.spec` |
| `lionagi._checkpoint_version` | `lionhub.orchestration.flow.checkpoint_version` |
| `lionagi._checkpoint_v3_codec` | `lionhub.orchestration.flow.checkpoint_codec` |

The coding-command environment modules below are deprecated at their old paths in 0.39.0
and are scheduled for removal in 2.0.0.

| Old import | Current import |
| --- | --- |
| `lionagi._command_env` | `lionhub.tools.command_env` |
| `lionagi._command_env_metadata` | `lionhub.tools.command_env_metadata` |

The prompt size bound below is deprecated at its old path in 0.39.0 and is scheduled for
removal in 2.0.0.

| Old import | Current import |
| --- | --- |
| `lionagi._spec_limits` | `lionhub.orchestration.spec_limits` |

The schedule services below are deprecated at their old paths in 0.38.0
and are scheduled for removal in 2.0.0.

| Old import | Current import |
| --- | --- |
| `lionagi.studio.services._schedule_validation` | `lionhub.orchestration.schedule.validation` |
| `lionagi.studio.services.schedule_action_validation` | `lionhub.orchestration.schedule.action_validation` |
| `lionagi.studio.services.schedule_timing` | `lionhub.orchestration.schedule.timing` |
| `lionagi.studio._timezone_rules` | `lionhub.orchestration.schedule._timezone_rules` |
| `lionagi.studio.services.schedule_execution_root` | `lionhub.orchestration.schedule.execution_root` |
| `lionagi.studio.services.schedule_declaration` | `lionhub.orchestration.schedule.declaration` |
| `lionagi.studio.services.schedule_export` | `lionhub.orchestration.schedule.export` |
| `lionagi.studio.services.project_lookup` | `lionhub.orchestration.project_store` |
| `lionagi.studio.services._db` | `lionhub.orchestration.state_store` |
| `lionagi.integrations` | `lionhub.integrations` |
| `lionagi.studio._host` | `lionhub.serve.daemon._host` |
| `lionagi.studio._traceback_dump` | `lionhub.serve.daemon._traceback_dump` |
| `lionagi.studio.daemon_log` | `lionhub.serve.daemon.daemon_log` |
| `lionagi.studio.reconnect` | `lionhub.serve.daemon.reconnect` |
| `lionagi.studio.services._io` | `lionhub.serve.daemon.services._io` |
| `lionagi.studio.services._path_safety` | `lionhub.serve.daemon.services._path_safety` |
| `lionagi.studio.services._sse` | `lionhub.serve.daemon.services._sse` |
| `lionagi.studio.services.artifact_verification` | `lionhub.serve.daemon.services.artifact_verification` |
| `lionagi.studio.services.signals` | `lionhub.serve.daemon.services.signals` |
| `lionagi.studio.services.run_view` | `lionhub.serve.daemon.services.run_view` |
| `lionagi.studio.services.task_applications` | `lionhub.serve.daemon.services.task_applications` |
| `lionagi.studio.operator.redact` | `lionhub.serve.daemon.services.redact` |
| `lionagi.studio.launch_access` | `lionhub.serve.daemon.launch_access` |
| `lionagi.studio.services.runs` | `lionhub.serve.daemon.services.runs` |
| `lionagi.studio.services.run_resume` | `lionhub.serve.daemon.services.run_resume` |
| `lionagi.studio.services.run_resume_worker` | `lionhub.serve.daemon.services.run_resume_worker` |
| `lionagi.studio.services.engine_runs` | `lionhub.serve.daemon.services.engine_runs` |
| `lionagi.studio.services.invocations` | `lionhub.serve.daemon.services.invocations` |
| `lionagi.studio.services.launches` | `lionhub.serve.daemon.services.launches` |
| `lionagi.studio.services.identity` | `lionhub.serve.daemon.services.identity` |
| `lionagi.studio.services.pairing` | `lionhub.serve.daemon.services.pairing` |
| `lionagi.studio.services.sessions` | `lionhub.serve.daemon.services.sessions` |
| `lionagi.studio.services.admin` | `lionhub.serve.daemon.services.admin` |
| `lionagi.studio.services.schedules` | `lionhub.serve.daemon.services.schedules` |
| `lionagi.studio.services._session_tail` | `lionhub.serve.daemon.services._session_tail` |

The Studio application itself moved out of `lionagi` in 0.39.0, to its own import root, and its
old path is scheduled for removal in 2.0.0. Every old name under `lionagi.studio` that the rows
above do not take to `lionhub` resolves under the new root, apart from `lionagi.studio`,
`lionagi.studio.services` and `lionagi.studio.operator`: those parent packages stay in `lionagi`,
so the old paths above import without the application.

| Old import | Current import |
| --- | --- |
| `lionagi.studio` | `lionagi_studio` |

## Logger names

Moved modules keep their original logger names so existing log handlers and filters
still match. New modules use `logging.getLogger(__name__)` by default and follow an
existing logging namespace where their package documents one. The scheduler documents
its namespace in the [orchestration README](src/lionhub/orchestration/README.md).

## Tests

From the repository root, install the project dependencies and run:

```sh
uv sync --all-extras
uv run pytest lionhub/tests
```

Some integration tests exercise optional dependencies or local subprocesses.
Tests that also cover core behavior remain under the repository's `tests/` tree.
Moved orchestration tests live beside their modules under
`lionhub/tests/lionhub_tests/orchestration/`; the schedule tests are in its
`schedule/` subdirectory, and the state store tests are in `test_state_store.py`.
The LionHub tests currently depend on the repository's `tests/` package: they
share `tests/_shared_conftest.py` and supporting fixtures and helpers under
`tests/`, including `tests/providers/` and `tests/mcp/`. Run them from a complete
repository checkout with both test trees
available. A separately distributed LionHub test suite will need those shared
fixtures and helpers supplied or moved with it.
