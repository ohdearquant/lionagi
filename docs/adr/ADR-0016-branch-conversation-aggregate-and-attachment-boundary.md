# ADR-0016: Branch Conversation Aggregate and Attachment Boundary

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: session-branch
- **Date**: 2026-07-09
- **Relations**: none

## Context

`Branch` is the stateful aggregate for one conversation. It constructs and exposes a
`MessageManager`, `ActionManager`, `iModelManager`, `DataLogger`, and `OperationManager`. The
message manager owns the message pile and active progression; the other managers own registered
tools, model selection, logs, and named-operation lookup rather than parallel copies of those
resources (`lionagi/session/branch.py`).

Conversation state and invocation state have different lifetimes. Messages, progression, tool and
model configuration, logs, memory, identity, and metadata belong to the branch. Context-provider
blocks, the latest provider report, pending loop control, and background signal tasks are currently
held on the same object even though they describe an invocation in progress. Provider behavior and
request compilation are specified separately (see the messages-context ADR on pre-turn context
providers and the messages-context ADR on canonical turn-request compilation).

The public verb methods are adapters over implementations in `lionagi/operations/`. `Branch` keeps
the stable caller surface and supplies the aggregate those implementations mutate; it is not a
second operation engine. Recording, streaming, parsing, action, and `Middle` semantics belong to the
operation layer (see the operations ADR on the Branch operation facade and turn adapters).

A standalone branch lazily creates private in-memory storage and a private operation registry. A
session may attach an observer and hook bus, replace the operation registry with its shared registry,
and supply shared memory when the branch has not already adopted a store. Capability grants remain
branch-local: granting replaces one marked system-prompt block, and observed assistant messages are
validated against that grant before structured-output or rejection signals are emitted
(`lionagi/session/capabilities.py`, `lionagi/operations/_observe.py`).

## Decision

`Branch` remains the aggregate and facade for exactly one durable conversation. Its load-bearing
invariants are:

- branch identity, message history and progression, registered tools, selected models, logs,
  metadata, and an adopted memory store have branch lifetime;
- the manager properties are the authoritative access paths to those resources; callers do not
  maintain shadow registries or message collections alongside them;
- context providers and capability grants are branch-scoped extensions, while provider results and
  lifecycle bookkeeping are invocation-scoped data rather than durable conversation records;
- an explicitly supplied or previously adopted memory store is retained when a branch joins a
  session; only a branch without a store adopts the session store;
- observer, hook, session-ownership, and shared-operation references are coordination attachments,
  not serialized conversation content, and removal from a session detaches them without deleting
  messages, memory, or logs; and
- public verb methods delegate to the operation layer. The Branch boundary owns state and API
  continuity, while operation modules own execution algorithms and transport semantics.

```text
                  Branch
       conversation state and identity
          /       |       |       \
   messages    actions   models    logs
          \       |       |       /
        memory, providers, capabilities
                       |
          session coordination attachments
       observer, hooks, shared operation registry
```

## Consequences

Operations receive one coherent object instead of a bundle of loosely related managers, and a
branch can operate alone or be attached to a session without changing its conversation identity.
Manager replacement and attachment remain internal implementation details, so callers retain one
public surface.

The aggregate has a broad dependency set and currently mixes durable conversation state with
invocation-local state. Private attributes are therefore not interchangeable: moving or serializing
one without classifying its lifetime can introduce cross-turn leakage or silently persist runtime
coordination objects.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Implement the turn-scoped execution context and same-Branch serialization contract from ADR-0018; acceptance requires two overlapping top-level turns to neither overwrite provider data, consume each other's control, drain each other's signal tasks, nor change the branch's configured model. | M | (filled at issue-open time) |
| 2 | Remove the deprecated no-op `system_template` and `system_template_context` constructor arguments under the public deprecation policy, and classify `Branch.connect()` as supported or deprecated after a repository-wide consumer inventory; acceptance requires updated API documentation and compatibility tests for the selected outcome. | S | (filled at issue-open time) |

## Notes

Splitting every manager into a separately passed operation dependency was rejected because the
managers jointly define one conversation's state. Treating session attachments as durable branch
state was rejected because a branch must be detachable and reparentable without carrying the prior
session's observer, hooks, or operation registry.
