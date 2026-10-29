# ADR-0127: Prompt-cache placement for prefix-caching providers

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: partial — the `prompt_caching` option and the provider declaration,
  the tool section in the stable segment (D2), the system text as its own block (D3), the one-hour
  mark on the stable segment and the five-minute mark on the turn (D5), and cache writes recorded per
  lifetime (D6) are shipped; Amendment 2 empties the conversation segment, so its mark (D1, D4) is
  never placed; the segment-change count (D6) and the multi-round acceptance test (D7) are open;
  Amendment 1 is Proposed and not started; Amendment 2 is Proposed and implemented, and replaces D4's
  placement; Amendment 3 is Proposed and implemented, and replaces D5's list of Anthropic marks with
  marks chosen from the entries a conversation has written, which includes the lookback reach;
  Amendment 4 is Proposed and implemented, and drops the end-of-turn mark on a request a host fold
  is expected to follow
- **Area**: messages-context
- **Date**: 2026-10-16
- **Relations**: amends ADR-0007 D1 (the tool definitions' lifetime), D2 steps 2, 4 and 5, and D4,
  and ADR-0008 D4, for providers that accept a native system block and cache by prompt prefix, and
  only while the prompt-caching option is on; depends on ADR-0018 D2 (Proposed), whose Branch lock
  makes the conversation segment safe to build once, and on ADR-0027 D4, which gives the adapter the
  native system field; changes one promise of ADR-0125 step 2 — context providers keep their gather
  count and order and their source attribution, but first-turn context block delivery, that
  promise's "today's behavior", is the one promise D4 changes, and only while the option is on, so
  with the option off the promise holds; carries the cache placement ADR-0126 R3 assigns to this
  record, whose reference is Claude Code's placement (lion1 ADR-0028 S10 and A1), a claim external
  to this repository's ADR corpus and held here at `lion1/docs/adr/028-anthropic/`; read with
  ADR-0125 step 2, whose request compiler inherits these rules; Amendment 1 qualifies ADR-0132
  Amendment 2's placement of the first round's guidance on endpoints that place the prompt cache;
  Amendment 4 reads the estimate of ADR-0130 Amendment 1's host fold and adds that fold's
  `tail_margin` setting, as a fifth profile key beside ADR-0130 Amendment 3's four

**Instruction.** This record serves an instruction the project's maintainer gave on 2026-10-16
(13:46 to 13:55 ET): "the problem with our own harness for too much stuff is the caching
management, mainly"; the "system prompt and common front-loaded prompts cached, commonly 1 hour";
and a run is one operation of many model calls, so the cache spans the run. The decisions carry
it: a one-hour entry on the system text and the tool section, a five-minute mark at the end of the
conversation segment and on the moving tail, cache writes accounted per lifetime (D6), and all of it
off unless the caching option is on. The same instruction asked that a model kept under a
per-request context ceiling for price reasons get context management from the library when work
continues past that ceiling ("lionagi needs to do context management, similar to auto
compaction"). This record departs from that part: the per-request input ceiling and the context
management past it are deferred to their own record, not yet written and not yet numbered (Out of
scope).

Purpose: repeated model calls inside one conversation should reuse the provider's prompt cache, so a
conversation on a per-token API model pays the write price for the tokens each call adds and the read
price for the rest, instead of the full input price for everything.

## Amendment 1 (2026-10-26): an LNDL Middle's round guidance in the stable segment

Status: Proposed. Extends D1, D2, D3, D5 and D7 for guidance an LNDL Middle (ADR-0132) holds
constant for a whole operation, and qualifies ADR-0132 Amendment 2's placement of the first round's
guidance. Every rule here applies only while both gates of the Decision are on. With the option off,
or on an endpoint without the declaration, compiled requests keep their previous bytes, wording and
guidance placement, and no loop default changes.

**Placement.** When the endpoint that sends the request places the prompt cache, the LNDL Middle's
rules, its fixed target spec and, for `lndl-calls`, the tools rendered as calls render in the stable
segment, after the Branch's system text and before any tool section. One resolution of the model the
operation runs on decides both the placement and the request: a model the caller passes for the
operation is used whatever its value, as `_run_round_chat` in `lndl_middle.py` already selects it,
and the Branch's model only when none is passed.

**Ownership.** The Middle passes that text as the operation's stable guidance, an explicit field of
the chat parameters that defaults to none, and passes the instruction to leave the `Tools:` section
out of the stable segment on the same path. Request compilation reads both only from there, never
from ambient context, so a nested call, a call on another Branch, or a task started while a round
runs receives neither. The stable guidance is not written into any instruction, is not persisted
with one, and is not forwarded to the provider as a request option. The `_no_tool_section` context
variable of ADR-0132 Amendment 2 moves onto this path.

**Caller guidance.** Guidance the caller supplies keeps its place in the first round's instruction
and is never copied into the stable guidance. Response schemas and formats other than the Middle's
fixed target spec keep D2's turn placement.

**`lndl-calls`.** The tools appear only as LNDL calls, and the stable segment holds no further
`Tools:` section. Every request of the operation carries the same stable guidance, and the first
round's user content no longer carries it.

**Stability.** The complete stable segment is taken once, before the operation's first request, and
normal, repair and final rounds all send the same bytes; a repair never rebuilds it. A change to the
Branch's system text or tool set while the operation runs is refused.

**Transitions.** The next operation on the Branch uses its own configuration. A stable segment that
differs from the previous request's, including one that adds or drops operation guidance, is a new
segment, and D6 counts it before the request is sent.

**Resume.** A resumed LNDL operation rebuilds its stable guidance from the recorded configuration and
guidance version; a changed configuration starts a new, counted segment. Instructions already in
history are not rewritten.

**Marks.** D5 is unchanged. The one-hour mark at the end of the stable segment now covers the
guidance with the system text.

**Acceptance.** D7 adds checks on captured payloads: caller guidance stays in the turn segment and
never reaches the stable segment; a nested call, a call on another Branch and a task started inside a
round carry no stable guidance and no tool-section suppression, and both are cleared on success,
error and cancellation; the stable bytes are identical across normal, repair and final rounds; an
LNDL, then ordinary, then LNDL sequence on one Branch counts each segment change; a resumed operation
rebuilds the same stable bytes, and a record from before this amendment resumes with a counted
change; with the option off, on filtered and unfiltered endpoints, and on an endpoint without the
declaration, requests are byte-identical to before; a model passed explicitly decides the placement.

**Why.** With caching on and `lndl-calls`, the stable segment under D3 is the system text alone, 656
characters in the measured configuration, below the 512-token minimum cacheable prompt of
`anthropic/claude-haiku-5-5`, so its one-hour mark caches nothing. The rules and the calls, about
11,000 characters, sit in the first round's user message beside the task, with no mark between
them, so every new operation writes them to the cache again. Measured on 50 SWE-bench Verified tasks
on `anthropic/claude-haiku-5-5`, `lndl-calls`, caching on, three runs per layout:

| | guidance in the first round (shipped) | guidance in the stable segment |
|---|---|---|
| cache reads over all input, per run | 84.4%, 85.0%, 86.4% | 88.8%, 88.9%, 89.8% |
| first replies written as provider tool markup, which nothing runs | 112 of 150 | 31 of 150 |
| requests whose output reached the 8,192-token cap | 57 | 14 |
| cost of a 50-task run, mean | $0.365 | $0.220 |
| tasks resolved | 106 of 150 | 100 of 150 |

Most of the saving is output: fewer rounds that run to the cap, and fewer requests. The arms differ
in more than placement: the measured change also reworded one sentence of the guidance, which this
amendment does not adopt, wrapped the guidance differently, carried it in another role, and copied
the caller's guidance into the system block as well, which the Caller guidance rule above forbids.
These numbers describe that change as measured, not placement alone. Averaged per task, the stable
layout resolved 4.0 points fewer, with a standard error of 2.45 across the 50 tasks, which does not
establish a quality difference in either direction.
A claim about placement alone needs runs with the wording held constant and the arms interleaved.
`prompt_caching` stays off unless set, so the amendment changes no default.

## Amendment 2 (2026-10-27): a turn's context blocks stay at that turn

Status: Proposed. Replaces D4's placement of context blocks and empties D1's conversation segment;
D4's other rules stand: a block is gathered once, it persists for the conversation, it is not a
conversation record, and the selection and budget rules are unchanged. Every rule here applies only
while both gates of the Decision are on. With the option off, or on an endpoint without the
declaration, compiled requests keep their previous bytes: each request shows only its own blocks, in
the first instruction, and nothing is kept.

**Instruction.** The maintainer, 2026-10-27 (16:49 and 16:50 ET): "a react loop, every following
round can benefit from previous round being cached", and, of recall, "...cannot inject info all the
way at beginning... we shouldn't change the initial parts that often".

**Placement.** The blocks a context provider returns for a turn are kept on that turn's instruction,
under its `metadata["context_blocks"]`, and sent after the instruction's own content as one more text
block of the same user message, on that request and on every later request of the conversation. The
first blocks kept stay: a gather repeated for the same instruction does not replace them. A block a
provider returns on a later turn is kept on that later turn's instruction and is sent at that turn,
so it never rewrites an earlier message. Nothing is placed in the system field, so the stable segment
is the same bytes for every conversation of one configuration.

**Segments.** Context blocks are turn-segment content from the turn that gathered them. D1's
conversation segment therefore holds nothing, D5's mark at its end is never placed, and D4's adapter
change (an ordered list of conversation blocks in the system field) is not needed.

**Persistence.** The kept blocks are part of the instruction's metadata and are serialized with the
Branch, so a resumed conversation sends the same bytes; a clone keeps them with its copy of the
instruction. What a provider returns is therefore stored with the Branch and sent again, as
user-role content, on every later request of the conversation. A provider whose output must not be
stored or sent again returns nothing for that turn; with the option off nothing is kept.

**Why.** A provider that answers only before the conversation has a response, as a first-turn
recall does, returned its block on the first request only, so the second request rewrote the first
user message and read nothing past the system text; every later request was a prefix of the one
before it only from the third on. Putting the block in the system field would instead place
conversation-specific text ahead of every message and in front of the turn segment, the part of the
request the instruction asks to keep unchanged.

**Acceptance.** D7 adds checks on captured payloads: with a first-turn-only provider every request's
messages are a prefix of the next request's, and every request carries the block; a later turn's
blocks stay at that turn and the task message is unchanged; a repeated gather keeps the first blocks;
the kept blocks survive a serialized round trip; with the option off, the blocks are shown once, in
the first instruction, and nothing is kept.

## Amendment 3 (2026-10-27): marks placed from the entries a conversation has written

Status: Proposed and implemented. Replaces D5's list of Anthropic marks; D5's option, its two gates,
the limit of four marks and the order of lifetimes stand. Adds a per-request cache record to D6 and
its checks to D7. Every rule here applies only while both gates of the Decision are on. With the
option off, or on an endpoint without the declaration, compiled requests keep their previous bytes.

**Instruction.** The maintainer, 2026-10-27 (16:49 ET): "a react loop, every following round can
benefit from previous round being cached. all processes can benefit from the shared system prompt
and guidance cache", and of a loop's last step: "the last step doesn't need caching, because we will
fold, then after fold, we do cache control again".

**Why.** D5 marked fixed places: the end of the stable segment and the end of the turn. A round whose
new content is longer than the provider's lookback then reads nothing of the previous round's
entry, and a request that nothing follows writes an entry no request reads. The marks are chosen
here from what the conversation's earlier requests are known to have written.

**Certificates.** The adapter reads the request as the API reads it: tools, then system blocks, then
each message's content blocks. Every block gets a prefix certificate, a running SHA-256 over the
request through that block, in three tiers. The tools tier is seeded with the certificate format
version, the model, the `thinking` setting and `output_config.effort`; the system tier continues
the tools tier and adds `speed`; the messages tier continues the system tier and adds `tool_choice`.
Each message adds a boundary token carrying every message field except its content, so a block moved
across a message boundary, or a message inserted, changes every later certificate. The only edit
before hashing is removing `cache_control` from the block itself, and string content certifies as
the one text block the API reads it as. Equal certificates mean two requests are identical through
that block; they say nothing about whether the provider still holds the entry. `thinking` and effort
sit in the tools tier, the conservative choice, until a rule per model is documented.

**Reach.** A mark reaches an entry when it sits at or after the entry's block and fewer than 20
lookback positions past it. A run of consecutive `tool_use` blocks, or of `tool_result` blocks, in
one message is one position. Thinking blocks and blank text blocks take no mark.

**Rows.** A Branch keeps, in `metadata["prompt_cache"]`, one row per entry its completed requests
wrote or read, keyed by certificate: the lifetime, the tier, the stored prefix length when it is
known, the time it was last marked and last read, and, per route (an account name, or `direct`),
the process that saw it written or read there. Rows are written only from a completed request's own
placement and usage, never at compile time. An entry is established when this process saw it on
every route the request can take: the configured accounts, or `direct`. A row restored in another
process, as a resumed run's Branch is, or copied into a clone, therefore starts with no evidence; a
row without the shape written here is dropped when the rows are read. Rows leave at expiry. Past
512 rows the rows that matter least are evicted (five-minute rows first, then the shortest prefixes,
then the soonest to expire), and the latest expiry evicted is kept as a watermark until which the
guarantee below does not apply.

**Intent.** An operation declares what follows its request. `continuing` is the default. `final`
means nothing of the conversation follows: a `ReAct` answer request, an LNDL Middle's last round, a
parse repair. `side` means the request shares nothing with the conversation: the context fold's
summary request (ADR-0130).

**Placement.** At most four marks, the caller's own counted, chosen in this order, each only while a
slot is free and the order of lifetimes stays legal:

1. A read of the deepest live entry established on every route. A mark this request places anyway,
   the end of the turn or an anchor, serves as the read when it reaches the entry; otherwise the
   entry's own block is marked, with the entry's lifetime.
2. Five minutes on the last markable block of the last message, when the request is `continuing`.
   When the turn carries kept context blocks (Amendment 2), that text block is the last one, so the
   mark covers it.
3. One hour on the end of the tools and system blocks. A caller's mark there is raised to one hour
   when every earlier mark is one hour.
4. A probe of each deeper live entry whose evidence is unknown, deepest first, skipped when a mark
   already reaches it; `probe_slots` is recorded when the slots run out. A `side` request probes
   nothing.
5. Five minutes on each anchor the compiler names, a (message, block) place a later request should
   still reach, when the request is `continuing`. Each anchor takes a mark of its own: a later mark
   that reaches an entry reads it but never writes or refreshes it. The chat compile names two: the
   end of an included first round and the end of the newest compaction note (ADR-0130 Amendment 4).

A mark whose lifetime would break the order in either direction is not placed, and `ttl_order` is
recorded with its role; a read's lifetime is never changed to make it legal. A `final` request
places no turn mark and no anchors. A `side` request reads no conversation rows and places only the
one-hour mark.

**Record (D6).** Each request records, under `prompt_cache` in its call metadata, the marks with
their roles, the landing set (the live entries a mark reaches) and three fields:

- `g1`: `excluded` with a reason, one of `no_established_candidate`, `route_unselected` (an entry is
  established on some routes but not all), `ledger_evicted`, `caller_marks`, `ttl_order` and
  `length_unknown`; else `met` when a mark as sent reaches the read target, and `defect` when none
  does. Only `defect` is a defect.
- `read`: `unevaluated` when the attempt returned no complete usage. For `g1: met`, `at_or_above`
  the target's stored length, `below_expired` when the attempt started after the target's evidence
  expired, and `below_live` otherwise. An excluded request records the read size with no verdict.
- `hit`: `identified` with the entry's certificate only when exactly one landing entry established
  on the attempt's route has a stored length equal to the read and no deeper landing entry has an
  unknown length; else `unidentified`, or `none` when nothing was read. With a server tool (web
  search, web fetch, code execution) in the request, a read is always `unidentified`.

When the request completes, each marked entry is recorded as written on the attempt's route, the
deepest mark's stored length becomes the read plus the cache write, and an identified entry's read
time moves to the attempt's start. With a server tool in the request no stored length is recorded,
because the usage then need not describe this request's prefix alone.

**What is guaranteed.** Where `g1` applies, the payload carries a mark that reaches the deepest
established entry, and that is checked on the payload as sent. A read at or above the entry's length
is expected and measured, never guaranteed: the provider may no longer hold the entry. Whether the
provider's usage counts are exact is unverified, so a rounded count equal to a landing entry's
length would refresh that entry falsely; the landing-set rule narrows this and does not exclude it.

**Limits.** The streaming `run()` path opens no call context: its requests get the turn and one-hour
marks and write no rows.

**Acceptance.** D7 adds tests on captured payloads and a scripted endpoint: each certificate seed
and the message envelope, string and list content, runs; reach at 19, 20 and 21 positions; the order
of placement for `continuing`, `final` and `side` requests, a resumed Branch, a clone and a fold;
the order of lifetimes in both directions; one and two routes; each exclusion; and the record after
a request completes, including a retry on a second route, eviction at the ceiling and expiry. Two
conversation requests end to end: the second request's read target is the first request's turn mark,
and it records `g1: met` and `read: at_or_above`. A paid measurement of reads across rounds is a
separate characterization and not one of these tests.

## Amendment 4 (2026-10-28): no end-of-turn entry on a request a fold follows

Status: Proposed and implemented. Changes step 2 of Amendment 3's placement only: the read (step 1),
the one-hour mark (step 3), the probes (step 4) and the anchors (step 5) stand, with their lifetimes.
Adds one field to the D6 record. Applies only while both gates of the Decision are on and a host fold
(ADR-0130 Amendment 1) is installed on the branch; every other request places as Amendment 3 says.

**Instruction.** Amendment 3 quotes the maintainer on a loop's last step: "the last step doesn't need
caching, because we will fold, then after fold, we do cache control again". Amendment 3 serves the
last step with `final`. This amendment serves the fold: the request before a fold is the last one to
read the conversation as it stands.

**Why.** A fold rewrites the active view from its oldest folded message on, so the request after it
shares the earlier requests' prefix only up to that message, and the entry the request before the
fold writes at the end of its turn goes unread. On the API-model agent runs of 2026-10-27 and the
morning of 2026-10-28 (105 runs on `claude-haiku-5-5`, cap 100,000, fold line at 0.9 of the cap),
the 119 requests a fold followed wrote 2,600,072 cache-write tokens that no later request read, of
15,116,173 written in all.

**Predicate.** The host fold estimates each request before it is compiled (ADR-0130 Amendment 1, D3
and D4 as amended) and folds when the estimate reaches the fold line, `context_fold_at` times the cap
(ADR-0130 Amendment 3). Let e be the fold's estimate of request N as it will be sent: the value it
compared with the line, or, when a fold landed, its estimate after the fold. Request N+1 folds when
it reaches the line, so a fold follows N when the round grows by at least the gap,
`fold_at × cap − e`. Request N places no end-of-turn mark when `0 < gap ≤ m × cap`. A request
sent at or past the line keeps its mark: its fold landed nothing, or not enough, and nothing says
the next request's fold lands where this one's did not.

**Margin.** The price sets m. Without the end-of-turn mark, request N's new tokens are billed at the
base input price instead of the five-minute write price, a saving of 0.25 of the base price per
token. If no fold follows, request N+1 writes those tokens at 1.25 where it would have read them at
0.1, a cost of 1.15 per token, 0.9 net of the saving. Skipping therefore pays when a fold follows
with probability at least 0.9 / 1.15 ≈ 0.78, that is, when a round grows by less than the gap with
probability at most p\* = 0.25 / 1.15 ≈ 0.217. The margin m is the p\* quantile of a round's growth
as a share of the cap. It measured 0.0434 over the 893 requests of the population above, 0.0464 over
the 2,538 requests of 2026-10-20 to 2026-10-26 (whose runs include a fold line of 0.8 and a target of
0.4), and 0.0453 over both. The default is m = 0.045, the fold's `tail_margin` setting; 0 turns the
rule off, and a value outside `0 <= m < context_fold_at` is refused.

**The profile key.** A profile declaring `api_tools` sets m with the top-level key
`context_fold_tail_margin`, a fifth key beside the four of ADR-0130 Amendment 3 and under its rules:
it is read from the profile's `extra` mapping; an explicitly present null, a boolean, a numeric
string, a non-finite value and a value outside `0 <= m < 1` are refused before a run is allocated,
as is an effective margin not below the effective `context_fold_at`; presence without `api_tools` is
refused. Absent means 0.045. The branch's `api_launch` record stores the requested value, null when
absent, and a resume reads and persists it as it does the other four. Installing a second fold on
the same live branch returns the existing one only when cap, line, target and margin agree. The run
manifest's `context_fold` entry records the margin beside the line and the target.

**Placement, changed.** Step 2 places no five-minute mark on a request the rule selects. Step 1 then
reads through an anchor that reaches the entry, else marks the entry's own block, as Amendment 3
provides. `final` and `side` requests, and conversations without a host fold, are unchanged.

**Record (D6).** A selected request records `tail_skip` under `prompt_cache`, with `estimate` (e),
`fold_line` and `margin`, in the fold's scaled tokens. Other requests record no `tail_skip`.

**Measured on replay.** At m = 0.045, priced at 0.25 saved per unread write token and 0.9 per token a
wrong selection costs the next request, in base-price tokens:

- 2026-10-27: 721 requests, 99 followed by a fold. 44 selected: 37 followed by a fold and 7 not, of
  which 2 lost reads of 20,356 tokens in all. Net +139,199.
- 2026-10-28, morning: 172 requests, 20 followed by a fold. 11 selected: 9 followed by a fold and 2
  not, no read lost. Net +30,541.
- 2026-10-20 to 2026-10-26: 2,538 requests, 160 followed by a fold. 70 selected: 58 followed by a fold
  and 12 not, no read lost. Net +139,968.

A wrong selection lost nothing when the next request did not read the entry either, as after a
conversation the agent shortened itself or with caching off. On 2026-10-27 and 2026-10-28 the
selected requests wrote 752,243 tokens no later request read. Skipping exactly the requests a fold
followed would save 650,018 on 2026-10-27 and 2026-10-28 and 495,378 on 2026-10-20 to 2026-10-26; the
rule takes 26% and 28% of that. Every margin from 0.005 to 0.075 is net positive on all three
populations; at 0 the rule selects nothing, and 0.08 is net negative on 2026-10-28 and on
2026-10-20 to 2026-10-26. Four of the 3,431 requests were sent at or past the line, and the rule
keeps their marks. The next request read none of those four entries, so keeping them gives up
22,944 on replay. The case the guard is for, a conversation that stays above the line with nothing
folded, appears once: two requests at the end of one run, where the second read nothing of the
first. On one run with three folds, the gaps before them were 45,312, 564 and 177 tokens: the rule
selects the last two, and of the requests no fold followed, the nearest to the line had a gap of
8,965 and is not selected.

**Limits.**

- A round that grows past the margin is not foreseen, so the requests before a large step keep their
  unread writes; they hold most of the 2,600,072.
- The rule rests on the estimate. Rebuilt from the recorded runs, it matched the 119 recorded
  post-fold estimates with a median error of 0.0% and a tenth percentile of −1.0%, against a margin
  of 4.5% of the cap.
- A conversation that stays at or above the line with nothing left to fold keeps every turn's
  mark, as it would without this amendment: no gap says when such a conversation folds next.
- These numbers replay recorded runs; they do not measure the rule. It is measured on new runs once
  it lands, and the default returns to 0 if a week of runs is net negative.

**Acceptance.** D7 adds tests on captured payloads and a scripted fold: a request within the margin
of the line places no end-of-turn mark, reads through its anchor or the entry's own block, keeps the
one-hour mark and the probes, and records `tail_skip`; a request outside the margin keeps its turn
mark; a folded request is judged by its estimate after the fold; a request at or past the line
keeps its mark, whether its fold landed nothing or left it above the line; a zero margin selects
nothing; the fold's own summary request clears the decision; `final` and `side` requests ignore it;
and a margin outside its range is refused.

## Context

Several providers cache a request by its prefix: a later request that starts with the same bytes
reads that part from cache at a fraction of the input price. Anthropic's Messages API, read on
2026-10-16, works this way. A request places at most four cache marks; a hit looks back at most 20
content positions from a mark; a write costs 1.25 times the input price for a five-minute entry and
2 times for a one-hour entry, a read 0.1 times for most models (0.05 times for Opus 5.5 and Sonnet
5.5, 0.025 times for Fable 5.1 and Mythos 5.1), and a hit renews the entry's lifetime; one-hour marks
must come before five-minute marks; a mark whose prefix is shorter than the model's minimum
cacheable length (512 tokens on the current models, up to 4,096 on older ones) is not cached: the
request is processed without caching and without an error.

OpenAI's [prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching),
read on 2026-10-29, describes implicit caching for GPT-5.6 and later: an eligible prefix is written
at the end of the "latest eligible message" at 1.25 times the uncached input price for the default
30-minute lifetime. A later request
can read a matching rendered prefix at its implicit breakpoint or up to 20 earlier eligible message
endings, subject to the model's minimum cacheable length and an available entry. The OpenAI chat
adapter places no explicit marks.

Two records use "run" for different things, so this record names both senses. ADR-0126 D3 defines a
**run**, for this router, as the lifetime of one Branch conversation identity; ADR-0123 D1 defines a
**Run** as exactly one admitted execution attempt. This record uses **conversation** for the first
sense — the lifetime of one Branch conversation identity — and fixes the **conversation segment** to
that lifetime. Inside one conversation, `ReAct` or repeated `operate(actions=True)`
makes a loop of model calls, and each call sends the system text, the tool definitions, the history
so far and one new turn. Caching that loop needs each request to be a prefix of the next. Measured at
commit 54176709 with a scripted endpoint (four `operate(actions=True, tools=True)` rounds; this probe
is a separate characterization, distinct from the acceptance tests in D7):

**P1: no request in a tool loop is a prefix of the next.** Each pair of consecutive requests first
differs at the previous round's instruction. While an instruction is current it renders its
`Tools`, `ResponseSchema` and `ResponseFormat` sections (`protocols/messages/instruction.py:211-216`);
once it is history it is re-rendered without them (`operations/chat/_prepare.py:314`). Rounds 0 to
1 differ at message 0, 1 to 2 at message 2, 2 to 3 at message 4. The current instruction is about
3,170 characters of `json.dumps` of the whole message object, role wrapper included (3,141 of the
content alone), almost all of it those sections, sent uncached on every call.

**P2: the system text is folded into the first user message,** as ADR-0007 D2 step 4 specifies, so
it cannot be cached with its own lifetime apart from the conversation, and it moves whenever the
first message changes.

**P3: a context block injected on the first turn only changes message 0 on the second.** A context
provider on the first-turn cadence is gathered into the fold for request 0 and absent from requests
1 and 2; message 0 shrinks from 3,240 to 112 characters of `json.dumps` of the whole message object,
role wrapper included (3,211 to 83 of the content alone), so request 1 shares no message-level prefix
with request 0 at all (the two message 0 values share the folded system line, 46 characters of raw
text, before the injected block makes them differ).

**P4: the Anthropic adapter places one five-minute mark, and whether the flag that asks for it
reaches the mark code depends on the endpoint.** `providers/anthropic/messages.py:1152-1178` marks only
the last message's last block, with no lifetime choice, and lifts a native `system` field only when
the first message has role `system`, which the fold never produces. The `cache_control` flag reaches
that code on one of two paths, and the two paths differ:

- **A default `iModel` carries a request-option model**, whose fields are the Anthropic request
  model's; `cache_control` is not one of them, so the request-option filter drops the flag
  (`service/connections/endpoint.py:562-565`) before `messages.py:1152` looks for it. Building that
  payload for the probe's last request with `cache_control=True` gave no `system` field and no cache
  mark.
- **An endpoint with no request-option filter carries no request-option model** — an `iModel`
  restored from a serialized config, whose `request_options` comes back None, or a model configured
  from a plain dict. There the flag is not filtered out, so the flag reaches the mark code and one
  ephemeral five-minute mark lands on the last message's last block.

`iModel` then pops the same flag from the request (`service/imodel.py:403`) and, in `Endpoint.call`,
it switches on a local response memo, except for a call routed through an account
(`service/connections/endpoint.py:705`, `:776-794`), a different feature.

**P5: the run record cannot show what caching saved.** `RunEnd` carries `cached_tokens` and
`cache_write_tokens` (`session/signal.py:85-86`) without the lifetime of the write, and ADR-0126's
ledger needs read and write counts per lifetime to price a run in its own sense of the word.

| Concern | Decision |
|---------|----------|
| What may change between calls | D1: three lifetime classes, a turn segment whose changes are counted, and a history rewrite as one of those changes. |
| Tool definitions and response format | D2: the conversation's tool set renders once in the stable segment; every response schema and format renders in the turn segment; an instruction renders the same bytes in every request that contains it. |
| System text | D3: a native system block, chosen by a declared provider capability. |
| Context blocks | D4: context gathered for a conversation stays, unchanged, as the system blocks after the stable block. |
| Marks | D5: the provider adapter places them, only while the caching option is on: one-hour at the end of the stable segment, five-minute on the moving tail. |
| Accounting | D6: each call records input, cache read, cache write per lifetime and output, side by side. |
| Acceptance | D7: the prefix test, the Anthropic payload test, the two option-off byte-identity tests, the stable-segment test and the two body-absence tests. |

**Out of scope.** A per-request input ceiling and the context management past it, which the
instruction this record serves asked for (deferred to their own record, not yet written or
numbered; D1's classes are what any folding must respect). The provider's native `tools` field,
until a record maps its `tool_use` and `tool_result` blocks to action requests and responses
(D2). Providers that cache without marks. CLI providers, whose prompt the CLI assembles. Price
tables and budgets (ADR-0126).

## Decision

These decisions apply only when both gates are on: the selected provider declares that it accepts a
native system block and caches by prompt prefix (D3 names the declaration) **and** the model
configuration turns prompt caching on. The option is `prompt_caching` (bool, default False), off
unless set (D5). With the option off, every provider's compiled request is byte-identical to today's
on every path, with no exception: the Anthropic adapter's `cache_control`-triggered last-message mark
(`providers/anthropic/messages.py:1152-1178`) stays exactly as today wherever it fires today.

Restated for both paths: on a default `iModel` the request-option filter drops the flag before the
mark code, so no mark is placed; on an endpoint with no request-option filter — an `iModel` restored
with `request_options` None, or a dict-configured model
(`service/connections/endpoint.py:562-565`) — the flag reaches the mark code and one five-minute mark
lands on the last message. With the option on and the declaration present, this record's placement
replaces that mark. For a provider without the declaration the request is unchanged whatever the
option says. There is no ungated change.

### D1: three lifetime classes

Every part of a request belongs to one class:

- **stable**: the system text and the tool definitions. It is the same for every call of one
  configuration, and the tool set the Branch exposes (D2) is part of that configuration.
- **conversation**: what is fixed when a conversation starts and kept until it ends, today the
  context blocks a context provider returns for the conversation (D4). The Branch owns this segment.
  It is keyed by the Branch's conversation identity and built once, under the Branch lock a
  top-level turn holds (ADR-0018 D2, Proposed), and it keeps the position where its previous
  request ended. It
  ends when the Branch is closed or when its conversation identity changes; both end events discard
  the segment, and the next call builds a new one. Concurrent calls never overwrite or clear it, so
  ADR-0008 D4's call-local rule holds for everything else a call threads. A retried attempt (an
  ADR-0123 run) inside one conversation reuses the segment unchanged: a retry is a new attempt, not
  a new conversation.
- **turn**: what each call appends. A request's turn segment is the previous request's turn segment
  followed by what the last call added.

A request is the stable segment, then the conversation segment, then the turn segment. Inside one
conversation, a request is a byte-for-byte prefix of the next unless the stable or conversation
segment changed, and a change to either is a new segment: it is allowed, it is not hidden, and the
run record counts it (D6).

A **history rewrite** is a turn-segment change, not a new class. It is allowed; it breaks the prefix
from the rewritten message on, so every later request in the conversation is a cache miss from
there; and the run record counts it (D6). Today's rewrites are the hide, show and summarize
directives of ADR-0125 step 2 (`operations/chat/_prepare.py:258-344`).

### D2: tools render once; an instruction renders the same everywhere

Amends ADR-0007 D1 (the tool definitions' lifetime and the transient set), its D2 steps 2 and 5, and its D4.

**The stable segment is the system text and the tool definitions only.** Tool definitions leave the
instruction and render once in the stable segment, as a section of the system block (D3). They are
the conversation's tool set — the tools the Branch exposes to the operation — not the per-call
`action_param`. A call that requests no action, such as `ReAct`'s final answer
(`operations/ReAct/ReAct.py:619` sets `action_param` None), still carries the same tool section in
the stable segment; its turn content says that no action is requested. A plain `operate()` whose
tool set differs from the previous call's is therefore a counted stable-segment change (D6).

Rendering the tools from the conversation's tool set rather than from the call's `action_param` is
what keeps the stable segment stable: `action_param` is None on ReAct's final call, so a tool
section built from it would drop out of that request and rewrite the stable block, paying the
one-hour write price again — the defect this decision removes. Reaching for `branch.acts` instead is
not a new dependency: `operations/operate/operate.py:200-204` already builds the tool schemas from
the Branch's action manager when a call passes `tools=True`.

The provider's native `tools` field is out of scope: it is not written until a record maps the
`tool_use` and `tool_result` blocks a native-tools reply carries back to action requests and
responses, which today's parser does not do (`protocols/messages/assistant_response.py:33-37`).

**Every response schema and response format renders in the turn segment.** The rendered schema is
the `ResponseSchema` of the request operative: it includes the fields lionagi adds on top of the
caller's model — `reason` when the call asks for it (`operations/operate/step.py:92-94`) and
`action_required`, `action_requests` and `action_responses` when the call passes actions
(`operations/operate/step.py:96-99`), which `operations/operate/operate.py:218-223` requests
through `Step.request_operative`. It renders there as part of the instruction that asks for it, and
it renders the same bytes whether it is current or history, instead of appearing in the one request
and vanishing from the next. A format that changes between turns therefore renders in the turn
segment and never rewrites the stable segment.

Why this way: rendering the tool section in every historical instruction would also make each
request a prefix of the next, but it repeats the whole tool set once per turn, so input grows with
the number of turns times the tool set. Dropping it only from history is what P1 measured. The
response sections are small beside the tool set and belong to the instruction that asks for them, so
they stay in the turn segment and repeat with each instruction that carries them.

### D3: system as its own block, by declared capability

Amends ADR-0007 D2 step 4 for these providers. A provider adapter declares whether it accepts a
native system block and caches by prefix. When it does, the compiler hands the adapter the stable
blocks — the system text and the tool section — as an ordered list (D5), and the first user message
carries only its own content. Per-call `guidance` stays in the instruction, in the turn segment,
where the caller put it; it is not part of the stable block. The compiler chooses from the
declaration; the adapter never re-folds or re-reads history (ADR-0007 D4 holds), and the adapter is
what builds the provider's native system field (ADR-0027 D4). Providers without the declaration keep
today's fold and role sequence.

ADR-0007 rejected a native system message because it changes the provider-visible role sequence on
surfaces that treat it differently. The declaration confines the change to providers that state
they accept it, and the characterization in D7 pins the sequence for the rest.

### D4: context gathered for a conversation stays for the conversation

**Amended by Amendment 2:** context blocks stay at the turn that gathered them, not in the system
field; the rest of this decision stands.

Amends ADR-0008 D4 for these providers and only while caching is on. Context blocks returned for a
conversation are placed in the system field as the conversation blocks, after the stable block,
with one mark at the end of the conversation segment (D5). They are sent unchanged on every request
of the conversation: they are not re-gathered per call and not dropped after the first. A provider
that returns a block only on the first call of a conversation (one that checks whether the branch
has a response yet) keeps its block in the conversation segment for the later calls; a block a
provider returns on a later call is added to the turn segment as new content and does not rewrite
the conversation segment. The turn segment's first message is the first user message, carrying only
its own content.
The block list is threaded to the compiler the way ADR-0008 D4 already threads it; the conversation
segment itself is held by the Branch (D1), which `_prepare_run_kwargs` reads from the branch it is
handed.

Placing a second block in the system field is an adapter change, not a compiler-only one. The
Anthropic adapter today lifts at most one leading system-role message out of the messages and wraps
that one string in a single-element `system` list (`providers/anthropic/messages.py:1112-1117` and
`:1180-1182`), so a request carrying two system messages is rejected as an invalid payload. The
compiler therefore hands the adapter the ordered lists it needs — the stable blocks and the
conversation blocks — and the adapter must accept an ordered list of system blocks and mark the end
of each segment, rather than lifting a single message. ADR-0027 D4 owns that boundary: provider
adapters own vendor request grammar and the payload remapping that goes with it.

ADR-0008 D4's alternatives reject persisting these blocks as ordinary messages partly because
retrieved material is "refreshed per turn". For these providers with caching on that reading
changes: a conversation-cadence block is gathered once per conversation and persists for the
conversation, so it stops being per-turn material. Its other reasons stand: the block is still not a
conversation record, it is not replayed as a user or system utterance, and the selection and budget
rules are unchanged.

Attribution and boundaries: source attribution stays in `ProviderReport.fired` (ADR-0008 D5), which
pairs `fired[i]` with `blocks[i]`; it does not render in the request today, and it moves to
ADR-0007 D3's canonical JSON once that record is accepted.

The model therefore keeps seeing first-turn context on later turns where today it sees it once. That
is a change in what the model reads, so it lands behind its own characterization (D7) and the
injection budget is unchanged.

### D5: marks are placed by the provider adapter

**Amended by Amendment 3:** the list of Anthropic marks below is replaced by marks chosen from the
entries a conversation has written; the option, its gates, the limit of four and the order of
lifetimes stand.

The compiler hands the adapter the ordered lists — the stable blocks and the conversation blocks —
and the position in this request where the previous request ended; the adapter builds the native
system blocks from them (ADR-0027 D4) and turns the boundaries into its provider's marks. The
adapter must accept an ordered list of system blocks and mark the end of each segment (D4); the
compiler does not emit the native system field. For Anthropic:

- one mark with a one-hour lifetime at the end of the stable segment;
- one five-minute mark at the end of the conversation segment when it is non-empty;
- one five-minute mark on the last block of the turn segment;
- when a turn adds more than the provider's lookback, one five-minute mark at that recorded position,
  on the previous request's last block, so the next read still finds the previous write.

At most four marks; one-hour marks precede five-minute marks. A mark is placed whatever the length of
its prefix: one shorter than the model's minimum cacheable length is not cached, and the adapter
holds no exact token count to compare with the minimum. The lifetimes are settings of the model
configuration, with these as defaults.

The option is `prompt_caching` (bool, default False), a field of the endpoint configuration. The
adapter reads it from that configuration before the request-option filter runs, so a filtered
endpoint cannot drop it, and it is never written into the request body. With the option off every
provider's compiled request is byte-identical to today's on every path, the adapter's
`cache_control`-triggered last-message mark (`providers/anthropic/messages.py:1152-1178`) included
wherever it fires today. The mark code only runs when the option is on and the provider declares that
it accepts a native system block and caches by prefix (D3); marks come only from this option. With
the option on and the declaration present, this decision's placement replaces that one
`cache_control`-triggered mark: the placement is the mark the stable, conversation and turn segments
receive, and the adapter's own last-message mark is not placed on top of it. The existing
`cache_control` flag keeps its other current meaning, the local response memo in `Endpoint.call`.

### D6: accounting beside input

Each model call records, side by side: uncached input tokens, cache-read tokens, cache-write tokens
per lifetime, and output tokens. The write counts are the provider's own per-lifetime fields. As
lionagi's extractor reads them (`session/signal.py:317-323`), those are `cache_read_input_tokens`
and `cache_creation_input_tokens`, the total cache-creation count, so the extractor drops the
per-lifetime split. This record extends it to carry both, in the per-lifetime shape the provider
reports and lion1's backend already reads: `ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`
under `cache_creation` (`lion1/hub/backend/anthropic.py:498-501`). The run record sums each field per
conversation and counts segment changes, including a history rewrite (D1). Billing consumers
(ADR-0126) price from these fields. These fields land with the first merged change under this record,
before any caching default changes.

### D7: acceptance

Six tests, all written to pass after this record's change and its first implementation.

The first drives the API chat path on a scripted endpoint for several rounds with the option on and
the declaration present, with a system message, tools and a first-turn context provider, and
requires: each request's messages are a strict prefix of the next; the tool section appears once in
each request, in the system block; the system text is not in any user message; the context block is
present in every request of the conversation. The second builds the Anthropic payload for the last
request and requires the stable and conversation blocks in `system`, at most four marks and one-hour
marks before five-minute ones. The third runs the same rounds with the prompt-caching option off, on
both an endpoint whose request-option filter is present and one whose `request_options` is None, and
requires the compiled request to be byte-identical to today's on both paths, the adapter's mark
included wherever it fires today. The fourth runs a `ReAct` conversation whose final turn changes the
response format, and requires the tool section's bytes to be identical in every request of the
conversation, the final call included, so the changed format lands in the turn segment and never
rewrites the stable segment. The fifth and sixth run one call per path, filtered and unfiltered, with
the option on, and require the key to be absent from the request body in both.

The P1 to P4 measurements in Context are a separate characterization, taken at commit 54176709 by a
probe that drives the same rounds against the code before this record; they are not these tests.

## Consequences

- Conversations on prefix-caching providers pay the write price for the stable and conversation
  segments once per entry lifetime (1.25 times input for a five-minute entry, 2 times for a one-hour
  entry), the write price for each turn's new content once, and the read price (0.1 times input, less
  on the newest models) for the rest. A conversation that changes its tool set mid-conversation pays a
  new write, visibly.
- The compiled request for those providers changes shape: a system block holding the tool section,
  the conversation blocks after it, every response section inside the instruction that asks for it,
  context blocks kept for the conversation, and marks from the caching option. Every other provider
  sees no change, and while the option is off every provider sees byte-identical requests.
- ADR-0007's compiler, when built, inherits D1 to D5 for these providers; until then they land in the
  current `_prepare_run_kwargs` path, code that compiler will replace, which ADR-0007 D6 expects.
- `cache_control` and prompt caching become two options; code that set `cache_control=True` keeps
  getting the memo it got, while a provider cache mark now comes only from the caching option.
- Reversal: the capability declaration and the caching option are off switches; D6's fields are
  additive.

## Alternatives considered

- **Wait for ADR-0007's compiler and build caching on it.** One path and no interim code; it lost
  because conversations on per-token providers pay uncached prices on every call until that compiler
  ships, and the rules here carry over to it unchanged.
- **A separate agent loop for API providers outside `Branch`.** Fastest to write; it lost because it
  is a second request compiler beside ADR-0007's, which ADR-0007 D5 rules out.
- **The provider's automatic caching (one top-level mark).** One mark moves with the tail and covers
  the whole prefix with one lifetime; it lost because the stable segment would be rewritten at the
  five-minute price whenever the tail's entry lapses, and P1 to P3 still break the prefix.
- **Keep the fold and mark the first user message.** Fewer changes; it lost because the fold mixes
  the stable segment with the first turn's content and P3's context block, so its lifetime is the
  shortest of the three, and P1 still breaks the prefix after it.
- **Render the tool section in every historical instruction.** Restores the prefix with no new
  placement for the tools; it lost on input growth (D2), since the whole tool set repeats once per
  turn.
- **Drop context blocks after the first turn, as today.** Smaller later requests; it lost because
  dropping them changes the earliest message of the request and so voids the whole cached prefix.

## Notes

- Provider facts are from Anthropic's prompt-caching and pricing documentation as read on
  2026-10-16; the per-model read prices and the minimum-length behaviour were corrected from the
  same pages read on 2026-10-17; other providers that cache by prefix declare their own marks and
  lifetimes.
- The measurements in Context were taken at commit 54176709 by the probe described there. They are a
  characterization of the code before this record, not a D7 acceptance test; the D7 tests are written
  to pass after the change.
