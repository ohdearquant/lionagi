# ADR-0126: Provider credential routing and monthly account budgets

- **Status**: Accepted, amended 2026-10-14, 2026-10-26, 2026-10-28 and 2026-10-30 (see Amendments 1 to 6)
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: service-providers
- **Date**: 2026-10-12
- **Relations**: extends ADR-0027; follows ADR-0119 and ADR-0125 D1 and D8 (carries lion1 ADR-0024 and lion1 ADR-0028 by concept); keeps ADR-0029 D4's retry classes; preserves the boundaries of ADR-0028, ADR-0030 and ADR-0043

## Amendment 1 (2026-10-14) — what a stream's mask can tell from other text

D2 requires stream chunks scrubbed of the chosen key, including a key split across chunks, with
masks that reveal no key characters. A masker cannot tell a few characters of the key from other
text, so the stream path matches runs: a character is masked when it lies in a run of eight or more
characters that occurs anywhere in the key, in one of the joins the stream path makes of one
message's text. The joins are each delta kind across the message, the quoted text of its citations,
the text of the chunks a consumer receives, by chunk type and all of it in order, and every piece in
order. Every other string value of an event is matched on its own.

Decision: D2's scrubbing is met, in those joins, for every run of eight or more characters of the
key (the whole key, for a shorter key) and for every beginning of the key longer than its public
`sk-ant-` prefix. Five limits follow and are accepted: closing the first two would mean masking
ordinary text that shares a few characters with the key, or holding text until the stream ends, and
closing the fourth would mean a join for every set of chunk types a consumer might join.

- A key whose runs of fewer than eight characters have other characters between them, such as the
  key written in seven-character pieces separated by spaces, within one event or across events, is
  sent in those pieces.
- A join's held run of fewer than eight characters is sent at a pause, an event that carries no
  text into that join; if the key continues after the pause, the rest of the run is masked. A
  beginning of the key is masked once it is longer than `sk-ant-`, which every Anthropic key starts
  with, so those seven characters can appear before its mask; a key that does not start with
  `sk-ant-` has its first character masked wherever it appears.
- Text that is not the key is masked where it holds a run of eight characters from the key, or a
  beginning of the key past `sk-ant-` such as `sk-ant-api03-`.
- A consumer that joins the text of some chunk types and not others, such as text and thinking
  without the error chunk between them, makes a join the stream path does not match, so a run of
  the key split across that join's parts can be sent.
- An event's object names and numbers are matched for runs as their text, like every other string
  value; the chunk a consumer receives masks a whole key wherever one lies in the text it shows.

A response that is not streamed reaches its caller only with status 200 and only as parsed JSON;
every whole key in it is masked, the text, thinking and quoted text of its content are matched for
runs as joins of each kind and all in order (a bare string item counts as text), and every other
string, a dict name and a number alike, is matched for runs on its own, as its text. A run split
between one of the joined strings and any other string, such as a tool input value, a redacted
block, a citation title or a signature, is therefore matched in each string on its own and left
unmasked. Unlike a stream it does not mask a beginning of the key shorter than eight characters,
since it can read the whole text first. A routed call that asks for another response mode is
refused with `unsupported_pricing` before it is sent.

## Amendment 2 (2026-10-26) — a routed key is not filled into CLI children

D2's third source is the declared secret lookup, so a machine that keeps account keys in a store
declares each account's `key_env` in `secrets.lookup.names`. The same declaration also drives the
fill that runs on every CLI spawn (`fill_declared_secrets`, `lionagi/providers/_secret_resolution.py`),
which looks up each declared name missing from the child's environment and adds it. Without a rule,
every CLI spawn would run one lookup per routed account, and every CLI child's environment would
hold every account's key, readable by anything the child runs and outliving the request any one key
was meant for. The output redactor masks those values in what lionagi captures; it cannot stop a
child from reading its own environment. A CLI that reads a name such as `ANTHROPIC_API_KEY` would
also authenticate with an account key instead of its own credential.

Decision: the CLI fill does not look up a name that is the `key_env` of a configured account. The
key is read only by D2's resolution, for the attempt it signs. The name stays among the declared
names the spawn path redacts, and a value a child already inherits under it is left as it was:
the rule withholds the fill, it does not scrub the environment the caller hands the child.

## Amendment 3 (2026-10-26) — accounts declared in the user-wide file, and months that follow the credit refresh

**Where accounts are declared.** Amendment 2's rule keys on the configured accounts, so it holds only
in a process that sees the declaration. D1 named the process environment and the working directory's
dotenv files. A process started anywhere else, such as an MCP server or a scheduled run whose working
directory holds none of those files, would see no accounts, and its CLI fill would look up every
declared name, each account's key included. AppSettings also reads the user-wide file
`$LIONAGI_HOME/.env` (`~/.lionagi/.env` by default) as its lowest file source (Context P1). Decision:
`ANTHROPIC_ACCOUNTS` is declared there. Every lionagi process under the same `LIONAGI_HOME` then sees
the same accounts, and a working-directory file or the environment still replaces the declaration, as
D1 allows. A process under another `LIONAGI_HOME`, such as the test suite's, sees no declaration and
takes the legacy path: its CLI fill looks up every declared name, as before this ADR.

**When a month starts.** This ADR takes the provider's monthly credit to refresh at 00:00 UTC on
the last day of each calendar month, and credit left unspent at the refresh lapses. D5 keyed an
attempt by the calendar month in UTC, which starts a day later. On each month's last day the ledger therefore
charged spend to a month whose credit had already lapsed: an account that had spent that month's
budget was refused for the day although its new credit was available, on the one day spend is
pushed before a refresh. In the other direction, each calendar month also held the next credit
period's first day, so within one credit period the ledger could admit up to a day's spend past the
budget, which only the provider's own refusal (D6) ends.

Decision: a ledger month is a credit period. The period keyed `YYYY-MM` runs from 00:00 UTC on the
previous calendar month's last day to 00:00 UTC on that month's own last day, so an admission at
instant `t` takes the key of `t` plus one day in UTC: `2026-10-30T23:59Z` is in `2026-10` and
`2026-10-31T00:00Z` in `2026-11`. The rule applies to new admissions; a recorded attempt keeps its
key. At any instant the new key is never earlier than the calendar key, so D5's clock-regression
check does not refuse a ledger that holds rows keyed both ways. D5's other month rules are
unchanged, with its midnight read as the start of the credit period.

## Amendment 4 (2026-10-28) — the account a launcher chose is named in run.json

D1 to D6 route a key inside the process: the router chooses a configured account for each
attempt, and the run's API usage record names it (`account_labels`, written by
`APIUsageCollector.snapshot()` in `lionagi/cli/_api_usage.py`). A launcher can also choose the key
outside the process and hand the child only the provider's own variable, such as
`ANTHROPIC_API_KEY`. That child routes nothing, so its run records no account, and spend per
account cannot be counted from the runs.

Decision: a launcher that chose the key may name its account in `LIONAGI_API_ACCOUNT`, read from
the process environment only (a dotenv entry for it is not read), and `li agent` records the name
in run.json as `api_account` (the run manifest `_run_agent()` writes, in `lionagi/cli/agent.py`).
The value must be an account name under D1's rule for routed accounts (`_ACCOUNT_NAME_RE` in
`lionagi/config.py`): a lowercase letter, then up to 31 lowercase letters, digits, `_` or `-`, and
not containing `sk-ant` (`_looks_like_a_key()`, the same file). It must also not start with `sk-`,
which D1's rule admits, so this field is stricter than a configured account's name. It must not
equal a key the process holds: the value of a variable whose name marks a credential
(`is_secret_field_name()` in `lionagi/libs/credential_fields.py`) or is a configured account's
`key_env` (D1), whether that variable is set in the environment or in the dotenv files the settings
were built from. A dotenv file that cannot be read for that comparison refuses too. Every refusal
raises `ConfigurationError` before the run writes anything (`_read_api_account()`, called by
`_run_agent()` before the run is allocated); the error names the rule, or the variable whose value
matched, and never repeats the value. Unset, the run records no `api_account`, as before.

What the check guarantees: a value with any character outside the name rule (an upper-case letter,
`.`, `@`, a space), a value containing `sk-ant` or starting `sk-`, and the whole value of any key
the process holds under a credential-named or configured-account variable, in its environment or
its dotenv files, never reach run.json, so within those sources D2's bar on serializing key
material holds. The comparison goes by the variable's name, a credential marker anywhere in it, not
by its use, so a name equal to the value of a variable such as `MAX_TOKENS` is refused too, and the
refusal names that variable. What it cannot do: it does not run the secret lookup, D2's third
source, which runs only for a routed attempt; it compares whole values, so a value holding only
part of a held key passes, where the router drops a label holding a run of eight or more characters
of the attempt's own key; and a key made only of characters the name rule allows, such as lowercase
hex after a letter, passes when the process does not hold it under such a name, because no check on
a string's shape tells such a key from a name. The field is meant for names a launcher takes from
its own account list.

Not covered: the name is not checked against the configured accounts (D1), selects no key, and is
not budgeted by D4's ledger. For routed calls `account_labels` stays the record.

Verification: `tests/cli/test_agent_api_account_record.py` pins the recorded name, one of 32
characters and one beside the `sk-ant` marker (`ask-an1`) included, and its absence when the
variable is unset. It pins the refusal before any run.json of a key, an `sk-` prefix, `sk-ant`
inside and at the end of a name, a mixed-case value, a capital, a `.`, an `@`, a digit, `-` or `_`
first, a value of 33 characters, a space and an empty value; of a value held under a
credential-named and under a configured-account variable, each in the environment and in a dotenv
file; and of a name when a dotenv file cannot be read, with the value never shown in the error.
Its controls record the name when a credential variable holds a different value, and when a
variable or a dotenv entry that is neither holds the same one.

## Amendment 5 (2026-10-30) — a new conversation goes where its stable prefix is warm

Purpose: raise the prompt-cache hit rate of a process that starts many short conversations sharing one system prompt; the provider's usage report for such a workload showed a hit rate of at most 28% (2026-10-27).

D3 binds each conversation to one account for its whole life, because the Claude API keeps a
separate prompt cache for each organization (P3). D3 orders a NEW scope's candidates by utilization
alone (`_ratio_order`, `lionagi/service/accounts/_ledger.py:400`, applied at `:548`), without regard
to cache warmth. A process that starts many short conversations sharing one system prompt therefore
lands each new conversation on whichever account is furthest behind its budget pace. The first
request of that conversation then writes the stable prefix again on that account, even when the
same prefix was read on another account seconds earlier.

