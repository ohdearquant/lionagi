
# CLAUDE.md

Claude Code guidance. Read AGENT.md first (commands, workflow, standards). This file adds architecture depth.

## Architecture

```text
Session (multi-branch orchestrator)
  └─ Branch (single conversation thread)
       ├─ MessageManager  → Pile[RoledMessage] + Progression
       ├─ ActionManager   → Pile[Tool]
       ├─ iModelManager   → iModel (provider wrapper with rate limiting + hooks)
       └─ DataLogger      → activity logs
```

### Core Primitives (`protocols/`)

- **Element** (`protocols/generic/element.py`): UUID + timestamp + metadata. Base for all objects.
- **Pile** (`protocols/generic/pile.py`): O(1) UUID-keyed. Thread/async-safe. `pile[uuid]` not `pile[0]`.
- **Progression** (`protocols/generic/progression.py`): Ordered UUID deque, decoupled from Pile.
- **Node/Graph** (`protocols/graph/`): Node = Element + content + embedding. Graph = directed.

### Message Types (`protocols/messages/`)

```text
RoledMessage
├── System, Instruction, AssistantResponse
├── ActionRequest (tool call from LLM)
└── ActionResponse (tool result back to LLM)
```

### Session & Branch (`session/`)

**Branch** — facade over four managers, primary API surface:

- `branch.chat()` / `branch.run()` — LLM call (API) / async stream (CLI: claude_code, codex)
- `branch.parse()` — structured extraction into Pydantic models
- `branch.operate(instruction=...)` — universal op: tools, structured output, Middle routing. **`branch.instruct()` removed in 0.22.6.**
- `branch.ReAct()` — think-act-observe loops

### Middle Protocol (`operations/types.py`)

`Middle` = callable `(branch, instruction, ...) → text|dict|BaseModel` advancing branch one turn.

- `operations/communicate/communicate.py` — one-shot chat+parse (API endpoints)
- `operations/run/run.run_and_collect` — stream accumulation + parse (CLI endpoints)

Override: `branch.operate(instruction=..., middle=my_callable)`. Force stream: `stream_persist=True`.

### Service Layer (`service/`)

**iModel** wraps any provider via `match_endpoint.py`. Providers in `connections/providers/`: OpenAI, Anthropic, Gemini, Ollama, NVIDIA NIM, Perplexity, Groq/OpenRouter.

### Operations (`operations/`)

Modules: chat, parse, operate, ReAct, select, interpret, communicate, run, act. `Session.flow()` executes DAGs via `OperationGraphBuilder`. Same `branch=` reuses Branch without cloning — state accumulates.

### Tools / Utilities / Config

- **Tools** (`protocols/action/`): `FunctionCalling` handles schema extraction + invocation, registered with `branch.register_tools()`. Sync/async + MCP.
- **Utilities** (`ln/`): `alcall()`, `bcall()`, `race()`, `retry()` · `fuzzy_json()` for malformed LLM JSON · `Undefined`/`Unset` sentinels (`is_sentinel()`).
- **Config** (`config.py`): `AppSettings` from env. Defaults: `LIONAGI_CHAT_PROVIDER=openai`, `LIONAGI_CHAT_MODEL=gpt-4.1-mini`.

### Agent Infrastructure (`lionagi/agent/`)

- **`config.py`** — `AgentConfig` dataclass with presets: `.coding()` (file tools + guard hooks + strict path policy) and `.research()` (web + reader tools + log-only policy).
- **`factory.py`** — `create_agent()` async factory: wires a `Branch`, registers tools from config, attaches hooks, returns ready-to-use branch.
- **`permissions.py`** — `PermissionPolicy` with `allowlist` / `denylist` / `confirm` modes. Applied per tool call before execution.
- **`hooks.py`** — Built-in hooks: `guard_destructive` (blocks rm/drop/truncate), `guard_paths` (restricts file access to allowed roots), `log_tool_use` (structured tool-call logging).
- **`settings.py`** — Loads `.lionagi/settings.yaml`; merges global (`~/.lionagi/settings.yaml`) with project-level (`.lionagi/settings.yaml`), project wins on conflict.

### Sandbox (`lionagi/tools/sandbox.py`)

`SandboxSession` wraps git worktrees for isolated editing. Lifecycle: `SandboxSession.create(base_branch)` → edit files freely → `session.diff()` (returns unified diff) → `session.commit(msg)` → `session.merge()` (fast-forward into base) or `session.discard()` (deletes worktree, no trace). Safe for speculative or destructive edits — the base branch is never touched until an explicit `merge()`.

### CLI Architecture (`lionagi/cli/`)

- `cli/agent.py` — `li agent`: one-shot or resumed turn
- `cli/team.py` — `li team`: inbox (`~/.lionagi/teams/{id}.json`), concurrent writes via `fcntl.flock`
- `cli/_project.py` — `detect_project(cwd)`: returns `(project_name, project_source)` via detection cascade (see ADR-0026)
- `cli/orchestrate/` — `li o fanout` / `li o flow`:
  - `flow.py` — DAG planning (`plan()`), reactive expansion (`SpawnRequest`), `FlowPlanError`. `--team-mode` enables `li team` routing mid-pipeline.
  - `_common.py` — `BARE_WORKER_SYSTEM` / `TEAM_WORKER_SYSTEM` / `TEAM_COORD_SECTION` prompt templates
  - `fanout.py` — flat parallel workers · `_orchestration.py` — `OrchestrationEnv`, `OperationGraphBuilder`, shared setup/finalize

Project detection runs at every session creation site (`cli/agent.py`, `cli/orchestrate/_orchestration.py`, `cli/state.py`). The resolved `project` and `project_source` columns are written to the `sessions` table and displayed in Studio.

### Persistence (`lionagi/cli/_runs.py`)

Every run: `~/.lionagi/runs/{run_id}/` (`YYYYMMDDTHHMMSS-{uuid6}`).

- `run.json` — manifest · `branches/{id}.json` — snapshots · `stream/{id}.buffer.jsonl` — live chunks
- `artifacts/` when `--save` not provided · `find_branch(id)` scans `~/.lionagi/runs/` manifests

### CLI Logging (`lionagi/cli/_logging.py`)

`configure_cli_logging(verbose)`. Never `print(..., file=sys.stderr)` in CLI code:
`progress()` (silenced when verbose) · `hint()` (post-run) · `warn()` (warning prefix) · `log_error()` (error prefix)

## Key Design Patterns

- **Lazy imports**: `__init__.py` uses `__getattr__` — import time O(1).
- **Manager facade**: Branch thin; logic in MessageManager, ActionManager, iModelManager, DataLogger.
- **Pile + Progression**: Storage (dict) and ordering (deque) are independent. Multiple orderings over same Pile.
- **Observable** (`protocols/contracts.py`): Structural typing — Element auto-satisfies without explicit inheritance.
- **Serialization**: `element.to_dict(mode="python"|"json"|"db")`.
