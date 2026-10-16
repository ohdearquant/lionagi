# ADR-0127: Prompt-cache placement for prefix-caching providers

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: messages-context
- **Date**: 2026-10-16
- **Relations**: amends ADR-0007 D2 steps 4 and 5 and D4 for providers that cache by prompt prefix,
  and supersedes, for those providers only, its rejected alternative "Emit a native provider `system`
  message"; carries the cache placement ADR-0126 R3 assigns to this record; read with ADR-0008
  (pre-turn context providers), whose execution and attribution D4 keeps, and ADR-0125 step 2,
  whose request compiler inherits these rules

Purpose: repeated model calls inside one run should reuse the provider's prompt cache, so a run on a
per-token API model pays full price only for the tokens each call adds.

## Context

Several providers cache a request by its prefix: a later request that starts with the same bytes
reads that part from cache at a fraction of the input price. Anthropic's Messages API, read on
2026-10-16, works this way. A request places at most four cache marks; a hit looks back at most 20
content positions from a mark; a write costs 1.25 times the input price for a five-minute entry and
2 times for a one-hour entry, a read 0.1 times, and a hit renews the entry's lifetime; one-hour marks
must come before five-minute marks; below a per-model minimum prompt length nothing is cached.

An agent run is a loop of model calls: `ReAct`, or `operate(actions=True)` repeated, sends the
system text, the tool definitions, the history so far and one new turn on every call. Caching that
loop needs each request to be a prefix of the next. Measured at commit 54176709 with a scripted
endpoint (four `operate(actions=True, tools=True)` rounds; the test that pins this record, D7, is
that probe):

**P1: no request in a tool loop is a prefix of the next.** Each pair of consecutive requests first
differs at the previous round's instruction. While an instruction is current it renders its
`Tools`, response schema and response format sections (`protocols/messages/instruction.py:210`);
once it is history it is re-rendered without them (`operations/chat/_prepare.py:228`). Rounds 0 to
1 differ at message 0, 1 to 2 at message 2, 2 to 3 at message 4. The current instruction is about
3,170 characters, almost all of it those sections, sent uncached on every call.

**P2: the system text is folded into the first user message,** as ADR-0007 D2 step 4 specifies, so
it cannot be cached with its own lifetime apart from the conversation, and it moves whenever the
first message changes.

**P3: a context block injected on the first turn only changes message 0 on the second.** A context
provider on the first-turn cadence is gathered into the fold for request 0 and absent from requests
1 and 2; message 0 shrinks from 3,240 to 112 characters between them, so request 1 shares no prefix
with request 0 at all.

**P4: the Anthropic adapter places one five-minute mark, and the flag that asks for it never
reaches it.** `providers/anthropic/messages.py:209-221` marks only the last message's last block,
with no lifetime choice, and lifts a native `system` field only when the first message has role
`system`, which the fold never produces. The `cache_control` flag never reaches that code: the
Anthropic request model has no such field, so the request-option filter drops it
(`service/connections/endpoint.py:149-153`) before `messages.py:209` looks for it. `iModel` then pops
the same flag from the request (`service/imodel.py:278`) and, in `Endpoint.call`, it switches on a
local response memo (`service/connections/endpoint.py:255-263`), a different feature. Building the
Anthropic payload for the probe's last request with `cache_control=True` gave no `system` field
and no cache mark.

**P5: the run record cannot show what caching saved.** `RunEnd` carries `cached_tokens` and
`cache_write_tokens` (`session/signal.py:66-67`) without the lifetime of the write, and ADR-0126's
ledger needs read and write counts per lifetime to price a run.

| Concern | Decision |
|---------|----------|
| What may change between calls | D1: three lifetime classes, and an append-only rule for the turn class. |
| Tool definitions and response format | D2: tools render once in the stable segment; an instruction renders the same bytes in every request that contains it. |
| System text | D3: a native system block, chosen by a declared provider capability. |
| Context blocks | D4: context gathered for a run stays, unchanged, for the run. |
| Marks | D5: the provider adapter places them: one-hour at the end of the stable segment, five-minute on the moving tail. |
| Accounting | D6: each call records input, cache read, cache write per lifetime and output, side by side. |
| Acceptance | D7: the prefix test. |

