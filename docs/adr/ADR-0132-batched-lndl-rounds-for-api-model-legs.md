# ADR-0132: Batched LNDL rounds for API-model legs

- **Status**: Accepted (2026-10-18, #3628); D4 is decided by `r5`, as Implementation-status states
- **Kind**: Aspirational
- **Implementation-status**: implemented — D1-D3 are implemented and the D4 pilot ran four times
  (D4, Result); read against the source (Amendment 1), `r5`, the run that decides, does not meet
  the rule: `lndl` accepted one task fewer than ReAct, a margin that one answer read the other way
  would reverse (D4, Result). `lndl` stays an opt-in setting and is not recommended, and the default stays `react`
- **Area**: operations
- **Date**: 2026-10-17
- **Relations**: extends ADR-0024 (LNDL operate integration adapter) and ADR-0041 Amendment 1
  (API-model `li agent` legs); read with ADR-0051 (LNDL language boundary) and ADR-0130 (per-request
  input ceiling)

## Amendment 1 (2026-10-19): the reading of each answer decides acceptance

As first written, D4 had each task's check decide whether its answer is accepted. The checks are
substring and regular-expression tests, so they accept a wrong answer that repeats the expected
words and reject a right answer written in other words, and r2 to r4 had shown both (D4, Result).
D4 now treats each check as a first pass, and reading the answer against its task's source line
decides. The change was made after r5 had run, when its result had first been written as meeting
the rule, and it decides r5:

| rule | `lndl` accepted | ReAct accepted | `lndl` per accepted task | ReAct per accepted task | D4 |
|---|---|---|---|---|---|
| each task's check, as r5 ran them | 23 / 24 | 23 / 24 | 3.43 | 4.30 | met |
| the reading | 22 / 24 | 23 / 24 | 3.59 | 4.30 | not met |

The two differ in one answer, the `lndl` answer to `t24`, which says `lionagi/mcp/config.py`
defines no `RUNS_DIR`, where line 19 of that file is `RUNS_DIR = RUNS_ROOT`; its check accepted it.
After r5 the `t24` check was also corrected to require the assignments themselves. Run against the
committed r5 answers, the corrected checks reject that answer and agree with r5's checks on every
other one, so the checks as they now stand give 22 against 23 as well. Each run's `reading.jsonl`
records the reading answer by answer, and the pilot's `summarize.py` computes the rule both ways
from it and the run's results (`d4_rule_by_checks` and `d4_rule_by_substance` in each run's
`summary.json`).

## Amendment 2 (2026-10-26): `lndl-calls` shows the tools only as LNDL calls

`api_loop` takes a third value, `lndl-calls`. It runs the `lndl` loop of D1 and D2 with the same
rounds, bounds and endings, and differs only in how the model is shown its tools. The first round's
guidance lists each tool as an LNDL call: its signature, with each parameter's type and default, the
tool's description and each parameter's description under it, and an example round that calls
(`_render_tool_calls` in `lndl_middle.py`). No round's request carries the tools' schemas: the
Middle passes an empty tool list to every round, and while a round runs, an endpoint that places
the prompt cache leaves the `Tools:` section out of its cached system block (`_no_tool_section` in
`lionagi/operations/chat/_prepare.py`). Every later round's request carries the first round's
instruction as history, so each request still names the tools, as calls. D1's rule that every
round's request names the tools holds for `lndl-calls` in that form, and stands as written for
`lndl`. The launch record names `lndl-calls`, and a resume runs it, as D1 says of every loop.

The reason is the round in which nothing runs. With the schemas in the request, a model writes some
of its calls as the provider's own tool-call markup, an `<invoke>` tag, which the loop does not run;
that round is a repair round (D1), and the turn spends a request on it. Of 300 such rounds counted
on 50 coding tasks, 247 carried an `<invoke>` tag. Measured on 26
code-review tasks on `anthropic/claude-haiku-5-5`, one run per loop per task with a task's two runs
adjacent in time, on profiles identical apart from `api_loop`:

| | `lndl` | `lndl-calls` |
|---|---|---|
| rounds in which nothing ran | 22 | 2 |
| repair notices, all causes | 26 | 9 |
| requests | 142 | 95 |
| cost | | 12.7% lower |
| cost, the tool section priced back in | | 11.4% lower |

