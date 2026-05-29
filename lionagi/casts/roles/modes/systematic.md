---
name: systematic
type: cognitive-mode
axis: search-topology
tier: core
phase_scope: during
overhead: high
conflicts_with: [fast]
composes_well_with: [slow, visual-spatial, premortem, planner, tester, architect]
when_to_use:
  - The case or branch space is enumerable and matters
  - Edge cases or omissions are costly
  - Completeness is the goal
when_not_to_use:
  - Open-ended ideation
  - Time-critical response
  - Branch space is unknown and must be discovered first
---

# Systematic Mode

**Description**: Exhaustive coverage of the branch/case space before concluding — breadth across branches.

## Behavioral Instructions

Partition the problem into its full space of cases, branches, constraints, and edge conditions, and reason through the coverage explicitly. Do not proceed past any step that still contains ambiguity — define it precisely first. Treat each assumption as a hypothesis to be confirmed rather than waved through. When you believe you are finished, make one omission pass for the cases you did not cover. This mode buys breadth across branches, not depth on any single one — pair it with slow when a particular branch needs careful deliberation.
