# ADR-0132: Batched LNDL rounds for API-model legs

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: implemented — D1-D3 are implemented and the D4 pilot ran (D4, Result);
  at the merged code it does not meet the rule by the checks fixed before the run, and would by a
  substance reading of one check, so `lndl` stays an opt-in setting and is not recommended
- **Area**: operations
- **Date**: 2026-10-17
- **Relations**: extends ADR-0024 (LNDL operate integration adapter) and ADR-0041 Amendment 1
  (API-model `li agent` legs); read with ADR-0051 (LNDL language boundary) and ADR-0130 (per-request
  input ceiling)

## Context

An API-model `li agent` leg runs lionagi's own tool loop, `Branch.ReAct(tools=True,
max_extensions=N)` (`lionagi/cli/agent.py:395-399`, ADR-0041 Amendment 1). Each ReAct analysis round
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
(`lionagi/operations/lndl_middle/lndl_middle.py:381-435`; ADR-0024 D3-D4). The same one-read task
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
(`lndl_middle.py:311-323`), so a model could answer beside a read it never saw. The answer model
has one field, `answer: str`, and accepts only text the model wrote: an `OUT{}` that binds a
call's result from an earlier round (a mapping, a list, or nothing from a failed call) is refused,
and the refusal tells the model to read the result and write the answer as text. The read tools
return mappings (`lionagi/tools/coding.py:455-671`, `lionagi/tools/context/context.py:228-432`),
so an answer is never a raw tool result.

Every LNDL round's request names the leg's tools, their parameters and descriptions, whatever the
provider's prompt-cache placement. Before this decision the Middle stripped the tool schemas from its
rounds, and only an endpoint that places the prompt cache rendered the branch's tool set into the
request (`lionagi/operations/chat/_prepare.py:209-227`), so on any other endpoint the model would
have been asked to call tools it was never shown. The Middle now passes the branch's tool schemas
to every round (`lndl_middle.py:369-372`).

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
- A round with neither a `<lact>` nor an `OUT{}` in an `lndl` block is a repair round, a round
  with no block at all included. ADR-0024 D3 reads a round with no `lndl` block as continued
  reasoning and starts the next round with no error. Here every round must call or answer, so a
  round that does neither has dropped whatever it meant to do, and a call written in other markup,
  such as a provider's own tool-call tags, is one the model believes it made. The refusal tells the
  model that nothing in the round ran, that a call written any other way, such as an `<invoke>`
  tag, is not run, and how a block opens and closes. A fence is read whether it is indented,
  longer than three characters or ends its lines with CRLF; an `lndl` block quoted inside
  another fence is quoted text and is not read.
  The round costs what a continued round would have; only what the next round is told changes.
- A refused `OUT{}`, an over-limit round, a round that both calls and answers and a round that
  neither calls nor answers are repair rounds and count against the round budget. A turn that spends the budget
  without an accepted `OUT{}` ends `failed`: the Middle raises its error, which ends the turn the
  way any turn failure does, a provider's refusal included. The session records `failed` with the
  error's message, `li agent` reports the error and exits non-zero, and the run's cause file records
  a cause that is not a provider error. This differs from ReAct, which on reaching its bound still
  asks for an answer.
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

#### Result

The pilot is `benchmarks/lndl_pilot/`: 24 read-only questions about lionagi's own source, each with
an acceptance check, on `anthropic/claude-haiku-5-5` with read tools and `max_extensions: 10`. Each
task's two runs are adjacent in time and the loop that goes first alternates by task. The per-task
tables of requests, tool calls, wall time and acceptance are `runs/<run>/table.md` there.

| run | lionagi | loop | accepted | requests | per accepted task | tool calls |
|---|---|---|---|---|---|---|
| r2 | `4d373f9c` | react | 23 / 24 | 94 | 4.09 | 56 |
| r2 | `4d373f9c` | lndl | 18 / 24 | 119 | 6.61 | 34 |
| r3 | `aca2e2e9` | react | 22 / 24 | 95 | 4.32 | 59 |
| r3 | `aca2e2e9` | lndl | 23 / 24 | 79 | 3.43 | 37 |
| r4 | `d8838f28` | react | 23 / 24 | 91 | 3.96 | 59 |
| r4 | `d8838f28` | lndl | 22 / 24 | 80 | 3.64 | 47 |

r2 fails the rule and r3 passes it, and one change to the Python package separates them: in r2 an `lndl` round with no
block continued with no error, and in r3 it is the repair round D2 describes. In both runs 17 of the
24 `lndl` runs opened with a call written as an `<invoke>` tag outside any block. In r2, 12 of those
recovered on their own and 5 (`t03`, `t04`, `t17`, `t18`, `t19`) repeated the tag until the budget
ran out at 11 requests and were not accepted; 64 of the 119 `lndl` requests went to rounds with no
block. In r3 each of the 17 wrote a block in its next round, and 18 of the 79 requests went to rounds
with no block.

- In r3 the accepts clause holds by one task, and the ReAct side moved by one task between r2 and r3
  (in r3 its `t04` run made no tool call and was not accepted), so r3 shows that `lndl` accepted no
  fewer tasks, not that it accepts more. The request saving is the larger effect: 79 requests
  against 95.
- `t06`'s check expects `100000` and all four answers to it wrote `100,000`. Read as accepting that,
  r3 is `lndl` 24 at 3.29 requests per accepted task and ReAct 23 at 4.13, and r2 still fails.
- The tool calls counted are the calls that ran. The most any `lndl` run made was four in r2, three
  in r3 and five in r4 (`t21` and `t22`). A round that declares more than 16 calls is refused and
  none of its calls runs (D2), so these counts cannot show whether a round reached the limit, and
  the limit is still unmeasured.
- r4 ran the merged code (`d8838f28`), whose repair round also tells the model how a block opens
  and closes, reads indented, longer and CRLF fences, and treats an empty block as a repair round
  (D2). By the checks fixed before the first run, it fails the rule: `lndl` used fewer requests per
  accepted task (80 requests, 3.64, against 91 and 3.96) and accepted one task fewer (22 against
  23). The deciding task is `t20`, and the outcome rests on its check. The question asks which
  module; the `lndl` run named it by its import path, `lionagi.tools.code.search`, and said it had
  not opened the file, and the check asks for the file path `lionagi/tools/code/search.py`. Read on
  substance, `t20` alone makes r4 meet the rule (`lndl` 23 at 3.48 requests per accepted task
  against ReAct 23 at 3.96). `t06` alone (both r4 answers name `_SHELL_CONTROL` and give the cap, as
  `100,000` or `100_000`) adds one task to each side and leaves the outcome as it is; with both,
  `lndl` is 24 at 3.33 against 24 at 3.79. r2 fails and r3 passes under every one of these
  readings. 17 of the 24 `lndl` runs again opened with an `<invoke>` tag, each wrote a block in its
  next round, 17 of the 80 requests went to rounds with no block, and no run reached the round
  budget.
- r3 and r4 split the accepts clause by one task in opposite directions (`t04` on the ReAct side in
  r3, `t20` on the `lndl` side in r4), so 24 tasks cannot settle it, and in r4 the split rests on how
  one check reads one answer. The request saving held in both runs: 79 requests against 95, and 80
  against 91. D4 fixes each task's check before the first run and lets it decide, so under D4 `lndl`
  is not a recommended setting and the default stays `react`. A recommendation needs a run whose
  `t06` and `t20` checks accept the answer's substance, fixed before that run starts, or a larger
  task set, and a later decision.
- The checks are substring checks, so a wrong answer that contains the expected words can pass. The
  answers where that is easiest, the 24 accepted answers to `t09`, `t11`, `t12` and `t22` (both
  loops, all three runs), were read by hand, and all of them are correct.
- The counts of runs that opened with an `<invoke>` tag and of requests that went to rounds with no
  block are read from each leg's own output, which the pilot writes beside its results and does not
  commit; the committed files give the per-task requests, tool calls and acceptance.
- All 144 legs exited 0. In r2 five `lndl` legs spent all 11 requests, on the code before the repair
  round; whether that code ended such a turn `failed`, as D2 now requires, has not been traced. No
  leg in r3 or r4 reached the budget, so the pilot does not exercise D2's exhausted-turn exit.

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
