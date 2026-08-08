---
name: orchestrate
description: >
  Plan and execute multi-agent workflows through lionagi's orchestration MCP tool:
  flow.submit (DAG pipelines), fanout.submit (parallel workers), agent.submit (one
  agent), and play.submit (saved playbooks). Use when a task needs multiple agents
  working in parallel or staged phases.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# orchestrate

Plan and execute multi-agent workflows through lionagi's orchestration MCP server. The
plugin ships one MCP tool, `mcp__plugin_orchestrate_lion__request`, which dispatches
namespaced verbs behind a single `ops` array — not a CLI, not one tool per verb.

## Start here, every session

```
mcp__plugin_orchestrate_lion__request(help=true)
```

Call this once, in its own call (never combined with `ops`), before writing any spawn call.
It returns the verb catalog, each verb's required parameters, and either its
`schema_fingerprint` or a marker that the fingerprint varies by playbook (see
`reference.md`). Never guess a parameter name — this call and targeted `help` are the source
of truth.

## When to use which verb

| Situation | Verb |
|---|---|
| Single task, one agent | `agent.submit` |
| Same prompt to N independent workers | `fanout.submit` |
| Staged pipeline with dependencies | `flow.submit` |
| Pre-saved parametric workflow | `play.submit` |
| Check on a run already submitted | `job.status`, `job.wait`, `job.output` |

If all subtasks are independent (no output feeds another), use `fanout.submit`.
If any subtask depends on the output of another, use `flow.submit`.
`play.submit` loads a saved prompt and defaults, then uses the same planner and executor as
`flow.submit`.

Every `*.submit` call spawns a **detached background run** and returns a run id right
away — there is no blocking call that returns a finished answer. Read the result back with
`job.status`, `job.wait`, or `job.output`.

## Quick start

```
# Catalog + fingerprints, once per session
mcp__plugin_orchestrate_lion__request(help=true)

# Single agent
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "agent.submit", "args": {"query": ["claude"], "prompt": "Write unit tests for auth.py"},
   "schema_fingerprint": "<from help>"}
])

# Fan out 4 parallel workers + synthesize
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "fanout.submit",
   "args": {"query": ["claude"], "prompt": "Review this codebase for security issues", "num_workers": 4, "with_synthesis": true},
   "schema_fingerprint": "<from help>"}
])

# DAG flow — dry-run first, then commit
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "flow.submit",
   "args": {"query": ["claude"], "prompt": "Audit auth, implement fixes, verify with tests", "dry_run": true},
   "schema_fingerprint": "<from help>"}
])
# ... inspect the plan, then resend without dry_run ...

# Run a saved playbook
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "play.submit", "args": {"playbook": "feature", "prompt": "Add JWT middleware"},
   "schema_fingerprint": "<from help={'verb': 'play.submit', 'playbook': 'feature'}>"}
])

# Check a run
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "job.wait", "args": {"run_ids": ["<run-id>"], "max_wait": 60}}
])
```

## Key principles

- **`help=true` in its own call, before writing a spawn call** — never combine `help` and
  `ops` in one call; the server refuses it.
- **`schema_fingerprint` rides as a sibling of `args`**, never inside it — every
  `*.submit` verb requires it, fetched from `help`.
- **Named profiles are installation-specific** — call `profile.list` before passing `agent`,
  or pass a model through the verb's positional `query` argument where supported.
- **Critic assignments depend on producers** — a critic is an ordinary `TaskAssignment`, not
  a special control node. Put it after every output it reviews.
- **Every assignment gets a separate branch** — repeated roles do not share conversation
  memory. Pass results through declared dependencies and artifacts.
- **Artifact paths are supplied by the engine** — name outputs clearly and use the exact
  upstream directories injected into downstream assignment context; do not guess `../` paths.
- **Use `depends_on` only for real data dependencies** — independent assignments leave it
  empty and run concurrently.
- **Reserve reactive capacity** — `max_ops` is shared by the initial plan and follow-up spawns;
  a plan that fills it leaves no room for reactive work.
- **`dry_run` is a text preview** — it does not build a run graph. `show_graph` writes a graph
  artifact only after an executing flow finishes.
- **The catalog is the authority, not this bundle** — `reference.md` documents the verbs
  these skills call, which is a subset of what the server offers; and the shipped server
  resolves the latest lionagi *release*, so a verb you found by reading a checkout may not
  be there yet. `help=true` settles both questions.

## Companion references

For detailed documentation, read these companion files in this skill directory:

- **[reference.md](reference.md)** — the MCP tool's parameter shapes for the verbs these
  skills call, the `schema_fingerprint` protocol, and the secondary `li` CLI flag tables
- **[dag-planning.md](dag-planning.md)** — `TaskAssignment` data model, dependency planning, branch and artifact handoff, reactive budgets
- **[workflows.md](workflows.md)** — standard workflow patterns (parallel exploration, staged pipeline, long-running work, playbooks, visualization)
- **[teams-and-tracking.md](teams-and-tracking.md)** — team coordination patterns, invocation tracking, scheduling — and which of these are CLI-only today

## Source code

For someone working inside a lionagi checkout — informational, not part of the MCP surface:

| Component | Path |
|---|---|
| MCP server + verb catalog | `lionagi/mcp/server.py`, `lionagi/mcp/verbs.py`, `lionagi/mcp/dispatch.py` |
| CLI entrypoint | `lionagi/cli/main.py` |
| Flow engine (`TaskAssignment` DAG) | `lionagi/cli/orchestrate/flow.py` |
| Fanout engine | `lionagi/cli/orchestrate/fanout.py` |
| Argparse definitions | `lionagi/cli/orchestrate/__init__.py` |
| Agent CLI | `lionagi/cli/agent.py` |
| Teams | `lionagi/cli/team.py` |
| Invocations | `lionagi/cli/invoke.py` |
| Scheduler engine | `lionagi/studio/scheduler/engine.py` |
