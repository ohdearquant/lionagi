# Studio backend: operator

The operator feature of the Studio application: durable operator turns, cancellation and permission proposals, the model catalog, bounded read tools over runs, human-approved run controls and the store behind them. It is not part of lionhub; it uses the core and `lionhub` (orchestration) and nothing in lionhub imports it. Source: `lionagi/studio/operator/`.

| Module | Holds | Source |
|---|---|---|
| `__init__.py` | package marker (the only file in this tree today) | `lionagi/studio/operator/__init__.py` |
| `_common.py` | shared operations for durable operator tools | `lionagi/studio/operator/_common.py` |
| `application_mcp.py` | strict stdio MCP bridge: bounded read tools, UI effects, a launch proposal | `lionagi/studio/operator/application_mcp.py` |
| `cancel_run.py` | cancel a run by reference after a human allow or deny decision | `lionagi/studio/operator/cancel_run.py` |
| `catalog.py` | models, their providers and the reasoning-effort levels each accepts | `lionagi/studio/operator/catalog.py` |
| `conversation_metadata.py` | validated presentation metadata for an operator conversation | `lionagi/studio/operator/conversation_metadata.py` |
| `coordinator.py` | durable turns, cancellation and permissions | `lionagi/studio/operator/coordinator.py` |
| `engine.py` | the default Branch-backed engine for operator turns | `lionagi/studio/operator/engine.py` |
| `permission_mcp.py` | dependency-free stdio MCP permission bridge | `lionagi/studio/operator/permission_mcp.py` |
| `redact.py` | shared bounds and redaction for the read tools | `lionagi/studio/operator/redact.py` |
| `rename_session.py` | rename adapter, exposed as the `rename_run` tool | `lionagi/studio/operator/rename_session.py` |
| `resume_run.py` | resume adapter over the run-resume surface | `lionagi/studio/operator/resume_run.py` |
| `run_control.py` | human-approved adapters for live run controls | `lionagi/studio/operator/run_control.py` |
| `run_detail.py` | read tool: the full projection of one run | `lionagi/studio/operator/run_detail.py` |
| `run_findings.py` | read tool: what a run produced | `lionagi/studio/operator/run_findings.py` |
| `run_progress.py` | read tool and the run-reference resolver | `lionagi/studio/operator/run_progress.py` |
| `store.py` | SQLite-backed durable store for the operator protocol | `lionagi/studio/operator/store.py` |
| `types.py` | wire and engine-side types | `lionagi/studio/operator/types.py` |

## Who uses it

- `lionagi/studio/services/operator.py` imports the coordinator, store, types and catalog; `lionagi/studio/app.py` starts and stops it through that service.
- `lionagi/studio/services/sessions.py` imports `run_control`; `lionagi/studio/services/engine_runs.py` imports `redact`.
- `lionagi/studio/operator/engine.py` starts `permission_mcp` and `application_mcp` as stdio helper processes by module path, so those launch arguments change when the package moves.
- `cancel_run.py` reuses the run lookup in `lionagi/cli/_util.py`.

## Status

Not moved yet; the code is at `lionagi/studio/operator/` and moves with the Studio backend.
