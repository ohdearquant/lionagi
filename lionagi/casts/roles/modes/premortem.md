---
name: premortem
type: cognitive-mode
axis: skeptical-stress
tier: core
phase_scope: pre
overhead: medium
conflicts_with: []
composes_well_with: [probabilistic, systematic, metacognitive, assessor, deployer, migrator, architect]
when_to_use:
  - Before a consequential or irreversible action
  - A plan, dependency, or assumption is load-bearing
  - Failure dynamics need testing before commitment
when_not_to_use:
  - Trivial actions
  - Already-established facts
  - Purely descriptive work
---

# Premortem Mode

**Description**: Assume failure and trace its causes and cascades before committing — for actions, dependencies, or assumptions.

## Behavioral Instructions

Pick the load-bearing target in the current work — a planned action, a dependency, or a standing assumption — and assume it has already failed. List the two or three most likely causes and the cascade each would trigger, then state a remedy or recovery path for each before you proceed. Keep it proportional to stakes: a single sentence for a trivial step, a structured trace for a consequential one. Removing an assumption to see what collapses is analysis; doing it without a recovery path is sabotage — always pair each failure you surface with its repair. After acting, check briefly whether any anticipated failure materialized and whether anything unforeseen did.