Measured on one long-running service that starts a new Branch for each unit of work and shares a few
system prompts across them. The records are its own request log; the account a call used is the
cache record's `route`, the last of the call's attempts
(`lionagi/service/_prompt_cache.py:447-449`). The prefix is identified by the certificate of the
system block the placement marked for one hour, as each record logged it. Recomputed from each
logged request body with `points()`, the certificate of the last tools or system block equals it on
all 293, 447 and 1,353 logged requests that carry a system prompt, so on this service the one-hour
mark sits on the last stable block and Decision 1's key is the logged one. The call totals and
the cost ratios below cover every logged call with usage (333, 510 and 1,551). The write census
covers only the 293, 447 and 1,353 records that carry a system prompt and a system-tier one-hour
certificate. A record whose placement has no `landing` is a new scope's first request, and a read
counts as a sighting of the prefix only at 1,000 tokens or more.

- With one configured account: 333 calls, and no one-hour write of a prefix seen within the hour.
- With seven configured accounts, of 510 and 1,551 calls: 54 and 59 of the records with a
  certificate made a one-hour write, every one on the first request of a new scope.
  - 46 and 55 of them wrote a prefix read or written less than an hour earlier on another account.
    The other 8 and 4 wrote a prefix not seen within the hour.
  - None wrote on an account where the prefix had been read or written within the hour.
- Cost, in base-input equivalents: serving the 101 writes as reads would cut the two processes'
  input cost by 28% and 13%. The cached-to-uncached cost ratio is 0.516 and 0.416, against 0.355
  with one account.
- Not all 101 are movable by this amendment. A row is written when the first scope's request
  settles (Decision 3), so a second new scope that reserves before then sees no row. Counting a
  write as a concurrent cold start when a same-prefix one-hour write, or a read of at least 1,000
  tokens, on another account came less than 30 seconds earlier (an upper bound: reads count as
  events), 5 and 17 are. Without the other 41 and 38 the ratio is 0.388 and 0.377; without all of
  them, 0.373 and 0.360.
- An idealized replay of the new-scope requests (396 and 1,196 of them, 9 and 4 distinct prefixes)
  gives the scale of what affinity can recover: 9 and 9 one-hour writes, against the 54 and 59
  new-scope one-hour writes measured. It holds every warm account admissible, makes a row visible
  30 seconds after the request that writes it, extends the row on every new-scope call that arrives
  while the row is visible, whatever that call read, and counts every new scope that finds no row as
  a write. Three of these err toward fewer writes than this amendment would produce: a real pool
  can refuse a warm account the replay admits; the extension is the any-read refresh the
  Alternatives reject, which keeps rows alive on calls Decision 3's read arm does not count; and a
  refresh takes effect at once, with no settle delay. Two err toward more: it models no account
  pool, so a scope it counts as a write can land on an account where the prefix is warm, and a
  concurrent cold start's write does not extend the row. Its figures are a scale, not a bound on a
  real run, and they do not rank refresh rules against each other.

The per-Branch prompt-cache rows (ADR-0127, `lionagi/service/_prompt_cache.py`) cannot carry this
choice. A new Branch has no rows of its own, and the choice must be read inside D4's reservation
transaction, across cooperating processes. The evidence has to live where every new scope's
selection reads it: the D4 ledger.

### Decision

