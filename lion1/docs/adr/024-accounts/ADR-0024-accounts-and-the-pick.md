---
adr: ADR-0024
status: proposed
liveness: partial (the pool file, a Claude Code token's keychain item, the child's environment as a function that starts nothing, the evidence file under its lock with the two limit-row parsers, and `lion account` add, list and remove are under tests; the agent's `accounts` key, pointing the private home's link, the pick, the probe, the refusal classes, the launch record and every backend's use of these are owed, S1)
date: "2026-10-04"
area: accounts
kind: new
depends_on:
  - "[[ADR-0009-backends|ADR-0009]]"
  - "[[ADR-0013-the-agent|ADR-0013]]"
  - "[[ADR-0016-the-lion-command|ADR-0016]]"
  - "[[ADR-0017-the-desk|ADR-0017]]"
tags:
  - adr
  - runtime
  - accounts
---

# ADR-0024: Accounts and the pick

## Context

This record serves the author's request of 2026-10-04:

- 11:40:29: Claude Code and Codex can each sign in to many accounts; build a load balancer across
  them

The same line asks that the earlier tool's work move into this repository. This record serves the
agent half of that instruction first; moving a person's sessions is S2, and the order is the
author's to reverse.

An agent on Claude Code or Codex runs today on the sign-in its process inherited. The Claude Code
backend runs the CLI's one-envelope form, which carries no limit figure (measured 2026-10-04). No
failure reads as a spent account.

An earlier tool does part of this for interactive sessions. It ranks an unobserved account at 50
percent, reads an expired figure as 0, keeps tokens in plain files and learns a session's account
from the process table.

This record fixes one runtime part: the two CLI providers' accounts, the evidence about each, the
pick at a wake, what a refusal does, how a run's account is known, and the commands. API providers'
keys and the app's screen are later records (S3, S4). Source: `hub/backend/claude_code/__init__.py`
(`_exec`), `hub/backend/codex.py` (`TRANSIENT`, `_env`, `_meter`), `hub/agent/agent.py` (`wake`,
`_spend`), `apps/cli/lion_cli/agent.py` (`arm`).

## Definitions

- **account**: one sign-in of Claude Code or Codex, named in the pool with its provider, its
  credential reference and, optionally, its plan group.
- **pool**: the accounts `accounts.toml` declares for every agent on the machine.
- **credential reference**: where an account's credential is kept: `keychain` for a Claude Code
  token in a keychain item named by the account, a home directory for Codex, or `default` for the
  CLI's own sign-in.
- **plan group**: accounts that draw on one limit.
- **window**: a period over which a provider limits use: five hours or seven days.
- **limit reading**: for each window, the used fraction and the reset instant, with the instant
  observed and the source, a call or a probe.
- **probe**: one small call on a provider's cheapest model, made only to take a limit reading.
- **pick**: the choice of one account for a wake.
- **mark**: what a refusal leaves on an account: out of quota until an instant, or needing its
  person.
- **launch record**: the note naming a wake's account, written before the wake's first child starts.
- **evidence file**: the one file of every account's limit readings, probes, marks and last use on
  the machine.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | Claude Code's stream form carries its limit row under a token sign-in as it does under the CLI's stored login. | measured 2026-10-04: one stream call under a token sign-in carried the row with both windows, its seven-day figure equal to that account's own reading; two calls under the stored login carried it too | Claude Code accounts with a token are usable with figures unknown, and a refusal is their only signal |
| A2 | A keychain item can be read by a job the operating system starts in the background, with the screen locked. | the router's key is read from a keychain item by name (`api_key` in `hub/backend/openrouter.py`); a locked screen was not measured | a served agent cannot set a token, so its Claude Code accounts other than `default` hold its wakes until custody is amended |
| A3 | Two Codex homes keep separate sign-ins, and the CLI writes a refreshed credential back into the home it read it from. | the backend's private home reads one login through a link the CLI writes in place (`hub/backend/codex.py`); two real homes were not measured | Codex accounts cannot be told apart by home, and Codex runs on `default` alone |
| A4 | A Codex `token_count` row names each window's length and the instant it resets. | the session tests' rows carry `used_percent` and `window_minutes` (`tests/test_codex_session.py`); no code here reads a reset | a Codex limit reading has no reset, and only `max_age` ends it |
| A5 | A Claude Code token is `sk-ant-` then letters, digits, `-`, `_` and `.`, 65 to 2048 characters in all. | gitleaks 8.30.1's rules for Anthropic keys: `sk-ant-api03-` and `sk-ant-admin01-`, each with 95 characters more; measured 2026-10-06 on one live token, the access token of Claude Code's stored login: prefix `sk-ant-oat01-`, 108 characters; the token `claude setup-token` prints was not measured | `lion account add` refuses a real token of another form, so its account cannot be added until the form is widened; one not holding `sk-ant-` would also pass as a label |

## Claims

### C1: A credential is written once and shown nowhere _(enforced: mechanical; owed, S1)_ ^c1

- **Subject**: the Claude Code token `lion account add` reads on standard input and the runtime
  reads back, wherever the runtime writes: output, route answers, logs, errors, envelopes, state
  files, screens and every child's arguments.
- **Violated when**: any of them holds that token, any part of it, its length or its prefix; the
  pool keeps a label shaped as a credential (A5); or the runtime opens a Codex home's login file.

No form of `lion account` takes a token as an argument, and the runtime reads one back only for a
child's environment (C2). A keychain tool's failure gives its exit code, never its words. A label
the pool shows is shown as written, so one past 64 characters or holding `sk-ant-` in any case is
refused, and a refusal names the key, never the value. A made-up token put through every command,
route, log and error, and `lion account add` as its own process, is nowhere (S1).

### C2: A child's environment holds the chosen account's credential and no competing one _(enforced: mechanical; owed, S1)_ ^c2

- **Subject**: the environment of every Claude Code and Codex child a wake or a probe starts.
- **Violated when**: a child inherits a credential variable the pick did not set; a Claude Code
  child of an account with a token lacks it; or a Codex child runs on another account's login.

The environment starts from the process's own, removes every variable the CLI takes a credential
from, then sets the chosen one. For Claude Code these are `ANTHROPIC_API_KEY`, which the CLI prefers
when it is set (`arm` in `apps/cli/lion_cli/agent.py`), and `CLAUDE_CODE_OAUTH_TOKEN`, which carries
a token. `CC_ACCOUNT`, the earlier tool's account name, goes too.

For Codex, `CODEX_ACCESS_TOKEN` goes (named in `hub/backend/codex.py`), and `CODEX_HOME` is the
agent's private home, whose login the pick points at the account's (D2). A relative private home, or
one whose login resolves to another, is refused; neither file is opened to tell. The `default`
account sets nothing. The list is completed before D3 lands (S9).

### C3: A limit reading comes from its source, and an unknown figure is never a number _(enforced: mechanical; owed, S1)_ ^c3

- **Subject**: every limit reading the runtime keeps, ranks or shows.
- **Violated when**: a figure its source did not report is kept, ranked or shown as a number; a
  reading lacks its instant observed or its source; or a reading whose reset has passed, or one
  older than `max_age`, counts as fresh.

Claude Code's source is the `rate_limit_event` row of its stream form: each window's `utilization`
and `resetsAt` under `unifiedWindows`, observed when the row is read. It carries a `status` too.
Codex's is the `rate_limits` of the rollout's last `token_count` row, observed at that row's own
`timestamp`, its percentage read as a fraction.

A reading past its reset is no reading. The earlier tool's 0 for an expired figure and its 50 for a
missing seven-day one are rejected by name, and so is its 0 for a missing five-hour one. Accounts of
one plan group share one limit: a reading of one is a reading of all.

### C4: The pick happens once at a wake's start and binds every child of that wake _(enforced: mechanical; owed, S1)_ ^c4

- **Subject**: every wake on a Claude Code or Codex model, each burst of a continuous run included,
  and every child it starts.
- **Violated when**: a child runs on an account other than its wake's pick; a wake picks twice; the
  account used within `keep` is left while a fresh reading says it is usable; or D5's order is
  broken.

A wake picks after it found work and before it writes `taking` or marks its batch read
([[ADR-0013-the-agent#^c2|ADR-0013/C2]]), so an idle poll picks nothing. An account the agent used
within `keep` is kept while the prompt cache is warm. After that the pick ranks every candidate
again, so use spreads.

Usable is under 0.97 of seven days and under 0.95 of five hours. A candidate whose probe gave no
figure ranks after every one with figures, and one whose probe failed is out.

A change of account starts the session from the whole view, as the fold does
([[ADR-0009-backends#^c4|ADR-0009/C4]]).

### C5: A pick with no usable account holds the wake, and the mail waits unread _(enforced: mechanical; owed, S1)_ ^c5

- **Subject**: every pick, and the agent's `accounts` key.
- **Violated when**: a pinned account that is not usable gives way to another; a wake with no usable
  candidate starts a child or marks its batch read; or the hold does not say why.

An agent's `accounts` narrows the pool, and one name pins it. A pin that is not usable holds the
wake and never falls back, and so does a pool with no usable candidate. The hold names each
candidate and why it was passed over, as the caps' hold names its cap
([[ADR-0013-the-agent#^c5|ADR-0013/C5]]), and the mail is there for the next wake.

### C6: Probes are bounded across the machine _(enforced: mechanical; owed, S1)_ ^c6

- **Subject**: every probe any agent or command on the machine starts.
- **Violated when**: one account is probed twice within `max_age`; a probe runs during an idle poll;
  or two picks probe at once.

Readings and probes live in one evidence file, written under an exclusive lock. A pick holds the
lock from its first read to its decision, probes included, so a second pick waits and then reads
what the first learned. A probe is made only by a pick or by `lion account probe`, never by a poll
that found nothing.

### C7: A refusal is classified, the account is marked, and no call moves to another account _(enforced: mechanical; owed, S1)_ ^c7

- **Subject**: every failed Claude Code or Codex call of a wake, and every probe that fails.
- **Violated when**: a failure is read as out of quota with no evidence for it; one with evidence
  leaves its account unmarked; a marked account is picked before its mark ends; or a call is sent
  again on another account.

A failure is out of quota, sign-in failed or passing (D6). The Codex backend retries any failure
holding "429" that `AUTH` does not match. That rule has no class for a spent account, so a quota
refusal holding "429" is retried as a busy server is. D6 says what the class reads instead (S6).

Out of quota marks the account until the reset the refusal names, else until a later probe succeeds.
The wake ends as today, its batch told to the owner ([[ADR-0013-the-agent#^c3|ADR-0013/C3]]), and
the next wake picks again. Sign-in failed marks the account as needing its person.

### C8: A run's account is known from what the runtime wrote _(enforced: mechanical; owed, S1)_ ^c8

- **Subject**: every wake's launch record, every envelope of a Claude Code or Codex call, and the
  spend row.
- **Violated when**: a child starts before its launch record is written; such an envelope lacks its
  account's name; the spend row cannot say what each account spent that day; or anything takes an
  account from another process's environment.

The launch record is a note in the agent's own store, where the cursor is kept
([[ADR-0013-the-agent#^d3|ADR-0013/D3]]). The spend row is built from the envelopes
([[ADR-0016-the-lion-command#^c3|ADR-0016/C3]]), so the name rides each envelope into it. A probe's
envelope names its account and lands in the evidence file, never in an agent's spend.

### C9: A Codex account in the pool authorises nothing _(enforced: process)_ ^c9

Serving an agent on a Codex model takes the operator's own word. No earlier record says so, and this
one does. Such an agent has no cost cap, since no Codex call carries a cost
([[ADR-0016-the-lion-command|ADR-0016]] S11), only its wakes per hour, and Codex accounts of one
plan group draw on one limit. Declaring a Codex account in the pool changes none of this.

## Decisions

### D1: `accounts.toml` beside the areas registry, and the agent's `accounts` key ^d1

Serves C1, C4, C5 and C9. The file sits beside `areas.toml` ([[ADR-0017-the-desk|ADR-0017]]). Each
account is a table of the `account` array under a header line of its own; an inline array, or a
header inside a value, is refused, since D8 edits by those lines. A `[pick]` table holds the
settings. A repeated name, unknown key or value outside its rule refuses the file, naming the key
and rule. An agent's definition takes `accounts`, names of its model's provider. With no file, each
provider's pool is one account on `default`.

| key | rule |
| --- | ---- |
| `name` | unique; a label: letters, digits, `.`, `_` and `-`, at most 64, never holding `sk-ant-` (C1) |
| `provider` | `claude_code` or `codex` |
| `credential` | the credential reference: `keychain` for Claude Code, a home directory for Codex, or `default` |
| `group` | optional; the plan group's name, a label as `name` is |
| `[pick]` `max_age` | seconds a limit reading stays fresh; 600 |
| `[pick]` `keep` | seconds after its last use an agent keeps its account; 3600, the prompt cache's life (`_CACHE_TTL_S`) |
| `[pick]` `seven_day`, `five_hour` | the fractions under which an account is usable; 0.97 and 0.95 |
| `[pick]` `probe_model` | the model a probe calls, per provider; `haiku` for Claude Code, the CLI's default for Codex; a label that may also hold `:`, `/` and `@` |

- **Landing evidence**: `tests/test_accounts.py`:
  `test_a_pool_file_declares_its_accounts_their_plan_groups_and_the_pick_settings`,
  `test_every_refusal_of_the_pool_file_names_the_entry_and_the_rule`,
  `test_no_refusal_of_the_pool_file_repeats_a_value_shaped_as_a_credential`,
  `test_no_file_is_one_account_per_provider_named_default_on_the_default_credential`,
  `test_the_pool_sits_beside_the_areas_registry_and_the_evidence_file_beside_the_pool`; the agent's
  `accounts` key is owed with the pick (S7).

### D2: A keychain item per Claude Code account, a home per Codex account ^d2

Serves C1 and C2. `lion account add` writes a Claude Code token, read on standard input and only in
A5's form, to a keychain item, which D9 reads back. The runtime reads the item as the router's key
is read. An agent's private home ([[ADR-0009-backends#^d3|ADR-0009/D3]]) links its login to the
chosen Codex account's home while no child runs. A child is refused while it links elsewhere, and
with no private home Codex runs on `default`.

- **Landing evidence**: `tests/test_accounts.py`:
  `test_a_token_goes_to_the_keychain_tool_on_standard_input_and_reads_back`,
  `test_a_token_outside_its_form_is_refused_before_the_tool_runs_and_never_repeated`,
  `test_the_keychain_tools_words_never_reach_an_error_only_its_exit_code`,
  `test_no_value_is_both_a_token_and_a_label`,
  `test_a_claude_code_child_carries_its_accounts_token_and_no_competing_credential`,
  `test_a_codex_child_runs_from_the_private_home_and_keeps_no_access_token`,
  `test_a_codex_child_whose_private_home_is_not_its_accounts_login_is_refused`; pointing the private
  home's link is owed with the pick (S1).

### D3: Each backend builds its child's environment from the pick ^d3

Serves C2 and C4. The agent hands its backend the pick at each wake. The backend builds every
child's environment from it as C2 says, and writes the account's name on each envelope. A backend
built with no pick, as a chat's or the bench's is, keeps today's inherited environment (S10). A pick
that differs from the last call's account makes the next call start the session over from the whole
view.

- **Landing evidence**: none yet; owed with the code (S1).

### D4: The evidence file under one lock ^d4

Serves C3 and C6. `evidence.json` beside `accounts.toml` holds, per account, its last reading per
window, its last probe and how it ended, its mark and last use. Every read and write takes an
exclusive `flock` on it, as the agent's pid file is held ([[ADR-0013-the-agent#^d2|ADR-0013/D2]]). A
pick holds it until its decision. Its probes run together, each within 60 seconds, and a pick that
waits past 120 seconds for the lock, or takes it only after, holds its wake. After each call the
backend writes its reading under the lock.

- **Landing evidence**: `tests/test_accounts.py`:
  `test_two_processes_writing_the_evidence_file_at_once_lose_no_write`,
  `test_a_waiter_on_a_file_that_was_replaced_or_removed_takes_the_lock_on_the_path_anew`,
  `test_a_holder_that_waits_past_its_bound_is_refused`,
  `test_a_lock_taken_only_after_its_bound_is_refused_before_anything_is_read`,
  `test_a_holder_whose_file_was_replaced_past_its_bound_is_refused_on_the_new_one`,
  `test_fresh_ends_at_the_reset_instant_and_holds_at_max_age_exactly`,
  `test_usable_at_each_threshold_exactly_and_unknown_with_a_window_unknown`,
  `test_a_plan_groups_reading_is_every_members_and_stays_within_its_provider`, and each parser's
  full, missing and wrong-type rows; the probes and the backend's write are owed (S1).

### D5: The pick, in order ^d5

Serves C4 and C5. A pick runs these steps under the evidence file's lock and writes the chosen
account's last use before it returns.

| step | rule |
| ---- | ---- |
| candidates | the pool's accounts of the model's provider, narrowed by `accounts`; a marked account is out |
| keep | the account this agent used within `keep`, while a fresh reading says it is usable |
| read | every other candidate with no fresh reading is probed once, within C6's bound; one whose probe failed is out |
| usable | under `seven_day` of seven days and under `five_hour` of five hours |
| order | lowest seven-day fraction, then five-hour, then name; after them, candidates whose probe gave no figure, least recently used first |
| none | the wake is held, naming each candidate and why it is out |

- **Landing evidence**: none yet; owed with the code (S1).

### D6: Three classes of refusal, and their marks ^d6

Serves C7. The backend decides the class once per failed call and writes it to the evidence file
before the error goes on. No class sends the call to another account.

| class | read from | does |
| ----- | --------- | ---- |
| out of quota | the limit row's `status` saying the call was refused, a limit reading from the failed call at a window's limit, or the CLI stating a usage limit (S6) | marks the account and its plan group until the reset named, else until a later probe succeeds; the wake ends |
| sign-in failed | what `AUTH` in `hub/backend/codex.py` matches: 401, 403, unauthorized or forbidden | marks the account as needing its person until `lion account add` writes it again or a probe succeeds |
| passing | any other failure | retried as today; a call that still fails ends the wake; no mark |

- **Landing evidence**: none yet; owed with the code (S1).

### D7: The launch record, the envelope's account and the spend row by account ^d7

Serves C8. Before a wake's first child starts, the agent writes `launch` in its own store: the
wake's number, the account's name, its provider and the time. Each Claude Code and Codex envelope
carries `account`. The spend row gains `accounts`, mapping each name to that day's calls, tokens in
and out, and calls of unknown cost. `arm` names the pool's accounts where it now reads `CC_ACCOUNT`.

- **Landing evidence**: none yet; owed with the code (S1).

### D8: `lion account` and its four forms ^d8

Serves C1 and C3. `add <name> --provider P --credential R [--group G]` writes the entry and, for
`keychain`, the standard-input token into its item. `list` shows each account's name, provider,
group, readings with age and source, and marks; never a credential or where one is kept. `probe
[<name>]` probes unless C6's bound was reached, then says when it can. `remove <name>` writes the
file, then deletes a Claude Code account's item, which D9 reads back; a Codex home stays. Both hold
`.accounts.toml.lock` from read to write, refusing it taken late.

- **Landing evidence**: `tests/test_accounts.py`:
  `test_add_writes_the_entry_at_the_end_and_keeps_every_other_byte`,
  `test_add_reads_the_token_without_echo_at_a_terminal`,
  `test_remove_deletes_the_table_and_its_item_and_leaves_the_rest_and_a_codex_home`,
  `test_remove_writes_the_file_before_it_deletes_the_item`,
  `test_a_lock_taken_only_after_its_bound_is_refused_before_anything_is_read`,
  `test_two_adds_at_once_on_one_pool_both_land`,
  `test_list_shows_each_window_with_its_age_and_source_and_each_mark`,
  `test_a_made_up_token_is_in_nothing_any_command_or_refusal_wrote`,
  `test_lion_account_add_as_its_own_process_has_the_token_in_no_processs_arguments`; `probe` is owed
  (S1).

### D9: A keychain item is read back after a write and after a failed delete ^d9

Serves C1 and C2. The keychain tool's exit code after a failure is not measured, so it decides
nothing. After `add` writes a token the item is read back. A refused write that left the token there
deletes it or names the command that does; one that left another value, perhaps part of it, names
that command. A delete the tool failed is read back without the secret: still there, `remove`
restores its table; gone, the delete held; unreadable, the table stays out and the refusal names the
delete command.

- **Landing evidence**: `tests/test_accounts.py`:
  `test_a_write_the_tool_failed_after_storing_deletes_the_item_or_names_the_command_that_does`,
  `test_a_write_that_left_another_value_names_the_command_that_deletes_the_item`,
  `test_a_write_whose_read_back_cannot_be_read_names_the_command_that_deletes_the_item`,
  `test_a_remove_whose_item_is_not_deleted_puts_its_table_back`,
  `test_a_delete_the_tool_failed_after_deleting_reads_back_gone`,
  `test_a_remove_whose_delete_failed_after_deleting_or_cannot_be_read_back_keeps_the_table_out`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| A pick per model call | a session's turns would cross accounts, and each change loses the provider's prompt cache and starts the session over (C4) |
| A pick per serving process | a served agent runs for days and would stay on an account long after its limit was reached |
| Keeping the last account until it is no longer usable | one account is spent to its threshold before the next is touched, and the person's own sessions on it find it spent |
| Ranking a missing reading in the middle of the table | an account never observed may be spent or empty, and a middle rank treats a guess as a figure (C3) |
| Probing every candidate at every pick | at 60 wakes an hour and five candidates it is 300 calls an hour for one agent, while the kept account's own calls already carry its reading (C6) |
| Reading a run's account from the process table | another process's environment is not readable for every process, and the earlier tool's reads missed live ones; the runtime's own record answers (C8) |
| Tokens in plain files | a file travels with every copy and backup of its directory, and its mode is its only guard (C1) |
| Retrying a refused call on another account | the lost call's effects are not known, so a second call may repeat them (C7) |
| One record for this and for moving interactive sessions | the two are separate state machines with separate failures; that record reads this one's pool and evidence (S2) |

## Consequences

- **S1**: Nothing is built. D1 to D9 land with tests named when they land, and each claim marked
  owed names this row. Among them is the canary of C1: a made-up token put through every command,
  route, log and error and asserted absent from all of them.
- **S2**: Moving a person's interactive sessions between accounts is a later record, and so is the
  rest of what the earlier tool does. The earlier tool stays in use until the part that record fixes
  has run and moved a real session.
- **S3**: API providers' keys and limits are a later record. This record takes only the accounts of
  the two CLI providers from what ADR-0022 S15 defers.
- **S4**: The app's screen for accounts is an amendment to ADR-0022. It shows what `lion account
  list` shows and nothing more.
- **S5**: [[ADR-0016-the-lion-command|ADR-0016]] is amended with the code: `lion account` and its
  four forms in C1 and D1 there, the spend row's `accounts` in C3, and `arm`'s account line.
- **S6**: [[ADR-0009-backends|ADR-0009]] is amended with the code. The Claude Code backend runs the
  CLI's stream form and keeps its result row as the envelope, and both backends classify a refusal.
  The fields and wording of a quota refusal and of a failed sign-in are unmeasured for both CLIs, so
  one of each is recorded as a test fixture before D6 lands.
- **S7**: [[ADR-0013-the-agent|ADR-0013]] is amended with the code: the agent's `accounts` key, and
  the pick in both paths that take mail, a wake's and a continuous run's burst. The pick comes
  before `taking` is written and the batch is marked read, since recovery reads a batch left under
  `taking` as lost. A batch carried to a fresh run keeps the pick made when its mail was taken.
- **S8**: A1 to A4 are measured before the code that rests on each lands. `max_age` is a first
  value: one account's five-hour figure rose 0.03 in four minutes (measured 2026-10-04), and the
  default is set again from more such readings.
- **S9**: C2 names the credential variables this tree's code confirms. The rest of each CLI's list
  is taken from its own documentation, each variable with a test, before D3 lands.
- **S10**: The pick serves agents. A chat ([[ADR-0018-chat-in-a-box|ADR-0018]]), the bench and every
  other caller build their backend as today, on the CLI's own sign-in and the inherited environment.
- **S11**: Two agents on one Codex account read one credential file through their links, and a
  refresh by either is written in place for both, as every private home shares the CLI's login
  today. Nothing here orders two refreshes that land at once.
- **S12**: A person's own use of an account spends it unseen, and the runtime learns of it at the
  next call or probe. Two accounts of one plan the person did not group are two limits to the pick,
  which can move between them for nothing.
- **S13**: A probe's cost on a subscription is unknown. It is bounded by count: at most one per
  account per `max_age`, six an hour at the default, however many agents share the machine.
- **S14**: Which account `default` is cannot be told from inside the runtime, so a pool entry with a
  token may be the same account. Grouping the two is the person's act.
