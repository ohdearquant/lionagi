# DAG Planning

How the orchestrator plans and executes multi-agent workflows.

---

## FlowPlan Data Model

When running `li o flow`, an orchestrator LLM produces a `FlowPlan` before execution.

**FlowPlan**

```
agents:     list[FlowAgent]   # who exists
operations: list[FlowOp]      # what happens and in what order
synthesis:  bool              # request a final consolidation pass
```

**FlowAgent**

```
id:        str        # short unique id, e.g. "r1", "impl-1" (^[A-Za-z0-9_-]{1,64}$)
role:      str        # from the available-agents roster
model:     str|None   # optional model override
guidance:  str|None   # default behavioral framing for all ops on this agent
```

**FlowOp**

```
id:         str         # short unique op id (same regex as agent id)
agent_id:   str         # references a FlowAgent.id
instruction: str        # concrete task text
guidance:   str|None    # per-op override (replaces agent.guidance)
depends_on: list[str]   # upstream FlowOp ids this op waits on
control:    bool        # True = critic checkpoint
```

**FlowControlVerdict** (produced by `control=True` ops)

```
should_continue: bool   # False = flow ends; True = orchestrator re-plans
reason:          str    # justification grounded in specific op outputs
next_steps:      str    # specific gaps to address
```

Up to 3 re-plan rounds are allowed. Existing agents are reused (memory persists).

Source: `lionagi/cli/orchestrate/flow.py`

---

## Decomposition Principles

**Identify independence first.** Two ops are independent when neither needs the
other's output. Independent ops run in the same phase (parallel). Dependent ops
are sequential.

```
Phase 1 (parallel): [research-1, research-2, context-fetch]
Phase 2 (parallel): [implement]  ← depends on research-1, research-2
Phase 3 (parallel): [write-tests] ← depends on implement
Phase 4 (serial):   [critic] ← control op, runs last
```

**Agent reuse is cheaper than spawning.** An agent is a Branch with persistent
memory. Reusing the same `agent_id` across ops means the agent remembers prior
turns — no re-injection needed. Prefer 2-4 agents running multiple ops over
8 agents with one op each.

**Critic runs last, never parallel with producers.** Set `control=True` only on
an op that reviews completed work. At most one control op per round. Must declare
`depends_on` referencing all ops it reviews.

**Every non-root op must have `depends_on`.** Root ops (explorers, researchers
sourcing external info) may have empty `depends_on`. Everything else must declare
at least one upstream.

---

## Role-to-Model Guidance

| Role | Recommended tier |
|---|---|
| researcher, explorer, analyst, architect, reviewer | high-reasoning model, medium-high effort |
| implementer, tester, coordinator | code-capable model, high effort |
| critic (quality gate) | highest reasoning model, high-xhigh effort |
| writer, documenter | mid-tier model, medium effort |

Don't prescribe specific model names — let the user's agent profile or `--effort`
flag handle routing.

---

## Artifact Handoff

Each agent writes outputs to `{save_dir}/{agent_id}/`. Op results are also
persisted as `{op_id}.md`.

Downstream ops (different agent) read from `../{dep_agent_id}/{filename}`.
Same-agent deps need no file read — the branch already has memory.

**Instructions must specify**: WHERE to write, WHAT to name, WHERE to read upstream.

---

## DAG Sizing

| Complexity | Agents | Phases | Example |
|---|---|---|---|
| Simple | 2-4 | 2-3 | Bug fix: explorer → implementer → tester |
| Medium | 4-8 | 3-4 | Feature: researcher + architect → implementer → tester + reviewer → critic |
| Complex | 8-13 | 4-5 | Refactor: multiple explorers → analyst → multiple implementers → multiple testers → critic |
| Max | 15 | 5 | Full audit: 6 parallel scanners → consolidator → fix implementers → verifiers → critic |

---

## Anti-Patterns

- **Over-decomposing** — 1 agent is fine for simple tasks. Don't plan a 5-agent DAG for a typo fix.
- **Critic parallel with producers** — defeats the purpose. Critic reviews completed work.
- **Vague instructions** — "review the code" is useless. Specify files, concerns, output format.
- **Lost artifacts** — always specify where agents write and where downstream reads.
- **Meta-delegation** — "orchestrate the team to build X" is circular. YOU plan the DAG.
- **First-wave implementer** — analysis/research must come before implementation.