Recall of the files each task's reference review cites was equal (paired difference +0.006,
standard error 0.023). Part of the cost difference is the tool section `lndl-calls` no longer
sends; priced back in at each request's own cache class, it accounts for about a tenth of the
difference, and the rest is fewer requests and shorter output. The measurement compares
`lndl-calls` with `lndl`, never with ReAct, so it does not reopen D4: `lndl-calls` is an opt-in
setting as `lndl` is, and the default stays `react`.

## Amendment 3 (2026-10-26): an `lndl-calls` leg is asked for every read it already needs

An `lndl-calls` leg's guidance ends with one more sentence: "In each calling round, declare every
read you already know you need, up to 16 calls, and answer only after reading what the answer
depends on." The number is the round's call limit (`_API_LNDL_MAX_ACTIONS` in
`lionagi/cli/agent.py`), from which the sentence is built, so the two cannot drift apart. An `lndl`
leg's guidance is unchanged, because the sentence was measured on `lndl-calls` only.

The reason is how little an `lndl-calls` leg read before it answered. It was measured on 26
code-review tasks on `anthropic/claude-haiku-5-5`, on source identical apart from this sentence, in
two passes over the tasks. Each pass runs each task once per arm, with a task's two runs adjacent in
time, and the second pass reverses which arm runs first. The first pass covered 23 of the tasks and
the second all 26, so the figures below are over 49 pairs:

| | without the sentence | with it |
|---|---|---|
| tool calls per run | 9.7 | 16.4 |
| requests per run | 4.04 | 4.29 |
| recall of the files the reference review cites | 0.175 | 0.254 |
| verdicts that match the reference | 36 of 49 | 39 of 49 |
| cost per run | | 36% higher |

Recall rose in 29 pairs, fell in 7 and was equal in 13 (paired difference +0.079, standard error
0.022). Averaging each task's pairs first, it rose on 19 of the 26 tasks and fell on 3 (+0.075,
standard error 0.021). Precision, the share of a run's cited files that the reference also cites,
fell: over the 40 pairs in which both runs cited a file it went from 0.693 to 0.581 (paired
difference -0.111, standard error 0.038; lower in 26 pairs, higher in 8, equal in 6). Each pass
shows both changes on its own: recall +0.080 and +0.077, precision -0.129 and -0.095. The sentence
buys recall at a cost in precision and in money. The verdict counts are close: in the second pass,
on 12 of the 26 tasks one arm's verdict matched the reference and the other's did not, 7 times the
arm with the sentence, and one task split that way in both passes.

## Context

