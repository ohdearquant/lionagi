# orchestrate

DAG orchestration planning and execution via `li o flow` and `li o fanout`.

## What's inside

- **skills/flow-it** — converts a task into a `li o flow` DAG: shape, roles, dependencies, artifact protocol
- **agents/orchestrator** — λᵢ meta-agent: owns the plan, spawns agents, verifies deliverables
- **agents/coordinator** — mid-flow coordination agent; manages team signals and handoffs

## Install

```
claude /plugin marketplace add ohdearquant/lionagi
claude /plugin install orchestrate@lionagi
```

## Quick start

```
/flow-it
```

Converts a task intent into a runnable `li o flow` DAG invocation.

## See also

- ADR-0003 (docs/adrs/ADR-0003-claude-code-marketplace.md) — marketplace pattern
