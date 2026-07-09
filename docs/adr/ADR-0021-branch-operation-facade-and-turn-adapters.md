# ADR-0021: Branch operation façade and turn-adapter contract

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: operations
- **Date**: 2026-07-09
- **Relations**: none

## Context

`Branch` is the public verb surface for a conversation. It exposes model transport,
structured parsing, action execution, composed operation, interpretation, and reasoning-loop
methods while keeping most implementations in `lionagi/operations/`. The façade imports those
implementations inside each method, which lets operation modules depend on `Branch` only for type
checking and avoids a runtime import cycle (`lionagi/session/branch.py`,
`lionagi/operations/types.py`).

Named extensions are session-scoped. `Session` owns one `OperationManager`, shares it with every
included branch, and registers asynchronous callables into it. `Branch.get_operation()` resolves a
built-in branch method before consulting that registry, and `Operation` uses the same lookup when a
graph node is invoked (`lionagi/session/session.py`, `lionagi/operations/manager.py`,
`lionagi/operations/node.py`).

The model-facing verbs have distinct state contracts. `chat()` performs an API request and logs the
event without adding the instruction or response to branch messages. `communicate()` records an API
instruction and response and may then parse the response. `run()` is restricted to CLI endpoints;
it streams and records typed instruction, assistant, action-request, and action-response messages
while owning stream cleanup and lifecycle signals. `run_and_collect()` converts that stream back to
a single result shape (`lionagi/operations/chat/chat.py`,
`lionagi/operations/communicate/communicate.py`, `lionagi/operations/run/run.py`).

`Middle` is the structural substitution seam used by `operate()`. Its current docstring calls the
seam one assistant turn, but its implementations do not share that cardinality: CLI collection may
join several assistant messages, and the LNDL adapter may run several inner exchanges. The stable
as-built contract is one logical adapter invocation that owns whatever recorded exchange or bounded
exchange sequence it performs (`lionagi/operations/types.py`,
`lionagi/operations/lndl_middle/lndl_middle.py`).

## Decision

The operation layer retains `Branch` as its public façade, the session-owned asynchronous registry
as its named extension mechanism, and `Middle` as the typed adapter seam for a logical model
exchange.

```text
Session-owned registry ───────┐
                              v
Caller ──> Branch façade ──> built-in verb implementation
                    │
                    └── operate() ──> Middle adapter
                                      ├── communicate (API, recorded)
                                      ├── run_and_collect (CLI, streamed and recorded)
                                      └── bounded adapter (for example LNDL)
```

The load-bearing invariants are:

- Built-in `Branch` methods take precedence over session-registered names. Registered operations
  must be asynchronous and become visible to every branch included in the session.
- Operation implementations receive a fully formed branch; they do not own branch or session
  construction. Runtime imports from the façade remain lazy or type-only in the implementation
  direction.
- `chat()` is non-recording, while `communicate()`, `run()`, and any custom `Middle` own their message
  persistence. `operate()` does not append a duplicate instruction or response around a `Middle`.
- `run()` remains a public async-generator contract for CLI endpoints. API one-shot calls and CLI
  stream lifecycle are not forced into one transport primitive.
- A `Middle` accepts `branch`, `instruction`, `ChatParam`, optional `ParseParam`, `clear_messages`, and
  `skip_validation`, and returns the logical operation result. The protocol does not guarantee one
  provider message or prohibit a bounded internal loop.

## Consequences

Library callers get one discoverable branch surface, while graph nodes and custom session operations
reuse the same dispatch path. Transport implementations retain the cleanup and persistence behavior
their endpoint family requires, and custom adapters can replace transport-plus-turn behavior without
replacing the branch.

The façade is broader than a conventional thin interface, and lifecycle ownership is not uniform:
`operate()` and `communicate()` use the branch wrapper, `run()` emits its own lifecycle, and `ReAct()`
coordinates nested lifecycle suppression. The extension registry's location under `operations/`
also obscures its session-owned lifetime.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Replace the `Middle` one-turn wording with a logical-exchange contract that states message-recording, streaming, bounded-loop, native-tool, and validation responsibilities; add conformance tests for `communicate`, `run_and_collect`, and LNDL. | M | (filled at issue-open time) |
| 2 | Make the branch-operation vocabulary explicitly extensible or include every built-in verb, including `run`, and publish the intended registry and adapter exports from one canonical namespace. | S | (filled at issue-open time) |
| 3 | Document the session ownership of the named-operation registry in its type and public API, preserving built-in precedence and asynchronous registration checks. | S | (filled at issue-open time) |
| 4 | Represent lifecycle ownership for API turns, CLI streams, and nested operations in an explicit internal contract and add tests that prevent duplicate start or terminal signals. | M | (filled at issue-open time) |

## Notes

A single transport primitive was rejected because one-shot API calls and cancellation-sensitive CLI
streams have different cleanup and message-order obligations. Making every verb a `Middle` was also
rejected because parsing, action execution, and interpretation are not persisted model exchanges.