**Out of scope.** A per-request input ceiling and what happens past it (a separate record or an
amendment; D1's classes are what any folding must respect). Providers that cache without marks.
CLI providers, whose prompt the CLI assembles. Price tables and budgets (ADR-0126).

## Decision

These decisions apply when the selected provider declares that it caches by prompt prefix (D3
names the declaration). For every other provider the compiled request is unchanged.

### D1: three lifetime classes

Every part of a request belongs to one class:

- **stable**: the system text, the tool definitions and any guidance fixed for the agent. It is the
  same for every call of every run of one configuration.
- **run**: what is fixed when a run starts and kept until it ends, today the context blocks a
  context provider returns for the run (D4).
- **turn**: the conversation. A request's turn segment is the previous request's turn segment
  followed by what the last call added.

A request is the stable segment, then the run segment, then the turn segment. Inside one run, a
request is a byte-for-byte prefix of the next unless the stable or run segment changed, and a
change to either is a new segment: it is allowed, it is not hidden, and the run record counts it
(D6).

### D2: tools render once; an instruction renders the same everywhere

Amends ADR-0007 D2 step 5 and D4. Tool definitions leave the instruction and render once in the
stable segment: in the provider's native tools field where it has one (Anthropic `tools`), built
from the operation's explicit tool parameters as ADR-0007 D4 already allows, sorted by tool name;
otherwise as a section of the system block (D3). An instruction renders the same bytes whether it is
current or history: its response schema and response format sections, when it had them, stay in its
historical rendering. A response format that changes between turns (a final answer in `ReAct`)
therefore changes only the new turn.

Why this way: rendering the tool sections in every historical instruction would also make each
request a prefix of the next, but it repeats the whole tool set once per turn, so input grows with
the number of turns times the tool set. Dropping them only from history is what P1 measured.

### D3: system as its own block, by declared capability

Amends ADR-0007 D2 step 4 for these providers. A provider adapter declares whether it accepts a
native system block and caches by prefix. When it does, the compiler emits the system text, the
stable guidance and, where D2 puts them there, the tool sections as the system block, and the first
user message carries only its own content. The compiler chooses from the declaration; the adapter
never re-folds or re-reads history (ADR-0007 D4 holds). Providers without the declaration keep
today's fold and role sequence.

ADR-0007 rejected a native system message because it changes the provider-visible role sequence on
surfaces that treat it differently. The declaration confines the change to providers that state
they accept it, and the characterization in D7 pins the sequence for the rest.

### D4: context gathered for a run stays for the run

For these providers, context blocks returned for a
run are placed in the run segment, after the stable segment, in their gather order and with their
source attribution, and are sent unchanged on every request of the run. They are not re-gathered
per call and not dropped after the first. A provider that returns a block only on the first call of a run (one that checks whether the
branch has a response yet) keeps its block in the run segment for the later calls; a block a provider
returns on a later call is added to the turn segment as new content and does not rewrite the run
segment. ADR-0008's pre-turn execution, ordering and attribution are unchanged.

The model therefore keeps seeing first-turn context on later turns where today it sees it once. That
is a change in what the model reads, so it lands behind its own characterization (D7) and the
injection budget is unchanged.

### D5: marks are placed by the provider adapter

The compiler hands the adapter the three segment boundaries; the adapter turns them into its
provider's marks. For Anthropic:

- one mark with a one-hour lifetime at the end of the stable segment;
- one five-minute mark at the end of the run segment when it is non-empty;
- one five-minute mark on the last block of the turn segment;
- when a turn adds more than the provider's lookback, one five-minute mark on the previous request's
  last block, so the next read still finds the previous write.

At most four marks; one-hour marks precede five-minute marks; a segment below the provider's minimum
cacheable length is not marked. The lifetimes are settings of the model configuration, with these as
defaults.

Prompt caching is its own option on the model configuration, off unless set, so no existing caller's
bill changes. The existing `cache_control` flag keeps its current meaning, the local response memo
in `Endpoint.call`; it no longer stands for provider caching, and the request-option filter stops
dropping the marks the adapter writes.

### D6: accounting beside input

Each model call records, side by side: uncached input tokens, cache-read tokens, cache-write tokens
per lifetime, and output tokens, as the provider reports them. The run record sums them per run and
counts segment changes (D1). Billing consumers (ADR-0126) price from these fields. These fields land
with the first merged change under this record, before any caching default changes.

### D7: acceptance

A test drives `operate(actions=True, tools=True)` for several rounds on a scripted endpoint with a
system message, tools and a first-turn context provider, and requires: each request's messages are a
strict prefix of the next; the tool definitions appear once per request; the system text is not in
any user message; the context block is present in every request of the run. A second test builds the
Anthropic payload for the last request and requires a `system` block, a `tools` field, at most four
marks and one-hour marks before five-minute ones. For a provider without the declaration, the
compiled request is byte-identical to today's. The P1 to P4 measurements above are that test run
against the code before this record.

## Consequences

- Runs on prefix-caching providers pay full input price for the stable and run segments once per
  lifetime and for each turn's new content once; the rest is read at the cache price. A run that
  changes its tool set mid-run pays a new write, visibly.
- The compiled request for those providers changes shape: system block, native tools, historical
  instructions keeping their response sections, context blocks kept for the run. Every other provider
  sees no change.
- ADR-0007's compiler, when built, inherits D1 to D5 for these providers; until then they land in the
  current `_prepare_run_kwargs` path, code that compiler will replace, which ADR-0007 D6 expects.
- `cache_control` and prompt caching become two options; code that set `cache_control=True` keeps
  getting the memo it got.
- Reversal: the capability declaration and the caching option are off switches; D6's fields are
  additive.

## Alternatives considered

- **Wait for ADR-0007's compiler and build caching on it.** One path and no interim code; it lost
  because runs on per-token providers pay uncached prices on every call until that compiler ships,
  and the rules here carry over to it unchanged.
- **A separate agent loop for API providers outside `Branch`.** Fastest to write; it lost because it
  is a second request compiler beside ADR-0007's, which ADR-0007 D5 rules out.
- **The provider's automatic caching (one top-level mark).** One mark moves with the tail and covers
  the whole prefix with one lifetime; it lost because the stable segment would be rewritten at the
  five-minute price whenever the tail's entry lapses, and P1 to P3 still break the prefix.
- **Keep the fold and mark the first user message.** Fewer changes; it lost because the fold mixes
  the stable segment with the first turn's content and P3's context block, so its lifetime is the
  shortest of the three, and P1 still breaks the prefix after it.
- **Render the tool sections in every historical instruction.** Restores the prefix with no new
  placement; it lost on input growth (D2).
- **Drop context blocks after the first turn, as today.** Smaller later requests; it lost because
  dropping them changes the earliest message of the request and so voids the whole cached prefix.

## Notes

- Provider facts are from Anthropic's prompt-caching and pricing documentation as read on
  2026-10-16; other providers that cache by prefix declare their own marks and lifetimes.
- The measurements in Context were taken at commit 54176709 and are reproduced by the D7 test against
  the code before this record.
