# ADR-0024: LNDL operate integration adapter

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: operations
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0087; extends ADR-0021 and ADR-0022

## Context

LNDL is integrated into operations as an opt-in `Middle`, exported from
`lionagi.operations.lndl_middle`. It does not add a runtime or replace `operate()`; callers select it
with `middle=lndl_middle`, and `build_lndl_middle()` creates the same adapter with a custom round
budget. The default budget is three (`lionagi/operations/lndl_middle/__init__.py`,
`lionagi/operations/lndl_middle/lndl_middle.py`).

Each round reuses the normal API or CLI turn implementation. The first round injects the LNDL system
prompt, a rendered target-model field specification, and caller guidance. Per-round native tool
schemas and response-format rendering are removed because LNDL's assembler, rather than the provider
transport, constructs and validates the target result.

The adapter normalizes, lexes, parses, and assembles fenced LNDL blocks. Missing blocks or a missing
`OUT{}` produce `Continue`; language errors produce `Retry`; and an assembled output produces a
`Success` candidate. Retry notices are sent on the next round. Exhaustion raises `LNDLError`, while
non-LNDL exceptions propagate rather than becoming a returned `Failed` value.

The as-built action rule differs by round shape. With `OUT{}`, only action calls reachable from the
assembled output execute. Without `OUT{}`, every declared action call executes before the next round,
and its result is available by alias to later rounds. All calls are translated to ordinary branch
action requests and use the concurrent, error-suppressed `act()` path, so authorization, hooks,
logging, and message recording still apply. This eager continuation behavior conflicts with the
prior record's statement that only `OUT{}`-referenced actions execute.

## Decision

The implemented LNDL behavior is a bounded-loop turn adapter under the contracts of ADR-0021 and
ADR-0022. This record carries forward the as-built integration seam and replaces broader prior claims
that are not enforced by the operations implementation.

```text
operate(middle=lndl_middle)
          │
          v
  prompt + target specification
          │
          v
 communicate or run_and_collect
          │
          v
 normalize ─> lex ─> parse ─> assemble
          │
          ├── Retry/Continue ──> next round
          └── Success ──> act pending calls ─> validate ─> return
```

The load-bearing invariants are:

- LNDL is opt-in per `operate()` call. Non-LNDL callers and the default adapter-selection policy are
  unchanged.
- One LNDL adapter invocation may own several recorded inner exchanges, bounded by the configured
  round budget. API models use `communicate`; CLI models and `RunParam` use `run_and_collect`.
- Round-one guidance contains the LNDL prompt and, when available, the exact target-model field
  names. Native provider tools and native response-format rendering are disabled for inner rounds.
- Language failures are repairable retries. A valid assembled result is action-resolved and then
  validated against the target model unless validation is skipped; budget exhaustion raises.
- A continuation round with no `OUT{}` eagerly executes every valid declared action. A success round
  executes only actions reachable through its assembled output. Cross-round action results are kept
  by alias for the duration of the adapter invocation.
- LNDL actions go through `act()` and therefore do not bypass branch authorization or hooks. Tool
  failures are returned as action results so a later round can repair or adapt.

## Consequences

LNDL can reuse the established transport, message, validation, and tool-governance paths, and callers
can choose it without affecting ordinary operations. The target model is made explicit to the prompt
even though native structured-output rendering is disabled.

One logical operation may incur several provider exchanges and action rounds. The eager continuation
rule can cause a tool side effect before any `OUT{}` commits it. The adapter uses only
`Continue`/`Retry`/`Success` values directly, so the package's broader round-outcome vocabulary does
not describe the externally observed exhaustion and unexpected-failure behavior precisely.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Choose one LNDL action-commit rule for no-`OUT{}` rounds, then align the system prompt, adapter, architecture record, and tests so eager continuation actions or output-gated actions are stated consistently. | M | (filled at issue-open time) |
| 2 | Decide whether `note.X` is supported across adapter rounds; either thread and test bounded scratchpad state or remove the cross-round promise from the prompt and public language surface. | M | (filled at issue-open time) |
| 3 | Align the adapter's public failure contract with the round-outcome vocabulary by defining whether exhaustion and unexpected failures are typed outcomes or raised exceptions, and add end-to-end tests for the chosen policy. | S | (filled at issue-open time) |

## Notes

A dedicated LNDL runtime was rejected because it would duplicate branch transport, validation, and
action governance. Direct invocation of the action manager was rejected because it would bypass the
normal authorization and hook path. The alternative output-gated continuation rule remains viable,
but adopting it is a behavioral change and requires the explicit resolution captured in the delta.

Language-package cleanup, action-registry expansion, and quantitative measurement policy are not
asserted as implemented operation behavior by this retrospective record; each requires a separate
current decision if it remains desired.
