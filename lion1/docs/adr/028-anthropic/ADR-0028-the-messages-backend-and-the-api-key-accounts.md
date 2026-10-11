---
adr: ADR-0028
status: proposed
liveness: partial (the backend and its price table run under tests with a stubbed endpoint and ran on one key for the measurements of S15 and S17; the accounts, the ledger, the ceremony and the bench are owed)
date: "2026-10-09"
area: anthropic
kind: new
depends_on:
  - "[[ADR-0009-backends|ADR-0009]]"
  - "[[ADR-0012-the-bench|ADR-0012]]"
  - "[[ADR-0016-the-lion-command|ADR-0016]]"
  - "[[ADR-0024-accounts-and-the-pick|ADR-0024]]"
tags:
  - adr
  - runtime
  - backend
  - accounts
---

# ADR-0028: The Messages backend and the API-key accounts

## Context

This record serves the author's requests of 2026-10-09:

- 10:47: support the Anthropic Messages endpoint and take six API keys, one for each of six
  accounts, then use their credit up and make good use of it. The same line names Haiku on the mini
  SWE bench and iterating the source.
- 10:49: weigh cache use carefully, and look at how Claude Code does it
- 11:07: use the standard Messages API endpoint, on the API credit the accounts carry, not extra
  usage inside Claude Code

Six Console accounts each carry a month's API credit. Each is its own organization: six keys
answered with six rate-limit sets and reset clocks, so six prompt caches.

The router backend ([[ADR-0009-backends|ADR-0009]]) sends the view as messages and lets the provider
cache. This record adds the provider's own endpoint as a second chat-completion shape, with the
cache placed by the backend. It amends [[ADR-0024-accounts-and-the-pick|ADR-0024]] with accounts
that have no windows but a month's budget. Source: `hub/backend/openrouter.py`, `hub/accounts.py`,
`apps/cli/lion_cli/account.py`, `bench/swe/run_swe.py`; the provider's prompt-caching and pricing
pages read 2026-10-09; the installed Claude Code 2.1.295 bundle's placement, extracted and kept in
the user's workspace.

## Definitions

- **breakpoint**: a `cache_control` mark on one content block; the provider caches the prompt up to
  it, at most four per request.
- **tail**: the last block of the last message of a call.
- **pin**: the breakpoint kept on the previous call's tail, so its entry is found whatever the
  lookback.
- **lookback**: the twenty positions before a breakpoint the provider checks for an entry.
- **tier**: the price band a Haiku request falls in by its whole prompt length, 100,000 tokens the
  line.
- **API-key account**: an account of provider `anthropic`: a Console key in a keychain item, its own
  organization, cache and limits.
- **month ledger**: per API-key account, the calendar month's spend summed from envelopes, with its
  calls and the calls of unknown cost.
- **pace**: an account's spend against the share of its budget the month has elapsed.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | A breakpoint on the head and one on the tail, moved each call, hit the previous prompt only while the tail is within the lookback of the previous breakpoint. | measured 2026-10-09 on Haiku 5.5: grown by one turn, read 919 and wrote 10; the tail 25 blocks past the last write, read the head's 912 only and wrote 187 | the pin is a breakpoint spent for nothing |
| A2 | A 1h entry must come before every 5m entry in a request. | measured 2026-10-09: the reverse order is a 400 naming the rule | one TTL per call makes the order moot |
| A3 | The credit is spent at the published prices, per request, and a Haiku request is priced by its whole prompt, cache reads included. | the pricing page read 2026-10-09; the Console's own bill not read | the month ledger drifts from the bill; the Console's figure corrects it by hand (S6) |
| A4 | Caches and limits are per organization, and the six keys are six organizations. | the prompt-caching page; six rate-limit sets with their own reset clocks measured 2026-10-09 | two runs on two keys share one cache and one limit; the spread gains less, nothing breaks |
| A5 | The credit expires at the month's end; a fresh one is not promised, and the provider's policy moves. | the author's word on the month | the pace targets the wrong end; `month_start` moves it (D3); a credit that does not return is spent, not paced |

## Claims

### C1: The backend sends the view in the provider's shape and places the cache itself _(enforced: mechanical; owed, S1)_ ^c1

- **Subject**: every call of the Messages backend.
- **Violated when**: a notification is sent under the system role; a call has more than four
  breakpoints or a 5m entry before a 1h one; a call sends tools, images or a thinking setting not
  the run's; or a native tool-use block is executed.

The head is the request's `system`, one text block with a breakpoint. Every other message is one
text block under `user` or `assistant`; a notification goes as `user`, the router's rule. Two more
breakpoints: the pin, when the previous call's tail is still in the view, and the tail. One spare.

