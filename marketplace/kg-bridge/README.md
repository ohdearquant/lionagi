# kg-bridge

Bridge lionagi runs/agents to khive's knowledge graph — opt-in, implementation pending.

## What's inside

- **skills/bridge-design** — Design contract for the lionagi→khive bridge. Read this
  before implementing. Specifies entity kinds, edge relations, emit/recall hook shapes,
  and confidence thresholds for writing and auto-linking.

## Install

```
claude /plugin marketplace add khive-ai/lionagi
claude /plugin install kg-bridge@lionagi
```

## Quick start

```
/bridge-design
```

Opens the design SKILL.md as context. When the bridge is implemented, two invocations
replace this one: `/bridge-emit` (post-run, writes to KG) and `/bridge-recall`
(pre-run, injects KG entities into context).

## See also

- ADR-0003 (docs/adrs/ADR-0003-claude-code-marketplace.md) — marketplace pattern
- [khive kg plugin](https://github.com/ohdearquant/khive/tree/main/khive/marketplace/kg)
  — the actual KG backend this bridge will emit to
