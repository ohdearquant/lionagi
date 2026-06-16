# Concepts

Six terms you need to use lionagi. For full API tables see [api/](api/index.md).

---

## Branch

A single conversation thread. Holds message history, registered tools, and model config.
All LLM calls happen through a Branch.

```python
import lionagi as li
branch = li.Branch(
    chat_model=li.iModel(model="gpt-4o-mini"),
    system="You are a technical writer.",
)
result = await branch.communicate("What is a DAG?")
```

**Use when**: you need a stateful unit of LLM work in Python.
**Don't use a single Branch for parallel workers** — give each worker its own Branch
(or let `li o fanout` / `li o flow` do it for you).

→ Full reference: [`Branch`](api/branch.md)

---

## Session

Orchestrates multiple Branches with a shared in-process message bus and a DAG engine.

```python
session = li.Session()
session.include_branches([b1, b2])
session.send(sender=b1.id, recipient=b2.id, content="draft ready")
await session.sync()
```

**Use when**: embedding lionagi in Python and needing programmatic multi-branch control.
**Don't use directly** if you're running `li o flow` — the CLI manages Session internally.

→ Full reference: [`Session`](api/session.md)

---

## Provider & Endpoint

A provider wraps one LLM/API service. An endpoint is one capability of that provider.

```python
model = li.iModel(provider="openai", endpoint="chat")
```

That's all most users need. The directory structure is the capability map:

```text
lionagi/providers/openai/   → chat, embed, images, audio, response, codex
lionagi/providers/anthropic/ → chat, response
lionagi/providers/gemini/    → chat, embed
```

Providers are registered via `@register` decorator; the registry resolves both
`iModel(provider="openai")` (single-endpoint shorthand) and
`iModel(provider="openai", endpoint="embed")` (explicit).

→ Full table: [reference/providers.md](reference/providers.md)

---

## flow

A DAG of named agent operations. Dependencies are declared explicitly; independent steps
execute in parallel. The orchestrator LLM plans the graph before execution starts.

```bash
li o flow claude/sonnet "Research async patterns and write a guide" --save ./out
li o flow claude/sonnet "Research async patterns and write a guide" --dry-run
```

**Use when**: you have ≥2 steps that could run concurrently, or want role-typed agents
(researcher → implementer → reviewer) with separate memory.
**Don't use** if your pipeline is strictly sequential — `branch.operate()` in a loop is simpler.

→ Cookbook: [Multi-model pipeline](cookbook/multi-model-pipeline.md)

---

## team

Persistent, file-backed inbox for coordinating agents across separate CLI invocations.
Messages survive process restarts, stored at `~/.lionagi/teams/{id}.json`.

```bash
li team create "research-team" -m "researcher,writer,reviewer"
li team send "draft ready" --team research-team --to writer --from researcher
li team receive --team research-team --as writer
```

**Use when**: coordination spans separate CLI runs or background processes.
**Don't use** within a single `li o flow` — the orchestrator's dependency graph is sufficient.

→ Cookbook: [Team coordination](cookbook/team-coordination.md)

---

## operate

The universal Python method for an LLM turn with tool invocation, structured output,
and streaming. Automatically routes to the right backend (API vs CLI endpoint).

```python
from pydantic import BaseModel
class Result(BaseModel):
    summary: str
    risk: str

result = await branch.operate(
    instruction="Analyze this diff for security issues:\n" + diff,
    response_format=Result,
)
```

**Use when**: you need tools, structured output, or streaming in Python.
**Don't use** for raw message objects — use `branch.chat()`. For live stream chunks — use `branch.run()`.

→ Full reference: [`operate()`](api/operations.md)

---

## persist — run_id

Every CLI invocation writes state to `~/.lionagi/runs/{run_id}/` (format: `YYYYMMDDTHHMMSS-{uuid6}`).

```text
~/.lionagi/runs/20260420T103404-abc123/
├── run.json          ← manifest
├── branches/         ← branch snapshots
├── stream/           ← live chunks (stream_persist=True)
└── artifacts/        ← agent-written files
```

Resume a previous run:

```bash
li agent -r "follow up on the auth module"          # last branch
li agent -r b_abc456 "deepen section 3"             # specific branch
```

**Use when**: you want reproducibility, inspection, or resume without re-running.
**Persist is CLI-only** — driving Branch from Python? Use `branch.to_df()` for history export.

