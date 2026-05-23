# Standard Workflows

Common patterns for using lionagi orchestration commands.

---

## 1. Parallel exploration

Three independent researchers, synthesized at the end.

```bash
li o fanout claude/sonnet "What are the security risks in this codebase?" \
    -n 3 \
    --with-synthesis claude/opus-4-6-high \
    --save ./fanout-out \
    --yolo --bypass
```

Use when: the task is embarrassingly parallel (same question, different angles).

---

## 2. Staged pipeline (dry-run first)

Preview the DAG, then execute.

```bash
# Preview
li o flow claude/opus-4-6-high \
    "Audit auth.py, implement fixes, verify with tests" \
    --dry-run --effort high

# Execute
li o flow claude/opus-4-6-high \
    "Audit auth.py, implement fixes, verify with tests" \
    --with-synthesis \
    --save ./flow-out \
    --max-ops 8 \
    --effort high \
    --yolo --bypass
```

Use when: you want to inspect the plan before committing compute.

---

## 3. Background flow with monitoring

Fire and forget, check results later.

```bash
li o flow claude/sonnet "Full codebase migration to async" \
    --save ./migration-out \
    --background \
    --yolo --bypass

# Monitor progress
tail -f ./migration-out/flow.log
```

Use when: the task is long-running and you have other work.

---

## 4. Spec file for a repeatable pipeline

Commit a YAML spec to the repo for a reusable pipeline.

```yaml
# security-review.yaml
model: claude/opus-4-6-high
effort: xhigh
max_ops: 12
with_synthesis: true
save: ./security-review-out
prompt: |
  Perform a full security and correctness audit.
  Focus on: authentication, input validation, secret handling.
```

```bash
li o flow -f security-review.yaml "Focus on the payments module" --yolo --bypass
```

Use when: you run the same pipeline regularly with different targets.

---

## 5. Playbook with typed args

Save as `~/.lionagi/playbooks/code-review.playbook.yaml`:

```yaml
name: code-review
description: "Multi-agent code review with critic checkpoint"
argument-hint: "[--target FILE] [--depth N]"
model: claude/sonnet
effort: high
with_synthesis: true
args:
  target:
    type: str
    default: "."
    help: "file or directory to review"
  depth:
    type: int
    default: 1
    help: "number of review passes"
prompt: |
  Perform a {depth}-pass code review of {target}.
  {input}
```

```bash
li play code-review "Focus on error handling" --target src/auth.py --depth 3
```

Use when: you want a reusable command with custom parameters.

---

## 6. Graph visualization

See the DAG the orchestrator planned.

```bash
li o flow claude "Plan and implement feature X" \
    --dry-run --show-graph --save ./viz-out
# Saves DAG as PNG to ./viz-out/
```

---

## 7. Invocation-grouped runs

Group multiple flows under one parent record for Studio tracking.

```bash
INV=$(li invoke start --skill orchestrate --prompt "Full security audit")

li o flow claude "Audit authentication" --save ./auth-out \
    --invocation "$INV" --yolo --bypass

li o fanout claude "Audit input validation" -n 3 \
    --invocation "$INV" --save ./val-out --yolo --bypass

li invoke end "$INV" --status completed
```

Visible in Studio at `/invocations`.
