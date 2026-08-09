---
name: flow
description: >
  Plan and run a dependency-aware lionagi DAG through `flow.submit`, including
  dry runs and reactive follow-up capacity. Use when work has stages,
  dependencies, parallel branches, or a final synthesis step.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# Running a Dependency DAG

Use `flow.submit` when tasks have dependency edges or workers should be able to discover
follow-up work during execution. Use `fanout` for a fixed set of independent perspectives.

## Required call sequence

Ask for the catalog first, in its own call, and confirm `flow.submit` is available:

```text
mcp__plugin_orchestrate_lion__request(help=true)
```

For a prompt-driven flow with no playbook, fetch the exact schema and fingerprint:

```text
mcp__plugin_orchestrate_lion__request(help="flow.submit")
```

If `args` names a `playbook`, instead ask with the exact same spelling because the schema and
fingerprint vary by playbook:

```text
mcp__plugin_orchestrate_lion__request(help={"verb": "flow.submit", "playbook": "feature"})
```

Never combine `help` and `ops`. Put `schema_fingerprint` beside `args`, not inside it.

## Worked example

First plan only. The prompt limits the initial DAG to four assignments while `max_ops: 6`
reserves capacity for up to two reactive follow-ups:

```text
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "flow.submit",
   "args": {
     "query": ["claude"],
     "prompt": "Create at most four initial assignments to analyze an API migration, propose a plan, and independently verify it. Preserve remaining capacity for follow-up work discovered by the workers.",
     "max_ops": 6,
     "reactive": "all",
     "with_synthesis": true,
     "dry_run": true
   },
   "schema_fingerprint": "<fingerprint from help=\"flow.submit\">"}
])
```

The dry run is a background job. Read its returned `run_id` with `job.status` and
`job.output`. Confirm the planned assignment count leaves headroom, then resubmit the same op
without `dry_run`:

```text
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "flow.submit",
   "args": {
     "query": ["claude"],
     "prompt": "Create at most four initial assignments to analyze an API migration, propose a plan, and independently verify it. Preserve remaining capacity for follow-up work discovered by the workers.",
     "max_ops": 6,
     "reactive": "all",
     "with_synthesis": true
   },
   "schema_fingerprint": "<fingerprint from help=\"flow.submit\">"}
])
```

The second response returns a new `run_id`, not the final answer. Poll it and retrieve output:

```text
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "job.status", "args": {"run_id": "<run_id>"}}
])
```

```text
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "job.output", "args": {"run_id": "<run_id>"}}
])
```

`job.list` finds recent runs, and `job.kill` stops one by `run_id`.

## Reactive budget

For a nonzero `max_ops`, reactive spawn capacity is `max_ops - initial assignment count`.
The planner and reactive spawns share that one budget; there is no separate reservation
parameter. If the dry-run plan already contains `max_ops` assignments, follow-ups have zero
capacity. Tighten the requested initial plan or raise `max_ops` before execution.

With `max_ops: 0`, the CLI leaves the initial plan uncapped and applies its separate default
reactive-spawn ceiling. Prefer an explicit total when cost needs to be predictable.

Multiple outer MCP ops run in order, but a failure returns `ok=false` without stopping its
siblings. Check every op's `ok` field.

## Checkout-local alternative

Inside a lionagi checkout with `li` on `PATH`:

```bash
li o flow claude "Create at most four initial assignments to analyze an API migration." \
  --max-ops 6 --reactive all --with-synthesis --dry-run
```