1. **The key.** A request's stable-prefix certificate is the certificate, as `points()` computes it
   (`lionagi/providers/anthropic/_prompt_cache.py`), of the last markable tools-tier or system-tier
   block (`Point.markable`, `_prompt_cache.py:138-142`: not blank text, not thinking) that carries a
   one-hour mark (`cache_control` with `"ttl": "1h"`) in the body that is sent. It
   names the longest one-hour entry the tools and system tiers of that body can hold.
   - With the adapter's own placement that block is normally the stable one. The adapter writes
     the system tier as blocks (`lionagi/providers/anthropic/messages.py:383-384`) and `place()`
     puts the stable one-hour mark on the last markable block outside the messages (`stable`,
     `_prompt_cache.py:257`, marked at `:305-315`).
   - An earlier one-hour mark in those tiers (a caller's own, or the placement's read or probe of
     an older one-hour entry, `:298`, `:322`) ends a shorter entry. When the stable block also carries a one-hour mark,
     the stable block is the key. When the stable block's mark is refused or kept at five minutes
     (`ttl_order:stable`, `:309-312`) or there is no mark left for it (`MAX_MARKS`, `:228-229`,
     `:315`), the last earlier one-hour mark is the key. That is still the entry the provider holds
     for an hour.
   - A request with no one-hour mark in its tools or system tiers has no certificate and takes no
     part in this amendment: it neither reads nor writes the index.
   - `router.begin` computes it from the final payload it freezes, the dict `request_ttls` reads
     (`lionagi/service/accounts/_router.py:123`), after parsing the frozen bytes back (next
     bullet but one). It never comes from the placement record,
     so a direct endpoint, a prebuilt event and a body a hook changed after placement are keyed by
     the body that is sent. A standalone call takes part only when that body carries a one-hour mark
     in its tools or system tiers. A string system prompt carries no mark, so such a call is keyed
     by a one-hour mark on its last tool or not at all.
   - `begin` also records `hour_after_key`: whether any block after the key block carries a
     one-hour mark at any depth: on the block itself, or on any object its content reaches through
     lists and dicts (a `tool_result`'s content, a `document` block's `source.content`, a
     `search_result`'s content). `begin` walks the parsed body for it. `Point.nested` reads one
     level of `content` only (`_prompt_cache.py:99-107`) and is not used for the flag. Who placed
     the mark does not matter: a caller, or the placement's read or probe of an older one-hour entry
     (`:298`, `:322`). It is also true when the sent body carries a top-level `cache_control` with
     `"ttl": "1h"` and a markable block follows the key block: the provider's automatic caching then
     puts a one-hour breakpoint on the last cacheable block, after the key block. When the key block
     is the last markable block, that breakpoint is at the key block and the flag stays false. The
     adapter never sends that field itself (`messages.py:1158-1160`, `:1170-1173`), so only a
     direct or prebuilt body, or a hook, carries it. Decision 3 uses the flag.
   - `begin` computes the key and `hour_after_key` from the frozen bytes parsed back
     (`json.loads`), not from the dict. `points()` reads only lists as content
     (`_prompt_cache.py:88`, `:101`), and serialization turns a tuple into the array the provider
     receives, so parsing first counts a mark that a tuple carries.
   - `points()` is unchanged.
2. **The index.** The D4 ledger file gains one table, `warm_prefix`, one row per account and
   certificate.

   ```sql
   CREATE TABLE IF NOT EXISTS warm_prefix (
       provider      TEXT NOT NULL,
       account       TEXT NOT NULL,
       cert          TEXT NOT NULL,
       stable_tokens INTEGER,        -- input tokens through the key block; NULL when unknown
       expires_at    TEXT NOT NULL,  -- UTC ISO-8601 on the ledger clock, as attempt.created_at
       PRIMARY KEY (provider, account, cert),
       FOREIGN KEY (provider, account) REFERENCES account (provider, name)
   );
   ```

   Every time the amendment reads or writes is parsed with `datetime.fromisoformat` and compared as
   an aware datetime in Python, never by SQLite date functions or by text order. The ledger's own
   values are `isoformat()` of UTC datetimes, and those sort in time order as text, zero microseconds
   included. The parse is what keeps the comparison right for any other offset a stored value can
   carry. A stored `expires_at` counts only when it is text and `fromisoformat` returns an aware
   datetime. A value that is not text (the table is not STRICT), does not parse, or parses without
   an offset makes its row absent everywhere: it is not in W, a read does not refresh it, and a
   write replaces it. Nothing refuses because of it. Python 3.10 parses only the forms
   `isoformat()` emits; 3.12 also accepts a `Z` suffix, colonless or hour-only offsets, other
   fraction lengths and basic-format dates, so such a value is absent on 3.10 only. The ledger
   writes only `isoformat()` of aware UTC datetimes.
3. **Writing a row.** Settle writes only inside its open-to-settled branch (`_settle_sync`,
   `_ledger.py:668-701`), only when the evidence is `CompleteUsage` (D5) with cost at or under the
   ceiling, only when settle was given the certificate, and only when the account is not blocked
   when that transaction reads it. The block is read in the branch where cost is at or under the
   ceiling, so no later statement in the transaction sets one. Then:
   - **Write arm.** `cache_write_1h_tokens > 0`: a one-hour entry was written on that account. The
     ledger upserts the row. When the body carries no one-hour mark after the key block,
     `stable_tokens` is `cache_read_input_tokens + cache_write_1h_tokens`. The read that precedes a
     one-hour write ends inside the stable segment, so the sum is the token count through the key
     block. That relies on the provider counting each input token once across `input_tokens`,
     `cache_creation_input_tokens` and `cache_read_input_tokens`, as its usage documentation
     states. Otherwise `stable_tokens` is NULL, because the write may include tokens past the key.
     A request with a server tool (`_server_tools`, `_prompt_cache.py:184-189`) also stores NULL:
     the provider sums usage over its internal iterations, so the sum is not a length. The
     prompt-cache logger is blind to those requests for the same reason
     (`service_prompt_cache.py:462`).
   - **Read arm.** `cache_write_1h_tokens == 0`, a row exists for the account and certificate, its
     `stable_tokens` is not NULL, and `cache_read_input_tokens == stable_tokens`: the request read
     exactly the entry that ends at the key block, because a read is a prefix of the request, and the
     only prefix of that length is the stable one. That holds when the stored count came from a
     request with the same provider prefix through the key block. Two cases break it, and both are
     disclosed under "What it guarantees, and what it does not": a certificate two differently cached
     prefixes share, and a five-minute entry at the key block's position if the provider serves the
     one-hour mark there as a read of it. The ledger refreshes that row's `expires_at`, whether or
     not the row has lapsed: a read of exactly the stable length is evidence that an entry ending
     at the key block is live. A
     read of any other length refreshes nothing. That covers a cloned Branch's deeper read, a first
     message several conversations share, and a shorter caller-marked entry. A read never creates a
     row, and a request with a server tool refreshes nothing. The arm applies to every settled
     attempt, new scope or bound.

   `expires_at` is the attempt's `created_at` plus 3,600 seconds, and the upsert keeps the later of
   the stored and the new value. The base is `created_at`, read in the same statement as the
   attempt, because the provider's hour starts when it processes the request, which is after the
   reservation: the row lapses no later than the entry would. An attempt whose `created_at` does
   not parse as an aware datetime writes and refreshes nothing. A write-arm upsert sets
   `stable_tokens` to the new value when it is not NULL and keeps the stored value otherwise. A
   stored row that counts as absent under Decision 2 is replaced whole: the write sets both
   fields, a NULL `stable_tokens` included.

   No other outcome writes a row: not a repeated or conflicting settlement, not a ceiling block, not
   an attempt settled without the certificate (an open attempt settled by another process), and not
   an attempt on an account blocked when it settles. A stable prefix below the provider's minimum
   cacheable length is never written for an hour by itself. It gets a row only when a one-hour
   mark after the key block wrote a longer entry. That write stores NULL, so no read refreshes the
   row unless an earlier write had stored a number. Each further deeper one-hour write on that
   account extends the row by another hour. For as long as those writes keep coming, the row steers
   new scopes there, with no cache benefit to them.
4. **Clearing.** In the transaction that records the outcome, the ledger deletes every
   `warm_prefix` row for the provider and account when it records:
   - an `UnknownOutcome` on an open attempt with any reason but `cancelled` and `stream_closed` (the
     still-open branch, `_ledger.py:644-651`), whether or not an earlier reason already stands:
     `auth_required`, `rate_limited`, `provider_error`, `request_rejected`, `transport_error`,
     `usage_incomplete`, `stream_error`, `stream_truncated`;
   - a `ProvenNotSent` the caller did not cause, settled in the open-to-settled branch
     (`:668-701`): an unresolved key in `begin` (`_router.py:132-134`) and a transport failure
     before the request entered (`messages.py:619`, `:1083`). `ProvenNotSent`
     (`lionagi/service/accounts/_types.py:141-143`) gains `caller_cancelled: bool = False`. It is
     set by the two cancellation handlers (`messages.py:585`, `:1055`) and by `begin` when the
     exception at `_router.py:140` is the cancellation class or `GeneratorExit` (a consumer that
     closes its own stream during `begin`). The recorded outcome stays `not_sent`;
   - a block: `settlement_conflict` (`_ledger.py:662`), `ceiling_exceeded` (`:697`) and every
     `block()` (`_block_sync`, `:714`), `auth_required` included;
   - `clear_block()` (`:968`) on an account whose `block_reason` is not NULL when that transaction
     reads it. A process without this amendment can block an account without deleting its rows, so
     the re-admission deletes them. A `clear_block()` on an account that is not blocked deletes
     nothing, and a repeat of an adjustment already recorded returns before that read (`:983`) and
     deletes nothing.

   It does not clear on a complete settlement. It does not clear on `cancelled`, `stream_closed` or
   a caller-cancelled `not_sent`, which say the caller ended the request and nothing about the
   account. A consumer that closes the stream after the provider sent an error event records
   `stream_error`, not `stream_closed` (item 8), so that failure clears. It does not clear on a
   reconciliation (`_reconcile_sync`, `:772`), which prices an old attempt after the fact, or on a
   settlement of an attempt already settled, which returns before any write (`:642-643` for
   `UnknownOutcome`, `:659-660` for an identical repeat). A repeated `ProvenNotSent` whose
   `caller_cancelled` differs from the first reads as identical, because the flag is not stored. It
   returns `already_settled` and clears nothing.

   The clear has to cover more than blocks. Only a 401 or 403 blocks an account
   (`messages.py:516`, `:986`). A rate-limited account, or one whose credit is exhausted (a 400), stays a
   candidate. Without the clear, its row would keep it first in W for every new scope with that
   prefix, and a bound scope never moves. The clear removes that preference and nothing else: D3's
   order still binds the failing account when it ranks first, as it does today. A `ProvenNotSent`
   settles at cost zero (`_ledger.py:607-609`) and an unknown outcome keeps only its attempt's quote
   open, so a failure need not move the account down that order. A request already in flight when
   its account failed, whose complete settlement with a one-hour write commits after the failure's,
   writes a row again. That settlement is evidence the account served the request, not that it
   served it after the failure: the provider can have answered it first, and only the ledger commit
   came later. The row then steers new scopes back to the account until the next failure there
   clears it. A completion that only reads restores nothing, because a read
   refreshes a row that exists and never creates one.
   Clearing more than needed (a second reason on one attempt, an older failure deleting a fresher
   row) costs only writes.
5. **Selection for a new scope.** The candidate set is unchanged: configured, unblocked accounts
   with room for the attempt (`_fits`, `_ledger.py:395-397`).
   - Let W be the candidates holding a row for the request's certificate whose `expires_at` is later
     than the reservation's now, and whose committed spend including this attempt satisfies
     `10 * (spent + opened + ceiling) <= 9 * budget`. Equality is inside W. The values are the
     Python integers `_fits` uses: `spent` and `opened` from `_spend_parts` (`:346`), the quote's
     ceiling, and `spec.budget_nusd`, which equals the month row whenever one exists (`:473`).
   - If W is non-empty, the scope binds the first of W under D3's order.
   - Otherwise it binds the first of all candidates under D3's order, as before. A warm candidate
     above the bound is not in W and stays a candidate under that order, so it is bound again when
     it still ranks first there.
   - A reservation without a certificate has an empty W.
   - The reservation and the binding are still one D4 transaction, and they read the index inside
     it.
   - The bound keeps a tenth of a warm account's budget free beyond this attempt at the moment
     affinity admits a new scope there. It does not promise that a bound conversation's later turns
     fit: a bound scope never moves (item 6), and its later turns can still meet `budget_exhausted`,
     as today. It stops affinity from filling an account that its own conversations still need.
   - At a month boundary spend restarts at zero and rows keep their absolute expiry, because the
     provider's cache does not follow the ledger's months.
6. **Unchanged.** Everything else in D3 and D4 stands, except the three settlement shields, the
   cancellation delivery point and the stream-close reason in item 8.
   - A bound scope keeps its account and is never rebound. An absent or blocked bound account
     refuses. A bound scope's reservation does not read the index.
   - Selection never happens per HTTP retry or per shared model.
   - A first chosen account with an unresolved key refuses without trying another.
   - D3's order itself is unchanged: integer cross-multiplication of committed utilization, then
     distinct scopes in flight, then account name. Unknown attempts remain open for ranking and
     admission.
7. **Schema.** `user_version` stays 1. The table is not added to `_TABLES`.
   - The unlocked fast path in `_ensure_schema` (`_ledger.py:233-236`) returns only when the
     version is 1 and `_TABLES | {"warm_prefix"}` is present.
   - The locked version-0 branch, which a fresh file takes, creates the table beside the base
     tables and before `PRAGMA user_version = 1` (`:343`), in the same transaction.
   - The locked version-1 branch requires `_TABLES` as today and then runs the
     `CREATE TABLE IF NOT EXISTS` statement before it returns. A version-1 file missing a base table
     still refuses with `LEDGER_UNAVAILABLE`.
   - So every connection `_connect` returns (`_ledger.py:200-218`) holds the table. Every connection
     is opened writable, so the first open after an upgrade creates it under the lock, and no reader
     needs a write it could not already make.
   - An older lionagi opens a file that has the table, because its check admits a superset of
     `_TABLES`, and it neither reads nor writes it. Its failures therefore clear nothing (see the
     limits below).
   - The index is never required: an empty table is the pre-amendment behaviour, and no reservation
     refuses because of anything in the table or missing from it.
8. **Operations.**
   - D4's `reserve(scope, attempt_id, quote, accounts)` becomes
     `reserve(scope, attempt_id, quote, accounts, *, cert=None)`; it reads the index when the
     reservation inserts the scope's binding (`_ledger.py:551`). `Reservation` is unchanged: no
     later step needs to know whether the scope was new, because the read arm applies to every
     settled attempt.
   - `settle(attempt_id, evidence)` becomes `settle(attempt_id, evidence, *, cert=None,
     hour_after_key=False)`; it writes (Decision 3) and clears (Decision 4). `hour_after_key` is
     the flag from Decision 1.
   - `RouterAttempt` (`_router.py:62-67`, `@dataclass(slots=True)`, built positionally at `:143`)
     gains `cert: str | None = None` and `hour_after_key: bool = False` after its four fields, so
     positional construction is unchanged. `begin` computes both from the frozen payload (`:124`),
     passes `cert` to `ledger.reserve` (`:129`) and builds the attempt with both (`:143`). Only the
     two settlements that can carry `CompleteUsage` pass them on to `ledger.settle`:
     `settle_response` (`:159-169`) and `settle_stream_end` (`:184-195`). `settle_unknown`
     (`:171-172`) and the not-sent paths pass neither; nothing is lost, because only a
     `CompleteUsage` settlement writes a row and a clear needs only the account.
   - `ProvenNotSent` gains `caller_cancelled` (Decision 4). `AccountRouter.settle_reservation_not_sent`
     (`_router.py:177-179`) gains `*, caller_cancelled: bool = False` and builds its `ProvenNotSent`
     with it. `settle_not_sent` (`:174-175`), which delegates to it, gains the same keyword and
     forwards it. The adapter's two cancellation handlers pass `True` at their `settle_not_sent`
     calls (`messages.py:585`, `:1055`). `begin`'s bare `except BaseException` (`:140-142`) binds
     the exception and passes `True` when it is the cancellation class or `GeneratorExit`, `False`
     otherwise. Its `AccountRoutingError` handler (`:133-139`) passes nothing.
   - `AccountRouter.settle_response` (`_router.py:159-169`), `AccountRouter.settle_unknown`
     (`:171-172`) and `AccountRouter.block` (`:181-182`) run their ledger write under
     `CancelScope(shield=True)`, as `settle_reservation_not_sent` and `settle_stream_end` already do
     (`:177-179`, `:184-195`). `settle_response` records Decision 4's `usage_incomplete` and
     `ceiling_exceeded` on the non-streamed path (`messages.py:573`), and the complete settlements
     that D6 settles under cancellation (`ADR-0126.md:565`). A worker write that waits for admission
     while an AnyIO scope cancellation is pending never starts
     (`lionagi/ln/concurrency/utils.py:64-65`). Today, when a 401, 403, 429, 5xx or other non-200
     status is being recorded (`messages.py:511-541`, `:981-1011`), the adapter's cancellation
     handler then records `cancelled` instead: no clear, no block for a 401 or 403 whose cancellation
     was already pending when `block` was called, and a held ceiling for a 200 whose settlement never
     started. A
     cancellation that arrives while `block` runs lets the block commit and stops `settle_unknown`.
     With the shields, both writes commit.
   - The adapter delivers a pending cancellation after the failure is recorded. The clauses that
     re-raise a routed failure (`messages.py:591-599`, `:1066-1069`) and the two transport-failure
     branches (`:615-620`, `:1079-1084`) call `await anyio.lowlevel.checkpoint()` before they raise.
     Without it, the transport branch raises the routed failure with no checkpoint after its
     shielded write, so a caller under `fail_after` receives the routing error instead of
     `TimeoutError`, and a status-code branch delivers the cancellation only if closing the HTTP
     session checkpoints. When the cancellation fires first, the cancellation handler's second
     settlement records nothing: the first reason on an open attempt stands, and a settled attempt
     is left as it is (`_ledger.py:642-650`). The comment at `messages.py:512-515` and `:982-985` is
     rewritten: both writes are shielded, and the block still goes first because a ledger error on
     the first write stops the second, and losing the attempt's reason only keeps its ceiling held.
   - The shields hold against AnyIO scope cancellation only. A native `task.cancel()` is not held:
     pending before the write is admitted, it stops the write; arriving during the worker wait, it
     returns to the caller at once and the write commits after (`messages.py:578-580`). Both hold
     for the two existing shields as well. A shielded write also waits without a timeout for a
     thread-limiter token. D6's five-second bound on the SQLite lock (`busy_timeout_s`) covers that
     wait, not admission, for the existing shields and for these three alike. Amendment 6 adds a
     separate admission bound.
   - The streamed adapter's `GeneratorExit` handler (`messages.py:1061-1065`) records `stream_error`
     when the stream carried an error event (`saw_error_event`, `:935`, `:955-958`) and
     `stream_closed` otherwise. Its end-of-stream branch makes the same error-event test to choose
     `stream_error` over `stream_truncated` (`:1045-1047`).
   - In the streamed attempt every settlement sets `settled` before it awaits the router (the
     status branches, `messages.py:986-1017`, and `:951`, `:1048`), and the transport-failure
     branch (`:1079-1084`) settles only when `settled` is false; it still raises. Today a status
     branch settles without setting the flag, and the transport branch has no guard, so a
     `GeneratorExit` or an exception that the unwinding response or client raises after a
     settlement leads the close handler or the transport branch to settle a second time. After
     `CompleteUsage` that is `already_settled`; on an attempt still open (`usage_incomplete`, a
     status outcome) a second clearing reason clears the account's rows again, a row another
     request wrote in between included. A settlement whose ledger write raises leaves the attempt
     open for reconciliation, as an unsettled attempt is today.
   - The summary row "Assignment" reads: "D3: bind a conversation once; a new conversation prefers
     an account where its stable prefix is warm, then ranks by committed budget utilization."

### What it guarantees, and what it does not

- Guaranteed: a new scope whose request has a certificate is bound to an account holding an
  unexpired row for it whenever such an account is a candidate and stays within the 90% bound with
  this attempt. Among several, D3's order decides. A warm account above the bound is not preferred,
  and selection uses D3's order over all candidates, which can still choose it.
- Guaranteed: a row exists only after a complete settlement with a one-hour write on that account,
  and the read arm extends it only on a read whose length equals the stable entry's.
- Guaranteed, among processes carrying this amendment: a failure outcome of Decision 4 deletes the
  account's rows in the transaction that records it, and an AnyIO scope cancellation pending while
  the failure is recorded does not replace it (Decision 8). A native `task.cancel()` pending before
  the write is admitted still can.
- Not guaranteed: that a streamed failure is seen as one. The close reason reads
  `saw_error_event`, which is set only once the adapter has processed the error event. An error
  event the redactors still hold when the consumer closes, or a cancellation that replaces
  `GeneratorExit` while the stream unwinds, records `stream_closed` or `cancelled` and clears
  nothing. A frame flagged `call_failed` before `message_stop` makes a close before `message_stop`
  record `stream_error`, which clears the rows of an account that served the request. Both cost
  steering only: the next failure there clears a missed row, and the next one-hour write restores
  a cleared one.
- Not guaranteed: the write arm's count when a live five-minute entry extends past the key block
  (a bound conversation's message entry that outlived the key's one-hour entry). The sum assumes
  the provider counts the read before the one-hour write. If it writes the hour at the key from
  that entry and counts the rest as read, the stored count is longer than the key, no stable read
  matches it, and only an exact read of that deeper entry refreshes the row. That costs steering
  only. This amendment has not measured how the provider counts it.
- Not guaranteed: that the provider still holds the entry. A row can outlive it through an early
  eviction or a row written just before a crash. A five-minute entry at the key block's position
  (written by a request whose stable mark was five-minute) can do the same, depending on how the
  provider serves a one-hour mark that ends where a live five-minute entry ends. This amendment
  has not measured that:
  - If the provider serves it as a read of the five-minute entry and writes no one-hour entry, the
    read arm refreshes the row for an hour while the entry lives five minutes. While those reads
    arrive less than five minutes apart the row stays fresh with only a five-minute entry behind
    it, and the first longer pause leaves the account cold under a live row.
  - If the provider writes a one-hour entry there, the request takes the write arm and the row is
    backed by a real one-hour entry.

  In the first case, and for an evicted or crash-written row, until the next one-hour write there
  settles or the row lapses, every new scope that reserves goes to that account, and each pays the
  one write it would pay on a cold account today. A second account that is genuinely warm
  can be passed over for that window. The index steers selection only; it never feeds placement,
  which stays ADR-0127's per-process, per-Branch decision.
- Not guaranteed: that the certificate matches the provider's cache key. `points()` seeds on the
  model, thinking and effort and hashes raw values. Two requests the provider caches alike can
  carry different certificates (an omitted option against its explicit default), and two it caches
  apart can share one (`output_config` keys other than effort; a key on the last tool for requests
  differing in `speed`, which `points()` hashes after the tools). `tool_choice` is hashed after the
  system tier, which matches the provider: a `tool_choice` change keeps the tools and system caches.
  A split certificate (one provider prefix, two certificates) costs one write per certificate, as
  today. A shared certificate (two provider prefixes, one certificate) costs more. Each prefix's
  one-hour write sets the row's `stable_tokens` to its own length, so the other prefix's stable
  reads stop refreshing the row. The row then lapses an hour after the last write, every new scope
  that finds no row writes on the account it lands on, and the cycle repeats while both prefixes
  keep writing. A read of the other prefix that happens to have the stored length also refreshes
  the row. While the row lives, written or refreshed by one prefix on an account where the other
  prefix is cold, every new scope of the other prefix that reserves goes there, passing over an
  account where that prefix is warm, and each pays a write until a one-hour write of that prefix
  settles there or the row lapses. A prefix below the provider's minimum cacheable length that
  shares a certificate is steered to the row's account while the row lives; it caches nowhere, so
  that costs no write, only concentration.
- Not guaranteed: that a row is as fresh as the entry. A first attempt that writes the entry and
  then fails before its usage settles leaves no row: the failure clears the account's rows, and the
  retry that then reads the entry cannot create one, because a read never creates a row. A stable
  entry kept alive only by reads that also cover deeper entries does not refresh the row, and a row
  whose `stable_tokens` is NULL is never refreshed by a read. In each case the row can lapse, or
  never appear, while the entry is warm. New scopes with that prefix then go by D3's order, as
  today, each paying a write on the cold account it lands on, until a one-hour write settles and
  writes a row. A row that lapsed, rather than never appearing, also comes back with the next
  stable-length read on its account. A failure after the attempt settled (a transport error after a
  stream's final usage) clears nothing: the attempt is already settled (`_ledger.py:642-643`), and
  the account served the request.
- Not guaranteed: a key that names a shorter one-hour entry finds a row written under the longer
  key that contains it. The row records the last one-hour block of the request that wrote it.
- Spreading still happens, per prefix, whenever W is empty:
  - at a prefix's first use, after its row lapses, or after a failure cleared it;
  - when its warm account is blocked or out of room;
  - when its warm account is above the 90% bound and another candidate ranks ahead of it under
    D3's order. Above the bound the warm account can still rank first and be bound again. When the
    prefix does move, each new scope that reserves before a row settles on the new account writes
    on the account it lands on;
  - for concurrent cold starts: a new scope that reserves before the first scope's request settles
    sees no row and writes on its own account. Routing it to the same account would save nothing,
    because the provider serves an entry only after the request that writes it.
  So several system prompts still land on several accounts, and each stays warm where it landed.
- Not guaranteed: even spend across accounts inside the month. A prefix that carries most of a
  process's traffic stays on one account until the prefix leaves W there (the account fails or
  passes the bound, or the row lapses) and another candidate ranks ahead of it under D3's order.
  Measured: one system prompt carried 58% of one process's 1,353 calls. The
  amendment adds no spend: total spend is what the requests cost, wherever they run. D3's
  consequence "new work spreads by committed utilization" now reads "new prefixes spread by
  committed utilization; a warm prefix stays on its account".
- Rate limits concentrate: an account carrying a hot prefix takes its whole new-conversation rate,
  unmeasured at this service's rate. The first 429 clears the account's rows (Decision 4). As
  after any clearing failure, a request in flight there whose one-hour write settles afterwards
  writes the row again, even when the provider answered it before the 429; one that only reads
  restores nothing.
- Shared across processes: the D4 ledger is one file for cooperating local processes. A prefix
  warmed by one process is preferred by the next, which is correct because caches belong to the
  organization, not to the process. A process without this amendment records its failures and
  clears nothing, so for up to an hour, and longer while stable reads keep refreshing the row, a
  process carrying it can prefer an account the older one found failing. A later `clear_block()` by
  a process carrying it deletes the rows while the account is still blocked when it runs. Once a
  process without the amendment has cleared the block, the rows stay until they lapse or the next
  failure there. A re-admission after an accounting block that process set
  (`settlement_conflict`, `ceiling_exceeded`) deletes rows of an account that served its requests.
- An outer timeout that cancels the task (`asyncio.timeout`, `anyio.fail_after`) reaches the
  adapter as the cancellation class: before the request is sent it records a caller-cancelled
  `not_sent`, after it `cancelled`, and neither clears. A transport timeout inside the adapter on an
  open attempt records `transport_error`, which clears; one after the attempt settled clears
  nothing.

### Alternatives considered

- **Refresh on any new-scope read.** Rejected: a cloned Branch or a shared first
  message reads a deeper entry, which does not refresh the stable one, so the row outlives the
  entry and steers new scopes to a cold account. Comparing the read length with the stored stable
  length keeps the refresh for the stable reads that a hot prefix's new scopes make.
- **Refresh rows only on one-hour writes.** Not adopted. A hot prefix's provider entry stays warm
  through reads while a write-only row lapses an hour after its write, after which new scopes fall
  back to D3's order and pay a write on each cold account they land on. A replay put that cost above
  the measured process, but it models no account pool and counts every scope without a row as a
  write, so its figure does not decide the choice. The discriminating read arm removes the false
  freshness that was the reason to consider it.
- **Order writes after clears by a recorded clear time.** Rejected: a strict comparison fails on equal ticks and on a wall-clock step back, and it drops the rows of
  healthy attempts reserved before an unrelated failure on the same account. A complete settlement
  is evidence that the account served the request, at a time the ledger does not know and that can
  precede a failure the ledger recorded first. Unless the account is blocked when it settles, one
  with a one-hour write writes a row and a stable read refreshes a row that still exists. After a
  clear only a one-hour write restores the row, and the next failure clears it again. Until then,
  new scopes go to an account that has failed.
- **Order writes by a ledger sequence number.** Not adopted: it needs a column on `attempt` (a base
  table) for an ordering the evidence above already makes unnecessary.
- **Key on the last stable block whatever its marks.** Rejected: a request whose stable mark is
  five-minute or absent has no one-hour entry to steer toward, and a block nobody marked keys
  requests whose entries end elsewhere.
- **Key on the placement's first system-tier one-hour mark.** Rejected: the placement does not
  exist on standalone and direct-endpoint calls and is stale after a hook changes the body, and a
  caller's mark on an early system block keys requests whose later system blocks differ.
- **Normalize containers inside `points()` instead of parsing the frozen body.** Rejected: the
  placement calls `points()` and `_write` writes through what it returns, so the change would reach
  every placement, while parsing the frozen bytes gives `begin` exactly the arrays the provider
  receives and leaves `points()` as it is.
- **Clear on every outcome that is not complete, cancellation included.** Rejected: a caller's
  cancellation says nothing about the account, and a fanout that cancels its losers would keep
  wiping the rows its winners wrote.
- **Clear only on 429 and 5xx.** Rejected: an unresolved key, a 400 for exhausted credit, a
  transport timeout and a failed stream each left the row, which kept the failing account first
  for every new scope with that prefix even when another account ranked ahead under D3's order.
- **Disclose the budget cost instead of the 90% bound.** Rejected: a bound conversation refused
  with room on other accounts is an availability loss. When the bound trips and the prefix moves,
  it costs one write for each new scope that binds before a row settles on its account.
- **Yield when the warm account runs ahead of pace** by more than a threshold. Rejected for now:
  each yield costs one one-hour write of the prefix, and a pace gap alone refuses nothing. Revisit
  if a month-end measurement shows credit left unspent because of affinity.
- **Use the per-Branch rows.** Rejected: a new scope has none.
- **One account per process.** That is the one-account measurement, and it gives up spreading
  entirely.

### Verification

Tests under `tests/service/accounts/` and `tests/providers/anthropic/`:

- A new scope with an unexpired row on a candidate binds that account even when another candidate
  is further behind pace (control: with the row removed, it binds the furthest-behind account).
  With two warm candidates in W, the one first under D3's order binds.
- A reservation without a certificate binds by D3's order with warm rows present and issues no
  statement naming `warm_prefix` (asserted with `sqlite3.Connection.set_trace_callback` on the ledger
  connection).
- Expiry: a row whose `expires_at` is not later than now is ignored; `expires_at` is the attempt's
  `created_at` plus an hour, not the settle time; an upsert never moves `expires_at` earlier; a
  stored `expires_at` with a `+01:00` offset compares by time, where text order would misorder it;
  a stored value without an offset and a stored value that is not ISO-8601 each count as absent:
  not in W, not refreshed by a stable read, replaced by a write (a NULL `stable_tokens` included),
  and nothing refuses. Both on Python 3.10 and 3.12. A stored value with a `Z` suffix counts as
  absent on 3.10 and compares by time on 3.12; every value the ledger writes ends in `+00:00`.
- Month boundary: a row written before a credit-month rollover stays in W after it, with the bound
  computed on the new month's spend.
- The bound: a warm candidate for which `10 * (spent + opened + ceiling) > 9 * budget` is excluded
  from W and can still be chosen by D3's order; the equality case is in W.
- A warm account that is blocked or has no room is skipped, and the order decides among the rest.
- Write arm: a complete settlement with a one-hour write creates the row with `stable_tokens` equal
  to read plus one-hour-write tokens; with a one-hour mark after the key block it stores NULL; an
  upsert with NULL keeps a stored value, and a non-NULL upsert replaces a stored NULL; a streamed
  request settled through `settle_stream_end` writes the same row as the non-streamed
  `settle_response` path.
- Read arm: a settlement with no one-hour write whose read equals `stable_tokens` refreshes the row
  (new scope and bound scope alike), a row whose `expires_at` has passed included; a read one token
  longer, one token shorter, a read with no row and a read on a NULL row refresh nothing and create
  nothing. A retried first request, whose first attempt wrote the entry and failed, creates no row
  with its read.
- No write: a repeated settlement, a conflicting settlement, a ceiling block, a settlement without
  a certificate, and a complete settlement on an account blocked before the settlement.
- Each clearing outcome in Decision 4, `clear_block()` on a blocked account included, deletes that
  account's rows; the next new scope falls back to the order. `clear_block()` on an account that is not
  blocked deletes nothing. A consumer that closes the stream after an error event records `stream_error`
  and clears; one that closes it without an error event records `stream_closed` and does not. A
  transport failure after `settle_stream_end` recorded complete usage clears nothing. With an AnyIO
  scope cancellation (`fail_after`) pending when the adapter records a 429, the attempt keeps
  `rate_limited`, the rows are cleared and the caller receives `TimeoutError`; pending at a 401, the
  account is blocked, the attempt keeps `auth_required`, the rows are cleared and the caller receives
  `TimeoutError`; pending at a 5xx, the attempt keeps `provider_error`, the rows are cleared and the
  caller receives `TimeoutError`; pending at a 400, the attempt keeps `request_rejected`, the rows are
  cleared and the caller receives `TimeoutError`; pending at a transport failure after the request
  entered, the attempt keeps
  `transport_error`, the rows are cleared and the caller receives `TimeoutError`; pending at a 200 whose
  usage is incomplete, the attempt keeps `usage_incomplete` and the rows are cleared; pending at a 200
  that prices over the ceiling, the account is blocked with `ceiling_exceeded` and the rows are cleared.
  A clearing `UnknownOutcome` on an open attempt that already recorded `cancelled` clears. A
  non-cancellation exception at `begin`'s `except BaseException` settles a `not_sent` that clears.
  `cancelled`, `stream_closed`, a caller-cancelled `not_sent` from either adapter cancellation handler
  or from `begin` (cancellation class and `GeneratorExit`), a reconciliation, an `UnknownOutcome` on a
  settled attempt and an identical repeat delete nothing. `ProvenNotSent(caller_cancelled=True)`
  followed by `ProvenNotSent(caller_cancelled=False)` on one attempt returns `already_settled` and
  clears nothing.
- A request reserved before a clearing outcome that does not block its account, and completing
  after it with a one-hour write, writes its row; one completing after it with a stable read writes
  nothing; one completing after a block writes nothing.
- Usage shapes: a request with a server tool stores NULL on a write and refreshes nothing on a matching
  read; a one-hour mark inside a `document` block's `source.content` after the key block sets
  `hour_after_key`; a top-level one-hour `cache_control` with no markable block after the key block
  leaves it false; a caller's one-hour mark on a blank system text block is not the key; a non-text
  `expires_at` counts as absent; an attempt with an unparsable `created_at` writes nothing; a
  `GeneratorExit` or an exception while unwinding a stream that already settled (a status outcome or
  `CompleteUsage`) settles nothing more; a repeated `clear_block()` with a recorded adjustment deletes
  nothing.
- The key: computed from the frozen body; a hook that changes the system text after placement
  changes it; a direct endpoint call and a prebuilt event are keyed from the body they send; a
  standalone call with a string system prompt and a one-hour mark on its last tool is keyed by that
  tool; a request whose only tools/system one-hour mark is a caller's on an earlier block, with the
  stable block at five minutes, is keyed by that earlier block; the same when `MAX_MARKS` leaves the
  stable block unmarked; a request with no one-hour mark in those tiers has no key; with a caller
  one-hour mark on an earlier system block and the adapter's on the last, the last is the key.
- `hour_after_key` is true in each of these cases:
  - a one-hour mark on a message block;
  - a one-hour mark nested in a `tool_result` block's content;
  - a placement probe mark on an older one-hour message entry;
  - a one-hour mark carried by a message whose content is a tuple;
  - a top-level `cache_control` with `"ttl": "1h"`.

  It is false when the messages carry no one-hour mark. For the tuple case the key is the same as
  for the list form of the same body. A mutation that computes `hour_after_key` from `Point.mark`
  alone turns the nested arm red; one that skips the parse turns the tuple arm red.
- In a fixture with a one-hour mark nested after the key block, the write stores NULL, not the
  longer read-plus-write count. The `Point.mark`-only mutation turns this red, because that
  mutation stores the longer count.
- `points()` output is byte-identical before and after the change for every fixture in the
  existing prompt-cache tests.
- A bound scope's reservation is unchanged by any row and issues no statement naming `warm_prefix`
  (the same trace callback).
- `RouterAttempt` built positionally with four arguments still constructs, with `cert` None and
  `hour_after_key` false.
- A version-0 (fresh) file gains the table before `user_version` is set; a version-1 ledger without
  it opens and gains it under the lock; a version-1 file missing a base table still refuses; an
  older reader admits a file that has it.
- Two router instances on one ledger file see each other's rows and clears.
- Mutations: replacing the W preference with the plain order turns the first test red; dropping the
  length comparison from the read arm turns the one-token-longer and one-token-shorter cases red;
  dropping the block check turns the blocked-account no-write case red; dropping the server-tool check
  turns the server-tool arm red; reading `Point.nested` for the flag turns the `source.content` arm red;
  setting `settled` after a status-branch settlement instead of before turns the settled-stream arm red;
  treating `GeneratorExit` like any other exception in `begin` (`_router.py:140`) turns `begin`'s
  no-clear case red; comparing the stored and current `expires_at` as text turns the `+01:00` case red;
  removing the shield from `settle_unknown` turns the pending-cancellation 429 case red; removing it
  from `block` turns the pending-cancellation 401 case red; removing it from `settle_response` turns the
  pending-cancellation `usage_incomplete` case red; removing the checkpoint from the transport branch
  turns its `TimeoutError` arm red; ignoring the error event in the `GeneratorExit` handler turns the
  close-after-error case red; dropping the `block_reason` read from `clear_block()` turns the
  not-blocked case red.

KPI: the same per-route census on the first process of that service started on code carrying this
amendment. Like for like: new-scope one-hour writes, 54 and 59 measured, fall toward the replay's 9
and 9, a scale whose errors run both ways (Context); one-hour writes of a prefix seen within the hour on another account fall
from 46 and 55 toward the concurrent cold starts (at most 5 and 17); the cost ratio falls to about
0.38 to 0.39 on those mixes. Each remaining first-on-account write is a first use, a lapsed row, a
cleared row, a bound trip, a blocked or out-of-room warm account, a concurrent cold start, or a
retried first request.

## Amendment 6 (2026-10-30) — bound the ledger's thread-limiter admission wait

Purpose: bound the wait for a worker thread that every ledger call takes before its SQLite work starts, so a shielded ledger call cannot wait without limit.

### Context

D6 says stream close and finalization shield an available settlement from caller cancellation, bounded by the ledger's five-second wait. That bound covers only the SQLite lock wait. Each ledger call runs its SQLite work through `CredentialLedger._run` in `lionagi/service/accounts/_ledger.py`. `_run` calls `run_sync` with no limiter, so the call first waits for a token from AnyIO's process-wide default thread limiter, which every other thread-offloaded call that names no limiter also shares. `busy_timeout_s` (default 5.0) is the sqlite3 connect timeout and `PRAGMA busy_timeout`, and it starts only once the worker thread runs. Nothing bounds the token wait. Inside `anyio.CancelScope(shield=True)`, the caller's deadline cannot reach that wait, so a shielded ledger write can wait without limit. Amendment 5, Decision 8 discloses this for the existing shields and for the three it adds. This amendment supplies the bound.

### Decision

1. Every ledger call bounds its admission wait with its own five-second bound, separate from `busy_timeout_s`. The worst case for one call is five seconds waiting for a token plus five seconds waiting for the SQLite lock. A 401 or 403 makes two ledger calls (it records the block, then the attempt's reason), so that path can wait up to twice the per-call worst case, twenty seconds.
2. When the admission bound trips, the call raises `AccountRoutingError` with code `ledger_busy` (`RouterErrorCode.LEDGER_BUSY`), the outcome a SQLite lock timeout produces today. It never surfaces as a bare `TimeoutError`. The default retry exceptions in `lionagi/service/resilience.py` include `TimeoutError`, so a bare timeout would be retried as if the request had failed in transit. `AccountRoutingError` is not in that default set, so it is not retried.
3. A caller of a routed request receives that `AccountRoutingError(ledger_busy)` unchanged and is not retried. The `begin` call in `_request_attempt` (`lionagi/providers/anthropic/messages.py`) sits after the try block that converts routing errors, and the handlers in `lionagi/service/connections/endpoint.py` convert only the internal routed-attempt classes, so the error passes through. The streamed attempt's `begin` call in `_routed_stream_attempt` (same file) also sits outside that conversion, and a test measures that its error reaches the caller unchanged and is not retried.
4. The bound covers admission only. Once the worker thread has started the operation, the admission bound no longer applies, and the SQLite wait stays bounded by `busy_timeout_s`. A bound that also spanned the worker wait could expire after the write committed, and the caller would then be told the call failed while its ledger row exists. A test pins this rule.
5. The same change adds an admission-wait timing record. For each ledger call, it records the time from requesting a token to the operation's first line in the worker, kept apart from the SQLite wait. The record is one structured log event per ledger call, described under Timing record. It adds no database column, because a schema change needs its own decision.
6. Not decided here: a dedicated thread limiter for the ledger. It is new structure with no measurement behind it. It returns as its own amendment only if admission-wait numbers from real runs (p50 and p99, with the population named) show that the shared pool is the cause.

### What it guarantees and what it does not

- A shielded ledger call that cannot obtain a token raises `ledger_busy` within the admission bound plus a small margin. It never raises a bare timeout.
- A call whose worker starts before the bound expires runs to its own SQLite bound and is reported by its actual outcome.
- It does not change the native `task.cancel()` behavior that Amendment 5 describes.
- It does not change the existing busy-timeout arm, which still raises `ledger_busy` when the SQLite lock is held past `busy_timeout_s`.
- The admission bound is not a retry. The caller decides whether to retry.

### Alternatives considered

- One bound over admission and the worker wait together: rejected, because it could expire after the write committed and tell the caller the call failed while its ledger row exists (Decision 4).
- A separate admission bound for each ledger call: chosen (Decision 1).
- A dedicated thread limiter for the ledger: deferred until measured admission waits show that the shared pool is the cause (Decision 6).

### Verification

Each item is a test that fails without this change:

- A shielded ledger call whose limiter is saturated raises `AccountRoutingError(ledger_busy)` within the admission bound plus a small margin, and never `TimeoutError`.
- The caller of a routed request whose `begin` trips the admission bound receives `AccountRoutingError(ledger_busy)`, and the request is not retried.
- A call admitted just before the bound whose operation commits is reported as committed. The bound never fires after the worker started, and the ledger row and the return value agree.
- A SQLite lock held past the busy timeout still raises `ledger_busy`, as today.
- Each ledger call emits exactly one DEBUG event carrying `admission_wait_s`, `sqlite_wait_s` and the outcome, including a call whose admission bound trips (with `sqlite_wait_s` absent) and a call whose SQLite lock times out.

### Timing record

Each ledger call emits one structured log event at DEBUG level, with three fields:

- `admission_wait_s`: the time from requesting a worker thread token to the operation's first line in the worker. When the admission bound trips, it is the time waited until the trip.
- `sqlite_wait_s`: the time from that first line to the operation's return, which includes any wait for the SQLite lock. It is absent when the operation never started.
- the outcome: committed, `ledger_busy` from the admission bound, `ledger_busy` from the SQLite lock, or the class name of any other error.

The event is the whole record. The change adds no state to `CredentialLedger` and no field to `RouterAttempt`, and nothing in the process reads the event back. Numbers across runs (p50 and p99, Decision 6) come from collecting these events from a run's logs. A run that does not keep DEBUG logs keeps no timing.

## Requirements

The request this record answers, and where each part is carried:

- **R1**: run Anthropic Messages calls on several API keys, each an organization
  with its own monthly credit, and spend that credit deliberately. Carried here:
  D1, D3, D5.
- **R2**: read a key from the process environment, then `.env`, then the
  configured secret lookup. Carried here: D2.
- **R3**: design prompt-cache placement, with Claude Code's placement as the
  reference (lion1 ADR-0028 S10 and A1). Not carried here: carried by ADR-0127
  (to be proposed), under ADR-0027 D4's rule that a provider adapter owns its
  request grammar. The CLI-account half of lion1 ADR-0024 is out of scope (P4).

## Context

**P1 — One provider credential cannot distribute a workload over named budgets.**
`AppSettings` declares one `ANTHROPIC_API_KEY` and reads `$LIONAGI_HOME/.env`, `.env`,
`.env.local`, then `.secrets.env` (`lionagi/config.py:174`, `lionagi/config.py:192`). Provider metadata
supplies that name to endpoint configuration
(`lionagi/providers/anthropic/_config.py:23`,
`lionagi/service/connections/provider_config.py:99`). A missing settings name
falls through to `os.getenv(name, name)`, allowing the unresolved name to become
the credential (`lionagi/service/connections/endpoint_config.py:132`).

**P2 — A logical call is not a billable attempt.** The path is Branch chat →
`iModel.invoke` → `APICalling` → endpoint HTTP transport
(`lionagi/operations/chat/chat.py:103`, `lionagi/service/imodel.py:709`,
`lionagi/service/connections/api_calling.py:329-338`). HTTP retry happens inside the
endpoint, below that event boundary (`lionagi/service/connections/endpoint.py:895`,
`lionagi/service/connections/endpoint.py:918`). Response JSON returns directly;
Branch cost aggregation sums reported costs and leaves absent cost unknown
(`lionagi/service/connections/endpoint.py:826`, `lionagi/session/signal.py:346`).
Neither a successful outer operation nor its final response accounts for every
attempt that might have reached the provider.

**P3 — Shared models are not conversation identities.** API Branch clones share
their models (`lionagi/session/branch.py:580`). Binding a credential on that
shared endpoint would couple independent conversations. Switching accounts per
call would instead disrupt cache reuse. The Claude API isolates caches by
workspace, so keys of different organizations never share a cache
([prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)).
The setup this record was written against is lion1 ADR-0028 A4: six keys in six
organizations, measured on 2026-10-09 as six rate-limit sets with their own reset
clocks, so six caches. There every account switch starts a conversation's cache
again, which is why D3 binds a conversation to one account for its whole life.

**P4 — The design exists in lion1, outside the library.** lion1 ADR-0024 (accounts and the pick)
and lion1 ADR-0028 (the Messages backend and API-key accounts) built an account pool in lion1's
hub: a credential read only to build the request header, a per-attempt ceiling written to a month
ledger before the request, settlement once by attempt id, an unanswered attempt keeping its
ceiling, new work assigned to the account furthest behind its pace and then the one with fewest
calls in flight, and a 401 or 403 marking the account with no silent move to another. Under
ADR-0125 D1 the concept enters through the construct that owns it, lionagi's provider
configuration, rather than as a hub module. This record restates each lion1 decision it adopts
(D2 to D6) and rejects two: lion1's ceiling bounded the prompt by the request body's byte count
(lion1 ADR-0028 S15), which is an assumption about the tokenizer rather than a provider bound (D5),
and lion1's per-wake pick, which binds a long-lived agent rather than a conversation (D3). lion1's
CLI-account half (keychain items per Claude Code account, homes per Codex account, child
environments) stays out of scope.

| Concern | Decision |
|---|---|
| Declaration and activation | D1: an optional AppSettings account list; no second settings hierarchy. |
| Secret custody | D2: strict named env → dotenv → existing declared lookup, at dispatch only. |
| Assignment | D3: bind a conversation once; rank new conversations by committed budget utilization. |
| Durability | D4: one local ledger file; selection, reservation and settlement are transactions. |
| Admission and cost | D5: conservative priced ceilings; complete provider usage settles once. |
| Errors and compatibility | D6: same-account retries, explicit uncertainty, unchanged unconfigured path. |

Out of scope: CLI/OAuth accounts and child credential injection (a different
transport under ADR-0030); provider cache placement (R3, carried by ADR-0127);
distributed budgeting across machines (requires a shared authority); provider
invoice synchronization, credit-expiry discovery and account provisioning
(require separate credentials or user interfaces); general lifecycle changes to
`iModel` (ADR-0027); catalog and role-resolution redesign (ADR-0028, ADR-0043).

## Decision

### D1 — Accounts extend AppSettings

Add one optional field in `lionagi/config.py`:

```python
ANTHROPIC_ACCOUNTS: tuple[ProviderAccount, ...] = ()
# Validated configuration fields; no credential value:
# ProviderAccount(name: str, key_env: str, budget_usd: Decimal)
```

The settings DTO uses the existing Pydantic settings boundary; resolved immutable
configuration follows ADR-0119's `Params` ownership. No new declaration registry,
role field, provider identifier or required dependency is introduced.

Example dotenv value (the same JSON array is accepted from the process environment):

```dotenv
ANTHROPIC_ACCOUNTS='[{"name":"primary","key_env":"ANTHROPIC_PRIMARY_KEY","budget_usd":"50.00"},{"name":"secondary","key_env":"ANTHROPIC_SECONDARY_KEY","budget_usd":"25.00"}]'
```

- Absence or `[]` selects the legacy path. Null, an empty string, a malformed
  array, duplicate names/references or unknown account fields is a configuration
  error; invalid configuration never means an empty pool.
- Names match `[a-z][a-z0-9_-]{0,31}`. References match
  `[A-Za-z_][A-Za-z0-9_]{0,63}`, the existing secret lookup's name rule
  (`lionagi/providers/_secret_resolution.py:44`) with a length bound, so a lookup
  item named in lower case is referenced as itself (D2). Both reject credential-shaped text, including `sk-ant`
  case-insensitively, without echoing rejected values. Budgets are finite positive
  decimal USD strings with at most nine fractional digits, representable as signed
  64-bit nanodollars; booleans and floating-point inputs are rejected.
  Settings/validation errors report field paths and rules, never raw input or
  chained parser/validation exceptions that could repeat a pasted credential.
- The process environment overrides dotenv. Dotenv uses AppSettings' existing
  paths, encoding and precedence; later files override earlier files. Project
  dotenv declarations are allowed as launch configuration, just as credentials
  already are. Project `settings.yaml` declares neither accounts nor lookup
  commands. The global `settings.yaml` remains the authority for `secrets.lookup`.
- Each process captures its configuration and resolved dotenv locations with its
  settings instance. It does not reinterpret relative paths after changing cwd.
  Mid-process configuration reload is DEFERRED: restart to load new declarations.
- One account name is a stable budget allocation with one immutable reference.
  A different name is a different allocation, not a supported way to reset spend.
  Operators must not duplicate one intended allocation under several names or
  rotate its credential into a different billing identity. The router cannot
  establish provider organization ownership from an opaque API key.

Why: this is a delta to the settings path API callers already use. Reading agent
YAML on every otherwise-unconfigured API call would introduce new failure modes.
MAY add provider account DTOs; MAY NOT put secrets in their fields. Verify by
environment/dotenv precedence and unchanged construction with no account field.

### D2 — Resolve only the selected credential, strictly

The provider uses this private seam, returning `SecretStr`:

```python
async def resolve_account_key(key_env: str, sources: CredentialSources) -> SecretStr: ...
```

`CredentialSources` contains the captured dotenv sources and global lookup
declaration, never a dumpable environment dictionary. For each physical attempt:

1. A present process variable wins, including an invalid/empty value.
2. Otherwise read the named variable through the existing dotenv settings-source
   machinery. A present empty/bare assignment is invalid, not a lookup miss.
3. Otherwise use the existing `resolve_secret_lookup_config` and its argv runner
   for **that name only**, provided it is in `secrets.lookup.names`.

The reference is one name used unchanged at every step: the environment variable,
the `.env` key and the lookup's `{name}` argument. It is never case-folded or
mapped, so a lookup item keeps its own name and the account declares that name.

Factor a one-name helper from `lionagi/providers/_secret_resolution.py`; retain
the CLI fill helpers' behavior. They currently validate global-only argv/names,
run with a 15-second timeout and fill all missing declared names
(`lionagi/providers/_secret_resolution.py:89`,
`lionagi/providers/_secret_resolution.py:296`,
`lionagi/providers/_secret_resolution.py:469`,
`lionagi/providers/_secret_resolution.py:543`). Reusing the
whole environment filler is prohibited. The one-name helper uses a sanitized
lookup-child environment without configured account-key variables, bounds output
to 1,025 bytes and keeps the existing cancellation/process cleanup. It removes
one command-output terminal newline; it does not strip an otherwise-invalid key.
No stdout, stderr, argv values or chained subprocess exceptions enter diagnostics.

The accepted header value is 8–1,024 printable ASCII characters excluding space
(`\x21`–`\x7e`); it must differ from its reference name. This validates header
safety, not provider authenticity. Missing, empty, malformed, unauthorized-name,
timeout and unavailable-program outcomes refuse before HTTP. No fallback to the
literal name, default `ANTHROPIC_API_KEY`, or another account follows. A machine
without `security` needs no keychain when env/dotenv resolves the chosen name.

Only request-local header construction unwraps the secret. Routed
`create_payload` and `APICalling.headers` contain no account key; they must not use
the current eager header path (`lionagi/service/imodel.py:384`,
`lionagi/service/connections/endpoint.py:483`). For routed calls, explicit
`api_key`, authentication headers, client-level authentication/trace callbacks,
custom hosts and
redirects are rejected before lookup: the ledger must describe the credential
actually sent to the canonical Anthropic Messages endpoint. Preserve whether
`api_key` was explicitly supplied before metadata defaults are materialized.

MAY retain a `SecretStr` for the attempt's lifetime. MAY NOT mutate process env,
shared endpoint authentication or serialize key material, lengths or fingerprints.
Only the new strict source-loading path sanitizes parser failures; the existing
CLI lookup's permissive failure behavior stays unchanged. Transport exceptions
are replaced with sanitized errors without raw request-info,
header objects or exception chains. Responses/chunks are scrubbed of the chosen
key before hooks, logging or yield, including a key split across stream chunks;
the stream redactor must retain enough boundary context. Masks reveal no key
characters (for the stream path, as bounded by Amendment 1). Verify with a
canary in validation errors, repr/dumps, cache keys, HTTP errors and echoed bodies.

### D3 — One binding per conversation and provider

For this router, a **run** means the lifetime of one Branch conversation identity,
not one `operate`, `ReAct` turn, process or Session. Its durable scope is
`("branch", str(branch.id), "anthropic")`. All of that Branch's Anthropic calls,
including parse calls and explicit model overrides, use the same binding. New
Branches/clones are new runs; restoring the same identity restores its binding.
A Session contains independent Branch scopes, rather than imposing one account.

Standalone iModel calls use `("imodel", str(imodel.id), "anthropic")`; a direct
endpoint instance uses a fresh private UUID scope. iModel serialization already
preserves its id: `to_dict` writes it (`lionagi/service/imodel.py:851`) and
`from_dict` reads it back (`lionagi/service/imodel.py:936`). Direct endpoints have
no durable resume promise. A model reused for independent conversations must be
invoked through separate Branches or separate iModel identities.

Capture the Branch scope in private APICalling runtime state **before enqueue**;
do not discover it from a context variable in the executor. Explicitly restore
that captured scope during transport. A prebuilt event with a different scope is
refused; an unscoped one captures the caller's scope before enqueue. Concurrent
Branches sharing an endpoint
must never update an endpoint-wide selected account. The binding survives idle
time, month rollover, errors and process restart on the same ledger. An absent or
blocked bound account refuses; it is never rebound automatically.

New scopes select and reserve their first attempt in one D4 transaction. Candidates
are configured, unblocked accounts with room for that attempt. Order is:

```text
( (settled_nusd + open_ceiling_nusd) / budget_nusd,
  number_of_distinct_scopes_with_open_attempts, account_name )
```

Compare ratios by integer cross multiplication, not float. All accounts use the
same UTC calendar month; subtracting the same elapsed-month fraction gives the
same order, so this is “furthest behind pace” without division by zero at reset.
Unknown attempts remain open for both ranking and admission. Idle bindings do
not count as in flight. No capacity means no binding and no send. A first chosen
account with an unresolved key refuses that call; resolution never hunts through
other candidates. This intentionally favors attribution over automatic recovery.

MAY carry non-secret scope/account ids; MAY NOT select per HTTP retry or globally
per shared model. Verify shared-model concurrency, clone, resume, parse and month
rollover with different budgets and exact tie cases.

### D4 — A local ledger with atomic admission

Use `credential-ledger.sqlite3` beside the global `settings.yaml`, through stdlib
SQLite. This is private provider accounting, not a state-store call/event schema;
it writes no new columns or rows to StateDB. ADR-0125's existing state read
contracts remain unchanged. One canonical location serves all processes of the
same OS user; separate users, copied homes and network filesystems are outside
this coordination contract.

The private version-1 shape is:

```text
account(provider, name, key_env, block_reason?)             PK(provider,name)
month(provider, name, month_utc, budget_nusd)               PK(provider,name,month_utc)
binding(scope_kind, scope_id, provider, account)            PK(scope_kind,scope_id,provider)
attempt(id, scope_kind, scope_id, provider, account, month_utc,
        ceiling_nusd, price_revision, requested_model, request_digest,
        state: open|settled, cost_nusd?, usage_json?, request_id?,
        outcome?, created_at, settled_at?)                  PK(id)
adjustment(id, kind: correction|reconciliation|unblock, attempt_id?, provider, account, month_utc?,
           delta_nusd, source_ref, created_at)              PK(id)
```

Foreign keys bind attempts to accounts/months; settled requires a nonnegative
integer cost, open requires null cost. Only sanitized numeric usage and a validated
bounded provider request id are stored, never prompts, request bodies or headers.
`request_digest` hashes the final credential-free request bytes for identity checks.
Amounts/sums must fit signed 64-bit integers; overflow refuses. Schema version is
stored using `user_version`; unknown versions or corrupt data refuse, never reset.

- Create the private directory/file with user-only access. Use SQLite's own file
  locking, `BEGIN IMMEDIATE`, foreign keys and `synchronous=FULL`; use rollback
  journal mode. Never replace/delete a live ledger or lock a replaceable inode.
  Lock acquisition has a five-second bound, then `ledger_busy` with no send.
  Run blocking database work off the event loop; hold no transaction during key
  lookup, HTTP, backoff or user hooks.
- The first admitted attempt fixes that account's month's budget. A process with
  a different budget or a changed reference refuses `account_config_conflict`.
  New months may have new budgets; old rows and ceilings never disappear. Account
  removal excludes new work but never deletes history or a binding.
- Admission includes any signed, sourced adjustments in settled spend, checks
  nonnegative totals, checks the account mark and existing binding, checks D5's
  inequality, inserts a fresh attempt and any new binding, then commits before
  transport. No read-check-write sequence spans transactions.
- `attempt_id` is minted per physical request. A duplicate reserve is rejected,
  never another authorization to send. The returned runtime ticket is single-use;
  restart never replays an open attempt. Even a crash just before dispatch keeps
  its ceiling until positive evidence resolves it.
- Settle by attempt id and immutable account/month, not current scope or current
  month. Matching repeated settlement is a no-op; differing final evidence raises
  `settlement_conflict`, blocks that account, and retains the original row.
  Reconciliation is an explicit sourced adjustment, never a silent rewrite.

The internal ledger operations are:

```text
Scope = (kind: branch|imodel|endpoint, id: UUID, provider: str)
Quote = (requested_model: str, ceiling_nusd: int, price_revision: str,
         request_digest: str)
Reservation = (attempt_id: UUID, scope: Scope, account: str,
               month_utc: YYYY-MM, quote: Quote)
Evidence = complete_usage(served_model, numeric_usage, request_id)
         | proven_not_sent | unknown(reason_code)
reserve(scope, attempt_id, quote) -> Reservation
settle(attempt_id, evidence) -> settled|already_settled|still_open
reconcile(adjustment_id, attempt_id, cost_nusd, source_ref)
adjust(adjustment_id, account, month, delta_nusd, source_ref, attempt_id=None)
clear_block(adjustment_id, account, source_ref)
```

Only complete usage or transport-owned proof of no dispatch closes a row;
`unknown` records a reason without changing its reservation. Ledger identity and
the captured quote, rather than fields supplied by a response, choose the charge
account and month. A quote's validity is checked again inside admission.

`reconcile` atomically closes an open attempt at the supplied nonnegative cost
and inserts a zero-delta reconciliation row: the cost enters S **once** through
the attempt. `adjust` changes S by its signed delta without rewriting or closing
an attempt. An adjustment/settlement taking S + R above B records the truth and
blocks further admission. `clear_block` inserts a zero-delta unblock row with no
month; restart never clears marks. Duplicate adjustment ids are no-ops
only for identical inputs. Later conflicting evidence needs a new sourced
correction; neither it nor reconciliation silently replaces a settled cost.

MAY migrate this private file under its own version gate; MAY NOT create a second
ledger on lock/I/O failure or infer zero from a missing response. Verify using two
processes, fault injection at commit/send/settle, and duplicate/conflicting ids.

### D5 — Price a ceiling before every attempt, settle from complete usage

Money uses integer nanodollars (`1 USD = 10^9`), never binary float. A provider-owned,
dated price revision specifies exact model ids, accepted request features, maximum
billable input tokens, maximum output tokens, and tiered rational nanodollars per
token for fresh input, 5-minute write, 1-hour write, read and output. It includes
source URLs and the date the prices were read; a model or request feature the
revision does not price refuses. A revision carries no expiry date (see Alternatives).
Persist the revision with each attempt and keep it available for later settlement.
Never select a rate by substring or assume an alias still denotes the same model.

Initially support ordinary Messages requests with text, base64 images, client tools
and supported cache TTLs; streaming uses the same cost contract. Server tools,
batch/fast/priority modes, context editing, external file inputs and unpriced beta
features are DEFERRED and rejected in routed mode before dispatch. The price entry
must cover the complete accepted request, including any model tier or multiplier.

Let `Imax` be that model/feature profile's documented maximum billable input,
`Omax` the actual final request's positive `max_tokens` (within the model's output
limit), `Pin` the greatest of all applicable input/cache rates across all tiers,
and `Pout` the greatest applicable output rate. Reserve:

```text
C = ceil(Imax * Pin + Omax * Pout)               # integer nanodollars
S = settled costs + sourced adjustments
R = sum(ceiling for every open attempt in the account/month)
admit iff account is usable AND S + R + C <= B   # equality is allowed
```

The full model bound is intentionally conservative: a small request near the
budget may refuse although its eventual bill would fit. lion1's byte-count bound
(lion1 ADR-0028 S15) is not a proof; the provider also describes `count_tokens` as an
estimate, not an upper bound ([token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting)).
A tighter ceiling is DEFERRED until a provider-supported upper-bound contract
exists for the accepted inputs. A guessed safety percentage does not meet it.

Price final usage at the captured revision and the returned exact model:

```text
A = ceil(input_tokens * Pbase + cache_read_input_tokens * Pread
       + write_5m_tokens * P5m + write_1h_tokens * P1h + output_tokens * Pout_actual)
```

These are the actual served model's rates at its applicable tier, not the maxima
used to reserve C. Reject unknown rate modifiers instead of guessing a tariff.
All counts are nonnegative integers, excluding booleans. Cache writes must split
exactly to `cache_creation_input_tokens`; an absent split is derivable only when
the final request unambiguously uses one TTL. Absent cache fields mean zero only
where that API version documents omission as zero. Otherwise missing/inconsistent
usage, an unknown served model or a new billing dimension keeps the ceiling and
cost unknown. Input tier selection uses fresh + read + created input; output
already includes thinking tokens, which must not be added a second time.
The pricing categories follow [provider pricing](https://platform.claude.com/docs/en/about-claude/pricing).

Settlement replaces C by A in one transaction. If A exceeds C, record the true A
and block the account with `ceiling_exceeded`; never clamp the bill. Admission's
hard invariant is over this local, version-priced ledger. Keeping real charges
within B additionally assumes the provider respects the documented token/rate
bounds and all spending on the allocation goes through this ledger. External
spend, price changes, taxes and credits are not discovered by this router.

Each attempt's month is the UTC month sampled inside its admission transaction.
Settlement after midnight stays in that month; a retry admitted after midnight
has a new row in the new month. This is a local admission-month policy, not a
claim about provider invoice allocation. UTC clock regression across a recorded
month boundary refuses until corrected; deleting history is never clock repair.

MAY settle before response parsing or hooks finish; MAY NOT use approximate
`APICalling.required_tokens` or Branch aggregate usage for money. Verify integer
boundaries, cache tiers/splits, unknown usage, unpriced models and month rollover.

### D6 — Failure, cancellation and the unchanged default

The router wraps each physical HTTP attempt in the existing Anthropic endpoint;
it does not wrap only `iModel.invoke` or duplicate the provider implementation.
Both native and `RetryConfig` retry paths must cross reserve/settle once per send.
Final payload validation and quoting happen after all request mutations, including
hooks; freeze the bytes so the quote and wire request cannot diverge.
Cancellation of a waiter is not proof that its executor/database worker stopped.
The owner revokes the one-use dispatch ticket and joins or accounts for that
worker before claiming “not sent”; an uncertain handoff keeps the ceiling.

| Outcome | Ledger and account action | Retry |
|---|---|---|
| Local validation/lookup/admission refusal | No send; no cost. If already reserved, settle zero only with transport-owned proof it was never dispatched. | None. |
| Complete priced response, even if parsing/hook later fails | Settle from usage before outward completion. | Never retry merely because accounting or a consumer failed. |
| 401/403 | Persist `auth_required` before propagation; keep ceiling absent complete billing evidence. | None; never another account. |
| 429 | Classify rate limit, not monthly exhaustion; keep ceiling without complete billing evidence. | Existing bounded backoff on the same account, with a new reservation. |
| 5xx, timeout, transport loss | Unknown cost keeps ceiling; no quota/auth mark inferred. | Existing bounded retry policy, same account and a new row. |
| Other 4xx | Classify request rejection; keep ceiling without complete billing evidence. | None. |
| Cancellation, process death or stream truncation | Settle any complete evidence already captured; otherwise retain ceiling. | No cancellation replay. |

Status alone is not proof of a zero bill. Streams merge the initial usage and
later cumulative usage fields by replacement, not addition, and settle only with
complete terminal evidence. HTTP usage is intercepted before generic stream
projection and before yielding to consumers; the current generic projection keeps
raw events (`lionagi/service/connections/endpoint.py:1143`). No automatic retry after
stream output starts. Stream close/finalization shields an available settlement
from caller cancellation; the shield waits at most five seconds for a worker thread
token (admission) and at most five seconds for the SQLite lock (`busy_timeout_s`), a
worst case of ten seconds, and a timeout in either wait raises `ledger_busy`
(Amendment 6). If commit
fails or the process dies, the durable ceiling remains. It must never report the
accounting operation as committed when it was not.

Routed errors expose only `code`, optional validated account/attempt ids and HTTP
status through the existing event/exception paths. Required codes are
`credential_missing`, `credential_invalid`, `credential_lookup_failed`,
`credential_conflict`, `unsupported_pricing`, `budget_exhausted`, `account_blocked`,
`account_config_conflict`, `ledger_busy`, `ledger_unavailable`, `duplicate_attempt`,
`settlement_conflict`, `ceiling_exceeded`, `auth_failed`, `rate_limited`,
`request_rejected`, `provider_error` and `transport_error`. No raw provider message is required.
The ledger is the authority on routed spend; successful user-facing result
types, response bodies and normal chunk shapes stay unchanged apart from secret
redaction. Failure accounting must remain queryable even without an outer result.

The unconfigured path bypasses all router resolution, ledger I/O, price checks,
marks and new error handling. It retains current direct-key and unresolved-name
semantics, retry/cache behavior, return types and serialization. Tightening
`EndpointConfig._validate_api_key` globally is explicitly not authorized here.
Configured routing disallows the endpoint's application response cache initially:
its current wrapper receives headers (`lionagi/service/connections/endpoint.py:732`),
and making that cache account-aware is DEFERRED. Provider prompt caching remains
supported and is distinct from that application cache.

MAY add private account/quote/ledger seams usable by future key providers; MAY NOT
add a router service, required SDK, second endpoint registry, public Branch/Session
parameter, implicit failover or placeholder provider implementations. Verify every
existing unconfigured path unchanged, plus direct endpoint, invoke, stream,
retry-config, prebuilt-event and cancellation routing without bypasses.

## Consequences

- Attribution and admission are deterministic across cooperating local processes;
  sticky conversations can exhaust one account while another has room. New work
  spreads by committed utilization, not by an assurance of equal daily spending.
- Unknown attempts and conservative ceilings reduce availability deliberately.
  A cancellation can remain unresolved indefinitely; explicit sourced
  reconciliation is required, never a timeout that invents a free request.
- Contributors maintain price revisions and feature bounds alongside Anthropic
  request/usage fixtures. At least one usable, fully priced model must ship with
  the router; an implementation that refuses every request is not completion.
- D1/D2 reverse cheaply before release. D3 affinity and D4 persistent identities
  require migration after use; removing the file is not a migration. D5's strict
  bound is expensive in utilization; relaxing it changes the budget contract.

## Alternatives considered

### Global provider block in settings.yaml or a separate accounts file

YAML is pleasant for many accounts and could prohibit project overrides. It loses
because API settings already own env/dotenv precedence, while reading agent YAML
adds I/O and parse failures to existing API construction. A separate accounts file
adds another hierarchy and discovery rule. AppSettings is less readable for large
lists but preserves one existing configuration entry point.

### Dedicated keychain reader or the existing full-environment filler

A direct `security` reader is smaller locally but duplicates timeout, cancellation
and trust policy. Filling every declared secret reuses code unchanged but reads
unselected keys and materializes a competing credential set. A shared one-name
runner retains the mechanism without either expansion.

### Per-call, per-model or per-Session assignment

Per-call selection responds fastest to utilization but crosses cache boundaries.
Per-model affinity needs less plumbing but shared models couple separate Branches.
A Session pin preserves broad affinity but concentrates independent conversations
on one budget. Branch identity is the existing conversation boundary; standalone
model identity is the explicit lower-level fallback.

### State-store tables or a JSON ledger under a file lock

StateDB would reuse migrations and expose accounting to its readers, but makes an
optional provider feature a published state schema and persistence dependency.
JSON plus a stable sibling lock is viable and human-readable; whole-file rewrites,
crash-safe replacement and platform lock handling buy nothing over stdlib SQLite's
transactions. This file owns only the router's accounting, not conversation state.

### Estimated prompt ceilings, provider counts or settlement at run completion

Byte counts (lion1's bound) and provider counts reserve less money and improve
parallelism, but neither supplies the needed proven upper bound: a byte bound
assumes every token covers at least one byte of the body, which holds for a
byte-level tokenizer on text but is not a provider guarantee and does not cover
images, and the provider describes its count as an estimate. Run-end accounting is cheaper but
lets concurrent or crashed runs spend unreserved money. Per-attempt ceilings with
the maximum supported model input bound make that tradeoff explicit.

### Price revisions that expire

An exclusive `valid_until` date that refuses an expired revision would stop the
router from pricing with a table the maintainers no longer vouch for. It lost
because the table ships inside a release: every routed call of an installed
version would start refusing on a calendar date, with no change on the user's
side, and the budget invariant already excludes price changes the router cannot
see (D5). Keeping the table current is release maintenance; a revision records
when its prices were read so a reader can judge its age.

### Automatic failover, expiring unknown attempts or status-based zero costs

All improve availability, but a lost request may already be billed. Failover can
duplicate work on another credential; expiry or assumed zero restores money with
no evidence. Explicit failover is DEFERRED: a future caller-visible operation must
create a new conversation/binding, retain old reservations, and acknowledge
possible duplicate effects. It must never be implemented as a hidden retry.

### Broad first release

CLI accounts, live provider invoice reads, custom reset dates, administrative
screens, dynamic reload and other providers would need additional transport,
trust or lifecycle contracts. They are DEFERRED. Other key providers may later
implement the same quote/reserve/settle semantics with their own pricing and
response evidence; no generic provider plug-in system is introduced now.
