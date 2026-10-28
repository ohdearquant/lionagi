# ADR-0130: Per-request input ceiling and context management past it

- **Status**: Accepted (2026-10-23) for D1 and D2 on `li agent` API-model legs and for D3 and D4
  as amended; D1 and D2 elsewhere, D5, D6 and D7 remain Proposed (see Amendment 1)
- **Kind**: Aspirational
- **Implementation-status**: partial: the ceiling on `li agent` API-model legs and as an `iModel`
  field, the calibrated count, the refusal before the send, the host fold and ranged read-back
  have landed, with the local result-body bound, per-leg fold/cache settings and startup manifest
  fields in Amendment 3; the default for requests built outside `li agent`, the per-call value,
  resumed-session and command-line measurement, the 80% notice, D5, D6 and D7 have not
- **Area**: messages-context
- **Date**: 2026-10-17
- **Relations**: takes the per-request input ceiling and the context management past it that
  ADR-0127 (Proposed) defers to "their own record"; any rewrite of history this record allows keeps
  ADR-0127's segment layout and re-places its marks (D6); depends on ADR-0007's request compiler as
  the one place a request is assembled, and on ADR-0033's signal bus for the decision event (D7);
  leaves ADR-0126's routing and the pricing table's capacities unchanged; Amendment 3
  coordinates per-leg cache settings with ADR-0127 D3 and D5 without changing provider capability
  or cache layout

**Instruction.** This record serves an instruction the project's maintainer gave on 2026-10-16: a
model whose price rises past a per-request input size is kept under that size; the bound is a flag;
and when work continues past it, the library manages the context the way automatic compaction
does, while models still manage their own context first. The decisions carry it: a ceiling that
applies by default where a model's price changes with prompt size, at that size, and that a caller
can lower or turn off (D1, D2); the model's
own context tools first, then a recoverable fold the library performs, then a refusal (D3, D4);
and the same rule for a spawned child that inherits its emitter's conversation (D5).

Purpose: a conversation that keeps working past a per-request size its caller cares about should
stay under that size without silently losing what it worked on, and a request that cannot fit should
be refused before it is sent, with the count, rather than sent and billed at a higher tier or
rejected by the provider.

## Context

**P1 — price tiers change at a prompt size, and nothing in the request path knows it.** The pricing
table prices some models by prompt length: `claude-haiku-5-5` carries a first tier bounded at
100,000 prompt tokens (`lionagi/service/accounts/_pricing.py:128-149`, `PriceTier.max_prompt_tokens`
at `:57`), and the tier counts fresh, cache-read and cache-written input together. The same entry's
`ModelPrice.max_input_tokens` (`:67`) is the model's capacity, one million, and is used to reserve
spend; it is not the size a caller wants to stay under. No code before a send compares the request
against either number.

**P2 — the model's own context tools exist, and nothing calls them for it.** `ContextTool`
(`lionagi/tools/context/context.py:136`) gives a model `status` (`:345-361`, an estimate from
`TokenCalculator` over message content, `:198-201`), `evict` (`:390`), `evict_action_results` (`:406`),
`restore` (`:425`) and `compact` (`:484`). Eviction removes messages from the branch's active view
and keeps them in the full record, so `restore` brings them back. A model that never calls the tool
keeps growing its request until the provider refuses it or bills it at the next tier.

**P3 — a dependent spawned child inherits its emitter's whole conversation.** A reactively spawned
child with no branch of its own is built from its emitter's branch
(`lionagi/operations/flow.py:1613-1641`, `_assign_injected_branch`), as `base.clone(...)`; the
clone copies every message and maps the emitter's active view onto the copies
(`lionagi/session/branch.py:642-653`). Branch setup runs after the clone (`flow.py:1634`, `:1638-1639`). A child
cloned from a long emitter starts at the emitter's size, before it has done anything. Observed: a
child whose inherited history rendered to 3,477,964 characters was started on a CLI provider whose
turn input is limited to 1,048,576 characters, and died at turn start.

**P4 — what one "request" is differs by provider.** API providers send the message list each turn.
The Codex and fresh Gemini CLI paths flatten the non-system messages into one turn input
(`lionagi/providers/_cli_subprocess.py:1536-1559`), and Codex carries the system text as a separate
argument (`lionagi/providers/openai/codex.py:1879-1885`). A provider that resumes a session it holds
sends only the new turn and keeps the history remotely, where nothing on this side can fold it.

## Decision

**D1 — the ceiling is a policy declared per profile or model, on by default where price depends on
prompt size, tightened per call.** A caller sets `max_input_tokens` on an agent profile, or on the
iModel it builds, to a positive integer, `"auto"` or `"none"`. Unset resolves as `"auto"` for a
request whose model the pricing table prices by prompt length (D2), and as no ceiling, with no change
to the request, for every other request. `"none"` is the opt-out: no ceiling and today's behaviour,
on any model. A per-call value may lower the resolved ceiling and never raise it. Zero, negatives and
booleans are refused at load. The default has a cost to public callers, stated here: a caller that
sets nothing and sends a tier-priced model past its first boundary is today sent and billed at the
next tier, and once landing step 2 ships it is refused before the send instead (and, from step 4, an
iterative run is folded); that caller restores today's behaviour with `"none"`, and the release notes
of step 2 say so. The field is a policy of this library and is never sent to a provider. It is
distinct from the pricing table's `ModelPrice.max_input_tokens`, which keeps its meaning.

