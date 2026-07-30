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
It returns the verb catalog, each verb's required parameters, and the `schema_fingerprint`
a call needs to carry alongside `args` (see `reference.md`). Never guess a parameter name —
this call and `help="<verb>"` are the source of truth.

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
`play.submit` is `flow.submit` with a saved playbook name instead of a hand-written plan.

Every `*.submit` call spawns a **detached background run** and returns a run id right
away — there is no blocking call that returns a finished answer. Read the result back with
`job.status`, `job.wait`, or `job.output`.

## Quick start

```
# Catalog + fingerprints, once per session
mcp__plugin_orchestrate_lion__request(help=true)

# Single agent
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "agent.submit", "args": {"prompt": "Write unit tests for auth.py", "agent": "implementer"},
   "schema_fingerprint": "<from help>"}
])

# Fan out 4 parallel workers + synthesize
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "fanout.submit",
   "args": {"prompt": "Review this codebase for security issues", "num_workers": 4, "with_synthesis": true},
   "schema_fingerprint": "<from help>"}
])

# DAG flow — dry-run first, then commit
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "flow.submit",
   "args": {"prompt": "Audit auth, implement fixes, verify with tests", "agent": "orchestrator", "dry_run": true},
   "schema_fingerprint": "<from help>"}
])
# ... inspect the plan, then resend without dry_run ...

# Run a saved playbook
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "play.submit", "args": {"playbook": "security-audit", "prompt": "JWT middleware"},
   "schema_fingerprint": "<from help={'verb': 'play.submit', 'playbook': 'security-audit'}>"}
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
- **Critic runs last** — never parallel with producers. Set `control=true` in the plan.
- **Agent reuse > spawning** — reusing `agent_id` across ops preserves memory.
- **Artifact handoff** — agents write to `{save_dir}/{agent_id}/`, downstream reads from `../{dep_id}/`.
- **`depends_on` is mandatory** for every non-root op in a `flow.submit` plan.
- **`dry_run` before executing** — preview the DAG before committing compute.
- **Only the published catalog exists** — a verb this checkout's source shows but that
  isn't in `reference.md`'s eleven-verb table does not exist for a plugin user, because
  the shipped server resolves the latest lionagi release, not any local checkout.

## Companion references

For detailed documentation, read these companion files in this skill directory:

- **[reference.md](reference.md)** — the MCP tool's parameter shapes for all eleven verbs,
  the `schema_fingerprint` protocol, and the secondary `li` CLI flag tables
- **[dag-planning.md](dag-planning.md)** — FlowPlan data model, DAG decomposition principles, role-to-model routing, re-plan rounds
- **[workflows.md](workflows.md)** — standard workflow patterns (parallel exploration, staged pipeline, long-running work, playbooks, visualization)
- **[teams-and-tracking.md](teams-and-tracking.md)** — team coordination patterns, invocation tracking, scheduling — and which of these are CLI-only today

## Source code

For someone working inside a lionagi checkout — informational, not part of the MCP surface:

| Component | Path |
|---|---|
| MCP server + verb catalog | `lionagi/mcp/server.py`, `lionagi/mcp/verbs.py`, `lionagi/mcp/dispatch.py` |
| CLI entrypoint | `lionagi/cli/main.py` |
| Flow engine (FlowPlan, FlowOp, FlowAgent) | `lionagi/cli/orchestrate/flow.py` |
| Fanout engine | `lionagi/cli/orchestrate/fanout.py` |
| Argparse definitions | `lionagi/cli/orchestrate/__init__.py` |
| Agent CLI | `lionagi/cli/agent.py` |
| Teams | `lionagi/cli/team.py` |
| Invocations | `lionagi/cli/invoke.py` |
| Scheduler engine | `apps/studio/server/scheduler/engine.py` |
