# Standard Workflows

Common patterns for using lionagi orchestration through the MCP tool
`mcp__plugin_orchestrate_lion__request`. Every pattern below assumes `help=true` was
already called once this session (see `SKILL.md`) so the `schema_fingerprint` values are
in hand; they're omitted from the examples for readability but every real call needs one.

---

## 1. Parallel exploration

Three independent workers, synthesized at the end.

```
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "fanout.submit",
   "args": {
     "prompt": "What are the security risks in this codebase?",
     "num_workers": 3,
     "with_synthesis": true
   },
   "schema_fingerprint": "<from help>"}
])
```

Use when: the task is embarrassingly parallel (same question, different angles).

---

## 2. Staged pipeline (dry-run first)

Preview the DAG, then commit it.

```
# Preview
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "flow.submit",
   "args": {
     "prompt": "Audit auth.py, implement fixes, verify with tests",
     "effort": "high",
     "dry_run": true
   },
   "schema_fingerprint": "<from help>"}
])

# Read the plan back, then commit — same call, dry_run dropped
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "flow.submit",
   "args": {
     "prompt": "Audit auth.py, implement fixes, verify with tests",
     "effort": "high",
     "with_synthesis": true,
     "max_ops": 8
   },
   "schema_fingerprint": "<from help>"}
])
```

Use when: you want to inspect the plan before committing compute.

---

## 3. Long-running work — every submit is already detached

There is no separate "background" mode to opt into: `flow.submit`, `fanout.submit`,
`agent.submit`, and `play.submit` all return a run id immediately and keep working after
the call returns. Poll or wait on the id instead of blocking on the submit call:

```
run = mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "flow.submit",
   "args": {"prompt": "Full codebase migration to async"},
   "schema_fingerprint": "<from help>"}
])
# run.ops[0].result carries the run id

mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "job.wait", "args": {"run_ids": ["<run-id>"], "max_wait": 60}}
])
# still running past the window? call job.wait again — it's not an error, just a
# partial observation

mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "job.output", "args": {"run_id": "<run-id>", "tail_chars": 20000}}
])
```

Use when: the task is long-running and you have other work — submit, then check back with
`job.wait` or `job.status` on your own schedule.

---

## 4. Playbook with typed args

Playbooks live at `~/.lionagi/playbooks/<name>.playbook.yaml` (a checkout-local file, kept
the same shape regardless of which interface submits it). Run one with `play.submit`:

```
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "play.submit",
   "args": {
     "playbook": "code-review",
     "prompt": "Focus on error handling",
     "target": "src/auth.py",
     "depth": 3,
     "cwd": "/absolute/path/to/your/checkout"
   },
   "schema_fingerprint": "<from help={'verb': 'play.submit', 'playbook': 'code-review'}>"}
])
```

`target` and `depth` above are this playbook's own declared arguments (its YAML `args:`
block) — that's why `help={'verb': 'play.submit', 'playbook': 'code-review'}` resolves them
into the returned schema instead of the argument-free one. Read that schema before writing
a playbook's custom args; don't guess their names or whether they nest under `args` or a
sub-key.

`cwd` is not one of them. It is a `play.submit` argument, and it is here because `target` is
a relative path: without it the run starts in the server's directory rather than yours, and
`src/auth.py` resolves somewhere else or nowhere.

Use when: you want a reusable, parametrized pipeline someone has already saved.

---

## 5. Graph visualization

Preview the planned DAG as an image, from a `dry_run` flow.

```
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "flow.submit",
   "args": {"prompt": "Plan and implement feature X", "dry_run": true, "show_graph": true},
   "schema_fingerprint": "<from help>"}
])
```

Then read the run's artifact list with `job.output` to find the rendered graph.

---

## 6. Grouping related runs

*Creating* a parent record is CLI-only: the MCP catalog declines `invoke.start` and
`invoke.end`, because the surface cannot tell that whoever opened a record is closing it
(see `teams-and-tracking.md`). Reading is not — `invoke.list` returns the records the CLI
wrote, and `job.list` filtered by status shows everything currently in flight:

```
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "invoke.list", "args": {"limit": 10}},
  {"op": "job.list", "args": {"limit": 20, "status": "running"}}
])
```

---

## Checkout-local equivalents (secondary path)

Everything above has a `li` CLI form for someone working inside a lionagi checkout —
`li o fanout`, `li o flow --dry-run`, `li o flow --background`, `li play`, and
`li o flow --show-graph`. Flag tables are in `reference.md`; the CLI also exposes a couple
of things the MCP surface does not yet (background monitoring via a log file, invocation
grouping) — see `teams-and-tracking.md` for what's CLI-only and why.
