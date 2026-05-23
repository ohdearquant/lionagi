---
name: orchestrate
description: >
  Plan and execute multi-agent workflows using lionagi's CLI: li o flow (DAG pipelines),
  li o fanout (parallel workers), and li play (playbook invocations). Use when a task
  needs multiple agents working in parallel or staged phases.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# orchestrate

Plan and execute multi-agent workflows using lionagi's CLI.

## When to use which command

| Situation | Command |
|---|---|
| Single task, one agent | `li agent MODEL PROMPT` |
| Same prompt to N independent workers | `li o fanout MODEL PROMPT -n N` |
| Staged pipeline with dependencies | `li o flow MODEL PROMPT` |
| Pre-saved parametric workflow | `li play NAME [PROMPT]` |

If all subtasks are independent (no output feeds another), use `fanout`.
If any subtask depends on the output of another, use `flow`.
`li play NAME` is sugar for `li o flow -p NAME`.

## Quick start

```bash
# Single agent
li agent claude "Write unit tests for auth.py"

# Fan out 4 parallel workers + synthesize
li o fanout claude "Review this codebase for security issues" -n 4 \
    --with-synthesis --save ./out --yolo --bypass

# DAG flow — dry-run first, then execute
li o flow claude "Audit auth, implement fixes, verify with tests" \
    --dry-run --effort high
li o flow claude "Audit auth, implement fixes, verify with tests" \
    --with-synthesis --save ./flow-out --yolo --bypass

# Run a saved playbook
li play security-audit "JWT middleware" --save ./out
li play list  # list available playbooks
```

## Key principles

- **Critic runs last** — never parallel with producers. Set `control=True`.
- **Agent reuse > spawning** — reusing `agent_id` across ops preserves memory.
- **Artifact handoff** — agents write to `{save_dir}/{agent_id}/`, downstream reads from `../{dep_id}/`.
- **`depends_on` is mandatory** for every non-root op.
- **`--dry-run` before executing** — preview the DAG before committing.

## Companion references

For detailed documentation, read these companion files in this skill directory:

- **[cli-reference.md](cli-reference.md)** — complete flag tables for `li agent`, `li o fanout`, `li o flow`, `li play`, `li team`, `li invoke`
- **[dag-planning.md](dag-planning.md)** — FlowPlan data model, DAG decomposition principles, role-to-model routing, re-plan rounds
- **[workflows.md](workflows.md)** — standard workflow patterns (parallel exploration, staged pipeline, background flow, spec files, visualization)
- **[teams-and-tracking.md](teams-and-tracking.md)** — team coordination patterns, invocation tracking, scheduling

## Source code

| Component | Path |
|---|---|
| CLI entrypoint | `lionagi/cli/main.py` |
| Flow engine (FlowPlan, FlowOp, FlowAgent) | `lionagi/cli/orchestrate/flow.py` |
| Fanout engine | `lionagi/cli/orchestrate/fanout.py` |
| Argparse definitions | `lionagi/cli/orchestrate/__init__.py` |
| Agent CLI | `lionagi/cli/agent.py` |
| Teams | `lionagi/cli/team.py` |
| Invocations | `lionagi/cli/invoke.py` |
| Scheduler engine | `apps/studio/server/scheduler/engine.py` |