**D2 — `"auto"` resolves to the model's first price-tier boundary.** For a request whose model the
pricing table prices by prompt length, `"auto"` is the first tier's `max_prompt_tokens` (100,000 for
`claude-haiku-5-5`). The table prices the requests it is consulted for, the Anthropic Messages path
and the account router (`lionagi/providers/anthropic/messages.py:988`,
`lionagi/service/accounts/_router.py:124`); a provider the table does not price, such as a command-line
agent billed by subscription, has no tier here, so unset leaves it unchanged. For a model with one flat
tier, or none in the table, `"auto"` resolves to no ceiling, and the resolution says so in the
decision event (D7) rather than inventing a number. A
transport limit a provider adapter states in characters (a CLI's turn-input limit) is a separate,
always-on refusal that belongs to that adapter; it is not this ceiling and this record does not move
it.

**D3 — measurement before every send, on the whole request.** The measured size is the request as it
will be sent: system text, tool definitions, the instruction, the history and anything injected, with
cache reads and writes counted like fresh input. After each response the size is re-anchored to the
provider's own reported input count. Before the next send, the estimate is that count plus the bytes
appended since, at 0.66 tokens per byte; any single appended block larger than one tenth of the
ceiling is counted at one token per byte instead, so one oversized tool result cannot hide under the
ratio. The first request of a conversation, which has no reported count, is counted with the local
tokenizer. Every send counts, retries included. A provider that holds the history remotely and sends
only the new turn is measured the same way, from the input count it reported for the session's last
request plus the bytes appended since; it gets the 80% notice, and at the ceiling it goes to D4 step
1 or 3, refused before the send with the count, because a fold cannot reach history held remotely.
The reported count is the provider's input count for one request; a provider that reports none, or
only a total over several requests, and a resumed session with no reported count on record, cannot
be measured, and under a ceiling are refused with that reason, never sent unmeasured. Whether a
provider holds the history remotely, and whether a count it reports is for one request, is that
provider adapter's statement, not an inference from a `resume` argument. The Codex adapter
renders branch history for every turn and does not resume a Codex thread
(`lionagi/providers/openai/codex.py:2637-2642`). Today no resuming adapter makes
that statement: the Claude Code adapter yields only its terminal `result` usage, a total
whenever the session ran more than one request. The run attaches that metadata to an
`AssistantResponse` only when the turn produced text (`lionagi/operations/run/run.py:544-563`),
although it separately accumulates usage and passes it to `API_POST_CALL` even without a text
response (`lionagi/operations/run/run.py:715-720,825-833`). The Gemini CLI path reports one
summary per invocation. Landing step 2 makes the Claude Code adapter report the input count of the
session's last request from that request's own event, and the run keep it when the turn produced no
text; a Gemini CLI summary for more than one turn, or one with no turn count, counts as a total.

**D4 — past the line: the model first, then a recoverable fold, then a refusal.** Each turn at or past
80% of the ceiling carries one line telling the model its measured size, the ceiling and that its
`context` tool can reduce it. At or past 100% before a send:

1. A bounded run (one request, or a fixed sequence the caller planned) is refused before the send
   with the measured size, the ceiling and the largest parts. It is the caller's brief that is too
   large, and the library does not rewrite it.
2. An iterative run folds the oldest completed tool results, oldest first, until the estimate is at
   or under 60% of the ceiling. A fold replaces a result's body in the request with a stub that keeps
   the tool name, the call id, the original size and how to read it back; the original stays in the
   branch's full record and the model reads it back, in ranges, with its `context` tool (whose output
   is measured like any other result). The context tool previews a message in 120 characters and
   restores the original to the active view (`lionagi/tools/context/context.py:203-208`, `:425-482`);
   Amendment 2 below specifies ranged reading. A run whose model has no `context` tool (a profile with `context_management` off) is not
   folded, because it could not read the original back; it is refused as in 3. A fold never leaves a
   tool call without its result in the request, never touches the system text, the instruction, or
   the assistant's own messages, and never changes another branch.
3. If nothing is left to fold and the request is still at or past the ceiling, it is refused before
   the send with the count, as in 1.

This record adds no summarizing compaction and the library never summarizes on its own account. The
model can still ask for one through its `context` tool's `compact`, as today, as its own bounded
request. A cap that drops the oldest history unread is not offered.

**D5 — a dependent spawned child keeps today's inheritance, under the same ceiling.** A dependent
child with no branch of its own still starts from a clone of its emitter's active view (P3); this
record does not change what independent, role-targeted or planned children start from. The child's
first request is measured after its branch setup, under the child's own resolved ceiling, and D4
applies to it as to any other request: an iterative child folds its own copy, a bounded child is
refused before it starts, and the emitter's branch is never folded on the child's account. A refused
child fails as its own node with the count, and its siblings run.

**D6 — a fold re-places the cache marks.** A fold rewrites history, so every ADR-0127 mark at or after
the first rewritten message is dropped and placed again on the rewritten request; the stable segment
before it keeps its bytes and its one-hour entry. The cost is one new write of the rewritten suffix:
at the pinned revision of `claude-haiku-5-5` a 60,000-token five-minute suffix costs $0.0075 to
write against $0.0006 to read, $0.0069 extra once, after which later requests read it.

**D7 — every ceiling decision is a recorded, versioned event.** A new `ContextDecision` signal records,
per request: the run, branch and request ids, the resolved ceiling and where it came from (profile,
iModel, call, the default for an unset policy, `"auto"` with the tier it read, an explicit `"none"`,
or none because nothing prices the model by prompt length), the measurement method, the size before and
after, the action (`notice`, `fold`, `refuse`), the folded call ids, and for a refusal that no
request was sent. It is an additive signal type under `SIGNAL_SCHEMA_VERSION`
(`lionagi/session/signal.py:41`); its readers are told before it ships.

## Landing order

1. D1, D2 and D7: resolving the ceiling, unset included, and recording it in the event, with no change
   to any request yet.
2. D3 and D4 step 1 and 3: measurement and refusal before the send, with the Claude Code adapter's
   per-request input count and the run keeping usage from a turn without text (D3). From this step
   the D1 default refuses a tier-priced request past its boundary for a caller that set nothing, and
   the release notes name `"none"`; until step 4 an iterative run past the ceiling is refused, not
   folded.
3. The 80% notice (D4).
4. The fold (D4 step 2) with the `context` tool's range read, and the re-placed marks (D6), after
   ADR-0127's placement lands.
5. D5: the child's first-request check.

## Tests that prove it

- No tier-priced model, or an explicit `"none"`: request bytes and behaviour identical to today on
  every provider path.
- Unset on a tier-priced model resolves to its first boundary and the event says it came from the
  default; unset on a flat-priced model, an unpriced model, and a provider the table does not price
  resolves to none.
- A ceiling of 0, a negative, a boolean, and a per-call value above the profile's (a per-call
  `"none"` under a profile ceiling included) are each refused; `"auto"` on a flat-priced model
  resolves to none and says so.
- Boundary: estimate exactly at, one under, and one over the ceiling; a single tool result larger than
  a tenth of the ceiling made of one-token-per-byte text is counted at one per byte.
