# ADR-0003: In-Process Event Execution Lifecycle

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: core-data-model
- **Date**: 2026-07-09
- **Relations**: extends ADR-0001

## Context

API calls and executable operations need a shared in-process representation of pending work and its
outcome. `Event` provides mutable execution state containing status, duration, response, error, and
retryability. Its seven statuses are `pending`, `processing`, `completed`, `failed`, `skipped`,
`cancelled`, and `aborted`. `lionagi/protocols/generic/event.py` defines this lifecycle.

Invocation begins only from `pending`, changes the event to `processing`, and then records
`completed`, `failed`, or `cancelled` according to the outcome. Streaming uses the same result
contract. The terminal set is `completed`, `failed`, `skipped`, `cancelled`, and `aborted`; assigning
one of those states signals a lazily created process-local completion event. Execution state can be
serialized for observation, but `Event.from_dict()` deliberately rejects reconstruction.

`Processor` and `Executor` in `lionagi/protocols/generic/processor.py` are lightweight runtime
facilities around this lifecycle. Processor queues events, applies permission and capacity checks,
and invokes them; permission denial is terminal `skipped` unless a processor explicitly defers it.
Executor stores live events in a Pile and their pending UUIDs in a Progression.

`APICalling` uses Event for endpoint work, and `iModel` waits on the process-local completion signal
before removing the call from its executor. `Operation` combines `Node` and `Event`, so executable
graph nodes use the same outcome vocabulary. The anchors are
`lionagi/service/connections/api_calling.py`, `lionagi/service/imodel.py`, and
`lionagi/operations/node.py`.

Durable dispatch is a different contract. The outbox persists delivery attempts, acknowledgements,
expiry, retries, and dead-letter outcomes with its own state machine. It does not rehydrate or drive
Event execution state (see the persistence-state ADR on durable dispatch lifecycle). Reactive flow
also exposes a smaller completion projection; its current mapping does not preserve every Event
terminal state.

## Decision

Event is the common in-process execution lifecycle for API calls and executable Operations. Its
load-bearing invariants are:

- execution begins in `pending`, active work is `processing`, and the complete seven-value status
  vocabulary remains available to runtime consumers;
- `completed`, `failed`, `skipped`, `cancelled`, and `aborted` are terminal and signal the local
  completion primitive;
- normal results populate `response`, ordinary exceptions produce `failed` with captured error
  state, and cancellation-class base exceptions produce `cancelled` and are re-raised;
- Event execution records may be serialized for observation but are not durable, rehydratable work
  items;
- Processor and Executor coordinate live Event instances in memory; and
- durable dispatch retains an independent delivery state machine rather than adopting EventStatus.

The defining implementation anchors are `lionagi/protocols/generic/event.py` and
`lionagi/protocols/generic/processor.py`.

## Consequences

API calls and graph operations share one outcome shape, completion signal, and processor model.
Failures and cancellations remain observable on the live object, while processors can apply
capacity, concurrency, and permission policy without embedding durable delivery semantics in the
generic event type.

Live Events cannot resume after process loss, and serialized Event state is diagnostic rather than
an executable record. Consumers that expose a smaller status vocabulary today must define a total
projection or risk reporting cancellation or abort as success. Systems needing retries,
acknowledgements, or dead-letter handling currently integrate with the durable dispatch contract instead.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Make the flow completion projection total over every terminal EventStatus; acceptance requires `cancelled` and `aborted` to remain non-success outcomes through status or reason fields, with tests for all five terminal states. | S | (filled at issue-open time) |
| 2 | Document the translation boundary between in-process Event execution and durable dispatch delivery; acceptance requires each integration point to identify ownership of retry, acknowledgement, expiry, and terminal-outcome mapping without merging the two state machines. | M | (filled at issue-open time) |

## Notes

Alternatives considered were making Event a durable workflow record and sharing one status enum with
the dispatch outbox. The first conflicts with process-local completion primitives and arbitrary
runtime response objects; the second would collapse execution outcomes and delivery guarantees that
have different transition and retry semantics.
