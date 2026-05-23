---
name: review
description: >
  General-purpose code review checklist. Use when reviewing any code change
  without a narrower specialization. Complements security-review, pr-review
  (multi-perspective), and the other review skills — pull this when you
  need the standard correctness/quality rubric without a specific angle.
allowed-tools: [Bash, Read, Glob, Grep]
---

# Code review procedure

A general-purpose review rubric for any code change. Use as the
"correctness" specialist dimension of a multi-perspective review, or as
a standalone checklist when reviewing a diff without a specialization.

## Scope

Review ONLY the assigned diff. Cross-module concerns go to architecture;
security-sensitive code goes to security-review.

## Checklist summary (9 sections)

1. **Correctness** — logic, control flow, off-by-one, error handling, invariants, concurrency, numeric
2. **API contract** — backward compatibility, type annotations, error types, return value shape
3. **Tests** — coverage of new paths, edge cases, mock fidelity, flaky risk
4. **Readability & maintainability** — names, function length, nesting, comments, dead code
5. **Consistency** — naming convention, error handling pattern, import ordering, abstraction level
6. **Docs & changelog** — public API changes, behavioral changes, new config options
7. **Dependencies** — necessity, maintenance, license, version constraints, lockfile
8. **Performance (light-touch)** — obvious O(n²) regressions, hot-path allocations, N+1 queries
9. **Over/under-engineering** — premature abstraction, speculative flexibility, too-simple cases

See [checklist.md](checklist.md) for the full detailed checklist with all sub-items.

## Output format

Findings table per finding:

| Severity | Location | Issue | Suggested fix | Confidence |
|---|---|---|---|---|
| MEDIUM | `lionagi/operations/act.py:87` | Off-by-one in `range(len(xs))` | Use `enumerate(xs)` | High |

Verdict line at top: `APPROVE` | `APPROVE-WITH-FIXES` | `REQUEST CHANGES`.

Cite `file:line` for every finding.

## Severity calibration

- `CRITICAL` — will break production on merge
- `HIGH` — correctness bug affecting real use cases
- `MEDIUM` — edge case bug, significant maintainability issue
- `LOW` — style, naming, minor refactor
- `INFO` — future consideration, not blocking

## Ground rules

1. Describe the bug, propose a fix.
2. Don't invent requirements.
3. Rank severity honestly.
4. Flag assumptions you can't verify from the diff.
5. Review against committed HEAD of the PR branch.
