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
  - "[[ADR-0020-the-experience|ADR-0020]]"
tags:
  - adr
  - runtime
  - serve
---

# ADR-0021: The served actor

## Context

An actor is reached only by the Python that calls `Actor.run`. Every program outside that process
gets its own glue: the areas server reads residents' files
([[ADR-0017-the-desk#^c11|ADR-0017/C11]]), and the terminal chat owns its own loop over `wait`
([[ADR-0018-chat-in-a-box#^c1|ADR-0018/C1]]). A native app, a script in another language or a second
terminal cannot start a run, watch it or speak to it.

This record fixes one service over one actor: what it offers, how a run is started, watched, spoken
to and ended from outside, and one HTTP transport bound to loopback. The bus already tells a run as
it happens ([[ADR-0005-command-handling#^c2|ADR-0005/C2]]), and the record already has a row form
([[ADR-0018-chat-in-a-box#^d1|ADR-0018/D1]]). The service forwards both and adds no second model of
either.

It decides no remote bind, no token, no tool-protocol transport, no store beyond one log file per
run, and no client. Source: `lionagi/actor.py` (`run`, `Run.inbox`, `Run.interrupt`),
`lionagi/bus.py`, `lionagi/checkpoint.py` (`entry_of`), `hub/chat.py` (`chat`, `entry_row`,
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
| A2 | One event loop carries the actor, its runs and the transport, and no handler holds it for long. | the bus and the actor are single-loop; the hub's backends call out on threads | one slow handler stalls every stream; the transport moves to a thread and bridges to the loop |
| A3 | A reader of one origin-form HTTP/1.1 request per connection, with a counted body, is small enough to test whole. | C10 names every form it takes and refuses; the standard library parses the header lines | the transport is rebuilt on the standard library's threaded server; the service is unchanged |
| A4 | A local client needs the rows it missed and the state as it stands, and can do without the bus events it missed. | every row carries its sequence number and a stream opens with the state; an event that leaves no row is a progress mark | the stream gains event ids and a bounded replay |
| A5 | A row is complete at `run.started`, at each `round.started`, at each `command.` event, when the run waits and when it ends. | `Actor.run` attaches a notification's fold after it emits `memory.recalled`; `chat` in `hub/chat.py` writes its log at these points | the runtime gains an event that marks an entry final, and D3 publishes on it |

## Claims

### C1: A client starts only what the owner offered _(enforced: mechanical; owed, S1)_ ^c1

- **Subject**: every run a client starts.
- **Violated when**: a start names a profile not offered; carries a prompt, a class, a callable, an
  unknown field or a value of the wrong type; carries a bound above the offer's; or a second offer
  takes a name already given.

A start names an offer and carries data only: the inputs, bounds, the id of a run to resume and a
`once` key. Each is checked by type and range, and anything else refuses the start whole, with its
reason, and no run starts. The run's request is the offer's template with those inputs.

A client's bound is the run's starting bound, at or below what the template resolves to
([[ADR-0003-the-bounds#^c1|ADR-0003/C1]]); a bound that resolves to none takes any value (S12). A
start past the service's count of live runs is refused. An offer is fixed when it is made.

### C2: A served run is the run `Actor.run` makes _(enforced: mechanical; owed, S1)_ ^c2

- **Subject**: every served run's record, outcome and events.
- **Violated when**: one offer, the same inputs and a scripted backend give a different record
  through the service than through `Actor.run`, the run's id and its clock readings aside; or the
  service appends to a record or changes an outcome.

The service calls `Actor.run` once per start and awaits it as a task on the actor's loop. It reads
the run and writes two things on it: inbound input (C5) and the interrupt (C6). An error before
`run.started` is a refusal with no run; after it the run exists and its state says how it ended.

### C3: A row is published complete, once, in order _(enforced: mechanical; owed, S1)_ ^c3

- **Subject**: every row a record read returns or a stream carries.
- **Violated when**: a published row differs from the record's row at the run's end; a row arrives
  twice or out of order on one stream; or a run awaits a subscriber's queue or a socket.

Rows are published at `run.started`, at each `round.started`, at each `command.` event, at each wait
and at the run's end, and at no other event (A5). A row is in the chat log's form
([[ADR-0018-chat-in-a-box#^d1|ADR-0018/D1]]). Every bus event of a served run is forwarded as
emitted, with or without rows.

Each subscriber has a bounded queue. One that overflows is closed and never waited for.

### C4: A stream opens with the state, so a late client still learns the end _(enforced: mechanical; owed, S1)_ ^c4

- **Subject**: every stream and every state.
- **Violated when**: a stream's first frame is not the state; a stream of an ended run stays open; a
  client that subscribes and then reads the record lacks a row; or an ended run's state lacks how it
  ended.

The state is the run's id, the runtime's run id, the offer, the round, the spend, the count of rows
published and whether the run is working, waiting, ended or aborted. At its end it holds the outcome
and the output as JSON.

A stream's first frame is the state as it stands. For an ended run that frame is the whole stream. A
client that reads the record after that frame, and drops the rows it already holds, has every row.
Bus events from before a subscription are not replayed (A4). A closed stream is resumed by
subscribing again.

### C5: Input is queued on the run it names, and the state says what was taken _(enforced: mechanical; owed, S1)_ ^c5

- **Subject**: every input a client sends.
- **Violated when**: an input lands on another run, on the actor's shared inbox or on an ended run;
  an accepted input is neither taken nor listed as untaken in the final state; or one `once` key
  among the last 100 acts twice.

Acceptance means queued. The service appends an inbound to the named run's own inbox and wakes a
waiting run in the same step. The run takes it at its next round as INPUT
([[ADR-0010-delegation#^c1|ADR-0010/C1]]), with an origin the service sets. The answer is a handle,
and the state lists the handles queued and not yet taken.

An offer made as a conversation gives its runs a `wait`: after a reply the run waits for input, and
the client's end makes its outcome `Stopped`. An `OUT{}` ends such a run as it ends any run. A
repeated `once` key returns the first answer.

### C6: Interrupt, stop and cancel are the runtime's own, and so is the outcome _(enforced: mechanical; owed, S1)_ ^c6

- **Subject**: the three controls on a live served run.
- **Violated when**: an interrupt ends a run; a stop leaves a run waiting; a stop or a cancel
  replaces an outcome the run had reached; or a cancel before the terminal commit reports an
  outcome.

Interrupt is `Run.interrupt`: a call in flight is cut and that round ends with no turn. Stop asks
the run to end. It wakes a waiting run, whose wait answers false, and cuts a call in flight. Pending
commands settle, and the run ends `Stopped` at its next round unless a bound or an accepted output
ended it first.

Cancel cancels the task. Before the terminal commit that is an abort with no outcome
([[ADR-0002-the-run#^c4|ADR-0002/C4]]). During it the outcome stands and the closure is written
([[ADR-0020-the-experience#^c5|ADR-0020/C5]]). A cancel that arrives with an interrupt is the cancel
([[ADR-0018-chat-in-a-box#^c9|ADR-0018/C9]]).

### C7: With a directory, a run's log holds what was published and says how the run ended _(enforced: mechanical; owed, S1)_ ^c7

- **Subject**: a service started with a directory.
- **Violated when**: a row is streamed that the log lacks; a log is created over an existing file; a
  restart reports a run as ended on the strength of a closure its history replayed; or a resume
  names anything but a stored run's id.

A run's log is `<dir>/<id>.jsonl`, created exclusively before the run starts. Its first line is a
`SERVE` row: the id, the offer, and for a resumed run the earlier id and the count of rows replayed.
The rows follow, each appended before any subscriber gets it. When the task ends, the service
appends an `END` row: the outcome and output, or that the run aborted.

A start that names an earlier run replays that log's entries as `history`
([[ADR-0018-chat-in-a-box#^c6|ADR-0018/C6]]). After a restart the logs are listed as ended runs by
their `END` row, or as cut short without one. Without a directory a resume is refused.

### C8: A log that cannot be written stops its run and says so _(enforced: mechanical; owed, S1)_ ^c8

- **Subject**: a log file that cannot be created, and an append that fails.
- **Violated when**: a row past a failed append is published; the run goes on to another round; the
  failure is absent from the state; the failure changes the outcome; or a run starts whose log could
  not be created.

The bus records a subscriber's error and goes on ([[ADR-0005-command-handling#^c2|ADR-0005/C2]]), so
the service catches its own write failure. The run is stopped (C6), its state names the failure, and
no row past it is published. A start whose log cannot be created is refused before any run exists.

### C9: The transport answers this machine's loopback only _(enforced: mechanical; owed, S1)_ ^c9

- **Subject**: every connection to the HTTP transport.
- **Violated when**: a bind off loopback succeeds; a peer off loopback is answered; a request with a
  Host other than the bound address and port, or with another Origin, is answered on any route; or a
  response carries a CORS header.

The transport binds a literal loopback address and refuses any other. Before it dispatches, it
checks the peer, then that Host is the bound address or `localhost` with the bound port, then that
an Origin, when present, is this server's. A POST must be `application/json`. Responses carry
`nosniff` and `no-store`.

This admits any process on the machine that can open the port. There is no token, and the checks
stop a web page while identifying no client (S3).

### C10: One request per connection, in one strict form _(enforced: mechanical; owed, S1)_ ^c10

- **Subject**: every request the transport reads.
- **Violated when**: a request is dispatched with two Host or two Content-Length headers, a
  Transfer-Encoding, an Expect, a target not in origin form or a version other than HTTP/1.1. Also
  when one is dispatched with a head over 16 KiB, a body over 1 MiB or a body that is not one JSON
  object, or when a slow sender holds a connection past the deadline.

The transport reads one request per connection and closes it. A POST carries exactly one decimal
Content-Length, and a GET carries no body. Anything outside this form is answered with its error and
never dispatched. One deadline of 10 seconds covers the whole request, however its bytes arrive.

A plain response carries its length, and a stream ends when the connection closes. Connections and
subscribers are capped, and one past the cap is refused.

### C11: Ending the service ends everything it started _(enforced: mechanical; owed, S1)_ ^c11

- **Subject**: `serve` returning, by cancellation or by an error.
- **Violated when**: after it returns a request is answered; a connection, stream or ping task is
  alive; a served run is live; a bus subscription of the service remains; or a log lacks a row that
  was published.

Admission closes first: the listener stops and requests already read are refused. Connection, stream
and ping tasks are cancelled, each write bounded. Live runs are stopped together (C6) and awaited
under one grace deadline, then cancelled and awaited. Each log gets its `END` row, and the bus
subscriptions are removed last.

Cancellation is cooperative. A handler or a store that will not yield holds the return, and the
service does not kill it (S6).

### C12: The core gains no dependency and imports no provider _(enforced: mechanical; owed, S1)_ ^c12

- **Subject**: `lionagi/serve.py` and the package's dependency list.
- **Violated when**: the module imports anything but the standard library, pydantic with its core
  and `lionagi`, or the dependency list changes.

### C13: The card tells a client what the model is told, and nothing of the prompt _(enforced: mechanical; owed, S1)_ ^c13

- **Subject**: the card.
- **Violated when**: it lists a command the offer's profile lacks, a schema differs from the Spec's
  own JSON schema, a bound differs from what a run of that offer resolves, or it carries the role
  prompt or a credential.

Per offer the card holds the profile's name and language, each command of the profile's subset
([[ADR-0001-the-actor#^c5|ADR-0001/C5]]) with its doc, JSON schema, effect class and required
privileges, each output's name and schema, the resolved bounds, and whether its runs are
conversations. It names the actor and protocol version 1.

## Decisions

### D1: `lionagi/serve.py` holds `Service` and `Offer`; `entry_row` moves beside `entry_of` ^d1

Serves C1, C12 and C13. `Service(actor, *, dir=None, max_live=8, keep=100)`; `keep` counts ended
runs held in memory. `Service.offer(profile, backend, request=RunRequest(), *, converse=False)`
refuses a template that sets `wait` and a name already offered. `Service.card()` builds the card
from `Actor.subset` and `model_json_schema`. `entry_row` moves from `hub/chat.py` to
`lionagi/checkpoint.py`, and the hub imports it. Values become JSON through
`pydantic_core.to_jsonable_python`; one it cannot project is carried as its `repr`, marked as such.

- **Landing evidence**: owed: `tests/test_serve.py` for the card against the model's own guidance,
  the import boundary and the dependency list.

### D2: `Service.start` reserves an id and a log, then reads the run from the bus ^d2

Serves C1, C2 and C8. `start(offer, inputs, bounds=None, resume=None, once=None)` checks the data,
draws a 16-hex id that no live run and no log has, and creates the log. A wrapper coroutine
subscribes to `run.started` and calls `Actor.run` in one step; the callback removes the subscription
(A1). The runtime's own id rides the state as `run`. A repeated `once` key returns the first run.

- **Landing evidence**: owed: the parity test of C2, a refusal test per field of C1, two starts in
  one loop turn, and a start over an existing log.

### D3: One publisher per run, called at the publishing points ^d3

Serves C3, C7 and C8. `_publish` takes the record's rows past the run's cursor, appends each to the
log, advances the cursor after the whole line is written, then hands the rows to each subscriber's
queue of 1,000 with `put_nowait`. Subscriptions on `run.started`, `round.started`, `command.` and
`run.completed` call it, as do the service's `wait` and the task's end. A write that raises is
caught there: the cursor stays, the state takes the failure and the run is stopped.

- **Landing evidence**: owed: a first round with a recall and a fold, compared row by row across
  record, log, stream and a resumed view; order and once-only; an overflow; a failing write.

### D4: The reads: `state`, `record` and `events` ^d4

Serves C4. `state(id)`; `record(id, start=0)`, the rows from a sequence number, at most 500 a call;
and `events(id=None)`, an async iterator whose first item is the state, or the list of states when
it follows every run. An ended run's iterator ends after that item. The service's `wait` emits
`run.waiting` on the bus.

- **Landing evidence**: owed: a run that ends before its stream opens, a subscription racing the
  end, an abort, and a second subscription after an overflow.

### D5: `say`, `end`, `interrupt`, `stop` and `cancel` ^d5

Serves C5 and C6. `say(id, text, once=None)` appends an `Inbound` with a fresh handle and the origin
`client` to `run.inbox`, sets the run's wake event and returns the handle. The service's `wait`
answers true when the inbox holds input, false after `end` or `stop`, and otherwise sleeps on the
wake event. `stop(id)` sets the flag `Actor.run` reads as `stop`, sets the wake event and calls
`Run.interrupt`. `cancel(id)` cancels the task and awaits it.

- **Landing evidence**: owed: stop while waiting, mid-call, during a command and after an accepted
  output; cancel before the commit and during it; input mid-round, before a wait and at the end.

### D6: The log's `SERVE` and `END` rows ^d6

Serves C7. The `SERVE` row holds `kind`, `version`, `id`, `offer`, `resumed` and `replayed`. The
`END` row holds `kind`, `state`, `outcome`, `output` and `rows`. `entry_of` returns none for both,
so a log read as history holds entries only. A resume id is 16 hex characters and names a file in
the directory. A last line that is not JSON is a write that was cut, and is dropped. The listing
after a restart reads each log's first and last line.

- **Landing evidence**: owed: a resumed run that aborts after an ancestor that succeeded, a cut
  `END` row, and a resume id that is a path.

### D7: The HTTP transport on `asyncio.start_server` ^d7

Serves C9 and C10. `Service.serve(host="127.0.0.1", port=8710, grace=10.0)` runs until cancelled and
reports the address it bound; a port of 0 takes a free one. The head is read to its blank line under
the cap and its header lines go through `http.client.parse_headers`.

| method and path | does | answers |
| --------------- | ---- | ------- |
| `GET /` | the card | 200 |
| `GET /runs` | the states of live and kept runs | 200 |
| `POST /runs` | starts a run from `offer`, `inputs`, and optionally `bounds`, `resume`, `once` | 201 with the state; 400, 404, 409 or 429 |
| `GET /runs/{id}` | the state | 200; 404 |
| `GET /runs/{id}/record?start=N` | the rows from N | 200; 404 |
| `GET /runs/{id}/events` and `GET /events` | the stream of one run, or of every run | 200 as `text/event-stream` |
| `POST /runs/{id}/input` | queues `text`, optionally with `once` | 202 with the handle; 409 for an ended run |
| `POST /runs/{id}/end`, `/interrupt`, `/stop`, `/cancel` | the control | 202 with the state; 409 |

- **Landing evidence**: owed: each refusal of C9 and C10 over real loopback sockets, with raw
  requests sent whole and in pieces.

### D8: Frames and errors ^d8

Serves C4 and C10. An error is a JSON object with `error`, a stable code, and `reason`, a sentence,
under its status. A stream frame is one server-sent event: `state` carries the state; `rows` carries
the run's id and its new rows; `bus` carries the run's id, the event's kind and its payload. A
comment line goes out every 15 seconds. A change that breaks a client raises the card's protocol
version.

- **Landing evidence**: owed: the example of D10, written against D7 and D8 alone.

### D9: `Actor.serve`, and the order of shutdown ^d9

Serves C11. `Actor.serve(profile, backend, **options)` is a service with one offer, served. In
order: close the listener; refuse requests already read; cancel connection, stream and ping tasks,
each write bounded by 5 seconds; stop every live run and await them under `grace`; cancel what
remains and await it; append the `END` rows; remove the bus subscriptions. At most 64 connections
and 32 subscribers are open at once.

- **Landing evidence**: owed: a shutdown with an idle stream, a stalled writer, a half-read request
  and a run mid-command, which finds no task, socket or subscription left.

### D10: `examples/serve_an_actor.py` and its test ^d10

Serves C2 and C4. The example serves the structured-output actor on a scripted backend at a free
port, then acts as its own client over the socket. It reads the card, starts a run, opens the stream
and finishes on the first state that says ended, whether that is the stream's first frame or a later
one. `tests/test_examples.py` runs it offline.

- **Landing evidence**: owed: the example in the scripted set, and its output pinned.

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
| Calling a handler with no run | a `Context` belongs to a run, and the gates, receipts and bounds are the run's ([[ADR-0005-command-handling#^c3\|ADR-0005/C3]]) |
| A WebSocket | it needs a framing layer or a dependency; a stream down and POSTs up carry everything here |

## Consequences

- **S1**: Nothing is built. D1 to D10 land with the tests they name, and until then every claim is
  owed here.
- **S2**: The areas server and the terminal chat are unchanged. Two servers stand until a client
  moves; a chat's box and its patch behind an offer are not decided here.
- **S3**: Loopback is per machine: any process of any account on the machine that can open the port
  is a client. A token per launch is the next step where that matters, and it does not wait for a
  remote bind; a remote bind and TLS are a later record.
- **S4**: Inputs are text. An input with an image waits for the record to carry one.
- **S5**: An actor's shared inbox is still taken by whichever of its runs starts a round first
  ([[ADR-0010-delegation#^c1|ADR-0010/C1]]). The service never uses it; with several live runs the
  limit stands for a peer's `queue`.
- **S6**: A handler that blocks the loop stalls every run and every stream (A2), and a log append is
  a file write on the loop. `serve` returns only once every handler and store has yielded to its
  cancellation.
- **S7**: A tool-protocol transport over the same service, and a `lion serve` command, are later
  changes; the command amends [[ADR-0016-the-lion-command|ADR-0016]].
- **S8**: A restart loses live runs: their logs remain and a resume is the client's ask (C7). The
  `once` keys live in memory, so a key replayed after a restart acts again.
- **S9**: The boundary: who may start what, a durable store of runs and supervision across restarts
  are the kernel's. Here an offer is the grant and a file is the store.
- **S10**: The transport is under the core's coverage gate, so its tests open real loopback sockets.
- **S11**: Two live runs of one actor can draw the same 8-hex runtime id, and the later replaces the
  earlier in `Actor.runs`. The service's own ids do not collide; the runtime's limit stands until a
  run draws its id against the live set.
- **S12**: A client's lowered bound is where the run starts. An offer whose `extend` grants more
  lets the run rise past it, and an owner who wants a ceiling offers an `extend` that refuses.
- **S13**: One service owns a directory. A second service over the same directory cannot write into
  a log it did not create, and lists the first one's live runs as cut short.
- **S14**: A row is written and flushed, never synced. A power loss may take the log's tail, which
  then reads as a write that was cut.
- **S15**: C6 rests on a correction to the loop that lands before the service: a cancel of the run's
  task that arrives with an interrupt was read as the interrupt and lost.
