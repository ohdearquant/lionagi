---
adr: ADR-0009
status: draft
liveness: partial (the router, both shapes of the session CLI and the subscription CLI operate and report the count; the mixed-response refusal and the session CLI's failed-call envelope are pinned by tests; the gate over an acting session is pinned by tests with a scripted CLI, its two hook contracts measured once against the installed CLI)
date: "2026-09-25"
area: backend
kind: new
depends_on:
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0004-the-language|ADR-0004]]"
  - "[[ADR-0006-the-notification|ADR-0006]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
tags:
  - adr
  - runtime
  - backend
---

# ADR-0009: Backends and the context figure

## Context

The runtime has to run on whatever program runs the model: an API reached through a router, a CLI on
a subscription with its acting features switched off, a CLI that keeps its own conversation and may
run its own tools inside a turn. The loop knows none of this. This record fixes what a backend is,
what it hands back beside the text, what it keeps for whoever reads the spend, and the one number
the runtime derives from it: the context figure [[ADR-0006-the-notification|ADR-0006]] prints and
[[ADR-0003-the-bounds|ADR-0003]] bounds on.

The backend is the caller's argument to `run` ([[ADR-0001-the-actor|ADR-0001]]), called once per
turn ([[ADR-0002-the-run|ADR-0002]]); a native tool call in its answer is read back as text
([[ADR-0004-the-language|ADR-0004]]); the fold it must follow is [[ADR-0007-the-record|ADR-0007]].
Which model a name resolves to, through which route and on whose key, and whether the model that
answered is the one named, are decided below this boundary: here a backend is built with a model
name and answers with text. Source: `Backend`, `Reply` and the anchor in `lionagi/actor.py`;
`hub/backend/openrouter.py`, `hub/backend/claude_code.py`, `hub/backend/codex.py`.

## Definitions

- **backend**: a function from the view, as messages, to the model's text; the caller's argument to
  `run`. It acts on nothing unless it is given tools of its own (C6).
- **call**: one invocation of the backend: one per turn, awaited when it is awaitable.
- **attempt**: one request a call sends; a call retries on a transient failure and answers from its
  last attempt.
- **prompt count**: the provider's own count of the prompt an attempt sent: fresh input, cache read
  and cache write together.
- **`Reply`**: the text a backend hands back with the prompt count of the attempt that answered, or
  none when the provider did not say, and why the backend refused the turn when it did.
- **envelope**: what a backend keeps per call beside the text: usage, cost, attempts, timing, the
  provider's finish reason; a failed call keeps one too.
- **session**: a CLI conversation a backend keeps across calls, one per run, each call that goes on
  sending only what the view gained.
- **point**: what one session call left, which a later call can go on from: the entries it holds,
  their size, the count its call reported.
- **floor**: the least a run's prompt count has stood above the view estimate sent with it: the
  backend's own fixed context, which no fold takes off.

## Assumptions

| #  | statement                                                                                                                         | source                                                                                                                                                                                                    | if false                                                                                              |
| -- | --------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| A1 | A provider's prompt count is the size of the context the model saw on that attempt.                                               | the router's `usage.prompt_tokens`; the session CLI's input, cache read and cache write per turn; on 4,240 bench calls the count read 1.20 times the view estimate plus 1,695 ([[ADR-0006-the-notification#Assumptions\|ADR-0006/A3]]) | the anchor is exact about the wrong prompt; the figure returns to the estimate                        |
| A2 | A CLI with its acting features switched off is a chat completion.                                                                 | with the flags in `hub/backend/codex.py` the CLI cannot run a command, search or spawn, measured on its 0.153 release; the session CLI under `--tools ""` and one turn                                    | C1's CLI backends are agents, and their acts pass no gate ([[ADR-0005-command-handling\|ADR-0005]])   |
| A3 | What a count stands above the view at its least is fixed context; what it later stands above that is history the record never saw. | the session CLI reads 34k cached input per call with its settings loaded against 3.9k without; on a served run one call read 14.8M tokens over its tool turns while the estimate never moved (2026-09-24) | the fold fires every turn past a budget the fixed context exceeds, or never sees the tool turns       |

## Claims

### C1: A backend is a function from the view to the model's text, and acts on nothing _(enforced: mechanical)_ ^c1

- **Subject**: every backend handed to `run` without its own tools (C6).
- **Violated when**: a backend acts on the world, forwards a tool schema, executes a native tool
  call, or hands back anything but the model's text.

