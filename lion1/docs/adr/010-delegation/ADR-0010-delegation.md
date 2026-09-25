---
adr: ADR-0010
status: draft
liveness: operating (the round trip, a peer ending without answering, a peer whose backend raises and an answer landing early are pinned by tests; no bench exercises the path)
date: "2026-09-25"
area: delegation
kind: new
depends_on:
  - "[[ADR-0001-the-actor|ADR-0001]]"
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0005-command-handling|ADR-0005]]"
  - "[[ADR-0006-the-notification|ADR-0006]]"
tags:
  - adr
  - runtime
---

# ADR-0010: Delegation between actors

## Context

Work that one actor cannot settle from what it holds goes to another. Earlier designs put a graph, a
team or a cast object between them, each with its own scheduler. Here a handler hands the work to a
peer, the peer runs it as a run of its own, and the peer's output settles the command that asked,
under the alias the model chose. This record fixes how the work moves, how the answer comes back,
what the asker hears when it never does, and where the process ends.

The long-running agent's mail is the other layer, and the kernel below both owns who may ask whom.
The actor holds the inbox ([[ADR-0001-the-actor|ADR-0001]]); the asking command settles by the
turn's rule ([[ADR-0002-the-run#^c1|ADR-0002/C1]]) with nothing awaited inline
([[ADR-0005-command-handling#^c2|ADR-0005/C2]]); the notification names what arrived
([[ADR-0006-the-notification#^c2|ADR-0006/C2]]). Source: `Actor.queue`, `Actor._take_inbox`, the
handle branch of dispatch, `Actor._deliver` and the `_ACTORS` registry in `lionagi/actor.py`;
`examples/two_actors.py`.

## Definitions

- **peer**: another actor in the same process, asked from inside a handler; it runs the item as a
  run of its own.
- **handle**: the id `queue` returns for one item; the key under which the asking command finds the
  answer.
- **inbound**: an item on an inbox: the handle, the content, the origin actor, its run and alias,
  and whether it is a reply.
- **ask**: the entry the asking run keeps per handle: the command's alias and a future for the
  answer.
- **answer**: the output the peer's run ended with, made plain, settling the asking command under
  its alias.
- **reply**: an inbound carrying an answer to a run that has ended, placed on the origin actor's
  inbox.
- **registry**: the module-level map from actor name to actor; how an answer finds its asker.
- **mail**: a message from outside the process; the long-running agent takes a wake's mail as one
  inbound from "the inbox".

## Assumptions

| #  | statement                                                                 | source                                                                             | if false                                                   |
| -- | ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| A1 | Every actor an answer must reach lives in the same process.               | the registry is a module dict; nothing beyond that (assumption)                    | C2's answer path needs an address, not a name (S5)         |
| A2 | Waiting for an answer is the same wait as waiting for a slow file read.   | the asking command is one dispatched task; the turn gathers it (ADR-0002/C1)       | the asking run needs its own wake rule                     |
| A3 | The peer's whole output is what the asker wants, not the peer's record.   | `_deliver` hands over `run.output` made plain; the example's answer is one Finding | an answer needs a projection, chosen by the asker          |

## Claims

### C1: `queue` returns at once and lands as INPUT with provenance _(enforced: mechanical)_ ^c1

- **Subject**: every call to `Actor.queue`.
- **Violated when**: `queue` blocks on the peer or starts its run, or the INPUT carries no handle or
  no origin.

`peer.queue(content, context)` appends an inbound to the peer's inbox, records the ask on the asking
run under a fresh handle, emits `inbox.queued` and returns the handle. The peer's next turn, or a
run already going, takes its inbox before the notification: each item becomes one INPUT entry whose
`meta` holds the handle, the origin actor and the reply flag, and moves to the run's consumed list.

The notification lists it under `inbound:` as a request or an answer, with origin and handle.
Running the peer is not `queue`'s business: the handler that asked starts the peer's run as its own
task, or something outside the loop does.

### C2: A handler that returns its handle is settled by the answer, or by the peer's end _(enforced: mechanical)_ ^c2

- **Subject**: every command whose handler returns a `Handle` on the run's ask table.
- **Violated when**: the alias resolves to the handle, a peer run ending without `Success` or by
  raising leaves the ask open, or an answer landing before the handler returns is lost.

The command's value is the peer run's output made plain, under the alias the model chose; the next
turn starts when it is in, by the settle rule. A peer that ends `Exhausted`, `Stopped` or `Refused`
settles the command failed as "<peer> ended <outcome> without answering". One whose backend raises
settles it "ended in error (<error>) without answering" before the exception leaves the peer's run:
the asker never waits on a dead run.

The ask stays on the table until the command reads it, so an answer landing while the handler still
runs is found; the command drops it once read. An ask still open when the asking run ends is
cancelled.

### C3: An answer to a run that has ended is a reply on the origin's inbox _(enforced: mechanical)_ ^c3

- **Subject**: every delivery at a peer run's end.
- **Violated when**: a `Success` whose asker has ended is dropped, a failed peer writes to an
  origin's inbox, or a reply is delivered twice.

At its end, `Success` or not, a run delivers once for every inbound it consumed that is not itself a
reply. The origin is found by name, its run by id. A live run with the ask open takes the answer or
the failure.

An origin whose run has ended, or whose command already read its ask, gets a reply on its inbox from
a peer that ended `Success`; its next run takes it as INPUT and delivers nothing back for it. A
failed peer sends nothing to an origin that is not waiting. Every delivery emits `inbox.answered`.

### C4: No graph object; peers are one layer, mail is another _(enforced: process)_ ^c4

A group of actors is a set of handlers that queue to each other; no scheduler stands above the
actors, and delegation is one round trip on two records. Nothing in this record reaches another
process.

The long-running agent (ADR-0013, not yet written) is the other layer. Mail arrives on its run's
inbox as one inbound from "the inbox"; an answer goes out as a `send` command that requires the
`comm.send` privilege; every send goes through `Agent.deliver`, which charges the thread's hop count
before the transport. The gate is code review of anything that proposes a scheduler above the
actors, or a second send path.

## Decisions

### D1: One in-process registry so an answer finds its asker ^d1

Serves C2 and C3. `_ACTORS` maps names to actors; each actor keeps its live runs by id. `_deliver`
runs when a run ends by an outcome, and when an exception ends it, before the re-raise; it resolves
the ask's future, or falls back to the origin's inbox.

- **Landing evidence**: four tests in `tests/test_actor.py`: the answer settles the ask with the
  peer's plain output; a peer ending `Exhausted` settles it failed naming the outcome; a backend
  that raises settles it failed naming the error; an early answer still settles it.

### D2: The handler starts the peer; the runtime does not ^d2

Serves C1. `queue` carries content and provenance only; the peer's profile, request and backend are
the asking handler's to choose, and it starts the peer's run as its own task. For mail, the
long-running agent is the something outside the loop that runs the actor.

- **Landing evidence**: `examples/two_actors.py` (the escalation handler queues, starts the
  researcher's run and returns the handle); the tests' `_pair` helper does the same, and the
  round-trip test asserts the peer's INPUT names the asker.

## Alternatives

| approach                                    | rejected because                                                                                                       |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| A DAG, team or cast object                  | a second scheduler beside the turn loop; handlers already compose                                                      |
| Awaiting the peer's run inside the handler  | the runtime could not tell an ask from a slow read: no `inbound:` line, no provenance on either record, no reply once the asker has ended |
| The runtime starting the peer on `queue`    | `queue` would need the peer's profile, request and backend; the handler has them and decides them (D2)               |
| An address per actor instead of a name      | an address names a principal across processes; that is the kernel's, with the lineage that comes with it (S5)         |
| Copying the asker's bounds into the peer    | a copy conserves no shared budget; the peer's bounds are the handler's choice, and a shared cap is below the boundary |

## Consequences

- **S1**: Delegation costs the model one command and one turn; the answer is in view the turn after
  it lands.
- **S2**: The registry is per process, keyed by name: a second actor made with the same name takes
  the name, and answers to the first go to it.
- **S3**: No bench exercises this path; the tests drive it with scripted backends, and
  `examples/two_actors.py` runs it against a live model.
- **S4**: No budget is shared across peers, and a peer run started as its own task outlives an asker
  that ends: cancellation at the asking run's end reaches the ask's future, never the peer's run.
- **S5**: The boundary: who may ask whom, the lineage of an ask, ending a peer with the asker,
  authority that narrows as it passes down, and delivery across processes are not decided here.
  In-process peers are a test and bench convenience; the long-running agent's mail is the
  process-side form (ADR-0013, not yet written).
- **S6**: An answer whose origin actor is not in the registry is dropped with no event; a reply to
  an actor that never runs again waits on its inbox for the process's life.
- **S7**: The ask stays on the table until read only since 2026-09-20; before, a handler that
  yielded after starting the peer got its handle back as the value. The early-answer test pins it.
