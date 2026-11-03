# LionHub

LionHub contains packages built on the LionAGI core, including orchestration,
provider integrations, LNDL, and the MCP server. The current LionAGI distribution
includes both `lionagi` and `lionhub`.

## Layout

| Path | Contents |
| --- | --- |
| `src/lionhub/orchestration/` | Orchestration, engines, dispatch, and work forms. |
| `src/lionhub/providers/` | Provider implementations and shared provider helpers. |
| `src/lionhub/lndl/` | LNDL parsing, assembly, and middle layer. |
| `src/lionhub/serve/mcp/` | MCP server and its supporting modules. |
| `tests/lionhub_tests/` | Tests for packages that have moved into LionHub. |

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

## Tests

From the repository root, install the project dependencies and run:

```sh
uv sync --all-extras
uv run pytest lionhub/tests
```

Some integration tests exercise optional dependencies or local subprocesses.
Tests that also cover core behavior remain under the repository's `tests/` tree.
