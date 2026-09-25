---
adr: ADR-0013
status: draft
liveness: operating (the wake, the gate, the caps, the hop count, the cursor, the lock and the continuous run with its checkpoint are pinned by tests; supervision and the lease are not in the process)
date: "2026-09-25"
area: agent
kind: new
depends_on:
  - "[[ADR-0001-the-actor|ADR-0001]]"
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0003-the-bounds|ADR-0003]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
  - "[[ADR-0010-delegation|ADR-0010]]"
tags:
  - adr
  - runtime
  - agent
---

# ADR-0013: The long-running agent

## Context

An actor runs only when something calls `Actor.run`; nothing in the runtime runs it when mail
arrives. The long-running agent is that something: one identity with a khive inbox, one process, and
a loop that turns mail into runs. This record fixes what an agent is, what a wake does, how a reply
leaves, what bounds the loop, and what crosses from one wake to the next.

In-process peers are one layer and this is the other ([[ADR-0010-delegation#^c4|ADR-0010/C4]]): mail
from outside the process becomes one INPUT, a reply leaves as a `send` command the profile's
privilege admits ([[ADR-0001-the-actor#^c4|ADR-0001/C4]]), and the notes carry state between runs
([[ADR-0007-the-record#^c5|ADR-0007/C5]]). The kernel owns the process: who the identity is, who
starts and restarts it, and when it counts as alive. Here the agent only keeps two processes from
acting as one identity. Source: `Agent` in `hub/agent/agent.py`, `Khive` in `hub/khive.py`, `build`
and `agent_main` in `apps/cli/lion_cli/agent.py`, `examples/echo_agent.py`,
`examples/bench_agent.py`.

## Definitions

- **agent**: an actor bound to one khive identity and driven by a loop outside the runtime that runs
  it when mail arrives; one per process.
- **wake**: one pass of the loop that found work: one batch, one INPUT, booked once in the spend and
  the cursor.
- **batch**: the mail a wake takes, marked read together, with the notices that joined it.
- **notice**: a mail-shaped item from outside the agent's own inbox, a watch's or a due task's, that
  joins a batch and is never marked read.
- **cursor**: the agent's note on the last wake: its number, time, profile, run, outcome, turns,
  mail ids, sends and usage.
- **hop count**: the messages the agent has sent on one thread; a send past the cap is refused.
- **posture**: the model's own line on what a wake did and what a later wake should look for, kept
  in the profile's notes.
- **continuous run**: one run that carries many wakes, the next batch queued into it each time the
  model's reply ends a burst of mail.
- **checkpoint**: a continuous run's record written to disk with its named state, which the next
  process's first run replays as history.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | Work reaches the agent as mail on its khive inbox, a watch's notice or a queue task; a timer or an outside event arrives as a message. | `wake` polls `Khive.inbox` and the watch; `wake` and `rerun` are the only callers of `_run_batch` | a second channel needs its own gate, cursor entry and cap |
| A2 | A wake that dies unanswered costs less than a wake that answers twice. | `wake` writes `taking`, then marks read, then runs; `_recover` tells the owner | mail is marked read after the run and replies are deduplicated by key instead |
| A3 | The notes and the cursor carry enough across the wakes of a per-wake agent. | the next INPUT opens with the cursor and the posture (`_input`) | the record has to cross wakes, as the continuous run's checkpoint does (C7) |
| A4 | The host kernel drops an advisory file lock when its holder ends, however it ends. | `fcntl.flock` in `_claim`; a stale pid file nobody holds does not block a start | a dead process holds its identity until someone removes the file |

## Claims

### C1: One identity per process, held by a kernel lock _(enforced: mechanical)_ ^c1

- **Subject**: every `Agent.serve`, and every command of `lion agent` but `--stats`.
- **Violated when**: two live processes act as one identity, a stale pid file blocks a start, or a
  khive call resolves to an actor other than the one the agent was built for.

The agent's directory is its identity. `Khive` refuses a directory without `.khive/config.toml` and
makes every call from it with the actor asserted (`--expect-actor`), so a wrong directory refuses
instead of answering as someone else.

`_claim` takes an exclusive, non-blocking `flock` on `agent.pid` there and holds it for the
process's life; a second claimant is refused, naming the pid inside. The kernel drops the lock with
the process, so a file nobody holds never blocks. The command line claims before its first act and
`serve` keeps that claim; `--stats` only reads.

### C2: A wake takes its mail as one batch, marked read before anything acts, and runs it once _(enforced: mechanical)_ ^c2

- **Subject**: every `Agent.wake`, and every burst `_wait` takes into a continuous run.
- **Violated when**: the backend is called before the mark-read, mail the gate refused reaches a
  run, one batch becomes two runs, or a poll that found nothing calls the model.

The loop long-polls the inbox for `poll_ms` (5 s) per call, beside the watch when one is set. Mail
found is named under `taking`, marked read, then passed through `admit`: refused mail is recorded on
the cursor as dropped and never runs. The policy maps the batch to the profile it runs under.

The batch is one INPUT: the run's input, or for each later burst of a continuous run one inbound
from "the inbox". Notices join it unmarked. A poll that found nothing counts as idle and calls no
model.

### C3: The cursor is written as running before the run, and a lost batch is told to the owner _(enforced: mechanical)_ ^c3

- **Subject**: the cursor, `taking` and `unreported` in the agent's own note store.
- **Violated when**: a wake that dies after its mark-read leaves no id behind, a lost batch is not
  told, or a batch leaves `unreported` before a notice naming it landed.

Before the run the cursor holds the wake's facts, outcome `running`, and the batch's ids; after, it
is overwritten with the outcome, turns, sends and usage. Recovery runs at start and before each wake
takes its mail: finding `taking` set or the cursor at `running`, it puts that batch under
`unreported` and tells the owner in one message. A wake whose run raises does the same for its own.

A batch leaves `unreported` only once that notice landed. `Agent.rerun(ids)` takes read mail back
through a wake by id, and the caps hold it as they hold a wake.

### C4: Every send a command handler makes goes through one gate that charges the hop count first _(enforced: mechanical)_ ^c4

- **Subject**: every call to `Agent.deliver`.
- **Violated when**: a send passes its thread's cap, two concurrent sends both take the last hop, a
  key is charged twice, or a refusal before sending keeps its charge.

`send` is a Spec whose handler requires `comm.send`: a profile without it reads mail and never
answers. That handler, the bench report and the chores, desk and mail handlers call `deliver`. Under
one lock it reads the thread's count, refuses at `hops_per_thread`, charges the count and the key,
then calls the transport.

A key already charged is a replay, neither capped nor charged. An `AgentError` before sending gives
the charge back; any other failure keeps it, since the message may have landed. A new thread is
charged under the id the transport returns. The sends that bypass the gate are named in S4.

### C5: The caps hold a wake, and the mail waits unread _(enforced: mechanical)_ ^c5

- **Subject**: every wake, rerun and continuous burst.
- **Violated when**: a wake runs with a cap reached or a hold file present, or held mail is marked
  read.

Before polling, `hold` checks a `hold` file in the directory, the last hour's wakes against
`wakes_per_hour` (60), and the day's cost against `cost_per_day` (1.0 USD). At any of them the wake
returns nothing and the inbox is not read, so the mail stays for a later poll.

The cost is summed from the backend's own envelopes (`total_cost_usd`); a backend that writes none
spends 0. A continuous run also ends at a burst past `max_rounds` turns, or at the day's cap with
the burst's own calls counted.

### C6: The agent writes the posture, and a per-wake run starts new _(enforced: mechanical)_ ^c6

- **Subject**: the posture note, and the first INPUT of every wake.
- **Violated when**: the runtime writes the posture, the INPUT omits the last cursor or posture, or
  a per-wake run starts from an earlier wake's record with no checkpoint on disk.

A per-wake run declares `Posture` as its output and ends with `OUT{posture: ...}`
([[ADR-0003-the-bounds#^c4|ADR-0003/C4]]). After a `Success` the agent's own code writes its `done`
and `awaiting` as one line under `posture` in the profile's notes; the runtime's loop writes none.

A continuous run declares no output: its plain reply at each quiet point is the posture. The next
wake's INPUT opens with the last cursor and the posture, and the record stays with the run that made
it.

### C7: A continuous run carries many wakes, and its checkpoint carries the record across a restart _(enforced: mechanical)_ ^c7

- **Subject**: every run `serve` starts with `continuous` set.
- **Violated when**: mail for the same profile starts a second run, a burst is booked twice, or the
  next process's first run starts from another view than the one checkpointed.

`build` sets `continuous` for a chair. When the model's reply ends a burst, `_wait` books it as one
wake (spend, cursor, posture, the log line), polls under the same caps, and queues the next batch
into the same run, whose view folds. A batch that maps to another profile ends the run and runs
fresh.

At a fold, and when a burst's turn bound or the day's cap ends the run, it writes a checkpoint in
the agent's directory, only where git ignores it; the next process's first run replays it as
history.

## Decisions

### D1: `Agent` over `Khive`, one khive call per operation from the agent's directory ^d1

Serves C1, C2 and C4. `hub/khive.py` runs one `exec --strict --expect-actor` of the khive CLI per
call, from the agent's directory; an inbox wait longer than 20 s is sliced, and an inbox read whose
answer was lost is read once more, a write never. `hub/agent/agent.py` holds the wake, the gate, the
caps, the cursor and `deliver`; `lion agent` builds one from the directory's `chores.toml`.

- **Landing evidence**: `tests/test_agent.py`:
  `test_a_wake_marks_mail_read_before_the_model_sees_it_answers_on_the_thread_and_leaves_a_cursor`,
  `test_mail_from_an_unknown_sender_runs_the_reader_profile_which_cannot_answer`,
  `test_no_mail_means_no_run_and_the_next_wake_reads_the_cursor_first`; `tests/test_chores.py`:
  `test_mail_from_outside_the_trusted_set_is_dropped_at_the_gate_and_never_runs`.

### D2: The pid file under an exclusive `flock`, claimed before the first act ^d2

Serves C1. `_claim` locks `agent.pid` without blocking and writes the pid for people to read;
`_release` removes the file only while it holds this process's pid, then closes the lock.
`agent_main` claims before any branch runs, and `serve` keeps a claim its process already holds.

- **Landing evidence**: `tests/test_agent.py`:
  `test_the_identity_is_a_lock_the_process_keeps_not_a_pid_it_wrote`,
  `test_serve_refuses_a_held_identity_reports_a_died_wake_first_and_logs_one_line_per_wake`,
  `test_serve_keeps_the_lock_its_process_already_claimed`, `test_once_refuses_served_identity`.

### D3: The agent's own note store beside the profiles' notes ^d3

Serves C3, C5 and C6. `notes/agent.json` in the agent's directory holds `cursor`, `taking`,
`unreported`, `threads`, `hops_charged`, `spend` and `deferred`. The profiles' notes hold `posture`;
`lion agent` keeps them in the same folder, one file per profile. The wake number continues from the
stored cursor, so a restart keeps counting.

- **Landing evidence**: `tests/test_agent.py`:
  `test_the_cursor_names_the_batch_before_the_run_so_a_wake_that_dies_still_shows_what_it_took`,
  `test_a_wake_killed_after_its_mark_read_is_reported_to_the_owner_and_rerun_takes_it_back`,
  `test_a_recovery_notice_the_transport_dropped_is_told_at_the_next_start`, the two cap tests.

### D4: The hop count in `deliver`, charged at admission under a lock ^d4

Serves C4. `threads` counts per thread and `hops_charged` maps each idempotency key to its thread,
both written before the transport under an `asyncio.Lock`. The count is written before the key, so a
death between them over-counts, never under. `hops_per_thread` defaults to 12, for the thread's
whole life.

- **Landing evidence**: `tests/test_agent.py`:
  `test_the_hop_limit_refuses_a_send_past_the_cap_on_one_thread`,
  `test_two_sends_in_one_turn_share_the_thread_cap`; `tests/test_desk_residuals.py`: the keyed
  replay, lost receipts, restart, cap refusal and transport refusal tests;
  `tests/test_bench_agent.py`.

### D5: The continuous run on the runtime's `wait` and `history`, the checkpoint in `lionagi/checkpoint.py` ^d5

Serves C7. The request carries `wait=_wait`, `reply_on_commands`, `fold_inputs` and no declared
output, with a stop that ends a burst past `max_rounds` or the day's cap. `_checkpoint` writes the
record beside a checkpoint through `take` and `save`; `_restore` hands `to_history(load(...))` to
the process's first run.

- **Landing evidence**: `tests/test_agent.py`:
  `test_a_continuous_chair_keeps_one_run_across_bursts_and_books_each_quiet_point_as_a_wake`,
  `test_mail_for_another_profile_ends_the_conversation_and_runs_fresh`; `tests/test_checkpoint.py`:
  the fold, stop and failed-restore tests.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| An HTTP endpoint as the agent's surface | a second protocol with its own auth and no history; comm already has addresses, threads and audit |
| One run per message | the cost scales with the mail; a batch is one INPUT whatever arrived |
| Marking mail read after the run | a wake that dies mid-run answers again on restart; marked first, it loses the answer and the owner is told (C3) |
| A pid file read, then written | it let two starters through; a kernel lock dies with its holder (D2) |
| Counting a hop after the transport | a reply that lost its receipt and was settled by replay went uncounted: three went out on a thread capped at two (D4) |
| One process hosting many agents | one death takes every identity, and the lock is per identity |
| The runtime writing the posture | the runtime's loop knows no agent; the posture is the agent's note to its next wake (C6) |
| A per-wake agent continuing the last wake's record | the cost grows with history; the cursor and posture carry the next wake, and a chair that needs the record runs continuous (C7) |

## Consequences

- **S1**: A wake that dies after its mark-read loses its answer: the mail stays read and unanswered,
  the unread view never offers it again, and only a rerun by id takes it back.
- **S2**: The hop limit is per thread for good; a conversation that reaches it continues on a new
  thread or not at all.
- **S3**: A backend that writes no cost spends 0 against the day's cap, so an agent on the
  subscription CLI is bounded by wakes per hour alone; its spend is unknown, never zero
  ([[ADR-0009-backends#^c5|ADR-0009/C5]]).
- **S4**: Two sends bypass `Agent.deliver`: the owner's notice of a lost batch goes to the transport
  directly, and the bench example's job child mails the agent itself through the khive CLI. Neither
  is capped or counted. The bench report of the row goes through `deliver`. A handler calling the
  transport itself would also stand outside the hop limit; only review stops it.
- **S5**: Which mail wakes the model at once is a rule (`[wake]`: `now`, `batch` or `drop`, by
  sender, subject and the day's spent fraction). Batched mail is read and kept under `deferred`
  until its window opens; `now` mail takes it along. A task queue read from khive adds due tasks as
  notices.
- **S6**: A `hold` file and a `wake-now` file in the directory are a person's levers, read each
  poll; `[budget]` and `[wake]` follow the config file while serving, and a file that does not parse
  keeps the values in force.
- **S7**: The watch is a second box polled beside the agent's own for the same wait; its notices are
  named apart on the cursor and never rerun, and a failed watch costs the agent's own mail nothing.
  The front desk sets it ([[ADR-0017-the-desk|ADR-0017]], the desk).
- **S8**: The mail watch, a chore that confirms a script's class on tracked mail each tick
  (`hub/agent/mail.py`), sends through `Agent.deliver` and is not decided here.
- **S9**: The first run of any process replays the directory's latest checkpoint when it is a
  continuous run's and its record still folds to the view taken; for any other the failure is kept
  among the agent's errors and the run starts fresh.
- **S10**: The boundary: the identity and its grants, the lease that says a process is alive by its
  turn activity, the one supervisor that starts and restarts it, killing it, and the durable record
  are the kernel's. The agent holds a lock and nothing more; `scripts/serve_agent.sh` is an entry
  point a host supervisor calls and decides none of these.
- **S11**: A profile named `agent` would share the agent's own note file, since a profile's notes
  are filed by its name; nothing refuses the name.
