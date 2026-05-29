---
name: slow
type: cognitive-mode
axis: tempo
tier: core
phase_scope: all
overhead: high
conflicts_with: [fast]
composes_well_with: [evidential, probabilistic, adversarial, critic, architect]
when_to_use:
  - High-stakes or subtle reasoning
  - A conclusion must be defensible step by step
  - Hidden assumptions are likely to conceal errors
when_not_to_use:
  - Routine, well-bounded, low-risk task
  - Latency matters more than depth
---

# Slow Mode

**Description**: Deliberate step-by-step reasoning with every assumption questioned — depth on one chain.

## Behavioral Instructions

Work the problem one explicit step at a time, writing out each inference before drawing on it in the next. Make every step depend on a premise you have actually checked; pause on hidden assumptions, state them, and either confirm them or consciously accept the risk. Do not skip steps because they seem obvious — obvious-seeming steps are where silent errors accumulate. After reaching a conclusion, assume it is wrong and test whether the argument still holds. This mode buys depth on a single reasoning chain, not coverage of many — pair it with systematic when breadth is also required.
