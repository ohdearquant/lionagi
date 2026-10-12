---
adr: ADR-0016
status: draft
liveness: operating (the command tree, the shared lock, the spend row with its count of calls of unknown cost, a same-day row from before the count, the readers' mark of a partial sum, the chat's total-first cost line and the text `--stats` prints are under tests; the phone page renders the mark untested)
date: "2026-09-25"
area: command
kind: new
depends_on:
  - "[[ADR-0007-the-record|ADR-0007]]"
  - "[[ADR-0009-backends|ADR-0009]]"
  - "[[ADR-0010-delegation|ADR-0010]]"
tags:
  - adr
  - product
  - command
---

# ADR-0016: The lion command and the spend row

## Context

A long-running agent is a plain process whose state is files in its own directory. Someone has to
start it, run one step of it by hand, leave evidence a reader can open later, and measure whether it
saves tokens: tokens per handled message beside polls that cost nothing. This record fixes the one
command that does this, the row the agent writes per day about what it spent, who reads that row,
how a one-shot form shares the serving process's lock, and what the command leaves alone.

The spend comes from the envelopes a backend keeps per call ([[ADR-0009-backends#^c5|ADR-0009/C5]]);
the row lives in a note store ([[ADR-0007-the-record#^d3|ADR-0007/D3]]); the wake, the caps and the
supervision are the long-running agent's ([[ADR-0013-the-agent|ADR-0013]]). Source:
`apps/cli/pyproject.toml`; `main.py`, `agent.py`, `chat.py`, `view.py`, `config.py`, `areas.py`,
`context.py` and `account.py` in `apps/cli/lion_cli/`; `Agent._claim`, `Agent._usage`,
`Agent._spend` and `_line` in `hub/agent/agent.py`; `resident` and `wakes` in `hub/areas.py`.

## Definitions

- **spend row**: the `spend` note of the agent's `agent` profile in the store
  ([[ADR-0025-the-graph|ADR-0025]] D6): one row per local day of model calls, tokens, cost, wakes
  that ran, mail handled and idle polls.
- **one-shot form**: a `lion agent` form that acts once and exits: `--check`, `--ticks`, `--arm`,
  `--once`, `--rerun` and `--forget`.
- **landing file**: `landing/<date>-<name>.txt` in the agent directory, written by `--check` and
  `--arm` and overwritten by the day's latest run of that name.
- **idle poll**: a poll of the agent's inbox that found nothing to run and made no model call.
## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | A backend's envelope carries the call's usage and, when the provider states one, its `total_cost_usd`. | [[ADR-0009-backends#^c5\|ADR-0009/C5]]; the router and the session CLI keep `calls`; the code-only backend keeps an empty list | the row counts the call and reads zero tokens and zero cost for it |
| A2 | The machine's local day is the day a reader of the row means. | `_day` in `hub/agent/agent.py` formats `time.localtime` | a reader in another zone attributes late wakes to the wrong day (S4) |
| A3 | One agent directory is one identity: no two directories name the same actor. | `build` takes the actor from `chores.toml` and the client from the directory's `.khive/config.toml`; the lock sits in that directory | two processes act as one actor, each holding a lock of its own (S1) |

## Claims

### C1: One command, `lion`, holds every entry _(enforced: process)_ ^c1

The workspace member `apps/cli`, package `lion_cli`, declares the one console script, `lion =
"lion_cli.main:main"`; the root package declares none. `parser()` requires one of ten subcommands.
The package composes the runtime and `hub` and talks to a terminal; the runtime package imports
neither `hub` nor `lion_cli`, and `hub` does not import `lion_cli`.

| form | what it runs | what it writes |
| ---- | ------------ | -------------- |
| `lion agent --dir D` | the agent built from `D`, serving until interrupted; refused after the claim and before any other act while `D` routes the lion tables and no fold point bounds the chain [[ADR-0026-the-submission|ADR-0026]] C6 has its wakes continue | the lock, the missing reminders at start, the agent's notes, ledgers and sends; a line per wake, failure and change of hold on standard output |
| `lion agent --dir D --once [--wait S]`, `--rerun ID...` | one wake on mail that arrives, or on read mail named by id | the missing reminders first, none for a chair, and a failed agenda read raises there, before the wake (`test_a_failed_agenda_read_raises_before_the_wake_once_or_rerun_asked_for`); what a wake writes; the record printed |
| `lion agent --dir D --forget RUN` | the owner's act after a refused wake ([[ADR-0026-the-submission|ADR-0026]] C6, C9): the cursor lets go of `RUN` when it names that run and the store cannot replay it, and it runs where the serving form is refused; given beside another action, the parser refuses it | a `forgotten` row naming the run, why and when, then the cursor without it; no mail read, no reminder, no model call |
| `lion agent --dir D --check NAME` | one instrument, outside its chore | a landing file; no ledger row, nothing sent |
| `lion agent --dir D --ticks` | the chores' missing reminders | the reminders on the khive schedule; none for a chair |
| `lion agent --dir D --arm` | the identity read and the boundary probes (D2) | a landing file; exit 0 only when every probe holds |
| `lion agent --dir D --stats` | a read of the notes and the ledgers (C4) | nothing |
| `lion chat` | a console actor over a directory ([[ADR-0018-chat-in-a-box|ADR-0018]]); its flags default from the config file; `--khive-config` names the file its khive calls sign with, and a file that names no actor or leaves the lion tables unrouted refuses the chat before it starts ([[ADR-0026-the-submission|ADR-0026]] C7, C8); the instruction files close its guidance | `.lion/chats/<id>.jsonl` with a usage row per call, and beside it `.lion/history`, a diagram page per mermaid block; its record and notes as rows of the lion database ([[ADR-0025-the-graph|ADR-0025]]); a box's patch, applied only under `--apply`; the clipboard under `/copy` and `/copy all` |
| `lion run --dir D --profile P --say TEXT` | one job of `[profile.P]` in `D`'s `profiles.toml`, on its model or `--model`'s; `--file` adds an input, `--var` makes `--say` a template, `--thread` and `--notify` place the result mail ([[ADR-0026-the-submission|ADR-0026]] C1, C3, C4) | the run's rows in `D`'s lion database, its id first on standard output and its output at the end; one result mail; a Daytona box's patch as `.lion/runs/<id>.patch`, never applied |
| `lion flow --dir D --name N` | `[flow.N]` of `D`'s `flows.toml`, each step a run of its own under its profile ([[ADR-0026-the-submission|ADR-0026]] C2, C3); `--var`, `--thread`, `--notify` | the flow's run row, a step row per step or item, each step's own run rows; the flow's id, then `step <name> <id>` as each opens, the output at the end; one result mail |
| `lion result ID --dir D [--history]` | a read of a run's or a flow's row by the id the command printed ([[ADR-0026-the-submission|ADR-0026]] C4) | nothing |
| `lion mcp --dir D` | a stdio MCP server for `D`'s actor whose three tools are `run`, `flow` and `result` ([[ADR-0026-the-submission|ADR-0026]] C5); needs the `mcp` extra | nothing itself; `run` and `flow` start `lion run` and `lion flow`, which write theirs |
| `lion config [--dir D]` | the chat's effective defaults for `D`, one TOML line per key with the file each came from, then the instruction files it composes | nothing |
| `lion areas [--serve PORT]` | the residents by area, or a page and its API ([[ADR-0017-the-desk|ADR-0017]]) | nothing without `--serve`; with it, a resident's `hold`, `wake-now`, `[budget]` keys, `landing/controls.log`, mail, a launchd restart, and checkpoints through the `lion context` functions |
| `lion context` `set`, `checkpoint`, `restore`, `status`, `show` | a Claude Code or Codex session's checkpoint in the store and its restore, no model call ([[ADR-0019-the-checkpoint|ADR-0019]], [[ADR-0025-the-graph#^c4|ADR-0025/C4]]); `checkpoint --codex` and `status --codex` read a Codex rollout, and `checkpoint` takes a rollout given as `--transcript` as one | the store's `sessions` and `messages` rows for the session, and the actor's `context.<name>` notes for declared vars |
| `lion account` `add`, `list`, `probe`, `remove` | the pool of Claude Code and Codex accounts ([[ADR-0024-accounts-and-the-pick|ADR-0024]]); `add` reads a token without echo; `list` shows each account's readings and mark, and of its credential only `keychain`, `default` or `home`; `probe` makes one model call per account not probed within `max_age`, the others none | `accounts.toml`, where `add` keeps every other byte; a Claude Code account's keychain item; `evidence.json`, where `probe` writes each probe, its readings and the marks it ends; `list` writes nothing |

The gate is code review of any second console script beside `lion`.

### C2: Every form of `lion agent` but `--stats` holds the directory's lock before it acts _(enforced: mechanical)_ ^c2

- **Subject**: `agent_main` in `apps/cli/lion_cli/agent.py`; `Agent._claim`, `Agent._release` and
  `Agent.serve` in `hub/agent/agent.py`.
- **Violated when**: a form other than `--stats` calls khive, lands a file or wakes while another
  live process holds the lock; `serve` claims twice in one process; or `--stats` takes the lock.

`agent_main` builds the agent, claims, runs its one branch and releases in a `finally`. The claim is
an exclusive, non-blocking `flock` on `agent.pid` in the directory whose `.khive/config.toml` names
the actor; the pid inside is for people, read as ASCII digits or as no pid. The operating system
drops the lock with the process, so a stale file never holds.

A refused claim raises before any act, naming the holder's pid. `serve` keeps a lock its process
already holds, so serving and every one-shot form contend for one lock. Release removes the file
while it carries this pid. `--stats` returns before the build and reads while the agent serves.

### C3: The agent writes its spend row from its backend's envelopes _(enforced: mechanical)_ ^c3

- **Subject**: `Agent._usage` and `Agent._spend` in `hub/agent/agent.py`.
- **Violated when**: a token or dollar figure comes from anything but the envelopes on the backend's
  `calls`, a wake's calls are left out of the row, or an idle poll counts as a wake.

At a wake's start its time joins the last hour's list, `runs` rises by one, and the idle polls since
the last wake move into `idle` and `idle_last`. After the run, or before a failed run's exception
leaves, the row adds the mail and notices the wake ran and what the calls since the wake began cost.

That is their count, tokens in (`prompt_tokens`, or input plus cache read plus cache write), tokens
out, the sum of `total_cost_usd`, and `unknown`, the calls it misses (S2). The keys are `day`,
`cost`, `unknown`, `calls`, `in`, `out`, `mail`, `runs`, `idle`, `idle_last` and `wakes`; the sums
restart on a new local day. The cursor keeps the wake's own `tokens` and `idle_polls`.

### C4: Every reader reads what the agent wrote _(enforced: mechanical)_ ^c4

- **Subject**: `stats` in `apps/cli/lion_cli/agent.py`; `resident` and `wakes` in `hub/areas.py`;
  `spent` in `apps/cli/lion_cli/chat.py`.
- **Violated when**: a reader estimates tokens from text, shows another day's row as today's, shows
  a directory it cannot read as zeros with no problem named, or a partial cost without its mark.

`lion agent --stats` prints the row, tokens per handled message, the last wake's cursor, and counts
from the ledger and desk files; a directory with no notes is said, never a zero. `lion areas` shows
each resident's runs, mail and cost for today from its notes and each wake's line from its
`serve.log`; what cannot be read lands as a problem.

The agent reads its own row for its caps: the hour's wakes, the day's cost, the spent fraction a
`[wake]` rule tests, and a continuous run's stop (ADR-0013). The chores digest prints the day's line
to the owner ([[ADR-0015-chores|ADR-0015]]). `lion chat` sums its own backend's envelopes for its
closing line.

### C5: What the command leaves alone _(enforced: process)_ ^c5

`lion agent` supervises nothing: a served agent is the process that runs it, and its restarts belong
to whatever started it. This tree has no host supervisor entry point; the CLI reads the `--model` in
an agent directory's one launchd plist `ProgramArguments` (`apps/cli/lion_cli/daemon.py`), which
wins over the agent's file, and writes no plist. A restart from `lion areas` asks launchd
(ADR-0017).

Only a wake, a chat and `lion account probe` call a model; `--check`, `--ticks`, `--arm`, `--stats`,
`lion areas`, `lion context` and the other `lion account` forms make no model call. The command
estimates no tokens, and the row sums what the envelopes say, never a price table
([[ADR-0009-backends|ADR-0009]] S7). The gate is code review.

## Decisions

### D1: `apps/cli/lion_cli/main.py`: one parser, ten subcommands ^d1

Serves C1. `parser(chat=None)` builds `agent`, `chat`, `run`, `flow`, `result`, `mcp`, `areas`,
`context`, `config` and `account`. `main()` parses `chat` twice, the second time with the config
file's values as defaults; it runs `agent`, `chat`, `run`, `flow`, `result` and `mcp` under
`asyncio.run`, the rest directly, and ends a chat quietly on an interrupt. `context` and `account`
take subcommands (`account`: `add`, `list`, `probe`, `remove`), and `context checkpoint` `--codex`
or `--hook`, never both. At the chat's prompt a line the console owns (`/copy [N]`, `/copy all`,
`/quit`) runs there and never reaches the model.

- **Landing evidence**: `apps/cli/tests/test_chat_command.py`: the chat flags through `parser()`;
  `apps/cli/tests/test_config.py`: the two files, the flag over them, the refusals, `lion config`;
  `apps/cli/tests/test_copy.py`: `/copy` never sent; `tests/test_areas.py`: a registry's table;
  `tests/test_context.py`: `lion context`; `tests/test_codex_checkpoint.py`: `--codex`;
  `apps/cli/tests/test_submit.py`: `run`, `flow`, `result`; `apps/cli/tests/test_mcp.py`: `mcp`;
  `tests/test_accounts*.py`: `lion account`.

### D2: `agent_main`, `land` and `arm` in `apps/cli/lion_cli/agent.py` ^d2

Serves C1 and C2. `build()` makes the agent from `chores.toml`: the trusted senders, the bounded
khive client, the gate, the chores, and the desk when configured. `agent_main` claims first,
`--stats` aside. `Agent.unservable`'s reason refuses the serving form, and `Agent.serve` before its
claim. `land()` writes a landing file. `arm()` reads the actor's own inbox and requires rows
addressed to it, tries five operations and a send to an outsider that the client must each refuse,
quotes each refusal, and fails when `ANTHROPIC_API_KEY` is set in the environment the CLI child
inherits.

- **Landing evidence**: `tests/test_agent.py`: `test_once_refuses_served_identity`,
  `test_a_once_wake_reads_the_pending_ticks_so_a_digest_inside_the_first_grace_is_not_missing`,
  `test_a_once_wake_of_a_chair_makes_no_tick`,
  `test_the_rerun_flag_takes_read_mail_through_one_wake_by_id`,
  `test_the_identity_is_a_lock_the_process_keeps_not_a_pid_it_wrote`,
  `test_serve_keeps_the_lock_its_process_already_claimed`,
  `test_serve_refuses_a_directory_that_routes_the_lion_tables_while_nothing_folds_the_chain`,
  `test_serve_runs_a_store_that_routes_nothing`,
  `test_the_serve_form_is_refused_before_the_start_makes_its_ticks`,
  `test_arm_prints_the_auth_env_the_claude_child_inherits_and_fails_on_an_api_key`,
  `test_the_forget_flag_lets_go_of_the_run_and_makes_no_tick` (the lock is released after),
  `test_the_forget_flag_runs_in_a_directory_whose_serving_form_is_refused`;
  `test_once_refuses_served_identity` holds `--forget` among the forms a held lock refuses;
  `test_forget_beside_another_action_is_refused_before_anything_is_claimed`.

### D3: `backend_for` maps a model name to a backend that keeps envelopes ^d3

Serves C3. The model is the flag's, else `chores.toml`'s, else `deepseek/deepseek-v4.1-flash`. A
`deepseek` name gets the router with a fixed provider order, fallbacks off and data collection
denied; any other name the router's own routing; both a 2,000-token reasoning budget and a stop on
the notification frame ([[ADR-0009-backends#^d1|ADR-0009/D1]]). `claude_code[/<model>]` is the
session CLI in the agent directory without tools of its own: `[claude_code]` takes `timeout`,
`effort` and `cli`, refusing any other by name, and `build` refuses one handed in with tools. The
subscription CLI is D6.

- **Landing evidence**: `tests/test_agent.py`:
  `test_backend_for_claude_code_is_the_cli_chat_shape_on_the_named_model`,
  `test_a_claude_code_key_that_is_not_timeout_effort_or_cli_is_refused_by_name_whatever_the_model`,
  `test_build_refuses_a_claude_code_session_handed_in_with_tools_of_its_own`.

### D4: `_usage`, `_spend`, `_facts` and `_line` in `hub/agent/agent.py` ^d4

Serves C3. `_usage(before)` reads the envelopes on the backend's `calls` from index `before`.
`_usage` also counts the calls whose cost is unknown or incomplete. `_spend` rewrites the day's row
in one put on the agent's store. `_facts` adds the wake's `tokens` and `idle_polls` to the cursor.
`_line` renders the cursor as the serve log's line, with the context the last turn stood in and the
session restarts when present.

- **Landing evidence**: `tests/test_agent.py`: the cost cap from the backend's own figures, each
  burst of a continuous chair booked once, one serve line per wake, the last turn's context on the
  line, `test_a_failed_session_cli_call_is_counted_in_the_days_spend_row_and_still_ends_the_run`,
  `test_a_call_of_unknown_or_incomplete_cost_adds_nothing_to_the_cost_and_the_readers_mark_the_sum`;
  `tests/test_chores.py`: the digest's tokens line,
  `test_the_digests_tokens_line_marks_a_days_cost_that_misses_calls_of_unknown_cost`.

### D5: The readers `stats`, `resident`, `wakes` and the chat's `spent` ^d5

Serves C4. `stats` reads the `agent` notes through `hub.areas.agent_notes`, one call of the binary
as the actor `chores.toml` names, and the directory's ledgers. `resident` reads the same notes, the
`chores.toml` and the lock, drops another day's row, and names what it could not read; serving is
the lock, never the pid in the file. `wakes` parses the serve log with the page's pattern. `spent`
sums each call's `total_cost_usd`, else its `usage.cost`, and counts the calls with neither or with
`cost_complete` false; a chat-log row without `cost_complete` is counted.

- **Landing evidence**: `tests/test_areas.py`: the resident's row read from its own files, serving
  as the lock, the serve log's wake lines,
  `test_a_days_cost_that_misses_calls_of_unknown_cost_is_marked_partial_in_every_view`;
  `tests/test_chat.py`: the cost line over every attempt,
  `test_the_cost_line_marks_a_sum_that_misses_calls_of_unknown_or_incomplete_cost`,
  `test_the_cost_line_takes_a_total_of_zero_over_the_usage_cost_beside_it`;
  `apps/cli/tests/test_chat_command.py`:
  `test_the_logs_usage_rows_read_back_to_the_sum_and_the_count_of_calls_it_misses`,
  `test_a_row_says_cost_complete_by_its_figure_when_the_backend_does_not`,
  `test_a_row_from_before_cost_complete_counts_among_the_calls_the_sum_may_miss`;
  `tests/test_agent.py`: `test_once_refuses_served_identity`, `--stats` under a held lock;
  `test_stats_prints_the_spend_row_the_last_wake_and_the_ledgers_as_this_text`.

### D6: `backend_for` offers the subscription CLI's session as `codex[/<model>]` ^d6

Serves C3. `codex` or `codex/<model>` is the subscription CLI on one thread per run
([[ADR-0009-backends#^d6|ADR-0009/D6]]), the model without tools of its own. `[codex]` takes
`timeout`, `effort`, `cli` and `home`. Any other key is refused by name, `unmeasured` among them, as
is a value its key does not take; `build` refuses a backend built with `unmeasured`. So no served
path runs a CLI release the tools-off claim was not measured on ([[ADR-0009-backends|ADR-0009]]
S23). The chat seeds a continued session's first figure from its last call's answering attempt (S29
there).

- **Landing evidence**: `tests/test_agent.py`:
  `test_backend_for_codex_is_the_session_on_the_named_model_on_a_measured_release_only`,
  `test_a_codex_key_that_is_not_timeout_effort_cli_or_home_is_refused_by_name_whatever_the_model`,
  `test_a_codex_value_that_is_not_what_its_key_takes_is_refused_by_the_key`,
  `test_build_refuses_a_codex_backend_built_for_an_unmeasured_release`;
  `tests/test_codex_session.py`:
  `test_a_chat_on_a_codex_model_keeps_one_thread_and_a_continued_chat_stands_on_its_last_count`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| A console script per agent kind | a kind is a subcommand; one entry and one parser keep every form's flags in one place |
| Token counts estimated from the transcript | the envelope carries the provider's own count; an estimate is a number the agent did not write |
| The command reading the backend on demand | the backend's `calls` live only in the serving process; the notes outlive it and any reader opens them |
| Evidence printed to the console only | a console line is gone with the terminal; a landing file sits where a reader looks |
| A pid file checked, then written | two starters both passed it; an exclusive lock is dropped with its process |
| `--stats` under the lock | it would be refused exactly while the agent serves |
| A price table for the cost | the envelope's figure is the provider's own, summed over the attempts that carry one ([[ADR-0009-backends#^c5\|ADR-0009/C5]]); a table would price an estimate of the tokens |

## Consequences

- **S1**: The boundary: the command is product-side and the in-process form of no kernel concern.
  The process it starts, its identity and its supervision are the long-running agent's
  ([[ADR-0013-the-agent|ADR-0013]]); which model a name binds to, through which route and on whose
  key, stay below [[ADR-0009-backends|ADR-0009]] S5. Two directories naming one actor (A3) are not
  caught here.
- **S2**: A call with no cost figure adds nothing to `cost`, a router call whose `cost_complete` is
  false adds its known subtotal; both count in `unknown`. The cap compares the known sum. While
  `unknown` is not zero, `--stats`, `lion areas`, the digest, the wake line, the chat's closing line
  and the cap's hold print `(partial: N call(s) of unknown cost)` beside the figure, as
  [[ADR-0009-backends|ADR-0009]] S1 asks. The phone page renders it too, untested. A failed session
  CLI call lands its envelope and the row counts it.
- **S3**: For `claude_code` the row's dollars are the figure the CLI prints, taken per call as a
  delta ([[ADR-0009-backends#^c4|ADR-0009/C4]]); whether that figure is a bill under a subscription
  login is not shown here.
- **S4**: The day is the machine's local day (A2); a reader elsewhere converts. A landing file keeps
  one run per name per day, the latest.
- **S5**: Idle polls reach the row only at the next wake: the polls after a process's last wake are
  never written, and a poll the caps held is not an idle poll.
- **S6**: `mail` counts the notices a wake ran, queue tasks and watch notices, beside its mail;
  tokens per handled message divides by that count.
- **S7**: A one-shot form against a served directory is refused; `--stats` is the read that works
  while the agent serves. The text `--stats` prints is pinned whole by
  `test_stats_prints_the_spend_row_the_last_wake_and_the_ledgers_as_this_text`; the numbers under it
  by the tests of D4.
- **S8**: The serve log is standard output; `lion areas` reads it as `serve.log` in the agent
  directory, so a served agent's output has to land there. `lion agent` does not redirect it.
- **S9**: An agent directory is whatever `--dir` names; the checkpoint no longer writes a file
  there, being the store's rows ([[ADR-0025-the-graph#^c4|ADR-0025/C4]]). The lock, ledgers and
  landing files are written wherever the directory is, so it belongs where no checkout tracks it.
  The implementation's own repository ignores `.khive/`, where the bench example keeps its
  directory.
- **S10**: A day's row written before the count carries no `unknown`; its calls cannot be counted
  later. Every reader takes the missing key as `None`, and the first wake after it writes `None`
  there for the rest of the day. So the readers print `(partial: calls of unknown cost before this
  count went uncounted)` beside the day's dollars until the next day, a row held at the cap
  included. A wake's own line counts its calls as ever. Pinned by `tests/test_agent.py`:
  `test_a_same_day_row_from_before_the_count_reads_as_partial_by_an_unknown_number_to_its_end`;
  `tests/test_areas.py`:
  `test_a_days_row_from_before_the_count_is_marked_partial_by_an_unknown_number_in_every_view`; the
  digest test in `tests/test_chores.py`.
- **S11**: For `codex` no call carries a cost figure: a subscription prints none. The row's dollars
  do not move and every call counts as of unknown cost (S2), so the day's cost cap never holds a
  codex agent; its wakes per hour still do.
- **S12**: A `[claude_code]` value its key does not take is refused by the key's name, as D6 says of
  `[codex]`: `timeout` is a number of seconds above zero, since `true` read as one second, and
  `effort` and `cli` are strings that are not empty. Pinned by `tests/test_agent.py`:
  `test_a_claude_code_value_that_is_not_what_its_key_takes_is_refused_by_the_key`.
