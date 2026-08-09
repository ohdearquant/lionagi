# DAG Planning

How the orchestrator plans and executes multi-agent workflows.

---

## TaskAssignment Data Model

When `flow.submit` or `play.submit` runs, the planner emits a
`list[TaskAssignment]`. There is no separate `FlowPlan`, agent registry, control node, or
re-plan verdict in the current execution model. The assignment list and its dependencies are
the DAG.

Each assignment has these fields:

```
task:          str        # concrete objective for one worker
assignee:      str        # role from the planner's available roster
inputs:        list[str]  # artifacts or context needed to start
exit_criteria: str|None   # observable condition that means the task is done
depends_on:    list[str]  # 1-based numbers of earlier assignments whose output is consumed
modes:         list[str]  # optional reasoning-mode overrides for this assignment
```

The planner must use roles from its supplied roster. Model and profile routing are resolved
after planning; `assignee` is a role, not a model name.

Source: `lionagi/casts/emission.py`, `lionagi/orchestration/prompts.py`, and
`lionagi/cli/orchestrate/flow.py`.

---

## Decomposition Principles

**Identify real data dependencies first.** Two assignments are independent when neither
consumes the other's output. Leave `depends_on` empty so they run concurrently. Add a
dependency only when a downstream assignment needs an upstream result, not to express a
preferred reading order or shared topic.

```
1. [researcher] Inspect authentication behavior              depends_on: []
2. [tester]     Map current authentication test coverage     depends_on: []
3. [implementer] Apply fixes using findings from 1 and 2     depends_on: ["1", "2"]
4. [tester]     Verify the changes from 3                    depends_on: ["3"]
5. [critic]     Review the implementation and test evidence  depends_on: ["3", "4"]
```

This creates a wide first phase, then joins only where outputs must combine.

**A critic is an ordinary assignment.** To review completed work, place it after every
producer it evaluates with `depends_on`. There is no `control` field and no engine-level
approve/re-plan loop. If a downstream assignment must act on a verdict, make that dependency
explicit and state the expected behavior in its task.

**Each assignment gets a separate branch.** Reusing the same `assignee` role on multiple
steps reuses role configuration, not conversation memory. Never rely on an earlier assignment
being remembered by a later one.

---

## Artifact Handoff

Each assignment receives its own artifact directory. A dependent assignment also receives
the exact directories for its upstream dependencies in execution context. Do not hard-code
relative paths such as `../researcher/` and do not assume two assignments share a working
conversation.

When a specific file is part of the contract, say what to write and name it consistently:

- Producer task: `Write the findings to auth_findings.md.`
- Consumer task: `Read auth_findings.md from the supplied upstream artifact directory.`
- Exit criteria: `auth_findings.md exists and cites the inspected files.`

---

## Reactive Follow-Ups and `max_ops`

Flows are reactive by default: eligible workers can request necessary follow-up assignments
that were not visible during initial planning. `max_ops` is a shared budget for both the
initial plan and these spawned follow-ups.

```
follow-up capacity = max_ops - initial assignment count
```

For example, an initial eight-assignment plan with `max_ops: 8` has no reactive capacity. If
the workflow is expected to discover work, set a larger cap or ask for a tighter initial plan.
With `max_ops: 0`, the initial plan is not caller-capped and reactive execution allows up to
20 follow-up assignments. Use `reactive: off` when the DAG must remain flat.

---

## Preview and Visualization

`dry_run: true` returns a textual rendering of the planned assignments and declared
dependencies. It exits before the run graph is built, so it cannot produce a graph image.

`show_graph: true` writes `flow_dag.png` after an executing flow finishes. Use `job.output`
to find the graph in the completed run's artifact list.

---

## Sizing Guidance

- Keep one assignment per distinct unit of work or dependency boundary.
- Prefer a wide graph over a near-linear chain when work is independent.
- Keep the initial plan below `max_ops` when reactive discovery is useful.
- Use one agent for simple tasks instead of manufacturing a DAG.

## Anti-Patterns

- **False dependencies** — serializing independent work increases wall-clock time.
- **Critic parallel with producers** — the critic cannot review outputs that do not exist yet.
- **Assumed branch memory** — repeated roles still receive separate branches.
- **Guessed artifact paths** — consume the exact upstream directories supplied at runtime.
- **Full initial budget** — filling `max_ops` leaves no room for reactive follow-ups.
- **Graph preview on dry run** — a dry run builds no executable graph.
- **Vague objectives** — specify files, concerns, output format, and exit criteria.
