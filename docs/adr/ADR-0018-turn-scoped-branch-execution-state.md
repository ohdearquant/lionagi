# ADR-0018: Turn-Scoped Branch Execution State

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: session-branch
- **Date**: 2026-07-09
- **Relations**: extends ADR-0016

## Context

Branch currently stores provider injection blocks, the latest provider report, one loop-control
directive, and pending signal tasks in branch-global mutable attributes. Chat and run populate and
clear the same provider slot, control polling consumes the same directive, and signal draining takes
the whole branch task list. The CLI run path also installs a per-call model override as the branch's
configured chat model (`lionagi/session/branch.py`, `lionagi/operations/chat/_prepare.py`,
`lionagi/operations/chat/chat.py`, `lionagi/operations/run/run.py`).

Those fields describe an invocation, not a conversation. Top-level operations are asynchronous and
the Branch API does not reject overlap, so two turns on one branch can overwrite or clear each
other's provider state, consume the wrong control, drain unrelated signals, or retain a temporary
model. The latest provider report also lacks a stable turn identity. This is separate from the
immutable provider request proposed for compilation (see the messages-context ADR on canonical
turn-request compilation): a compiled request is model input, while execution state owns lifecycle
and runtime resources.

Lifecycle ownership is also distributed. Ordinary recorded operations use `Branch._observed_run()`,
`ReAct()` repeats start and terminal emission while suppressing nested signals through a task-local
flag, and the CLI async generator owns its own abandonment and finalization path. Exact-once
terminal behavior is required, but adding another execution path currently requires reproducing
that policy (`lionagi/session/branch.py`, `lionagi/session/_lifecycle_ctx.py`,
`lionagi/operations/run/run.py`). Adapter and transport responsibilities remain separately governed
(see the operations ADR on the Branch operation facade and turn adapters).

## Decision

Every top-level model turn will create one internal, typed `TurnExecutionContext`. The minimum
contract is:

```python
@dataclass(slots=True)
class TurnExecutionContext:
    turn_id: UUID
    model: iModel
    provider_blocks: tuple[str, ...]
    provider_report: ProviderReport | None
    control: LoopControl | None
    signal_tasks: set[asyncio.Task[Any]]


@dataclass(frozen=True, slots=True)
class TurnScope:
    context: TurnExecutionContext
    owns_lifecycle: bool
```

The context is runtime state and is never serialized as conversation history. The target type lives
with operation parameter contracts in `lionagi/operations/types.py`; one internal lifecycle driver
in the operation layer owns its entry, completion, failure, stream-close, and cleanup behavior.

The load-bearing invariants are:

- top-level model turns on the same Branch are serialized for their full lifetime, including the
  lifetime of a returned async generator; turns on distinct branches remain concurrent;
- nested operations receive a new `TurnScope` over the active context with
  `owns_lifecycle=False`, so only the top-level scope emits start and terminal events and releases
  the branch turn lock;
- a per-call model is resolved into the context and never mutates the branch's configured model;
- provider blocks, provider report, control, and signal tasks are read and cleared only through the
  active context; no turn may observe or drain another turn's runtime state;
- every started turn receives a stable `turn_id`, and its start and exactly one end-or-failed event
  carry that identity; `turn_id` supplements rather than replaces graph-node `op_id`, and stream
  abandonment and cancellation are terminal paths rather than silent leaks;
- pending message signals for the turn are drained before its terminal event, and observer failures
  remain best-effort diagnostics that do not change the operation result; and
- lock acquisition cancellation emits no start event, while any failure after start releases the
  lock and emits exactly one failed terminal event.

## Consequences

Provider attribution, controls, model overrides, signal draining, and lifecycle events become
unambiguous per turn. The single driver gives ordinary calls, reasoning loops, and streams the same
failure and cleanup contract, and conversation-level objects no longer carry transient execution
slots.

One Branch deliberately gives up overlapping top-level model turns. Work that requires parallel
model calls must use separate branches or graph-created clones, which matches the existing
conversation-isolation model but can reduce throughput for callers that previously overlapped calls
on one branch. Migration must preserve async-generator close behavior and coordinate the new turn
identity with signal consumers and persistence adapters.

## Notes

Fully concurrent recorded turns on one Branch were rejected because isolated temporary fields would
not define deterministic history snapshots or append order. A branch-global lock without an
explicit context was rejected because nested ownership, model overrides, signal attribution, and
stream cleanup would remain implicit. The context is intentionally separate from `TurnRequest`:
one governs execution lifetime, the other governs compiled provider input.