- A request whose cache reads alone exceed the ceiling is refused or folded, not passed.
- A bounded run over the ceiling sends nothing and records `refuse` with the count.
- A fold: the call/result pairing holds in the rendered request; the original is readable in ranges;
  the system text, instruction and assistant messages are byte-identical; a sibling branch is
  unchanged; the event lists the folded ids.
- Nothing left to fold: refusal with the count, no send. An iterative run whose model has no
  `context` tool, past the ceiling: refusal, no fold.
- A resumed provider session under a ceiling is measured from its reported count plus the appended
  bytes, gets the notice at 80%, and at the ceiling is refused before the send with the count; a
  provider that reports no per-request input count, and a resumed session with none on record, are
  refused under a ceiling with that reason. A resumed Claude Code session whose last turn ran tools
  is measured from that last request's own input count, not the session total, including a turn
  that produced no text; a Gemini CLI summary for several turns, or without a turn count, is refused
  as a total; the Codex path, handed a `resume` argument, is measured from its whole rendered history.
- A dependent child cloned from an emitter over the ceiling: an iterative child folds its own copy
  and the emitter is unchanged; a bounded child fails as its node and its siblings complete.
- After a fold, the stable segment's bytes and mark are unchanged and the next request reads the
  rewritten suffix from cache.

## Run-contract impact

`ContextDecision` is a new signal type, additive under the current schema version, persisted where
signals are persisted. Nothing existing changes shape. Readers of the signal stream are told before
the first release that emits it. The D1 default changes what a public caller's request does without
any setting of theirs: the release that ships landing step 2 states it, with `"none"` as the way back.

## Out of scope

- A default ceiling for a model the pricing table does not price by prompt length, or for a provider
  the table does not price.
- Handing a dependent child only its spawn request and its emitter's last response instead of the
  inherited conversation. It would change what every dependent child sees, and needs its own record
  with evidence that children do not need the history.
- A new summarization algorithm, a model capability catalog, and any change to prices or to the
  transport limits adapters enforce.

## Amendment 1 (2026-10-23): a calibrated count and a host fold, first on `li agent` API-model legs

On 2026-10-23 the maintainer asked that the library fold and compact an API agent's context itself,
before the input cap refuses the run. The first landing serves that request, and the decisions above
as far as they reach an `li agent` leg on an API model. It changes two decisions: D3's measure, and
D4's rule that the library never summarizes on its own account. The text above stays as it was
decided; where it conflicts with this amendment, the amendment holds.

**D1 and D2, as landed.** `iModel(max_input_tokens=N)` takes a positive integer, or `None` for no
ceiling, and `None` stays the default for an `iModel` built directly. Every `APICalling` the model
builds checks its estimate (D3 below) before the send and raises `InputTokenCapError`, whose `details`
carry `estimate`, `cap`, `counted` and `scale`. The model keeps the first refusal and raises it for
every later request until a caller clears `input_cap_refusal`, so a loop that catches the error cannot
go on to send a smaller request as if the run had not been refused; `ReAct` and `parse` re-raise it
instead of returning an earlier round's text or an unparsed fallback. An agent profile that declares
`api_tools` may set `max_input_tokens` to a positive integer, `auto` or `none`, and unset reads as
`auto`: the first price tier's `max_prompt_tokens` in the pricing table's current revision for an
Anthropic model the table prices by prompt length (100,000 for `claude-haiku-5-5`), and no ceiling for
any other model. The leg
prints the ceiling and its source when it starts. The branch's launch record keeps the policy; a
resume under a profile takes the value that profile names, and a record written before the key existed
reads as unset. This is D1's stated cost, on these legs only: a leg on a tier-priced model that sets
nothing is now held under the first boundary, and `max_input_tokens: none` restores the old behaviour.
Not landed: `auto` and `none` on `iModel` itself, the unset-as-`auto` default for a request built
outside `li agent`, and the per-call value.

**D3, amended: the request itself is counted, then scaled by what the provider reported.** The
estimate is the `o200k_base` token count of the payload's `system`, `instructions`, `messages`,
`input`, `prompt`, `tools` and `response_format` as JSON text (`estimate_payload_input_tokens`), times the model's
`input_token_scale`. The scale starts at 1.0. After each completed response whose counted input was at
least 4,000 tokens, it rises to the provider's reported input for that request divided by the count,
and it never falls. The reported input is `prompt_tokens`, or `input_tokens` plus the cache-read and
cache-write counts; a streamed response's usage is merged across its events, which report cumulative
counts. The cap and the fold share the scale. This replaces the estimate of the reported count plus
0.66 tokens per appended byte: counting the compiled payload measures the request as it will be sent,
the first request included, and the scale carries the provider's own counting into every later one.
On one `claude-haiku-5-5` agent run the provider counted 93,525 and 95,483 input tokens for two
requests the local count put at 68,308 and 69,836, a ratio of 1.37, so a cap on the local count alone
would have let requests near 137,000 tokens through a 100,000 ceiling. Not landed from D3: the
resumed-session and remote-history provisions, the Claude Code adapter's per-request count and the
Gemini CLI rule. Command-line providers are unchanged.

**D4, amended: a refusal before the send, then a host fold in two stages, the second a summary.**
`ContextFold` (`lionagi/agent/context_fold.py`), installed on the leg's branch at `API_PRE_CALL`,
estimates the next request before it is compiled: the chat compiler is run over the active view, with
the newest instruction standing in for the one about to be sent, and the result is counted and scaled
as the cap counts. At or past 80% of the ceiling it folds the active view toward 60% with the `context`
tool's own `compact`, so the folded messages stay in the branch's full record and `restore` brings them
back:

1. `tool_io`: the tool calls and results older than the newest six active messages, then older than
   the newest two, are replaced by one note that lists each call with its function, its arguments (a
   path-like argument whole up to 1,000 characters, any other clipped to 160) and its output size;
   a call whose result stays in view is not listed, since the model reads a call only through its
   result.
2. `all`, only if that is not enough: every message older than the same tails, except the first
   message of the view and the task instruction, is replaced by a note that holds a summary written by
   one model call carrying only the folded span, beside the call lists of stage 1 and of earlier notes.
   If that call fails, the list stands alone.

This fold supports `chat` and `operate` requests using the chat path, which compiles after
`API_PRE_CALL`. The streaming `run` path compiles before that hook and is outside this fold's
supported request scope.

