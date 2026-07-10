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

## Depth contract (applies to every section — read before writing)

An ADR is the record a maintainer reads two years from now to understand what the
design thinking was. It must be usable AS a development document: someone extending
or debugging this area should be able to work from the ADR, checking the source only
to confirm. That imposes four obligations:

1. **Every consideration is recorded.** If a question was weighed while deciding —
   even one settled quickly — it appears here with its answer and reason. The
   reasoning may not live only in a chat log, a workspace file, or someone's head.
2. **Decisions are enumerated and separable.** An ADR that decides four coupled
   things names them D1-D4 in a table up front and gives each its own section.
   A reader must be able to cite "ADR-NNNN D3" and mean something exact.
3. **Contracts are shown, not paraphrased.** Where the decision fixes a signature,
   a data shape, a settings field, a state machine, a wire payload, or an ordering
   guarantee, the ADR shows the real thing (actual Python signatures, Pydantic
   model fields, table columns, JSON payload shapes, module trees) — extracted
   from source for Retrospective ADRs, specified exactly for Aspirational ones.
4. **Rejected paths are kept with their reasons.** "Alternatives considered" is a
   required section with real content: what the alternative was, what it would have
   bought, and the specific reason it lost. Deferred designs are retained in full,
   marked DEFERRED — a deferred design that is not written down is a lost design.

Target size falls out of the content, not a quota — but a load-bearing area ADR
that satisfies the four obligations rarely fits under ~250 lines, and the corpus's
richest records run 400-700. If a draft is under ~150 lines, it is almost certainly
paraphrasing contracts instead of showing them, or dropping considerations.

## Context

What forces exist. For retrospective ADRs: what the code actually does TODAY and
why it evolved that way (honest, not aspirational). Name the problems this decision
answers as P1, P2, … — each a concrete failure or need, with the code path or
scenario where it bites. Close with the decision table:

| Concern | Decision |
|---------|----------|
| <one row per named decision> | D1: <one-line statement> |
| … | D2: … |

And an explicit out-of-scope list: what this ADR deliberately does NOT decide,
each item with the one-line reason (points at the owning ADR when one exists).

## Decision

One subsection per decision (### D1 — <name>), each carrying:

- The decision in present tense.
- **The contract**: real signatures / model fields / table shapes / payload
  examples / module trees — whatever artifact the decision actually fixes.
  Code anchors at module level (`path.py`; line numbers rot).
- **Exact semantics**: the enumerated behavior cases (what happens on miss, on
  restart, on conflict, on the empty input, on the error path). Bullet-per-case;
  a case not listed is a case not decided.
- **Why this way**: the considerations that led here, including the ones that
  were settled fast. Where a comparison was made (two libraries, two orderings,
  two storage layouts), show the comparison, not just the winner.
- Budget/limit numbers where they exist (timeouts, retry counts, cache sizes,
  pool caps) — with the reason the number is what it is.

Decisions that are part of the design but not shipped are still sections, marked
**DEFERRED**, with the target design written in full.

## Consequences

What becomes easier, what becomes harder, what is deliberately given up — each as
a concrete claim someone could dispute, not a platitude. Includes the maintenance
consequences: what a contributor must now know, what new failure modes exist,
what the cost of reversing each Dn would be.

## Current-vs-ideal delta

ONLY for retrospective ADRs on organically-evolved areas: the gap between what is
and what this ADR says it should be. Each delta row is phrased so it can be lifted
verbatim into a GitHub issue (deliverable + acceptance).

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | <what to change> | S/M/L | (filled at issue-open time) |

## Alternatives considered

Required, one subsection per alternative that was genuinely weighed:

- **<Alternative>** — what it is, what it would have bought, and the specific
  reason it lost (with evidence: a measured cost, a violated invariant, a
  concrete failure scenario). Name external references where they shaped the
  call (a library's approach adopted or rejected, a pattern from another
  system).

An empty or perfunctory section here means the decision was not actually
examined — go back.

## Notes

Optional: anything that aids future tracing and fits nowhere above (migration
history, compatibility windows, naming rationale).
