# ADR-0132: Batched LNDL rounds for API-model legs

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: operations
- **Date**: 2026-10-17
- **Relations**: extends ADR-0024 (LNDL operate integration adapter) and ADR-0041 Amendment 1
  (API-model `li agent` legs); read with ADR-0051 (LNDL language boundary) and ADR-0130 (per-request
  input ceiling)

## Context

An API-model `li agent` leg runs lionagi's own tool loop, `Branch.ReAct(tools=True,
max_extensions=N)` (`lionagi/cli/agent.py:276-308`, ADR-0041 Amendment 1). Every round of that loop
is one model request. A round may ask for several tool calls, and `act()` runs them concurrently
(`lionagi/operations/ReAct/ReAct.py:98`, `:205`, `:542`), but the loop cannot answer in the round
that asked: the results come back as messages and a further request turns them into the answer.
A task whose answer is a function of one batch of reads therefore costs at least two requests, and
the analysis and extension structure of each ReAct round is paid for again in every one.

The LNDL Middle already has the shape this cost wants. In one round a model declares its tool calls
as `<lact>` aliases and its answer as an `OUT{}` projection that refers to them. The Middle runs
the calls the projection reaches, concurrently and through the ordinary `act()` path with its
permissions and hooks, puts their results into the projection, and validates it, all without
another model request (`lionagi/operations/lndl_middle/lndl_middle.py:158-182`, `:241-252`;
ADR-0024 D3-D4). A round with no `OUT{}` runs every declared call and continues, so a task that
needs to read the results before it can answer still can.

Nothing measures the difference today. A leg's branch holds its requests and tool calls as
messages, but no leg reports how many requests a task took, so a claim that one loop is cheaper is
an estimate.

## Decision

### D1: the loop is a profile choice, ReAct by default

An agent profile that declares `api_tools` may also declare `api_loop`, either `react` (the default,
today's loop) or `lndl`. Any other value is refused before a run exists, with the same error shape
as an unknown `api_tools` value. A profile that sets `api_loop` without `api_tools` is refused too,
since the key would otherwise be ignored on a leg whose provider runs its own loop.

An `lndl` leg runs `branch.operate(instruction=..., middle=build_lndl_middle(round_budget=1 +
max_extensions))` with the same read tool set, guards, recall and timeout as a ReAct leg, against a
one-field answer model (`answer: str`). The branch records `api_loop` beside the tool set and bound
under `metadata["api_launch"]`, and a resume runs the recorded loop; a record naming a loop this
version does not run is refused before a run exists, as an unknown tool set is.

### D2: bounds on a round

- A round may declare at most 16 `<lact>` calls. A round that declares more is a repair round: no
  call runs, and the model is told the limit and asked to split the work (`Retry` with that error).
- Each call keeps its tool's own limits (the read tools' size caps and the path guards); the round
  adds no timeout of its own, and the leg's `--timeout` bounds the whole turn, as for ReAct.
- A call's failure is a result the model sees, never an exception that ends the turn (the Middle's
  existing `suppress_errors=True`).
- The results a round feeds back count against the request ceiling of ADR-0130 like any other
  message; a round whose results would cross it is handled by that record, not by a second rule here.

### D3: every leg reports what it spent

When an API-model leg's turn ends, however it ends, the branch records under
`metadata["api_launch"]["spend"]` the number of model requests, the number of tool calls, and the
turn's wall time, counted from the branch's own messages and clock, and `li agent` prints them on
its final status line. Both loops record the same three numbers, so a comparison reads two records
and never an estimate.

### D4: one pilot before any default changes

The pilot runs one task set twice, once per loop, with the same model, tool set, bound and
instructions, on one profile pair that differs only in `api_loop`. The task set is fixed before the
first run and has at least 20 read-only tasks of the kind the leg is used for, each with a check
that decides whether its answer is accepted. The result is a per-task table of requests, tool
calls, wall time and acceptance for both loops, published with the pilot's commit. `lndl` becomes
a recommended profile setting only if it uses fewer requests per accepted task in total and accepts
no fewer tasks; the default stays `react` until a later decision changes it.

## Consequences

- An operator can try the batched loop on one profile without changing any other leg.
- Every API-model leg now reports its spend, which is useful whatever the pilot finds.
- An `lndl` leg's answer is whatever text `OUT{}` assembles; a model that writes poor LNDL spends
  repair rounds, and the round budget bounds that spend.
- The 16-call limit is a starting value with no measurement behind it; the pilot's tables are the
  evidence for changing it.

## Alternatives considered

### Change ReAct to answer in the round that asks

ReAct's structure separates the request for tools from the answer, and its callers depend on that
shape. LNDL already has a projection that names results before they exist, so the batched round
needs no change to ReAct.

### Make `lndl` the default for API-model legs now

No leg reports its spend today, so there is nothing to base a default on. D3 and D4 produce that
evidence first.

### Use the provider's native parallel tool calls

A model can already ask for several tools in one ReAct round, and they already run concurrently.
The request saved here is the one after the tools return, which native tool calling does not save.