The task instruction and the first message of the view are never folded, and no other branch is
changed. If the cap still refuses a request, the loop calibrates the fold by the refused estimate,
folds at least once more and continues, at most twice per turn (the task, or one queued message). A
refusal the fold cannot
bring under the ceiling fails the turn with the refusal, and the loop never returns an earlier round's
text as its answer. A request built outside these legs under an `iModel` ceiling is refused as in D4
step 1 and is not folded. Stage 2 reverses D4's "This record adds no summarizing compaction and the
library never summarizes on its own account": the request above asked for compaction by the library,
and a list of calls keeps what the agent read but not what it concluded. Not landed from D4: the
one-line notice at 80% (each tool result the coding toolkit returns already carries its context
status, which on these legs measures against the ceiling, counting message content without the scale),
and the per-result stub with its ranged read-back (`restore` returns a message whole).

**D5 and D6 not landed; D7 deferred.** No `ContextDecision` signal is emitted. The leg's run manifest
records every fold under `context_folds`: its time, the reason (`threshold` or `refused`), the stage,
the tail kept, the number of messages folded, up to 50 of the calls listed and their total, whether the summary
came from the model or the host, the estimate before and after, the ceiling and the scale. Each fold
also prints one line. Nothing in the signal schema changes.

**Library changes the landing needed.**

- A branch's curated view no longer freezes: a message added after the view exists joins it. Before
  this, once a model evicted or compacted, no later message reached the model again. When the
  message manager replaces the system message or clears the messages, the view keeps only messages
  still in the record, headed by the current system message; before this a replaced system message
  stayed in the view after it left the record, and every later compaction refused. A branch restored
  from its stored form gets its view back as a progression rather than a plain mapping, holding only
  messages its stored record holds.
- `compact` places its summary note once, where the first collapsed message was.
- `chat` emits `API_PRE_CALL` before it compiles the request from the active view, so a handler that
  curates the view shapes the request it precedes (ADR-0047 records the scope); `run` still emits it
  after compiling, before the stream starts.

**Tests that prove it.** `tests/service/imodel/test_input_cap.py` pins the refusal and the first
refusal kept, the count of the system prompt and of special-token text, each usage shape the reported
input is read from, the stream merge, and the scale (its floor, that it only rises, that copy and
serialization carry it). `tests/cli/test_agent_api_context_fold.py` pins the profile policy and its
resume, `auto` against the pricing table, a run that folds past the line and completes, a refused
request folded and continued, a refusal the fold cannot clear failing the run, the summary request
carrying the span alone, and the fold line moving with the provider's count.
`tests/tools/test_context_tool.py` and `tests/hooks/test_api_call_hookpoints.py` pin the view, the
note's single placement and the hook order.

## Amendment 2 (2026-10-23): the fold reaches its target when the newest results are large

On one `claude-haiku-5-5` agent run under a 100,000-token ceiling, Amendment 1's fold ran all four
of its passes before one request and ended at an estimated 94,829 tokens against a 60,000 target. The
round before that request had run five tool calls at once. Four of the five results, one of them a
25,494-character file read, were folded by the two `tool_io` passes before the model had read them;
the fifth, a 100,045-character directory listing from a shell command, was one of the two newest
messages, which no pass folds, and it kept the request far over the target. The same passes show a
third defect: the first `all` pass raised the estimate from 95,837 to 96,505 and still counted as
landed. Later in the run the model evicted results twice with its own context tool, so the run
finishing under the ceiling does not show what the fold did alone. A round's results are not bounded
by the kept tail: a ReAct round can return several, and an LNDL round up to sixteen, each as a call and
a result message. This amendment changes D4 as amended by Amendment 1; where the two conflict, this
amendment holds.

**D4, amended again: a clip stage between the two folds, and a pass lands only when it shrinks the
request.**

1. *Results the model has not read stay in view.* A message is delivered once a main request that
   carries it completes. The fold records delivery itself: at `API_PRE_CALL`, after any folding, it
   notes the ids of the view the request is compiled from, and at the matching `API_POST_CALL` with a
   completed status it marks those ids delivered. A summary request marks nothing: the fold's own
   summary call does not pass through the branch's hooks, and the context tool's automatic compaction
   summary, which does, carries a marker the fold skips. A failed or refused request marks nothing.
   The delivered ids are kept in branch metadata beside the curated view, persist with the branch, and
   `Branch.clone` maps them to the clone's ids as it maps the curated view. A branch that has no such
   record counts every message created before its newest assistant response that is not a
   compaction note as delivered.
   `tool_io` and `all` fold only delivered messages: the fold's compaction takes a set of messages to
   keep, which the model's tool schema does not expose, and a kept message inside the folded span
   stays where it is. An undelivered result, and the call it answers, stays in view wherever it sits;
   the kept tails (six, then two) still bound what the two folds reach among delivered messages.
2. *Order.* `tool_io` (tail six, then two), then `clip`, then `all`. Before `all`, the fold compiles
   and counts the floor view: the task instruction, the head of the view, every undelivered message
   and the tail of two, with the clips already made. If the floor view is over the ceiling, no fold
   can make the request sendable, `all` does not run and no summary request is sent. If it is over 60%
   of the ceiling but under the ceiling, `all` runs once, with the tail of two. Otherwise `all` runs
   with the tail of six, then two. The fold stops at the first pass that reaches the target.
   Each ordered tail step and the clip stage runs at most once per fold.
3. *`clip` cuts by the deficit.* It shortens tool result bodies in the view, the kept tail and
   undelivered results included. Its budget is the estimate minus 60% of the ceiling, both measured
   on the compiled request at the current scale. A text output is one body. In a mapping output each string value is a body of its own and is cut in place, so the mapping keeps
   every key and renders as it did; any other value the JSON encoder accepts is replaced by a labeled
   preview string of its JSON text with keys sorted, cut the same way, and a value it refuses (bytes, a set,
   a date) is not a body and stays whole; the `success` and `error` fields are never cut. A value the request
   cannot render at all (an object with no YAML form, a cycle) never reaches the clip, because compiling the
   view refuses it first. Bodies are taken largest first by
   current kept character length, ties to the oldest. Each body is cut just enough to cover the remaining deficit,
   using that body's own measured tokens per character, keeps a head and a tail of the original in a
   three-to-one ratio, and is never cut below 1,024 characters; a shorter body stays whole. After each
   cut the candidate view is compiled and counted, and the next body is taken only if the count is
   still over the target. Candidate views are counted from message lists outside the record; only the
   copies in the view that is kept enter the record. When every eligible body is at the floor and the
   target is still missed, that view is kept if it lowers the estimate, and the fold records the target
   as missed. The ratio and the floor are starting values, not measurements; a later measurement may
   change them without changing this rule.