→ Cookbook: [Resumable background](cookbook/resumable-background.md)

---

---

## Agent Infrastructure

`AgentSpec` + `create_agent()` — preset configurations for common agent patterns that wire a
fully configured `Branch` without boilerplate.

```python
from lionagi.agent import AgentSpec, create_agent

# Preset: coding agent with file tools and a strict system prompt
spec = AgentSpec.coding(model="openai/gpt-4.1", cwd="/Users/me/project")

# Add guardrail hooks
from lionagi.agent.hooks import guard_destructive, log_tool_use
spec.pre("bash", guard_destructive)
spec.post("*", log_tool_use)

# Create — returns a ready-to-use Branch
branch = await create_agent(spec)
response = await branch.chat("Fix the import cycle in utils.py")
```

**Why it exists**: `Branch` is a blank slate. `AgentSpec` captures what role, tools, permissions,
and system prompt belong together so that the same agent definition can be reused, tested, and
serialized to YAML without manually wiring hooks each time.

**Permission system**: rules on `AgentSpec.permissions` express what each tool is allowed to
do. `mode="rules"` checks allow/deny/escalate lists per tool call before execution. Convenience
presets: `PermissionPolicy.read_only()`, `PermissionPolicy.safe()`, `PermissionPolicy.deny_all()`.

**Hook system**: pre/post/error hooks attach at the tool phase level. `spec.pre("bash", fn)`
runs `fn(tool_name, action, args)` before the bash tool executes; return a modified `args` dict
to rewrite the call or raise `PermissionError` to block it. Post-hooks receive the result and
can mutate it. Built-in hooks live in `lionagi.agent.hooks`.

**Settings loading**: `create_agent()` reads `~/.lionagi/settings.yaml` (global) and optionally
`.lionagi/settings.yaml` (project-local, only when `trust_project_settings=True`). The YAML can
declare shell-command hooks and Python-import hooks without writing Python code.

```yaml
# ~/.lionagi/settings.yaml
hooks:
  pre:
    bash:
      - python: "lionagi.agent.hooks:guard_destructive"
  post:
    "*":
      - python: "lionagi.agent.hooks:log_tool_use"
```

**Use when**: you need a repeatable agent configuration — same tools, same guardrails, multiple
runs. For one-off Python use, wiring a `Branch` directly is fine.

**Don't use** if you're running `li agent` from the CLI — profiles (`~/.lionagi/agents/`) serve
that role.

→ Full reference: [`AgentSpec` and `create_agent()`](api/agent-config.md)

---

## Sandbox

`SandboxSession` wraps a git worktree for isolated, reversible code changes.

```python
from lionagi.tools.sandbox import create_sandbox, sandbox_diff, sandbox_commit, sandbox_merge, sandbox_discard

# Create: a new branch + worktree at <repo>/.worktrees/sandbox-<id>/
session = await create_sandbox(repo_root="/Users/me/project")

# Hand the worktree path to an agent
branch = await create_agent(AgentSpec.coding(cwd=session.worktree_path))
await branch.chat("Refactor the auth module")

# Inspect before committing
diff = await sandbox_diff(session)
print(diff["stat"])

# Commit inside the sandbox branch
await sandbox_commit(session, "refactor: split auth into separate module")

# Approve → merge back into the base branch
await sandbox_merge(session)

# OR reject → delete worktree, no trace left
await sandbox_discard(session)
```

**Why worktrees instead of temp dirs**:

- The agent sees the real repo (same file history, same git objects) — not a copy.
- Changes become a real git branch: reviewable via `git diff`, mergeable with `git merge --no-ff`.
- `discard()` removes the branch and worktree atomically — the base branch is never modified.

**Lifecycle**:

```text
create_sandbox() → [agent edits files] → sandbox_diff()
    → sandbox_commit() → sandbox_merge()   # accepted
                       → sandbox_discard() # rejected
```

`session.is_active` is `True` until `sandbox_merge()` or `sandbox_discard()` completes.

**Use when**: an agent might make destructive or speculative changes you need to review before
merging. Pair with `AgentSpec.coding(cwd=session.worktree_path)` to confine the agent.
**Don't use** for read-only analysis — worktrees have overhead; just point the agent at the repo.

→ Full reference: [`SandboxSession`](api/sandbox.md)

---

Next: [CLI reference](cli-reference.md) for full flag tables, or [API reference](api/index.md)
for the Python SDK surface.
