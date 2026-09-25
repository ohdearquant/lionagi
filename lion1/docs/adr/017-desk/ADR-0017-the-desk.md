---
adr: ADR-0017
status: draft
liveness: operating (every claim is pinned by tests with scripted backends and a stubbed store; no bench exercises the desk)
date: "2026-09-25"
area: desk
kind: new
depends_on:
  - "[[ADR-0001-the-actor|ADR-0001]]"
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0003-the-bounds|ADR-0003]]"
  - "[[ADR-0010-delegation|ADR-0010]]"
tags:
  - adr
  - runtime
  - desk
---

# ADR-0017: The desk: front desk, record answers, areas and chairs

## Context

An agent that works for an owner can read the owner's inbox first. Much of that mail is a question
of fact one instrument settles, a notice that asks nothing, or work to hand on, and each read costs
the owner a model turn. The desk answers what an instrument vouches for, forwards or files what it
can name, escalates what cannot wait, and leaves the rest with the reason on a row. A cheap model
only routes; code composes every reply.

The desk is a set of handlers on the long-running agent's actor ([[ADR-0013-the-agent|ADR-0013]],
the long-running agent; [[ADR-0001-the-actor|ADR-0001]]), gated on `OUT{}`
([[ADR-0003-the-bounds#^c4|ADR-0003/C4]]). Its owner is the chair of an area, and the owner's mail
is the layer beside in-process peers ([[ADR-0010-delegation#^c4|ADR-0010/C4]]). Settling a send
whose answer was lost is the kernel's contract, keyed replay; the desk is its in-process form.
Source: `hub/agent/desk.py`, `hub/agent/lookups.py`, `hub/agent/verdicts.py`; `Agent.wake`,
`Agent.deliver` and `admit` in `hub/agent/agent.py`; `Chores.admit` and `Bounded` in
`hub/agent/chores.py`; `build` in `apps/cli/lion_cli/agent.py`; `hub/areas.py`,
`apps/cli/lion_cli/areas.py`, `hub/app.py`.

## Definitions

- **desk**: the handlers `Desk` adds to a long-running agent so that it reads its owner's inbox
  first; the code also calls it the front desk.
- **reader**: the second khive client the desk reads the owner's box through, bound to the owner's
  directory and identity and allowed `comm.inbox` and `comm.thread` only.
- **sweep**: the command that reads the owner's box since the cursor and lists, by id, the pending
  messages the model may act on.
- **desk ledger**: `desk.jsonl` in the agent's directory: append-only, one row per message the desk
  disposed of, with its class and its reason.
- **pending message**: a message of the owner's box the desk has read and not disposed of, kept
  under `desk_pending` in the agent's own notes until its row.
- **delivery unresolved**: a pending message carrying a recorded send whose outcome is unknown: the
  request may have landed.
- **keyed replay**: sending a recorded request again, byte for byte, under its idempotency key.
- **code escalation**: a message forwarded to the owner word for word by code, before any model
  reads it.
- **area**: a registry table naming one chair and the directories of its residents.
- **chair**: the actor accountable for an area, as the registry names it; a desk's guidance calls
  its owner the chair of its area.
- **resident**: the long-running agent serving from one directory an area lists, as `lion areas`
  reads it.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | A key the store holds answers the same request again with the message that landed, marked `replayed`, and refuses another request under it. | `Khive.send`; the tests stand in for the store (`Server` in `tests/test_desk_residuals.py`), so this is the store's contract (assumption) | a replay sends a second message and C7 settles by a duplicate |
| A2 | The box's `since` is an inclusive bound on a row's arrival stamp, to the microsecond. | `_discover` and `after` in `hub/agent/desk.py`; `test_after_is_one_microsecond_past_the_stamp_as_since_reads_it` | the cursor skips or repeats a row at its edge (C2) |
| A3 | A cheap model maps a listed message to one command and copies the ask's words, and nothing more. | `hub/guidance/desk.md`; no bench measures the routing (assumption) | a sender can get another chore's report, still composed by code; the brief names every answer (C9) |
| A4 | A serving agent holds an exclusive lock on its `agent.pid` for its life. | `Agent._claim`; `test_a_residents_row_reads_its_own_files_and_serving_is_the_lock_not_the_pid_file` | a row reads a dead resident as serving, or a live one as stopped (C10) |

## Claims

### C1: The desk reads the owner's box through a second, read-only client in the agent's process _(enforced: mechanical)_ ^c1

- **Subject**: the reader and every op it carries.
- **Violated when**: the reader carries a verb other than `comm.inbox` or `comm.thread`, marks,
  sends or holds a recipient, or reads as any identity but the owner's.

`lion agent` builds the reader from the owner's directory, named under `[desk]`, and the owner's
actor: a khive client whose every call asserts that the directory resolves to that actor. `Reader`
wraps it in the agent boundary with those two verbs and no recipients and exposes only `exec`, so no
helper of that client is reachable; an op whose calls the boundary cannot count is refused.

The reader sits beside the agent's own bounded client in one process. The agent signs every send as
itself, through `Agent.deliver`; the reader reads as the owner and holds nothing to act with.

### C2: The agent's wake marks its own mail read; the desk reads the owner's box by cursor and never marks it _(enforced: mechanical)_ ^c2

- **Subject**: the agent's box and the owner's box, on every wake.
- **Violated when**: the desk marks a row of the owner's box, a watch notice is marked, or discovery
  never reads an unread row that arrived after the desk went live.

A wake marks its batch read before anything acts on it. The desk sets the agent's `watch`: discovery
long-polls the owner's unread box beside the agent's own, for the same wait, and pending messages
become one `tick: sweep` notice, which is not mail and is never marked.

Discovery reads every page of unread mail since the cursor, an instant one microsecond past the
newest stamp read. The first discovery sets it to the time the desk went live. It moves only after a
scan that fit one page, because offsets index an unread set the owner changes between pages, and
never back.

### C3: `Agent.admit` drops untrusted senders before any model call _(enforced: mechanical)_ ^c3

- **Subject**: the agent's own mail, on every wake, rerun and continuous wait.
- **Violated when**: mail from outside the trusted set reaches a run, or a dropped message is not
  recorded on the cursor.

`lion agent` sets the gate to `Chores.admit`: the sender is the owner, the steward, the agent itself
or in the config's `trusted` list. A wake marks its batch read, then drops what the gate refuses,
recording each id and sender under `dropped` on the cursor; a wake left with no mail and no notice
starts no run and calls no model. `rerun` and the continuous wait apply the same gate.

A `[chair]` table with `admit = "all"` sets no gate: every sender's mail runs, and the bounded
client still refuses a send to a recipient it was not built with. The gate covers the agent's own
box. The owner's box carries mail from anyone; C4 and the sender tests of C5 are its gate.

### C4: What cannot wait is escalated, and review notices are triaged, by code before any model _(enforced: mechanical)_ ^c4

- **Subject**: every new row discovery reads from the owner's box.
- **Violated when**: such a row is listed to the model, its body is paraphrased, or an escalation
  passes the day's cap.

A code escalation takes mail from a `never` prefix (default `email:`), a sender in `escalate_from`,
a subject with `[NOW]`, or another desk of the owner (the owner's id and a colon) whose subject
begins `ESCALATE`. It goes to the owner as `[NOW] <sender>: <subject>` with the body its thread
holds, else the inbox snippet and why. Past `escalations_per_day` (40), or refused before its send,
it is left for the brief.

With `[desk.verdicts]` set, `Verdicts` triages a review notice from `verdict_from`: stale, closed, a
stub or another owner's is left with the reason, a fresh one on the owner's pull request files one
task, an undecided one is escalated. Ownership reads the branch prefix, the commit author's name,
then a repository map, never the login.

### C5: Every command the model may call, with its gate _(enforced: mechanical)_ ^c5

- **Subject**: the desk's wake profile.
- **Violated when**: the profile holds a command outside the table, a command runs past its gate, or
  model text reaches a sender other than as an instrument's arguments or a clarify's two readings.

| command | gate, in code, before anything is sent |
| ------- | -------------------------------------- |
| `sweep()` | none of its own; lists at most a page (20) of pending messages, oldest first, and apart the deliveries unresolved the model may leave; leaves a clarify past `clarify_hours` (24) |
| `answer(msg, chore, args)` | an id this run's sweep listed, held by no other handler, with no recorded send or task; the chore's `senders` (`record`: `Record.senders` only); `answers_per_hour` (10), shared with `route`; a void outcome is left with the instrument's reason |
| `leave(msg, why)` | a listed id; `why` one of six tests, else kept as other |
| `clarify(msg, a, b)` | two different readings, each the message's own words or a chore or test name; `CLARIFY_SENDERS` only; once per thread; never a message that is itself a clarify |
| `route(msg, desk)` | a desk `[desk.routes]` names, checked at build to be another desk of the owner; never mail of the owner or its desks; the hourly cap |
| `task(msg, title)` | a title of 1 to 120 characters; the `CLARIFY_SENDERS` prefixes only; `tasks_per_day` (20), unresolved ones counted |
| `check`, `digest`, `aged` | the chores' own ([[ADR-0015-chores|ADR-0015]], chores); an asker `check` names must be trusted, with mail on that thread this wake admitted |

Each requires `comm.send` ([[ADR-0001-the-actor#^c4|ADR-0001/C4]]); the agent's own `send` is not in
the profile, and every send passes the hop count of `Agent.deliver`. Code composes every text: an
answer is the instrument's report, which echoes the arguments the model gave it; a clarify is a
template around two readings taken from the message; a route is the message word for word. Beyond
that echo, the model's own words reach the owner only as a task title or a leave reason.

### C6: A message stays pending until its row, so a failed wake lists it again _(enforced: mechanical)_ ^c6

- **Subject**: `desk_pending` and the desk ledger.
- **Violated when**: a message leaves `desk_pending` without a row, a wake ends `Success` with a
  listed message that has neither a row nor a recorded send, or a pending message waits for a new
  arrival to wake the desk.

Discovery writes a new row into `desk_pending` before it moves the cursor; only a desk ledger row
takes it out. A handler holds a listed message while it works, and one that raises leaves it listed
and pending.

The desk's `accept` refuses `OUT{}` while a message this run's sweep listed has neither a row nor a
recorded send, naming the ids; three refusals in a row end the run `Refused`
([[ADR-0002-the-run#^c4|ADR-0002/C4]]). While a message the model can act on is pending, the watch
polls without waiting, so the next wake lists it again with no new arrival.

### C7: An unknown send is settled by its keyed replay or a leave, never by chronology _(enforced: mechanical)_ ^c7

- **Subject**: every send the desk causes and every task it files.
- **Violated when**: a send whose outcome is unknown goes out again unkeyed or changed, is settled
  by a clock or the thread's order, is charged a second hop, or is answered again.

Before a send starts, the desk writes the exact request, keyed `desk:<agent>:<message>:<class>`,
onto the pending message. A refusal before the send clears it; any other failure keeps it, and the
message is a delivery unresolved. Every discovery makes its keyed replay: `replayed` means the first
attempt landed, otherwise it lands now, and either writes the row with the reply's id. A failure
settles nothing.

The model may only `leave` a delivery unresolved, and the row keeps the request; an escalation's is
replayed only. `Agent.deliver` charges a key's hop once. A task is keyed the same way and replayed
by the next `task` on its message, or by discovery when code filed it.

### C8: The `record` chore answers from the record in code's words, and a miss is no answer _(enforced: mechanical)_ ^c8

- **Subject**: every `answer(msg, chore="record")`.
- **Violated when**: a reply goes out with no finding or after a failed control, lacks the lead or
  the tail, or reaches a sender outside `Record.senders`.

`Record` runs three lookups on `args.query`, the ask's words. `Issues` searches the configured
repositories' issues and pull requests once `gh repo view` returns each one's own name. `Adrs`
scores ADR titles, headings and Alternatives rows. `Decisions` keeps khive notes the text arm
matched, beside a known positive in the same batch. One failed control fails the whole; no hit is an
empty population, and the desk leaves it.

The reply's first line says the desk answered from its `record` instrument and that the owner has
not read the message. Then come the lead, "the record holds these, one may settle it:", and the
report, each finding naming its lookup. The tail asks whether what settles it is complete, or to say
so on the thread to reopen it.

### C9: The owner hears of what the desk did in the brief, and a window moves only once delivered _(enforced: mechanical)_ ^c9

- **Subject**: the desk's part of the chores digest.
- **Violated when**: a row other than a clarify's resolution falls in no window, a window moves
  while its digest was not delivered, or an escalation, a delivery unresolved, an open clarify or a
  refusal is not named as anomalous.

The brief covers the window from the last brief delivered to now. Its first line counts the sweeps
run and the messages read, answered, routed, tasked, escalated, left and asked, and names what is
anomalous. Then come one line per escalated, routed, tasked, answered and clarified row, the
answer's first finding with it, the left rows grouped by reason, at most 60 lines before a count of
the rest, and the clarifies still open. A digest that fails, or whose outcome is unknown, moves
nothing, so its window comes again.

### C10: An area is one chair and its residents; a resident's row is what its own files say _(enforced: mechanical)_ ^c10

- **Subject**: the registry `areas.toml` and every resident row.
- **Violated when**: an area loads without a chair, a row reads serving from the pid file's
  presence, a file that could not be read goes unnamed, or another day's spend is shown as today's.

`[area.<name>]` carries `chair` and `desks`, the residents' directories; a table without a chair is
refused by name, and a missing file is no areas. A resident's actor, model and role (desk, mail or
chores, by the table its `chores.toml` carries) come from that file.

It is serving when a non-blocking shared lock on its `agent.pid` is refused, and in a wake while its
cursor's outcome is `running`. Wakes, the last wake and today's runs, mail and cost come from its
notes; a spend row of another day counts as none. What could not be read is named in the row's
`problem`, so a zero beside it is not a reading.

### C11: The areas server reads and changes registered residents only, for loopback and the tailnet _(enforced: mechanical)_ ^c11

- **Subject**: every route of `lion areas --serve`.
- **Violated when**: a request from outside loopback and `100.64.0.0/10`, or with a foreign Host or
  Origin, is served; a directory the registry does not name is read, written or mailed; a recipient
  comes from the form; a replayed control acts twice.

`/` is a page with a meta refresh and `/api/areas` the rows as JSON. `/r` and `/api/resident` give
one resident with its wakes, log tail and conversation; `/events` streams rows and logs; `/app` is a
phone shell over those; the checkpoint routes sit under `/api/context/`.

`POST /send` mails the resident's own actor, read from its `chores.toml`, as the `--as` identity,
the page's token as the idempotency key. `POST /control` holds, resumes, wakes, restarts or rewrites
`[budget]`, and answers a replayed token, among the last 100, with the first result. Every request
that reads or changes a resident re-reads the registry and the files; `/events` re-reads them each
second.

## Decisions

### D1: `Desk` in `hub/agent/desk.py`: six handlers, the gate on `OUT{}`, the watch ^d1

Serves C2, C5 and C6. `lion agent` builds `Desk` when `chores.toml` has a `[desk]` table and `front`
is not false. It registers `sweep`, `answer`, `leave`, `clarify`, `route` and `task` on the agent's
actor, each requiring `comm.send`, adds them and the desk guidance to the chores profile, and sets
the agent's `accept` and `watch`. No tick is scheduled: the watch wakes the desk. Caps and routes
come from `[desk]`.

- **Landing evidence**: `tests/test_desk.py` (89 tests), among them
  `test_build_wires_the_desk_from_chores_toml_with_a_reader_that_only_reads` (the nine-command
  profile, the watch) and `test_failed_selected_message_blocks_success_and_remains_pending`.

### D2: The reader is built from the owner's directory, beside the agent's own client ^d2

Serves C1. `build` in `apps/cli/lion_cli/agent.py` makes one `Reader` over a khive client of the
owner's directory and actor, and hands it to the desk and to the instruments that read the owner's
box. The agent's own client is `Bounded` over the agent's directory, its recipients the trusted set
and the routed desks. Two clients, two identities, one process. The reader stands in until the store
lets an actor read a namespace it can see; then the agent's own client reads the owner's box, and
nothing else here changes.

- **Landing evidence**: `test_reader_helpers_cannot_write` (mark, grant, send and six JSON-form ops
  refused before the transport) and `test_agent_marks_only_own_mail`, both in `tests/test_desk.py`.

### D3: Keyed replay in `_send`, `_deliver` and `_discover`, charged once in `Agent.deliver` ^d3

Serves C6 and C7. `_send` records the request and its key in `desk_pending` before `_deliver` runs;
`_deliver` sends it under the key, inside a one-reply grant for an answer or a clarify; `_discover`
replays every recorded send nobody left. `Agent.deliver` keeps `hops_charged`, key to thread, so a
replay is neither capped nor charged again. `_file` keys its task write the same way.

- **Landing evidence**: `tests/test_desk_residuals.py` (25 tests), among them
  `test_each_unresolved_send_is_settled_by_its_own_key_and_by_no_other_message`,
  `test_an_older_own_message_on_the_thread_never_settles_an_unresolved_send` and
  `test_a_key_is_charged_once_through_a_repeated_settlement_and_a_restart`.

### D4: The gate is `Chores.admit`, set by `lion agent` ^d4

Serves C3. `Agent.admit` is a field, `None` admitting all; `build` sets it to the trusted-set test
unless `[chair] admit = "all"`. The trusted set is fixed at build: the owner, the steward, the
agent's actor and `trusted`.

- **Landing evidence**:
  `test_mail_from_outside_the_trusted_set_is_dropped_at_the_gate_and_never_runs` in
  `tests/test_chores.py`;
  `test_a_chair_admitting_all_runs_mail_from_any_sender_and_still_sends_only_to_the_trusted` in
  `tests/test_agent.py`.

### D5: Escalation and triage in `_discover`; `Verdicts` in `hub/agent/verdicts.py` ^d5

Serves C4. `_discover` sorts every new row into escalate now, review notice or pending before it
writes `desk_pending`. `_escalate` sends to the owner under the day's cap and a key.
`Verdicts.triage` reads the verdict artifact from its directory, resolves the repository and reads
the pull request once; `_file` writes the task.

- **Landing evidence**: `test_the_forty_first_escalation_of_a_day_is_left_cap` and
  `test_a_lost_escalation_receipt_is_settled_by_its_keyed_replay_and_never_listed` in
  `tests/test_desk.py`; `tests/test_verdicts.py` (46 tests).

### D6: `hub/agent/lookups.py` and `[chores.record]` ^d6

Serves C8. `Issues`, `Adrs`, `Decisions` and `Record` run on ask only. `[chores.record]` builds
them: `repos` for `Issues`, `adr_dirs` for `Adrs`, `decisions` (default on) for `Decisions` with
`positive` (default the owner) and `kinds` (default decision and insight). A record with no lookup
refuses to build. `answer` puts an instrument's `lead` under the first line and its `tail` last.

- **Landing evidence**: `tests/test_lookups.py` (8 tests), among them
  `test_record_unions_its_parts_and_one_failed_part_fails_the_control` and
  `test_the_desk_answers_a_record_ask_with_the_tail_and_leaves_one_the_record_does_not_carry`.

### D7: The brief is the chores digest on the desk's cadence ^d7

Serves C9. `Desk.digest_lines` is the digest's extra part and `_briefed` moves the window only once
the digest carrying it was delivered; `[desk] brief` (default every 12 hours) sets the digest's
tick.

- **Landing evidence**:
  `test_the_second_brief_starts_where_the_first_ended_and_each_row_is_briefed_once`,
  `test_a_brief_whose_delivery_failed_is_briefed_again_from_the_same_start` and
  `test_two_digests_of_one_turn_brief_each_row_and_each_sweep_once` in `tests/test_desk.py`.

### D8: `hub/areas.py` and `lion areas` ^d8

Serves C10 and C11. `load_areas`, `resident`, `status`, `table` and `page` read files and one lock;
`controls_info` adds a process check, which a restart reads before it acts. The server in
`apps/cli/lion_cli/areas.py` is a stdlib `ThreadingHTTPServer` on `127.0.0.1` by default, `--allow`
adding a name it answers to; the phone shell is `hub/app.py`. The registry defaults to
`.lionagi/areas.toml` under the home directory.

- **Landing evidence**: `tests/test_areas.py` (22 tests), among them
  `test_a_residents_row_reads_its_own_files_and_serving_is_the_lock_not_the_pid_file` and
  `test_the_guards_refuse_a_peer_off_the_tailnet_a_foreign_host_or_origin_and_admit_a_native_post`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| The model writes the reply | a cheap model cannot judge what a reply commits the owner to; code composes it from the outcome |
| The desk marks what it handled as read | the owner loses sight of its own mail; the desk ledger and the brief say what the desk did |
| The agent's own client reads the owner's box | the store does not yet let an actor read a namespace it can see; when it does, this replaces the reader (D2) |
| Settling an unknown send by the thread's order or a clock | an older message of the desk's on the thread retires a send that never landed; only the key names the message |
| A timed sweep | an idle box would still wake the model; the watch wakes nothing while nothing is pending |
| Three lookups, the model picks one | `answer` takes a message once, so a second lookup after a miss is refused; one chore reads all three |
| A "nothing found" reply | a word search under-reports; a miss goes to the owner, whose copy stays unread |
| Serving read from the pid file's presence | a file left by a crash reads as serving; the lock is what a serving agent holds |

## Consequences

- **S1**: A sender gets an answer from the desk's actor, saying the owner has not read the message,
  before the owner reads it. A sender who wants the owner's judgment says so, and the guidance
  leaves such mail.
- **S2**: The desk handles mail that arrives after it went live; the backlog is the owner's. A row
  the owner marks read before discovery reads it is never pending, since discovery lists unread
  rows.
- **S3**: A clarify asks the sender to answer the owner on the thread. A reply mailed to the desk's
  own actor meets C3's gate and is dropped unless its sender is trusted. The next message handled on
  the thread resolves the clarify; otherwise `clarify_hours` leaves it as unanswered.
- **S4**: Nothing checks a desk's owner against its area's chair, and the registry's `desks` lists
  every resident's directory whatever its role; only a directory whose `chores.toml` has `[desk]`
  runs a desk.
- **S5**: The boundary: that a key answers with the message that landed and refuses another request,
  which identity a directory resolves to, who may read whose box, and delivery across processes are
  the kernel's contract, mail settlement by keyed replay. The desk is its in-process form: it
  records a request before it runs and replays it unchanged, and decides none of these.
- **S6**: `Record` costs three `gh` calls per repository per ask, one resolution and two searches;
  the hourly cap bounds it.
- **S7**: No bench exercises the desk. The tests drive it with scripted backends and a stubbed
  store: 267 tests across `tests/test_desk.py`, `tests/test_desk_residuals.py`,
  `tests/test_areas.py`, `tests/test_lookups.py`, `tests/test_verdicts.py`, `tests/test_chores.py`
  and `tests/test_agent.py` pass on the code of 2026-09-24.