4. *A clipped result is a new message, and the original stays.* The copy is an `ActionResponse` with
   the original's function and arguments, placed in the view at the original's position. Its output
   holds the head, a line naming how many characters were left out and the exact `read_result` call
   that returns them, and the tail, so the model sees that the result is incomplete. Its metadata
   carries `clipped_from`, the original's id (always the original, never another copy), at the top
   level beside `clone_from`, not under `extra`. The copy carries no `action_request_id`, and it enters
   the record through a message-manager path that accepts a built response, fires the same
   message-added callbacks as every other message (persistence and the view), and leaves every
   `ActionRequest` unchanged; the existing response constructor, which requires a request, links it
   and rewrites the output, is not used for copies. A copy is always cut from the original, never from
   an earlier copy. `Branch.clone` maps `clipped_from` to the clone's ids. A `clipped_from` key inside
   a tool's own output means nothing to the host. A copy of a file read does not count as a read for
   the read-before-edit check: the file counts as read again once its original is restored or it is
   read again. On a later fold a copy stands for its original, whose request it answers through
   `clipped_from`: an undelivered copy keeps that request in view as point 1 keeps the call of any
   undelivered result, a delivered copy that stays in view leaves the request unlisted as any result
   in view does, and a request folded with its copy is listed once, with the original's index and
   length. When a later `all` pass folds a copy, its summary reads the copy, and the note's entry
   for that call records the original's index and length, so `read_result` still reaches it.
5. *Recovery reads the stored original.* `context(action="read_result", index=N, offset=O, limit=L)`
   addresses the original by its index in the full record, the index `get_messages(scope="all")` lists
   and `restore` takes; ids are not shown to the model and change on clone. It returns characters `O` to
   `O+L` of the text the copy was cut from, with the total length and the next offset; `L` is at most
   16,000 characters, a starting value, and a larger request returns that many. It never runs a tool
   again and never changes the view. The coding toolkit's `context` function forwards the new
   arguments. `restore` places each message it brings back by a chronology key: the original's index
   for a clipped copy, the index of the first message a fold or compaction note replaced (which the note
   records in its metadata) for a note, and its own index for any other message. Today restore keys on
   each message's own index, so a note or copy, appended late to the record but standing early in the
   view, misplaces every later restore. `restore` of a clipped original replaces its copy in place; a
   range holding both brings back the original and skips the copy, and a copy is never restored on its
   own; if the copy has left the view, the original goes to its own place. The fold registers on its
   branch a check that a view fits under the ceiling; `restore` of a clipped original asks it, and a
   restore it refuses leaves the copy, returns `success: false` and names `read_result`. With no fold
   installed there is no ceiling to check and `restore` admits as today. A restored original, or a
   range just read, is not clipped again before the next main request completes.
6. *A pass lands only when it lowers the estimate.* The fold snapshots the view's id list before each
   pass. After a pass that wrote a summary, it measures the estimate before and after at the scale in
   force once the summary request has returned, since that request can raise the scale. A pass that
   does not strictly lower the estimate is rolled back: the fold writes the snapshot back as the
   curated view, also when the branch had no curated view before (compaction always creates one, and
   the rejected note stays in the full record outside the view), and drops the carried tool-call entries
   it recorded for that note. A rolled-back pass is not landed, for the refusal path as well, which
   resumes only after a fold landed; a refused request with nothing the fold may take stays refused.
7. *Counts stay counts of work done.* The run's tool-call count excludes clipped copies; a
   `read_result` call is a call and counts.
8. *Records.* Each `context_folds` entry gains `outcome` (`landed` or `rolled_back`), `target_reached`,
   and `summary_usage`, the provider-reported input and output tokens of the pass's summary request,
   with its cache-read and cache-write tokens when the provider reports them, null when none was sent;
   the fold's summary call is not logged on the branch, so this field is where its cost is recorded. A `clip` entry has `kept_tail` and `summary_source` null and `messages_folded`
   0, and adds `clipped`, up to 50 objects `{"index", "chars", "kept"}` (the original's index in the
   full record, its body length and the length kept), and `clipped_total`. The existing fields keep
   their meaning.

Unchanged: the 80% trigger and the 60% target, the task instruction and the first message of the view
never folded, the ceiling still refusing a request the fold could not bring under it, and at most two
resumes per turn.

**Not decided here.** A character bound on the built-in `reader` and `search` results, beside their
line and row bounds, with a continuation that reaches the rest. It changes those tools' output
contract and would spare most clips on the library's own tools, but it cannot bound any other tool, so
`clip` must reach the target without it. A combined timeline of the host's passes and the model's own
`evict`, `evict_action_results`, `compact` and `restore` belongs with the run manifest's record shape,
which its readers consume, and is versioned with that change. Both sources are already stored: the
host's passes in `context_folds`, the model's own actions as its `context` calls in the branch record.
An acceptance run of this amendment reads both and attributes each change to the view.

**Tests that prove it.**

- A 100,045-character result in the newest round, the model making no context calls: the estimate
  after the fold is at most 60% of the ceiling, the original is unchanged in the full record, the
  compiled request carries the copy with its marker and its `read_result` call, and no summary request
  is sent.
- Sixteen results from one round with a tail of two: all sixteen, whole or clipped, are in the next
  compiled request; neither `tool_io` nor `all` takes one; with each larger than the floor and together
  over the line, the target is reached and every call is still represented.
- Delivery: a result carried by a completed main request is delivered; one carried only by the context
  tool's automatic summary, by the fold's own summary or by a refused request is not; a cloned branch
  keeps the marks under the clone's ids; a branch without the record counts what predates its newest
  assistant response.
- A small deficit cuts a little, not to the floor; a mapping output keeps every key, its `success` and
  `error` whole and the copy renders like the original; a list value becomes a labeled preview.
- Candidate counting adds nothing to the record; the kept copy enters it once, the persistence callback
  fires, metadata `clipped_from` is top level, and the original request's `action_response_id` is
  unchanged.
