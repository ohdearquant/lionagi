# ADR-0127: Prompt-cache placement for prefix-caching providers

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: messages-context
- **Date**: 2026-10-16
- **Relations**: amends ADR-0007 D1 (the response format's lifetime), D2 steps 2, 4 and 5, and D4,
  and ADR-0008 D4, for providers that cache by prompt prefix and only while the prompt-caching
  option is on; carries the cache placement ADR-0126 R3 assigns to this record, whose reference is
  Claude Code's placement (lion1 ADR-0028 S10 and A1); read with ADR-0125 step 2, whose request
  compiler inherits these rules

Purpose: repeated model calls inside one run should reuse the provider's prompt cache, so a run on a
per-token API model pays the write price for the tokens each call adds and the read price for the
rest, instead of the full input price for everything.

## Context

Several providers cache a request by its prefix: a later request that starts with the same bytes
reads that part from cache at a fraction of the input price. Anthropic's Messages API, read on
2026-10-16, works this way. A request places at most four cache marks; a hit looks back at most 20
content positions from a mark; a write costs 1.25 times the input price for a five-minute entry and
2 times for a one-hour entry, a read 0.1 times, and a hit renews the entry's lifetime; one-hour marks
must come before five-minute marks; a mark whose prefix is shorter than the model's minimum
cacheable length is not placed.

ADR-0126 D3 defines a **run** as the lifetime of one Branch conversation identity. Inside one run,
`ReAct` or repeated `operate(actions=True)` makes a loop of model calls, and each call sends the
system text, the tool definitions, the history so far and one new turn. Caching that loop needs each
request to be a prefix of the next. Measured at commit 54176709 with a scripted endpoint (four
`operate(actions=True, tools=True)` rounds; this probe is a separate characterization, distinct from
the acceptance tests in D7):

**P1: no request in a tool loop is a prefix of the next.** Each pair of consecutive requests first
differs at the previous round's instruction. While an instruction is current it renders its
`Tools`, response schema and response format sections (`protocols/messages/instruction.py:210`);
once it is history it is re-rendered without them (`operations/chat/_prepare.py:228`). Rounds 0 to
1 differ at message 0, 1 to 2 at message 2, 2 to 3 at message 4. The current instruction is about
3,170 characters of JSON-encoded content, almost all of it those sections, sent uncached on every
call.

**P2: the system text is folded into the first user message,** as ADR-0007 D2 step 4 specifies, so
it cannot be cached with its own lifetime apart from the conversation, and it moves whenever the
first message changes.

**P3: a context block injected on the first turn only changes message 0 on the second.** A context
provider on the first-turn cadence is gathered into the fold for request 0 and absent from requests
1 and 2; message 0 shrinks from 3,240 to 112 characters of JSON-encoded content between them, so
request 1 shares no message-level prefix with request 0 at all (the two message 0 values share the
folded system line, 46 characters, before the injected block makes them differ).

**P4: the Anthropic adapter places one five-minute mark, and the flag that asks for it never
reaches it.** `providers/anthropic/messages.py:209-224` marks only the last message's last block,
with no lifetime choice, and lifts a native `system` field only when the first message has role
`system`, which the fold never produces. The `cache_control` flag never reaches that code: the
Anthropic request model has no such field, so the request-option filter drops it
(`service/connections/endpoint.py:149-153`) before `messages.py:209` looks for it. `iModel` then pops
the same flag from the request (`service/imodel.py:278`) and, in `Endpoint.call`, it switches on a
local response memo (`service/connections/endpoint.py:263-281`), a different feature. Building the
Anthropic payload for the probe's last request with `cache_control=True` gave no `system` field
and no cache mark.

**P5: the run record cannot show what caching saved.** `RunEnd` carries `cached_tokens` and
`cache_write_tokens` (`session/signal.py:66-67`) without the lifetime of the write, and ADR-0126's
ledger needs read and write counts per lifetime to price a run.

| Concern | Decision |
|---------|----------|
| What may change between calls | D1: three lifetime classes, an append-only turn segment, and a history rewrite as a counted turn-segment change. |
| Tool definitions and response format | D2: tools and the run's response sections render once in the stable segment; an instruction renders the same bytes in every request that contains it. |
| System text | D3: a native system block, chosen by a declared provider capability. |
| Context blocks | D4: context gathered for a run stays, unchanged, for the run. |
| Marks | D5: the provider adapter places them, only while the caching option is on: one-hour at the end of the stable segment, five-minute on the moving tail. |
| Accounting | D6: each call records input, cache read, cache write per lifetime and output, side by side. |
| Acceptance | D7: the prefix test, the Anthropic payload test, the option-off byte-identity test and the stable-segment test. |

**Out of scope.** A per-request input ceiling and what happens past it (a separate record or an
amendment; D1's classes are what any folding must respect). The provider's native `tools` field,
until a record maps its `tool_use` and `tool_result` blocks to action requests and responses
(D2). Providers that cache without marks. CLI providers, whose prompt the CLI assembles. Price
tables and budgets (ADR-0126).

## Decision

These decisions apply only when both gates are on: the selected provider declares that it caches
by prompt prefix (D3 names the declaration) **and** the model configuration turns prompt caching on.
Prompt caching is its own option on the model configuration, off unless set (D5). With the option
off, every provider's compiled request is byte-identical to today's apart from the mark removal D5
names, which changes today's bill for callers who get a five-minute cache write on every call; for a
provider without the declaration the request is otherwise unchanged whatever the option says.

### D1: three lifetime classes

Every part of a request belongs to one class:

- **stable**: the system text, the tool definitions and, where D2 puts them there, the response
  sections a run declares. It is the same for every call of every run of one configuration.
- **run**: what is fixed when a run starts and kept until it ends, today the context blocks a
  context provider returns for the run (D4). The Branch owns this segment: it builds it on the first
  call under this record and keeps it, with the position where its previous request ended, until the
  run ends.
- **turn**: the conversation. A request's turn segment is the previous request's turn segment
  followed by what the last call added.

A request is the stable segment, then the run segment, then the turn segment. Inside one run, a
request is a byte-for-byte prefix of the next unless the stable or run segment changed, and a
change to either is a new segment: it is allowed, it is not hidden, and the run record counts it
(D6).

A **history rewrite** is a turn-segment change, not a new class. It is allowed; it breaks the prefix
from the rewritten message on, so every later request in the run is a cache miss from there; and the
run record counts it (D6). Today's rewrites are the hide, show and summarize directives of
ADR-0125 step 2 and `ReAct`'s context dedupe (`operations/chat/_prepare.py:191-247`), which renders a
caller's context once on the instruction whose history first carries it and leaves it out of the
later copies.

### D2: tools render once; an instruction renders the same everywhere

Amends ADR-0007 D1 (the response format's lifetime), its D2 steps 2 and 5, and its D4. Tool
definitions leave the instruction and render once in the stable segment, as a section of the system
block (D3) built from the operation's explicit tool parameters as ADR-0007 D4 already allows. The
provider's native `tools` field is out of scope: it is not written until a record maps the
`tool_use` and `tool_result` blocks a native-tools reply carries back to action requests and
responses, which today's parser does not do (`protocols/messages/assistant_response.py:33-37`).

The response schema and the response format a run declares are fixed for the run, so they render
once, in the stable segment beside the tools. An instruction carries a response section of its own
only when its turn's format differs from the run's: a format that changes between turns (a final
answer in `ReAct`) lands in the turn segment, as that turn's content, and never rewrites the stable
segment. Rewriting the stable segment mid-run would pay the one-hour write price again, which is the
defect this decision removes. When an instruction does carry a section of its own, it renders that
section identically while the turn is current and while it is history, instead of appearing in the
one request and vanishing from the next. An instruction renders the same bytes whether it is current
or history.

Why this way: rendering the tool or response sections in every historical instruction would also
make each request a prefix of the next, but it repeats the whole tool set once per turn, so input
grows with the number of turns times the tool set. Dropping them only from history is what P1
measured.

### D3: system as its own block, by declared capability

Amends ADR-0007 D2 step 4 for these providers. A provider adapter declares whether it accepts a
native system block and caches by prefix. When it does, the compiler emits the system text and, where
D2 puts them there, the tool and response sections as the system block, and the first user message
carries only its own content. Per-call `guidance` stays in the instruction, in the turn segment,
where the caller put it; it is not part of the stable block. The compiler chooses from the
declaration; the adapter never re-folds or re-reads history (ADR-0007 D4 holds). Providers without
the declaration keep today's fold and role sequence.

ADR-0007 rejected a native system message because it changes the provider-visible role sequence on
surfaces that treat it differently. The declaration confines the change to providers that state
they accept it, and the characterization in D7 pins the sequence for the rest.

### D4: context gathered for a run stays for the run

Amends ADR-0008 D4 for these providers and only while caching is on. Context blocks returned for a
run are placed in the run segment as a second block of the system field, after the stable block,
each block carrying its own mark (D5). They are sent unchanged on every request of the run: they are
not re-gathered per call and not dropped after the first. A provider that returns a block only on the
first call of a run (one that checks whether the branch has a response yet) keeps its block in the
run segment for the later calls; a block a provider returns on a later call is added to the turn
segment as new content and does not rewrite the run segment. The turn segment's first message is
the first user message, carrying only its own content. The block list is threaded to the
compiler the way ADR-0008 D4 already threads it; the run segment itself is held by the Branch (D1),
which `_prepare_run_kwargs` reads from the branch it is handed.

ADR-0008 D4's alternatives reject persisting these blocks as ordinary messages partly because
retrieved material is "refreshed per turn". For these providers with caching on that reading
changes: a run-cadence block is gathered once per run and persists for the run, so it stops being
per-turn material. Its other reasons stand: the block is still not a conversation record, it is not
replayed as a user or system utterance, and the selection and budget rules are unchanged.

Attribution and boundaries: source attribution renders as it does today (ADR-0008 D4's one-newline
join), and moves to ADR-0007 D3's canonical JSON once that record is accepted.

The model therefore keeps seeing first-turn context on later turns where today it sees it once. That
is a change in what the model reads, so it lands behind its own characterization (D7) and the
injection budget is unchanged.

### D5: marks are placed by the provider adapter

The compiler hands the adapter the segment boundaries and the position in this request where the
previous request ended; the adapter turns them into its provider's marks. For Anthropic:

- one mark with a one-hour lifetime at the end of the stable segment;
- one five-minute mark at the end of the run segment when it is non-empty;
- one five-minute mark on the last block of the turn segment;
- when a turn adds more than the provider's lookback, one five-minute mark at that recorded position,
  on the previous request's last block, so the next read still finds the previous write.

At most four marks; one-hour marks precede five-minute marks; a mark whose prefix is shorter than the
model's minimum cacheable length is not placed. The lifetimes are settings of the model
configuration, with these as defaults.

Prompt caching is its own option on the model configuration, off unless set; with the option off
every provider's compiled request is byte-identical to today's apart from the mark removal below, and
the mark code only runs when the option is on and the provider declares prefix caching (D3). Marks
come only from this option. The Anthropic adapter's single `cache_control`-triggered mark
(`providers/anthropic/messages.py:209-224`) is removed, and that removal changes today's bill for
callers who get a five-minute cache write on every call: those tokens become ordinary input tokens.
The existing `cache_control` flag keeps its other current meaning, the local response memo in
`Endpoint.call`.

### D6: accounting beside input

Each model call records, side by side: uncached input tokens, cache-read tokens, cache-write tokens
per lifetime, and output tokens. The write counts are the provider's own per-lifetime fields, for
Anthropic the five-minute and one-hour cache-creation token counts. Today's extractor keeps only the
total cache-creation count (`session/signal.py:290-294`), so it drops the split; this record extends
it to carry both. The run record sums each field per run and counts segment changes, including a
history rewrite (D1). Billing consumers (ADR-0126) price from these fields. These fields land with
the first merged change under this record, before any caching default changes.

### D7: acceptance

Four tests, all written to pass after this record's change and its first implementation.

The first drives the API chat path on a scripted endpoint for several rounds with the option on and
the declaration present, with a system message, tools and a first-turn context provider, and
requires: each request's messages are a strict prefix of the next; the tool section appears once in
each request, in the system block; the system text is not in any user message; the context block is
present in every request of the run. The second builds the Anthropic payload for the last request and
requires the stable and run blocks in `system`, at most four marks and one-hour marks before
five-minute ones. The third runs the same rounds with the prompt-caching option off on a provider
that declares prefix caching, and requires the compiled request to be byte-identical to today's. The
fourth runs a `ReAct` run whose final turn changes the response format, and requires the stable
segment to stay byte-identical across every request of the run, so the changed format lands in the
turn segment and never rewrites the stable segment.

The P1 to P4 measurements in Context are a separate characterization, taken at commit 54176709 by a
probe that drives the same rounds against the code before this record; they are not these tests.

## Consequences

- Runs on prefix-caching providers pay the write price for the stable and run segments once per
  lifetime (1.25 times input for a five-minute entry, 2 times for a one-hour entry), the write price
  for each turn's new content once, and the read price (0.1 times input) for the rest. A run that
  changes its tool set or response sections mid-run pays a new write, visibly.
- The compiled request for those providers changes shape: a system block holding the tool and
  response sections, the run block after it, historical instructions keeping their own response
  sections, context blocks kept for the run, and marks from the caching option. Every other provider
  sees no change; and while the option is off, the only change is the removed adapter mark, which
  changes today's bill for callers who get a five-minute cache write on every call.
- ADR-0007's compiler, when built, inherits D1 to D5 for these providers; until then they land in the
  current `_prepare_run_kwargs` path, code that compiler will replace, which ADR-0007 D6 expects.
- `cache_control` and prompt caching become two options; code that set `cache_control=True` keeps
  getting the memo it got, while a provider cache mark now comes only from the caching option.
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
- **Render the tool and response sections in every historical instruction.** Restores the prefix
  with no new placement; it lost on input growth (D2).
- **Drop context blocks after the first turn, as today.** Smaller later requests; it lost because
  dropping them changes the earliest message of the request and so voids the whole cached prefix.

## Notes

- Provider facts are from Anthropic's prompt-caching and pricing documentation as read on
  2026-10-16; other providers that cache by prefix declare their own marks and lifetimes.
- The measurements in Context were taken at commit 54176709 by the probe described there. They are a
  characterization of the code before this record, not a D7 acceptance test; the D7 tests are written
  to pass after the change.
