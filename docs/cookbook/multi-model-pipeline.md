# Multi-Model Pipeline

Run a three-agent DAG where researcher, implementer, and reviewer each use a different model.
Preview the planned DAG with `--dry-run` before spending tokens.

## Setup

```bash
pip install lionagi          # or: uv add lionagi
pip install matplotlib       # only for --show-graph
# claude — Option A (subscription): npm install -g @anthropic-ai/claude-code && claude login
#          Option B (API key):      export ANTHROPIC_API_KEY="sk-ant-..."
# codex  — requires ChatGPT Plus/Pro (not an API key):
#          npm install -g @openai/codex && codex login
```

## Command

```bash
li o flow claude/sonnet \
  "Research rate-limiting algorithms, implement one in Python, then review the implementation" \
  --dry-run --show-graph
```

```text
# output:
FlowPlan (3 agents, 3 ops, synthesis=False)

Agents:
  r1: researcher
    model: codex/gpt-5.4-high
  i1: implementer
  rv1: reviewer
    model: claude/opus-4-7-medium

Operations:
  o1 → r1
    instruction: Research token-bucket, sliding-window, and fixed-window rate-limiting algorithms. Document trade-offs in research.md...

  o2 → i1
    instruction: Implement a Python rate-limiter based on r1's research.md. Write impl.py and implementation_notes.md...
  depends_on: o1

  o3 → rv1
    instruction: Review impl.py for correctness and edge cases. Write findings to review.md...
  depends_on: o2

Model resolution:
  r1: codex/gpt-5.4-high (plan)
  i1: claude/sonnet (profile)
  rv1: claude/opus-4-7-medium (plan)
```

```bash
# FlowAgent.model overrides role profile; omit to use profile default (i1 → claude/sonnet)
li o flow claude/sonnet \
  "Research rate-limiting algorithms, implement one in Python, then review the implementation" \
  --save ./pipeline-out
```

```text
# output:
Planning DAG...
Plan done (2.3s): 3 agents, 3 ops — o1:r1 | o2:i1 ← o1 | o3:rv1 ← o2
Executing DAG: 3 agents / 3 ops...
  ▶ researcher started
  ✓ researcher done (8.4s)
  ▶ implementer started
  ✓ implementer done (11.2s)
  ▶ reviewer started
  ✓ reviewer done (9.6s)
DAG done (29.2s).
Saved to /Users/you/pipeline-out

[orchestrator] li agent -r orc-abc123 "..."
[researcher]   li agent -r res-def456 "..."
[implementer]  li agent -r imp-ghi789 "..."
[reviewer]     li agent -r rv-jkl012 "..."
```

## Next

- [Team coordination](team-coordination.md) — add mid-flow messaging between agents
- [Resumable background](resumable-background.md) — run long pipelines detached
- [CLI reference: `li o flow`](../cli-reference.md#li-o-flow) — all flags