- A system prompt and task already over 60%: `target_reached` is false, nothing in the task changes,
  and the ceiling still refuses a request over it. A floor view over the ceiling sends no summary
  request; a floor view between 60% and the ceiling sends one.
- A summary that raises the estimate, and a summary request that raises the scale: the view's ids are
  exactly what they were, the pass is not landed, the note stays outside the view and its carried
  calls are gone; tested with and without a curated view before.
- `restore` of a clipped original, twice, and over a range holding both the original and its copy: one
  form of the result, at the original's place. With a note and a copy standing earlier in the view, a
  restored message lands at its chronological place.
- A restore the ceiling cannot admit is refused with `read_result` named; through the coding toolkit,
  `read_result` returns the original text from the omitted middle, unchanged after the file it came
  from is edited, and a limit over 16,000 returns 16,000.
- An edit to a file whose read is in view only as a copy is refused by the read-before-edit check until
  the original is restored or the file is read again.
- Clipping twice under a tighter budget, a clone, then a stored-form round trip: one copy, cut from the
  original, `clipped_from` naming the clone's original, and `read_result` still works.
- A `tool_io` fold after a clip: an undelivered copy keeps its request in view; a delivered copy in
  the kept tail leaves its request folded and unlisted; a request folded with its copy is listed once
  with the original's index and length.
- A `clipped_from` key in a tool's own output is not taken as provenance.
- The run's tool-call count is the same with and without clipping.

**Run-contract impact.** Additive. A clipped copy is a stored `ActionResponse` with `clipped_from` in
its metadata and no `action_request_id`; a reader that counts responses as executions skips those
copies, and request-to-response pairing is unchanged. Fold and compaction notes gain the index of the
first message they replaced. Branch metadata gains the delivered ids. `context` gains the
`read_result` action, so its tool schema changes. `context_folds` entries gain the fields in 8.

## Amendment 3 (2026-10-24): per-leg settings for result bodies, the fold line and prompt caching

On 2026-10-23 the maintainer asked that the project's own agent legs be measured on the
API route and tuned from what the measurement shows. On 26 tasks comparing design
records with their code, each repeated twice on
`claude-haiku-5-5` under a 100,000-token ceiling, a development build compared two
profiles of the same API leg. One used the existing settings. The other enabled
prompt-cache placement, kept up to 20,000 source characters from tool outputs and
folded at 90% of the ceiling toward 60%. Across the 52 paired executions, the second
cost $2.69 against $5.14, or 0.52 as much; median leg cost was $0.046 against $0.099.
Its answers agreed with a model-authored reference in 36 executions against 27
(two-sided sign test over 21 discordant pairs, p = 0.078). Mean cited-line recall
was 0.210 against 0.206, and input-ceiling failures were zero against one.

These are observations of a combined setting on one task family and one model,
with repeated tasks; they establish neither equivalence in quality nor the effect
of any key alone. That build had no `clip` stage. Its output cut kept a prefix and
added an omission note outside the character allowance; its mapping handling also
differed from the body rule below. The measurements support offering settings,
without establishing the cost or quality of their interaction with Amendment 2.

This amendment adds profile controls to D2 and D4 as amended, and connects one
profile control to ADR-0127's existing endpoint option. ADR-0127 continues to own
cache placement and accounting; its unimplemented provisions are not made a
prerequisite or declared complete here. Where this amendment conflicts with
Amendments 1 and 2, it holds only for the setting it enables. With no result bound,
the existing result rendering and clip rules remain in force.

**Four profile keys beside `max_input_tokens`.** A profile declaring `api_tools`
may set these top-level keys. They remain API-launch settings read from the
profile's `extra` mapping:

- `tool_result_max_chars`: an integer at least 1,024. Absent means no bound.
- `context_fold_at` and `context_fold_target`: finite numbers in (0, 1]. An absent
  side takes 0.8 or 0.6 respectively, and the effective pair must satisfy
  `0 < context_fold_target < context_fold_at <= 1`.
- `prompt_caching`: a boolean. `true` enables ADR-0127's endpoint option and
  `false` disables it. Absent means no override of the endpoint configuration,
  whose default remains `false`; in particular, absence does not erase an option
  already stored on an older branch's endpoint.

An explicitly present null, a boolean used as a number, a fractional result
bound, a numeric string, a non-finite fraction, an invalid pair, and a non-boolean
cache value are refused before a run is allocated. Validation uses parsed YAML
types: quoted `"true"` and `"yes"` are strings and are refused; this amendment
does not change the YAML loader's interpretation of unquoted scalars. Presence of
any of these keys without `api_tools`, including `prompt_caching: false`, is
refused. A declaration on a CLI-provider leg is refused rather than ignored.

The branch's `api_launch` record stores the four requested settings, using null
for absent settings. The same validation applies to non-null stored values;
missing fields in an older record read as unset. A resume without a profile uses
that record. A resume with a profile uses that profile's four settings, including
their unset values, and persists the resulting policy for the next resume.
Consequently an omitted bound removes a previous bound, omitted fold fractions
take their defaults, and disabling an already-enabled endpoint cache requires
explicit `false`. Effective settings are applied to the branch's models and fold
before the first request, including on resume. The result bound is a local model
setting, defaults to unset, survives model copying and serialization, and is
never passed as a provider request parameter. Settings on another branch are not
changed by this installation.

**A bound on each eligible body, including its marker.** On a leg with
`tool_result_max_chars: N`, the chat compiler projects the tool results carried
by both `react` and `lndl` requests, including historical results and newly added
ones. It changes neither the stored original nor the active view merely to apply
the bound. It creates no result message and records no tool execution.

A body is Amendment 2 point 3's body: a text output, or an individual top-level
mapping value that is text or has JSON text. Mapping keys and their order are
kept; nested structured values are one JSON body, not recursively selected
fields. JSON text uses the same sorted-key encoder and acceptance rules as
`read_result`. The `success` and `error` mapping fields remain whole, as do values
the JSON encoder refuses but the request can otherwise render. Values the
request cannot render still fail to compile. A value that needs no shortening
retains its existing type and rendering.

N counts Unicode characters in the projected body value, including its omission
marker and any `[JSON preview]` label. It is not a byte or token count, a bound on
the complete mapping, or a bound on serialization punctuation, function names,
arguments or protected fields. The per-request input ceiling still counts the
complete payload and can refuse it.

