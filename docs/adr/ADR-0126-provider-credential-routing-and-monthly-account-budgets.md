# ADR-0126: Provider credential routing and monthly account budgets

- **Status**: Accepted, amended 2026-10-14 and 2026-10-26 (see Amendments 1 and 2)
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
`.env.local`, then `.secrets.env` (`lionagi/config.py:169`, `lionagi/config.py:191`). Provider metadata
supplies that name to endpoint configuration
(`lionagi/providers/anthropic/_config.py:23`,
`lionagi/service/connections/provider_config.py:98`). A missing settings name
falls through to `os.getenv(name, name)`, allowing the unresolved name to become
the credential (`lionagi/service/connections/endpoint_config.py:132`).

**P2 — A logical call is not a billable attempt.** The path is Branch chat →
`iModel.invoke` → `APICalling` → endpoint HTTP transport
(`lionagi/operations/chat/chat.py:81`, `lionagi/service/imodel.py:697`,
`lionagi/service/connections/api_calling.py:268-275`). HTTP retry happens inside the
endpoint, below that event boundary (`lionagi/service/connections/endpoint.py:895`,
`lionagi/service/connections/endpoint.py:918`). Response JSON returns directly;
Branch cost aggregation sums reported costs and leaves absent cost unknown
(`lionagi/service/connections/endpoint.py:826`, `lionagi/session/signal.py:346`).
Neither a successful outer operation nor its final response accounts for every
attempt that might have reached the provider.

**P3 — Shared models are not conversation identities.** API Branch clones share
their models (`lionagi/session/branch.py:576`). Binding a credential on that
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
(`lionagi/providers/_secret_resolution.py:88`,
`lionagi/providers/_secret_resolution.py:214`,
`lionagi/providers/_secret_resolution.py:373`,
`lionagi/providers/_secret_resolution.py:431`). Reusing the
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
the current eager header path (`lionagi/service/imodel.py:375`,
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
preserves its id: `to_dict` writes it (`lionagi/service/imodel.py:841`) and
`from_dict` reads it back (`lionagi/service/imodel.py:924`). Direct endpoints have
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
from caller cancellation, bounded by the ledger's five-second wait. If commit
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
