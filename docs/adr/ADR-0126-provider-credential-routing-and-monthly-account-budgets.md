# ADR-0126: Provider credential routing and monthly account budgets

- **Status**: Accepted, amended 2026-10-14, 2026-10-26, 2026-10-28, 2026-10-30, 2026-11-01, 2026-11-02 and 2026-11-09 (see Amendments 1 to 10).
- **Kind**: Aspirational
- **Implementation-status**: partial — D1 to D5 and Amendments 1 to 8 are implemented, and so are
  D6's failure classes and shielded settlement. Within that accepted scope, two clauses remain. D4's single-use runtime ticket:
  the reserve call returns a plain reservation. The ledger refuses a duplicate attempt id and settles an attempt once (an identical repeat changes nothing, a different one blocks the account in this implemented baseline), but nothing is consumed when the request is dispatched. D6's revoke-and-join before "not sent": the not-sent settle is chosen by whether the request was entered, with no ticket to revoke and no join of the worker. Amendment 9 is unimplemented; the preceding implementation claims apply through Amendment 8 only. The required Amendment 9 runtime-after-reconciliation no-op is a new exception, not implemented by that baseline.
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
fill that runs on every CLI spawn (`fill_declared_secrets`, `lionagi/service/accounts/_secret_resolution.py`),
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
matched, and never repeats the value. Unset, the run records no `api_account`, as before. A run
whose own chat endpoint sends through the account router (D1 to D6: the Anthropic API endpoint with
accounts configured) records none either, with the variable set and valid: the router chooses the
account for each attempt and ignores the variable, so the field could name an account that paid
nothing. For that run `account_labels` and each attempt's `account_label` in the API usage record
name the account that did. A run whose endpoint routes nothing, such as a CLI run or an API run
with no accounts configured, records the name as before.

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
variable or a dotenv entry that is neither holds the same one. It also pins that a routed API run
with the variable set records no `api_account` while its API usage record names the account that
paid, and that an API run with no accounts configured records the name.

## Amendment 5 (2026-10-30) — a new conversation goes where its stable prefix is warm

Purpose: raise the prompt-cache hit rate of a process that starts many short conversations sharing one system prompt; the provider's usage report for such a workload showed a hit rate of at most 28% (2026-10-27).

D3 binds each conversation to one account for its whole life, because the Claude API keeps a
separate prompt cache for each organization (P3). D3 orders a NEW scope's candidates by utilization
alone (`_ratio_order`, `lionagi/service/accounts/_ledger.py::CredentialLedger._ratio_order`, applied at `lionagi/service/accounts/_ledger.py:548@c6ac2d97`)
(line numbers at `c6ac2d97400fe0ed480b942c945570e197cdfcc8`), without regard
to cache warmth. A process that starts many short conversations sharing one system prompt therefore
lands each new conversation on whichever account is furthest behind its budget pace. The first
request of that conversation then writes the stable prefix again on that account, even when the
same prefix was read on another account seconds earlier.

Measured on one long-running service that starts a new Branch for each unit of work and shares a few
system prompts across them. The records are its own request log; the account a call used is the
cache record's `route`, the last of the call's attempts
(`lionagi/service/_prompt_cache.py::settle`). The prefix is identified by the certificate of the
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
   (`lionhub/src/lionhub/providers/anthropic/_prompt_cache.py`), of the last markable tools-tier or system-tier
   block (`Point.markable`, `lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::points.add`: not blank text, not thinking) that carries a
   one-hour mark (`cache_control` with `"ttl": "1h"`) in the body that is sent. It
   names the longest one-hour entry the tools and system tiers of that body can hold.
   - With the adapter's own placement that block is normally the stable one. The adapter writes
     the system tier as blocks (`lionhub/src/lionhub/providers/anthropic/messages.py::_place_prompt_cache`) and `place()`
     puts the stable one-hour mark on the last markable block outside the messages (`stable`,
     `lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::place`, marked at `lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::place`).
   - An earlier one-hour mark in those tiers (a caller's own, or the placement's read or probe of
     an older one-hour entry, `lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::place`) ends a shorter entry. When the stable block also carries a one-hour mark,
     the stable block is the key. When the stable block's mark is refused or kept at five minutes
     (`ttl_order:stable`, `lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::place`) or there is no mark left for it (`MAX_MARKS`, `lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::place.free`,
     `lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::place`), the last earlier one-hour mark is the key. That is still the entry the provider holds
     for an hour.
   - A request with no one-hour mark in its tools or system tiers has no certificate and takes no
     part in this amendment: it neither reads nor writes the index.
   - `router.begin` computes it from the final payload it freezes, the dict `request_ttls` reads
     (`lionagi/service/accounts/_router.py::AccountRouter.begin`), after parsing the frozen bytes back (next
     bullet but one). It never comes from the placement record,
     so a direct endpoint, a prebuilt event and a body a hook changed after placement are keyed by
     the body that is sent. A standalone call takes part only when that body carries a one-hour mark
     in its tools or system tiers. A string system prompt carries no mark, so such a call is keyed
     by a one-hour mark on its last tool or not at all.
   - `begin` also records `hour_after_key`: whether any block after the key block carries a
     one-hour mark at any depth: on the block itself, or on any object its content reaches through
     lists and dicts (a `tool_result`'s content, a `document` block's `source.content`, a
     `search_result`'s content). `begin` walks the parsed body for it. `Point.nested` reads one
     level of `content` only (`lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::_nested_marks`) and is not used for the flag. Who placed
     the mark does not matter: a caller, or the placement's read or probe of an older one-hour entry
     (`lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::place`). It is also true when the sent body carries a top-level `cache_control` with
     `"ttl": "1h"` and a markable block follows the key block: the provider's automatic caching then
     puts a one-hour breakpoint on the last cacheable block, after the key block. When the key block
     is the last markable block, that breakpoint is at the key block and the flag stays false. The
     adapter never sends that field itself (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint.create_payload`), so only a
     direct or prebuilt body, or a hook, carries it. Decision 3 uses the flag.
   - `begin` computes the key and `hour_after_key` from the frozen bytes parsed back
     (`json.loads`), not from the dict. `points()` reads only lists as content
     (`lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::_content_blocks`, `lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::_nested_marks`), and serialization turns a tuple into the array the provider
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
   CREATE INDEX IF NOT EXISTS warm_prefix_cert ON warm_prefix (provider, cert);
   CREATE INDEX IF NOT EXISTS warm_prefix_expiry ON warm_prefix (provider, expires_at);
   ```

   The certificate read (Decision 5) filters on `(provider, cert)`, which the key's leading columns
   do not cover, so without `warm_prefix_cert` it reads every row the provider holds.
   `warm_prefix_expiry` serves the purge in Decision 3.

   Every time the amendment reads or writes is parsed with `datetime.fromisoformat` and compared as
   an aware datetime in Python, never by SQLite date functions or by text order; the one exception
   is the purge in Decision 3, which only deletes and keeps a margin for it. The ledger's own
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
   `lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`), only when the evidence is `CompleteUsage` (D5) with cost at or under the
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
     A request with a server tool (`_server_tools`, `lionhub/src/lionhub/providers/anthropic/_prompt_cache.py::_server_tools`) also stores NULL:
     the provider sums usage over its internal iterations, so the sum is not a length. The
     prompt-cache logger is blind to those requests for the same reason
     (`lionagi/service/_prompt_cache.py::settle`).
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

   Before the upsert the write deletes the provider's rows whose `expires_at` sorts as text before
   the attempt's `created_at` less 25 hours, written as UTC `isoformat()` (`lionagi/service/accounts/_ledger.py::_record_warm_prefix`,
   the margin at `lionagi/service/accounts/_ledger.py::_WARM_PURGE_MARGIN`). An offset moves an `isoformat()` value's text by less than a day from its
   UTC time, so a row in that form is deleted only after it expired. A value in any other form was
   not written by the ledger, and a row deleted early loses only a routing preference. Without the
   purge the table would keep a row per account and certificate for every conversation it has seen.

   No other outcome writes a row: not a repeated or conflicting settlement, not a ceiling block, not
   an attempt settled without the certificate (an open attempt settled by another process), and not
   an attempt on an account blocked when it settles. A stable prefix below the provider's minimum
   cacheable length is never written for an hour by itself. It gets a row only when a one-hour
   mark after the key block wrote a longer entry. That write stores NULL, so no read refreshes the
   row unless an earlier write had stored a number. Each further deeper one-hour write on that
   account extends the row by another hour. For as long as those writes keep coming, the row steers
   new scopes there, with no cache benefit to them.
