
# AGENT.md

Practical guide for all coding agents working in the LionAGI repository.

## Mission

LionAGI is an async-first SDK for building AI workflows: structured operations, tool calling, graph execution.

Make minimal, correct changes. Preserve API behavior unless explicitly requested. Update tests and docs with every behavioral change.

## Commands

```bash
uv sync --all-extras                        # Install all deps (NEVER use pip)
uv run pytest                               # Run all tests (parallel, -n auto)
uv run pytest tests/path.py -v              # Run specific test file
uv run pytest tests/path.py::test_func -v   # Run specific test function
uv run pytest -m unit                       # By marker; see pyproject.toml for the full marker list
uv run pytest -n0 -s tests/path.py          # Debug: no parallelism, show stdout
uv run pytest --cov=lionagi                 # With coverage
uv run ruff format . && uv run ruff check --fix .  # Format + autofix lint
pre-commit run -a                           # All hooks (file sanity, ruff, pyupgrade, markdownlint, etc.)
uv build                                    # Build wheel
```

CI tests Python 3.10 and 3.14 on PRs, and 3.10-3.14 on `main`/`develop` pushes. Async mode is auto-detected.

## Repository Map

- `lionagi/session/` — Branch, session orchestration
- `lionagi/operations/` — chat, parse, operate, ReAct, run, act; `Middle` dispatch
- `lionagi/protocols/` — core types: messages, graph, generic, action
- `lionagi/service/` — iModel, provider connections, rate limiting
- `lionagi/ln/` — concurrency utils (alcall, bcall), fuzzy JSON, sentinels
- `lionagi/tools/` — tool interfaces and built-ins
- `lionagi/cli/` — `li` command; `cli/orchestrate/` (flow, fanout); `cli/_runs.py` (RunDir); `cli/_logging.py`; `cli/team.py`; `cli/schedule.py` (`li schedule`); `cli/_project.py` (project detection)
- `tests/` — mirrors package structure
- `benchmarks/` — micro-benchmark runners and baselines

## Architecture Invariants

- `Branch` composes five managers: MessageManager, ActionManager, iModelManager, DataLogger, OperationManager.
- `branch.operate()`: CLI endpoints stream (via `run_and_collect`); API endpoints one-shot (via `communicate`). Override with `middle=<callable>` or force streaming with `stream_persist=True`.
- `Session.flow()`: multiple ops with the same `branch=` reference reuse the Branch without cloning — this is the CLI two-level flow pattern.

When editing `session/`, `operations/`, or `cli/orchestrate/`: preserve these contracts unless the task explicitly changes behavior with tests + docs.

## Testing Strategy by Change Type

- `lionagi/session/*` → `tests/session/*`
- `lionagi/operations/*` → `tests/operations/*` and related `tests/operatives/*`
- `lionagi/protocols/*` → `tests/protocols/*`
- `lionagi/service/*` → `tests/service/*`
- `lionagi/ln/*` → `tests/libs/concurrency/*`, `tests/ln/*`
- `lionagi/cli/*` → smoke with `li o flow ... --dry-run` for structure or `--bare --yolo` for a short real run; add smoke assertions in `tests/docs/` when changing user-facing output shape.

CLI has no dedicated unit test suite.

## Coding Standards

- Line length: 100 chars (`ruff format` + `ruff check`; `[tool.ruff]` in `pyproject.toml` is the source of truth). Target `py310`.
- Ruff lint selects `E F W B I UP N S A` (incl. bugbear, isort, pyupgrade, naming, bandit).
- New or materially changed `.py` files under `lionagi/` should keep/add the Apache-2.0 SPDX header, `from __future__ import annotations`, and an `__all__` tuple for public surface.
- Reuse existing abstractions before creating new ones — `lionagi.ln` (`alcall`, `bcall`, `race`, `retry`, `fuzzy_json`, `json_dumps`, sentinels), `Pile`/`Progression`/`Element`, `iModel`. Don't fork near-duplicates.
- Prefer LionAGI-native primitives over naked stdlib/third-party calls when a local helper exists. Examples: `alcall`/`bcall` over raw gather loops, `json_dumps`/`fuzzy_json` over direct `json` on model/provider payloads, `now_utc`/`to_uuid` over ad hoc time/UUID handling. Raw stdlib is fine at process boundaries when no LionAGI abstraction applies.
- Keep code async-safe; avoid blocking calls in async execution paths.
- Follow existing typing patterns; add type hints on new/changed public APIs.
- Keep changes surgical: do not refactor unrelated modules in the same patch.
- Preserve backward compatibility unless the request explicitly allows breaking changes.

## Common Pitfalls

- Forgetting `await` in async flows.
- Introducing sync I/O inside hot async paths.
- Changing `Branch` defaults or message behavior without test updates.
- Adding provider-specific assumptions to generic protocol/operation layers.

## Change Workflow

1. Read adjacent code and existing tests.
2. Implement minimal patch.
3. Add/update tests to protect behavior.
4. Run focused tests first; then `uv run pytest`.
5. Update docs for user-visible changes.