The loop renders the view, calls the backend once per turn, awaits the result when it is awaitable,
and reads the text; a `Reply` is text. Three shapes are built. The router sends the view as a real
message array, the head as system and later notifications as user, so the provider's prefix cache
holds.

The subscription CLI takes the view as one transcript with its roles marked, every acting feature
off by flag and a chat framing in place of its agent instructions. The session CLI keeps one
conversation per run (C4). A native tool call in a router answer with empty content is rebuilt as
the tag text ([[ADR-0004-the-language#^c4|ADR-0004/C4]]); beside content the `Reply` carries a
refusal and nothing runs.

### C2: A backend hands back the prompt count of the attempt that answered _(enforced: mechanical)_ ^c2

- **Subject**: every backend response, and the loop's reading of it.
- **Violated when**: the count handed back is a sum over attempts, a boolean or a non-positive value
  anchors the figure, or the record keeps anything but plain text.

The count rides the `Reply`; the loop reads it and the record keeps the text as a plain string. The
router hands back the answering attempt's `usage.prompt_tokens`; its envelope sums every attempt for
the bill, and a sum of two prompts is the size of neither.

The session CLI hands back the last model turn's input, cache read and cache write when the CLI
prints per-turn usage, else the call's sum over its turn count, an average that reads low. The
subscription CLI hands back the input count of the answering attempt's `turn.completed` event, cache
read included (S3). A boolean is an integer in Python, so a value that is not a positive integer is
ignored.

### C3: The fold measures the estimate plus the reported context above the floor _(enforced: mechanical)_ ^c3

- **Subject**: the fold event and the context figure of every notification.
- **Violated when**: the fold fires on the estimate alone while a count stands above the view beyond
  the floor, or the floor is folded, scaled or measured.

Per run the runtime keeps the last count with the estimate sent beside it, the summed growths, and
the floor. What the count stands above the estimate beyond the floor is history the record never
saw: a session CLI's tool turns.

The fold event ([[ADR-0007-the-record#^c2|ADR-0007/C2]]) compares the estimate plus that excess
against `view_budget`. Under `fold_inputs` earlier inputs and replies fold too, the latest input
stays. Over the budget with nothing older to fold, the event still rides the notification and a
session starts over (C4). The figure is the count plus the estimated growth since, or on a shrink
the count less what left at the run's rate ([[ADR-0006-the-notification#^c4|ADR-0006/C4]]); the
floor stays in the figure, out of the measure.

### C4: A session sends what the view gained, and starts over when that pays or on the fold _(enforced: mechanical)_ ^c4

- **Subject**: every session backend call and the notice before it.
- **Violated when**: a restart is taken or skipped against its `payback` price, an acting session
  ignores a context directive, the fold event goes on, notice and call disagree within a cache hour,
  or two runs share a session.

One session per run id (S14). A call sends what the session lacks.

Without tools, each resumed call forks its point. On a changed view it prices going on against
starting over from an intact point or the whole view. It starts over when the prompt saved over
`payback` turns covers what it writes again at the served model's prices (S16), and always on the
fold (A3). An acting session goes on in place and starts over whole on a context directive or the
fold.

The actor asks `plan` first, so the figure and a `session:` line say what the call does, unless a
cache hour ends between them (S13).

### C5: Every call lands an envelope, failed ones too, and only a transient failure is retried _(enforced: mechanical)_ ^c5

- **Subject**: every backend call.
- **Violated when**: a billed attempt's usage is lost, a failed call lands no envelope, a 401 or 403
  is retried, or a subscription's cost is written as a number.

The router retries with backoff on a 429, a 5xx, a transport failure and an errored choice; a 200
carrying the provider's error is retried only by that error's code. An empty finished answer is
sampled again, then handed over blank. Usage sums every attempt; the cost is the known subtotal,
flagged when an attempt is missing from it; a call that fails after a billed attempt lands a failed
envelope carrying the bill.

The subscription CLI retries a transport failure or an empty answer, keeps every attempt's usage,
and writes its cost as unknown, never zero. A refused CLI turn rides its cost on the error.
Cancelling a call kills and reaps the CLI child before the cancellation goes on.

### C6: A session CLI's own tool calls pass the runtime's privilege check and before hooks _(enforced: mechanical)_ ^c6

- **Subject**: every tool call of a session CLI built with an actor.
- **Violated when**: a tool runs after a refusal or after a hook that could not ask, or a call lands
  nowhere on the record.

A session's `tools` run inside each turn, `max_turns` per turn. With an actor, the pre-tool hook
hands each call to the actor's privilege check ([[ADR-0001-the-actor#^c4|ADR-0001/C4]]), `requires`
naming a tool's privileges, then its before hooks ([[ADR-0005-command-handling#^c4|ADR-0005/C4]]). A
refusal, or a hook that cannot ask, stops the tool; a replacement is its input.

Each call lands on the run's record as a RESULT under the CLI's tool-use id, admitted or not
executed; a repeated id is refused, landing suffixed by its entry's seq. What the tool did stays in
the CLI's session, past subset, after hooks and bus (S11). `lion chat` and `lion agent` build none
([[ADR-0016-the-lion-command#^d3|ADR-0016/D3]]). Without an actor it is trusted as configured. The
bench's host-side baseline is no backend ([[ADR-0012-the-bench|ADR-0012]] S6).

## Decisions

### D1: The router backend in `hub/backend/openrouter.py` ^d1

Serves C1, C2 and C5. `OpenRouter(model, ...)` posts the view with usage included and `max_tokens`
32768: at 8192, 3 of 125 turns that wrote 40 to 52 actions were cut mid-tag. Temperature, provider
routing, reasoning control and stop sequences are the caller's options; the thinking budget and the
stop on the notification frame are ruled on in the bench record ([[ADR-0012-the-bench|ADR-0012]]).
The key comes from the environment or the keychain, never logged. `tool_calls_text` rebuilds a
native call when the content is empty; `calls` keeps the envelopes.

- **Landing evidence**: `tests/test_openrouter.py`: the count of the attempt that answered, the
  retried errored choice, the billed failure kept, the native call handed back as text, the mixed
  response refused
  (`test_a_native_tool_call_beside_text_refuses_the_turn_whole_and_the_model_is_told`).

### D2: The two CLI shapes in `hub/backend/claude_code.py` ^d2

Serves C1, C2, C4, C5 and C6. `ClaudeCode` runs the CLI once per turn: tools off, no settings,
strict MCP config. `ClaudeCodeSession` forks a point per call unless it has tools, one session per
run; `_step` prices a changed view, `plan` answers it for `_notify`; `base` seeds a continued chat's
first figure. A session may be built with the CLI's tools, a directory, one MCP. Both hand back the
last turn's count. A failed call lands its envelope and leaves no point. Cancellation reaps the
child; the gate settles (C6).

- **Landing evidence**: `tests/test_claude_code.py`: forked with only the new entries, a small hide
  waiting, a large one forking, the whole view, the fold, the cache's hour, the seed, the notice,
  one session per run, failed calls' bills and cache reads, the gate (S11).

### D3: The subscription CLI backend in `hub/backend/codex.py` ^d3

Serves C1 and C5. `Codex` runs the CLI once per turn in a scratch directory with every acting
feature disabled: spawning removed by `agents.max_depth=0` (the feature flag alone does not), web
search off, a chat framing replacing the base instructions, the environment context dropped. A
private home holding a symlinked login is used when given. The view goes in as one transcript; usage
comes from the `turn.completed` event; cost is unknown. It hands back that event's input count for
the answering attempt (S3).

- **Landing evidence**: `tests/test_codex.py`: the transcript in and the usage shaped, the transient
  retry with attempts recorded, the empty answer retried with both attempts counted, the failed
  envelope, cancellation joining the child, the count handed back and anchoring the next figure
  (`test_the_input_count_of_turn_completed_is_handed_back_and_the_next_figure_stands_on_it`).

### D4: The anchor at the call site and the floor in `_notify` ^d4

Serves C2 and C3. After each call `Actor.run` reads `prompt_tokens` off the reply, keeps the pair
(count, estimate sent) as the run's anchor, adds to the summed growths when the view grew, lowers
the floor to the least excess seen, and hands the record `str(text)`. `_notify` measures the folded
view, adds the excess above the floor, folds on that measure, then writes the figure from the
anchor.

- **Landing evidence**: `tests/test_actor.py`: the figure as the last count plus the view added
  since, the fold at the measured rate, the fold deciding on the reported context and folding old
  inputs, a plain string on the record, a boolean not a count.

### D5: The call's end joins its asks within a bound; none answers after it ^d5

Serves C6. When a session call ends, every hook still asking is cancelled and joined; a caller's
cancel arriving during the join is raised once it is done. The join is bounded by `_JOIN_S`: an ask
still running at the bound has its hook connection dropped, so the hook reads EOF and refuses, and
is abandoned with a `RuntimeWarning` naming it; the call returns. Unbounded, an ask that swallowed
its cancel held the join open and a caller cancelling each turn (a SIGINT under `asyncio.run`) was
swallowed too: a spin.

- **Landing evidence**: `tests/test_claude_code.py`:
  `test_an_ask_that_will_not_end_is_abandoned_at_the_bound_and_a_cancelling_caller_still_returns`:
  the call returns at the bound with the warning, under a caller cancelling every turn too, and the
  real hook exits 2 while the abandoned ask's later allow reaches nothing.

## Alternatives

| approach                                                        | rejected because                                                                                                                       |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| The provider's tool schema as the channel                       | a wire format per provider and an await per call; the language carries the requests instead (ADR-0004)                               |
| One backend                                                     | the bench needs the same model through two harnesses, and a served run works on a subscription CLI                                    |
| Counting the prompt in the runtime with a tokenizer             | a dependency per provider; the provider's count is exact for the prompt it saw, and free (ADR-0006)                                   |
| Anchoring on the envelope's summed usage                        | a retried call bills two prompts; the sum is the size of neither                                                                       |
| Resending the whole transcript to the CLI each turn             | the prefix never caches: 160k tokens in for one five-round exchange, measured 2026-09-23                                               |
| Scaling a shrunk count by the view's ratio                      | it scales the fixed context too, which never shrinks: up to 96% off at a fold in simulation, 5% by the run's rate                      |
| Folding by editing the CLI's history                            | the conversation is the CLI's; a fork from a point before the change applies the fold and reads that point from the cache              |
| Starting over on every context directive                        | a hide of about 730 estimated tokens wrote 25.7k again; one chat's four restarts added about 29% of its whole cost over going on, re-derived from its bills |
| Keeping every change until a forced restart                     | the session reads what it holds every turn: for that chat about three times the premium of forking where it pays                       |
| Going on in place, forking only to start over                   | an in-place call leaves no earlier point to go back to; the checkpoint needs a fork per call                                          |
| Cutting a session at an entry with `--resume-session-at`        | a hidden flag of the CLI, unmeasured; a fork per call costs a session copy on disk instead (S12)                                       |
| Resuming the old session after `lion chat -c`                   | it needs the same system prompt and each entry's digest in the log; the first call starts over, its figure seeded (S8)                |
| Reading the served model off the response and refusing a mismatch | which model a name binds to is decided below the boundary; the envelope keeps the response's model and compares nothing (S5)         |

## Consequences

- **S1**: A subscription CLI has no cost figure: the spend is unknown, never zero, and a reader that
  sums it says so. The agent's spend row counts such calls in `unknown`, beside the router's calls
  whose `cost_complete` is false, and the readers [[ADR-0016-the-lion-command|ADR-0016]] S2 names
  print the sum as partial.
- **S2**: The same model through the loop and through its own CLI is comparable in one box
  ([[ADR-0012-the-bench|ADR-0012]], the bench).
- **S3**: The subscription CLI hands back the input count of its `turn.completed` event, cache read
  included, and the next figure stands on it. It handed back no count before, so a run on it kept
  the view estimate as its figure and folded on the estimate alone. The CLI's cache-write field is
  not added to the count.
- **S4**: A session call whose CLI ran several tool turns reports the last turn's context only when
  the CLI prints per-turn usage; otherwise the average reads low, and the figure with it, until the
  next call.
- **S5**: The boundary: which model a name resolves to, through which route, on whose key, and
  whether the model that answered is the one named, are not decided here. The router's envelope
  keeps the response's `model`; the subscription CLI's keeps the name requested; nothing compares
  them.
- **S7**: The session CLI's default model emits thinking on the language path, about 330 output
  tokens for a 30-token turn; a dollar figure is read from a run's envelopes, never from a price
  table.
- **S8**: After a session starts over from a point, the figure is that point's count plus the
  characters sent over four. A start from the whole view stands on the session's last start the same
  way, and a continued chat's first call on its log's last billed count and `prompt_chars`. With no
  count it is the view's estimate. The next count corrects either
  ([[ADR-0006-the-notification|ADR-0006]] S3). On one continued chat 15,317 was told against 31,730
  sent; the seed replayed gives 30,698.
- **S9**: A backend that raises ends the run by its exception ([[ADR-0002-the-run#^c4|ADR-0002/C4]])
  with the notification already on the record ([[ADR-0006-the-notification#^d1|ADR-0006/D1]]); a
  command still pending then is reaped before the exception leaves `run`
  ([[ADR-0002-the-run|ADR-0002]] S4).
- **S10**: A failed session CLI call lands its envelope before the error goes on: the one the CLI
  printed, as a delta over the point it resumed
  (`test_a_session_call_the_cli_fails_lands_its_envelope_with_its_own_share`), else one of unknown
  cost. It leaves no point; the next call goes on from the one it resumed. A running total missing,
  or below that point's, leaves the share unknown, never negative. It is not retried, and a spend
  row counts it ([[ADR-0016-the-lion-command|ADR-0016]] S2). A fork's total being its point's plus
  its own was measured once on the installed CLI.
- **S11**: The gate over an acting session holds (C6), pinned with a scripted CLI that runs the
  pre-tool hook: in `tests/test_claude_code.py`,
  `test_an_acting_sessions_tool_calls_ask_the_gate_and_land_on_the_record`,
  `test_the_gate_admits_nothing_when_it_cannot_decide`,
  `test_a_call_that_ends_while_the_gate_waits_lands_the_tool_call_not_executed_before_it_returns`,
  `test_a_hook_that_connects_as_the_call_ends_runs_no_gate_after_the_call_returned`,
  `test_a_request_still_being_sent_when_the_call_ends_lands_not_executed_once_it_arrives`,
  `test_a_cancel_as_a_hook_is_accepted_at_the_calls_end_still_returns`,
  `test_two_requests_with_one_tool_use_id_at_the_gate_together_admit_it_once` and
  `test_a_request_the_hooks_left_that_cannot_be_sent_is_a_refusal_that_lands`; `lion agent` refuses
  such a session, by `tests/test_agent.py`
  `test_build_refuses_a_claude_code_session_handed_in_with_tools_of_its_own`. The record keeps the
  call and its verdict, never what the tool returned. Both hook contracts were measured once on the
  installed CLI: exit 2 stops the tool and the model reads the reason; an allow with `updatedInput`
  runs the rewritten call. No test runs the live CLI.
- **S12**: A fork per call leaves the CLI one copy of the session per call under its projects
  directory, about 5 bytes per token held: 3 to 5 MB for an 18-call chat, up to 50 MB for 100 calls
  at 100k tokens. Nothing here removes them; that layout is the CLI's own.
- **S13**: A point's cache hour runs from the last call resuming that point (S15), a failed one too
  when it reports a cache read: a request refreshes the cache entry it hits, not the shorter
  prefixes inside it (assumed, not measured). If a descendant's read keeps them warm, a fork from an
  earlier point is priced too high and the session goes on where a restart would pay. At the hour's
  edge the call can decide otherwise than the plan its notice told.
- **S14**: With no run id named, another first entry or another system prompt is another run with
  its own session. A view that only shrank sends its notification again, so no call goes out empty.
  Starting over also drops the tool turns an acting session's CLI held, which this backend does not
  estimate: so its restart is unpriced, and its notice names no figure for what goes.
- **S15**: A call's read of the cache is stamped at its reply less the rest of its turn after its
  first request, both timed per call (`duration_ms`, `time_to_request_ms`), so the CLI's startup
  ages no point. Its exit (0.34 to 0.38 s on 2.1.287 with no MCP server) can keep a point warm that
  long past its hour. A call of several requests is stamped at its first. Times missing, not
  numbers, or not fitting the call leave the reply, so unknown timing never reads a held point as
  expired.
- **S16**: A restart is priced at the cache prices of the model the CLI reports serving, else of the
  model asked for; a served name that is no model id falls through to the one asked for. An alias
  reads as the model the CLI runs for it, its `ANTHROPIC_DEFAULT_<FAMILY>_MODEL` when set. An entry
  also names an id continuing it after a hyphen, as a dated id does. A model id the table does not
  name, or no model at all, prices at 20 reads per character written.
- **S17**: A gated call's RESULT (C6) reads admitted: the gate's verdict, written before the CLI
  acts on it, never that the tool ran. A log written before reads started there, and the log's
  readers keep either as written
  (`test_an_acting_sessions_tool_calls_ask_the_gate_and_land_on_the_record`).
- **S18**: The hook answers `allow` only for a tool the CLI's `--allowedTools` names exactly; any
  other admitted call goes back with its input and no decision, for the CLI's own rules
  (`test_the_hook_allows_only_a_tool_the_clis_allowed_list_names`). Whether an `allow` would skip
  the CLI's own check of that list is not measured; the rule holds either way.
