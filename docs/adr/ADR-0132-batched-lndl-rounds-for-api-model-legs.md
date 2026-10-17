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
max_extensions=N)` (`lionagi/cli/agent.py:276-308`, ADR-0041 Amendment 1). Each ReAct analysis round
is one model request, and a round's tool calls run concurrently through `act()`
(`lionagi/operations/ReAct/ReAct.py:98`, `:205`, `:542`). After the last analysis round ReAct always
makes one more request for the answer (`ReAct.py:631-684`). A ReAct turn therefore costs its
analysis rounds plus one. A model that asks for a read cannot see the result in that round, so it
usually asks for another round to look at it before saying it is done: the leg's own test of a
one-read task scripts three requests, the read round, a round that sees the result and stops, and
the answer (`tests/cli/test_agent_api_provider.py:135`).

The LNDL Middle has no separate answer request. In a round a model declares tool calls as `<lact>`
aliases and may write its answer as an `OUT{}` block. A round with no `OUT{}` runs every declared
call, concurrently and through the ordinary `act()` path with its permissions and hooks, and the
next round sees the results; a round whose `OUT{}` validates ends the turn
(`lionagi/operations/lndl_middle/lndl_middle.py:217-262`; ADR-0024 D3-D4). The same one-read task
takes two requests: the round that declares the read, and the round that reads the result and
writes the answer. The difference is at most the one request per turn that ReAct spends on the
answer after the model has stopped, and only in turns where the model would have taken that extra
round; whether models do, and how often, is what the pilot below measures.

Nothing measures it today. A leg's branch holds its requests and tool calls as messages, but no leg
reports how many requests a task took, so a claim that one loop is cheaper is an estimate.

## Decision

### D1: the loop is a profile choice, ReAct by default

An agent profile that declares `api_tools` may also declare `api_loop`, either `react` (the default,
today's loop) or `lndl`. Any other value is refused before a run exists, with the same error shape
as an unknown `api_tools` value. A profile that sets `api_loop` without `api_tools` is refused too,
since the key would otherwise be ignored on a leg whose provider runs its own loop.

An `lndl` leg runs `branch.operate(instruction=..., middle=build_lndl_middle(round_budget=1 +
max_extensions))` with the same read tool set, guards, recall and timeout as a ReAct leg. Its answer
model has one field, `answer: str`, and accepts only text the model wrote: a call's result placed in
`OUT{}` (a mapping, a list, or nothing from a failed call) is refused, and the refusal tells the
model to read the result and write the answer in the next round. The read tools return mappings
(`lionagi/tools/coding.py:454-670`), so an answer is never a raw tool result.

Every LNDL round's request names the leg's tools, their parameters and descriptions, whatever the
provider's prompt-cache placement. Today the Middle strips the tool schemas from its rounds
(`lndl_middle.py:212`) and only an endpoint that places the prompt cache renders the branch's tool
set into the request (`lionagi/operations/chat/_prepare.py:209-227`), so on any other endpoint the
model would be asked to call tools it was never shown.

The branch records `api_loop` beside the tool set and bound under `metadata["api_launch"]`, and a
resume runs the recorded loop. A record with no `api_loop` was written before this key existed and
runs `react`; a record naming a loop this version does not run is refused before a run exists, as an
unknown tool set is.

### D2: bounds and endings of an LNDL turn

- A round may declare at most 16 `<lact>` calls. A round that declares more is a repair round: no
  call runs, and the model is told the limit and asked to split the work.
- Each call keeps its tool's own limits (the read tools' size caps and the path guards); the round
  adds no timeout of its own, and the leg's `--timeout` bounds the whole turn, as for ReAct.
- A failed call is a result: `act()` runs with errors suppressed, so the call's output is nothing or
  an error mapping, the next round's request carries it, and the model decides what to do. It never
  becomes the answer (D1).
- A refused `OUT{}` and an over-limit round are repair rounds and count against the round budget.
  The last round's instruction says it is the last. A turn that spends the budget without an
  accepted `OUT{}` ends `failed`, with the Middle's error as its recorded cause, the same way a turn
  ends on a provider's refusal; it never ends as a crash with no recorded cause. This differs from
  ReAct, which on reaching its bound still asks for an answer.
- The results a round feeds back count against the request ceiling of ADR-0130 like any other
  message; a round whose results would cross it is handled by that record, not by a second rule here.

### D3: every leg reports what it spent

When an API-model leg's turn ends, however it ends, the branch records under
`metadata["api_launch"]["spend"]`:

- `requests`: the requests the leg's model sent during this turn, counted where the model sends them,
  so repair and parse requests and failed requests count;
- `tool_calls`: the tool calls that ran during this turn;
- `wall_seconds`: the turn's elapsed time on a monotonic clock.

Each counts this turn only, from a baseline taken when the turn starts, so a resumed branch's earlier
turns are not counted again. The record is written before the branch snapshot that ends the turn, so
a resume and a reader of the run directory find it. `li agent` prints the three numbers on one line,
`[api] spend: <requests> request(s), <tool_calls> tool call(s), <wall_seconds>s`, before its resume
hint. Both loops record the same keys, so a comparison reads two records and never an estimate.

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
- An `lndl` leg's answer is text the model wrote; a model that writes poor LNDL spends repair rounds,
  the round budget bounds that spend, and a turn that exhausts it ends `failed` where ReAct would
  still have asked for an answer. The pilot's acceptance column shows what that costs.
- The expected saving is small: at most one request per turn, the answer request ReAct makes after
  the model has stopped. If models seldom take ReAct's extra round, the pilot will show no saving and
  the default stays as it is.
- The 16-call limit is a starting value with no measurement behind it; the pilot's tables report it
  as unmeasured and are the evidence for changing it.

## Alternatives considered

### Change ReAct to answer in the round that asks

ReAct's structure separates the analysis rounds from the answer, and its callers depend on that
shape. LNDL already has a round that can end the turn with an answer, so the batched loop needs no
change to ReAct.

### Make `lndl` the default for API-model legs now

No leg reports its spend today, so there is nothing to base a default on. D3 and D4 produce that
evidence first.

### Accept a call's result as the answer

An `OUT{}` that projects a call's result would let a turn end in the round that asks, one request in
place of two. The read tools return mappings, a failed call returns nothing or an error mapping, and
the model has not seen any of them when it writes the projection, so the leg would hand back an
answer nobody read. Text the model wrote after reading the result is the answer.

### Use the provider's native parallel tool calls

A model can already ask for several tools in one ReAct round, and they already run concurrently.
The request saved here is ReAct's separate answer request, which native tool calling does not save.
