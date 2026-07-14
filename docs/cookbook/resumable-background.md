# Resumable Background Runs

Long flows finish while you sleep. `li o flow --background` detaches immediately and writes
all progress to a log file. Resume any branch afterward with `li agent -r`.

## Setup

```bash
pip install lionagi          # or: uv add lionagi
# claude — npm install -g @anthropic-ai/claude-code && claude login
```

## Launch

`--background` requires `--save`.

```bash
li o flow claude/sonnet \
  "Audit the auth module, find security gaps, draft fixes" \
  --background --save ./auth-audit
```

```text
# output:
Flow running in background (PID 84231)
Session: 4c7c2ac9-75c7-4b  →  li monitor 4c7c2ac9-75c7-4b
Output: auth-audit/flow.log
```

The parent exits immediately. The subprocess runs the full flow and writes all output to
`auth-audit/flow.log`.

## Monitor progress

```bash
# structured run and branch status
li monitor 4c7c2ac9-75c7-4b

# raw subprocess output
tail -f ./auth-audit/flow.log
```

Use the session prefix printed by your launcher; the value above is illustrative. The
log contains planner and worker progress, final results, and resume hints.

## Find branch IDs

When the flow finishes, its log includes one resume command for the orchestrator and
each worker. Extract those hints directly:

```bash
grep 'li agent -r' ./auth-audit/flow.log
```

```text
# output:
[orchestrator] li agent -r b7f2a1e3... "..."
[researcher]   li agent -r c4d8e9f0... "..."
```

## Resume a branch

```bash
# prefix match — full id not required
li agent -r b7f2a1 "Which gaps are highest severity?"

# continue the most recently used branch — no id needed
li agent -c "Add rate-limit tests for the two critical gaps."
```

```text
# output:
The highest-severity gaps in the auth module are:
1. Missing expiry check on /api/auth/refresh — token replay window is unlimited
2. Legacy session cookie sent without Secure flag on HTTP endpoints
```

## Run directory layout

```text
~/.lionagi/runs/<run_id>/
  checkpoint.json                    # resumable flow plan and operation state
  branches/<branch_id>.json          # branch snapshot — restore point for -r
  stream/<branch_id>.buffer.jsonl    # live chunk buffer during stream

<--save dir>/
  flow.log                           # all stdout from the background subprocess
  flow_dag.png                       # DAG render (only with --show-graph)
  <agent_id>/                        # per-agent working directories
```

Deleting the `--save` directory does not break resume — branch snapshots are always in
`~/.lionagi/runs/`.

## LIONAGI_RUN_ID

Set this env var to group separate CLI invocations under the same run directory:

```bash
export LIONAGI_RUN_ID=20260420T140312-a1b2c3
li agent claude/sonnet "Write integration tests for the auth fixes"
```

The new agent's snapshot lands in `~/.lionagi/runs/20260420T140312-a1b2c3/branches/`
alongside the background flow's branches. The subprocess itself checks `LIONAGI_RUN_ID` on
startup via `allocate_run()` — set it before launching background flows you want grouped.

## Next

- [Multi-model pipeline](multi-model-pipeline.md) — design the flow before running it detached
- [Team coordination](team-coordination.md) — add mid-flow team messaging to background runs
- [CLI reference](../cli-reference.md) — all `li o flow` and `li agent` flags