## Agent Development

Use the `lionagi/agent/` module to build sandboxed, permission-aware coding agents.

**Create a coding agent**

```python
import asyncio
from lionagi.agent.spec import AgentSpec
from lionagi.agent.factory import create_agent

async def main():
    spec = AgentSpec.coding()              # CodingToolkit + guard hooks + workspace path policy
    agent = await create_agent(spec)       # returns a wired Branch
    reply = await agent.communicate("Refactor auth.py to use async/await throughout.")
    print(reply)

asyncio.run(main())
```

**Register custom hooks**

```python
from lionagi.agent.hooks import guard_paths, log_tool_use
from lionagi.agent.spec import AgentSpec

spec = AgentSpec.coding()
spec.pre("reader", guard_paths(allowed_paths=["/tmp/sandbox", "./src"]))
spec.post("*", log_tool_use)
agent = await create_agent(spec)
```

**Use Sandbox for isolated edits**

```python
from lionagi.tools.sandbox import create_sandbox, sandbox_diff, sandbox_commit, sandbox_merge

session = await create_sandbox(repo_root="/path/to/repo", base_branch="main")
# agent edits happen inside the worktree at session.worktree_path
spec = AgentSpec.coding(cwd=session.worktree_path)
agent = await create_agent(spec)
await agent.communicate("Add type hints to all public functions in auth.py.")
print(await sandbox_diff(session))         # inspect changes before committing
await sandbox_commit(session, "feat: add type hints to auth module")
await sandbox_merge(session)               # merge into base; or sandbox_discard(session)
```

**Permission policies**

```python
from lionagi.agent.permissions import PermissionPolicy

# Allow-all mode: everything permitted (default for orchestrators)
policy = PermissionPolicy(mode="allow_all")

# Deny-all mode: nothing permitted (safe mode)
policy = PermissionPolicy(mode="deny_all")

# Rules mode: per-tool allow/deny/escalate patterns
policy = PermissionPolicy(
    mode="rules",
    allow={"reader": ["*"], "search": ["*"]},
    deny={"bash": ["rm *"]},
    escalate={"bash": ["*"]},
)

spec = AgentSpec.coding()
spec.permissions = {"mode": "rules", "allow": {"reader": ["*"]}, "deny": {"bash": ["rm *"]}}
```

**Settings** — place `.lionagi/settings.yaml` in the project root to override defaults. Global settings live at `~/.lionagi/settings.yaml`; project settings win on conflict.

**Project identity** — place `.lionagi/config.toml` in the repo root (committed, no secrets) to declare the project name:

```toml
[project]
name = "lionagi"
github = "ohdearquant/lionagi"
```

This is separate from `settings.yaml` (which is gitignored/local). The detection cascade at session creation (`lionagi/cli/_project.py`):

1. Walk up from cwd → read `.lionagi/config.toml` → `[project].name`
2. Check `project_overrides` in `~/.lionagi/settings.yaml` (key = `org/repo` remote or absolute path prefix)
3. Parse git remote URL → derive `org/repo` as fallback
4. Non-git directory → `null` (shown as "Unassigned" in Studio)

## Scheduled Runs

Lion Studio can fire agent work on a schedule. The scheduler engine runs in-process inside the Studio server (`apps/studio/server/scheduler/engine.py`) and ticks every 30 seconds.

**CLI** (`lionagi/cli/schedule.py`):

```bash
li schedule list                                      # List all schedules
li schedule create nightly --trigger cron \
    --cron "0 0 * * *" --action play \
    --playbook perf-opt                               # Cron schedule
li schedule create pr-review --trigger github \
    --repo ohdearquant/lionagi --poll 300 \
    --action flow --model claude/sonnet \
    --prompt "Review PR #{{pr_number}}"               # GitHub poll schedule
li schedule enable <name>                             # Enable
li schedule disable <name>                            # Disable (no delete)
li schedule trigger <name>                            # Fire immediately
li schedule delete <name>                             # Remove
li schedule runs <name>                               # Execution history
```

The CLI writes directly to `state.db` — Studio server does not need to be running for CRUD. Schedules only fire while the Studio server is running.

**Trigger types**: `cron` (5-field), `interval` (seconds), `github_poll` (polls PR events, cursor-based, ETag-cached).

**Action kinds**: `agent`, `flow`, `fanout`, `play` — each maps to the corresponding `li` subcommand, spawned via `asyncio.create_subprocess_exec`.

**Conditional chains**: Each schedule action can define `on_fail` / `on_success` as nested action definitions. Exit code 0 follows `on_success`; non-zero follows `on_fail`. Chain depth capped at 10.

**Agent tools** (`lionagi/tools/schedule.py`): `schedule_create`, `schedule_cancel`, `schedule_list` — register on any Branch so agents can schedule/cancel their own follow-up work.

**New tables in `state.db`**: `schedules` (one row per definition) and `schedule_runs` (one row per firing). Each fire creates an `invocations` row; child sessions link via `LIONAGI_INVOCATION_ID` env var.
