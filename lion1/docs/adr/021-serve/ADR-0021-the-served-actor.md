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
`lionagi/bus.py`, `lionagi/checkpoint.py` (`entry_of`), `hub/chat.py` (`chat`, `entry_row`),
`apps/cli/lion_cli/areas.py` (`guarded`).

## Definitions

- **service**: the object that holds one actor's offers and its served runs, with the operations a
  client may ask of them; it knows no HTTP.
- **offer**: a profile, a backend and a request template the service will run, named by the
  profile's name; a served run starts from an offer and from nothing else.
- **card**: the service's description of itself as JSON: the actor's name, the protocol version and
  each offer with its commands, their schemas, its outputs and its resolved bounds.
- **served run**: a run the service started, with its task, its state and its subscribers.
- **transport**: code that carries the service's operations over a wire; this record has one, HTTP
  with a server-sent event stream.
- **client**: any program that speaks to a transport.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | `Actor.run` emits `run.started` before its first await, so a subscriber set in the same step as the call sees that run's id first. | `run` builds the run, replays history, appends the inputs and emits with no await between | the run takes its id as an argument, a change to the loop, and D2 is rewritten |
| A2 | One event loop carries the actor, its runs and the transport, and no handler holds it for long. | the bus and the actor are single-loop; the hub's backends call out on threads | one slow handler stalls every stream; the transport moves to a thread and bridges to the loop |
| A3 | An HTTP/1.1 reader limited to a request line, headers and a counted body is small enough to test whole. | the routes need no chunked request, no kept connection and no TLS on loopback | the transport is rebuilt on the standard library's threaded server; the service is unchanged |
| A4 | A local client needs events from now plus one read of the record, and no replay of missed events. | every row carries its sequence number, so a client that subscribes and then reads keeps each row once | the stream gains event ids and a bounded replay |

## Claims

### C1: A client starts only what the owner offered _(enforced: mechanical; owed, S1)_ ^c1

- **Subject**: every run a client starts.
- **Violated when**: a start names a profile not offered, carries a prompt, a class or a callable,
  raises a bound above the offer's, or sets `wait`, `accept`, `extend`, `emits` or `history`.

