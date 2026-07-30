# DAG Decomposition Reference

## Task Shapes

Match the plan structure to the work type. These are starting templates — adapt to actual
scope, not the label.

```text
AUDIT / INVENTORY
  parallel explorers → analyst → critic

DESIGN / PLAN
  researcher → architect → strategist → critic

FIX / HARDEN
  explorer (find problems, file:line precision) →
  analyst (write fix specs with before/after) →
  implementer (apply specs) →
  tester →
  critic

BUILD
  researcher → architect → implementer + tester (parallel) → coordinator → reviewer → critic

LARGE BUILD (multi-lane)
  coordinator (branch setup) →
  parallel explorers per lane →
  implementers per lane →
  coordinator (merge) → reviewer → critic

REVIEW / COMPARE
  parallel explorers → analyst → reviewer → critic

RESEARCH / REPORT
  parallel researchers → analyst → synthesizer
```

The FIX/HARDEN pattern is critical: analysis-capable models find problems with breadth and
file:line precision. Give the implementer a brief with exact locations and before/after
snippets — the implementer does not need to explore the codebase independently. This
eliminates wasted scanning and dramatically reduces errors.

---

## Identifying Independence

Ask: can agent A do its work without agent B's output? If yes, they run in parallel (no edge).
If no, add a `depends_on` edge.

Every false dependency you add serializes work and wastes time. Every missing dependency
produces garbage because the downstream agent runs without the data it needs.

---

## depends_on — The Most Common Planning Failure

**Observed failure mode**: A planner emits a 10-agent plan where most non-root ops have
`depends_on = []`. All ops run flat-parallel. Implementers finish before explorers, write
from thin air, produce incorrect output. This is a total waste.

Before returning any FlowPlan, check explicitly:

```text
For every op in plan.operations:
  If the op's role is a root producer (explorer, researcher reading from
  non-flow sources — web, codebase, prior knowledge):
    depends_on MAY be empty.
  Otherwise:
    depends_on MUST list at least one upstream op that produces data
    this op needs.
```

Default fan-in expectations by role:

| Role | Minimum depends_on |
|---|---|
| analyst | 2+ explorer or researcher ops |
| architect | all analysts + auditor |
| implementer | architect or design reviewer + the explorer covering its scope |
| tester | the implementer(s) whose code it tests |
| reviewer | all implementers in its scope |
| critic (control) | all reviewers + testers + every implementer |

If your plan has any non-root op with an empty `depends_on`, revise before returning.

---

## DAG Patterns

Think in dependency edges, not phases. An op starts as soon as all its `depends_on` ops
have completed. Independent ops run in parallel automatically.

```text
Diamond: two parallel branches that fan in
  r1: researcher   (no deps)
  au1: auditor     (no deps)       -- parallel with r1
  ar1: architect   <- r1           -- starts when r1 done, does not wait for au1
  i1: implementer  <- ar1
  rv1: reviewer    <- i1, au1      -- fan-in from two separate branches
  c1: critic       <- rv1, i1      -- waits for review and implementation

Wide fan-out with selective fan-in
  e1: explorer(backend)   (no deps)
  e2: explorer(frontend)  (no deps)
  e3: explorer(config)    (no deps)
  a1: analyst  <- e1, e2           -- only needs backend + frontend
  a2: analyst  <- e1, e3           -- only needs backend + config
  ar1: architect <- a1, a2
  c1: critic   <- ar1, e1, e2, e3  -- reads everything for full gate context

Control node (iterative refinement)
  r1: researcher (no deps)
  i1: implementer <- r1
  rv1: reviewer   <- i1
  c1: critic      <- i1, rv1  [control=true]
    -- if should_continue: orchestrator re-plans targeted fix ops
```

Key principles:
- **Edges represent data needs, not sequence.** If architect does not need the auditor's
  output, do not add that edge.
- **Fan-out wide, fan-in selective.** Downstream agents depend only on upstream agents
  whose artifacts they actually need.
- **Critic sees everything.** Critic `depends_on` lists all agents it reviews, not just
  the last one.
- **Control nodes trigger re-planning.** A critic with `control=true` can request additional
  ops if `should_continue=true`. Re-plans should be surgical — target the specific gaps
  the verdict named, not a full re-run.

---

## Phase Structure

For large plans, think in phases to reason about parallelism:

- **Phase 1 (Root producers)**: All ops with `depends_on = []`. Run immediately, in parallel.
- **Phase N**: All ops whose deps all completed in phases 1..N-1. Run as deps resolve.
- **Terminal phase**: The critic and synthesis. Must wait for everything upstream.

Phases emerge naturally from the dependency graph — you do not declare them explicitly. But
reasoning through phases helps you find missing edges before they cause failures in execution.

---

## Sizing Rules

- Maximum 15 agents total. Beyond this, coordination overhead exceeds value.
- If a role would produce the same artifact as another role, merge them.
- If a role's op instruction could be merged into an adjacent op without losing quality,
  merge them. Two ops on the same agent are cheaper than spawning a new agent.
- Prefer reusing agent ids across ops (second op inherits first op's memory) over spawning
  fresh agents for trivially different work.
