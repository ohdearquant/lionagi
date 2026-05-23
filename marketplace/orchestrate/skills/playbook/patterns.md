# Playbook Patterns

Common structural patterns for lionagi playbooks. Each section shows a minimal
YAML snippet and explains when to reach for it.

---

## 1. Iterative Loop Pattern

Run multiple rounds of the same operation until a stopping condition is met.

```yaml
name: test-coverage
effort: medium
args:
  target:
    type: int
    default: 90
    help: "coverage % to reach before stopping"
  max_rounds:
    type: int
    default: 5
    help: "hard cap on improvement rounds"
prompt: |
  Improve coverage for {input} to {target}% in up to {max_rounds} rounds.
  Each round: measure, write tests for the lowest-covered module, re-measure.
  Stop when {target}% is reached or {max_rounds} rounds are exhausted.
```

**When to use**: quality ratchets (coverage, lint score, benchmark), workflows
where iterations are data-driven rather than fixed, tasks where one pass is
unlikely to be sufficient.

---

## 2. Fan-Out Pattern

Spawn one parallel worker per item in a work list, then consolidate results.

```yaml
name: module-audit
workers: 8
with_synthesis: true
args:
  mode:
    type: str
    default: security
    help: "audit dimension: security | dead-code | api-surface | all"
prompt: |
  Enumerate top-level modules in {input}. Spawn one worker per module.
  Each worker runs a {mode} audit independently. The synthesis step
  consolidates findings and ranks by severity.
```

**When to use**: large codebases where modules can be reviewed independently,
multi-file refactors, any task with a natural "one agent per item" split.
Set `workers` to bound concurrency; `with_synthesis: true` appends a
consolidation step automatically.

---

## 3. Pipeline Pattern

Execute a fixed sequence of stages where each stage depends on the prior one.

```yaml
name: feature
max-ops: 20
show-graph: true
prompt: |
  Implement the feature described in {input}. Stages in order — do not begin
  a stage until the previous is complete.
  STAGE 1 — SPEC:    Write a one-page design doc covering API and edge cases.
  STAGE 2 — TESTS:   Write failing tests. Confirm they fail.
  STAGE 3 — IMPLEMENT: Write minimum code to pass the tests.
  STAGE 4 — REVIEW:  Self-review for correctness, coverage, and style.
```

**When to use**: TDD loops, research-then-implement workflows, any task where
ordering is mandatory. Use `show-graph: true` to confirm the DAG is sequential
before spending tokens.

---

## 4. Persistent State Pattern

Use `team_attach` for a shared message channel that survives across invocations.

```yaml
name: ongoing-audit
team_attach: audit-memory
prompt: |
  Continue the ongoing audit of {input}. Check the team channel for findings
  from prior runs. Audit only modules not yet covered. Add new findings with
  a timestamp so future runs can skip them.
```

**When to use**: long-running projects spanning multiple sessions, incremental
workflows where re-auditing covered ground wastes tokens. Use `team_mode`
(not `team_attach`) when each run should be stateless. The two are mutually
exclusive.

---

## 5. Gate Pattern

Insert a critic checkpoint after parallel workers finish. The critic approves
or rejects before the workflow proceeds to the output phase.

```yaml
name: gated-refactor
max-ops: 15
args:
  strict:
    type: bool
    default: false
    help: "abort on any HIGH or CRITICAL finding"
prompt: |
  Refactor {input} in sequence:
  PHASE 1 — PLAN: workers propose changes per sub-component. No code written.
  PHASE 2 — CRITIC GATE: one critic reviews all proposals. Must output
    APPROVED (proceed) or REJECTED (list blockers; halt if strict={strict}).
  PHASE 3 — IMPLEMENT: apply approved proposals and run tests.
```

**When to use**: high-stakes changes (migrations, security patches, public API
changes) where a bad plan executed quickly causes more damage than a slow correct
one. The critic runs after all planners return — never in parallel with them.
