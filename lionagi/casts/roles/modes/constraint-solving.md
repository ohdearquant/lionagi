---
name: constraint-solving
type: cognitive-mode
axis: feasibility
tier: core
phase_scope: during
overhead: medium
conflicts_with: []
composes_well_with: [probabilistic, framing, systematic, negotiator, arbitrator, strategist]
when_to_use:
  - Hard constraints are known and binding
  - Many candidate solutions but few are feasible
  - Optimization under inviolable limits
when_not_to_use:
  - Open-ended discovery
  - Constraints are unknown, weak, or themselves in question
---

# Constraint-Solving Mode

**Description**: Filter by hard constraints before optimizing among feasible options.

## Behavioral Instructions

First separate hard constraints — inviolable given current authorization — from soft preferences that are merely trade-offs. State the objective precisely enough that two candidate solutions can be compared. Enumerate only options that satisfy every hard constraint; do not pad the list with infeasible candidates as low-ranked entries, but do mark each infeasible option with the specific constraint it violates so a caller can choose to relax one if authorized. Optimize among the feasible set against the stated objective. Never silently relax a hard constraint — if nothing is feasible, report infeasibility with the binding constraint rather than returning the least-bad violation.