A shortened body keeps a head and a tail from the stored original, with the head
holding `floor(3k/4)` of the k source characters retained. Between them is one
complete marker stating the omitted source-character count, the omitted interval
and how to recover it. The interval uses the original's `read_result` text and
its global character offsets, including mapping labels and separators. The
marker addresses the original's full-record index, gives a first call with
`limit <= min(N, 16000)`, and says to continue at `next_offset`, stopping at the
interval's end. It does not claim that one call returns an arbitrarily long
omitted interval.

The renderer chooses a deterministic retained length that fits the entire body
within N, accounting for the marker's actual length. It must retain both head
and tail and the complete recovery instructions. If those cannot fit, compilation
fails explicitly; it neither drops the instructions nor exceeds N. The profile
minimum of 1,024 is a minimum allowance for this representation, not a promise
of 1,024 retained source characters.

For an unchanged original, full-record index, clip allowance and leg policy, the
projected body is identical on every request. It does not depend on a changing
request deficit, delivery state or recent recovery reads. This avoids an
additional history rewrite from applying the bound; it does not promise a cache
hit or make every whole request a prefix of the next. A fold, restore or policy
change can still rewrite history under ADR-0127's rules.

**The bound and `clip` share one projection from the original.** Amendment 2's
stage order and delivery rules remain: `tool_io`, `clip`, then `all`. For a leg
with a result bound, these rules replace the size arithmetic and rendering in its
clip stage:

1. For each eligible body, keep separate its original source length, its retained
   source-character count and its complete projected-body length. Let `k_N` be the greatest retained source count the bound's representation permits for that original, measured in
   the form that count implies: the original length is the unshortened body, and it is `k_N` when that body fits; any
   smaller count is the shortened body (head, the complete recovery marker, tail and any preview label), and `k_N` is
   then the greatest such count whose shortened body fits. The greatest fitting count is established by measuring
   each candidate's complete body, not assumed from a monotonic length, since marker offsets change width. A copy's
   allowance comes from its original-derived clip metadata. The current effective retained count is the original
   length when the allowance admits the whole original and the unshortened body fits; otherwise it is the greatest
   shortened-form count at or below both the allowance and `k_N`, which may be below a copy's recorded allowance so
   that the complete marker fits. When no shortened count fits, the body is refused and the refusal recorded; it is
   never delivered over N. With no
   copy, the allowance is the original length. Every projection is built from
   the original with this effective count, never by slicing a preview or marker.
2. Choose bodies largest first by effective retained source count, ties to the
   oldest original. The deficit is the compiled and scaled estimate minus the
   target. A tokens-per-character proposal uses the effective retained count,
   not the unbounded original length or a stale copy allowance; it is only a
   proposal. Each candidate is compiled and counted with its complete marker,
   labels, other fields and the same model policy. Source removed by the bound
   cannot be credited again as savings from `clip`.
3. On these legs the retained-source floor is `min(1024, k_N)`, with `k_N` as point 1 defines it. A body already at
   that floor has no further clip capacity. A proposed cut must lower the current
   effective allowance; cuts that only remove source already hidden by the bound
   are not attempted. Recent recovery protections can raise the floor further;
   if no legal reduction remains, skip that body. Only a candidate that strictly
   lowers the measured request is kept. No fixed token saving or number of
   candidate compilations is promised by character arithmetic.
4. A new copy contains the original-derived head, one marker and tail, with each
   retained count in `context_clip_kept` matching the source actually present
   in that copy. The compiler recognizes its provenance and applies the smaller
   allowance directly to the original. It never places a second marker around
   the copy's marker. A pre-existing copy under a newly smaller bound follows
   the same rule. `clipped_from` continues to name the original, and the existing
   copy insertion, persistence, chronology and call-pairing rules hold.
5. Candidate estimation retains access to the authoritative original and its
   full-record index even when a temporary view omits that original. Temporary
   view positions are never used as recovery indices. Counting adds nothing to
   the durable record. A retained copy enters it through Amendment 2's existing
   message-added path, and a rejected candidate leaves it unchanged.

The fold and the input ceiling use the same result projection and endpoint
layout. The fold still estimates a future request with the newest stored
instruction as a stand-in, and may differ from the actual request's instruction
or injected context. The final payload check and bounded refusal recovery remain
necessary; sharing a compiler does not make that forecast exact.

**Recovery under the bound.** `read_result` continues to read the stored original
without rerunning a tool. With a bound, its `text` field returns at most
`min(requested_limit, N, 16000)` source characters. Its reply adds `limit`, the
applied limit, beside the existing total recovery-text length and next offset;
the returned slice can be shorter at the end of the source. That slice remains
unchanged in the next main request, including through the coding toolkit:
host status annotations must not be appended to that slice and push it through
another cut. Paging metadata and status annotations are separate from `text`.
Without a bound, the existing response shape and 16,000-character limit remain.

`restore` restores the original message to its chronological place and replaces
its copy as before; the next compiled request still projects that original under
N. A restore admission check uses that projected candidate view. Restoring an
already-active original cannot reveal its omitted middle. The original and
recovery pages retain Amendment 2's protection against additional deficit-driven
clipping until the next completed main request; the standing per-body bound is
not suspended. A recovery page reaches that request whole. Reading a range does
not cause historical previews of its original to change to include the range.
Several pages can together expose more than N characters of an original; N is
per body, not an aggregate allowance over all pages of one original.

**Other consumers of results.** Host-generated summaries of a span project its
tool-result bodies with the same policy before assembling their source text;
directly serializing stored results for such a summary must not bypass the
bound. A summary's own total source budget remains separate. The bound does not
scan or shorten arbitrary task instructions, assistant prose or user-supplied
context merely because they quote tool output.

The fold notes' `output_chars` continues to describe the stored original output
under its existing size convention. It is neither a count of characters shown
nor the total of `read_result`'s labeled recovery text. On bounded legs the note
labels it as stored-output size. The `clipped` entries keep their meanings:
`chars` is original body length and `kept` is retained original source characters,
excluding the marker and JSON label. These are not payload token counts. The
fold's estimates remain counts of compiled payloads.

Context status and compaction's `tokens_freed_est` keep their existing advisory
message-content estimates; neither is promoted to the compiled, scaled request
estimate. Coding-tool status annotations remain advisory and are included where
they actually render in the final payload count. Delivery of a result means
delivery of its current projection, not proof that all stored source was read.
Tool-call counts still exclude copies and include actual `read_result` calls.
The bound changes neither stored originals nor the tool set a leg may execute.