One TTL per backend, on every breakpoint, so A2 holds by construction: 5m by default, 1h for an
agent whose wakes are spaced ([[ADR-0024-accounts-and-the-pick#^d1|ADR-0024/D1]] `keep`). Thinking
is an effort fixed for a run (S17). A `tool_use` block in the answer is the tag text or refuses the
turn ([[ADR-0004-the-language#^c4|ADR-0004/C4]]); thinking blocks are not kept.

### C2: The count and the usage are the provider's figures, the cost is the dated table's, and a spend neither can price is unknown _(enforced: mechanical; owed, S1)_ ^c2

- **Subject**: every call's `Reply` and envelope.
- **Violated when**: the prompt count is not the answering attempt's fresh, read and written input
  together; an unreported count reads as zero; or a 401 or 403 is retried. A bill missing a count,
  or whose split does not sum to its write, read as whole, an unpriced model's cost, an unsplit
  write priced off its request's TTL, and a failed call landing no envelope violate it too.

The count is [[ADR-0009-backends#^c2|ADR-0009/C2]]'s, none with one of its three unknown. The
envelope keeps the usage in the router's shape, each count summed over attempts reporting it, the
split, limits, request id, tier, account.

Each attempt is priced at its model's tier from its four counts, an unsplit write at the request's
TTL (C1); the sum is the known subtotal, whole or not. A refusal before any billed request, here or
the provider's, is a complete zero ([[ADR-0009-backends#^c5|ADR-0009/C5]]); retries are D1's.

### C3: An API-key account is one organization's key, read by the runtime only for the request header and imported by a ceremony that keeps no copy _(enforced: mechanical; owed, S1)_ ^c3

- **Subject**: every API-key account, its import, and every run on one.
- **Violated when**: the runtime reads the key outside the request header; the pool, any output, an
  error or a template holds it or eight of its characters in a row past its public start
  ([[ADR-0009-backends|ADR-0009]] S32, S34). So do a run's calls crossing accounts, an import
  pre-filling a stored key, and two pool entries naming one item.

The item's service is `ANTHROPIC_API_KEY` and its account the pool's name. `lion account add` takes
one key on standard input, and `lion account keys` several through the person's editor (D4): a blank
template, every line checked before any write, each item read back, the file overwritten and
removed. What the editor kept of its own is outside the runtime's reach and is said, never certified
gone (S12). Every rule of [[ADR-0024-accounts-and-the-pick#^c1|ADR-0024/C1]] holds. A run stays on
its pick's account for its whole life, since the cache is the organization's (A4).

### C4: A call on an API-key account is admitted against its month ledger, every call settles once, and a new run goes to the account furthest behind pace _(enforced: mechanical; owed, S1)_ ^c4

- **Subject**: every call on an API-key account, its settlement, and every assignment.
- **Violated when**: a request is sent at the budget or before its attempt is on the ledger; an
  attempt settles twice, or an unanswered one reads as free; or a refused call moves to another
  account. A ledger figure not a cost, a ceiling or a sourced adjustment violates it too.

The ledger is per attempt, a retry its own row. Before each attempt the backend writes it under the
lock with a ceiling (D3) and refuses when settled cost plus open ceilings reach the budget. It
settles once by its id, cost replacing ceiling; unanswered, it keeps its ceiling until an adjustment
settles it (S6).

One rule assigns wakes and bench instances: lowest spend over budget, then fewest runs in flight,
then name; `keep` is a wake's exception. A 401 or 403 marks the account for its person; else the run
ends unmarked (S13).

## Decisions

### D1: The backend in `hub/backend/anthropic.py` ^d1

Serves C1 and C2. `Anthropic(model, timeout, max_tokens, temperature, stop, effort, ttl, retries,
account, calls)`, the router's loop, posting to the endpoint with `x-api-key`. It keeps the last
tail by name and marks it while the view holds it. `account` names the pool's account; none reads
`ANTHROPIC_API_KEY` from the environment. The answer's text blocks are joined; `prompt_tokens` is
the three input counts summed.

A 429 waits its `Retry-After` within a bound, else backs off, as a 5xx, a 529 and a transport
failure do; a 401 or 403 is not retried.

- **Landing evidence**: `tests/test_anthropic.py`:
  `test_the_head_is_system_with_a_breakpoint_and_the_tail_is_marked`,
  `test_the_previous_calls_tail_is_pinned_while_the_view_holds_it_and_never_more_than_three_marks`,
  `test_an_empty_entry_is_sent_as_a_placeholder_block_and_a_view_without_a_message_is_refused`,
  `test_a_429_waits_its_retry_after_and_a_5xx_backs_off_but_a_401_is_not_retried`,
  `test_a_refusal_before_any_billed_attempt_lands_an_envelope_with_a_complete_zero`,
  `test_a_view_refused_before_any_request_lands_an_envelope_with_zero_attempts`,
  `test_a_call_that_fails_after_a_billed_attempt_keeps_the_bill_with_its_account`,
  `test_a_tool_use_block_alone_is_the_turn_as_text_and_beside_text_refuses_the_turn`,
  `test_a_turn_cut_at_the_output_cap_reads_length_and_thinking_blocks_are_not_kept`,
  `test_a_blank_answer_is_sampled_again_then_handed_over_blank_with_its_content_kept`,
  `test_a_body_cut_mid_read_keeps_its_own_headers_on_the_failed_envelope`,
  `test_a_refusal_whose_body_is_cut_mid_read_is_still_that_refusal_with_its_headers`,
  `test_the_limits_and_request_id_ride_the_envelope`,
  `test_the_key_comes_from_the_environment_without_an_account_and_from_the_item_with_one`,
  `test_the_key_rides_only_the_request_header_and_is_in_no_envelope_or_error`.

### D2: The price table ^d2

Serves C2. A dated table in the backend's module: per model, base input, 5m write, 1h write, cache
read and output per million tokens, and for Haiku 5.5 both tiers. Cost per request is the four usage
counts at the request's tier. A model not in the table has no cost, flagged as the router flags a
missing attempt; on a budgeted account the call is refused before it is sent.

- **Landing evidence**: `tests/test_anthropic.py`:
  `test_the_count_is_the_answering_attempts_three_input_counts_and_usage_sums_every_attempt`,
  `test_cost_is_the_dated_table_at_the_requests_tier_and_unknown_for_a_model_it_lacks`,
  `test_a_response_without_usage_has_no_count_and_no_cost`,
  `test_a_count_the_response_lacks_is_unknown_and_a_bill_with_one_is_incomplete`,
  `test_a_write_the_response_does_not_split_is_priced_at_the_requests_ttl`.

### D3: API-key accounts in the pool and the evidence file ^d3

Serves C3 and C4. [[ADR-0024-accounts-and-the-pick#^d1|ADR-0024/D1]] gains provider `anthropic`,
credential `keychain`, and `budget`, dollars per month; `[pick]` gains `month_start`, the reset day.
[[ADR-0024-accounts-and-the-pick#^d4|ADR-0024/D4]]'s file keeps the month ledger per account:
attempts by id, open with a ceiling or settled with cost and request id, and sourced adjustments.
The ceiling is the body's bytes at the upper tier's 1h write rate plus `max_tokens` at its output
price (S15); one settled over its ceiling is flagged (S16).
[[ADR-0024-accounts-and-the-pick#^d5|ADR-0024/D5]] assigns these accounts by C4 and probes none;
[[ADR-0024-accounts-and-the-pick#^d6|ADR-0024/D6]] gains no class until S13 lands.

- **Landing evidence**: none yet; owed with the code (S1).

### D4: `lion account keys` and the pace column ^d4

Serves C3 and C4. `keys` writes a blank template, mode 600 in a private temporary directory, one
line `<name>=` per API-key account and never a stored key, opens `$VISUAL`, else `$EDITOR`, and
waits. Every line is checked before any write: a name not in the pool or named twice, or a value
outside the key's form, refuses the file by name. Each key goes to its item on the keychain tool's
standard input and is read back ([[ADR-0024-accounts-and-the-pick#^d9|ADR-0024/D9]]); the file is
overwritten and removed. `list` shows spend, budget and pace.

- **Landing evidence**: none yet; owed with the code (S1).

### D5: The bench on API-key accounts ^d5

Serves C2 and C4. `--backend anthropic` with `--accounts a,b,...` assigns each instance one account
at launch by C4's rule, stamped on its row and manifest; the cost cap applies, the arm being
metered. On Haiku 5.5 the view budget defaults to 75,000 tokens: a heuristic from the count's fit
([[ADR-0006-the-notification|ADR-0006]] A3), not a bound, since the head is outside the estimate and
one oversized result crosses the line. Each row counts its calls over the tier and the summary
totals them. The row's tokens carry the cache split.

- **Landing evidence**: none yet; owed with the code (S1).

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Automatic caching, the top-level `cache_control`, alone | its breakpoint sits on the tail and no pin is possible, so a turn adding more than twenty blocks loses the message prefix (A1); explicit marks cost nothing |
| One 1h TTL everywhere | a 1h write is 2x base against 1.25x: it wins only where a 5m entry would have lapsed and loses the premium where it would not; the bench's turns come within minutes |
| Settling the ledger at a run's end | a running or crashed run's spend is invisible to every other account's admission; per-call settlement under the lock is measured before it is optimized |
| Merging a turn's results into one message so a turn adds two blocks | changes the view every backend sees; the pin keeps the view and the hit |
| Reading spend from the provider's usage endpoint | it takes an administrator's key per organization, six more secrets; the envelopes are already the spend row ([[ADR-0016-the-lion-command|ADR-0016]]) |
| Spending the credit through the Claude Code CLI with the key set | the author's 11:07 line: the credit is for the endpoint, not the CLI; and the CLI's fixed context is 34k tokens a call ([[ADR-0009-backends|ADR-0009]] A3) |
| Picking an account per call, spreading faster | each change loses the organization's cache (A4), the reason [[ADR-0024-accounts-and-the-pick|ADR-0024]] picks once per wake |
| Moving a refused call to another account | the lost call's effects are unknown ([[ADR-0024-accounts-and-the-pick#^c7|ADR-0024/C7]]) |
| Keys in the environment or a dotfile | [[ADR-0024-accounts-and-the-pick#^c1|ADR-0024/C1]] |

## Consequences

- **S1**: Nothing is built. Each decision's landing evidence names its tests when it lands, and
  every claim marked owed names this row.
- **S2**: [[ADR-0009-backends|ADR-0009]] is amended with the code: a fourth shape, its claims C1, C2
  and C5 holding for it, and a decision pointing here.
- **S3**: [[ADR-0024-accounts-and-the-pick|ADR-0024]] is amended with the code as D3 says, and its
  S3 row is discharged by this record.
- **S4**: [[ADR-0016-the-lion-command|ADR-0016]] is amended with the code: the `keys` form in its
  table and the pace column in `list`.
- **S5**: [[ADR-0012-the-bench|ADR-0012]] is amended with the code: a metered arm on the endpoint,
  `--accounts`, and the account on the row and the manifest.
- **S6**: The month ledger is the runtime's own sum. The Console's bill is the truth and is not
  read. A drift, and an attempt left open by a death, are entered as an adjustment with its source
  and date, never by editing a row; an attempt the table or the provider could not price holds its
  ceiling, never zero.
- **S7**: Using the credit up is a schedule of work, not a property of the backend. The pace column
  says which account is behind; what to run on it is the author's call.
- **S8**: At Haiku 5.5's lower tier, our own ledgers reprice to about $0.73 for a Mini-50 bench run
  and $2.36 for Hard-45; Sonnet 5.5 is twenty times that. A request over the tier's line is priced
  five times higher, and the envelope says which tier it paid.
- **S9**: The cache's life runs from the start of the request that wrote or read it; a turn whose
  commands run past five minutes rewrites the prompt at 1.25x. The 1h TTL is the remedy where the
  gap is the rule and only a premium where it is not: the choice is per backend and measured.
- **S10**: The pin is the Claude Code bundle's own placement, read from its 2.1.295 release: the
  system prompt, the last cacheable message, and the previous turn's end. An entry's name surviving
  a fold is not an unchanged prefix, so a folded stand-in is never pinned, and a rewrite before the
  pin rewrites everything after it.
- **S11**: Six organizations are six caches and six limits: the bench spreads instances across them,
  and an agent spreads wakes by `keep`. Within one run nothing spreads.
- **S12**: The import's file is wiped; what the editor kept of its own, a backup or a swap file, is
  outside the runtime's reach, and `keys` says so. A person who wants no residue uses `add` on
  standard input.
- **S13**: No response naming an exhausted credit has been measured. Until one is, kept as a fixture
  with its status, type and body, no refusal marks an account out of quota, and the month's reset is
  the author's word (A5).
- **S14**: The router landed no envelope on a call refused before any billed attempt; this backend
  does, and [[ADR-0009-backends#^c5|ADR-0009/C5]] now holds the router to the same rule.
- **S15**: The counted prompt is bounded by the body's bytes as measured, not as proved: through the
  backend on Haiku, Sonnet and Opus 5.5, six shapes each, thinking off, the same counts on all
  three, at most 0.66 of the bytes. The expectation behind it: a token is at least one byte of
  content, and a block's JSON framing is more bytes than the provider's framing tokens for it. The
  provider's free `count_tokens` equalled the billed prompt on every attempt measured: the exact
  instrument if S16's flag fires.
- **S16**: An attempt whose settled prompt exceeds its ceiling's token figure is flagged on its row,
  and the pace column says so; the overshoot is bounded by the attempts in flight, since each
  settles to the provider's count at its response. The flag firing amends this record: the count
  before dispatch, or a wider bound.
- **S17**: The current models refuse `thinking.type.enabled` with a budget: a 400 naming
  `thinking.type.adaptive` and `output_config.effort`, measured 2026-10-09 on all three. The
  backend's thinking setting is an effort (low, medium, high) sent as adaptive thinking; none sends
  no setting. The thinking block arrives as content and is not kept; its tokens ride
  `reasoning_output_tokens`.
