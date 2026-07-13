# Multi-Model Pipeline

Plan a role-based DAG, then route each assignment through an explicit worker-model pool.
Preview the assignments with `--dry-run` before spending worker tokens.

## Setup

```bash
pip install lionagi          # or: uv add lionagi
pip install matplotlib       # only for --show-graph
# claude — npm install -g @anthropic-ai/claude-code && claude login
# codex  — requires ChatGPT Plus/Pro (not an API key):
#          npm install -g @openai/codex && codex login
```

## Command

```bash
li o flow claude/sonnet \
  "Research rate-limiting algorithms, implement one in Python, then review the implementation" \
  --workers codex/gpt-5.4-high,claude/sonnet,codex/gpt-5.3-codex-spark \
  --dry-run
```

The planner returns `TaskAssignment` entries, so the exact tasks and roles depend on the
prompt. Dry-run output has two useful sections:

- `Plan (N assignments)` lists each assignment's number, assignee, task, dependencies,
  and optional exit criteria.
- `Model + modes resolution` shows the generated agent ID, selected worker model, and
  any role modes.

`--workers` is what makes this run multi-model. Model 1 goes to assignment 1, model 2
to assignment 2, and so on; the pool wraps if the planner creates more assignments.
Assignments do not carry their own model field.

```bash
li o flow claude/sonnet \
  "Research rate-limiting algorithms, implement one in Python, then review the implementation" \
  --workers codex/gpt-5.4-high,claude/sonnet,codex/gpt-5.3-codex-spark \
  --save ./pipeline-out --show-graph
```

The live progress text and timing depend on the generated plan. Worker artifacts land
under `pipeline-out/<agent_id>/`; `--show-graph` writes `pipeline-out/flow_dag.png`.

## Next

- [Team coordination](team-coordination.md) — add mid-flow messaging between agents
- [Resumable background](resumable-background.md) — run long pipelines detached
- [CLI reference: `li o flow`](../cli-reference.md#li-o-flow) — all flags
