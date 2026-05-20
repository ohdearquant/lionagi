# research

Multi-perspective research with web search, codebase analysis, and synthesis.

## What's inside

- **skills/progress-research** — Multi-round ChatGPT Deep Research pipeline with quality
  gates. Each round: drill → refine → fire → evaluate → record. Uses ChatGPT for
  breadth-first literature exploration (50+ min per query); Claude handles synthesis,
  hallucination checks, and tradability decisions.
- **agents/researcher** — Evidence-gathering agent profile. Mission: gather knowledge,
  track sources, document gaps. Researcher never commits or proposes — it gathers so
  analysts and architects can decide.

## Install

```
claude /plugin marketplace add khive-ai/lionagi
claude /plugin install research@lionagi
```

## Quick start

```
/progress-research "What are the state-of-the-art methods for time-series forecasting?"
```

The skill opens a multi-round deep research loop. At each gate it asks whether to
drill deeper or synthesize. Use `/researcher` to spawn a dedicated evidence-gathering
agent on a specific sub-question.

## See also

- [ADR-0003](../../docs/adrs/ADR-0003-claude-code-marketplace.md) — marketplace pattern
