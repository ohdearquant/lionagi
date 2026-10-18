# ADR-0132: Batched LNDL rounds for API-model legs

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: partial — D1-D3 are implemented; the D4 pilot has not run
- **Area**: operations
- **Date**: 2026-10-17
- **Relations**: extends ADR-0024 (LNDL operate integration adapter) and ADR-0041 Amendment 1
  (API-model `li agent` legs); read with ADR-0051 (LNDL language boundary) and ADR-0130 (per-request
  input ceiling)

## Context

An API-model `li agent` leg runs lionagi's own tool loop, `Branch.ReAct(tools=True,
max_extensions=N)` (`lionagi/cli/agent.py:393-397`, ADR-0041 Amendment 1). Each ReAct analysis round
is at least one model request (a reply that fails to parse is asked for again), and a round's tool
calls run through `act()`, concurrently unless the model asks for them in sequence
(`lionagi/operations/ReAct/ReAct.py:98`, `:205`, `:542`). After the last analysis round ReAct always
makes one more request for the answer (`ReAct.py:631-684`). A ReAct turn therefore costs its
analysis rounds plus one. A model that asks for a read cannot see the result in that round, so it
usually asks for another round to look at it before saying it is done: the leg's own test of a
one-read task scripts three requests, the read round, a round that sees the result and stops, and
the answer (`tests/cli/test_agent_api_provider.py:172`).

The LNDL Middle has no separate answer request. In a round a model declares tool calls as `<lact>`
aliases and may write its answer as an `OUT{}` block. A round with no `OUT{}` runs every declared
call, concurrently and through the ordinary `act()` path with its permissions and hooks, and the
next round sees the results; a round whose `OUT{}` validates ends the turn
(`lionagi/operations/lndl_middle/lndl_middle.py:367-421`; ADR-0024 D3-D4). The same one-read task
takes two requests: the round that declares the read, and the round that reads the result and
writes the answer. With the same number of rounds, then, the batched loop saves the request ReAct
makes for the answer, one per turn, a turn with no tool calls included (two requests against one).
Whether a model takes the same number of rounds in each loop, and how many repair rounds the
batched loop costs it, is what the pilot below measures.

Nothing measures it today. A leg's branch holds its requests and tool calls as messages, but no leg
reports how many requests a task took, so a claim that one loop is cheaper is an estimate.

## Decision

### D1: the loop is a profile choice, ReAct by default

An agent profile that declares `api_tools` may also declare `api_loop`, either `react` (the default,
today's loop) or `lndl`. Any other value is refused before a run exists, with the same error shape
as an unknown `api_tools` value. A profile that sets `api_loop` without `api_tools` is refused too,
since the key would otherwise be ignored on a leg whose provider runs its own loop.

An `lndl` leg runs `branch.operate(instruction=..., middle=build_lndl_middle(round_budget=1 +
max_extensions))` with the same read tool set, guards, recall and timeout as a ReAct leg. It keeps
calls and the answer in separate rounds, and its guidance tells the model so: a round either
declares `<lact>` calls, with no `OUT{}`, or writes the answer, with no `<lact>`. A round that does
both is a repair round, in which none of its calls runs and its answer is not taken. Left to the
Middle's own rule, such a round would run only the calls its `OUT{}` binds and drop the rest
(`lndl_middle.py:301-310`), so a model could answer beside a read it never saw. The answer model
has one field, `answer: str`, and accepts only text the model wrote: an `OUT{}` that binds a
call's result from an earlier round (a mapping, a list, or nothing from a failed call) is refused,
and the refusal tells the model to read the result and write the answer as text. The read tools
return mappings (`lionagi/tools/coding.py:454-670`, `lionagi/tools/context/context.py:228-432`),
so an answer is never a raw tool result.

Every LNDL round's request names the leg's tools, their parameters and descriptions, whatever the
provider's prompt-cache placement. Before this decision the Middle stripped the tool schemas from its
rounds, and only an endpoint that places the prompt cache rendered the branch's tool set into the
request (`lionagi/operations/chat/_prepare.py:209-227`), so on any other endpoint the model would
have been asked to call tools it was never shown. The Middle now passes the branch's tool schemas
to every round (`lndl_middle.py:355-358`).

The branch records `api_loop` beside the tool set and bound under `metadata["api_launch"]`, and a
resume runs the recorded loop. A record with no `api_loop` was written before this key existed and
runs `react`; a record naming a loop this version does not run is refused before a run exists, as an
unknown tool set is.

### D2: bounds and endings of an LNDL turn

- A round may declare at most 16 `<lact>` calls. A round that declares more is a repair round: no
  call runs, and the model is told the limit and asked to split the work.
- From the second round on, each round's instruction names its number and the budget, and the
  last says it is the last. A repair round's error is shown to the round after it only.
- Each call keeps its tool's own limits (the read tools' size caps and the path guards); the round
  adds no timeout of its own, and the leg's `--timeout` bounds the whole turn, as for ReAct.
- A failed call is a result: `act()` runs with errors suppressed, so the call's output is nothing or
  an error mapping, the next round's request carries it, and the model decides what to do. It never
  becomes the answer (D1).
- A refused `OUT{}`, an over-limit round and a round that both calls and answers are repair rounds
  and count against the round budget. A turn that spends the budget without an accepted `OUT{}`
  ends `failed`: the Middle raises its error, which ends the turn the way any turn failure does,
  a provider's refusal included. The session records `failed` with the error's message, `li agent`
  reports the error and exits non-zero, and the run's cause file records a cause that is not a
  provider error. This differs from ReAct, which on reaching its bound still asks for an answer.
- The results a round feeds back count against the request ceiling of ADR-0130 like any other
  message; a round whose results would cross it is handled by that record, not by a second rule here.

### D3: every leg reports what it spent

When an API-model leg's turn ends, however it ends, the branch records under
`metadata["api_launch"]["spend"]`:

- `requests`: the requests the leg sent, counted where each model the branch uses sends them (the
  chat and the parse model, which a resumed branch builds as two), so repair, parse and failed
  requests count; a request its endpoint retries counts once;
- `tool_calls`: the tool calls that ran;
- `wall_seconds`: elapsed time on a monotonic clock, rounded to milliseconds.

The record totals this `li agent` invocation: its turn and any queued messages it drains as further
turns, each turn counted from a baseline taken when it starts. A resume is a new invocation and
starts from zero, so a resumed branch's earlier turns are not counted again. The record is written
before the branch snapshot that ends the turn, so a resume and a reader of the run directory find
it. After each turn `li agent` prints the totals on its hint channel (stderr) as one line,
`[api] spend: <requests> request(s), <tool_calls> tool call(s), <wall_seconds>s` with the seconds
to one decimal, before its resume hint when one prints. Both loops record the same keys, so a
comparison reads two records and never an estimate.

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
