# ADR-NNNN: <decision title — a noun phrase naming the thing decided>

- **Status**: Proposed | Accepted | Superseded by ADR-NNNN
- **Kind**: Retrospective (records what IS) | Aspirational (records the target state)
  — never both in one document; a delta between a retrospective truth and an
  aspirational target is an ISSUE, not a section that blurs the two.
- **Area**: <exactly one of the 16 ratified areas: core-data-model,
  messages-context, actions-tools, session-branch, operations, service-providers,
  orchestration, agent-roles, hooks, utilities, persistence-state, cli-surface,
  scheduling-control-plane, studio, governance, substrates>
- **Date**: YYYY-MM-DD
- **Relations**: supersedes v0-NNNN[, v0-NNNN…] | extends ADR-NNNN | none
  — every v0 ADR referenced gets an explicit disposition in the corpus index:
  carried-forward-as / merged-into / retired-because.

## Context

What forces exist. For retrospective ADRs: what the code actually does TODAY and
why it evolved that way (honest, not aspirational). 3-6 paragraphs max.

## Decision

The decision, stated in the present tense, with the load-bearing invariants named
explicitly. Code anchors (`path.py` — module level, not line numbers, which rot).

## Consequences

What becomes easier, what becomes harder, what is deliberately given up.

## Current-vs-ideal delta

ONLY for retrospective ADRs on organically-evolved areas: the gap between what is
and what this ADR says it should be. Each delta row is phrased so it can be lifted
verbatim into a GitHub issue (deliverable + acceptance).

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | <what to change> | S/M/L | (filled at issue-open time) |

## Notes

Optional: alternatives considered and why rejected. Keep short — the analyses
backing this decision live outside the repo.
