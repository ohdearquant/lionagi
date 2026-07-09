# ADR-0017: Session Membership and Coordination Boundary

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: session-branch
- **Date**: 2026-07-09
- **Relations**: none

## Context

`Session` is the coordination owner for a pile of branches and one default branch. It also owns a
shared operation registry, a lazily created observer and hook bus, a shared memory-store default, and
an `Exchange`. These resources give member branches a common coordination context without making
the session the owner of their messages, logs, or explicit memory backends
(`lionagi/session/session.py`).

Membership is exclusive. `include_branches()` preflights the whole batch, rejects a branch owned by
another session, then records ownership and attaches the shared operation registry, observer, hooks
when initialized, memory when absent, and an Exchange mailbox. `remove_branch()` reverses the
session-owned attachments and installs a fresh private operation registry while preserving branch
conversation data. Reparenting is therefore an explicit remove-then-include operation.

The session observer is the in-process event transport. Emission applies the optional gate, stores
the event in its `Flow`, adds matching routes, and then invokes matching subscribers; asynchronous
subscribers are awaited concurrently. HookBus uses the observer as an integration seam, while the
observer's current database-binding helper also contains StateDB-specific serialization and
best-effort persistence policy (`lionagi/session/observer.py`, `lionagi/hooks/bus.py`).

`Session.flow()` and `flow_stream()` resolve a branch and delegate graph execution to
`lionagi/operations/flow.py`. The executor registers any clones through `include_branches()` so they
receive the same coordination attachments. Session is therefore the membership and resource
boundary, not the DAG execution kernel (see the operations ADR on dependency-aware operation-graph
execution and the orchestration ADR on the operation-graph boundary).

`Exchange` is constructed for every session and exposes explicit send, receive, collect, and sync
operations. Routing does not run automatically and the Exchange is excluded from Session
serialization. The only adjacent messenger adapter requires explicit construction and binding, so
the default mailbox does not establish a durable or end-to-end interbranch messaging contract
(`lionagi/session/exchange.py`, `lionagi/tools/communication/messenger.py`).

## Decision

`Session` remains the exclusive membership and shared-coordination boundary for branches. Its
load-bearing invariants are:

- one branch belongs to at most one session at a time, batch inclusion is all-or-nothing with
  respect to ownership checks, and reparenting requires removal from the current owner;
- inclusion installs the session's observer, operation registry, optional hook bus, memory default,
  and Exchange registration; removal detaches ownership, observer, hooks, the shared operation
  registry, and the mailbox while leaving the branch's adopted memory and other conversation data
  intact;
- a branch-supplied memory store wins over the session default, while branches without one share the
  session's single store instance;
- `SessionObserver` is the canonical in-process gate, event store, router, and subscriber dispatcher;
  lifecycle calls using safe emission and the bound database subscriber treat observation and
  persistence failures as best-effort diagnostics;
- `Session.flow()` and `flow_stream()` are convenience delegates, and scheduling, dependency,
  branch-cloning, and reactive-mutation algorithms remain in the operation graph kernel; and
- the default `Exchange` is an explicitly pumped, non-serialized compatibility facility. Its
  presence does not promise automatic delivery, durability, recovery, or a provisioned Messenger.

```text
                         Session
              membership and shared resources
                  /          |          \
            Branches     Observer      Memory
               |          + Hooks       default
               |              |
               +---- shared operations ----+
               |
          flow()/flow_stream()
               |
               v
       operation graph execution kernel
```

## Consequences

Branch ownership, shared extensions, observation, and graph-created clone wiring have one lifecycle.
Standalone branches remain usable, and orchestration code can rely on Session membership without
moving the graph executor into the session package.

The Session surface exposes facilities with different maturity and durability. Observer history and
Exchange mailboxes are process-local, Exchange requires explicit pumping, and direct database
binding couples the observer to persistence policy. Consumers must not infer recovery guarantees
from in-memory coordination APIs.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Extract StateDB signal persistence from `SessionObserver` into a persistence-owned subscription adapter; acceptance requires the observer module to contain no StateDB construction or payload-size policy while CLI and Studio retain best-effort writes, payload bounds, and unbind behavior. | S | (filled at issue-open time) |
| 2 | Move versioned signal and loop-control vocabulary to a neutral low-level module with compatibility re-exports from `lionagi.session`; acceptance requires unchanged schema versions, serialized payloads, `lane_for()` results, dispatch envelopes, and public import behavior throughout the relocation. | M | (filled at issue-open time) |
| 3 | Choose and implement one Exchange product posture: provision Messenger, collection, durability, and recovery as an end-to-end supported path, or make Exchange opt-in and deprecate the default Session mailbox; acceptance requires documentation and integration tests that demonstrate the selected lifecycle. | M | (filled at issue-open time) |

## Notes

Moving graph execution into `Session` was rejected because it would combine membership lifecycle
with scheduling and reactive graph mutation. Treating Observer, HookBus, and Exchange as one
transport was rejected because they have different delivery, persistence, and invocation contracts.
