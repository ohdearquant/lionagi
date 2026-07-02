# Choosing a Surface

lionagi's CLI has several orchestration surfaces. They are layered, not
competing: each one buys you something over the previous layer and costs
something in latency and setup. This page is the decision guide; per-flag
detail lives in the [CLI Reference](cli-reference.md).

## The decision table

| Your task shape | Reach for | Why |
|-----------------|-----------|-----|
| One question, one answer | `li agent MODEL "prompt"` | Single turn, no planning overhead |
| Follow-up on earlier work | `li agent -r BRANCH_ID` / `-c` | Resumes the branch with full context |
| Same role used often | `li agent -a NAME` | Profile carries model, effort, system prompt, yolo |
| N independent subtasks, same shape | `li o fanout` | Decompose → parallel workers → optional synthesis |
| Subtasks depend on each other | `li o flow` | Planner builds a DAG; engine runs legs as dependencies clear |
| A pipeline you run repeatedly | `li play NAME` | Playbook = named, parametric, version-controlled flow |
| A well-known domain pipeline | `li engine run KIND` | Prebuilt research / review / coding / hypothesis / planning engines |
| Run it later, or on a cadence | `li schedule create` | Cron, interval, or repo-event triggers |
| Script must wait for a run to finish | `li monitor run ID` | Blocks until terminal state, exit code reflects outcome |
| Watch progress live | `li monitor --watch` | Live table or per-run detail view |
| Agents messaging each other across runs | `li team` | Persistent shared inbox |
| Group many runs into one record | `li invoke` | One parent row instead of N session rows |

## Sizing: don't pay for structure you don't need

Each layer adds a planning or coordination step that costs real wall-clock
time before any work starts. The single most common misuse is reaching for a
heavier surface than the task shape needs:

- **1 leg** → `li agent`. Never `li o flow` — you would pay a planner turn
  to produce a one-node DAG.
- **2–3 independent legs** → `li o fanout`, or just two `li agent` calls in
  parallel from your own script. Fanout's decomposition phase only earns its
  cost when you want the orchestrator to *choose* the split.
- **3+ legs with dependencies** → `li o flow`. This is the break-even point:
  below it the planner turn dominates; above it dependency-aware parallelism
  wins.
- **The same flow, more than twice** → promote it to a playbook and use
  `li play`. The point of a playbook is that the *second* invocation is one
  short command with typed args — not that the first one is faster.

The corollary: `li play` feeling slow is usually a shape problem, not an
engine problem. A playbook wrapping a 2-leg task inherits the full
plan-then-execute cycle. Check the DAG with `--dry-run` — if it plans one or
two nodes, drop down a layer.

## Composition patterns

The surfaces are designed to chain:

```bash
# Recurring pipeline: schedule fires a playbook on a cron
li schedule create nightly-audit --cron "0 6 * * *" \
  --action-kind playbook --playbook audit

# Scriptable orchestration: launch, then block until terminal
li play backend-review --pr 42 &
li monitor run "$RUN_ID" && echo "review done"

# One dashboard row for a multi-run skill
INV=$(li invoke start --skill release-check --prompt "v0.28 gate")
li play backend  --invocation "$INV"
li play frontend --invocation "$INV"
li invoke end "$INV" --status completed

# Resume a worker that a fanout or flow left unfinished
li agent -r BRANCH_ID "pick up where you left off"
```

Two rules of thumb for choosing the chain:

1. **Automate the trigger before automating the pipeline.** If you find
   yourself re-typing the same `li play` invocation daily, the next step is
   `li schedule`, not a bigger playbook.
2. **Poll the surface, not the filesystem.** `li monitor` and
   `li monitor run` read the same state the engine writes; tailing run
   directories or sleeping in shell loops re-implements them badly.

## What each layer persists

Every surface writes to the same run store (`~/.lionagi/runs/`) and state
database, so anything you start on one surface is observable and resumable
from the others: a flow leg is a branch you can `li agent -r`, a play is
visible in `li monitor`, a scheduled firing shows up in `li schedule runs`.
Escalating a task to a heavier surface never orphans the work the lighter
one already did.
