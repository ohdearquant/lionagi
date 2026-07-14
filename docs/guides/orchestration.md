# From One Agent to a Repeatable Playbook

LionAGI's terminal surfaces form a progression. Each stage below is shipped in
0.28 and adds one kind of coordination.

## 1. Agent: one owner

Use one agent while one conversation can own the outcome:

```bash
li agent codex "Review the error handling in this package." --cwd . --timeout 300
```

The response prints to stdout. LionAGI records the branch and prints commands
for resuming it and checking its status.

Move up when you can name independent perspectives or dependent phases that
should not share one conversation.

## 2. Fan-out: independent workers

Fan-out asks an orchestrator to decompose the task, runs workers in parallel,
and can synthesize their answers:

```bash
li o fanout codex \
  "Review this package for correctness, security, and maintainability." \
  --cwd . -n 3 --with-synthesis --save ./lion-results/review
```

Expected artifacts are `worker_1.md` through `worker_3.md` and
`synthesis.md` in the save directory. Workers do not depend on one another.

Move up when a later worker needs an earlier worker's result.

## 3. Flow: dependent and reactive work

Flow plans a graph, then starts operations as their dependencies clear. Always
preview a new task shape:

```bash
li o flow codex \
  "Inspect the package, propose a fix, implement it, then verify the result." \
  --cwd . --max-ops 6 --dry-run
```

Execute after inspecting the plan:

```bash
li o flow codex \
  "Inspect the package, propose a fix, implement it, then verify the result." \
  --cwd . --max-ops 6 --save ./lion-results/fix
```

Reactive expansion is shipped and defaults to `--reactive all`: workers may
emit additional work while the graph is live. Use `--reactive off` to execute
only the initial planned graph, or provide a comma-separated role allowlist
such as `--reactive critic,evaluator`. `--max-ops` caps both initial and spawned
operations.

Team coordination is also shipped. Add `--team-mode NAME` when workers need a
shared inbox during one flow, and use `--team-max-rounds N` to bound final
message-delivery wakeups. A dependency edge remains the clearer choice when
one operation simply consumes another's result.

## 4. Playbook: repeatable flow input

Promote a flow to a playbook when its model, prompt shape, and arguments should
be reviewed and reused. Save this as
`.lionagi/playbooks/repo-review.playbook.yaml`:

```yaml
name: repo-review
description: Review a target with a selectable focus
model: codex

args:
  focus:
    type: str
    default: correctness
    help: Primary review concern

prompt: |
  Review {input} with emphasis on {focus}.
  Return findings ordered by impact and cite exact paths.
```

Confirm discovery and inspect the planned graph:

```bash
li play list
li play repo-review --focus security "." --dry-run
```

Run it and save artifacts:

```bash
li play repo-review --focus security "." --save ./lion-results/playbook
```

Project-local playbooks take precedence over global playbooks under
`~/.lionagi/playbooks/`. Trusted, enabled plugins can also contribute
namespaced playbooks.

## What persists

Every stage records durable state under `~/.lionagi/runs/<run-id>/`. Fan-out,
flow, and playbook `--save` paths hold user-facing artifacts. Flow and playbook
runs also maintain `checkpoint.json` for cross-process recovery.

Next, learn how to [observe, control, and resume durable work](durable-operations.md).