**The fold line and target.** `context_fold_at` replaces the host fold's 80%
trigger. `context_fold_target` replaces its 60% target in every stage's stopping
condition, the deficit used by `clip`, the floor-view comparison that selects one
or two `all` passes, and `target_reached`. The floor view is compiled after clips:
over the ceiling it sends no summary; over the target but at or under the ceiling
it permits one `all` pass with tail two; otherwise it permits tails six then two.
Each stage and tail step remains bounded as in Amendment 2.

These settings do not change the 100% input ceiling, the two refusal resumptions,
the kept tails, source-character floors, summary budgets, calibration, advisory
context-tool thresholds or time-budget guidance. Historical measurements and
examples containing 60% or 80% are not new policy expressions. The deferred
one-line notice and remote-history provisions remain deferred. The startup line
reports the effective fold pair and token values. Installing a second fold on the
same live branch returns the existing one only when cap, line and target agree;
a disagreement is refused. With no resolved ceiling, fractions are validated and
stored but no fold is installed; the result bound and cache option still apply.

**Prompt caching.** The profile key configures `prompt_caching` on the selected
endpoint configuration, the option ADR-0127 D5 specifies and D3 capability-gates.
The compiler and endpoint remain the owners of layout and marks. An endpoint
without the declaration keeps its request layout even when asked to enable the
option. This amendment neither enables caching by default nor changes
`cache_control`'s separate local-response-memo meaning. It does not implement
ADR-0127's still-open conversation segment, lookback placement or segment-change
accounting.

**Run record.** An initialized API leg adds three top-level manifest fields:

- `prompt_caching`: the endpoint's `places_prompt_cache` answer after settings
  are applied. It means placement is enabled for this endpoint, not that a
  request has been sent, a mark was observed, or a cache hit occurred.
- `tool_result_max_chars`: the effective body bound or null.
- `context_fold`: `{"fold_at": <fraction>, "target": <fraction>}` from the
  installed fold, or null when there is no resolved ceiling.

Write these during startup after resolution and successful fold installation,
before the first main request, and retain them through subsequent manifest
writes, including failure and cancellation. If setup has not completed, these
fields are absent rather than claiming an installed policy. They describe this
invocation, including a resumed invocation, and remain fixed during it. Existing
`api_usage` records actual calls and usage; `context_folds` remains the plural
history of fold actions. Neither is renamed or replaced, and no `usage` alias is
introduced. The requested cache value remains in `api_launch` separately from
the manifest's effective endpoint answer.

**Not decided here.** Defaults that opt any caller into these settings; a native
provider tool loop; new tool-output contracts; an exact compiled-token status
tool; or completion of the remaining parts of ADR-0127 and ADR-0130. The body
bound is not an aggregate context budget. Quality and cost with the clip stage
need measurement before any claim that the observed savings transfer to it.

**Tests that prove it.**

- Validate every key before run allocation, including absent sides of the pair,
  explicit nulls, booleans as numbers, strings, non-finite values, exact boundaries,
  keys without `api_tools` and CLI-provider declarations. Verify unset cache
  policy preserves a pre-existing endpoint option and explicit `false` disables it.
- Store and reload the launch policy and model state; resume with and without a
  profile; remove a former bound and fold settings; disable caching explicitly;
  load an older record with no keys; refuse malformed stored settings. Copy a model
  and confirm the bound survives without changing another branch's configuration.
- Capture payloads before this change. For unchanged effective settings with no
  new keys, both loops produce identical bytes, including after a clip, restore,
  recovery read and summary. Global tool descriptions and default result shapes
  must not change those requests. Additive manifest fields are permitted.
- For a 100,045-character result and mapping values containing strings, long
  lists and nested mappings, verify the whole projected body fits N including
  marker and label. Cover N = 1,024, 4,000 and 20,000; an uncut value at the
  boundary; Unicode; protected fields; JSON-refused but otherwise renderable
  values; and unrenderable values. Keys and uncut values retain their form.
- With a small deficit and a source much larger than N, `clip` starts from the
  effective retained count and measures actual savings. Cover an older copy whose
  source allowance exceeds N, a copy whose source plus marker exceeds N, a body
  already at its effective floor, several bodies and sixteen undelivered results.
  Require one complete marker derived from the original, exact recovery offsets,
  no savings attributed twice, and strict measured decrease for every kept cut.
  If legal cuts cannot reach the target, record that outcome and retain the final
  ceiling check.
- Compile temporary candidate views whose originals are absent and compare their
  projections and recovery indices with the authoritative branch. Ensure counting
  creates no stored messages; accepted copies use the existing persistence path;
  rejected candidates do not enter the record. Clone and reload a clipped branch
  and recover the same source text through the mapped original.
- Under N = 4,000, page an omitted interval longer than 16,000 through the coding
  toolkit. Every returned slice reaches the next main request unchanged, the
  continuation covers the interval exactly, and historical previews remain
  identical. Restore the original, including a range containing its copy, and
  verify the next request remains bounded and admission counts that projection.
- Force both host summary paths over bounded tool results; neither carries a
  whole hidden body. Verify stored-output note sizes and retained-source clip
  counts separately from projected lengths and payload tokens. Actual tool-call
  counts are unchanged except for recovery calls that really execute.
- With line 0.9 and target 0.5, check the trigger boundary, every stopping
  condition, clip deficit, floor-view decisions and `target_reached`. A repeated
  installation with another pair fails; without a ceiling no fold is installed.
  Count the same compiled candidate with the same model policy and separately
  exercise a forecast differing from the actual next instruction.
- Enable caching on a declaring endpoint and on a non-declaring endpoint, using
  filtered and unfiltered request configurations. Verify the existing placement
  rules, unchanged non-declaring layout, and absence of local policy keys from
  provider payloads. Startup fields must reflect the resolved policy even when
  zero requests complete, and survive later `api_usage` and terminal writes.

**Run-contract impact.** Additive launch settings, model state and the three
top-level manifest fields; older missing settings remain readable. On bounded
legs, recovery replies additionally report the applied limit and fold notes
distinguish stored-output size. Originals, call/result links, copy provenance,
usage counters and existing fold-history field meanings remain intact. Enabling
the bound deliberately limits direct presentation of an original even after
restore; complete source remains available by ranged reads.
