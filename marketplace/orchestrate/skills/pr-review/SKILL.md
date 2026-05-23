---
name: pr-review
description: >
  Multi-perspective PR review procedure. Plan a minimal DAG of specialists
  scoped to what the PR actually touches, synthesize with a critic,
  post ONE consolidated comment per verbosity tier. Pull before any
  structured PR review so the methodology stays consistent.
allowed-tools: [Bash, Read, Glob, Grep]
---

# PR review procedure

A reusable rubric for reviewing a pull request. Works as an orchestrated
flow (via `li o fanout` or `li o flow`) or for a solo reviewer.

## When to use

Any time you need a structured PR review — OSS contribution evaluation,
internal code review, security sign-off, release gate.

## Workflow Phases

| Phase | Action |
|-------|--------|
| **0. Fetch context** | `gh pr view` + `gh pr diff` → save to `_context/` |
| **1. Discovery** | Parallel specialists from closed set (correctness, security, architecture, tests, perf) |
| **2. Discussion** | Optional — only if dimensions cross-pollinate |
| **3. Critic synthesis** | Reads all findings → `critic_final/final_synthesis.md` |
| **4. Post comment** | ONE consolidated comment per verbosity tier |

## Verdict options

`APPROVE` | `APPROVE-WITH-FIXES` | `REJECT`

## Verbosity tiers for posting

| Verbosity | Content | Safe for |
|-----------|---------|----------|
| none | don't post, synthesis stays local | any |
| brief | ≤10 lines: verdict + top MUST-FIX only | OSS |
| substantive | MUST-FIX + SHOULD-FIX with file:line; omit blind-spots | internal repos |
| full | complete synthesis including blind spots + coverage | private repos only |

## Ground rules

1. Cite line numbers against committed HEAD of the PR branch.
2. Don't invent requirements — only flag if absence is a bug.
3. If runtime context is needed that isn't in the diff, say so.
4. Graceful degradation — note any tool failures in synthesis.

## Related skills

- `li skill security-review` — threat-model rubric for security dimension
- `li skill review` — general correctness/quality rubric

See [specialist-guide.md](specialist-guide.md) for specialist dimensions,
severity rubric, synthesis format, CLI examples, and source code reference.
