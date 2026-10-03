---
adr: ADR-0021
status: proposed
liveness: unimplemented
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
(ADR-0022).

An actor is reached only by the Python that calls `Actor.run`. Every program outside that process
gets its own glue: the areas server reads residents' files
([[ADR-0017-the-desk#^c11|ADR-0017/C11]]), and the terminal chat drives `Actor.run` with its own
`wait` ([[ADR-0018-chat-in-a-box#^c1|ADR-0018/C1]]). A native app, a script in another language or a
second terminal cannot start a run, watch it or speak to it.

This record fixes one service over one actor: what it offers, how a run is started, watched, spoken
to and ended from outside, and one HTTP transport bound to loopback. The bus already tells a run as
it happens ([[ADR-0005-command-handling#^c2|ADR-0005/C2]]), and the record already has a row form
([[ADR-0018-chat-in-a-box#^d1|ADR-0018/D1]]). The service forwards both and adds no second model of
either.

It decides no remote bind, no token, no tool-protocol transport, no store beyond one log file per
run, and no client. Source: `src/lion1/actor.py` (`run`, `Run.inbox`, `Run.interrupt`),
`src/lion1/bus.py`, `src/lion1/checkpoint.py` (`entry_of`), `hub/chat.py` (`chat`, `entry_row`,
`load_log`), `apps/cli/lion_cli/areas.py` (`guarded`).

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
| A3 | A reader of one origin-form HTTP/1.1 request per connection, with a counted body, is small enough that its tests reach every line and branch, as the core's coverage gate requires (S10). | C10 and D8 name every form taken and refused; the reader's own checks run before the standard library parses the lines | the transport is rebuilt on the standard library's threaded server; the service is unchanged |
| A4 | A local client needs the rows it missed and the state as it stands, and can do without the bus events it missed. | every row carries its sequence number and a stream opens with the state; an event that leaves no row is a progress mark | the stream gains event ids and a bounded replay |
| A5 | A row is complete at `run.started`, at each `round.started`, at each `command.` event, when the run waits and when it ends. | `Actor.run` attaches a notification's fold after it emits `memory.recalled`; `chat` in `hub/chat.py` hands each entry to its `say` callback at these points | the runtime gains an event that marks an entry final, and D3 publishes on it |

## Claims

### C1: A client starts only what the owner offered _(enforced: mechanical; owed, S1)_ ^c1

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

### C2: A served run is the run `Actor.run` makes _(enforced: mechanical; owed, S1)_ ^c2

- **Subject**: every served run's record, outcome and events.
- **Violated when**: a record through the service differs from the one `Actor.run` gives for the
  same effective request, backend script, stores and schedule of inputs and controls, the run's id
  and clock readings aside. Also when the service appends to a record or changes an outcome.

The service calls `Actor.run` once per start, a task on the actor's loop. It writes two things on
the run: input (C5) and the interrupt (C6). An error before `run.started` is a refusal with no run,
and the log reserved for it is removed; a cancel before then keeps it, aborted (D7). After it the
state says how the run ended. Departures: a log that stops its run (C8), and `run.waiting`, the
service's own event (D4). Rows and events are per run as far as ids differ (S11).

### C3: A row is published complete, once, in order _(enforced: mechanical; owed, S1)_ ^c3

- **Subject**: every row a record read returns or a stream carries.
- **Violated when**: a published row differs from the record's row at the run's end; a row arrives
  twice or out of order on one stream; or a run awaits a subscriber's queue or a socket.

Rows are published at `run.started`, at each `round.started`, at each `command.` event, at each wait
and at the run's end, and at no other event (A5). A row is in the chat log's form
([[ADR-0018-chat-in-a-box#^d1|ADR-0018/D1]]). Every bus event of a served run is forwarded as
emitted, with or without rows.

Each subscriber has a queue bounded in frames. One that overflows is closed and never waited for.

### C4: A stream opens with the state, so a late client still learns the end _(enforced: mechanical; owed, S1)_ ^c4

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

### C5: Input is queued on the run it names, and the state says what was taken _(enforced: mechanical; owed, S1)_ ^c5

- **Subject**: every input a client sends.
- **Violated when**: an input lands on another run, on the actor's shared inbox or on an ended run;
  an accepted input is neither taken nor listed as untaken in the final state; or one `once` key
  among the last 100 acts twice.

Acceptance means queued. The service appends an inbound to the named run's own inbox and wakes a
waiting run in the same step. The run takes it at its next round as INPUT
([[ADR-0010-delegation#^c1|ADR-0010/C1]]), with an origin the service sets. The answer is a handle,
and the state lists the handles queued and not yet taken. A repeated `once` key returns the first
answer.

### C6: Interrupt, stop and cancel are the runtime's own, and so is the outcome _(enforced: mechanical; owed, S1)_ ^c6

- **Subject**: the three controls on a live served run.
- **Violated when**: an interrupt ends a run; a stop leaves a run waiting; a stop or a cancel
  replaces a committed outcome; or a cancel before the terminal commit reports an outcome.

Interrupt is `Run.interrupt`: it cancels an awaited call. A backend that lets the cancel through
ends the round with no turn; one that catches it makes its turn; a synchronous call is not cut (S6).
A conversation then waits for input once no command is pending (C15); another goes on.

Stop wakes a waiting run, whose wait answers false, and cancels an awaited call; pending commands
settle, and the run ends `Stopped` at its next round unless it ended first.

Cancel cancels the task: an abort with no outcome ([[ADR-0002-the-run#^c4|ADR-0002/C4]]). A cancel
with an interrupt is the cancel ([[ADR-0018-chat-in-a-box#^c9|ADR-0018/C9]]).

### C7: With a directory, a run's log holds what was published and says how the run ended _(enforced: mechanical; owed, S1)_ ^c7

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

### C8: A log that cannot be written stops its run and is never written again _(enforced: mechanical; owed, S1)_ ^c8

- **Subject**: a log file that cannot be created, and an append that fails.
- **Violated when**: a row past a failed append is served; anything, the `END` row included, is
  appended after it; or a `round.started` follows it. Also when the failure is absent from the
  state, it changes the outcome, or a run starts whose log could not be created.

The bus records a subscriber's error and goes on ([[ADR-0005-command-handling#^c2|ADR-0005/C2]]), so
the service catches its own write failure. The first failed append latches the log, so a torn line
can only be its last. While the service runs, reads and streams serve the rows confirmed before it,
and a dropped latched run answers `gone`.

The run is stopped (C6) and its state names the failure. A failure at a `round.started` follows that
round's stop check, so its backend call still goes out, and an output its turn makes accepted ends
the run `Success`.

### C9: The transport answers this machine's loopback only _(enforced: mechanical; owed, S1)_ ^c9

- **Subject**: every connection to the HTTP transport.
- **Violated when**: a bind off loopback succeeds; a peer off loopback is answered; a request whose
  Host or Origin is not one D8 accepts is answered on any route; or a response carries a CORS
  header.

The transport binds `127.0.0.1` or `::1` and refuses any other address. Before it dispatches, it
checks the peer, then that Host is one of the spellings D8 lists for the bound address and port,
then that an Origin, when present, is `http://` followed by one of them. A POST must be
`application/json`. Responses carry `nosniff` and `no-store`.

This admits any process on the machine that can open the port. There is no token, and the checks
stop a web page while identifying no client (S3).

### C10: One request per connection, in one strict form _(enforced: mechanical; owed, S1)_ ^c10

- **Subject**: every request the transport reads.
- **Violated when**: a request D8's table refuses is dispatched; a request is dispatched on the
  strength of the parsed headers alone; or a slow sender holds a connection past the deadline.

The transport reads one request per connection and closes it. Its own checks on the raw head come
first, and the standard library's parser reads only lines that passed them. A POST carries exactly
one decimal Content-Length and that many bytes, and a GET carries no body. Anything outside this
form is answered with its error and never dispatched. One deadline of 10 seconds covers the whole
request, however its bytes arrive.

A plain response carries its length, and a stream ends when the connection closes. Connections and
subscribers are capped, and one past the cap is refused.

### C11: Ending the service ends everything it started _(enforced: mechanical; owed, S1)_ ^c11

- **Subject**: `serve` returning, by cancellation or by an error.
- **Violated when**: after it returns a request is answered; a connection, stream or ping task is
  alive; a served run's task is live; a bus subscription of the service remains; or an unlatched log
  lacks a row that was published or its `END` row.

Admission closes first: the listener stops and requests already read are refused. Connection, stream
and ping tasks are cancelled, each write bounded, so no stream takes a final state (C4). Live runs
are stopped together (C6) and awaited under one grace deadline, then cancelled and awaited. Each
unlatched log gets its `END` row, and the bus subscriptions are removed last.

Cancellation is cooperative. A handler that will not yield holds the return, and the service does
not kill it (S6).

### C12: The core gains no dependency and imports no provider _(enforced: mechanical; owed, S1)_ ^c12

- **Subject**: `src/lion1/serve.py` and the package's dependency list.
- **Violated when**: the module imports anything but the standard library, pydantic with its core
  and `lion1`, or the dependency list changes.

### C13: The card tells a client what the model is told, and nothing of the prompt _(enforced: mechanical; owed, S1)_ ^c13

- **Subject**: the card.
- **Violated when**: it lists a command the offer's profile lacks, a schema differs from the Spec's
  own JSON schema, a bound differs from what a run of that offer resolves, or it carries the role
  prompt or a credential.

Per offer the card holds the profile's name and language, each command of the profile's subset
([[ADR-0001-the-actor#^c5|ADR-0001/C5]]) with its doc, JSON schema, effect class and required
privileges, each output's name and schema, the resolved bounds, and whether its runs are
conversations. It names the actor and protocol version 1.

### C14: What the service holds is bounded in count, and a run it no longer holds says so _(enforced: mechanical; owed, S1)_ ^c14

- **Subject**: ended runs kept in memory, inputs queued on a run, and frames queued for a
  subscriber.
- **Violated when**: a read of a run no longer kept answers as an unknown run or as an empty one; a
  record read with a directory lacks rows its unlatched log holds; or an input past the cap is
  accepted.

Live runs are always held. Ended runs are held up to a count, the oldest dropped first. With a
directory, a dropped run's state and record are read from its log, unless it was latched while the
service ran (C7, C8). With none, a dropped run answers `gone`, between two pages as on a first read,
and a client starts over from what it holds.

A run holds at most 64 inputs not yet taken, and one more is refused. The bounds are counts, and a
row's size is not bounded (S16).

### C15: A conversation waits for input, and `end` closes it _(enforced: mechanical; owed, S1)_ ^c15

- **Subject**: every run of an offer made as a conversation, and `end`.
- **Violated when**: input is accepted after a run's `end`; an `end` leaves a run waiting; an `end`
  replaces a committed outcome; or an `end` is accepted on a run that is no conversation.

An offer made as a conversation gives its runs a `wait`: after a reply, a turn with no commands, the
run waits for input (D1). An `OUT{}` ends it as it ends any run. The client's `end` closes the
conversation: later input is refused, a waiting run wakes, and the run ends `Stopped` at its next
wait unless a bound or an accepted output ended it first.

Input may still be taken while the exchange goes on through command rounds. A handle not taken when
the next wait or the run's end closes it stays untaken, and the final state lists it (C5). An `end`
on any other run is refused.

## Decisions

### D1: `src/lion1/serve.py` holds `Service` and `Offer`; `entry_row` moves beside `entry_of` ^d1

Serves C1, C12 and C13. `Service(actor, *, dir=None, max_live=8, keep=100)`; `keep` counts ended
runs held in memory. `Service.offer(profile, backend, request=RunRequest(), *, converse=False)`
raises `ValueError`, naming which, for a template setting `wait` or `reply_on_commands` or a name
already offered. It keeps a copy of the profile with its own `defaults` dict, and of the template;
the stores the profile names and the callbacks a request carries stay the caller's, live.
`Service.card()` builds the card from `Actor.subset` and `model_json_schema`. `entry_row` and
`load_log` move from `hub/chat.py` to `src/lion1/checkpoint.py`, and the hub imports them.

- **Landing evidence**: owed: `tests/test_serve.py` for the card against the model's own guidance,
  the import boundary and the dependency list.

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
| `max_rounds`, `max_idle`, `max_refusals`, `context_budget` | integer | 1 or more; a boolean or a float is refused |
| `time_budget`, `cost_budget` | number | finite and above 0; a boolean is refused |
| `resume` | string | 16 lowercase hex digits; the log of a run that ended or was cut short |
| `once` | string | 1 to 64 characters of letters, digits, `.`, `_` and `-` |

- **Landing evidence**: owed: the parity test of C2, a refusal per row of the table with no file
  left, two starts in one loop turn, a start over an existing log, a history that does not replay.

### D3: One publisher per run, called at the publishing points ^d3

Serves C3, C7 and C8. `_publish` takes the record's rows past the run's cursor, appends and flushes
each to the log, advances the cursor after the whole line, then hands the rows to each subscriber's
queue of 1,000 frames with `put_nowait`. Subscriptions on `run.started`, `round.started`, `command.`
and `run.completed` call it, as do the service's `wait` and the task's end. A write or a flush that
raises latches the log: the file is closed, the cursor stays, the state takes the failure, the run
is stopped, and later calls publish nothing.

- **Landing evidence**: owed: a recall and a fold in round one across record, log and stream; order
  and once-only; an overflow; a short write, a failed flush, a failed first and later
  `round.started` under both backend kinds, and calls after the latch.

### D4: The reads: `state`, `record` and `events` ^d4

Serves C4 and C14. `state(id)`; `record(id, start=0)`, at most 500 rows from a sequence number, with
`more`; and `events(id=None)`, an async iterator. `events` registers its queue and takes its first
item with no await between: the state, or for all runs a `runs` frame, empty when no run is held. A
run's end puts the last rows, the final state and a close on its own streams' queues, after the
`END` row; the stream of every run takes them too and stays open. The service's `wait` emits
`run.waiting`.

- **Landing evidence**: owed: a run that ends before its stream opens, a subscription racing the
  end, an abort, a second subscription after an overflow, a read between two pages of a dropped run.

### D5: `say`, `end` and the service's `wait` ^d5

Serves C5, C14 and C15. `say(id, text, once=None)` refuses text past 1 MiB in UTF-8, appends to
`run.inbox` an `Inbound` from `client` with a fresh handle, the service's id as `origin_run` and no
alias (S19), sets the wake event, returns the handle. `end(id)` sets the ended flag, the state's
`closed` and the wake event with no await between. The `wait` loops: false once the ended or stopped
flag is set, read first; true when the inbox holds input; else it clears the wake event, checks
again and sleeps on it.

- **Landing evidence**: owed: input mid-round, before a wait and at the end; `end` while waiting,
  mid-call, during command rounds, a retry and the first round, with input queued; a wake that finds
  nothing; the 65th input.

### D6: `interrupt`, `stop` and `cancel` ^d6

Serves C6. `interrupt(id)` calls `Run.interrupt`; `cut` in its answer says whether a pending call
was found and its cancellation requested, which a backend may absorb: the run's rows say what it
did. `stop(id)` sets the flag the service's `stop` callback reads and the wake event, with no await
between, then calls `Run.interrupt`. `cancel(id)` cancels the task and awaits it; on a starting run
that is the reserved task, aborted with no runtime id. Each answers the state. An ended run refuses
all three, a starting run all but `cancel`.

- **Landing evidence**: owed: stop while waiting, mid-call, during a command and after an accepted
  output; cancel before the commit and during it; cancel with an interrupt in both orders.

### D7: The log's `SERVE` and `END` rows ^d7

Serves C7 and C8. The `SERVE` row holds `kind`, `version`, `id`, `offer`, `resumed` and `replayed`.
The `END` row holds `kind` and the final `state` (D9), so a dropped run reads whole from its log; a
task cancelled before its first step gets one in phase `aborted`. `entry_of` skips both. `load_log`,
given a `torn` list, drops a last line that is not JSON as a cut write; one with lines after it
makes the log unreadable. The listing after a restart reads each log's first and last line.

- **Landing evidence**: owed: a resumed run aborting after an ancestor that succeeded, a cut `END`
  row, a file with no whole `SERVE` row, a resume id that is a path, a dropped run's untaken input,
  and a latched log after a restart.

### D8: The HTTP transport on `asyncio.start_server` ^d8

Serves C9 and C10. `Service.serve(host="127.0.0.1", port=8710, grace=10.0)` runs until cancelled and
reports the address it bound; a port of 0 takes a free one, and a `host` outside loopback is refused
with `ValueError` before any bind (C9). The head is read to its blank line under the cap, checked
against the second table, and only then parsed by `http.client.parse_headers`; a parsed message with
a defect is refused.

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

| the raw request | rule |
| --------------- | ---- |
| request line | `GET` or `POST`, an origin-form target, `HTTP/1.1`; anything else is refused |
| line endings | CRLF only; a bare line feed is refused |
| header line | a token name, a colon with no space before it, a value with no control character; a line that starts with a space or a tab is refused |
| Host, Origin, Content-Type, Content-Length | at most one of each; Host is required |
| Transfer-Encoding, Expect | refused when present |
| Host | `127.0.0.1:<port>` or `localhost:<port>` on a `127.0.0.1` bind; `[::1]:<port>` or `localhost:<port>` on a `::1` bind; `localhost` in any letter case |
| body | a POST's is exactly Content-Length bytes of UTF-8 holding one JSON object, at most 1 MiB; a GET with a Content-Length other than 0 is refused |
| head | at most 16 KiB |

- **Landing evidence**: owed: each row of the second table and each refusal of C9 over real loopback
  sockets, raw requests sent whole and in pieces; the off-loopback peer with its address substituted
  at the check; an off-loopback `host` refused before any bind.

### D9: Shapes, frames and errors ^d9

Serves C4, C10 and C14. Every answer but a stream (D12) is one JSON object in a shape below; a key
holds null where it has no value, and a null `pending` means unknown, never empty. Values become
JSON through `pydantic_core.to_jsonable_python`; one it cannot project is carried as an object with
the single key `repr`, so what a client reads is a projection and never the object. A change that
breaks a client raises the card's protocol version.

| shape | keys |
| ----- | ---- |
| `state` | the keys of the next table |
| `runs` | `runs`, a list of `state` |
| `card` | `actor`; `protocol`; `offers`, a list of `offer` |
| `offer` | `name`; `language`; `commands`, each with `name`, `doc`, `schema`, `effect` (`write`, `read` or `pure`) and `privileges`, a list; `outputs`, each with `name` and `schema`; `bounds`, each bound's name to a number or null; `converse`, a boolean |
| `outcome` | `kind`, one of `Success`, `Exhausted`, `Refused` and `Stopped`, beside that outcome's own fields but `output`: `rounds` and `reason`, `why` and `times`, or `round` |
| `rows` | `id`; `start`, the first row's sequence number; `rows`, each in the log's row form; `more`, true on a record read with rows past the page, false on a stream frame |
| `bus` | `id`; `kind`; `payload` |
| `handle` | `id`; `handle` |
| `error` | `error`, a code below; `reason`, a sentence |

| state key | value |
| --------- | ----- |
| `id`, `offer`, `resumed` | the service's id; the offer's name; the id of the run resumed, or null |
| `run` | the runtime's id; null until `run.started`, on a run aborted before it began and on a cut-short log |
| `phase` | `starting` until `run.started`, then `working` or `waiting`, and last `ended` or `aborted`; `starting` may go straight to `aborted`; `cut_short` for a log with no `END` row after a restart |
| `round`, `cost` | the run's round and cost so far; null on a cut-short log |
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
| `timeout` | 408 | the request's deadline |
| `ended` | 409 | input after the run's `end` or on an ended run; a control on an ended run; either on a cut-short log |
| `starting` | 409 | input, or a control other than `cancel`, on a run still starting |
| `not_conversation` | 409 | `end` on a run that is no conversation |
| `no_resume`, `unreadable_log` | 409 | a resume with no directory, no such log or a live run; a resume whose log cannot be read or whose history does not replay; a record read of a log the loader refuses |
| `conflict` | 409 | a held `once` key repeated with other fields (D13) |
| `gone` | 410 | a run no longer held, with no log to read or a latched one |
| `too_large` | 413 | the head, the body, or an input's text past D5's bound |
| `refused` | 422 | a start `Actor.run` refused before `run.started`, the error's type and message as the reason |
| `unsupported_type` | 415 | a POST that is not `application/json` |
| `too_many` | 429 | live runs, or inputs not taken |
| `log_failed` | 500 | a log that could not be created |
| `busy` | 503 | connections or subscribers past the cap, or the service ending |

- **Landing evidence**: owed: the example of D11, written against D8 and D9 alone, a table-driven
  request per body and per code, and every outcome kind projected.

### D10: `Actor.serve`, and the order of shutdown ^d10

Serves C11. `Actor.serve(profile, backend, **options)` is a service with one offer, served. In
order: close the listener; refuse requests already read; cancel connection, stream and ping tasks,
each write bounded by 5 seconds; stop every live run and await them under `grace`; cancel what
remains and await it; append the `END` rows; remove the bus subscriptions. At most 64 connections
and 32 subscribers are open at once.

- **Landing evidence**: owed: a shutdown with an idle stream, a stalled writer, a half-read request
  and a run mid-command, which finds no task, socket or subscription left.

### D11: `examples/serve_an_actor.py` and its test ^d11

Serves C2 and C4. The example serves the structured-output actor on a scripted backend at a free
port, then acts as its own client over the socket. It reads the card, starts a run, opens the stream
and finishes on the first state that says ended, whether that is the stream's first frame or a later
one. `tests/test_examples.py` runs it offline.

- **Landing evidence**: owed: the example in the scripted set, and its output pinned.

### D12: The stream's frames ^d12

Serves C3 and C4. A stream frame is one server-sent event named `state`, `runs`, `rows` or `bus`,
its data one object of that shape. A `state` frame follows the first whenever its run's phase
changes or its `closed` turns true, and the stream of every run gets one when a run starts. A
comment line goes out every 15 seconds.

- **Landing evidence**: owed: each phase change on one run's stream and on the stream of every run,
  and the comment line under a stepped clock.


### D13: A repeated `once` key ^d13

Serves C1 and C5. A key is checked and held in one step, and a repeat is answered before the phase
check. A repeated start with the same fields gets 200 and the first run's current state while it is
readable, else `gone`; it never starts a run. A repeated input with the same text gets the first
handle. A repeat in flight waits for the first answer. Other fields or text are refused `conflict`,
the key unchanged; only the refusal of the act that took a key releases it.

- **Landing evidence**: owed: two starts with one key in one loop turn, a repeat after the run ended
  or was dropped, a repeat with other fields and then the first's own, and a key reused after its
  first start was refused.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Extending the areas server to any actor | it reads residents' files from a registry, on a thread per request, inside the command-line package; a served run needs the actor's own loop and nothing of the registry |
| The standard library's threaded server with a bridge to the loop | its parser reads the head, but the body rules, the guards and the deadline of C10 are written either way; every event then crosses a thread and shutdown owns threads beside tasks; kept as the fallback of A3 |
| A web framework | a dependency in the core, or `Actor.serve` absent without an extra |
| The transport in the hub | `Actor.serve` would need the hub installed; the service itself knows no HTTP and a later transport may live there |
| A client that sends its own profile, prompt or tools | the actor would run what any local page or process hands it; the owner's offer is the gate (C1) |
| Publishing rows at every bus event | the runtime attaches a notification's fold after `memory.recalled`, so a row published there stays incomplete in the log and on every stream (A5) |
| The runtime's 8-hex run id as the served run's id | a directory outlives the process and 32 bits collide across kept logs; the id is known only once the run began, too late to reserve its log |
| Replay of missed events by id | a second ordered store beside the record; the state frame and the record read recover the rows and the end (A4) |
| A failed append that only marks the state | rows would stream that no log holds, and a resume would replay less than the client saw (C8) |
| Appending the `END` row, or retrying, after a failed append | the new row either joins the torn bytes into one last line the loader drops, or follows them past a line feed, where the loader refuses the whole file; neither keeps the row, and the cursor cannot repair bytes already written (C8) |
| Trusting the parsed headers | the parser accepts a folded line, a space before the colon and a repeated Origin; the raw checks of D8 run first (C10) |
| Calling a handler with no run | a `Context` belongs to a run ([[ADR-0005-command-handling#^c3\|ADR-0005/C3]]); its bounds and its request's gates are the run's, and the actor's `before` hooks apply to every run it serves |
| An error code on `Service.offer`'s refusal | it is owner-side Python, a `ValueError` naming the field; D9's codes are the transport's |
| A WebSocket | it needs a framing layer or a dependency; a stream down and POSTs up carry everything here |
| A check in the loop before each backend call, so a log failing at `round.started` holds that call back | a change to the core for one failure path; the one call it lets out is the run's last, and its output may stand (C8) |
| A gate in the loop that takes no input after `end` | a second change to the core; the exchange in flight finishes its command rounds, and what it never took stays listed (C15) |

## Consequences

- **S1**: Nothing is built. D1 to D13 land with the tests they name, and until then every claim is
  owed here.
- **S2**: The areas server and the terminal chat are unchanged. Two servers stand until a client
  moves; a chat's box and its patch behind an offer are not decided here.
- **S3**: Loopback is per machine: any process of any account on the machine that can open the port
  is a client. A token per launch is the next step where that matters, and it does not wait for a
  remote bind; a remote bind and TLS are a later record.
- **S4**: Inputs are text. An input with an image waits for the record to carry one.
- **S5**: An actor's shared inbox is still taken by whichever of its runs starts a round first
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
- **S10**: The transport is under the core's coverage gate, so its tests open real loopback sockets.
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
- **S16**: The caps count runs, inputs, frames and connections, never bytes. An input is at most 1
  MiB by D2 and D5, on every path, so a run's untaken inputs are bounded; a row is not, and a stream
  of large rows fills memory up to 1,000 frames a subscriber.
- **S17**: A `once` key is scoped to its operation: starts across the service, inputs within one
  run. The last 100 of each are held in memory, and a key past that acts again, as after a restart.
- **S18**: A resumed view equals the earlier run's rows only in what `entry_of` keeps: its filter of
  a row's meta (`src/lion1/checkpoint.py`) drops a recall's receipt on a SYSTEM row, so the receipt
  is not carried into history.
- **S19**: `_deliver` answers a consumed inbound to the actor its origin names
  ([[ADR-0010-delegation#^c3|ADR-0010/C3]]). A client's input names `client`, so it is answered to
  no one while no actor has that name, and the service refuses a start while one is registered.
