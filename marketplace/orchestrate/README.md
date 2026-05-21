# orchestrate

DAG orchestration planning and execution via `li o flow` and `li o fanout`.

## What's inside

- **skills/flow-it** — converts a task into a `li o flow` DAG: shape, roles, dependencies, artifact protocol
- **skills/reprompt** — strategic orchestration planning using KHIVE formalism; selects agents and phases
- **agents/orchestrator** — λᵢ meta-agent: owns the plan, spawns agents, verifies deliverables
- **agents/coordinator** — mid-flow coordination agent; manages team signals and handoffs

## Install

```
claude /plugin marketplace add khive-ai/lionagi
claude /plugin install orchestrate@lionagi
```

## Quick start

```
/reprompt
```

Transforms an intent into a phased execution plan with agent selection.

```
/flow-it
```

Converts the plan into a runnable `li o flow` DAG invocation.

## See also

- ADR-0003 (docs/adrs/ADR-0003-claude-code-marketplace.md) — marketplace pattern
