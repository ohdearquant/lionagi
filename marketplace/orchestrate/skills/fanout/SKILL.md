---
name: fanout
description: >
  Fan one task out to multiple independent lionagi workers through
  `fanout.submit`, optionally synthesizing their answers. Use when several
  parallel perspectives can work without dependency handoffs.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# Running a Fanout

Use `fanout.submit` when every worker can receive the same task independently. Use `flow` when
one worker needs another's output or the run should discover reactive follow-up work.

## Required call sequence

Ask for the catalog first, in its own call, and confirm `fanout.submit` is available:

```text
mcp__plugin_orchestrate_lion__request(help=true)
```

Then fetch the verb's full current schema and fingerprint:

```text
mcp__plugin_orchestrate_lion__request(help="fanout.submit")
```

Do not combine `help` and `ops`. Copy the returned fingerprint beside `args`, never inside it.

## Worked example

Run four independent analyses and synthesize them into one recommendation:

```text
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "fanout.submit",
   "args": {
     "query": ["claude"],
     "prompt": "Identify the main risks in migrating a Python service from threads to asyncio.",
     "num_workers": 4,
     "with_synthesis": true,
     "synthesis_prompt": "Merge the analyses into a prioritized migration checklist."
   },
   "schema_fingerprint": "<fingerprint from help=\"fanout.submit\">"}
])
```

The response returns a `run_id` immediately while the run continues in the background. It is
not the synthesized answer. Observe and retrieve it with separate calls:

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

## Capacity and boundaries

- `num_workers` caps the initial independent assignments; the planner may produce fewer.
- `fanout.submit` is a fixed fanout. Its current schema has no `reactive`, `max_ops`, or
  `dry_run` argument, so do not pass those names.
- For reactive discovery, use `flow.submit`. Its spawn capacity is
  `max_ops - initial assignment count` when `max_ops` is nonzero. A plan that fills
  `max_ops` has no capacity left for follow-ups; reserve headroom and verify the initial plan
  with `dry_run: true`.
- Ops in one request run in order. A failed op returns `ok=false` without stopping its
  siblings, so inspect every op result.

## Checkout-local alternative

Inside a lionagi checkout with `li` on `PATH`:

```bash
li o fanout claude "Identify the main risks in migrating a Python service from threads to asyncio." \
  -n 4 --with-synthesis --synthesis-prompt "Produce a prioritized migration checklist."
```
