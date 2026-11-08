# LionHub

LionHub contains packages built on the LionAGI core, including orchestration,
provider integrations, LNDL, and the MCP server. The current LionAGI distribution
includes both `lionagi` and `lionhub`.

## Layout

| Path (from repository root) | Contents |
| --- | --- |
| `lionhub/src/lionhub/cli/` | Built-in CLI command catalog and lazy command discovery. |
| `lionhub/src/lionhub/orchestration/` | Orchestration, engines, dispatch, and work forms. |
| `lionhub/src/lionhub/providers/` | Provider implementations and shared provider helpers. |
| `lionhub/src/lionhub/lndl/` | LNDL parsing, assembly, and middle layer. |
| `lionhub/src/lionhub/serve/mcp/` | MCP server and its supporting modules. |
| `lionhub/tests/lionhub_tests/` | Tests for packages that have moved into LionHub. |

## Moved imports

The compatibility table in `lionagi._moved.MOVED` maps these old import prefixes
to their current owners. More specific rows take precedence over a parent row.
Old paths are deprecated in 0.37.0 and are scheduled for removal in 2.0.0.

| Old import | Current import |
| --- | --- |
| `lionagi.orchestration` | `lionhub.orchestration` |
| `lionagi.engines` | `lionhub.orchestration.engines` |
| `lionagi.dispatch` | `lionhub.orchestration.dispatch` |
| `lionagi.work` | `lionhub.orchestration.work` |
| `lionagi.mcp` | `lionhub.serve.mcp` |
| `lionagi.providers` | `lionhub.providers` |
| `lionagi.lndl` | `lionhub.lndl` |
| `lionagi.operations.lndl_middle` | `lionhub.lndl.middle` |
| `lionagi.providers._provider_errors` | `lionagi._provider_errors` |
| `lionagi.providers._secret_resolution` | `lionagi.service.accounts._secret_resolution` |
| `lionagi.providers._subagent_admission` | `lionagi.service._subagent_admission` |
| `lionagi.providers.openai._chat_schemas` | `lionagi.service.connections._chat_schemas` |

The shared CLI leaf modules below are deprecated at their old paths in 0.38.0
and are scheduled for removal in 2.0.0.

| Old import | Current import |
| --- | --- |
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

## Tests

From the repository root, install the project dependencies and run:

```sh
uv sync --all-extras
uv run pytest lionhub/tests
```

Some integration tests exercise optional dependencies or local subprocesses.
Tests that also cover core behavior remain under the repository's `tests/` tree.
The LionHub tests currently depend on the repository's `tests/` package: they
share `tests/_shared_conftest.py` and supporting fixtures and helpers under
`tests/`, including `tests/providers/` and `tests/mcp/`. Run them from a complete
repository checkout with both test trees
available. A separately distributed LionHub test suite will need those shared
fixtures and helpers supplied or moved with it.
