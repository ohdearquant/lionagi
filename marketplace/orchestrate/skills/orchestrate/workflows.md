# Standard Workflows

Common patterns for using lionagi orchestration through the MCP tool
`mcp__plugin_orchestrate_lion__request`. Every pattern below assumes `help=true` was already
called and the relevant targeted help was used where a fingerprint varies by playbook (see
`SKILL.md`). Fingerprint placeholders remain in the examples because every submit needs one.

---

## 1. Parallel exploration

Three independent workers, synthesized at the end.

```
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "fanout.submit",
   "args": {
     "query": ["claude"],
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
     "query": ["claude"],
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
     "query": ["claude"],
     "prompt": "Audit auth.py, implement fixes, verify with tests",
     "effort": "high",
     "with_synthesis": true,
     "max_ops": 8
   },
   "schema_fingerprint": "<from help>"}
])
```

Use when: you want to inspect the plan before committing compute.

`max_ops` is shared by the initial plan and any reactive follow-up assignments. If reactive
execution should be able to discover more work, leave capacity below the cap; an initial
eight-assignment plan with `max_ops: 8` has no follow-up spawn budget.

---

## 3. Long-running work — every submit is already detached

There is no separate "background" mode to opt into: `flow.submit`, `fanout.submit`,
`agent.submit`, and `play.submit` all return a run id immediately and keep working after
the call returns. Poll or wait on the id instead of blocking on the submit call:

```
run = mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "flow.submit",
   "args": {"query": ["claude"], "prompt": "Full codebase migration to async"},
   "schema_fingerprint": "<from help>"}
])
# Check run.ops[0].ok; run.ops[0].result carries the run id

mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "job.wait", "args": {"run_ids": ["<run-id>"], "max_wait": 60}}
])
# Check ok and all_terminal. If still running past the window, call job.wait again.
# A bounded partial observation is not an error.

mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "job.output", "args": {"run_id": "<run-id>", "tail_chars": 20000}}
])
```

Call `job.output` after `all_terminal` is true when the final result is required.

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

Render the DAG that actually executed. A dry run returns a textual plan and exits before the
run graph exists, so `dry_run: true` and graph preview are not a supported combination.

```
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "flow.submit",
   "args": {"query": ["claude"], "prompt": "Plan and implement feature X", "show_graph": true},
   "schema_fingerprint": "<from help>"}
])
```

After the run finishes, read its artifact list with `job.output` to find `flow_dag.png`.

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
