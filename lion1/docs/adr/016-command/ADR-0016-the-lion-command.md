---
adr: ADR-0016
status: draft
liveness: operating (the command tree, the shared lock, the spend row with its count of calls of unknown cost, the readers' mark of a partial sum and the text `--stats` prints are under tests; the phone page shows no mark; a failed session CLI call lands no envelope, so the row misses it)
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
`apps/cli/pyproject.toml`; `main.py`, `agent.py`, `chat.py`, `view.py`, `areas.py` and `context.py`
in `apps/cli/lion_cli/`; `Agent._claim`, `Agent._usage`, `Agent._spend` and `_line` in
`hub/agent/agent.py`; `resident` and `wakes` in `hub/areas.py`.

## Definitions

- **spend row**: the `spend` note in the agent's own store, `notes/agent.json` in the agent
  directory: one row per local day of model calls, tokens, cost, wakes that ran, mail handled and
  idle polls.
- **one-shot form**: a `lion agent` form that acts once and exits: `--check`, `--ticks`, `--arm`,
  `--once` and `--rerun`.
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
"lion_cli.main:main"`; the root package declares none. `parser()` requires one of four subcommands.
The package composes the runtime and `hub` and talks to a terminal; the runtime package imports
neither `hub` nor `lion_cli`, and `hub` does not import `lion_cli`.

| form | what it runs | what it writes |
| ---- | ------------ | -------------- |
| `lion agent --dir D` | the agent built from `D`, serving until interrupted | the lock, the missing reminders at start, the agent's notes, ledgers and sends; a line per wake, failure and change of hold on standard output |
| `lion agent --dir D --once [--wait S]`, `--rerun ID...` | one wake on mail that arrives, or on read mail named by id | what a wake writes; the record printed |
| `lion agent --dir D --check NAME` | one instrument, outside its chore | a landing file; no ledger row, nothing sent |
| `lion agent --dir D --ticks` | the chores' missing reminders | the reminders on the khive schedule; none for a chair |
| `lion agent --dir D --arm` | the identity read and the boundary probes (D2) | a landing file; exit 0 only when every probe holds |
| `lion agent --dir D --stats` | a read of the notes and the ledgers (C4) | nothing |
| `lion chat` | a console actor over a directory ([[ADR-0018-chat-in-a-box|ADR-0018]]) | `.lion/chats/<id>.jsonl` with a usage row per call; notes under `.lion/notes`; a box's patch, applied only under `--apply` |
| `lion areas [--serve PORT]` | the residents by area, or a page and its API ([[ADR-0017-the-desk|ADR-0017]]) | nothing without `--serve`; with it, a resident's `hold`, `wake-now`, `[budget]` keys, `landing/controls.log`, mail, a launchd restart, and checkpoints through the `lion context` functions |
| `lion context` `set`, `checkpoint`, `restore`, `status`, `show` | a Claude Code session's checkpoint and restore, no model call ([[ADR-0019-the-checkpoint|ADR-0019]]) | `.khive/context/pending.jsonl`, `<id>.json` and `latest.json` |

The gate is code review of any second console script beside `lion`.

### C2: Every form of `lion agent` but `--stats` holds the directory's lock before it acts _(enforced: mechanical)_ ^c2

- **Subject**: `agent_main` in `apps/cli/lion_cli/agent.py`; `Agent._claim`, `Agent._release` and
  `Agent.serve` in `hub/agent/agent.py`.
- **Violated when**: a form other than `--stats` calls khive, lands a file or wakes while another
  live process holds the lock; `serve` claims twice in one process; or `--stats` takes the lock.

`agent_main` builds the agent, claims, runs its one branch and releases in a `finally`. The claim is
an exclusive, non-blocking `flock` on `agent.pid` in the directory whose `.khive/config.toml` names
the actor; the pid inside is for people. The operating system drops the lock with the process, so a
stale file never holds.

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
to whatever started it; `scripts/serve_agent.sh` execs the command, so the job's pid is the agent's.
A restart from `lion areas` asks launchd (ADR-0017).

Only a wake and a chat call a model; `--check`, `--ticks`, `--arm`, `--stats`, `lion areas` and
`lion context` make no model call. The command estimates no tokens, and the row sums what the
envelopes say, never a price table ([[ADR-0009-backends|ADR-0009]] S7). The gate is code review.

## Decisions

### D1: `apps/cli/lion_cli/main.py`: one parser, four subcommands ^d1

Serves C1. `parser()` builds `agent`, `chat`, `areas` and `context`, each with its own flags;
`main()` runs `agent` and `chat` under `asyncio.run`, `areas` and `context` directly, and ends a
chat quietly on an interrupt. `context` takes its verbs as a second level of subcommands.

- **Landing evidence**: `apps/cli/tests/test_chat_command.py`: the chat flags read through
  `parser()`; `tests/test_areas.py`: the command printing the table from a given registry;
  `tests/test_checkpoint.py`: `lion context` set, checkpoint, show, restore and status through the
  command.

### D2: `agent_main`, `land` and `arm` in `apps/cli/lion_cli/agent.py` ^d2

Serves C1 and C2. `build()` makes the agent from `chores.toml`: the trusted senders, the bounded
khive client, the gate, the chores, and the desk when configured. `agent_main` claims before any
branch. `land()` writes a landing file. `arm()` reads the actor's own inbox and requires rows
addressed to it, tries five operations and a send to an outsider that the client must each refuse,
quotes each refusal, and fails when `ANTHROPIC_API_KEY` is set in the environment the CLI child
inherits.

- **Landing evidence**: `tests/test_agent.py`: `test_once_refuses_served_identity`,
  `test_the_identity_is_a_lock_the_process_keeps_not_a_pid_it_wrote`,
  `test_serve_keeps_the_lock_its_process_already_claimed`,
  `test_arm_prints_the_auth_env_the_claude_child_inherits_and_fails_on_an_api_key`.

### D3: `backend_for` maps a model name to a backend that keeps envelopes ^d3

Serves C3. The model is the flag's, else `chores.toml`'s, else `deepseek/deepseek-v4.1-flash`. A
`deepseek` name gets the router with a fixed provider order, fallbacks off and data collection
denied; any other name the router's own routing; both a 2,000-token reasoning budget and a stop on
the notification frame ([[ADR-0009-backends#^d1|ADR-0009/D1]]). `claude_code[/<model>]` is the
session CLI with the config's `[claude_code]` options in the agent directory. The subscription CLI
of [[ADR-0009-backends#^d3|ADR-0009/D3]] is not offered.

- **Landing evidence**:
  `tests/test_agent.py::test_backend_for_claude_code_is_the_cli_chat_shape_on_the_named_model`.

### D4: `_usage`, `_spend`, `_facts` and `_line` in `hub/agent/agent.py` ^d4

Serves C3. `_usage(before)` reads the envelopes on the backend's `calls` from index `before`.
`_usage` also counts the calls whose cost is unknown or incomplete. `_spend` rewrites the day's row
in one put on the agent's store. `_facts` adds the wake's `tokens` and `idle_polls` to the cursor.
`_line` renders the cursor as the serve log's line, with the context the last turn stood in and the
session restarts when present.

- **Landing evidence**: `tests/test_agent.py`: the cost cap from the backend's own figures, each
  burst of a continuous chair booked once, one serve line per wake, the last turn's context on the
  line,
  `test_a_call_of_unknown_or_incomplete_cost_adds_nothing_to_the_cost_and_the_readers_mark_the_sum`;
  `tests/test_chores.py`: the digest's tokens line,
  `test_the_digests_tokens_line_marks_a_days_cost_that_misses_calls_of_unknown_cost`.

### D5: The readers `stats`, `resident`, `wakes` and the chat's `spent` ^d5

Serves C4. `stats` loads `notes/agent.json` and the directory's ledgers. `resident` takes a
resident's row from its own `chores.toml`, notes and lock, drops a row of another day, and names
what it could not read; serving is the lock, never the pid in the file. `wakes` parses the serve log
with the pattern the page shares. `spent` sums each call's `total_cost_usd`, else its `usage.cost`,
and counts the calls with neither or with `cost_complete` false; the chat log keeps each envelope's
cost as it came, null included.

- **Landing evidence**: `tests/test_areas.py`: the resident's row read from its own files, serving
  as the lock, the serve log's wake lines,
  `test_a_days_cost_that_misses_calls_of_unknown_cost_is_marked_partial_in_every_view`;
  `tests/test_chat.py`: the cost line over every attempt,
  `test_the_cost_line_marks_a_sum_that_misses_calls_of_unknown_or_incomplete_cost`;
  `tests/test_agent.py`: `test_once_refuses_served_identity`, `--stats` under a held lock;
  `test_stats_prints_the_spend_row_the_last_wake_and_the_ledgers_as_this_text`, the text it prints.

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
  false adds its known subtotal, and both count in `unknown`. The day's cap compares the known sum.
  While `unknown` is not zero, `--stats`, `lion areas`, the digest, the wake line, the chat's
  closing line and the cap's hold print `(partial: N call(s) of unknown cost)` beside the figure, as
  [[ADR-0009-backends|ADR-0009]] S1 asks; the phone page does not. A failed session CLI call lands
  no envelope today, so the row misses it whole.
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
- **S9**: An agent directory is whatever `--dir` names, and only a checkpoint written there checks
  where it sits: inside a git work tree whose ignore rules miss `.khive/context/`, the write refuses
  ([[ADR-0013-the-agent#^c7|ADR-0013/C7]]). The lock, notes, ledgers and landing files are written
  wherever the directory is, so it belongs where no checkout tracks it. The implementation's own
  repository ignores `.khive/`, where the bench example keeps its directory.
