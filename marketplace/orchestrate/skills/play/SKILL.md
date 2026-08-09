---
name: play
description: >
  Run an existing lionagi playbook through the orchestration MCP server and
  retrieve its background result. Use when a saved `.playbook.yaml` workflow
  should be executed with concrete input or declared arguments.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# Running a Saved Playbook

Use `play.submit` for a playbook that is already installed on the MCP server's
machine. Use the `playbook` skill instead when the file itself needs authoring or editing.

## Required call sequence

1. Ask for the catalog in its own call and confirm `play.submit` is available:

   ```text
   mcp__plugin_orchestrate_lion__request(help=true)
   ```

2. Ask for the schema with the exact playbook name. A playbook's declared arguments change
   the schema, so the catalog marks this fingerprint as varying by `playbook`:

   ```text
   mcp__plugin_orchestrate_lion__request(help={"verb": "play.submit", "playbook": "minimal"})
   ```

3. Copy that reply's `schema_fingerprint` into the op as a sibling of `args`:

   ```text
   mcp__plugin_orchestrate_lion__request(ops=[
     {"op": "play.submit",
      "args": {"playbook": "minimal", "prompt": "Explain monads with one example."},
      "schema_fingerprint": "<fingerprint from the qualified help call>"}
   ])
   ```

Never put `schema_fingerprint` inside `args`. It is not read there, so the server refuses the
op as stale. Do not combine `help` and `ops` in one call; the server refuses that shape too.

## Worked example

This example uses a saved playbook at `~/.lionagi/playbooks/minimal.playbook.yaml`. If it is
not already present, create it first:

```yaml
name: minimal
model: claude
prompt: |
  Explain the supplied topic in plain language, with one concrete example.
```

Run the qualified help and submit calls above, then read the returned `run_id`:

```text
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "job.status", "args": {"run_id": "<run_id>"}}
])
```

Repeat `job.status` until the run is terminal, then retrieve its console tail and artifact list:

```text
mcp__plugin_orchestrate_lion__request(ops=[
  {"op": "job.output", "args": {"run_id": "<run_id>"}}
])
```

The submit response is only a run id, not the playbook's result. `job.list` finds recent runs;
`job.kill` stops one by `run_id`.

## Arguments and planning

- Read the qualified help schema for every custom playbook argument. Pass each declared
  argument directly inside the op's `args`; do not invent another nesting layer.
- Set `dry_run: true` inside `args` to plan without executing workers. The dry run is still a
  background job, so inspect it through `job.status` and `job.output`.
- If calling `flow.submit` with a `playbook` instead, use the same qualified-help rule with
  `{"verb": "flow.submit", "playbook": "<same name>"}`.
- Multiple ops run in order, but one op returning `ok=false` does not stop its siblings. Check
  every op's `ok` field instead of trusting the top-level response alone.

## Checkout-local alternative

Inside a lionagi checkout with `li` on `PATH`:

```bash
li play minimal "Explain monads with one example."
```

Use `li play minimal --help` to see the playbook's declared flags.
