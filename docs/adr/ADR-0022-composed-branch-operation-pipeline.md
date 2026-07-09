# ADR-0022: Composed branch operation pipeline

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: operations
- **Date**: 2026-07-09
- **Relations**: extends ADR-0021

## Context

`Branch.operate()` is the composed operation entry point. Its preparer turns public arguments into
`ChatParam` or `RunParam`, optional `ParseParam`, and optional `ActionParam`. A CLI model or a request
for stream persistence selects `RunParam`; action enablement carries an explicit concurrent or
sequential strategy (`lionagi/session/branch.py`, `lionagi/operations/operate/operate.py`,
`lionagi/operations/types.py`).

Structured responses and actions require a response shape that differs between request and result.
When a base model, additional fields, reasoning, or actions are requested, `operate()` constructs an
internal `Operative`: action requests are exposed to the model, while action responses are excluded
from the request schema and included in the response schema (`lionagi/operations/operate/step.py`,
`lionagi/operations/schema/structure.py`).

After one `Middle` invocation, the coordinator applies the caller's validation policy. It only
executes tools when an `ActionParam` exists and the returned model or mapping contains
`action_requests`. Execution uses the normal `act()` path, including authorization, hooks, event
logging, message recording, and the selected execution strategy, then enriches the original result
with `action_responses` (`lionagi/operations/operate/operate.py`,
`lionagi/operations/act/act.py`).

The current public contract contains accumulated seams. `skip_validation=True` returns immediately
after the adapter and therefore also skips action execution. A caller-supplied `operative` is
accepted but deliberately replaced with `None` by the preparer. `Structure` exists in parameter
types and parsing internals but is not selectable through the composed branch façade.

## Decision

`operate()` remains the single coordinator that composes adapter selection, response-shape
construction, validation policy, authorized action execution, and result enrichment.

```text
Caller
  │
  v
Branch.operate ──> prepare parameters ──> choose or accept Middle
                                             │
                                             v
                                     one adapter invocation
                                             │
                                  ┌──────────┴──────────┐
                                  v                     v
                           validation policy     early raw return
                                  │               (skip_validation)
                                  v
                        action_requests present?
                                  │ yes
                                  v
                     authorize + hooks + act + record
                                  │
                                  v
                         enrich original result
```

The load-bearing invariants are:

- Unless the caller supplies a `Middle`, `RunParam` or a CLI model selects `run_and_collect`; all
  other calls select `communicate`.
- When an `ActionParam` exists, `operate()` obtains the selected branch tool schemas and publishes
  them to the adapter. The generated request model excludes `action_responses`, and its response
  model includes them.
- The selected `Middle` is invoked exactly once by `operate()`. The adapter owns message persistence
  and may internally stream or loop under ADR-0021.
- `skip_validation=True` is a raw-result short circuit. It bypasses outer type enforcement and the
  entire outer action phase.
- Tool execution requires both explicit action enablement and structured `action_requests`; arbitrary
  text does not trigger an action. All accepted requests go through `act()` rather than a transport-
  specific tool path.
- `handle_validation` governs a returned value that is not an instance of the caller's requested
  model: return the value, return `None`, or raise. Successful action responses augment rather than
  replace the original structured result.

## Consequences

API and CLI adapters share one structured-output and tool-execution stack. Authorization and hooks
remain effective for model-requested actions regardless of transport, and callers can inject a
specialized adapter without duplicating response or action policy.

The coordinator carries several responsibilities and depends on a generated response model that is
not part of the public contract. The raw-result shortcut has broader effects than its name suggests,
and currently accepted arguments can imply capabilities that the façade does not honor.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Deprecate and remove the ignored public `operative` argument, or define and implement a precedence rule that honors it while preserving generated request and response fields. | S | (filled at issue-open time) |
| 2 | Expose `Structure` through the chosen public branch APIs and parameter builders, or mark it internal and remove it from public parameter types; add one end-to-end public-path test. | S | (filled at issue-open time) |
| 3 | Split the raw-result shortcut into explicit validation and post-processing controls, or rename and document `skip_validation` so callers know that it also disables outer action execution. | S | (filled at issue-open time) |

## Notes

Separate API and CLI operation coordinators were rejected because they would duplicate structured
validation and action policy. Moving outer action execution into every `Middle` was rejected because
it would let adapters diverge from the common authorization, hook, logging, and enrichment path.