A start names an offer and carries data only: the inputs and, optionally, bounds. The run's request
is the offer's template with those inputs. A client bound replaces the template's only at or below
the value the template resolves to ([[ADR-0003-the-bounds#^c1|ADR-0003/C1]]); a bound that resolves
to none takes any value. Anything else is refused whole with its reason, and no run starts. A start
past the service's count of live runs is refused the same way.

### C2: A served run is the run `Actor.run` makes _(enforced: mechanical; owed, S1)_ ^c2

- **Subject**: every served run's record, outcome and events.
- **Violated when**: one offer, the same inputs and a scripted backend give a different record
  through the service than through `Actor.run`, or the service appends to a record, changes an
  outcome or holds a handler.

The service calls `Actor.run` once per start and awaits it as a task on the actor's loop. It reads
the run and writes two things on it: inbound input (C4) and the interrupt (C5). An error before
`run.started` is a refusal with no run; after it the run exists and its state says how it ended
([[ADR-0002-the-run#^c4|ADR-0002/C4]]).

### C3: A client sees a run through a state, its rows and one stream, and they agree _(enforced: mechanical; owed, S1)_ ^c3

- **Subject**: the state, the record read and the event stream of every served run.
- **Violated when**: a row reaches a stream before the record holds it, arrives twice or out of
  order on one stream, a bus event is rewritten, or a slow client delays the run.

The state is the run's id, offer, round and spend, whether it is working, waiting or ended, and at
its end the outcome and the output as JSON. The record read returns rows in the chat log's form from
a given sequence number. The stream carries each bus event as emitted and each new row once, in
order, flushed at every event of that run and at its end.

Each subscriber has a bounded queue; one that overflows is closed, never waited for. A client that
subscribes and then reads the record misses nothing (A4).

### C4: Input reaches the run it names, at that run's next round _(enforced: mechanical; owed, S1)_ ^c4

- **Subject**: every input a client sends to a live served run.
- **Violated when**: an input lands on another run, on the actor's shared inbox or on an ended run,
  or lands twice for one `once` token.

The service appends an inbound to the named run's own inbox, and the run takes it at its next round
as INPUT carrying the client's origin ([[ADR-0010-delegation#^c1|ADR-0010/C1]]). An offer marked as
a conversation gives its runs a `wait`: after a reply the run waits for input, and the client's end
makes its outcome `Stopped`. A repeated `once` token returns the first answer and does nothing
again. An input its run ended without taking is named in that run's state.

### C5: Interrupt, stop and cancel are the runtime's own three endings _(enforced: mechanical; owed, S1)_ ^c5

- **Subject**: the three controls on a live served run.
- **Violated when**: an interrupt ends a run, a stop leaves a run without its closure, or a cancel
  reports an outcome.

Interrupt is `Run.interrupt`: a call in flight is cut and that round ends with no turn. Stop cuts a
call in flight and ends the run at its next round boundary; the outcome is `Stopped` and the closure
is written. Cancel cancels the task: an abort, which has no outcome
([[ADR-0002-the-run#^c4|ADR-0002/C4]]).

### C6: With a directory, a run's log holds its rows, and a run resumes only from a log _(enforced: mechanical; owed, S1)_ ^c6

- **Subject**: a service started with a directory.
- **Violated when**: a row is streamed before it is in the log, the log lacks a row the record held
  at the run's end, or a resumed run's history differs from the log it named.

Each row is appended to `<dir>/<run id>.jsonl` before any subscriber gets it. A start that names an
earlier run replays that log as `history`, as a resumed chat does
([[ADR-0018-chat-in-a-box#^c6|ADR-0018/C6]]). After a restart the directory's logs are listed as
ended runs: with the outcome when the log holds a closure, else as cut short. Without a directory
nothing outlives the process and a resume is refused.

### C7: The transport answers loopback only _(enforced: mechanical; owed, S1)_ ^c7

- **Subject**: every connection to the HTTP transport.
- **Violated when**: a bind to another address succeeds, a peer off loopback is answered, a request
  whose Host is not this server's is answered, a POST with another Origin acts, or a body over the
  limit is read.

The transport binds a loopback address and refuses any other. Before a body is read it checks the
peer, then the Host, then on a POST the Origin and a JSON content type: the guards of the areas
server ([[ADR-0017-the-desk#^c11|ADR-0017/C11]]). A request's head is capped at 16 KiB, its body at
1 MiB, each read at 10 seconds. There is no token (S3).

### C8: Ending the service ends everything it started _(enforced: mechanical; owed, S1)_ ^c8

- **Subject**: `serve` returning, by cancellation or by an error.
- **Violated when**: after it returns a request is answered, a served run is live, a subscriber's
  task is alive, a bus subscription of the service remains, or a log lacks a row its record held.

On its way out the transport stops accepting and refuses requests already read. Each stream is
closed. Each live run is stopped (C5) and awaited for a grace period, then cancelled and awaited.
The service's bus subscriptions are removed last, after the final rows are flushed.

### C9: The core gains no dependency and imports no provider _(enforced: mechanical; owed, S1)_ ^c9

- **Subject**: `lionagi/serve.py` and the package's dependency list.
- **Violated when**: the module imports anything but the standard library, pydantic with its core
  and `lionagi`, or the dependency list changes.

### C10: The card tells a client what the model is told, and nothing of the prompt _(enforced: mechanical; owed, S1)_ ^c10

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

Serves C9 and C10. `Service(actor, *, dir=None, max_live=8, keep=100)` and `Service.offer(profile,
backend, request=RunRequest(), *, converse=False)`; an offer whose template sets `wait` is refused.
`Service.card()` builds the card from the actor's subset and `model_json_schema`. `entry_row` moves
from `hub/chat.py` to `lionagi/checkpoint.py`, and the hub imports it. Rows become JSON through
`pydantic_core.to_jsonable_python` with `repr` as the fallback.

- **Landing evidence**: owed: `tests/test_serve.py` for the card against the model's own guidance,
  the import boundary and the dependency list.

### D2: `Service.start` builds the request from the template and reads the id from the bus ^d2

Serves C1 and C2. `start(offer, inputs, bounds=None, resume=None)` checks the data, resolves the
template's bounds, and replaces only the inputs, the allowed bounds, the service's `wait` and the
replayed `history`. A wrapper coroutine subscribes to `run.started` and calls `Actor.run` in one
step (A1). `start` returns the served run once the id is known, or raises the refusal.

- **Landing evidence**: owed: the parity test of C2, a refusal test per field of C1, and a test that
  two starts in one loop turn get their own ids.

### D3: State, rows and events come from one flush per bus event ^d3

Serves C3 and C6. A subscription on the actor's bus appends the run's new rows to its log, then
hands rows and the event to each subscriber's queue of 1,000 with `put_nowait`. `state(id)`,
`record(id, after=0)` and `events(id=None)`, an async iterator, are the reads. The service's `wait`
flushes first and emits `run.waiting` on the bus.

- **Landing evidence**: owed: order and once-only tests on a stream, an overflow test where the run
  finishes unslowed, and the log-before-stream test.

### D4: `say`, `end`, `interrupt`, `stop` and `cancel` on the service ^d4

Serves C4 and C5. `say(id, text, once=None)` appends an `Inbound` with a fresh handle to `run.inbox`
and wakes a waiting run; the last 100 `once` answers are kept. `end(id)` makes the service's `wait`
return false. `stop(id)` sets the flag `Actor.run` reads as `stop`, then calls `Run.interrupt`.
`cancel(id)` cancels the task and awaits it.

- **Landing evidence**: owed: one test per control for the outcome and the closure, and an input
  test with two live runs.

### D5: The HTTP transport on `asyncio.start_server`, and `Actor.serve` ^d5

Serves C7 and C8. `Service.serve(host="127.0.0.1", port=8710, grace=10.0)` runs until cancelled.
`Actor.serve(profile, backend, **options)` is a service with one offer, served. The routes: `GET /`
the card; `GET /runs` and `POST /runs`; `GET /runs/{id}`, its `/record?after=` and `/events`; `GET
/events`; `POST /runs/{id}/input`, `/end`, `/interrupt`, `/stop` and `/cancel`. Every response
closes its connection; a stream pings every 15 seconds.

- **Landing evidence**: owed: guard tests per refusal of C7 over real loopback sockets, and a
  shutdown test that finds no task, socket or subscription left.

### D6: `examples/serve_an_actor.py` and its test ^d6

Serves C2 and C3. The example serves the structured-output actor on a scripted backend at a free
port, then acts as its own client over the socket: it reads the card, starts a run, follows the
stream to `run.completed` and prints the output. `tests/test_examples.py` runs it offline.

- **Landing evidence**: owed: the example in the scripted set, and its output pinned.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Extending the areas server to any actor | it reads residents' files from a registry, on a thread per request, inside the command-line package; a served run needs the actor's own loop and nothing of the registry |
| The standard library's threaded server with a bridge to the loop | every event crosses a thread and every stream holds one; kept as the fallback of A3 |
| A web framework | a dependency in the core, or `Actor.serve` absent without an extra |
| The transport in the hub | the core cannot import the hub, so `Actor.serve` could not exist in the core |
| A client that sends its own profile, prompt or tools | the actor would run what any local page or process hands it; the owner's offer is the gate (C1) |
| Replay of missed events by id | a second ordered store beside the record, which the record read already covers (A4) |
| Calling a handler with no run | a `Context` belongs to a run, and the gates, receipts and bounds are the run's ([[ADR-0005-command-handling#^c3\|ADR-0005/C3]]) |
| A WebSocket | it needs a framing layer or a dependency; a stream down and POSTs up carry everything here |

## Consequences

- **S1**: Nothing is built. D1 to D6 land with the tests they name, and until then every claim is
  owed here.
- **S2**: The areas server and the terminal chat are unchanged. Two servers stand until a client
  moves; a chat's box and its patch behind an offer are not decided here.
- **S3**: Loopback is per machine, not per account: another account on the same machine reaches the
  port. A token, a remote bind and TLS are one later record, and a shared machine is out of scope
  until it lands.
- **S4**: Inputs are text. An input with an image waits for the record to carry one.
- **S5**: An actor's shared inbox is still taken by whichever of its runs starts a round first
  ([[ADR-0010-delegation#^c1|ADR-0010/C1]]). The service never uses it; with several live runs the
  limit stands for a peer's `queue`.
- **S6**: A handler that blocks the loop stalls every run and every stream (A2), and a log append is
  a file write on the loop.
- **S7**: A tool-protocol transport over the same service, and a `lion serve` command, are later
  changes; the command amends [[ADR-0016-the-lion-command|ADR-0016]].
- **S8**: A restart loses live runs: their logs remain and a resume is the client's ask (C6). The
  `once` answers live in memory, so a token replayed after a restart acts again.
- **S9**: The boundary: who may start what, a durable store of runs and supervision across restarts
  are the kernel's. Here an offer is the grant and a file is the store.
- **S10**: The transport is under the core's coverage gate, so its tests open real loopback sockets.
