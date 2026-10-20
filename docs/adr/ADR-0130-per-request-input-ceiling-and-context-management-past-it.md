# ADR-0130: Per-request input ceiling and context management past it

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: messages-context
- **Date**: 2026-10-17
- **Relations**: takes the per-request input ceiling and the context management past it that
  ADR-0127 (Proposed) defers to "their own record"; any rewrite of history this record allows keeps
  ADR-0127's segment layout and re-places its marks (D6); depends on ADR-0007's request compiler as
  the one place a request is assembled, and on ADR-0033's signal bus for the decision event (D7);
  leaves ADR-0126's routing and the pricing table's capacities unchanged

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
100,000 prompt tokens (`lionagi/service/accounts/_pricing.py:125-146`, `PriceTier.max_prompt_tokens`
at `:54`), and the tier counts fresh, cache-read and cache-written input together. The same entry's
`ModelPrice.max_input_tokens` (`:64`) is the model's capacity, one million, and is used to reserve
spend; it is not the size a caller wants to stay under. No code before a send compares the request
against either number.

**P2 — the model's own context tools exist, and nothing calls them for it.** `ContextTool`
(`lionagi/tools/context/context.py:114`) gives a model `status` (`:245`, an estimate from
`TokenCalculator` over message content, `:176`), `evict` (`:290`), `evict_action_results` (`:306`),
`restore` (`:325`) and `compact` (`:353`). Eviction removes messages from the branch's active view
and keeps them in the full record, so `restore` brings them back. A model that never calls the tool
keeps growing its request until the provider refuses it or bills it at the next tier.

**P3 — a dependent spawned child inherits its emitter's whole conversation.** A reactively spawned
child with no branch of its own is built from its emitter's branch
(`lionagi/operations/flow.py:1613-1641`, `_assign_injected_branch`), as `base.clone(...)`; the
clone copies every message and maps the emitter's active view onto the copies
(`lionagi/session/branch.py:523`). Branch setup runs after the clone (`flow.py:1634`, `:1638-1639`). A child
cloned from a long emitter starts at the emitter's size, before it has done anything. Observed: a
child whose inherited history rendered to 3,477,964 characters was started on a CLI provider whose
turn input is limited to 1,048,576 characters, and died at turn start.

**P4 — what one "request" is differs by provider.** API providers send the message list each turn.
The Codex and fresh Gemini CLI paths flatten the non-system messages into one turn input
(`lionagi/providers/_cli_subprocess.py:1204-1227`), and Codex carries the system text as a separate
argument (`lionagi/providers/openai/codex.py:1799-1805`). A provider that resumes a session it holds
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
and the account router (`lionagi/providers/anthropic/messages.py:989`,
`lionagi/service/accounts/_router.py:123`); a provider the table does not price, such as a command-line
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
(`lionagi/providers/openai/codex.py:2066-2071`). Today no resuming adapter makes
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
   is measured like any other result). Today that tool previews a message in 120 characters or
   restores it whole (`lionagi/tools/context/context.py:185`, `:325`); landing step 4 adds the range
   read. A run whose model has no `context` tool (a profile with `context_management` off) is not
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
