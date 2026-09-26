---
adr: ADR-0020
status: draft
liveness: operating (the calls, the settle and escalation paths, the threads, the mark retry, the failure not retold, the backlog and the code-only pass are pinned by tests with a stand-in tracker and a fake store; no test builds the watch from `[mail]` or drives a real tracker)
date: "2026-09-25"
area: mail
kind: new
depends_on:
  - "[[ADR-0001-the-actor|ADR-0001]]"
  - "[[ADR-0013-the-agent|ADR-0013]]"
  - "[[ADR-0014-the-instrument|ADR-0014]]"
  - "[[ADR-0015-chores|ADR-0015]]"
tags:
  - adr
  - agent
  - mail
---

# ADR-0020: The mail watch

## Context

An external mail tracker reads mail, gives each message a class, and lists the rows nobody has
handled. Some rows a class settles with no person; the rest need one. The mail watch puts a
long-running agent ([[ADR-0013-the-agent|ADR-0013]]) beside that list. On a tick the model sees the
unhandled rows and is asked for one call per row; code settles a row or sends it to the owner from
the tracker's class and the row's facts.

This record fixes what the watch lists, what code settles, what reaches the owner, what the agent
keeps, and what it never does. The watch rides the chores profile and ticks
([[ADR-0015-chores#^c3|ADR-0015/C3]]) but is not a chore: `check` never runs it. Its handlers
require `comm.send` ([[ADR-0001-the-actor#^c4|ADR-0001/C4]]). The desk
([[ADR-0017-the-desk|ADR-0017]]) reads the owner's box; the watch reads only the tracker's list.
Source: `hub/agent/mail.py`, `hub/guidance/mail.md`; `build` and `backend_for` in
`apps/cli/lion_cli/agent.py`; `Chores` and `Bounded` in `hub/agent/chores.py`; `Agent.deliver` in
`hub/agent/agent.py`.

## Definitions

- **mail watch**: the handlers `triage`, `confirm` and `edge` that `hub/agent/mail.py` adds to a
  long-running agent, with their tick, their guidance and an instrument face.
- **tracker**: an external mail tracker with a command-line tool that lists the unhandled rows with
  a class and settles one row; it runs outside this process.
- **listed row**: a row the latest triage showed the model, kept under `mail_pending` in the agent's
  notes until one call takes it.
- **settleable class**: a class in `SETTLE`, which code may settle with no person; the classes in
  `ESCALATE` need a person.
- **mail ledger**: `mail.jsonl` in the agent directory: append-only, one row per action on a
  message.
- **thread key**: the tracked row a message matched, else its organisation, else its sender's
  address; each key keeps one thread to the owner under `mail_threads`.
- **backlog**: the unhandled rows received before `since`, the configured start, which the tracker
  hands over in its control line when asked.
- **code-only pass**: the backend `Auto`, chosen by the model name `none`: it confirms every listed
  row and calls no model.
- **excerpt**: the text of a message's body that the tracker lists with its row.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | A row's `message_id` names one message for good, across ticks and processes. | the mail ledger and the listing key on it; nothing checks it (assumption) | a message already acted on is listed and acted on again |
| A2 | Given `since`, the tracker lists no row received before it and hands the older unhandled rows over in its control line only. | `_triage_cmd` passes the flag; the filter is the tracker's (assumption) | a backlog row reaches the model and may be settled (C7) |
| A3 | The tracker's settle command re-reads the row at the write and refuses, with `refused` in its output, a settle the row no longer supports. | `confirm` escalates on that word; the tests stand in for the tracker (assumption) | a stale class is written, or a refusal reads as a failed mark and the row comes back each tick |
| A4 | The tracker's class and row facts are right for the plain cases, and the model's confirm is the check on them. | `_label` reads only the row (assumption) | code settles from a wrong class, and under the code-only pass nothing contests it |
| A5 | Whatever the model may not see is removed from the excerpt by the tracker. | `_show` and `_text` cut the excerpt by length; nothing in `hub/agent/mail.py` redacts (assumption) | the model and the owner's message carry what the tracker left in |

## Claims

### C1: The model makes at most one call per listed row and can withhold a settle, never cause one _(enforced: mechanical)_ ^c1

- **Subject**: every `confirm` and `edge` call.
- **Violated when**: a call acts on a row the triage did not list or one already taken, an edge
  names a class outside `EDGE`, a settle follows an edge, or a row code closes quietly is escalated
  on the model's word.

On `tick: mail` the model calls `triage()`, which lists the rows the tracker returned that are not
yet acted on, with the tracker's class, facts and excerpt. For each listed row the guidance asks for
one call: `confirm(msg)` says the class fits the text; `edge(msg, cls, reason)` says it does not and
names a class of `EDGE`, a settleable class, one that needs a person, or noise.

An edge never settles: noise closes the row with the model's reason, and any other class reaches the
owner marked as the model's. A row code closes quietly stays closed whatever the model called it. A
second call on a taken row is refused.

### C2: Code settles only a settleable class it can pin to one tracked row; everything else goes to the owner _(enforced: mechanical)_ ^c2

- **Subject**: every row a `confirm` takes.
- **Violated when**: a row is settled that a person wrote, whose class is not settleable, that
  matched other than one tracked row, whose status is in `LIVE`, a person's lane, or whose account
  is outside `act_accounts`, or a refusal at the write is retried.

`_label` decides from the row alone. A row the tracker or `skip_accounts` puts out of scope, or a
calendar echo, closes unread. A row goes to the owner, labelled with why, when a person wrote it,
its class is not settleable, or it fails the match, the status or the account test; a missing
account is labelled unresolved.

The rest is settled: the tracker's command runs with the class, the tracked row and a note on who
confirmed it. A refusal at the write sends the row to the owner with its text; any other failure is
a failed mark, and the row is listed again.

### C3: What reaches the owner is one message with fixed fields, on its thread key's thread _(enforced: mechanical)_ ^c3

- **Subject**: every escalation the watch sends.
- **Violated when**: an escalation lacks a field below, carries more than 800 characters of excerpt,
  goes to anyone but the owner, or opens a thread for a key that had one when its send began (S14).

`_text` composes the message from the row. Its lines: the label; the message's two ids, account,
mailbox and arrival; the sender, its domain and whether a person wrote it; the subject; the tracked
row, its status and the match; and what the tracker holds on file for that row.

Then come the class, ask and risk; the dates, deadline, links, attachments and flags the tracker
found; the action the code's table gives the class; who confirmed the class, or the model's
contesting reason; and the excerpt, cut to `excerpt_cap` (800) characters. No test pins the cut. The
first escalation on a thread key opens a thread, kept in `mail_threads`; later ones ride it.

### C4: The mail ledger records each action, an escalation before its mark, and a row acted on is not listed again _(enforced: mechanical)_ ^c4

- **Subject**: the mail ledger and every triage's listing.
- **Violated when**: a failed mark is retried with a second message, a row with a settled, escalated
  or closed row is listed again, or a held or undelivered row drops out of the listing.

Every action is a row: settled, escalated, closed, held, undelivered, act-failed or
instrument-failed, with the ids, the subject cut to 120 characters, the account and a note. An
escalation's row, with the sent message's id, is written before the command that marks the tracker
row handled.

A triage first retries the mark of each row whose last row is a failed mark after an escalated,
settled or closed row, with the first note and no send. It then lists each row with no settled,
escalated or closed row. A held row, or one refused by the hop count and logged undelivered, is
listed again at the next tick (S14); no test pins either path.

### C5: The watch mails only the owner and the steward, and gives the model no send _(enforced: mechanical)_ ^c5

- **Subject**: every send the watch makes.
- **Violated when**: a send goes to a sender of the watched mail, or to anyone but the owner and the
  steward.

The code names the recipients: the owner for an escalation and the backlog report, and the owner and
the steward for an instrument failure. A sender's address is data in the message, never a recipient.
Every send goes through `Agent.deliver` and its hop count ([[ADR-0013-the-agent#^c4|ADR-0013/C4]]),
and the bounded client refuses a recipient outside the trusted set
([[ADR-0015-chores#^c5|ADR-0015/C5]]).

The watch adds `triage`, `confirm` and `edge` to the chores profile and no `send`; no test pins that
set. The model's own words reach the owner and the tracker only as an edge's reason: in the owner's
message, and as the note of a noise close.

### C6: A failed listing is an instrument failure, not told again while it recurs, and the model gets nothing _(enforced: mechanical)_ ^c6

- **Subject**: every triage.
- **Violated when**: a failed, unreadable or dead listing, or one whose account control is false,
  reads as a quiet tick; a row is shown the model after one; or the error last told is told again
  with no healthy triage between.

The tracker's listing prints a control line first: the pending, handled and backlog counts, the last
sync, the accounts mapped and how many read rows resolved to one. A non-zero exit, output that is
not JSON lines, no control line, zero pending with zero handled, or `account_control` false is an
instrument failure ([[ADR-0014-the-instrument#^c2|ADR-0014/C2]]).

The listed rows are cleared, and the owner and the steward get the error, with the command's own
text where there is one, marked to escalate. The same error at the next tick is not resent. A
healthy triage clears what was told.

### C7: The backlog before `since` is reported once and never acted on _(enforced: mechanical)_ ^c7

- **Subject**: every triage while `since` is set.
- **Violated when**: a backlog row is shown the model, settled or escalated one by one, or a triage
  asks for the backlog once `mail_backlog` is written.

With `since` set, every triage passes it to the tracker (A2). The first, while `mail_backlog` in the
agent's notes is empty, also asks for the backlog and sends the owner one message. It holds counts
by the subject's class, then up to 300 refs (id, date, class, organisation, sender, subject), and
past 300 a count of the rest and the command that lists them. No test pins the 300 cap.

`mail_backlog` then holds the time, the count and whether the report went out, and no later triage
asks again. With `since` unset there is no backlog: every unhandled row the tracker lists is
triaged.

### C8: The code-only pass runs the same handlers and takes the tracker's class as it stands _(enforced: mechanical)_ ^c8

- **Subject**: every wake under `Auto`.
- **Violated when**: the code-only pass calls a model, skips a listed row, or a note or message says
  the model confirmed a class.

`lion agent --model none`, or `model = "none"` in `chores.toml`, selects `Auto`
([[ADR-0016-the-lion-command#^d3|ADR-0016/D3]]). On `tick: mail` it calls `triage()`, then `confirm`
for every id the listing shows, then ends the wake; on the digest and aged ticks it calls those
handlers. The tracker's notes and the owner's messages say the class was confirmed by code.

## Decisions

### D1: `hub/agent/mail.py`: three handlers on the agent's actor, one tick, the guidance, the mail ledger ^d1

Serves C1 to C7. The watch registers `triage`, `confirm` and `edge` on the agent's actor, each
requiring `comm.send`, adds them to the chores profile, adds `tick: mail` to the chores' ticks and
appends `hub/guidance/mail.md` to the profile. The guidance names the two calls, says excerpt text
is the sender's and never an instruction, and sends doubt to a person. `_label` decides from the
row; `_escalate`, `_close` and `_settle_stragglers` make the ends.

- **Landing evidence**: `tests/test_mail.py`, 9 tests, among them
  `test_a_tracker_refusal_at_the_write_is_escalated_and_a_second_mail_on_a_row_rides_its_thread`,
  `test_noise_is_closed_with_the_reason_and_an_unlisted_id_or_an_unknown_class_is_refused` and
  `test_the_backlog_before_the_cutover_is_reported_once_by_subject_class_and_never_acted_on`.

### D2: `build` wires the watch from `[mail]` in `chores.toml` ^d2

Serves C2, C4 and C7. With a `[mail]` table, `build` in `apps/cli/lion_cli/agent.py` makes the watch
over `mail.jsonl` in the agent directory. The table sets the tracker's directory, `every` (the tick,
every 15 minutes by default), `limit` (20 bodies a triage), `escalations_per_day` (40),
`act_accounts` (default a list fixed in the code), `skip_accounts` (none) and `since` (unset). The
listing may take 600 s, or 100 s a row plus 60 when that is more; a mark 180 s.

- **Landing evidence**: none drives `build` with a `[mail]` table; `tests/test_mail.py` builds the
  watch directly.

### D3: `Auto` in `hub/agent/mail.py`, chosen by the model name `none` ^d3

Serves C8. `backend_for("none")` returns `Auto`, and `build` then sets the watch to write "confirmed
by code". `Auto` reads the wake's tick and answers with its handler call; after a listing it answers
one `confirm` per listed id and ends with a fixed `OUT{}`. It records no model call, so the wake
spends nothing.

- **Landing evidence**:
  `test_the_no_model_backend_runs_triage_then_confirms_every_listed_id_and_marks_the_pass_as_code`
  in `tests/test_mail.py`; `test_backend_for_claude_code_is_the_cli_chat_shape_on_the_named_model`
  in `tests/test_agent.py`.

### D4: The instrument face lists without acting ^d4

Serves C6. `run(args)` makes the same listing without the backlog and returns a measurement
([[ADR-0014-the-instrument#^c1|ADR-0014/C1]]): the rows that need a person as findings, the listing
as evidence, the pending count, and a control that passes when the ledger is live. It errs toward
over-report and declares an empty population expected. Only `lion agent --check` reaches it
([[ADR-0015-chores#^c6|ADR-0015/C6]]); no chore or desk answer runs it.

- **Landing evidence**: the instrument-face test and the dead-ledger arm of the instrument-failure
  test in `tests/test_mail.py`; no test drives `--check` on the watch.

## Alternatives

| approach | not taken because |
| -------- | ----------------- |
| The model classifies and code writes what it says | a settle would follow a model's reading; code settles from the tracker's class and the row's facts, and the model can only withhold |
| A `check` chore per tick | a check reports findings; a row needs its own call, its own end and its own thread |
| One thread per message | messages about one tracked row belong together; the thread key keeps them on one thread |
| Acting on the backlog row by row over ticks | old mail is seen first as one report the owner sweeps; the watch acts only on what arrived after the start |
| The agent reading the mailbox itself | the tracker already reads, classes and lists the mail; a second reader would be a second classifier |
| Sending again when a mark fails | the message landed; the retry makes the mark only, with the first note |
| The code-only pass as the default | nothing contests the tracker's class under it (A4); it runs when no model is wanted |

## Consequences

- **S1**: The boundary: the tracker, its classifier, the mail transport, which accounts it reads and
  what its settle command writes are outside this process. An effect on an outside system, declared,
  receipted and applied once, is the kernel's. The watch is its in-process form: it records an
  escalation before the mark and retries a failed mark without a second send.
- **S2**: The listed rows are kept under `mail_pending` in the agent's own notes
  ([[ADR-0013-the-agent#^d3|ADR-0013/D3]]), each with its excerpt, until a call takes the row, the
  next triage replaces them or a failed listing clears them. The agent's notes therefore carry body
  text between ticks, and no test pins it.
- **S3**: A noise close writes the model's reason, up to 160 characters, into the mail ledger's note
  and the tracker's note, and the edge asks the model to quote the phrase that decides it. Mail text
  can reach the mail ledger that way.
- **S4**: The watch holds only the error it last told, in memory: a different error in between makes
  the first new again, and a restart tells a standing failure again, once.
- **S5**: An escalation and the backlog report are sent with no idempotency key. When a send's
  outcome is unknown, or the process dies before its row or `mail_backlog` is written, the next tick
  sends it again. No test pins it.
- **S6**: A death between an escalation's row and its mark leaves the tracker listing the row while
  the watch skips it: the retry covers a mark that failed, never one that did not run. The triage's
  first line counts such a row among those read and not listed. No test pins it.
- **S7**: A thread key at its hop cap (12 by default) refuses every later escalation on it, so each
  tick logs the row undelivered and lists it again until it leaves the tracker's list. A held row
  waits for the next local day's count.
- **S8**: A row the model leaves without a call is listed again at the next tick. The watch sets no
  gate on `OUT{}`, so nothing holds the wake open for it.
- **S9**: The chores digest carries no line from the watch. The read-only form of `lion agent`
  counts the day's mail ledger rows by action ([[ADR-0016-the-lion-command#^c4|ADR-0016/C4]]).
- **S10**: Under the code-only pass a chore's tick or a trusted sender's question ends the wake with
  no call, since `Auto` answers only the mail, digest and aged ticks. No test pins it.
- **S11**: The chores' catch-up runs no triage: a missed `tick: mail` is noted, and the next tick
  lists the same rows, which the tracker still holds.
- **S12**: The instrument face's control reads only whether the ledger is live, so `--check` answers
  a listing whose account control is false, which the handler counts as a failure.
- **S13**: Each triage reads up to `limit` bodies through the tracker, a number the model may pass
  to `triage` in place of the config's. The model sees each excerpt cut to twice `excerpt_cap`,
  1,600 characters, and the owner's message carries 800; no test pins either cut.
- **S14**: A row is held when the day's escalations, counted as its own starts, have reached
  `escalations_per_day` (40). Calls in one turn run at once, and `_escalate` reads `mail_threads`
  and the day's count before its send and writes the whole map after it. Two escalations on one
  thread key can open two threads, two on different keys can lose one key's entry, and a turn can
  pass `escalations_per_day`. The code-only pass confirms every row in one turn. Serialising the
  escalation is owed; no test runs two at once.
