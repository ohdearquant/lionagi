# ADR-{NNNN}: {Title}

- **Status**: Proposed | Accepted | Deprecated | Superseded by ADR-{NNNN}
- **Date**: YYYY-MM-DD
- **Supersedes**: none
- **Related**: {ADRs, issues}

## Context

The problem, and the forces acting on it. State what was **measured** rather than
what is believed, and give the command or file that produced each number. A claim
without its source is a claim the next reader has to re-derive.

## Decision

### D1 — {one decision per heading}

The contract, then the exact semantics, then why this way rather than the
alternative that was nearly chosen.

## Consequences

What becomes true, including the parts that are worse. A consequences section with
no costs in it has not been written yet.

## Alternatives considered

Each with the reason it lost. An alternative rejected on merit is worth more to a
future reader than one rejected by omission.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | What changes, and the acceptance test that proves it. | XS/S/M/L | #NNNN |

## Notes on verification

For anything in `lionrs`, state which claims are backed by a machine-checked proof
in `proofs/`, which are backed by tests, and which are backed by neither. These are
three different strengths and a reader should not have to guess which one applies.