An API-model `li agent` leg runs lionagi's own tool loop, `Branch.ReAct(tools=True,
max_extensions=N)` (`lionagi/cli/agent.py:690-694`, ADR-0041 Amendment 1). Each ReAct analysis round
is at least one model request, and a reply that fails to parse costs more, since it is sent back to
the model to be reformatted (`lionagi/operations/communicate/communicate.py:119-128`,
`lionagi/operations/parse/parse.py:187-218`). A round's tool calls run through `act()`, concurrently
unless the model asks for them in sequence: ReAct passes `strategy="concurrent"`
(`lionagi/operations/ReAct/ReAct.py:165`, `:502`), the model's `action_strategy` field
(`lionagi/operations/ReAct/utils.py:53`) can declare `sequential`, and `operate` then runs the calls
one at a time (`lionagi/operations/operate/operate.py:318`). After
the last analysis round ReAct always makes one more request for the answer
(`ReAct.py:591-644`). A ReAct turn therefore costs its analysis rounds plus one. A model that asks for a read cannot see the result in that round, so it
usually asks for another round to look at it before saying it is done: the leg's own test of a
one-read task scripts three requests, the read round, a round that sees the result and stops, and
the answer (`tests/cli/test_agent_api_provider.py:172`).

The LNDL Middle has no separate answer request. In a round a model declares tool calls as `<lact>`
aliases and may write its answer as an `OUT{}` block. A round with no `OUT{}` runs every declared
call, concurrently and through the ordinary `act()` path with its permissions and hooks, and the
next round sees the results; a round whose `OUT{}` validates ends the turn
(`lionagi/operations/lndl_middle/lndl_middle.py:493-554`; ADR-0024 D3-D4). The same one-read task
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
(`lndl_middle.py:411-423`), so a model could answer beside a read it never saw. The answer model
has one field, `answer: str`, and accepts only text the model wrote: an `OUT{}` that binds a
call's result from an earlier round (a mapping, a list, or nothing from a failed call) is refused,
and the refusal tells the model to read the result and write the answer as text. The read tools
return mappings (`lionagi/tools/coding.py:462-672`, `lionagi/tools/context/context.py:257-573`),
so an answer is never a raw tool result.

Every LNDL round's request names the leg's tools, their parameters and descriptions, whatever the
provider's prompt-cache placement. Before this decision the Middle stripped the tool schemas from its
rounds, and only an endpoint that places the prompt cache rendered the branch's tool set into the
request (`lionagi/operations/chat/_prepare.py:209-227`), so on any other endpoint the model would
have been asked to call tools it was never shown. The Middle now passes the branch's tool schemas
to every round (`lndl_middle.py:472-484`).

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
that marks its answer accepted or rejected as a first pass. Every answer is then read against the
source, and the reading decides: an answer the check accepts and the reading finds wrong is not accepted, and one
the check rejects and the reading finds right is. The result is a per-task table of requests, tool
calls, wall time and acceptance for both loops, published with the pilot's commit, and a per-task
record of each answer's reading beside its check. The pilot can run more than once as the code
changes, and each run is recorded in the Result. r2, r3 and r4 did not settle the rule (Result);
after r4 the run that decides was named as `r5`. r5 started on 2026-10-20 at 02:09 UTC (its `header.json`) with
lionagi installed from `208bcc08`, a commit on `main` and the head of its environment's checkout
(`benchmarks/lndl_pilot/runs/r5/environment.json`), and read the source at `6fa69b19`, as r2 to r4
did. It asks the same questions against the same source lines, with the `t06` and `t20` checks
corrected; those two changes, and the `t24` check changed after r5, which no run has used, depart
from the clause above that the task set is fixed before the first run. r5 is read by the rule
Amendment 1 records. The rule that the reading decides was written into D4 after r5 had run, at
03:10 UTC, so r5 is reported under the checks and under the reading; its task file
(`benchmarks/lndl_pilot/runs/tasks.r5.yaml`), rows and reading (`benchmarks/lndl_pilot/runs/r5/`)
are recorded with its result below. No later run reopens D4: a different outcome takes a new
decision. The reading is one reader's judgment, made knowing which loop wrote each answer, and no
blind second reading was done; it judges what each answer says to its question and counts
an answer right when it gives the value or name its task records, also when it corrects a premise
of the question on the way. `tasks.yaml` gives each task's source line (its `truth`), and the
pilot's README names the answers whose count turns on that rule, so that another reader can redo
it. Because the reading decides, a check corrected between runs changes
the first pass and not the outcome. `lndl` becomes a recommended profile setting only if it uses
fewer requests per accepted task in total and accepts no fewer tasks; the default stays `react`
until a later decision changes it.

#### Result

The pilot is `benchmarks/lndl_pilot/`: 24 read-only questions about lionagi's own source, each with
an acceptance check, on `anthropic/claude-haiku-5-5` with read tools and `max_extensions: 10`. Each
task's two runs are adjacent in time and the loop that goes first alternates by task. The per-task
tables of requests, tool calls, wall time and acceptance by the checks are `runs/<run>/table.md`
there, each answer's reading, beside its check and the source line it was read against, is
`runs/<run>/reading.jsonl`, and the pilot's test, `tests/benchmarks/test_lndl_pilot.py`, recomputes
the figures below and the ones this record gives for r3, r4 and r5 from the runs' files.

| run | lionagi | loop | accepted by the checks | accepted on reading | requests | per accepted task | tool calls |
|---|---|---|---|---|---|---|---|
| r2 | `4d373f9c` | react | 23 / 24 | 24 / 24 | 94 | 3.92 | 56 |
| r2 | `4d373f9c` | lndl | 18 / 24 | 18 / 24 | 119 | 6.61 | 34 |
| r3 | `aca2e2e9` | react | 22 / 24 | 23 / 24 | 95 | 4.13 | 59 |
| r3 | `aca2e2e9` | lndl | 23 / 24 | 23 / 24 | 79 | 3.43 | 37 |
| r4 | `d8838f28` | react | 23 / 24 | 24 / 24 | 91 | 3.79 | 59 |
| r4 | `d8838f28` | lndl | 22 / 24 | 23 / 24 | 80 | 3.48 | 47 |
| r5 | `208bcc08` | react | 23 / 24 | 23 / 24 | 99 | 4.30 | 65 |
| r5 | `208bcc08` | lndl | 23 / 24 | 22 / 24 | 79 | 3.59 | 43 |

Requests per accepted task divides a run's requests by the tasks accepted on reading. The lionagi
column is the commit each run's environment had installed, read from its checkout after the run
(`runs/<run>/environment.json`), and requests, tool calls and wall time come from each leg's spend
line (`runs/<run>/spend.jsonl`).

On reading, r2 fails the rule and r3 meets it with no margin. One change to the Python package
lies between them: in r2 an `lndl` round with no block continued with no error, and in r3 it is the
repair round D2 describes. With one run of each, the pilot cannot show that this change caused the
difference; the ReAct side, which the change does not touch, also moved by one task.
Read from each leg's conversation (each run's `rounds.jsonl` records the syntax every `lndl` round
used), in both runs 17 of the 24 `lndl` runs opened with a call written as an `<invoke>` tag outside
any block. In r2, 12 of those wrote a block by their fifth round, and 5 (`t03`, `t04`, `t17`, `t18`,
`t19`) wrote none before their tenth: `t03` wrote one in its tenth round, which ran its one tool
call, and the other four first wrote one in the eleventh and last round, whose notice asks for the
answer. All five answered that they could not confirm, list or determine what was asked, and none
was accepted. The other four describe reads and searches that returned nothing usable (`t18`: "Five read attempts and two grep attempts on that
file returned no usable content"), but none of those calls ran: what they report as tool results is
the model's account of calls it wrote as tags outside a block. The r3 ReAct `t04` leg is the same
case on the other loop: it made no tool call in its 3 requests and answered that none of its tool
calls returned results. 64 of the 119 `lndl` requests went to rounds with no block. In r3 each of
the 17 wrote a block in its next round, and 18 of the 79 requests went to rounds with no block.

- Every answer in r2, r3 and r4, accepted by its check or not, was read against the source line
  `tasks.yaml` records for its task. In these three runs the reading and the checks differ in ten
  answers. All six answers to `t06` are right and were rejected: the check expects `100000`, and
  each answer gives the cap as `100,000` or `100_000` and names `_MAX_OUTPUT_BYTES` and `_SHELL_CONTROL`. The r4
  `lndl` answer to `t20` is right and was rejected: the question asks which module defines the
  class, the answer names it by its import path, `lionagi.tools.code.search`, and says it did not
  open the file, and the check asks for the file path. The `lndl` answers to `t24` in all three runs
  are wrong and were accepted: each gives `RUNS_ROOT` in place of `RUNS_DIR` (r3 says no
  `RUNS_DIR` exists, r2 that its grep shows none in `lionagi/mcp/config.py`, and r4 that the module
  defines none, adding that it read the file only in part and does not rule one out), and line 19
  of that file is `RUNS_DIR = RUNS_ROOT`; the check these runs used asks for `.lionagi`, the
  default the question asks for, and the names `RUNS_DIR` and `JOBS_DIR`, which the question itself
  supplies, so it
  accepts these answers. Every other answer agrees with its check.
- On reading, r3 meets the rule with no margin: 23 tasks accepted on each side, and `lndl` used 79
  requests against 95 (3.43 per accepted task against 4.13). The ReAct side moved by one task
  between r2 and r3 (its r3 `t04` leg above), so r3 shows that `lndl` accepted no fewer tasks, not
  that it accepts more. This counts both r3 answers to `t03` as right (each says the endpoint's
  request model has no default and gives 8192, the value lionagi's CLI applies and the line the
  task records) and both r3 answers to `t05` (each gives the timeout field's default, 30000 ms; the
  ReAct answer says BashTool has no `max_tokens` field, and the `lndl` answer that the field is a
  time limit, not a token count). Counting either pair wrong leaves r3 at 22 against 22, which still
  meets the rule.
- The tool calls counted are the calls that ran. The most any `lndl` run made was four in r2, three
  in r3, five in r4 (`t21` and `t22`) and three in r5. A round that declares more than 16 calls is
  refused and none of its calls runs (D2), so these counts cannot show whether a round reached the
  limit, and the limit is still unmeasured.
- r4 ran `d8838f28`, whose repair round also tells the model how a block opens
  and closes, reads indented, longer and CRLF fences, and treats an empty block as a repair round
  (D2). On reading it fails the rule: `lndl` used fewer requests per accepted task (80 requests,
  3.48, against 91 and 3.79) and accepted one task fewer (23 against 24), so counting any one
  accepted ReAct answer wrong, such as its `t05` answer, would make r4 meet the rule. The checks
  give the same outcome through different tasks (22 against 23). 17 of the 24 `lndl` runs again
  opened with an `<invoke>` tag, each wrote a block in its next round, 17 of the 80 requests went to
  rounds with no block, and no run reached the round budget.
- On reading, r3 ties the accepts clause and r4 misses it by one task, so these runs do not settle
  it. The request saving held in both: 79 requests against 95, and 80 against 91.
- `r5`, the run D4 names, ran with lionagi installed from `208bcc08` (13 commits past `d8838f28`,
  none of which changes `lionagi/lndl/` or the LNDL Middle; both loops ran that same code). Before it
  started, the `t06` check was changed to accept the value written as `100000`, `100,000`, `100_000`
  or `100 000`, and the `t20` check to accept the module by its file or its import path. Its checks accept 23 tasks on each
  side. Every answer was read against the source as above, and the reading differs from the checks
  in one: the `lndl` answer to `t24` again says `lionagi/mcp/config.py` defines no `RUNS_DIR`, and
  its check accepted it. Read that way, `lndl` accepted 22 tasks at 3.59 requests per accepted task
  (79 requests) and ReAct 23 at 4.30 (99 requests). `lndl` used fewer requests per accepted task and
  accepted one task fewer, so r5 does not meet the rule: `lndl` is not recommended, and the default
  stays `react`. The other two rejected answers are wrong on reading too: the `lndl` answer to `t03`
  gives a default from another package in the read tree, and the ReAct leg on `t10` called a tool
  it does not have and then answered that it could not find the constants.
- r5's outcome is one task wide: any one of its accepted ReAct answers counted wrong, or either
  `lndl` answer read as wrong (`t03`, `t24`) counted right, would make `lndl` meet the rule. ReAct's
  answer to `t03` is one of those: it opens by saying the endpoint's request model has no default
  of its own and then answers 8192 for the CLI's API launch path, the value the task records, and it
  is read as right, like the r3 ReAct answer above. Counted
  wrong, r5 would be 22 tasks on each side; counting the `lndl` answer to `t03` or `t24` right
  would make it 23 on each side.
- Every answer is committed with the runs (`answers.jsonl`), beside each leg's spend line
  (`spend.jsonl`), and so is its reading, with the source line the reading used (`reading.jsonl`); the pilot's `summarize.py` writes each run's
  `summary.json` from that file and the run's results. The README lists the eleven answers
  where the reading and the checks differ and the answers to `t03` and `t05` whose count turns on
  the reading rule, and gives r5 task by task, each with the source line its reading used. After r5 the `t24` check was changed to require the two assignments themselves; run
  against the committed r5 answers, it rejects the `lndl` answer and accepts the ReAct one.
- The counts of runs that opened with an `<invoke>` tag and of requests that went to rounds with no
  block are read from each leg's conversation, which `li agent` keeps under the one home directory
  every run shares. The pilot commits what was read from them, the syntax each `lndl` round used
  (`rounds.jsonl`), and not the conversations.
- All 192 legs exited 0 and none exhausted its round budget, which D2 defines as spending it without
  an accepted `OUT{}`. The five r2 `lndl` legs that spent all 11 requests wrote an `OUT{}` in their
  last round (`rounds.jsonl`) and exited 0, so the loop accepted it (their answers gave no value, as
  above); an
  exhausted budget raises `LNDLError`, which `li agent` records as the run's failure and re-raises,
  so such a leg would have exited non-zero. No `lndl` leg in r3, r4 or r5 spent more than 5. The
  pilot therefore does not exercise D2's exhausted-turn exit.

## Consequences

- An operator can try the batched loop on one profile without changing any other leg.
- Every API-model leg now reports its spend, which is useful whatever the pilot finds.
- An `lndl` leg's answer is text the model wrote; a model that writes poor LNDL spends repair rounds,
  the round budget bounds that spend, and a turn that exhausts it ends `failed` where ReAct would
  still have asked for an answer. The pilot's acceptance column shows what that costs.
- The expected saving is small: at most one request per turn, the answer request ReAct makes after
  the model has stopped. If models seldom take ReAct's extra round, the pilot will show no saving and
  the default stays as it is.
- The 16-call limit is a starting value with no measurement behind it; the D4 Result reports it as
  unmeasured, and a pilot that measures it is the evidence for changing it.

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
