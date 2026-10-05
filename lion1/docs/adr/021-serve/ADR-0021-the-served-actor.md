---
adr: ADR-0021
status: proposed
liveness: operating (the service, its logs, the transport over real loopback sockets and the example are pinned by tests, and each mechanism S1 names turned a named test red)
date: "2026-10-02"
area: serve
kind: new
depends_on:
  - "[[ADR-0001-the-actor|ADR-0001]]"
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0003-the-bounds|ADR-0003]]"
  - "[[ADR-0005-command-handling|ADR-0005]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
  - "[[ADR-0010-delegation|ADR-0010]]"
  - "[[ADR-0017-the-desk|ADR-0017]]"
  - "[[ADR-0018-chat-in-a-box|ADR-0018]]"
tags:
  - adr
  - runtime
  - serve
---

# ADR-0021: The served actor

## Context

This record serves the author's request of 2026-10-02 that serving an agent be as easy as serving an
API, starting with a local app and designed in a record first; its first consumer is the native app
(ADR-0022). Its transport serves his of 2026-10-05: serving is split out, to the hub or anywhere, as
`Lion().serve(actor)` under `asyncio.run`; FastAPI replaces the hand-written logic.

An actor is reached only by the Python that calls `Actor.run`. Every program outside that process
gets its own glue: the areas server reads residents' files
([[ADR-0017-the-desk#^c11|ADR-0017/C11]]), and the terminal chat drives `Actor.run` with its own
`wait` ([[ADR-0018-chat-in-a-box#^c1|ADR-0018/C1]]). Nothing else can start a run, watch it or speak
to it.

This record fixes one service over one actor: what it offers, how a run is started, watched, spoken
to and ended, and one HTTP transport bound to loopback. The bus already tells a run as it happens
([[ADR-0005-command-handling#^c2|ADR-0005/C2]]), and the record already has a row form
([[ADR-0018-chat-in-a-box#^d1|ADR-0018/D1]]). The service forwards both.

It decides no remote bind, no token, no tool-protocol transport, no store beyond one log file per
run, and no client. Source: `src/lion1/actor.py` (`run`, `Run.inbox`, `Run.interrupt`),
`src/lion1/bus.py`, `src/lion1/checkpoint.py` (`entry_of`, `entry_row`, `load_log`), `hub/chat.py`
(`chat`), `apps/cli/lion_cli/areas.py` (`guarded`).

## Definitions

- **service**: the object that holds one actor's offers and its served runs, with the operations a
  client may ask of them; it knows no HTTP.
- **offer**: a profile, a backend and a request template the service will run, named by the
  profile's name; a served run starts from an offer and from nothing else.
- **card**: the service's description of itself as JSON: the actor's name, the protocol version and
  each offer with its commands, their schemas, its outputs and its resolved bounds.
- **served run**: a run the service started, with its id, its task, its state and its subscribers.
- **transport**: code that carries the service's operations over a wire; this record has one, HTTP
  with a server-sent event stream.
- **client**: any program that speaks to a transport.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | `Actor.run` emits `run.started` before its first await, so a subscriber set in the same step as the call sees that run's id first. | `run` builds the run, replays history, appends the inputs and emits with no await between | the run takes its id as an argument, a change to the loop, and D2 is rewritten |
| A2 | One event loop carries the actor, its runs and the transport, and the owner's handlers and awaited backend calls give it back within a second, which nothing in the core enforces; an offer's synchronous backend holds it for its whole call (S6). | the bus and the actor are single-loop; the OpenRouter backend calls out on a thread and the Claude Code and Codex backends await their subprocesses, while `Actor.run` calls a synchronous backend inline | one slow handler or synchronous backend stalls every stream; the transport moves to a thread and bridges to the loop |
| A3 | uvicorn's h11 reader hands the app each request with the peer it read off the connection and the header lines as sent, a repeated line kept, and answers a head it cannot read itself. | the guard reads the peer from the ASGI scope and counts Host and Origin in its raw header list; measured on uvicorn 0.30.0 and 0.54.0 | the guard reads a value the reader rewrote, and its checks move in front of the reader |
| A4 | A local client needs the rows it missed and the state as it stands, and can do without the bus events it missed. | every row carries its sequence number and a stream opens with the state; an event that leaves no row is a progress mark | the stream gains event ids and a bounded replay |
| A5 | A row is complete at `run.started`, at each `turn.started`, at each `command.` event, when the run waits and when it ends. | `Actor.run` attaches a notification's fold as it appends the notification, with no event between; `chat` in `hub/chat.py` hands each entry to its `say` callback at these points | the runtime gains an event that marks an entry final, and D3 publishes on it |

## Claims

### C1: A client starts only what the owner offered _(enforced: mechanical)_ ^c1

- **Subject**: every run a client starts.
- **Violated when**: a start names a profile not offered; carries a prompt, a class, a callable, an
  unknown field or a value outside D2's table; carries a bound above the offer's; or a second offer
  takes a name already given.

A start names an offer and carries data only: the inputs, bounds, the id of a run to resume and a
`once` key. D2's table fixes each field's type and range. Anything else refuses the start whole,
with its reason, before an id or a log is reserved, and no run starts. The run's request is the
offer's template with those inputs.

A client's bound is the run's starting bound, at or below what the template resolves to; a bound
that resolves to none takes any value in range (S12). A start past the service's count of live runs
is refused. An offer is fixed when it is made (D1).

### C2: A served run is the run `Actor.run` makes _(enforced: mechanical)_ ^c2

- **Subject**: every served run's record, outcome and events.
- **Violated when**: a record through the service differs from the one `Actor.run` gives for the
  same effective request, backend script, stores and schedule of inputs and controls, the run's id
  and clock readings aside. Also when the service appends to a record or changes an outcome.

The service calls `Actor.run` once per start, a task on the actor's loop. It writes two things on
the run: input (C5) and the interrupt (C6). An error before `run.started` is a refusal with no run,
and the log reserved for it is removed; a cancel before then keeps it, aborted (D7). After it the
state says how the run ended. Departures: a log that stops its run (C8), and `run.waiting`, the
service's own event (D4). Rows and events are per run as far as ids differ (S11).

### C3: A row is published complete, once, in order _(enforced: mechanical)_ ^c3

- **Subject**: every row a record read returns or a stream carries.
- **Violated when**: a published row differs from the record's row at the run's end; a row arrives
  twice or out of order on one stream; or a run awaits a subscriber's queue or a socket.

Rows are published at `run.started`, at each `turn.started`, at each `command.` event, at each wait
and at the run's end, and at no other event (A5). A row is in the chat log's form
([[ADR-0018-chat-in-a-box#^d1|ADR-0018/D1]]). Every bus event of a served run is forwarded as
emitted, with or without rows.

Each subscriber has a queue bounded in frames. One that overflows is closed and never waited for.

### C4: A stream opens with the state, so a late client still learns the end _(enforced: mechanical)_ ^c4

- **Subject**: every stream and every state.
- **Violated when**: a stream's first frame is not the state (on the stream of every run, the `runs`
  frame); a row published after it misses the stream; or one run's stream outlives the run. Also
  when a stream closes at a run's end without a final state, or an ended run's state lacks how it
  ended.

A subscriber's queue is registered in the step that takes its first state, so no later row misses
it. For an ended run it is the whole stream.

When a run ends or aborts, short of shutdown (C11), its own streams get the rows not yet sent, one
final state, and close; the stream of every run takes the same and stays open. Reading the record
after its first frame, a client has every row of a kept run (C14). Earlier bus events are not
replayed (A4). A closed stream is resumed by subscribing again.

### C5: Input is queued on the run it names, and the state says what was taken _(enforced: mechanical)_ ^c5

- **Subject**: every input a client sends.
- **Violated when**: an input lands on another run, on the actor's shared inbox or on an ended run;
  an accepted input is neither taken nor listed as untaken in the final state; or one `once` key
  among the last 100 acts twice.

Acceptance means queued. The service appends an inbound to the named run's own inbox and wakes a
waiting run in the same step. The run takes it at its next turn as INPUT
([[ADR-0010-delegation#^c1|ADR-0010/C1]]), with an origin the service sets. The answer is a handle,
and the state lists the handles queued and not yet taken. A repeated `once` key returns the first
answer.

### C6: Interrupt, stop and cancel are the runtime's own, and so is the outcome _(enforced: mechanical)_ ^c6

- **Subject**: the three controls on a live served run.
- **Violated when**: an interrupt ends a run; a stop leaves a run waiting; a stop or a cancel
  replaces a committed outcome; or a cancel before the terminal commit reports an outcome.

Interrupt is `Run.interrupt`: it cancels an awaited call. A backend that lets the cancel through
ends the turn with no message; one that catches it makes its message; a synchronous call is not cut
(S6). A conversation then waits for input once no command is pending (C15); another goes on.

Stop wakes a waiting run, whose wait answers false, and cancels an awaited call; pending commands
settle, and the run ends `Stopped` at its next turn unless it ended first.

Cancel cancels the task: an abort with no outcome ([[ADR-0002-the-run#^c4|ADR-0002/C4]]). The
terminal commit follows the last reap ([[ADR-0020-the-experience|ADR-0020]]), so a cancel during
that reap is before it (S20). A cancel with an interrupt is the cancel
([[ADR-0018-chat-in-a-box#^c9|ADR-0018/C9]]).

### C7: With a directory, a run's log holds what was published and says how the run ended _(enforced: mechanical)_ ^c7

- **Subject**: a service started with a directory.
- **Violated when**: a row is streamed that the log lacks; a log is created over an existing file;
  or a log is left for a start that was refused. Also when a restart reads a run as ended from a
  closure its history replayed, or a resume names anything but a stored run's id.

A run's log, `<dir>/<id>.jsonl`, is created exclusively with its `SERVE` row before `Actor.run` is
called. Each row is appended before a subscriber gets it; the task's end appends `END` unless the
log is latched (C8).

A start naming an earlier run replays that log's entries as `history`
([[ADR-0018-chat-in-a-box#^c6|ADR-0018/C6]]). After a restart a log with `END` lists as ended and
one without as cut short, latched or not: read whole, it may hold one row no client was sent (S14).
A file without a whole `SERVE` row is not listed. Without a directory a resume is refused.

### C8: A log that cannot be written stops its run and is never written again _(enforced: mechanical)_ ^c8

- **Subject**: a log file that cannot be created, and an append that fails.
- **Violated when**: a row past a failed append is served; anything, the `END` row included, is
  appended after it; or a `turn.started` follows it. Also when the failure is absent from the state,
  it changes the outcome, or a run starts whose log could not be created.

The bus records a subscriber's error and goes on ([[ADR-0005-command-handling#^c2|ADR-0005/C2]]), so
the service catches its own write failure. The first failed append latches the log, so a torn line
can only be its last. While the service runs, reads and streams serve the rows confirmed before it,
and a dropped latched run answers `gone`.

The run is stopped (C6) and its state names the failure. A failure at a `turn.started` follows that
turn's stop check, so its backend call still goes out, and an output its message makes accepted ends
the run `Success`.

### C9: The transport answers this machine's loopback only _(enforced: mechanical)_ ^c9

- **Subject**: every connection to the HTTP transport.
- **Violated when**: a bind off loopback succeeds; a peer off loopback is answered; a request whose
  Host or Origin is not one D8 accepts is answered on any route; or a response carries a CORS
  header.

The transport binds `127.0.0.1` or `::1` and refuses any other address. Before it dispatches, it
checks the peer, then that Host is one of the spellings D8 lists for the bound address and port,
then that an Origin, when present, is `http://` followed by one of them. A POST must be
`application/json`. The app's answers carry `nosniff` and `no-store`; a head uvicorn cannot read
never reaches the app, and uvicorn answers it (S23).

This admits any process on the machine that can open the port. There is no token, and the checks
stop a web page while identifying no client (S3).

### C10: uvicorn reads the request, and the app bounds what a route waits on _(enforced: mechanical)_ ^c10

- **Subject**: every request the transport answers.
- **Violated when**: a request D8's table refuses reaches a route; a body is read past 1 MiB or past
  its deadline; or a request past the cap of requests in flight is dispatched.

uvicorn reads each request with its h11 reader, and a connection may carry several in turn. The
app's guard runs before any route: C9's checks, then the end of the service, then the cap of
requests in flight. A POST's body is read as it arrives, at most 1 MiB, whole within one deadline of
10 seconds. Anything outside D8's table is answered with its error and never dispatched.

A plain response carries its length, and a stream is chunked and ends with its response. Subscribers
are capped too, and one past either cap is refused. A connection and its head are not (S24).

### C11: Ending the service ends everything it started _(enforced: mechanical)_ ^c11

- **Subject**: `serve` returning, by cancellation or by an error.
- **Violated when**: after it returns a request is answered; a connection, stream or ping task is
  alive; a served run's task is live; a bus subscription of the service remains; or an unlatched log
  lacks a row that was published or its `END` row.

Admission closes first: every later request is refused and every stream closes, so none takes a
final state (C4). Live runs are stopped together (C6) as uvicorn closes the listener and its
connections, and awaited under one grace deadline, then cancelled and awaited. Each unlatched log
gets its `END` row, and the bus subscriptions are removed last.

Cancellation is cooperative. A handler that will not yield holds the return, and the service does
not kill it (S6).

### C12: The core gains no dependency; the transport's are the `serve` extra _(enforced: mechanical)_ ^c12

- **Subject**: `src/lion1/`, `hub/serve/` and the package's dependency lists.
- **Violated when**: the core imports a web framework or the hub; `hub/serve/service.py` imports
  anything but the standard library, pydantic with its core and `lion1`; or the core's dependency
  list changes.

`hub/serve/transport.py` alone imports FastAPI and uvicorn, which `lion1[serve]` installs.

### C13: The card tells a client what the model is told, and nothing of the prompt _(enforced: mechanical)_ ^c13

- **Subject**: the card.
- **Violated when**: it lists a command the offer's profile lacks, a schema differs from the Spec's
  own JSON schema, a bound differs from what a run of that offer resolves, or it carries the role
  prompt or a credential.

Per offer the card holds the profile's name and language, each command of the profile's subset
([[ADR-0001-the-actor#^c5|ADR-0001/C5]]) with its doc, JSON schema, effect class and required
privileges, each output's name and schema, the resolved bounds, and whether its runs are
conversations. It names the actor and protocol version 1.

### C14: What the service holds is bounded in count, and a run it no longer holds says so _(enforced: mechanical)_ ^c14

- **Subject**: ended runs kept in memory, inputs queued on a run, and frames queued for a
  subscriber.
- **Violated when**: a read of a dropped run the service remembers answers as an unknown run or as
  an empty one; a record read with a directory lacks rows its unlatched log holds; or an input past
  the cap is accepted.

Live runs are always held. Ended runs are held up to a count, the oldest dropped first. With a
directory, a dropped run's state and record are read from its log, unless it was latched while the
service ran (C7, C8). With none, a dropped run answers `gone`, between two pages as on a first read,
and a client starts over from what it holds. It remembers the last 10,000 dropped ids (S21).

A run holds at most 64 inputs not yet taken, and one more is refused. The bounds are counts, and a
row's size is not bounded (S16).

### C15: A conversation waits for input, and `end` closes it _(enforced: mechanical)_ ^c15

- **Subject**: every run of an offer made as a conversation, and `end`.
- **Violated when**: input is accepted after a run's `end`; an `end` leaves a run waiting; an `end`
  replaces a committed outcome; or an `end` is accepted on a run that is no conversation.

An offer made as a conversation gives its runs a `wait`: after a reply, a turn with no commands, the
run waits for input (D1). An `OUT{}` ends it as it ends any run. The client's `end` closes the
conversation: later input is refused, a waiting run wakes, and the run ends `Stopped` at its next
wait unless a bound or an accepted output ended it first.

Input may still be taken while the exchange goes on through command turns. A handle not taken when
the next wait or the run's end closes it stays untaken, and the final state lists it (C5). An `end`
on any other run is refused.

## Decisions

### D1: `hub/serve/service.py` holds `Service` and `Offer`; `entry_row` sits beside `entry_of` ^d1

Serves C1, C12 and C13. `Service(actor, *, dir=None, max_live=8, keep=100)`; `keep` counts ended
runs held, and `max_live` under 1, `keep` under 0 or a non-integer raises `ValueError`.

`Service.offer(profile, backend, request=RunRequest(), *, converse=False)` raises `ValueError`,
naming which, for a template setting `wait` or `reply_on_commands` or a name already offered. It
copies the profile, with its own `defaults`, and the template; the profile's stores and a request's
callbacks stay the caller's, live. `Service.card()` builds the card from `Actor.subset` and
`model_json_schema`. `entry_row` and `load_log` live in `src/lion1/checkpoint.py`, which the hub
imports.

- **Landing evidence**: `tests/test_serve.py`:
  `test_the_card_lists_each_command_as_the_model_is_told_it_and_nothing_of_the_prompt`,
  `test_an_offer_is_fixed_when_made_and_one_the_service_cannot_serve_is_refused_by_name`,
  `test_a_capacity_that_is_no_count_is_refused_when_the_service_is_made`,
  `test_the_core_serves_nothing_and_the_transport_is_the_serve_extra`.

### D2: `Service.start` checks, reserves an id and a log, then reads the run from the bus ^d2

Serves C1, C2, C7 and C8. `start` checks the fields below, loads a resume's history, and only then
draws a 16-hex id that no live run, kept run, log file or kept `once` answer has. It creates the log
with its `SERVE` row and the task in one step, and from then the run is live. The wrapper subscribes
to `run.started`, calls `Actor.run` and unsubscribes on every exit (A1). `start` answers once the
run began, was refused or was cancelled first (D6); a refusal after the reservation removes the log.

| field | type | rule |
| ----- | ---- | ---- |
| `offer` | string | required; an offered name |
| `inputs` | list of strings | optional; at most 16, none empty, each at most 1 MiB in UTF-8 |
| `bounds` | object | optional; keys below only; a key absent or null takes the offer's value |
| `max_turns`, `max_idle`, `max_refusals`, `context_budget` | integer | 1 or more; a boolean or a float is refused |
| `time_budget`, `cost_budget` | number | finite and above 0; a boolean is refused |
| `resume` | string | 16 lowercase hex digits; the log of a run that ended or was cut short |
| `once` | string | 1 to 64 characters of letters, digits, `.`, `_` and `-` |

- **Landing evidence**: `tests/test_serve.py`:
  `test_a_served_run_records_what_actor_run_records_for_the_same_request`,
  `test_a_start_outside_the_table_is_refused_whole_before_an_id_or_a_log_is_reserved`,
  `test_a_start_at_or_below_the_offers_bounds_runs_there_and_a_null_bound_takes_the_offers`,
  `test_a_start_past_the_live_count_is_refused_and_the_next_after_an_end_is_not`,
  `test_two_starts_in_one_loop_turn_reserve_two_ids_and_two_logs`,
  `test_a_start_over_an_existing_log_is_refused_and_the_draw_skips_every_id_in_use`,
  `test_a_start_actor_run_refuses_is_refused_with_its_reason_and_leaves_no_run_log_or_key`,
  `test_a_serve_row_that_cannot_be_written_is_log_failed_and_leaves_no_file_run_or_key`.

### D3: One publisher per run, called at the publishing points ^d3

Serves C3, C7 and C8. `_publish` takes the record's rows past the run's cursor, appends and flushes
each to the log, advances the cursor after the whole line, then hands the rows to each subscriber's
queue of 1,000 frames with `put_nowait`. Subscriptions on `run.started`, `turn.started`, `command.`
and `run.completed` call it, as do the service's `wait` and the task's end. A write or a flush that
raises latches the log: the file is closed, the cursor stays, the state takes the failure, the run
is stopped, and later calls publish nothing.

- **Landing evidence**: `tests/test_serve.py`:
  `test_a_fold_in_turn_one_is_whole_on_the_row_the_log_the_read_and_the_stream`,
  `test_rows_reach_the_log_the_record_read_and_every_stream_once_in_order_as_the_record_has_them`,
  `test_a_stream_that_overflows_is_closed_never_waited_for_and_a_new_one_opens_with_the_state`,
  `test_a_failed_append_latches_the_log_stops_the_run_and_nothing_is_served_or_written_after_it`.

### D4: The reads: `state`, `record` and `events` ^d4

Serves C4 and C14. `state(id)`; `record(id, start=0)`, at most 500 rows from a sequence number, with
`more`; and `events(id=None)`, an async iterator. `events` registers its queue and takes its first
item with no await between: the state, or for all runs a `runs` frame, empty when no run is held. A
run's end puts the last rows, the final state and a close on its own streams' queues, after the
`END` row; the stream of every run takes them too and stays open. The service's `wait` emits
`run.waiting`.

- **Landing evidence**: `tests/test_serve.py`:
  `test_a_stream_opened_after_the_end_is_the_final_state_and_one_racing_the_end_takes_the_rest`,
  `test_an_aborted_run_closes_its_stream_with_a_final_state_naming_the_error_and_no_outcome`,
  `test_a_stream_that_overflows_is_closed_never_waited_for_and_a_new_one_opens_with_the_state`,
  `test_an_ended_run_past_keep_is_dropped_oldest_first_and_a_read_between_two_pages_says_so`,
  `test_the_ids_of_dropped_runs_are_remembered_to_a_count_and_one_forgotten_is_an_unknown_run`,
  `test_a_dropped_run_whose_id_is_forgotten_reads_from_its_log_as_after_a_restart`.

### D5: `say`, `end` and the service's `wait` ^d5

Serves C5, C14 and C15. `say(id, text, once=None)` refuses text past 1 MiB in UTF-8, appends to
`run.inbox` an `Inbound` from `client` with a fresh handle, the service's id as `origin_run` and no
alias (S19), sets the wake event, returns the handle. `end(id)` sets the ended flag, the state's
`closed` and the wake event with no await between. The `wait` loops: false once the ended or stopped
flag is set, read first; true when the inbox holds input; else it clears the wake event, checks
again and sleeps on it.

- **Landing evidence**: `tests/test_serve.py`:
  `test_input_mid_turn_waits_for_the_next_turn_and_input_never_taken_is_listed_at_the_end`,
  `test_input_lands_on_the_run_it_names_and_never_on_the_actors_shared_inbox`,
  `test_input_outside_its_form_is_refused_and_nothing_is_queued`,
  `test_end_closes_a_conversation_at_its_next_wait_wherever_it_lands`,
  `test_end_is_refused_on_a_run_no_conversation_and_a_wake_that_finds_nothing_sleeps_again`,
  `test_a_second_end_while_the_run_is_live_answers_its_state_and_sends_no_frame`.

### D6: `interrupt`, `stop` and `cancel` ^d6

Serves C6. `interrupt(id)` calls `Run.interrupt`; `cut` in its answer says whether a pending call
was found and its cancellation requested, which a backend may absorb: the run's rows say what it
did. `stop(id)` sets the flag the service's `stop` callback reads and the wake event, with no await
between, then calls `Run.interrupt`. `cancel(id)` cancels the task and awaits it; on a starting run
that is the reserved task, aborted with no runtime id. Each answers the state. An ended run refuses
all three, a starting run all but `cancel`.

- **Landing evidence**: `tests/test_serve.py`:
  `test_stop_ends_a_run_stopped_at_its_next_turn_unless_it_ended_first`,
  `test_an_interrupt_cuts_the_call_in_flight_and_the_run_goes_on_or_waits_for_input`,
  `test_an_interrupt_with_no_call_in_flight_cuts_nothing`,
  `test_a_cancel_before_the_commit_is_an_abort_with_no_outcome_even_with_an_interrupt`,
  `test_a_cancel_in_the_last_reap_aborts_with_no_outcome_ever_shown_and_a_stop_changes_nothing`,
  `test_a_starting_run_takes_a_cancel_only_and_ends_aborted_with_no_runtime_id`.

### D7: The log's `SERVE` and `END` rows ^d7

Serves C7 and C8. The `SERVE` row holds `kind`, `version`, `id`, `offer`, `resumed` and `replayed`.
The `END` row holds `kind` and the final `state` (D9), so a dropped run reads whole from its log; a
task cancelled before its first step gets one in phase `aborted`. `entry_of` skips both. `load_log`,
given a `torn` list, drops a last line that is not JSON as a cut write; one with lines after it
makes the log unreadable. The listing after a restart reads each log's first and last line.

- **Landing evidence**: `tests/test_serve.py`:
  `test_a_resume_replays_a_stored_runs_log_and_a_restart_reads_each_log_by_its_own_end_row`,
  `test_after_a_restart_the_newest_logs_are_kept_by_end_row_and_one_without_a_serve_row_is_not`,
  `test_a_resume_names_a_stored_runs_id_and_one_whose_log_cannot_be_replayed_is_refused`,
  `test_a_dropped_run_reads_whole_from_its_log_and_one_whose_log_failed_is_gone_until_a_restart`,
  `test_a_run_cancelled_while_starting_whose_end_row_fails_is_latched_and_stopped`.

### D8: The HTTP transport: FastAPI on uvicorn ^d8

Serves C9 and C10. `serve(service, host="127.0.0.1", port=8710, grace=10.0, *, ready=None)` in
`hub/serve/transport.py` runs until cancelled and reports the address it bound to `ready`; a port of
0 takes a free one, and a `host` outside loopback is refused with `ValueError` before any bind (C9).
It binds the socket and hands it to uvicorn: the h11 reader, no lifespan, no WebSocket, no logging
configured. `app(service, hosts)` is FastAPI's routes behind an ASGI guard.

| method and path | does | answers |
| --------------- | ---- | ------- |
| `GET /` | the card | 200 |
| `GET /runs` | the states of live and kept runs | 200 |
| `POST /runs` | starts a run from D2's fields | 201 with the state, or 200 for a repeated `once` key; 400, 404, 409, 410, 413, 422 or 429 |
| `GET /runs/{id}` | the state | 200; 404 or 410 |
| `GET /runs/{id}/record?start=N` | at most 500 rows from N, and `more` | 200; 400, 404, 409 or 410 |
| `GET /runs/{id}/events` and `GET /events` | the stream of one run, or of every run | 200 as `text/event-stream`; 404 or 410 |
| `POST /runs/{id}/input` | queues `text`, optionally with `once` | 202 with the handle; 409 or 429 |
| `POST /runs/{id}/end`, `/interrupt`, `/stop`, `/cancel` | the control | 202 with the state; 409 |
| any route | the shared answers | also 400, 403, 404, 405, 408, 410, 413, 415, 500 or 503, where D9 says they fit |

| the request | rule |
| ----------- | ---- |
| head | read by uvicorn's h11 reader; one it cannot read, or one still incomplete past 16 KiB, is uvicorn's own 400 (S23) |
| Host | exactly one: `127.0.0.1:<port>` or `localhost:<port>` on a `127.0.0.1` bind; `[::1]:<port>` or `localhost:<port>` on a `::1` bind; `localhost` in any letter case |
| Origin | at most one: `http://` followed by one of the Host spellings |
| query | none, but a record read's `start=N` |
| Content-Type | on a POST, exactly one: `application/json` in any letter case, with no parameter |
| body | a POST's, by Content-Length or chunked: UTF-8 holding one JSON object, no key twice and no NaN or Infinity, at most 1 MiB, whole within 10 seconds; a GET framing one, chunked or by a length not zero, is a bad request |
| a chunk of a body | ended by CRLF; h11 0.16 and later refuse one that is not before the body is whole, so no run starts (S23) |
| Content-Length, `start` | decimal digits, decided by the digits: a length of more than 20 digits is a head h11 refuses (S23), one past the body's cap is too large, and a `start` past 18 digits is a bad request |

- **Landing evidence**: `tests/test_serve_http.py`:
  `test_a_body_outside_d8s_form_is_answered_with_its_error_and_never_dispatched`,
  `test_a_body_in_d8s_form_is_dispatched_however_it_is_framed`,
  `test_a_body_nested_past_the_recursion_limit_is_a_bad_request`,
  `test_a_get_carrying_a_body_is_a_bad_request_and_a_zero_length_is_none`,
  `test_a_request_h11_cannot_read_is_answered_by_uvicorn_alone`,
  `test_a_host_or_origin_d8_does_not_list_for_the_bound_address_is_forbidden_on_every_route`,
  `test_a_peer_off_loopback_is_forbidden_before_any_route`,
  `test_a_host_off_loopback_is_refused_with_valueerror_before_any_bind`,
  `test_a_port_in_use_raises_the_bind_error_and_leaves_no_socket_open`,
  `test_a_scope_that_is_not_http_is_left_unanswered`,
  `test_a_body_not_whole_by_the_deadline_is_answered_timeout_however_its_bytes_arrive`,
  `test_a_peer_that_closes_before_its_request_is_whole_is_never_answered`.

### D9: Shapes, frames and errors ^d9

Serves C4, C10 and C14. Every answer of the app but a stream (D12) is one JSON object in a shape
below; a key holds null where it has no value, and a null `pending` means unknown, never empty.
Values become JSON through `pydantic_core.to_jsonable_python`; one it cannot project is carried as
an object with the single key `repr`, so what a client reads is a projection and never the object. A
change that breaks a client raises the card's protocol version.

| shape | keys |
| ----- | ---- |
| `state` | the keys of the next table |
| `runs` | `runs`, a list of `state` |
| `card` | `actor`; `protocol`; `offers`, a list of `offer` |
| `offer` | `name`; `language`; `commands`, each with `name`, `doc`, `schema`, `effect` (`write`, `read` or `pure`) and `privileges`, a list; `outputs`, each with `name` and `schema`; `bounds`, each bound's name to a number or null; `converse`, a boolean |
| `outcome` | `kind`, one of `Success`, `Exhausted`, `Refused` and `Stopped`, beside that outcome's own fields but `output`: `turns` and `reason`, `why` and `times`, or `turn` |
| `rows` | `id`; `start`, the first row's sequence number; `rows`, each in the log's row form; `more`, true on a record read with rows past the page, false on a stream frame |
| `bus` | `id`; `kind`; `payload` |
| `handle` | `id`; `handle` |
| `error` | `error`, a code below; `reason`, a sentence |

| state key | value |
| --------- | ----- |
| `id`, `offer`, `resumed` | the service's id; the offer's name; the id of the run resumed, or null |
| `run` | the runtime's id; null until `run.started`, on a run aborted before it began and on a cut-short log |
| `phase` | `starting` until `run.started`, then `working` or `waiting`, and last `ended` or `aborted`; `starting` may go straight to `aborted`; `cut_short` for a log with no `END` row after a restart |
| `turn`, `cost` | the run's turn and cost so far; null on a cut-short log |
| `rows` | the count of rows published; null on a cut-short log, whose record read holds them |
| `pending` | the handles not taken; null on a cut-short log |
| `closed` | true once an `end` was accepted, after which input is refused; else false; null on a cut-short log |
| `outcome`, `output` | an `outcome` and a `Success`'s output; null until ended, after an abort and on a cut-short log |
| `error` | the log's failure; on a run an exception aborted, its type and message; else null |
| `cut` | on an interrupt's answer only: whether a pending awaited call was found and its cancellation requested |

| request | body |
| ------- | ---- |
| input | `text`, a string of at least one character; `once`, optional, in D2's form; no other key |
| `end`, `interrupt`, `stop`, `cancel` | an empty object |

| code | status | when |
| ---- | ------ | ---- |
| `bad_request` | 400 | a body outside D2's table or the one above; a bound above its offer's; a request D8 refuses |
| `bad_request` | 400 | a record `start` outside 0 to the rows that read can serve: the confirmed rows while the service runs, the whole rows of a cut-short log after a restart |
| `forbidden` | 403 | the peer, the Host or the Origin |
| `unknown_offer`, `unknown_run`, `not_found` | 404 | no such offer, run or route |
| `method_not_allowed` | 405 | a route asked with the other method |
| `timeout` | 408 | the body's deadline |
| `ended` | 409 | input after the run's `end` or on an ended run; a control on an ended run; either on a cut-short log |
| `starting` | 409 | input, or a control other than `cancel`, on a run still starting |
| `not_conversation` | 409 | `end` on a run that is no conversation |
| `no_resume`, `unreadable_log` | 409 | a resume with no directory, no such log or a live run; a resume whose log cannot be read or whose history does not replay; a record read of a log the loader refuses |
| `conflict` | 409 | a held `once` key repeated with other fields (D13) |
| `gone` | 410 | a run no longer held, with no log to read or a latched one |
| `too_large` | 413 | the body, or an input's text past D5's bound |
| `refused` | 422 | a start `Actor.run` refused before `run.started`, the error's type and message as the reason |
| `unsupported_type` | 415 | a POST that is not `application/json` |
| `too_many` | 429 | live runs, or inputs not taken |
| `log_failed` | 500 | a log that could not be created |
| `busy` | 503 | requests in flight or subscribers past the cap, or the service ending |

- **Landing evidence**: `tests/test_serve_http.py`:
  `test_each_route_answers_as_the_table_says_in_the_shapes_d9_gives`,
  `test_each_code_is_answered_with_its_status_and_a_reason`,
  `test_a_route_outside_the_table_or_a_query_where_none_is_taken_is_refused`; `tests/test_serve.py`:
  `test_every_outcome_kind_is_projected_with_its_own_fields_and_the_output_beside_it`,
  `test_a_value_json_cannot_hold_is_carried_by_its_repr_and_never_as_the_object`; and the example of
  D11.

### D10: `Lion`, and the order of the end ^d10

Serves C11. `Lion(*, dir, max_live, keep, host, port, grace)` takes offers by `offer(profile,
backend, request=None, *, converse=False)`, which returns it; `serve(actor, *, ready=None)` serves a
`Service` of them. The end: `Service.halt` refuses admission, closes every stream and stops every
live run. Then uvicorn closes its connections, cutting a stalled write after 5 seconds, while the
runs are awaited under `grace`; what remains is cancelled, then the `END` rows and the bus
subscriptions. The serve leaves out uvicorn's signal handlers. At most 64 requests in flight and 32
subscribers.

- **Landing evidence**: `tests/test_serve_http.py`:
  `test_ending_the_service_refuses_admission_cuts_each_connection_and_ends_each_run`,
  `test_serving_leaves_the_signals_to_its_caller`,
  `test_lion_serves_its_offers_on_the_port_it_is_given_until_cancelled`,
  `test_an_ending_seen_through_a_cancel_that_then_fails_reaches_its_caller_and_no_one_else`,
  `test_a_stream_whose_reader_stalls_is_closed_at_its_frame_cap_and_the_run_never_waits`,
  `test_a_request_past_the_cap_is_refused_busy_and_the_next_after_one_ends_is_not`,
  `test_a_stream_past_the_subscriber_cap_is_refused_busy`,
  `test_a_request_after_the_end_began_is_refused_busy`,
  `test_a_start_while_the_service_ends_is_refused_busy`.

### D11: `examples/serve_an_actor.py` and its test ^d11

Serves C2 and C4. The example serves the structured-output actor through `Lion` on a scripted
backend at a free port, then acts as its own client over the socket. It reads the card, starts a
run, opens the stream and finishes on the first state that says ended, whether that is the stream's
first frame or a later one. `tests/test_examples.py` runs it offline.

- **Landing evidence**: `serve_an_actor` in the scripted set of `tests/test_examples.py`, and
  `test_the_served_example_reads_the_card_a_run_and_its_end_over_the_socket`, which pins its output.

### D12: The stream's frames ^d12

Serves C3 and C4. A stream frame is one server-sent event named `state`, `runs`, `rows` or `bus`,
its data one object of that shape. A `state` frame follows the first whenever its run's phase
changes or its `closed` turns true, and the stream of every run gets one when a run starts. A
comment line goes out every 15 seconds.

- **Landing evidence**: `tests/test_serve.py`:
  `test_a_conversation_waits_after_a_reply_takes_input_and_end_closes_it_on_both_streams`;
  `tests/test_serve_http.py`:
  `test_a_stream_sends_a_comment_line_each_time_its_clock_steps_and_frames_in_between`.


### D13: A repeated `once` key ^d13

Serves C1 and C5. A key is checked and held in one step, and a repeat is answered before the phase
check. A repeated start with the same fields gets 200 and the first run's current state while it is
readable, else `gone`; it never starts a run. A repeated input with the same text gets the first
handle. A repeat in flight waits for the first answer. Other fields or text are refused `conflict`,
the key unchanged; only the refusal of the act that took a key releases it.

- **Landing evidence**: `tests/test_serve.py`:
  `test_a_repeated_once_key_starts_nothing_and_answers_the_first_run_while_it_is_readable`,
  `test_a_start_actor_run_refuses_is_refused_with_its_reason_and_leaves_no_run_log_or_key`,
  `test_an_input_once_key_past_the_last_held_acts_again`,
  `test_a_repeated_start_whose_runs_id_was_forgotten_is_gone_and_never_an_unknown_run`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Extending the areas server to any actor | it reads residents' files from a registry, on a thread per request, inside the command-line package; a served run needs the actor's own loop and nothing of the registry |
| The standard library's threaded server with a bridge to the loop | every event crosses a thread and shutdown owns threads beside tasks; uvicorn runs on the actor's loop (D8) |
| A web framework | rejected at first as a dependency in the core, or `Actor.serve` absent without an extra; reversed on 2026-10-05, when the transport moved to the hub: FastAPI and uvicorn are its `serve` extra, and the core gains nothing (C12) |
| The transport in the core, as `Actor.serve` over `asyncio.start_server` | the form until 2026-10-05: a reader written by hand, one request per connection, with rules of its own on the raw head; replaced by `Lion` in the hub over FastAPI (D8, D10) |
| Checks on the raw head in front of uvicorn | h11 refuses a space before a colon and a second Host, and takes a folded line, a bare line feed, `Expect` and Transfer-Encoding beside a length; with no proxy between, none is read two ways. The guard counts Host and Origin as sent (A3) |
| A bound on each write of a stream | a stalled stream's queue overflows and closes it, and the run never waits (C3); at the end uvicorn cuts its write after 5 seconds |
| A run lookup for an empty run id (`/runs/`), the earlier transport's | FastAPI routes a run id of one character or more, so `/runs/` is no route and answers `not_found` (D9); no run has an empty id |
| A deadline on the head and a cap on connections, the earlier transport's | uvicorn has neither for a head still arriving, and adding them means its protocol class subclassed; a request and its body are bounded instead (C10, S24) |
| A client that sends its own profile, prompt or tools | the actor would run what any local page or process hands it; the owner's offer is the gate (C1) |
| Publishing rows at every bus event | every event falls on a complete row today, and nothing holds the runtime to that: an event added between a notification and its fold would publish half a row to the log and every stream; the named points are the ones `chat` uses (A5) |
| The runtime's 8-hex run id as the served run's id | a directory outlives the process and 32 bits collide across kept logs; the id is known only once the run began, too late to reserve its log |
| Replay of missed events by id | a second ordered store beside the record; the state frame and the record read recover the rows and the end (A4) |
| A failed append that only marks the state | rows would stream that no log holds, and a resume would replay less than the client saw (C8) |
| Appending the `END` row, or retrying, after a failed append | the new row either joins the torn bytes into one last line the loader drops, or follows them past a line feed, where the loader refuses the whole file; neither keeps the row, and the cursor cannot repair bytes already written (C8) |
| Calling a handler with no run | a `Context` belongs to a run ([[ADR-0005-command-handling#^c3\|ADR-0005/C3]]); its bounds and its request's gates are the run's, and the actor's `before` hooks apply to every run it serves |
| An error code on `Service.offer`'s refusal | it is owner-side Python, a `ValueError` naming the field; D9's codes are the transport's |
| A WebSocket | it needs a framing layer or a dependency; a stream down and POSTs up carry everything here |
| A check in the loop before each backend call, so a log failing at `turn.started` holds that call back | a change to the core for one failure path; the one call it lets out is the run's last, and its output may stand (C8) |
| A gate in the loop that takes no input after `end` | a second change to the core; the exchange in flight finishes its command turns, and what it never took stays listed (C15) |

## Consequences

- **S1**: D1 to D13 landed with the tests they name. Disabled alone, each mechanism turned a named
  test red: C1's bound check, C3's publishing points, C8's latch; C4's final state, C5's inbox, C6's
  wake; C7's `END` row. So did C9's peer, Host and Origin checks; C10's body deadline and request
  cap; C11's unsubscription, early stop and signal handlers; C14's dropped ids; D9's status table,
  D12's comment line and D13's conflict check.
- **S2**: The areas server and the terminal chat are unchanged. Two servers stand until a client
  moves; a chat's box and its patch behind an offer are not decided here.
- **S3**: Loopback is per machine: any process of any account on the machine that can open the port
  is a client. A token per launch is the next step where that matters, and it does not wait for a
  remote bind; a remote bind and TLS are a later record.
- **S4**: Inputs are text. An input with an image waits for the record to carry one.
- **S5**: An actor's shared inbox is still taken by whichever of its runs starts a turn first
  ([[ADR-0010-delegation#^c1|ADR-0010/C1]]), a served run included: `Actor.run` drains it with the
  run's own. The service writes nothing to it; with several live runs the limit stands for a peer's
  `queue`.
- **S6**: A handler that blocks the loop stalls every run and every stream (A2), and so does an
  offer's synchronous backend for the whole of its call; a log append is a file write on the loop.
  `serve` returns only once every handler has yielded to its cancellation.
- **S7**: A tool-protocol transport over the same service, and a `lion serve` command, are later
  changes; the command amends [[ADR-0016-the-lion-command|ADR-0016]].
- **S8**: A restart loses live runs: their logs remain and a resume is the client's ask (C7). The
  `once` keys live in memory, so a key replayed after a restart acts again.
- **S9**: The boundary: who may start what, a durable store of runs and supervision across restarts
  are the kernel's; here an offer is the grant and a file is the store. This serves `Actor.run`
  until the kernel's exec and observer surfaces land, when start, watch and stop route there;
  mid-run input (`say`) has no counterpart there and stays here. Every kernel loopback surface is
  bearer-credentialed, so C9's no-token choice does not carry over if this actor is ever served by
  or beside khived.
- **S10**: The transport is in the hub, outside the core's coverage gate. Its tests open real
  loopback sockets, and with the service's and the example's they reach every line and branch of
  `hub/serve/`; nothing holds that figure but a run that measures it.
- **S11**: The service routes a bus event to its served run by the runtime's id, so it rests on that
  id being unique among an actor's live runs. `Run` draws 8 hex digits and `Actor.runs` keys on them
  unchecked, so a new run's id meets a given live run's one time in 2^32; the service does not check
  it, and two runs that drew one id would mix their events on the streams. The service's own ids
  name the logs.
- **S12**: A client's lowered bound is where the run starts. An offer whose `extend` grants more
  lets the run rise past it, and an owner who wants a ceiling offers an `extend` that refuses.
- **S13**: One service owns a directory. A second service over the same directory cannot write into
  a log it did not create, and lists the first one's live runs as cut short.
- **S14**: A row is written and flushed, never synced. A power loss may take the log's tail, which
  then reads as a write that was cut. After a restart a log with no `END` row is read whole, so a
  line whose append failed may be among its rows though no client was sent it: C7's log holds every
  published row, and after a restart it may hold one more.
- **S15**: C6's cancel that arrives with an interrupt rests on the loop telling the two apart by the
  task's count of cancel requests ([[ADR-0018-chat-in-a-box#^d6|ADR-0018/D6]]), so a client's cut
  and cancel in one step end the run as a cancel.
- **S16**: The caps count runs, inputs, frames and requests, never bytes. An input is at most 1 MiB
  by D2 and D5, on every path, so a run's untaken inputs are bounded; a row is not, and a stream of
  large rows fills memory up to 1,000 frames a subscriber.
- **S17**: A `once` key is scoped to its operation: starts across the service, inputs within one
  run. The last 100 of each are held in memory, and a key past that acts again, as after a restart.
- **S18**: A resumed view equals the earlier run's rows only in what `entry_of` keeps. Its filter of
  a row's meta (`src/lion1/checkpoint.py`) keeps the keys it names and drops any other, so a key an
  app added to a row's meta is not carried into history.
- **S19**: `_deliver` answers a consumed inbound to the actor its origin names
  ([[ADR-0010-delegation#^c3|ADR-0010/C3]]). A client's input names `client`, so it is answered to
  no one while no actor has that name, and the service refuses a start while one is registered.
- **S20**: Between naming a run's outcome and its return the runtime awaits the last reap, and a
  command that absorbs its cancel holds that reap open. A cancel there is an abort and a stop
  changes nothing; D6 names the test. Nothing is awaited between the reap and the return.
- **S21**: A dropped run whose id is forgotten reads as after a restart: from its log as the log has
  it, a failure while it was held unremembered, or with no log as an unknown run.
- **S22**: A run's rows carry the model's text as it is published, an `OUT{}` in it included, before
  the run ends. Only an ended state's outcome and output say what the run committed. A run aborted
  during its last reap keeps those rows, in its record and in its log.
- **S23**: A head uvicorn's reader refuses (a second Host, a space before a colon, a Content-Length
  of more than 20 digits) and a chunk not ended by CRLF (D8) are answered by uvicorn alone. Its
  answer is a 400 in plain text, without `nosniff` or `no-store`, logged as a warning. A stream
  whose peer reads nothing holds the end for uvicorn's 5 seconds, and uvicorn logs its cancel as an
  error; the runs do not wait for it (C11).
- **S24**: Nothing bounds a connection that sends no request or a head still arriving: uvicorn holds
  both open, and only the end closes them. No idle timeout covers either in this design: uvicorn's
  keep-alive timeout starts only once a response is sent. Any process on the machine can hold as
  many sockets as it opens; the caps count requests once read (C10).
