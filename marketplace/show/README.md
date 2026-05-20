# show

Structured content review orchestration with play-gate, show-final-gate, and critic gating.

## What's inside

- **skills/show** — orchestrates a full show run: open a play, route through agents, apply critic gate
- **agents/play-gate** — entry gate that validates and initialises a play before execution
- **agents/show-final-gate** — exit gate that confirms show completion criteria are met
- **agents/critic** — adversarial quality gate; issues APPROVE / APPROVE-WITH-FIXES / REJECT verdicts

## Install

```
claude /plugin marketplace add khive-ai/lionagi
claude /plugin install show@lionagi
```

## Quick start

```
/show <topic-name>
```

Opens a show run for the given topic, routes through play-gate, executes the play,
then applies critic gate and show-final-gate before completion.

## See also

- ADR-0003 (docs/adrs/ADR-0003-claude-code-marketplace.md) — marketplace pattern