4. **Clearing.** In the transaction that records the outcome or an independent block, the ledger deletes every `warm_prefix` row for the provider and account when it records:
   - an `UnknownOutcome` on an open attempt with any reason but `cancelled` and `stream_closed` (the still-open branch, `lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`), whether or not an earlier reason already stands: `auth_required`, `rate_limited`, `provider_error`, `request_rejected`, `transport_error`, `usage_incomplete`, `stream_error`, `stream_truncated`; an automatic Amendment 9 rejection-zero settlement for 400 (`request_rejected`) or 529 (`provider_error`), or stream estimate whose original ending reason is `transport_error`, `stream_error` or `stream_truncated`. Neither new basis creates or refreshes warmth;
   - a `ProvenNotSent` the caller did not cause, settled in the open-to-settled branch
     (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`): an unresolved key in `begin` (`lionagi/service/accounts/_router.py::AccountRouter.begin`) and a transport failure
     before the request entered (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`, `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`). `ProvenNotSent`
     (`lionagi/service/accounts/_types.py::ProvenNotSent`) gains `caller_cancelled: bool = False`. It is
     set by the two cancellation handlers (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`, `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`) and by `begin` when the
     exception at `lionagi/service/accounts/_router.py::AccountRouter.begin` is the cancellation class or `GeneratorExit` (a consumer that
     closes its own stream during `begin`). The recorded outcome stays `not_sent`;
   - a block: `settlement_conflict`, including contradictory final evidence on an open attempt that leaves its row unchanged; `ceiling_exceeded`, including E > C in runtime settlement or typed reconciliation; and every explicit `block()` (`lionagi/service/accounts/_ledger.py::CredentialLedger._block_sync`), `auth_required` included;
   - `clear_block()` (`lionagi/service/accounts/_ledger.py::CredentialLedger._clear_block_sync`) on an account whose `block_reason` is not NULL when that transaction
     reads it. A process without this amendment can block an account without deleting its rows, so
     the re-admission deletes them. A `clear_block()` on an account that is not blocked deletes
     nothing, and a repeat of an adjustment already recorded returns before that read (`lionagi/service/accounts/_ledger.py::CredentialLedger._clear_block_sync`) and
     deletes nothing.

   Complete settlement preserves warmth unless it records a block. Cancellation, consumer close and caller-cancelled not-sent (`ProvenNotSent(caller_cancelled=True)`) say the caller ended the request and nothing about the account; they preserve warmth only in the absence of a block; E > C therefore clears even on a cancelled or closed stream. A consumer closing after an error event has original reason `stream_error`, not `stream_closed` (item 8), in its open outcome or estimate evidence, and clears. Reconciliation (`_reconcile_sync`, `lionagi/service/accounts/_ledger.py::CredentialLedger._reconcile_sync`) does not clear merely for pricing an old attempt, but an Amendment 9 reconciliation block clears. An identical repeat or reason-only write on a settled row returns before any write (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`) and clears nothing; runtime evidence on a reconciled row is also ignored without blocking. Other conflicting final evidence follows Amendment 9's conflict rules. A repeated `ProvenNotSent` whose
   `caller_cancelled` differs from the first reads as identical, because the flag is not stored. It
   returns `already_settled` and clears nothing.

   The clear has to cover more than blocks. HTTP 401/403 still blocks with `auth_required` (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`, `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`); accounting blocks `ceiling_exceeded` and `settlement_conflict`, and explicit operator blocks, also block an account and clear warmth. A rate-limited account, or one whose credit is exhausted (a 400), stays a
   candidate. Without the clear, its row would keep it first in W for every new scope with that
   prefix, and a bound scope never moves. The clear removes that preference and nothing else: D3's
   order still binds the failing account when it ranks first, as it does today. ProvenNotSent and qualifying typed rejections settle zero; eligible early-ended streams settle E; unresolved ambiguous attempts retain their ceilings. Thus a failure need not move an account down that order, including repeated zero rejections. A request already in flight when
   its account failed, whose complete settlement with a one-hour write commits after the failure's,
   writes a row again. That settlement is evidence the account served the request, not that it
   served it after the failure: the provider can have answered it first, and only the ledger commit
   came later. The row then steers new scopes back to the account until the next failure there
   clears it. A completion that only reads restores nothing, because a read
   refreshes a row that exists and never creates one.
   Clearing more than needed (a second reason on one attempt, an older failure deleting a fresher
   row) costs only writes.
5. **Selection for a new scope.** The candidate set is unchanged: configured, unblocked accounts
   with room for the attempt (`_fits`, `lionagi/service/accounts/_ledger.py::CredentialLedger._fits`).
   - Let W be the candidates holding a row for the request's certificate whose `expires_at` is later
     than the reservation's now, and whose committed spend including this attempt satisfies
     `10 * (spent + opened + ceiling) <= 9 * budget`. Equality is inside W. The values are the
     Python integers `_fits` uses: `spent` and `opened` from `_spend_parts` (`lionagi/service/accounts/_ledger.py::CredentialLedger._reserve_sync`), the quote's
     ceiling, and `spec.budget_nusd`, which equals the month row whenever one exists (`lionagi/service/accounts/_ledger.py::CredentialLedger._reserve_sync`).
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
6. **Preserved rules.** Amendment 5 changes the settlement shields, cancellation delivery and stream-close reason described in item 8. Amendment 9 additionally changes evidence-based closure, diagnostic capture and the clauses explicitly replaced there; the binding and selection boundaries below continue to apply. All other D3/D4 clauses remain in force unless an explicit Amendment 9 replacement changes them: fixed bindings, per-conversation selection, no fallback on unresolved credentials, integer ranking, atomic reservation, immutable attempt identity and sourced corrections remain. No blanket preservation claim overrides the new quote, evidence, reconciliation or block rules.
   - A bound scope keeps its account and is never rebound. An absent or blocked bound account
     refuses. A bound scope's reservation does not read the index.
   - Selection never happens per HTTP retry or per shared model.
   - A first chosen account with an unresolved key refuses without trying another.
   - D3's order itself is unchanged: integer cross-multiplication of committed utilization, then
     distinct scopes in flight, then account name. Unknown attempts remain open for ranking and admission only until authorized Amendment 9 evidence or sourced reconciliation closes them; recorded settled cost then replaces the ceiling in utilization.
7. **Schema.** `user_version` stays 1. The table is not added to `_TABLES`.
   - The unlocked `_ensure_schema` fast path requires version 1, `_TABLES | {"warm_prefix"}`, both warm-prefix indexes (`warm_prefix_cert` and `warm_prefix_expiry`), both rejection columns and `settlement_evidence_json`.
   - The locked version-0 branch, used by a fresh file, creates the extension table and indexes beside the base tables, and all required columns, before `PRAGMA user_version = 1` in the same transaction.
   - The locked version-1 branch re-reads version, tables, indexes and columns, requires `_TABLES`, then runs `CREATE TABLE IF NOT EXISTS`, both `CREATE INDEX IF NOT EXISTS` statements and adds missing extension columns before returning. A table predating the indexes therefore gains them. A version-1 file missing a base table still refuses with `LEDGER_UNAVAILABLE`. These extend the existing checks (`lionagi/service/accounts/_ledger.py::CredentialLedger._ensure_schema`); the table remains created in the same transaction as the five D4 tables.
   - Every connection `_connect` returns must have the warm-prefix table and indexes, rejection columns and Amendment 9 evidence column. Connections remain writable; the first open after an upgrade creates missing extensions under the schema lock, so readers need no new filesystem write privilege. This extends the existing `_connect`/schema call at `lionagi/service/accounts/_ledger.py::CredentialLedger._connect`.
   - An older lionagi opens a file that has the table, because its check admits a superset of
     `_TABLES`, and it neither reads nor writes it. Its failures therefore clear nothing (see the limits below). The older-reader statement describes the pre-warmth extension only. Amendment 9 compatibility is limited to the writer examined in Decision 6.
   - The index is never required: an empty table is the pre-amendment behaviour, and no reservation
     refuses because of anything in the table or missing from it.
8. **Operations.**
   - D4's `reserve(scope, attempt_id, quote, accounts)` becomes `reserve(scope, attempt_id, quote, accounts, *, cert=None)`; certificate selection reads the index when inserting the scope binding (`lionagi/service/accounts/_ledger.py::CredentialLedger._reserve_sync`). The public signature is at `lionagi/service/accounts/_ledger.py::CredentialLedger.reserve`. Reservation's outer fields and positional construction stay unchanged, but its nested quote gains `pricing_inputs`, persisted atomically before dispatch. No new-scope flag is added; the read arm remains scoped to CompleteUsage for both new and bound scopes.
   - `settle(attempt_id, evidence)` becomes `settle(attempt_id, evidence, *, cert=None,
     hour_after_key=False, server_tools=False)`; it writes (Decision 3) and clears (Decision 4).
     `hour_after_key` is the flag from Decision 1, and `server_tools` is true for a request that
     carries a server tool.
   - `RouterAttempt` (`lionagi/service/accounts/_router.py::RouterAttempt`, `@dataclass(slots=True)`, built positionally at `lionagi/service/accounts/_router.py::AccountRouter.begin`)
     gains `cert: str | None = None`, `hour_after_key: bool = False` and
     `server_tools: bool = False` after its four fields, so positional construction is unchanged.
     `begin` computes `cert` and `hour_after_key` from the frozen payload (`lionagi/service/accounts/_router.py::AccountRouter.begin`) and
     `server_tools` from the same parsed body through `_server_tools`, passes `cert` to
     `ledger.reserve` (`lionagi/service/accounts/_router.py::AccountRouter.begin`) and builds the attempt with all three (`lionagi/service/accounts/_router.py::AccountRouter.begin`). Only the two settlements that can carry `CompleteUsage` pass them on to `ledger.settle`: `settle_response` (`lionagi/service/accounts/_router.py::AccountRouter.settle_response`) and `settle_stream_end` (`lionagi/service/accounts/_router.py::AccountRouter.settle_stream_end`). The new `settle_rejection` forwards none of `cert`, `hour_after_key` or `server_tools`: rejection zeros never write warmth. `settle_unknown`
     (`lionagi/service/accounts/_router.py::AccountRouter.settle_unknown`) and the not-sent paths pass neither; nothing is lost, because only a
     `CompleteUsage` settlement writes a row and a clear needs only the account.
   - `ProvenNotSent` gains `caller_cancelled` (Decision 4). `AccountRouter.settle_reservation_not_sent`
     (`lionagi/service/accounts/_router.py::AccountRouter.settle_reservation_not_sent`) gains `*, caller_cancelled: bool = False` and builds its `ProvenNotSent`
     with it. `settle_not_sent` (`lionagi/service/accounts/_router.py::AccountRouter.settle_not_sent`), which delegates to it, gains the same keyword and
     forwards it. The adapter's two cancellation handlers pass `True` at their `settle_not_sent`
     calls (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`, `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`). `begin`'s bare `except BaseException` (`lionagi/service/accounts/_router.py::AccountRouter.begin`) binds
     the exception and passes `True` when it is the cancellation class or `GeneratorExit`, `False`
     otherwise. Its `AccountRoutingError` handler (`lionagi/service/accounts/_router.py::AccountRouter.begin`) passes nothing.
   - `AccountRouter.settle_response` (`lionagi/service/accounts/_router.py::AccountRouter.settle_response`), `AccountRouter.settle_unknown` (`lionagi/service/accounts/_router.py::AccountRouter.settle_unknown`) and `AccountRouter.block` (`lionagi/service/accounts/_router.py::AccountRouter.block`) run their ledger write under `CancelScope(shield=True)`, as `settle_reservation_not_sent` and `settle_stream_end` already do (`lionagi/service/accounts/_router.py::AccountRouter.settle_reservation_not_sent`, `lionagi/service/accounts/_router.py::AccountRouter.settle_stream_end`). The response path records `usage_incomplete` (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`) and keeps that row open (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`); complete usage through `settle_response` (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`) can record `ceiling_exceeded` and clear warmth (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`); it retains the complete settlements D6 settles under cancellation. Amendment 9 adds shielded `settle_rejection` and stream estimates through `settle_stream_end`; D6 cancellation can now record those estimates, with E > C blocking. A worker write is scheduled on a thread; the record’s guarantee remains that an unshielded write waiting for admission while AnyIO cancellation is pending never starts. These shields preserve the accounting attempt, subject to Amendment 6's separate bounded waits and ledger failure, rather than promising successful commit.

     Before this amendment, when a 401, 403, 429, 5xx or other non-200 status was being recorded
     (`lionagi/providers/anthropic/messages.py:511-541@04e03ffe`, `lionagi/providers/anthropic/messages.py:981-1011@04e03ffe`, line numbers at `04e03ffef292f1dad08549cdbf23b53f9b1d5644`),
     the adapter's cancellation handler then recorded `cancelled` instead: no clear, no block for a
     401 or 403 whose cancellation was already pending when `block` was called, and a held ceiling
     for a 200 whose settlement never started. A cancellation that arrived while `block` ran let the
     block commit and stopped `settle_unknown`. With the shields, both writes commit.
   - The adapter delivers a pending cancellation after the failure is recorded. The routed-failure handlers call `await anyio.lowlevel.checkpoint()` before re-raising (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`, `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`), as do the non-streamed and streamed outer transport paths (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`, `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`). Amendment 9 moves latched mid-stream transport accounting before output drain and context exit: `settled=True` precedes its shielded write, followed by a cancellation checkpoint. The outer guard then skips a second write and still checkpoints before raising if reached. Cancellation at the new checkpoint re-raises from the cancellation handler, so it need not reach the old outer checkpoint. Without the applicable post-write checkpoint, a pending `fail_after` can receive the routing error instead of `TimeoutError`, and a status-code branch delivers pending cancellation only if closing the HTTP session checkpoints. Reason-only writes retain the first open reason or ignore a settled row (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`). Final 400/529 evidence after a committed cancelled reason instead conflicts, retains the row and blocks/clears. Runtime evidence after reconciliation is ignored. Authentication writes still block first (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`, `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`); both calls are shielded, but a ledger error in the first stops the second and leaves the attempt ceiling held. Rewrite the comments at `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt` and `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt` to state this shielding and block-first order.
   - The shields hold against AnyIO scope cancellation only. A native `task.cancel()` is not held: pending before worker admission it stops the write; during the worker wait it returns to the caller at once and the write commits after. The worker-dispatch boundary is `lionagi/service/accounts/_ledger.py::CredentialLedger._run`; the cancellation handler’s settlement write is at `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`. A ledger write that fails still records nothing. This limitation applies to response, unknown, block, not-sent, stream-end and rejection settlement alike. Amendment 6 bounds each call’s thread-token admission wait and SQLite lock wait (`busy_timeout_s`) at five seconds each; the former unbounded admission wait is historical. No combined timeout may report failure after an actual commit.
   - The streamed adapter’s `GeneratorExit` handler (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`) selects original reason `stream_error` after an observed error event (`saw_error_event`, `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt.emit_ready`) and `stream_closed` otherwise. Its end-of-stream branch selects `stream_error` after an error event and `stream_truncated` otherwise (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`). Under Amendment 9 Decision 2, an eligible estimate stores that reason in structured evidence; otherwise `UnknownOutcome` records it on the open row.
   - In the streamed attempt the existing `settled` boolean is the settlement-attempt flag. Set it before awaiting each router accounting call (the existing status branches, `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`, and `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt.emit_ready`), including Amendment 9's latched transport-loss settlement before output drain or context exit. The outer transport-failure handler (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`) skips accounting when the flag is true but still propagates the failure with a cancellation checkpoint if reached. A failed call is not retried by unwind. Without the flag
     and the guard, a `GeneratorExit` or an exception that the unwinding response or client raises
     after a settlement would lead the close handler or the transport branch to settle a second
     time. After complete usage, an Amendment 9 final settlement, or reconciliation, a later reason-only write returns `already_settled` and changes nothing. On an attempt still open (`usage_incomplete` or a nonqualifying status outcome), another clearing reason can clear warmth again, including a row another request wrote between calls. Amendment 9's final-basis consistency and reconciled-row precedence govern final evidence, not reason-only writes. A settlement whose ledger write raises leaves the attempt open for reconciliation, as any unsettled attempt is. Ordinary sourced reconciliation remains available for any open month; only Amendment 9 typed closure has the current-month eligibility fence.
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
  `saw_error_event`, which is set only once the adapter has processed the error event. An error event the redactors still hold when the consumer closes, or a cancellation that replaces `GeneratorExit` while the stream unwinds, retains original reason `stream_closed` or `cancelled` in an open outcome or Amendment 9 estimate and preserves warmth unless a block is recorded. A frame flagged `call_failed` before `message_stop` makes a close before `message_stop` retain original reason `stream_error` in an open outcome or eligible estimate evidence, which clears the rows of an account that served the request even though the final outcome may be `stream_usage_estimate`. Apart from Amendment 9 estimate/block effects, these reason differences affect steering only: the next failure there clears a missed row, and the next one-hour write restores a cleared one.
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
  stable-length read on its account. A later reason-only failure after settlement, such as a transport error after a stream’s final usage, clears nothing (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`). A complete-usage settlement shows a response with priced usage, but settled state alone is not evidence that an account served the request: rejection zero and stream estimates are settled too.
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
- Not guaranteed: even spend across accounts inside the month. A prefix that carries most of a process's traffic stays on one account until the prefix leaves W there (the account fails, any block clears its rows, it passes the bound, or the row lapses) and another candidate ranks ahead of it under D3's order.
  Measured: one system prompt carried 58% of one process's 1,353 calls. Amendment 5 changes allocation, not provider pricing. Amendment 9’s estimates can overstate or understate provider spend; recorded totals are therefore not always the actual charges. D3's
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
  failure there. A re-admission after an accounting block that process set (`settlement_conflict`, `ceiling_exceeded`) deletes the account’s warmth regardless of whether it served a request. Separately, the writer at c6a3b4af already implements Amendment 5 and can clear warmth; it may conflict-block a replay against a new rejection-zero outcome. Decision 6 examines that writer only. It makes no compatibility promise for the pre-Amendment-5 process described above; an accounting block is not execution evidence.
- An outer timeout that cancels the task (`asyncio.timeout`, `anyio.fail_after`) reaches the adapter as cancellation: before send it records caller-cancelled `not_sent`; after send the original reason is `cancelled` unless a shielded status/response settlement has already selected its evidence or transport failure was latched first. Those selected bases keep their precedence under Amendment 9 Decision 2 and the shielded-status verification. An eligible stream settles Amendment 9's estimate, otherwise the reason leaves its ceiling held. Neither clears warmth unless a block is recorded; E > C blocks and clears. A transport timeout inside the adapter on an unsettled attempt uses `transport_error` in unknown or estimate evidence and clears. A late reason-only timeout write after final settlement changes nothing. If transport failure was latched first, Decision 2 records transport_error before delivering the pending cancellation; it does not replace that reason with cancelled.

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
- **Clear on every outcome that is not complete, cancellation included.** Clearing merely for caller cancellation remains rejected: cancellation alone says nothing about an account, and cancelling losing requests could repeatedly wipe warmth written by successful ones. Amendment 9 makes an independent block an explicit exception: E > C or contradictory final evidence must clear even during cancellation. That conservative deletion can remove useful warmth and is accepted for accounting safety, not as proof the account failed.
- **Clear only on 429 and 5xx.** Rejected: an unresolved key, a 400 for exhausted credit, a transport timeout or failed stream can leave a warm preference on a failing account. Clearing removes that preference but does not remove the account from D3. Amendment 9 zero settlements may keep it first by utilization until an operator blocks it.
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
- Index and purge: the certificate read's query plan uses `warm_prefix_cert`. A write deletes the
  provider's row that expired 26 hours before the attempt and keeps one that expired 24 hours
  before it, another provider's old row and an unexpired row at `-10:00` whose text sorts about
  9.5 hours before the attempt.
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
  blocked deletes nothing. A consumer that closes the stream after an error event retains original reason `stream_error` in the open outcome or estimate evidence and clears; one that closes it without an error event retains original reason `stream_closed` and preserves warmth unless blocked. A
  transport failure after `settle_stream_end` recorded complete usage clears nothing. With an AnyIO scope cancellation (`fail_after`) pending when the adapter records a 429, the attempt keeps `rate_limited`, the rows are cleared and the caller receives `TimeoutError`; pending at a 401, the account is blocked, the attempt keeps `auth_required`, the rows are cleared and the caller receives `TimeoutError`; pending at a qualifying 529, the attempt settles `rejection_zero` with `provider_error` in structured evidence, otherwise a 5xx keeps `provider_error`; pending at a qualifying 400, the attempt settles `rejection_zero` with `request_rejected` in structured evidence and its two rejection columns, otherwise keeps open `request_rejected`; in each case warmth is cleared and the caller receives `TimeoutError`; pending at a transport failure after the request entered, the attempt keeps `transport_error` in the open outcome or eligible stream-estimate evidence, the rows are cleared and the caller receives `TimeoutError`; pending at a non-streamed 200 whose usage is incomplete, the attempt keeps `usage_incomplete` and the rows are cleared; pending at a 200 that prices over the ceiling, the account is blocked with `ceiling_exceeded` and the rows are cleared.
  A clearing `UnknownOutcome` on an open attempt that already recorded `cancelled` clears. A
  non-cancellation exception at `begin`'s `except BaseException` settles a `not_sent` that clears.
  `cancelled`, `stream_closed`, a caller-cancelled `not_sent` from either adapter cancellation handler or from `begin` (cancellation class and `GeneratorExit`), and an Amendment 9 reconciliation preserve warmth unless that operation records a block; block precedence is mandatory. A reconciliation without a block, an `UnknownOutcome` on a settled attempt and an identical repeat delete nothing. `ProvenNotSent(caller_cancelled=True)` followed by `ProvenNotSent(caller_cancelled=False)` on one attempt returns `already_settled` and clears nothing. For an Amendment 9 estimate E > C, including cancellation and consumer close, verify the block clears warmth; 529 zero also clears. Neither new settlement basis creates or refreshes a prefix. Typed reconciliation preserves ordinary no-clear behavior unless it records a block. For transport-first cancellation, assert the latched transport reason and shielded write before outward TimeoutError; for cancellation-first, assert cancelled and block precedence, as Decision 2 specifies.
- A request reserved before a clearing outcome that does not block its account, and completing
  after it with a one-hour write, writes its row; one completing after it with a stable read writes
  nothing; one completing after a block writes nothing.
- Usage shapes: a request with a server tool stores NULL on a write and refreshes nothing on a matching read; a one-hour mark inside a `document` block's `source.content` after the key block sets `hour_after_key`; a top-level one-hour `cache_control` with no markable block after the key block leaves it false; a caller's one-hour mark on a blank system text block is not the key; a non-text `expires_at` counts as absent; an attempt with an unparsable `created_at` writes nothing; a `GeneratorExit` or an exception while unwinding a stream that already settled (a status outcome, `CompleteUsage`, Amendment 9 final evidence or reconciliation) settles nothing more; a repeated `clear_block()` with a recorded adjustment deletes nothing. Reason-only ledger writes on settled rows return `already_settled`; adapter settlement-attempt flags also protect still-open status outcomes from a second write.
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
- `RouterAttempt` built positionally with four arguments still constructs, with `cert` None,
  `hour_after_key` false and `server_tools` false.
- A version-0 (fresh) file gains the table before `user_version` is set; a version-1 ledger without
  it opens and gains it under the lock; a version-1 file whose table predates the indexes gains
  them; a version-1 file missing a base table still refuses; an older reader admits a file that
  has it.
- Two router instances on one ledger file see each other's rows and clears.
- Mutations: replacing the W preference with the plain order turns the first test red; dropping the length comparison from the read arm turns the one-token-longer and one-token-shorter cases red; dropping the block check turns the blocked-account no-write case red; dropping the server-tool check turns the server-tool arm red; reading `Point.nested` for the flag turns the `source.content` arm red; setting `settled` after a status-branch settlement instead of before turns the settled-stream arm red; treating `GeneratorExit` like any other exception in `begin` (`lionagi/service/accounts/_router.py::AccountRouter.begin`) turns `begin`'s no-clear case red; comparing the stored and current `expires_at` as text turns the `+01:00` case red; removing the shield from `settle_unknown` turns the pending-cancellation 429 case red; removing it from `block` turns the pending-cancellation 401 case red; removing it from `settle_response` turns the pending-cancellation `usage_incomplete` case red; removing the non-streamed outer transport checkpoint turns its `TimeoutError` arm red; removing the new mid-stream post-accounting checkpoint turns the transport-first pending-cancellation arm red; ignoring the error event in the `GeneratorExit` handler turns the close-after-error case red; dropping the `block_reason` read from `clear_block()` turns the not-blocked case red; dropping the `warm_prefix_cert` statement turns the query-plan and predates-the-indexes tests red; dropping the purge turns the 26-hour row red; a zero margin turns the 24-hour row and the `-10:00` row red. Add mutations removing the shield from `settle_rejection` and from estimate settlement. Close-after-error tests must assert the original reason in an open row or estimate evidence, and block precedence, rather than require an open stream_error outcome. The transport TimeoutError arm must assert transport-first and cancellation-first ordering from Decision 2. The settle_response mutation remains the incomplete non-streamed usage case. Exercise the outer streamed checkpoint separately for transport failures without an in-loop latch; it is not the sole checkpoint controlling mid-stream TimeoutError after this amendment.

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

D6 says stream close and finalization shield an available settlement from caller cancellation, bounded by the ledger's five-second wait. That bound covers only the SQLite lock wait. Each ledger call runs its SQLite work through `CredentialLedger._run` in `lionagi/service/accounts/_ledger.py`. `_run` calls `run_sync` with no limiter, so the call first waits for a token from AnyIO's process-wide default thread limiter, which every other thread-offloaded call that names no limiter also shares. `busy_timeout_s` (default 5.0) is the sqlite3 connect timeout and `PRAGMA busy_timeout`, and it starts only once the worker thread runs. Nothing bounds the token wait. Inside `anyio.CancelScope(shield=True)`, the caller's deadline cannot reach that wait, so a shielded ledger write can wait without limit. Amendment 5, Decision 8, as amended by Amendment 9, describes the former unbounded admission wait as historical and retains the native-task-cancellation limitation for all shielded accounting calls. This amendment supplies the bound.

### Decision

1. Every ledger call bounds its admission wait with its own five-second bound, separate from `busy_timeout_s`. The worst case for one call is five seconds waiting for a token plus five seconds waiting for the SQLite lock. A 401 or 403 makes two ledger calls (it records the block, then the attempt's reason), so that path can wait up to twice the per-call worst case, twenty seconds.
2. When the admission bound trips, the call raises `AccountRoutingError` with code `ledger_busy` (`RouterErrorCode.LEDGER_BUSY`), the outcome a SQLite lock timeout produces today. It never surfaces as a bare `TimeoutError`. The default retry exceptions in `lionagi/service/resilience.py` include `TimeoutError`, so a bare timeout would be retried as if the request had failed in transit. `AccountRoutingError` is not in that default set, so it is not retried.
3. A caller of a routed request receives that `AccountRoutingError(ledger_busy)` unchanged and is not retried. The `begin` call in `_request_attempt` (`lionhub/src/lionhub/providers/anthropic/messages.py`) sits after the try block that converts routing errors, and the handlers in `lionagi/service/connections/endpoint.py` convert only the internal routed-attempt classes, so the error passes through. The streamed attempt's `begin` call in `_routed_stream_attempt` (same file) also sits outside that conversion, and a test measures that its error reaches the caller unchanged and is not retried.
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

## Amendment 7 (2026-11-01) — a routed rejection names its class, never its text

Purpose: make a routed request that the provider rejects diagnosable from the error and the ledger,
while keeping the rule that no provider text reaches either. Amendment 9 keeps that rule for the routed error and the ledger: settlement of a rejected request reads its HTTP status and error type, never its message, and only Amendment 9's opt-in private diagnostic file (its Decision 5) may hold redacted message text.

### Context

D6 classifies any 4xx other than 401, 403 and 429 as a request rejection. Before Amendment 7, the `status != 200` branches of `_request_attempt` and `_routed_stream_attempt` in `lionhub/src/lionhub/providers/anthropic/messages.py` recorded only `request_rejected` and raised before reading its body. The outcome held only that reason, and `AccountRoutingError` carried only the code, the validated ids and the HTTP status. Thus, when accounts were configured, the old record could not distinguish a prompt over the context window, an unmatched tool result, a refused thinking setting or any other cause. Amendment 7 adds typed/class diagnosis; Amendment 9 additionally permits typed zero for qualifying 400 and 529, with separate outward-field rules. The unrouted path keeps the whole body in its error, and Amendment 7 leaves it as it is. Amendment 6, Decision 5 added no database column because a schema change needs its own decision; Decision 5 below is that decision for the two columns it adds. Amendment 9 owns its additional structured-evidence column.

The provider documents a closed list of error types and states the message for several 400
conditions. It also says the list of types may grow, and that callers should branch on its typed
errors rather than match message text. This amendment uses message text for diagnosis only; Amendment 9 permits selecting class `other` for diagnostic capture, but no settlement, routing, blocking, retry or warmth control may branch on the class.

### Decision

1. **Two closed-set fields.** A routed request rejection carries `provider_error_type` and `rejection_class`. Each value comes from a set written in this amendment, and `other` is the
   default for anything the set does not name. These diagnostic fields apply only to the request-rejected branch: qualifying HTTP 400 uses `RejectionZero`, other request rejections use `UnknownOutcome(request_rejected)`, and both propagate the fields to the routed error. HTTP 529 instead leaves both rejection columns and outward fields null; qualifying `overloaded_error` appears only in structured settlement evidence. Reconciliation retains already-recorded fields. Other settlements introduce neither field, and UnknownOutcome with another reason carries neither.
2. **The rule is provider-neutral.** The field names, the class vocabulary and the drop rule
   (Decision 4) apply to every routed provider. A provider's match table is written into this ADR
   before that provider's rejection branch reads a body. The table names the `error.type` values
   that provider documents and maps its documented messages onto classes. A class the neutral set
   does not have is added to that set by the same amendment. The sets the code validates against
   are the union of the tables written here, kept in one place that every validating site reads.
   A routed provider without a table records `other` for both fields. The Anthropic table below is
   the first implementation.
3. **Reading the body.** On the rejection branch, the classifier decodes at most 64 KiB of the
   response body, counted after content decoding, as JSON. The non-streamed attempt's client has
   already read the whole body before the status is checked, as it does today, so the bound limits
   only what is decoded. The streamed attempt reads the body of the non-200 response before closing
   it and stops after 64 KiB and one more byte, so a larger body is detected without being read
   further. Both bounds count decoded bytes: the streamed read consumes the decoded byte stream,
   never the bytes on the wire. That read comes before the settlement, so it runs under `CancelScope(shield=True)` bounded at five seconds, like the settlement writes Amendment 5 shields: a pending cancellation is still delivered after the failure is recorded (Amendment 5), and a read that has not finished within the bound returns `other` for both parser fields. On the request-rejected branch those fields are recorded; on HTTP 529 the adapter keeps both rejection columns and outward diagnostic fields null and retains the ceiling. A body over the bound, a body that does not decode, a body with no `error.type` string, or one whose `error.type` is not in the provider's set returns `other` for the parser type; the request-rejected branch records it, while HTTP 529 leaves the rejection columns and outward fields null. A body with no `error.message` string returns `other` for the parser class; HTTP 529 does not persist that diagnostic class. The settlement order remains accounting before outward failure: qualifying 400 uses `RejectionZero`, otherwise `UnknownOutcome(request_rejected)` records both fields. A possible 529 uses the same bounded read before the generic 5xx arm; only its exact pair qualifies, with type in structured evidence only.
4. **The drop rule.** The body, decoded object and raw message remain inside the bounded classifier/read function. That function catches its own failures, including malformed JSON, non-object root or error, missing fields, decoded-body overflow, read failure and timeout, returning `other` for both fields on failed classification. Capture failure follows the isolation rule below. HTTP 529 discards the parser diagnostics from the rejection columns and outward fields. Classifier failures log only the exception type at DEBUG, without traceback; enabled capture failures additionally emit Amendment 9's fixed WARNING event and closed failure code. No input, raw body or message is logged or placed in exception arguments, `__context__` or `__cause__`. The error is raised after
   that function has returned, outside any `except` block, so the attempt's error has no context
   from the read. This is the same rule the decode error already follows on the success path. On the streamed attempt the bounded read of Decision 3 also runs inside that function, so no string or bytes value holding the body is bound in the attempt's own frame. The sole storage exception is Amendment 9's enabled HTTP 400 class-`other` capture: the classifier passes only redacted, capped text and validated metadata to the private writer, under its reader and retention policy. Header construction supplies the exact-string redaction callable using an already-unwrapped credential; the classifier never unwraps one. Capture failure changes neither classification, accounting nor outward errors. The streamed `_classify_streamed_rejection` retains the decoded `aiter_bytes()` read under `CancelScope(deadline=..., shield=True)` (`lionhub/src/lionhub/providers/anthropic/messages.py::_classify_streamed_rejection`). Disabled capture retains no text; enabled capture grants no text input to financial or request control.
5. **Where the fields go.**
   - `AccountRoutingError` gains `provider_error_type` and `rejection_class`, both optional. Each is
     validated against its set when the error is built, and an unknown value raises `ValueError`.
     When present, they join the message as `type=<value>` and `class=<value>`, so the message is
     still built from validated fields only. The docstring's guarantee is restated, not weakened.
   - The internal routed-attempt error that the non-streamed attempt raises carries the same two
     fields, and the conversion to `AccountRoutingError` in `lionagi/service/connections/endpoint.py`
     (`_public_routed_failure`) passes them on. That internal error stays reachable as the public
     error's `__context__`, so it holds nothing beyond the validated fields either. The streamed
     attempt raises `AccountRoutingError` itself on this branch, as it does today, and passes the two
     fields to it directly.
   - `UnknownOutcome` gains the same two optional fields, validated the same way.
   - The ledger's `attempt` row gains two nullable text columns, `provider_error_type` and
     `rejection_class`. An open `request_rejected` reason writes them atomically and retains its first reason/fields. A qualifying 400 writes them with zero settlement; 529 leaves both null and stores its qualifying type only in structured evidence. Typed reconciliation preserves existing fields, and a new final basis snapshots them before replacing outcome. Other settlements introduce neither field, and later classification never replaces stored class. They are added to an existing version 1 ledger the same way the warm-prefix table is, when missing, without changing `user_version`, and the check that lets a complete ledger through also requires them. Amendment 9 additionally requires its evidence column and a locked re-read of extension completeness. A reader that predates them does not read them,
     and a row settled by a version that predates them holds null in both. The ledger validates
     both values against the sets before writing, as it validates reason codes today.
6. **Class remains diagnosis only.** Settlement, retry, blocking, account choice and warm-prefix clearing never read rejection class; diagnostic capture may use class `other` to select a message for inspection. Amendment 9 permits settlement to read the HTTP status and parsed error type. The resulting released ceiling changes D3 utilization and W eligibility indirectly, though their formulas are unchanged. A request rejection is still never retried and never moves to another account. Original ending reason and any block govern warmth under Amendment 9. Repeated typed zero rejections do not automatically block an account. Contradictory prior typed failure evidence can also cause settlement_conflict and clear warmth; diagnostic class cannot cause that conflict.
7. **Out of scope.** The unrouted path (`lionagi/service/connections/endpoint.py`, the error-body
   branch) is unchanged. Provider text stays absent from the ledger and routed errors. Amendment 9 supplies the formerly deferred bounded-message reader policy, authorizing only its enabled private diagnostic file; no other provider-text storage is permitted here.

### Vocabulary

`provider_error_type` for the Anthropic Messages API. Each value is the provider's documented
`error.type`, from https://platform.claude.com/docs/en/api/errors, "HTTP errors". This page and
every page the class table cites were read on 2026-11-01:
`invalid_request_error` (400), `authentication_error` (401), `billing_error` (402),
`permission_error` (403), `not_found_error` (404), `conflict_error` (409), `request_too_large`
(413), `rate_limit_error` (429), `api_error` (500), `timeout_error` (504), `overloaded_error` (529),
and `other`. On the request-rejected branch, only the 4xx types other than 401, 403 and 429 are expected under their documented statuses; Amendment 9 additionally reads a possible 529 with the bounded typed parser before the generic 5xx branch. The full documented type set remains available on the request-rejected branch even with an unexpected status. HTTP 529 is an explicit exception: an unrecognized pair retains the ceiling with null rejection columns/outward fields, and only qualifying overloaded_error enters structured zero evidence. No other pair qualifies for zero.

`rejection_class`, provider-neutral. The Anthropic match is a case-sensitive substring of
`error.message`, tried in this order, first match wins:

| Class | Anthropic message contains | Source |
|---|---|---|
| `prompt_too_long` | `prompt is too long` | context-windows page, "Context window overflow behavior": "If the input alone already exceeds the model's context window, the API returns a 400 `invalid_request_error` ("prompt is too long") on every model." |
| `tool_result_missing` | `tool_use ids were found without tool_result blocks immediately after`, or `was found without a corresponding` | troubleshooting-tool-use page, "Errors at request time" table, both rows quoted |
| `prefill_unsupported` | `does not support assistant message prefill` | errors page, "Prefill not supported" example body |
| `thinking_config_unsupported` | `"thinking.type.enabled" is not supported`, `"thinking.type.disabled" is not supported`, `"thinking.type.between_tools" is not supported`, `adaptive thinking is not supported on this model`, `is not supported when thinking is disabled on this model`, or `effort cannot change when thinking is disabled` | errors page, "Extended thinking not supported", "Adaptive thinking not supported" and "Thinking cannot be disabled", each message quoted |
| `thinking_block_invalid` | `blocks in the latest assistant message cannot be modified`, or `The block is bound to a different conversation` | errors page, "Thinking blocks cannot be modified" and "Thinking block bound to a different conversation" |
| `tool_choice_unsupported` | `tool_choice: type "tool" and "any" are not supported for this model` | errors page, "Forced tool use not supported" |
| `tool_type_unsupported` | `does not support tool types:` | errors page, "Computer use tool version not supported": "The API returns the same message for any Anthropic-defined tool type that the requested model doesn't support." |
| `image_too_large` | `exceed the maximum image size`, or `many-image requests` | vision-coordinates page, "Turn resizing into an error" example message; vision page, the many-image limit: "rejected with an `invalid_request_error` whose message references "many-image requests"" |
| `other` | anything else, and any body without a message string | |

Documented conditions with no stated wording are not classes and record `other`: a request over a
spend limit (the errors page says it returns a 400 but gives no message; Amendment 8 Decision 1 supersedes
this: such a request records `spend_limit_reached`, citing the rate-limits page's wording), `max_tokens` above a
model's output limit, and the hard image and page limits. A class is added only from a provider
documentation line or a captured message, cited in this table.

### What it guarantees, and what it does not

- A request rejection's error, evidence variant and ledger row retain a validated documented error type or `other`, and a class or `other`: qualifying HTTP 400 uses RejectionZero and outcome `rejection_zero`, otherwise UnknownOutcome leaves an open `request_rejected` reason. HTTP 529 instead has null rejection columns and outward fields, with its type only in qualifying structured zero evidence. Reconciliation preserves prior columns. None of them holds provider text. The separate enabled diagnostic file is governed by Amendment 9, not by this ledger/error-field guarantee.
- A provider message whose wording changes falls to `other`. It is never assigned a wrong class that a caller acts on for accounting or request control, because those decisions do not act on the class, so a diagnostic misclassification cannot change those decisions; only the opt-in diagnostic capture may select class `other` (Amendment 7 Decision 6 as amended by Amendment 9).
- It does not say why a rejection classed `other` happened. With capture disabled the message stays dropped; enabled Amendment 9 capture may supply redacted bounded wording. Alternatively, reproduce the request on the unrouted path, which keeps the body, and add a new class only by amendment. Neither route guarantees diagnosis or establishes the original attempt's billing.
- It does not cover the frames a traceback keeps alive. When the error is raised, the attempt's
  frame still holds the response object, the request and its headers, as it does on every other
  failure of this path today, so a crash reporter that records local variables sees them. That
  exposure, including the credential header, is not changed by this amendment. It would be falsified
  for this amendment's own values if a string or bytes local of a frame of the package's own code,
  in the error's traceback or in the traceback of an exception in its chain, held the body.
- The measured case that prompted this amendment, a 400 after a context fold, was never captured,
  and its class cannot be named from the evidence. The fold changes only lionagi's own message
  history, which the Anthropic body carries as text, with no `tool_use`, `tool_result` or
  `thinking` blocks, and the folded input was far below the model's context window. Its acceptance is that the next such rejection records a documented type, and a class other than `other` or, failing that, an enabled Amendment 9 diagnostic capture or a reproduction on the unrouted path supplying sanitized wording for a later class-table amendment. Neither reproduction nor captured wording alone proves the original attempt’s billing. Diagnostic acceptance is independent of accounting: HTTP 400 plus invalid_request_error settles zero regardless of whether its class is named or capture is enabled.

### Alternatives considered

- Error type only: kept as the floor. Nearly every rejection is `invalid_request_error`, so alone
  it does not tell causes apart.
- A bounded, redacted message on the ledger row: not taken here. Treat provider text as potentially containing request content. Amendment 9's sole durable-text exception is a separate enabled private diagnostic file with its own reader policy; provider prose remains forbidden in the ledger.
- Documenting that a routed rejection cannot be diagnosed: rejected. Routing is the normal
  configuration, so the defect would stay where requests run.
- Encoding the class into the reason code (`request_rejected_prompt_too_long`): rejected. Warm-prefix clearing uses original reasons and block precedence, including Amendment 9's separate outcomes; it never uses message class. Every new reason requires auditing each clearing rule. Among the original failure reasons, only `cancelled` and `stream_closed` preserve warmth, and only when no block is recorded, under Amendment 9 Decision 3.

### Verification

Each item is a test that fails without this change:

- For each class, a 400 body carrying the documented message, through both the non-streamed and the streamed routed attempt, raises `AccountRoutingError(request_rejected)` with that class and `provider_error_type="invalid_request_error"`, and the ledger row holds the same two values with outcome `rejection_zero` and zero cost under Amendment 9.
- A body with an unknown message, no message, an unknown or missing type, invalid JSON, or more
  than 64 KiB records `other` for the affected field, and never raises anything but the routed
  error. On the streamed attempt, a body larger than the bound is not read past 64 KiB and one
  byte, and a body that does not arrive within five seconds records `other` for both fields.
- A cancellation already pending when a 400 arrives is delivered after the attempt is recorded `rejection_zero` with its two fields for a qualifying 400, otherwise open `request_rejected`, on both attempts.
- A message containing the text of two rows gets the class of the earlier row.
- Sentinel: a body whose message carries a unique sentinel string, through both branches. The
  sentinel is absent from the error's `args` and `str`, from every exception reachable through
  `__context__` and `__cause__`, from every log record captured at DEBUG level for every logger
  (the HTTP client's included), from the ledger row, and from every string or bytes local of every
  frame of the package's own code in the traceback of the error and of each exception in its
  chain (the test's own frame, which holds the sentinel it built, is not one of them). A control
  arm with the classifier replaced by one that returns `other` passes the same assertions. An arm that deliberately places the message in the error's arguments fails them. Run these checks with capture both disabled and enabled. Only an eligible enabled capture may put a redacted sentinel in the designated private file; the ledger, logs, error and traceback assertions are unchanged. Verify the chosen credential is absent even when the cap cuts near it, and all diagnostic failures leave classification and settlement unchanged.
- An unknown value passed to `AccountRoutingError`, `UnknownOutcome`, either new evidence variant or the ledger write raises `ValueError`, except that a 400 `RejectionZero` with absent class defaults to `other`, and invalid `StreamUsageEstimate.request_id` normalizes to null at construction and deserialization under Amendment 9 Decision 6. An output bound above a known model’s maximum raises `ValueError` before writes.
- No rejection class changes settlement, retry, blocking, account choice or warmth. Each class of qualifying 400 settles zero, is not retried and clears warmth; changed totals may change subsequent eligibility/order. Qualifying 529 settles zero, clears warmth, keeps both rejection columns/outward fields null and retains its retry classification.
- A ledger created before this change gains the two columns on open and keeps its rows. Only the specified 400 settlement introduces those fields outside `UnknownOutcome(request_rejected)`; reconciliation preserves them. A later reason-only write changes neither first reason nor fields. A qualified closure records that snapshot before replacing outcome; verify contradictory typed evidence refuses and blocks, while a changed diagnostic class neither determines eligibility nor overwrites the stored class.
- The unrouted error-body path is unchanged.

## Amendment 8 (2026-11-02) — rejections caused by the account's billing state get their own classes

Purpose: tell a routed rejection caused by the account’s billing state apart from one caused by the request through the class field Amendment 7 added. Amendment 9 allows diagnostic capture only for class `other`; neither billing class controls settlement, routing, retry, blocking or warmth.

### Context

Before Amendment 8, Amendment 7’s classes named causes in the request only. A routed request on an account whose
prepaid credit has run out is rejected with HTTP 400 and `invalid_request_error`, the same type as a
malformed request. Amendment 8 adds billing classes; Amendment 9 settles the exact pair at zero regardless of class. If a 400 remains classed `other`, enabled bounded capture can supply diagnostic wording without reproduction; capture off still drops it, leaving an account with an unmatched billing rejection indistinguishable from a request defect without reproduction on the unrouted path. A
one-token request with a single user line, sent on 2026-11-02 to such an account, received this
body (its request id removed):

```json
{"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}}
```

No documentation page read for this amendment states that wording, so the row below cites the
captured message, as Amendment 7's vocabulary allows.

Amendment 7's vocabulary lists "a request over a spend limit" among the documented conditions with
no stated wording. The rate-limits page does state it, under "Setting your own spend limit"
(https://platform.claude.com/docs/en/api/rate-limits, read on 2026-11-02): "When usage reaches a
spend limit you set, requests return HTTP 400 with error type `invalid_request_error`. The message
starts with `You have reached your specified API usage limits`, or `You have reached your specified
workspace API usage limits` for a workspace limit, and states when access resumes." That clause of
Amendment 7 is superseded for this condition. Its other two conditions, `max_tokens` above a model's
output limit and the hard image and page limits, still record `other`.

### Decision

1. **Two classes.** `credit_balance_low` and `spend_limit_reached` join the provider-neutral
   rejection class set. Their Anthropic rows follow the eight rows of Amendment 7's table, in this
   order, under the same rule: a case-sensitive substring of `error.message`, first match wins.

   | Class | Anthropic message contains | Source |
   |---|---|---|
   | `credit_balance_low` | `Your credit balance is too low` | the captured message quoted in the Context |
   | `spend_limit_reached` | `You have reached your specified API usage limits`, or `You have reached your specified workspace API usage limits` | rate-limits page, "Setting your own spend limit", quoted in the Context |

2. **Still diagnosis only.** Amendment 7, Decision 6 as amended by Amendment 9 holds for both classes: no financial or request-control action reads message class; only diagnostic capture selects class `other`. Neither one blocks the
   account, moves the request to another account, retries it or changes warm-prefix clearing. As
   Amendment 5 says, an account whose credit is exhausted stays a candidate; an operator who reads
   the class from the error or the ledger row blocks the account through the ledger's `block`. Blocking an account on a class would key financial control on message text, which Amendment 7 as amended by Amendment 9 still rules out, and needs its own decision.
3. **Preserved boundaries.** The diagnostic vocabulary is Amendment 7’s set extended by Amendment 8’s `credit_balance_low` and `spend_limit_reached`; the 64 KiB bound and set validation remain. Amendment 9 revises settlement, evidence persistence and the explicit opt-in diagnostic-file exception to the drop rule. No provider text enters the ledger, public errors or ordinary logs.

### Conditions that still record `other`, or no class at all

- An organization or workspace the provider has disabled. No documentation page read for this
  amendment states the message and none has been captured, so it is not a class (Amendment 7: a
  class is added only from a documentation line or a captured message).
- `billing_error` (402). The errors page states no message for it. Its type is recorded as
  `billing_error`, which already names the cause.
- A usage tier's monthly spend cap. The rate-limits page, under "Reaching your spend cap", says it
  returns HTTP 429 `rate_limit_error` with `error.details.error_code` `enforced_spend_limit_reached`
  and keeps failing until access resumes. A 429 takes D6's rate-limited branch, which settles the
  attempt `rate_limited` and raises a retryable error without reading the body, so this table never
  sees it. Whether that branch should tell the spend cap apart from a rate limit is a separate
  decision.

### What it guarantees, and what it does not

- On both HTTP paths, a routed request rejection whose message contains one of the three wordings records the matching class: a qualifying HTTP 400 records its class on the error, the rejection columns and `RejectionZero` evidence, with outcome `rejection_zero`; a nonqualifying request rejection records the class on the error, in `UnknownOutcome` and on the open row. HTTP 529 keeps rejection columns and outward fields null, with `overloaded_error` only in qualifying structured zero evidence.
- An account out of credit remains eligible for reservation and rejects requests routed to it until blocked (Decision 2); a qualifying HTTP 400 releases each reserved ceiling at zero instead of retaining that hold. Each such rejection is now recorded with a class that names the account
  as the cause.
- The credit-balance row rests on one captured wording, not a documented one. If the provider
  rewords the message, the row stops matching and the rejection records `other`, with no error.

### Verification

Each of the first four items is a test that fails without this change; the last is a regression check:

- The pinned copy of the closed set and the tests' copy of the class table carry both classes, after
  Amendment 7's rows and in this order.
- Each of the three wordings, inside a longer message, through the non-streamed and the streamed
  routed attempt, records `invalid_request_error` and its class on the error and the ledger row.
- The captured credit-balance body, whole, through both attempts, records `credit_balance_low`.
- A message holding wordings of two different rows records the earlier row's class, for every pair
  that includes a new row.
- Run Amendment 7's sentinel, cancellation and body-bound tests with Amendment 9's explicit changes: capture off and on, qualifying zero versus held rejection, and no text outside the designated diagnostic file. Require all those tests to pass. The body bound itself remains unchanged.

## Amendment 9 (2026-11-02) — ending a hold with rejection evidence or observed stream usage

The open-attempt census contains nine `request_rejected` attempts across three accounts, distributed seven, one and one. Five printed attempt rows separately show open `request_rejected` attempts with type `invalid_request_error` and class `other`; those rows contain no HTTP status or account value. This amendment makes release automatic on qualifying typed evidence and keeps blocking for repeated rejection an operator act. It edits sentences of the existing record in place; [docs/adr/edits/ADR-0126-amendment-9-edits.md](edits/ADR-0126-amendment-9-edits.md) records the wording each edit replaced, with the new wording and the reason.

> You're billed only for successful API calls and completed tasks.
>
> Failed requests aren't charged.
>
> Note: If your client disconnects or times out in the middle of a request that was on track to succeed, that request is still charged.

— Help Center, “How do I pay for my Claude API usage?”, https://support.claude.com/en/articles/8977456, read 2026-11-02.

> When usage reaches a spend limit you set, requests return HTTP 400 with error type `invalid_request_error`. The message starts with `You have reached your specified API usage limits`, or `You have reached your specified workspace API usage limits` for a workspace limit, and states when access resumes.

— Rate-limits page, “Setting your own spend limit”, https://platform.claude.com/docs/en/api/rate-limits, read 2026-11-02; quotation retained from Amendment 8.

Purpose: release a reservation for a qualifying rejection, and replace an early-ended stream's reservation with an explicitly estimated cost based on observed usage. Other unknown attempts retain their ceilings.

### Context

A failure response and a client losing a response are different billing evidence. The Help Center distinguishes them, but does not give a bound on usage after disconnection. The status/type mappings in Amendment 7 identify 400 `invalid_request_error` and 529 `overloaded_error`. Applying the failed-request billing statement to those pairs is this amendment's policy interpretation; the quotation does not enumerate them or promise a numeric upper bound for interrupted streams.

At `c6a3b4af`, `UnknownOutcome` records a reason and optional rejection fields while leaving the row open (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`). Both HTTP paths send every status at least 500 through `provider_error` before inspecting its body (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`, `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`). A stream merges usage with replacement and waits for `message_stop` and a final output count (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt.emit_ready`). Its truncation and cancellation paths pass no usage to settlement (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`). These are the paths this amendment changes.

### Decision

1. **A closed status/type rule settles zero.** For routed Anthropic Messages, an actual non-success HTTP response with exactly either of these pairs settles at zero:

   | HTTP status | Parsed `error.type` | Settlement |
   |---|---|---|
   | 400 | `invalid_request_error` | `rejection_zero` |
   | 529 | `overloaded_error` | `rejection_zero` |

   The response must belong to this physical attempt and precede any successful stream response. A 200 stream containing an error event is not an HTTP 529 response. No other pair qualifies. All Amendment 7 and 8 classes qualify when the first pair is present, including class `other`; no settlement, retry, blocking, account choice or warm-prefix decision reads the rejection class. This supersedes Amendment 7's prohibition on settlement reading the typed error field, while preserving diagnosis-only use of message class. Releasing C changes the utilization used by D3 and the spending threshold used to form W in Amendment 5: their formulas stay unchanged, but candidate eligibility and ordering can change indirectly. The rule is deliberately typed, not an assertion that every unrecognized 400 is billable.

   Apply Amendment 7's decoded 64 KiB body limit and five-second shielded streamed-body read to both pairs. A larger body, invalid JSON, failed or timed-out read, missing or unrecognized type, or status/type mismatch keeps the ceiling. A missing or unfamiliar message with a valid type does not prevent zero settlement. Inspect a possible 529 before the generic 5xx arm, without changing its retry classification. Raise the existing routed error only after the settlement attempt; zero accounting is not request success.

   HTTP 401, 403, 429 and 413 retain D6's existing accounting, blocking and retry actions because this amendment authorizes only the two pairs above. Other 4xx and other 5xx also retain D6's treatment. No account block is inferred from a billing class. Repeated typed zero rejections do not automatically block an account.

2. **An early-ended stream has an estimate, not complete billing evidence.** Eligible endings are transport loss or timeout after a complete stream event, caller cancellation, consumer close, EOF or truncation without `message_stop`, and an in-stream error followed by termination without a complete priced terminal result. A stream that ends after `message_start` and before any complete `message_delta` with valid usage is not eligible and keeps the ceiling: the output count `message_start` carries is a placeholder, not observed output. Merely seeing an event is insufficient: before a complete `message_delta` with valid usage is seen, the ceiling remains. Process death without a committed settlement also leaves the ceiling.

   Feed each received byte chunk to an independent accounting UTF-8/SSE parser before `redactor.feed(raw)`; capture billing fields from its complete decoded provider events before redaction buffering, generic projection or delivery to a consumer can suspend processing. On ordinary `message_stop`, after updating and validating accounting usage, set `settled=True` and await shielded `settle_stream_end` before feeding that chunk to the output redactor or yielding any of its output; invalid terminal usage retains the ceiling and does not select an estimate. Keep only the exact served model, request identifier after the existing `provider_request_id` key-run check (`lionagi/service/accounts/_router.py::provider_request_id`) and Decision 6 normalization, and billing fields. A truncated event contributes nothing. Start from the initial usage; each later cumulative field replaces its previous value, never adds to it. An absent field leaves its previous value; an invalid later value invalidates that dimension rather than restoring an older valid value. Nested cache-creation splits replace their earlier split as a unit. Conflicting models, inconsistent counts or splits, unknown pricing dimensions and unpriceable usage keep the ceiling. Do not infer tokens from text bytes, output chunks or wall-clock time.

   All D5 pricing and validation requirements still apply to the observed vector, except that a terminal event and a terminal output count are not required. The observed output count comes only from a complete `message_delta` with valid usage; the `message_start` output count never qualifies, whatever its value, while its input and cache counts are still used. A qualifying count is not evidence that generation stopped there. An observed output count is mandatory: if no complete `message_delta` has supplied a valid output count, keep the ceiling. A missing later output field retains an earlier qualifying count; an invalid replacement invalidates it. Never substitute zero for an absent output count. Only cache-field omissions that D5 already permits as documented zero may become zero. Cache splits may be derived only under D5's single-TTL rule. A complete terminal response retains D5's exact settlement path, with no margin; a terminal response with invalid usage does not become an early-ended estimate merely to escape validation.

   Let `Aobs` be the observed vector priced once at the captured revision, exact served model and applicable tier. Let `Oobs` be its output count, and `Pout_max` the greatest applicable output rate from the captured quote. The policy margin and settlement are:

   ```text
   margin_tokens = max(1024, ceil(Oobs / 10))
   M = ceil(margin_tokens * Pout_max)
   E = Aobs + M
   ```

   Use integer counts and rational rates, rounding upward to integer nanodollars; no binary float. The fixed floor gives a nonzero allowance when little output has been observed; the ten-percent term scales the allowance for a longer observed output. These values are chosen accounting policy, not measured provider behavior, a chunk-size bound, or a guarantee that the remaining bill fits. For risk analysis this amendment assumes generation could continue to the request's output limit after disconnection; the supplied billing quotation does not establish whether it does. The estimate can understate or overstate the eventual bill. D5's local admission invariant continues to apply to recorded costs, but its assurance about real charges cannot rely only on documented bounds and exclusive ledger use for these estimates.

   Settle `stream_usage_estimate` with `cost_nusd=E`, retaining observed counts separately from the margin. Do not fabricate `CompleteUsage`, add margin tokens to provider counts, or clamp E to C. If E exceeds C, record E and block with `ceiling_exceeded`; this is a conservative accounting block, not a claim that the provider billed E. A later sourced correction uses the existing adjustment mechanism, not a second settlement. Revising the margin requires a later decision supported by observed-versus-final billing evidence.

   **Transport-loss ordering.** The current byte-source failure is saved before complete buffered events are drained, then raised before context-manager exit; the outer handler currently records unknown transport error (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`). Change this path as follows: when the byte-source exception is caught, latch `transport_error` as the ending reason for this attempt. Before the first subsequent yield or context-manager exit, collect billing fields from all complete events already received, without yielding to consumers, and make one shielded `settle_stream_end` call with complete usage if the already-received complete events include a valid priced `message_stop`, otherwise a valid `StreamUsageEstimate(original_reason="transport_error")` or `UnknownOutcome("transport_error")`. The accounting parser receives each raw byte chunk once, before output redaction, and never receives frames drained from the redactor. It uses the complete-event framing rules of the stream; after transport loss discard its incomplete UTF-8 tail and any unterminated event without synthesizing a boundary. Select the settlement basis solely from that parser’s accumulated complete events before flushing the output redactor. A complete terminal event buffered only on the output side has already selected complete usage through the accounting parser; if it already settled, the flag prevents another call. After accounting, flush the output byte redactor with `final=True` and drain sanitized complete frames for delivery; those frames never update billing fields. Invalid terminal evidence retains the ceiling. If complete usage already settled, make no additional settlement and do not clear warmth merely for the subsequent transport error. The current tail flush is at `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`; the new independent accounting parser is a requirement, not existing code. The settlement-attempt flag is the existing `settled` boolean. Set `settled=True` before awaiting the call; it prevents a second write even if the call fails. Afterward drain sanitized output as allowed and propagate the sanitized transport error with the existing cancellation checkpoint. No retry after output starts.

   With transport loss alone and no complete terminal result, eligible usage records `stream_usage_estimate` with original reason `transport_error`, otherwise the open row records `transport_error`; both clear warmth, and E > C additionally blocks. If an AnyIO `fail_after` cancellation becomes pending while that latched transport error propagates, complete the shielded accounting first and deliver cancellation/`TimeoutError`; keep the same transport reason and never replace it with `cancelled`. If cancellation is observed before any byte-source error is latched, use `cancelled`: eligible usage records an estimate with that original reason, otherwise keep the open cancelled hold; preserve warmth unless a block is recorded. The cancellation handler uses the latched reason if an exception interrupts preparation before the accounting call, and honors the flag if the call has begun. A failed write retains the durable ceiling and is not retried by unwinding. This is an explicit ordering change from the current cancellation handler, which passes `cancelled` (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`). Native task cancellation retains Amendment 5's weaker shielding guarantee. Verify both orders, including cancellation during context-manager exit and failed commits.

3. **Settlement remains atomic, bounded and once per attempt.** Both new evidence variants pass through the ledger transaction and the existing cancellation shields. Keep Amendment 6's separate five-second admission and SQLite waits. A failed write or process death releases nothing, and no caller reports an uncommitted write as committed. An identical repeated runtime settlement changes nothing; after the reconciled-row exception in Decision 4, conflicting final runtime evidence blocks with `settlement_conflict`, following the existing conflict write and post-transaction raise (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`). Neither estimate nor rejection zero establishes a warm prefix. For automatic settlement, clear warmth for rejection (400), provider error (529), transport failure, truncation and stream error. Cancellation and consumer close retain warmth only if no block is recorded. Every block, including E > C or settlement conflict on a cancelled or closed stream, clears warmth and takes precedence. Reconciliation normally leaves warmth alone; an Amendment 9 reconciliation that records a block clears it. None of these operations creates or refreshes warmth. Preserve original failure propagation and retry classification; never replay cancellation or retry automatically after stream output starts. A retry is a new reservation, even after a zero settlement.

4. **Existing open rows use sourced reconciliation.** Use the existing `CredentialLedger.reconcile(adjustment_id, attempt_id, cost_nusd, source_ref)` operation, with a new optional keyword `evidence: RejectionZero | StreamUsageEstimate | None = None`. Amendment 9 operator closure requires that typed evidence; existing sourced reconciliation without it retains its existing scope. This is one attempt and one transaction per call, not a batch sweep or direct SQL update. After handling an identical recorded adjustment as a no-op (even after rollover), the transaction checks the stored identity, open state, current admission month, prior-failure consistency and typed qualification, and recomputes the amount. A supplied amount differing from zero or computed E raises `ValueError` before any write. No HTTP or file read occurs inside the ledger transaction.

   A successful call retains outcome `reconciled`, writes the structured evidence with origin `operator`, retains the original rejection columns, and inserts exactly one zero-delta reconciliation adjustment with `source_ref`; cost enters spend once through the attempt. This extends the existing atomic update and adjustment insertion (`lionagi/service/accounts/_ledger.py::CredentialLedger._reconcile_sync`). Repeated adjustment ids require the same inputs and canonical evidence; a conflict refuses without rewriting. A late runtime settlement on a reconciled row returns `already_settled` without comparison, mutation or block, except for an attempt closed by lease expiry, where Amendment 11 records the evidence as a correction; any other later correction requires a sourced adjustment. In the reverse order, reconciliation on an already-settled runtime row raises `settlement_conflict` without writing a block or changing the row. Identical adjustment replay is checked first. An estimate above C records E and blocks, clearing warmth. The operation never unblocks an account.

   The operator supplies a retained evidence file referenced by `source_ref`, containing the attempt reference, observed response status/type or stream vector and its provenance, rule revision and, for a stream estimate, all required pricing inputs. A rejection-zero reconciliation does not require output or TTL inputs. The operator verifies attribution to the same physical attempt; this is trusted operator testimony, not cryptographic authentication of the provider. Retain that source for the ledger's audit lifetime; it is separate from the seven-day diagnostic files. A reproduction or another attempt's result is not evidence for this row. Existing reconciliations need no rewrite or fabricated new provenance.

   A legacy `request_rejected` reason and `invalid_request_error` type alone do not prove HTTP 400: the existing classifier names any documented type whatever the status (`lionhub/src/lionhub/providers/anthropic/messages.py::_classify_rejection`), and every non-200 status outside 401, 403, 429 and 5xx reaches it with reason `request_rejected` (`lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._request_attempt`, `lionhub/src/lionhub/providers/anthropic/messages.py::AnthropicMessagesEndpoint._routed_stream_attempt`); the ledger row keeps no HTTP status (`lionagi/service/accounts/_ledger.py::CredentialLedger._ensure_schema`). A retained routed error line tied to that attempt and containing status 400 and type `invalid_request_error` is sufficient typed evidence, even with class `other`. The same type with no recorded status remains held. A legacy `provider_error` alone does not prove 529; reason codes, null outcomes and elapsed time prove neither free execution nor observed usage.

   Legacy rows store `price_revision` and `requested_model`, but not the output bound or TTL derivation. An operator may compute E only if retained evidence supplies all the missing quote inputs, observed counts and exact served model, and the original revision is still available. Do not reconstruct them from today's configuration, from C, a retry, model defaults or a guessed TTL. Without them the row cannot receive this estimate; it remains held pending ordinary sourced reconciliation with independent billing evidence. Do not touch settled rows, previous-month rows or unrelated reservations through the Amendment 9 operator rule. Normal runtime settlement may cross a month boundary and always charges its original admission month; the operator rule's current-month eligibility check does not change that. Rollover does not erase old holds.

5. **An opt-in diagnostic capture can explain class `other`.** Hash and length alone cannot reveal the cause. For an HTTP 400 with capture enabled, parsed object root and object `error`, a nonempty string `error.message`, and classifier class `other`, permit an operator-enabled local diagnostic file outside the ledger, within the bounded classifier function. This is the sole capture trigger; a recognized provider type is not required, and unknown or missing types use the validated closed value `other`. This is the only exception here to Amendment 7's no-provider-text-storage rule. The diagnostic switch is `LIONAGI_ROUTED_REJECTION_CAPTURE`, read only from the process environment, never dotenv or settings YAML. Sample it once when constructing `AccountRouter`; store a private boolean on that router. Exactly `"1"` enables capture. Unset, `"0"` and every other value (including empty, `"true"` and `"2"`) disable it without logging the value or raising. It adds no AppSettings field. Reconstruct the router to change this diagnostic switch; existing routers do not reload it. Tests use separate router instances for enabled and disabled cases. Store one JSON record per capture at `<ledger-parent>/rejection-diagnostics/<attempt-id>.json`, never in the ledger. The private cleanup entry point is `cleanup_rejection_diagnostics(ledger_path, now)`. The ledger, routed error, exception chain and ordinary logs never contain provider text or the diagnostic path.

   An enabled capture records the bounded `error.message` string from a successfully parsed body, HTTP status, validated type and class, timestamp and attempt reference. It records neither the raw body nor headers, key, request payload or other response fields. Enforce the same 64 KiB decoded-body bound. Apply this rule to the entire decoded message before measuring or truncating it. Redact before the 8 KiB UTF-8 cap, truncate at a character boundary, and record truncation and the UTF-8 length of the redacted message before capping, never the raw length. Request-local header construction supplies a synchronous redaction callable using the already-unwrapped credential. Its string rule replaces every exact credential occurrence with `[REDACTED]`; it does not use streaming key-run masking or join fragments across fields. This matches the string replacement at `lionagi/service/accounts/_router.py::redact_value`, but the new callable uses the already-unwrapped string instead of invoking that function; the classifier never unwraps a secret or receives a new credential argument. Invoke that callable inside the bounded classifier before passing sanitized data to the private writer. The callable and raw message must not escape to a background worker or an exception chain. D2's sole secret-unwrapping site stays unchanged. As a precaution, treat the message as potentially containing request content; this file is sensitive diagnostic material, not public error text. Use a private directory, owner-only file access, no symlink following, no automatic upload and a seven-day retention limit enforced before capture and by explicit cleanup. Readers are the operator who enabled capture and tools explicitly authorized by that operator; publishing a captured example requires separate inspection and redaction.

   Diagnostic failure codes are the closed set `body_unreadable`, `body_too_large`, `body_timeout`, `message_missing`, `redaction_failed`, `writer_busy`, `write_failed`, `cleanup_failed`. When capture is enabled, emit `logging.getLogger("lionagi.service.accounts.rejection_diagnostics")` at WARNING with fixed message `rejection_capture_failed` and structured extra fields `event="rejection_capture_failed"`, `failure_code=<closed code>`. Include no body, path, identifiers, exception text or traceback. Pre-classification body/read codes and `message_missing` apply only to HTTP 400 with capture enabled, never to 529; post-classification capture uses only the trigger above. A non-object JSON root or non-object `error` reports `body_unreadable`; missing, non-string or empty `error.message` reports `message_missing`. An empty message is never captured as a zero-length string. Explicit cleanup may emit `cleanup_failed` independently of a response. With capture disabled no response diagnostic failure event is emitted. Existing classifier DEBUG exception-type logging is separate and unchanged. A body over 64 KiB fails classification before any typed pair is accepted: an enabled HTTP 400 capture reports `body_too_large`, writes no file, and retains the ceiling. A parsed qualifying type with a missing, non-string or empty message may still settle zero while reporting `message_missing`. An unreadable, oversized or timed-out body produces no message file. A redaction failure skips capture entirely. No message or class is inferred from a diagnostic failure. Capture failure must not change the classification, settlement or outward error. Use bounded nonblocking admission to diagnostic writing; on congestion, skip capture and report a closed diagnostic failure code without message text. Do not add another unbounded wait to the settlement path. With capture disabled, Amendment 7's drop and sentinel guarantees remain unchanged; with capture enabled, only the designated private file may contain the message. A bounded capture can help name a new cause but cannot guarantee diagnosis if the decisive wording is truncated or the body is unreadable. Add new message classes only by a later amendment citing a sanitized capture or documentation. No classifier self-updates.

6. **Two evidence variants, no new lifecycle state.** Add immutable evidence types `RejectionZero` and `StreamUsageEstimate`, producing outcomes `rejection_zero` and `stream_usage_estimate` respectively; neither is `UnknownOutcome`, `ProvenNotSent` or `CompleteUsage`. The `UnknownOutcome` docstring incorrectly says it closes a row; the ledger leaves it open (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`). Those existing variants have distinct fields (`lionagi/service/accounts/_types.py::CompleteUsage`, `lionagi/service/accounts/_types.py::ProvenNotSent`, `lionagi/service/accounts/_types.py::UnknownOutcome`). Retain `open` and `settled`. These two outcomes distinguish zero under this policy from zero complete usage and from not sent. `AccountRouter.settle_rejection(attempt, evidence: RejectionZero)` shields a ledger settle; extend `AccountRouter.settle_stream_end(attempt, usage, *, reason)` to accept `StreamUsageEstimate` as well as its existing variants. No new public request parameter is added.

   `RejectionZero` contains `http_status: int`, `provider_error_type: str`, `rejection_class: str | None`, and `original_reason: str`; validates the exact pairs in Decision 1; and has cost zero. A qualifying 400 has original reason `request_rejected`. Its constructor normalizes an absent diagnostic class to `other`; any non-null class must be in the closed set. On first runtime closure with no prior recorded reason, write that normalized class into the rejection column and evidence. If a prior `request_rejected` reason exists, preserve its stored class, including null, in both the column and new evidence; incoming reclassification does not replace it. Typed reconciliation always preserves the existing rejection columns and uses their stored class, including null, in evidence. The first-failure snapshot remains the pre-settlement fields including nulls. Outward errors retain the current classifier’s validated type/class; stored prior diagnosis may differ. These class choices affect audit representation only, never qualification. For 529, record `overloaded_error` only in structured settlement evidence, set its evidence class to null, and leave both existing rejection columns and both outward rejection fields null; the outward code remains `provider_error`, HTTP status remains 529 and bounded retries remain unchanged. A nonqualifying 529 remains `UnknownOutcome(provider_error)` with null rejection columns. Do not infer a message class for 529.

   `StreamUsageEstimate` has exactly the fields of its shared-contract signature below, which is its single definition: the original ending reason, exact served model, validated observed counts, optional validated request id, the quote pricing inputs, their `quote_origin`, the applied `rates`, `observed_cost_nusd`, `margin_tokens`, `margin_nusd` and `cost_nusd`, all amounts/counts integers except rational rates represented by integer numerator/denominator. Its constructor normalizes invalid request identifiers to null under the rule below and validates every other field strictly; the ledger validates correspondence to the stored quote and recomputes E. Extend construction to `CredentialLedger(path: Path, *, busy_timeout_s: float = 5.0, clock: Callable[[], datetime] | None = None, price_revisions: Mapping[str, PriceRevision] | None = None)`. Copy and freeze that registry at construction, keyed by exact revision identifier; `None` means empty for old-only callers. `get_router` supplies the retained registry including `CURRENT_REVISION`, keyed by its exact revision identifier, and gives that same revision to `AccountRouter` (the current wiring is `lionagi/service/accounts/_router.py::get_router`). For a quote with non-null `pricing_inputs`, reserve validates the model, revision, feature profile, output bound, TTL derivation, maximum rate and recomputed ceiling against this registry before any admission write. Missing revisions refuse with `unsupported_pricing`; with a known revision and model, an output bound above that model’s maximum raises `ValueError` before any write, as do inconsistent inputs. A legacy quote with null inputs follows the existing reserve checks and stores no quote envelope; runtime stream estimation is unavailable for it, while typed operator reconciliation may supply retained inputs under Decision 4. Each value supplies the retained D5 model/feature bounds and rational tier rates. This is a proposed dependency, not an existing class or lookup API. Pricing is a pure lookup and computation inside the transaction, with no disk or network I/O. Missing revision or unsupported pricing raises `AccountRoutingError(unsupported_pricing)` without writes; never fall back to current prices. A pricing-input mismatch against an open row, inconsistent computed amount, or a previous-month typed reconciliation raises `ValueError` without writes or block. Contradiction with the first recorded failure instead follows the `settlement_conflict` rule below. For an already-settled row, the reconciled-row exception and duplicate-final-evidence precedence apply before open-row pricing checks. Reconstruct the ledger with an expanded immutable registry to add retained revisions; it is not a mutable global provider lookup. For a legacy row with no quote snapshot, typed reconciliation may supply the missing inputs from retained evidence, validate them against the stored model/revision and ceiling, and record their operator provenance; it may not claim they were captured at reservation time. Rule revision is the literal `adr0126-am9-2026-11-02-v1`, with evidence schema version 1. The shared serialization contract below fixes the key layout; implementation may not choose a different layout for the same schema version.

   Add one nullable text column, `settlement_evidence_json`, containing a validated versioned JSON object, with no free-form message text. Version 1 records the basis, original ending reason and rule revision; it also retains the pre-settlement reason/type/class snapshot, including nulls, whenever an open row is closed by either new basis or typed reconciliation. For rejection zero it records status and validated type, with the first-closure and 400/529 field policy above. For a stream estimate it records the exact model, observed pricing counts, captured price revision, applied margin tokens and nanodollars, and observed priced cost. Extend `Quote` with immutable `pricing_inputs`: final positive output bound Omax, TTL set and any single-TTL derivation, applicable feature/tier selectors, and the rational maximum output rate used for the captured quote. `Reservation` carries that quote. For non-null `pricing_inputs`, at reserve time store those inputs in the `quote` member of `settlement_evidence_json`, with revision and requested model, before dispatch. The open envelope has a null settlement basis; it is not settlement evidence. Settlement retains this quote member and adds the observed vector, exact served model, applicable rates and computed amounts. Persist enough rate data to reproduce E with the retained original revision, never a mutable current price lookup. Existing quote and row shapes lack these inputs (`lionagi/service/accounts/_types.py::Quote`, `lionagi/service/accounts/_ledger.py::CredentialLedger._ensure_schema`). These are structured billing evidence, never request content. The rule revision names this amendment and its dated billing sources, not an arbitrary URL supplied by a response. Origin is the closed value `runtime` for automatic settlement or `operator` for typed reconciliation. The latter additionally carries a validated source reference to retained evidence, never provider prose. Do not populate `usage_json` with a partial vector disguised as complete usage; use the new evidence column and leave `usage_json` null for both new outcomes.

   Set state, cost, outcome, timestamp, structured evidence and applicable rejection fields together in one transaction, retaining attempt identity, original account/month, quote and existing first recorded failure evidence. Reject a new basis inconsistent with that first evidence; do not silently overwrite it. The following consistency table governs final-basis settlement of open rows only; an incompatible reason or contradictory non-null typed field raises `settlement_conflict`, leaves the attempt unchanged, blocks the account and clears warmth in the same committed transaction. Malformed input raises `ValueError` without a write. For a newly qualifying failure, record its original reason in the evidence object. Compare validated objects serialized as UTF-8 JSON with sorted object keys, compact separators, no duplicate keys, floats or nonfinite values; unordered sets are sorted arrays and optional fields are present as null. On a duplicate, reuse the persisted first-failure snapshot and quote provenance rather than taking a new snapshot of the already-settled outcome. For duplicate comparison, reuse the persisted first-failure snapshot exactly, including its original nulls, and replace only the incoming rejection evidence’s class with the stored rejection evidence’s class before canonicalization. Ignore a difference in incoming diagnostic class; it neither conflicts nor changes stored data. All other canonical fields still compare. Compare the full canonical envelope, amount and outcome; equal cost with different evidence is not identical. Validation rejects unknown schema versions and fields. Normalize request identifiers before canonical comparison: accept the existing bounded safe string grammar; convert every other value to null, as `lionagi/service/accounts/_ledger.py::_usage_json` does. Apply this normalization in the `StreamUsageEstimate` constructor as well as on deserialization, before shape validation and comparison; it is the sole normalization exception to malformed-input refusal for `StreamUsageEstimate`. `RejectionZero` separately defaults an absent 400 class to `other` as specified above. The runtime adapter first applies the key-run check; the ledger has no credential and does not repeat that check. Operator evidence must supply a sanitized identifier, or null, under the same confidentiality rule. Runtime stream estimates write that normalized id to both `attempt.request_id` and the evidence. Typed reconciliation preserves the existing attempt request-id column and keeps the normalized supplied id only in evidence; it does not fabricate complete usage. An exact typed reconciliation repeat also compares its adjustment id and source reference. Legacy null evidence means unknown provenance, never permission to infer one of the new bases.

   | New basis | Permitted prior open reason | Additional condition |
   |---|---|---|
   | 400 rejection zero | null, `request_rejected` | Original reason is `request_rejected`; any non-null stored type must equal `invalid_request_error`. Retain any stored class unchanged; class is never a qualification key. |
   | 529 rejection zero | null, `provider_error` | Original reason is `provider_error`; rejection columns remain null. |
   | Stream estimate | null, `cancelled`, `stream_closed`, `transport_error`, `stream_truncated`, `stream_error` | The supplied ending reason must equal any recorded reason. For runtime settlement with a null row reason, the validated `StreamUsageEstimate` built by this attempt’s adapter is sufficient ending evidence. For typed reconciliation, the operator supplies that variant plus `source_ref` identifying retained evidence for the same attempt. No complete terminal result and all Decision 2 checks. |

   These rules also apply to typed reconciliation of open rows. A reason-only `UnknownOutcome` on any settled row is ignored and returns `already_settled`, as today; it does not enter the consistency table. On an open row, the first recorded reason still stands. If `cancelled` has already committed before a later final 400 or 529 basis arrives, that final basis conflicts under this table, retains the open row and blocks/clears; this defines the order without claiming it is reachable in the current adapter. No other prior reason qualifies. A null reason is absence of contradiction, not evidence of qualification. Preserve stored class for audit; do not reject a zero based on a new classification of the same message. No later reason-only write may erase the stored first-failure snapshot.

   **Reservation selectors.** For this schema version, `PricingInputs.feature_profile` has the single allowed value `"messages_standard_v1"`; assign it only after the frozen final payload passes the retained revision's ordinary Messages feature checks in D5. `tier_selector` has the single allowed value `"max_applicable_at_reserve"`; it describes the reservation algorithm, not a chosen observed-usage tier. Derive `output_rate_max` as the greatest output rate over every applicable tier for that exact requested model and accepted profile in the retained revision. Do not guess a settlement tier before usage arrives: compute it from observed fresh + read + created input at settlement, and record its rates in `UsageRates`. The two literals are amendment-owned algorithm identifiers, not a claim that existing PriceRevision objects expose named profiles. Unsupported profile, modifier or selector refuses pricing with `AccountRoutingError(unsupported_pricing)`; an output bound above a known model’s maximum instead raises `ValueError` before any write. Adding literals requires a schema/rule revision; legacy operator evidence must establish the same accepted profile and algorithm, never infer an observed tier from a request default.

   **Closures outside the new bases.** When a new writer closes an open row with `CompleteUsage`, `ProvenNotSent` or untyped `reconcile(evidence=None)`, leave `settlement_evidence_json` exactly as stored: a quote-only envelope remains quote-only and legacy null remains null. Do not synthesize basis, origin, first-failure or evidence fields. Existing outcome, usage and adjustment records remain authoritative for those closures. Ignore that quote-only envelope in old-variant duplicate comparison; never treat it as Amendment 9 settlement evidence. This is the same preservation required of older writers, not a third new basis.

   Add the column to an existing version 1 ledger in the schema transaction, keeping `user_version=1`. The unlocked fast path requires the new column as well as all existing tables, indexes and rejection columns. If incomplete, acquire the write lock and re-read version, tables, indexes and columns together before adding each missing column. Fresh creation includes the same column. The current unlocked completeness check is at `lionagi/service/accounts/_ledger.py::CredentialLedger._ensure_schema`; the locked version/table/column re-read is at `lionagi/service/accounts/_ledger.py::CredentialLedger._ensure_schema`. The existing warm-prefix table and index DDL execute at `lionagi/service/accounts/_ledger.py::CredentialLedger._ensure_schema`, and missing rejection-column DDL at `lionagi/service/accounts/_ledger.py::CredentialLedger._ensure_schema`. Re-reading indexes under that lock and requiring the new column are proposed extensions, not claims about those cited lines. Older rows retain null. Version 1 is deliberately readable by older processes; it is not a capability gate. At the cited version, an older writer sums settled costs by state, not outcome (`lionagi/service/accounts/_ledger.py::CredentialLedger._spend_parts`). Its `UnknownOutcome` on a settled row returns without writing (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`); its open-row reason update and clear are separate (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`). A complete/not-sent settlement with a different outcome blocks with `settlement_conflict` and retains that row (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`). Its reconciliation refuses a settled row except an identical recorded adjustment (`lionagi/service/accounts/_ledger.py::CredentialLedger._reconcile_sync`). It ignores the added column, cannot emit either new variant, and can still settle an open row by old rules, leaving its quote-only envelope intact. New readers must use state plus validated basis: a quote-only envelope on an old-writer settlement is not evidence of an Amendment 9 outcome. Old writers can conservatively block on a replay they cannot recognize; this availability cost is accepted, not prevented by the schema. Do not promise compatibility for arbitrary unexamined older releases. Verify these cases against the cited version before deployment, including correction and reconciliation reads.

### Shared evidence and diagnostic contracts

These are proposed data contracts, not descriptions of the existing implementation. All types below are immutable; integer fields reject booleans, counts are nonnegative, denominators positive, and money obeys D4's signed-64-bit limits.

- `Rate(numerator: int, denominator: int)` is a nonnegative rational nanodollar rate reduced to lowest terms.
- `PricingInputs(output_bound: int, cache_ttls: tuple[str, ...], single_ttl: str | None, feature_profile: str, tier_selector: str, output_rate_max: Rate)`: output bound is positive; TTLs are a sorted unique subset of `("5m", "1h")`; `single_ttl` is fixed at reservation to the sole member when the final request’s TTL set has exactly one member, otherwise null. It records permission for D5 single-TTL derivation, not whether derivation later occurred. At settlement an absent usage split may use that permission; an explicit split is validated directly, and absent cache-write usage uses only D5’s documented-zero rule. No observed split, explicit split or lack of cache writes changes the reserved value. Feature and tier selectors are exactly the two amendment-owned literals defined in Decision 6; rates are derived from the retained revision, not from these strings alone. `Quote.pricing_inputs` has type `PricingInputs | None = None` for legacy construction; `Reservation.quote` carries it.
- `UsageCounts(input_tokens: int, cache_read_input_tokens: int, cache_write_5m_tokens: int, cache_write_1h_tokens: int, output_tokens: int)` contains the validated observed vector, with cache totals checked before constructing it.
- `UsageRates(input: Rate, cache_read: Rate, cache_write_5m: Rate, cache_write_1h: Rate, output: Rate)` contains exact served-model rates at the selected tier, recomputed against the retained revision.
- `StreamUsageEstimate(original_reason: str, served_model: str, counts: UsageCounts, request_id: str | None, pricing_inputs: PricingInputs, quote_origin: Literal["reservation", "operator"], rates: UsageRates, observed_cost_nusd: int, margin_tokens: int, margin_nusd: int, cost_nusd: int)`. Allowed reasons are the stream reasons in the consistency table. Runtime accepts only `quote_origin="reservation"`. Typed reconciliation accepts `operator` only when the legacy row has no quote snapshot, and verifies any supplied inputs against its immutable model/revision/ceiling before persisting them. A row with a snapshot requires matching reservation inputs. A legacy rejection zero requires no invented quote inputs.
- For `settle_stream_end`, use `reason: str | None = None`. With `StreamUsageEstimate`, omitted reason means `usage.original_reason`; an explicit different reason raises `ValueError` before any write. For `usage=None`, omitted reason remains `usage_incomplete`. Other existing variants keep their current handling.
- Typed reconciliation requires `source_ref` to match ASCII `evidence:[A-Za-z0-9][A-Za-z0-9._-]{0,127}` exactly. This is an opaque reference to an operator-retained evidence file, not its contents, path or a provider string. The operator maintains the reference-to-file mapping outside the ledger. Syntax cannot prove file existence or truth: no file is read in the transaction. Untyped existing reconciliation keeps its existing validation; existing source references are not migrated.

`settlement_evidence_json` has exactly these top-level keys, always present: `schema_version`, `rule_revision`, `quote`, `basis`, `origin`, `source_ref`, `first_failure`, `evidence`. Version is integer 1 and rule revision is `adr0126-am9-2026-11-02-v1`. At reservation, `quote` is populated for a quote with pricing inputs; legacy quotes leave the column null. In a populated open envelope, `basis`, `origin`, `source_ref`, `first_failure`, `evidence` are null. A legacy rejection-zero closure may have null `quote`. A stream estimate always has a quote.

`quote` has exactly `requested_model`, `price_revision`, `pricing_inputs`, `quote_origin`; the first two match the immutable attempt. `pricing_inputs` has exactly the six `PricingInputs` attribute names above. `quote_origin` is `reservation` or `operator` under the provenance rules above. `Rate` serializes as an object with exactly `numerator` and `denominator`; TTL tuples serialize as sorted arrays. `first_failure` has exactly `reason`, `provider_error_type`, `rejection_class`, each retaining its pre-settlement string or null. These snapshots are audit data, not qualification from message class.

On closure by either new typed basis, `basis` is `rejection_zero` or `stream_usage_estimate`; `origin` is `runtime` or `operator`; `source_ref` is null for runtime and the validated reference for operator. `evidence` for a rejection has exactly `http_status`, `provider_error_type`, `rejection_class`, `original_reason`, `cost_nusd` (zero); its class follows Decision 6’s first-closure or duplicate rule, not unconditionally the incoming variant. For a stream it has exactly all eleven `StreamUsageEstimate` attribute names listed above, with nested `counts`, `rates` and `pricing_inputs` using their exact attribute names. The quote inputs and provenance duplicated in stream evidence must equal `quote`. Model and count validation remains mandatory; request ids use Decision 6 constructor/deserialization normalization before canonical comparison. Envelope basis, ledger cost and computed cost must agree; operator outcome is still `reconciled`. Unknown fields or versions refuse. Canonical serialization and duplicate precedence follow Decision 6, with the Decision 4 reconciled-row exception checked before final-evidence comparison. Old-writer settlements may retain a quote-only envelope; it never establishes a new basis.

The diagnostic JSON file has exactly `schema_version` (1), `attempt_id` (validated UUID string), `captured_at` (UTC RFC3339), `http_status` (400), `provider_error_type` (validated closed string), `rejection_class` (`other`), `message` (redacted then capped UTF-8 text), `truncated` (boolean), `redacted_message_bytes` (nonnegative length of redacted UTF-8 before cap). No raw length, key length or fingerprint is recorded. Admission to writing is nonblocking with capacity one active writer per process and no queue; saturation emits `writer_busy`. Stamp `captured_at` using `datetime.now(timezone.utc)` at successful capture admission, before handing off the sanitized record; use the same UTC wall-clock source for automatic pre-write cleanup, with explicit cleanup taking its caller-supplied aware `now`. The synchronous classifier offers the sanitized record without waiting to a private process-wide worker thread, outside the event loop and the ledger’s worker runner. An atomic admission gate permits one active job and no waiting jobs; reserve it before handoff, release it in the worker’s `finally`, and do not submit on saturation. The worker performs cleanup then the file write. Explicit cleanup is a synchronous operator call made off the event loop: it acquires the same gate, submits cleanup to that worker and waits for its integer result; if busy, emit `cleanup_failed` and return zero. Automatic pre-write cleanup runs directly within the admitted capture job, without reacquiring the gate. Worker startup or submission failure emits `write_failed` and releases the gate. Only sanitized data may reach the writer. No request awaits worker completion; process exit may lose an uncommitted diagnostic capture without affecting accounting. `cleanup_rejection_diagnostics(ledger_path: Path, now: datetime) -> int` returns the count deleted; `now` must be timezone-aware. A filesystem failure emits `cleanup_failed` and returns the successfully deleted count, without raising into settlement. Cleanup uses the same private-directory/no-symlink rules. Retention is enforced when capture or cleanup runs, not by an unprovided background timer. Age is `now - captured_at`, using a validated UTC RFC3339 timestamp, never filesystem mtime. Delete at age greater than or equal to seven days. Enumerate only regular, non-symlink files with the expected attempt-id filename; validate bounded JSON and the complete diagnostic schema. A corrupt record, missing/invalid timestamp or future timestamp is deleted under the same private-directory rules, never retained indefinitely or logged by contents. If enumeration or deletion fails, emit `cleanup_failed` and skip a new capture until cleanup succeeds; explicit cleanup still returns the successfully deleted count. A clock regression therefore removes future-dated diagnostic records conservatively without changing ledger history.

### Decision — repeated rejections

7. **Typed zero affects utilization; repeated rejection does not automatically block.** Retain D3 ordering, Amendment 5's candidate rule and Amendment 8 Decision 2's operator block. Releasing the held ceiling changes utilization; an account that keeps rejecting remains a candidate until an operator blocks it. Existing authentication and accounting blocks remain. No persistent rejection counter, new block reason, counter schema or migration, or new error code is added. Cost: repeated failures can keep new cold scopes on an account that cannot serve them until an operator blocks it. Zero settlement releases C from open reservations R without adding spend S, removing the accumulation of held ceilings that would otherwise saturate B under D5. A qualifying 529 keeps its bounded same-account retry, with each zero reducing committed utilization from its reserved level; zero accounting therefore supplies no pressure to move that retry or subsequent new scopes away. Fixed bindings remain; a rejected request is not moved or retried on another account. <!-- markdownlint-disable-line MD029 -->

   Option (ii), automatic blocking for repeated typed zero rejections, is not adopted because it can block a healthy account for malformed requests. Reopen it as a later amendment when the same account has at least 60% of the provider's qualifying HTTP 400 `rejection_zero` settlements and at least seven such settlements on each of three consecutive UTC days, while on each day at least one other account is enabled and unblocked in that day’s retained closing snapshot, defined below, and settled at least one complete-usage request that day. Use integer comparison `5 * account_count >= 3 * provider_count`; a zero denominator never trips. This is a review trigger, not proof of a defective account or an automatic block. Seven rather than ten admits a seven/one/one concentration (7/9); an even distribution over two or more accounts cannot pass 60%. The successful other account is an observed service-capacity proxy, not proof that it could serve the rejected request or has unused budget.

   Record counts for every account on every UTC day beside the existing open-holds census, whether or not the trigger fires, including zero counts and accounts no longer enabled. Count settled attempt rows once by immutable attempt id, using validated `settlement_evidence_json.basis == "rejection_zero"`, not `outcome` alone: operator closures retain outcome `reconciled`. Partition counts into 400 and 529 using the evidence status/type pair. Count both origins and report their subtotals; operator backfills count on their settlement day, never their request day. HTTP 529 zeros are reported but excluded from both numerator and denominator of the reopening rule because provider overload is not account-specific inability to serve. `attempt.settled_at`, parsed as an aware datetime and converted to UTC, places each row in the half-open interval from 00:00:00 UTC inclusive to the next 00:00:00 UTC exclusive. Neither `created_at` nor the admission credit-month key assigns this day. Preserve timestamps on duplicate settlement. Before selecting the day, validate every settled attempt row for the requested provider: require an aware parseable `settled_at`, validate every non-null envelope against the versioned schema and stored row identity, cost and outcome, and validate basis/origin and its status/type or stream evidence. A null or quote-only envelope is allowed only for an old-variant outcome (`complete`, `not_sent` or `reconciled`) and contributes no typed-zero count. An outcome of `rejection_zero` or `stream_usage_estimate` with null or quote-only evidence is inconsistent and fails. Any malformed envelope, invalid settled timestamp or inconsistent outcome/basis raises `AccountRoutingError` with code `ledger_unavailable` (`RouterErrorCode.LEDGER_UNAVAILABLE`); return no partial counts. Open rows contribute account names for zero filling but are not settlement candidates. No other provider’s rows are validated by this read.

   Add `async CredentialLedger.settlement_day_counts(provider: str, day: date, *, accounts: tuple[str, ...] = ()) -> tuple[SettlementDayCount, ...]`. In one consistent read transaction through the existing bounded ledger runner, aggregate only attempt rows; return account-sorted immutable `SettlementDayCount(account: str, day: date, rejection_zero_400_runtime: int, rejection_zero_400_operator: int, rejection_zero_529_runtime: int, rejection_zero_529_operator: int, complete_usage: int)` for the union of supplied account names and distinct provider account names in attempt history, with zero-filled counts. `complete_usage` counts settled rows with outcome `complete`, the existing complete-usage outcome (`lionagi/service/accounts/_ledger.py::CredentialLedger._settle_sync`), not reconciliations or estimates. These counts require no new column or stored counter. The daily census caller supplies all declared accounts. For a UTC day ending at boundary T, it takes one closing snapshot during [T, T + five minutes): enabled means present in that caller’s captured active account configuration, and unblocked means the account row has null `block_reason`, read in one consistent ledger read transaction. Retain the configuration account set, account marks, day, and actual UTC read time beside the counts and open holds; this snapshot is the sole source of the status condition and describes census-time status, not eligibility throughout the preceding day. Different process configurations must not be merged into one snapshot. If no snapshot is retained within that interval, the status condition for that day is unknown. Recovery may re-read that day’s attempt counts but must reuse its original retained closing snapshot; a current configuration or block read never substitutes for a missed historical snapshot. A failed or missing day is unknown, never zero, and cannot satisfy the three-day rule. Counts alone are reproducible from attempts; historical enabled/unblocked status depends on the retained daily snapshot.

### What does not change

Ambiguous non-streamed failures, other 5xx and attempts without priceable observed stream usage retain their ceilings. Amendment 11 adds an age-based expiry; there is still no retry-success release, cross-account failover or provider-usage-report integration. Sending long non-streamed calls through streaming is a later amendment. The unconfigured path, account affinity, admission ceiling, price revision selection, not-sent proof and ordinary success results are unchanged. Provider text remains absent from the ledger and public errors. This amendment creates no new blocking, routing or retry action based on rejection class.

### Consequences and verification

Rejections qualifying under the policy can stop consuming local budget. A stream estimate can release budget before final billing is known, so the local ledger may undercount actual spend; the margin is not a hard budget guarantee. A 400 or 529 response can now wait up to fifteen seconds before its routed error is raised: the five-second shielded body read, then the settlement attempt with Amendment 6's per-call worst case of ten seconds; caller cancellation does not shorten the shielded parts. The daily census's validation pass reads every settled attempt row of the provider inside one read transaction, so its cost grows with the ledger's history and recurs for each counted UTC day. Under the ledger's rollback journal that transaction's shared lock holds back settlement commits until it ends, and a commit that waits past `busy_timeout_s` raises `ledger_busy` as under Amendment 6. This amendment does not bound the pass; the retained census record carries the read's elapsed time, so a later amendment can bound it from measurement, for example by a day-keyed scan or chunked validation. Diagnostic capture may support future classification at the cost of an explicitly enabled sensitive local file. Its retention is enforced lazily: until a capture is admitted or an explicit cleanup runs, written files stay on disk past seven days, including after capture is turned off, so an operator ends their retention with `cleanup_rejection_diagnostics`. Typed reconciliation is an operator act, and its trust boundary is the one every ledger write already has, write access to the ledger file: origin `operator` and the `source_ref` syntax check record the operator's testimony without authenticating it, and a mistaken typed closure stays until a sourced adjustment corrects it, because a later runtime settlement on the reconciled row returns `already_settled` without comparison, except for an attempt closed by lease expiry, where Amendment 11 records the evidence as a correction.

MAY implement the two settlement variants and the bounded diagnostic exception. MAY NOT broaden the zero table, estimate missing dimensions, reuse another attempt's evidence, release a ceiling on time alone before the lease of Amendment 11 has passed, or silently sweep old rows. Verify both HTTP paths for exact pairs, class `other`, mismatched or missing types, unreadable bodies and read cancellation. Verify that a stream ending after `message_start` with no qualifying `message_delta` keeps the ceiling, later cumulative stream counts, missing usage, invalid replacements, ambiguous models, every ending in Decision 2, full terminal success, E greater than C, no retry after output, cancellation during write and failed commits. Verify concurrent duplicate/conflicting settlements, locked migration races, legacy null evidence, current-month operator qualification and the specified warmth rules, including block precedence and reconciliation exceptions. Verify diagnostic access controls, byte bounds, redaction, expiry and failure isolation, and Amendment 7's sentinel checks outside the one explicitly enabled file.

Verify that an account that keeps returning typed zero rejections stays a candidate, its zeros move its utilization, and an operator block removes it from selection. Verify daily zero-filled counts, UTC boundary and offset conversion, both settlement origins, duplicate no-ops, legacy null/quote-only exclusion, invalid-data refusal with `ledger_unavailable`, inconsistent null evidence on typed outcomes, 529 exclusion, the five-minute closing-snapshot window, and recovery that reuses the original snapshot or leaves the day unknown; three seven/one/one days with a successful other account enabled/unblocked in each retained closing snapshot reopen the decision, while an even spread, two days, fewer than seven, or absent other-account service does not.

## Amendment 10 (2026-11-09) — the ledger records the HTTP status that ended an attempt

Attempts that end without a priced response stay open at their ceiling, and the ledger keeps no record of the HTTP status that ended them, so an operator cannot tell a pre-execution rejection from a lost response after the fact (issue #4257: 115 open reservations in one month on one ledger).

### Decision

1. **One nullable column.** Add `http_status INTEGER` to the attempt table, in the same schema transaction that adds Amendment 9's `settlement_evidence_json`, keeping `user_version=1`. The unlocked completeness check requires it beside the other columns, the locked re-read adds it when missing, and fresh creation includes it. Rows written before this amendment keep null; there is no backfill.

2. **Written by every routed ending that received a response.** When a routed attempt's settlement or ending-reason write follows an HTTP response for that physical attempt, the same transaction stores that response's status: 200 for complete usage and for a stream that later ends early, and the actual status for every non-success response, whether or not Amendment 9 qualifies it. An ending with no response (connection failure before headers, cancellation before headers, a not-sent proof, process death) leaves it null. The value is an integer from 100 to 599; any other value, including a boolean, raises `ValueError` before a write. A row whose column is already non-null keeps its first value: a later write for the same attempt neither overwrites nor clears it, and a differing later value is ignored rather than treated as a conflict.

3. **Display and diagnostics only.** No admission, routing, account choice, warm-prefix, settlement, blocking, retry or reconciliation decision reads the column. Amendment 9's qualification reads its own typed evidence, and its rejection-zero evidence keeps its own `http_status` field; the column neither substitutes for that evidence nor is checked against it. The open-attempt list is the column's reader. Amendment 9 Decision 4's statement that the ledger row keeps no HTTP status describes rows written before this amendment.

### Consequences and verification

An operator can split open holds by the status that ended them, and a 200 that ended early is distinguishable from a rejection that never executed. The column does not prove what the provider billed, and older rows remain unexplained. Older processes ignore the column and write null, which reads as "no status recorded", never as "no response". Verify fresh creation and the locked upgrade of a version 1 ledger without the column, the null default on every pre-existing row, the status write for complete usage, a non-success response, a stream that ends after a 200 and an attempt with no response, first-value retention on a repeated write, and refusal of out-of-range and boolean values.

## Amendment 11 (2026-11-09) — an open reservation past its lease is closed at zero by the next admission

An attempt that never settles keeps its ceiling in the account's open total, and nothing ends it: Amendment 9 states that there is no age-based expiry, and Amendment 10 only records why a hold ended. On one ledger, 114 reservations from one credit period stayed open for seven to ten days and held most of the budget the four routable accounts had left, so every large reservation was refused (issue #4257 is the same pattern, 115 open holds).

### Decision

1. **A lease.** An open attempt whose age exceeds the ledger's `open_lease` is closed by the next admission for its provider. The default is two hours. One reservation covers one provider call, not a run: over 29,824 completed attempts on one ledger the longest took 575 seconds and 99.9% took under 223 seconds, so two hours is more than twelve times the longest observed call. A caller may pass another positive duration.

2. **Same transaction as the admission.** The closure runs inside the admission transaction, before the account's spend is summed, for the accounts named in that admission and only for the current credit period. Rows of an earlier period are left as they are. Age is `now - created_at` read with the ledger's clock; a row exactly at the lease is still live. An admission that is refused rolls its transaction back, closure included: it closes nothing, and `status()` keeps counting an expired hold as open until the next committed admission closes it. Each admission decides on the totals after its own closure, so the refusal itself is correct.

3. **Closed through reconciliation.** Each closed attempt becomes `settled` at zero cost with outcome `reconciled`, and one `reconciliation` adjustment of delta zero is written in the same transaction, with an id derived from the attempt id so a repeat writes nothing. Its source reference names the attempt, its age, the lease, the attempt's scope, whether the ledger holds a later settled attempt on that scope, and that the ledger saw no provider usage for it. The ledger reads no run record, so the closure says nothing about whether the provider billed.

4. **Late evidence on a closed attempt blocks nothing.** Complete usage or a stream-usage estimate that arrives for an attempt this amendment closed is real spend, so it writes a `correction` adjustment, with origin runtime and a source reference naming the attempt and the closure, and returns `settled`. This holds whatever the cost: an arrival above the attempt's ceiling is recorded the same way and does not block the account. A stream estimate first passes every check an estimate on an open attempt passes (stored quote, revision, model, pricing inputs, recomputed amount, reservation provenance); a mismatched one raises `ValueError` and writes nothing. Typed zero evidence (`ProvenNotSent`, `RejectionZero`) agrees with the zero the closure recorded and returns `already_settled` without a write, as does a reason-only `UnknownOutcome`. The cost recorded for the attempt is the largest cost seen across its late arrivals, so an estimate followed by a larger bill ends at the bill and the bill is never clamped to the estimate: an arrival above the recorded cost writes a further correction for the difference and returns `settled`, and one at or below it writes nothing and returns `already_settled`, with one log line naming the attempt and both costs when it is below. A replay of an arrival therefore writes once, and a runtime that retries with different evidence cannot block the account. A correction that would take settled spend, adjustments and open reservations past a signed 64-bit total is refused with `budget_exhausted` and writes nothing, the range every other write to the account is held to. The attempt's own row stays closed at zero. For every other reconciled row nothing changes: conflicting final evidence is a `settlement_conflict` that blocks the account, as the ledger already does. Four sentences of Amendment 9 that describe late evidence on a reconciled row as `already_settled` carry the lease exception, and `docs/adr/edits/ADR-0126-amendment-11-edits.md` keeps their earlier wording, as it does for the two sentences that ruled out release by age. Blocking an account for a bill that arrives after a lease closed its hold would re-create the failure the lease removes, and keeping only the first arrival would record an estimate where a larger bill followed.

5. **Nothing else reads it.** Account choice, warm-prefix state, blocking, retry and the schema are unchanged. No migration is needed and `user_version` stays 1.

### Consequences and verification

A hold that leaked stops consuming budget after two hours without an operator act. A zero closure can understate spend by the cost of a call that billed but never reported. The worst case per closed hold is its full ceiling; the expected size is the average settled cost per call, a few cents on the ledger measured. Repeated holds that never settle therefore keep recorded spend below the provider's bill by up to their ceilings, and the ledger is not a hard budget guarantee. Tests pin the closure, the exact boundary, the budget a stale hold was refusing, the earlier-period exemption, once-only closure, the named later-settled attempt, late complete usage, including one above the ceiling, and a late stream estimate each writing one correction without a block, a mismatched estimate refused, an estimate followed by a larger bill recording the bill, a smaller arrival after the bill writing nothing and logging the drop, a replay writing once, typed zero and reason-only writes staying `already_settled`, and the conflict kept for a row an operator reconciled; removing the sweep call, moving the boundary, or removing any late-evidence rule fails them.

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
`.env.local`, then `.secrets.env` (`lionagi/config.py::AppSettings`). Provider metadata
supplies that name to endpoint configuration
(`lionhub/src/lionhub/providers/anthropic/_config.py` `AnthropicConfigs._API_KEY_ENV = "ANTHROPIC_API_KEY"`,
`lionagi/service/connections/provider_config.py::ProviderConfig.api_key_env`). A missing settings name
falls through to `os.getenv(name, name)`, allowing the unresolved name to become
the credential (`lionagi/service/connections/endpoint_config.py::EndpointConfig._validate_api_key`).

**P2 — A logical call is not a billable attempt.** The path is Branch chat →
`iModel.invoke` → `APICalling` → endpoint HTTP transport
(`lionagi/operations/chat/chat.py::chat`, `lionagi/service/imodel.py::iModel.invoke`,
`lionagi/service/connections/api_calling.py::APICalling._core_invoke`). HTTP retry happens inside the
endpoint, below that event boundary (`lionagi/service/connections/endpoint.py::Endpoint._call_http._make_request`,
`lionagi/service/connections/endpoint.py::Endpoint._call_http`). Response JSON returns directly;
Branch cost aggregation sums reported costs and leaves absent cost unknown
(`lionagi/service/connections/endpoint.py::Endpoint._request_attempt`, `lionagi/session/signal.py::_collect_branch_usage`).
Neither a successful outer operation nor its final response accounts for every
attempt that might have reached the provider.

**P3 — Shared models are not conversation identities.** API Branch clones share
their models (`lionagi/session/branch.py::Branch.clone`). Binding a credential on that
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
configuration, rather than as a hub module. This record restates each lion1 decision it adopts (D2 to D6), except that Amendment 9 permits typed rejection zero, eligible early-stream estimates and sourced closure instead of retaining every unanswered ceiling; it rejects two: lion1's ceiling bounded the prompt by the request body's byte count (lion1 ADR-0028 S15), which is an assumption about the tokenizer rather than a provider bound (D5), and lion1's per-wake pick, which binds a long-lived agent rather than a conversation (D3). lion1's
CLI-account half (keychain items per Claude Code account, homes per Codex account, child
environments) stays out of scope.

| Concern | Decision |
|---|---|
| Declaration and activation | D1: an optional AppSettings account list; no second settings hierarchy. |
| Secret custody | D2: strict named env → dotenv → existing declared lookup, at dispatch only. |
| Assignment | D3: bind a conversation once; a new conversation prefers an account where its stable prefix is warm, then ranks by committed budget utilization. |
| Durability | D4: one local ledger file; selection, reservation and settlement are transactions. |
| Admission and cost | D5: conservative priced ceilings; complete provider usage or Amendment 9 evidence settles once, with sourced reconciliation for open rows. |
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
  (`lionagi/service/accounts/_secret_resolution.py::_ENV_NAME_RE`) with a length bound, so a lookup
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
  Mid-process configuration reload is DEFERRED: restart to load new declarations. Separately, Amendment 9's diagnostic switch is read from process environment exactly once per AccountRouter construction; only "1" enables it. It is not an AppSettings field. Reconstructing the router samples the switch again; an existing router never reloads it.
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

Factor a one-name helper from `lionagi/service/accounts/_secret_resolution.py`; retain
the CLI fill helpers' behavior. They currently validate global-only argv/names,
run with a 15-second timeout and fill all missing declared names
(`lionagi/service/accounts/_secret_resolution.py::resolve_secret_lookup_config`,
`lionagi/service/accounts/_secret_resolution.py::_run_lookup`,
`lionagi/service/accounts/_secret_resolution.py::fill_declared_secrets`,
`lionagi/service/accounts/_secret_resolution.py::_fill_from_resolution`). Reusing the
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
the current eager header path (`lionagi/service/imodel.py::iModel.create_api_calling`,
`lionagi/service/connections/endpoint.py::Endpoint.create_payload`). For routed calls, explicit
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
preserves its id: `to_dict` writes it (`lionagi/service/imodel.py::iModel.to_dict`) and
`from_dict` reads it back (`lionagi/service/imodel.py::iModel.from_dict`). Direct endpoints have
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
Unknown attempts remain open for ranking and admission except when Amendment 9 evidence or sourced reconciliation closes them; use their recorded costs thereafter. Idle bindings do
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
attempt(id, scope_kind, scope_id, provider, account, month_utc, ceiling_nusd, price_revision, requested_model, request_digest, state: open|settled, cost_nusd?, usage_json?, request_id?, outcome?, created_at, settled_at?, provider_error_type?, rejection_class?, settlement_evidence_json?) PK(id)
adjustment(id, kind: correction|reconciliation|unblock, attempt_id?, provider, account, month_utc?,
           delta_nusd, source_ref, created_at)              PK(id)
```

Foreign keys bind attempts to accounts/months; settled requires a nonnegative
integer cost, open requires null cost. Only sanitized numeric usage, validated bounded provider request ids, closed diagnostic fields and Amendment 9's validated quote/evidence envelope are stored as response data. Never store prompts, request bodies, headers or provider prose in the ledger; the optional diagnostic file is a separate, explicitly bounded exception to dropping message text.
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
  restart never replays an open attempt. A crash just before dispatch retains its ceiling until transport-owned proof of no dispatch or explicit sourced reconciliation resolves it. A response from another physical attempt cannot release this hold; neither complete usage nor an HTTP rejection can belong to an attempt that never dispatched. Age or crash alone authorizes no release until the lease of Amendment 11 has passed.
- Settle by attempt id and immutable account/month, not current scope or current
  month. Reason-only writes on settled rows and runtime settlement after reconciliation return `already_settled` without blocking except for an attempt closed by lease expiry, where Amendment 11 records the evidence as a correction. Otherwise identical repeated final runtime evidence is a no-op and conflicting final evidence commits a `settlement_conflict` block, retains the attempt row and raises `settlement_conflict` after the transaction. Reconcile on a non-open row refuses with `settlement_conflict` and no block write, except identical adjustment replay.
  Reconciliation is an explicit sourced adjustment, never a silent rewrite.

The internal ledger operations are:

```text
Scope = (kind: branch|imodel|endpoint, id: UUID, provider: str)
Quote = (requested_model: str, ceiling_nusd: int, price_revision: str, request_digest: str, pricing_inputs: PricingInputs | None = None)
Reservation = (attempt_id: UUID, scope: Scope, account: str,
               month_utc: YYYY-MM, quote: Quote)
Evidence = complete_usage(served_model, numeric_usage, request_id) | proven_not_sent | unknown(reason_code) | RejectionZero | StreamUsageEstimate
reserve(scope, attempt_id, quote) -> Reservation
settle(attempt_id, evidence) -> settled|already_settled|still_open
reconcile(adjustment_id, attempt_id, cost_nusd, source_ref, *, evidence: RejectionZero | StreamUsageEstimate | None = None)
adjust(adjustment_id, account, month, delta_nusd, source_ref, attempt_id=None)
clear_block(adjustment_id, account, source_ref)
```

Runtime settlement closes a row only with complete usage, transport-owned proof of no dispatch, or Amendment 9 typed rejection-zero or stream-estimate evidence. `unknown` records a reason without changing its reservation. Explicit sourced reconciliation closes an open row through its separate operation; Amendment 9 operator closure supplies typed evidence and obeys its current-month eligibility fence. Ledger identity and
the captured quote, rather than fields supplied by a response, choose the charge
account and month. A quote's validity is checked again inside admission. Normal runtime settlement after rollover still charges that immutable original month.

`reconcile` atomically closes an open attempt at a nonnegative cost and inserts one zero-delta reconciliation row, so cost enters S **once** through the attempt. With Amendment 9 typed evidence it checks current-month eligibility and consistency, recomputes zero or E, and rejects amount mismatch before writing. Untyped sourced reconciliation retains its existing scope. Identical adjustment replay precedes open-state/month checks; a different call on a non-open row refuses without blocking. `adjust` changes S by its signed delta without rewriting or closing
an attempt. Amendment 9 estimates record E, an accounting policy value, not a verified provider bill. An adjustment or settlement taking recorded S + R above B records the truth of the accounting operation and blocks further admission; E > C also records ceiling_exceeded even when the true bill fits. `clear_block` inserts a zero-delta unblock row with no
month; restart never clears marks. Duplicate adjustment ids are no-ops only for identical inputs. For typed reconciliation, those inputs include canonical typed evidence and source reference. Later conflicting evidence needs a new sourced correction; neither it nor reconciliation silently replaces a settled cost. Runtime evidence arriving after reconciliation returns `already_settled` without comparison, mutation or blocking, except for an attempt closed by lease expiry, where Amendment 11 records the evidence as a correction; other final-evidence conflicts follow Amendment 9 Decision 6.

MAY migrate this private file under its own version gate; MAY NOT create a second
ledger on lock/I/O failure or infer zero from a missing response. Verify using two
processes, fault injection at commit/send/settle, and duplicate/conflicting ids.

### D5 — Price a ceiling before every attempt, settle from complete usage or Amendment 9 evidence

Money uses integer nanodollars (`1 USD = 10^9`), never binary float. A provider-owned,
dated price revision specifies exact model ids, accepted request features, maximum
billable input tokens, maximum output tokens, and tiered rational nanodollars per
token for fresh input, 5-minute write, 1-hour write, read and output. It includes
source URLs and the date the prices were read; a model or request feature the
revision does not price refuses. A revision carries no expiry date (see Alternatives).
Persist the revision with each attempt and keep it available for later settlement.
Never select a rate by substring or assume an alias still denotes the same model.

Initially support ordinary Messages requests with text, base64 images, client tools and supported cache TTLs; streaming uses the same admission ceiling and exact complete-usage pricing. Only eligible early-ended streams use Amendment 9’s observed-cost-plus-margin estimate; ambiguous non-streamed calls keep their ceilings. Server tools,
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
where that API version documents omission as zero. Otherwise missing/inconsistent pricing dimensions, an unknown served model or a new billing dimension keeps the ceiling and cost unknown. Amendment 9 alone permits missing terminal evidence for a priceable early-ended stream with an actually observed output count; it settles an estimate, not complete usage. Input tier selection uses fresh + read + created input; output
already includes thinking tokens, which must not be added a second time.
The pricing categories follow [provider pricing](https://platform.claude.com/docs/en/about-claude/pricing).

Complete-usage settlement replaces C by A in one transaction. If A exceeds C, record the true A and block the account with `ceiling_exceeded`; never clamp the bill. Amendment 9 instead replaces C by zero for qualifying typed rejections or by E = Aobs + M for eligible early-ended streams. E includes a policy margin, not just provider usage: record E without clamping and block with `ceiling_exceeded` if E > C, even if the eventual true bill is within C. Every block clears warmth. Admission's
hard invariant is over this local, version-priced ledger. Its recorded costs now include estimates. For complete-usage accounting, keeping real charges within B additionally assumes the provider respects the documented token/rate bounds and all spending on the allocation goes through this ledger. For stream estimates those assumptions are insufficient: unobserved billed usage may exceed M, and E is not a provider-charge bound. External
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
The owner revokes the one-use dispatch ticket and joins or accounts for that worker before claiming “not sent”; an uncertain handoff keeps the ceiling until transport proof, complete usage, a `RejectionZero` for the same physical attempt with a null or matching recorded request/provider-error reason, or a `StreamUsageEstimate` for that attempt with a null or identical recorded stream-ending reason or explicit sourced reconciliation resolves it. In particular, a committed cancelled reason conflicts with later 400/529 zero evidence; it cannot be released by that pair alone. A committed `cancelled` reason permits only a stream estimate with original reason `cancelled`, priceable observed usage and no complete terminal result; a later transport-reason estimate also conflicts. Joining or accounting for the worker must supply the actual attempt’s typed evidence, not merely observe that the worker finished. Uncertainty itself never proves not-sent or zero.

| Outcome | Ledger and account action | Retry |
|---|---|---|
| Local validation/lookup/admission refusal | No send; no cost. If already reserved, settle zero only with transport-owned proof it was never dispatched. | None. |
| Complete priced response, even if parsing/hook later fails | Settle from usage before outward completion. | Never retry merely because accounting or a consumer failed. |
| 401/403 | Persist `auth_required` before propagation; keep ceiling absent complete billing evidence. | None; never another account. |
| 429 | Classify rate limit, not monthly exhaustion; keep ceiling without complete billing evidence. | Existing bounded backoff on the same account, with a new reservation. |
| 400 `invalid_request_error` | Classify request rejection and settle zero under Amendment 9's bounded typed-evidence policy, irrespective of class. | None. |
| 529 `overloaded_error` | Settle zero under Amendment 9's bounded typed-evidence policy; no quota/auth mark inferred. | Existing bounded retry policy, same account and a new row. |
| Other 5xx (including nonqualifying 529), timeout, transport loss | Unknown cost keeps ceiling; an eligible early-ended stream instead records E under Amendment 9. No quota/auth mark is inferred, but E > C records `ceiling_exceeded` and clears warmth. | Existing bounded retry policy, same account and a new row; no automatic retry after stream output starts. |
| Other 4xx, including 413; 400 failing Amendment 9's typed-evidence rule | Classify request rejection; keep ceiling without complete billing evidence. | None. |
| Cancellation, process death or stream truncation | Settle any complete evidence already captured, or an eligible early-ended stream estimate E under Amendment 9; otherwise retain ceiling. E > C blocks with `ceiling_exceeded` and clears warmth, including on cancellation. Process death without a committed settlement retains the durable ceiling. | No cancellation replay. |

Status alone is not proof of a zero bill. Amendment 9 permits zero settlement only for its closed HTTP status/type pairs as a policy interpretation of the quoted failed-request statement. Streams merge initial usage and later cumulative fields by replacement, not addition; complete terminal evidence uses exact pricing, while only eligible early-ended streams use Amendment 9's estimate. Intercept HTTP accounting before redaction buffering, generic stream projection and consumer yields. The accepted record describes generic projection as retaining raw events (`lionagi/service/connections/endpoint.py::Endpoint._line_to_stream_chunk`, `lionagi/service/connections/endpoint.py::Endpoint._event_to_stream_chunk`); the additional pre-redaction accounting parser is proposed. No automatic retry after
stream output starts. Stream close/finalization shields an available settlement
from caller cancellation; the shield waits at most five seconds for a worker thread
token (admission) and at most five seconds for the SQLite lock (`busy_timeout_s`), a
worst case of ten seconds, and a timeout in either wait raises `ledger_busy`
(Amendment 6). If commit
fails or the process dies, the durable ceiling remains. It must never report the
accounting operation as committed when it was not.

Routed errors expose only `code`, optional validated account/attempt ids and HTTP status through the existing event/exception paths. Request rejections additionally expose Amendment 7’s validated type/class fields. A qualifying 529 keeps these outward rejection fields absent and retains provider_error; no provider prose is exposed. Required codes are
`credential_missing`, `credential_invalid`, `credential_lookup_failed`,
`credential_conflict`, `unsupported_pricing`, `budget_exhausted`, `account_blocked`,
`account_config_conflict`, `ledger_busy`, `ledger_unavailable`, `duplicate_attempt`,
`settlement_conflict`, `ceiling_exceeded`, `auth_failed`, `rate_limited`,
`request_rejected`, `provider_error` and `transport_error`. No raw provider message is required.
The ledger is the authority on recorded routed spend, including estimates rather than verified provider charges; successful user-facing result types, response bodies and normal chunk shapes stay unchanged apart from secret redaction. Failure accounting must remain queryable even without an outer result.

The unconfigured path bypasses all router resolution, ledger I/O, price checks,
marks and new error handling. It retains current direct-key and unresolved-name
semantics, retry/cache behavior, return types and serialization. Tightening
`EndpointConfig._validate_api_key` globally is explicitly not authorized here.
Configured routing disallows the endpoint's application response cache initially:
its current wrapper receives headers (`lionagi/service/connections/endpoint.py::Endpoint.call._cached_call`),
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
- Unresolved unknown attempts and conservative ceilings deliberately reduce availability; qualifying rejection and stream evidence can now end those holds. Amendment 9 permits policy-qualified rejection zeros and explicitly estimated early-ended stream settlements, including explicit operator reconciliation of qualifying current-month open rows with typed evidence and a retained source. Other unknown attempts retain their ceilings in their original admission month until ordinary sourced reconciliation; a cancellation can remain unresolved indefinitely. Month rollover does not erase or settle old rows, and elapsed time never invents a free request.
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

### Automatic failover, expiring unknown attempts or zero costs from status alone

All improve availability, but a lost request may already be billed. Failover can duplicate work on another credential; expiry or zero assumed without the closed status/type evidence restores money without an authorized basis. Expiry and assumed zero are not permitted. Amendment 9's zero settlement instead requires its closed HTTP status/type pair, as a policy interpretation of the quoted failed-request statement; status alone, elapsed time and a successful retry are not sufficient evidence. Its separate early-ended stream rule explicitly permits an estimate from observed usage plus a policy margin; that estimate is not complete billing evidence or a bound on the final charge. Every other unknown attempt retains its ceiling until explicit sourced reconciliation. Explicit failover is DEFERRED: a future caller-visible operation must
create a new conversation/binding, retain old reservations, and acknowledge
possible duplicate effects. It must never be implemented as a hidden retry.

### Broad first release

CLI accounts, live provider invoice reads, custom reset dates, administrative
screens, dynamic reload and other providers would need additional transport,
trust or lifecycle contracts. They are DEFERRED. Other key providers may later
implement the same quote/reserve/settle semantics with their own pricing and
response evidence; no generic provider plug-in system is introduced now.
