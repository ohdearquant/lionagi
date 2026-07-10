# ADR-0018: Turn-scoped Branch execution state

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: session-branch
- **Date**: 2026-07-09
- **Relations**: extends ADR-0016

## Context

Branch currently stores several values that describe an invocation in progress rather than the
conversation itself:

```python
# lionagi/session/branch.py — current state, not the target
_loop_control: LoopControl | None = PrivateAttr(None)
_signal_tasks: list = PrivateAttr(default_factory=list)
_context_injection_slot: list[str] | None = PrivateAttr(None)
_last_context_report: Any = PrivateAttr(None)
```

The public async API neither rejects nor serializes overlapping calls. Five concrete problems follow.

**P1 — provider state can be overwritten across turns.** `chat` and CLI `run` gather providers into
the same `_context_injection_slot`, request preparation reads that slot, and a `finally` block clears
it (`lionagi/operations/chat/_prepare.py`, `lionagi/operations/chat/chat.py`,
`lionagi/operations/run/run.py`). Two overlapping calls can replace or clear each other's blocks.
`_last_context_report` also identifies only a branch-global latest report and the zero-provider path
can leave an older report visible.

**P2 — loop control has no turn identity.** `Branch.control()` overwrites one `_loop_control`, while
`poll_control()` consumes and clears it. A directive can therefore be consumed by whichever
overlapping stream reaches a poll point first, not necessarily the invocation that caused the
observer to issue it (`lionagi/session/control.py`, `lionagi/operations/_observe.py`).

**P3 — message-signal drainage is branch-global.** Every message callback appends an asyncio task to
one list. `drain_signals()` swaps and awaits the whole list. A terminal boundary can drain another
turn's emissions, and a later terminal can run before its own message signal if the earlier turn
took the task list.

**P4 — a CLI model override mutates durable configuration.** Current `run()` assigns
`branch.chat_model = param.imodel` before endpoint validation and never restores the prior model.
A one-call override can therefore become the model for later turns, including after a failing call
(`lionagi/operations/run/run.py`).

**P5 — lifecycle ownership is duplicated.** Ordinary recorded operations use
`Branch._observed_run()`. `ReAct()` repeats start, failure, end, timing, signal-drain, and observer
isolation while setting `suppress_lifecycle_var`. CLI `run()` implements a third path with additional
async-generator abandonment and stream cleanup logic (`lionagi/session/branch.py`,
`lionagi/session/_lifecycle_ctx.py`, `lionagi/operations/run/run.py`). The paths are individually
careful, but a new adapter must reproduce the same terminal policy and nested calls depend on a
suppression flag rather than explicit ownership.

These failures cannot be repaired by moving provider blocks alone. A same-Branch conversation also
has one ordered progression and no merge rule for simultaneous turns. The target therefore combines
typed invocation state with full-lifetime serialization of top-level model turns.

| Concern | Decision |
|---------|----------|
| Runtime state shape | D1: every top-level model turn owns one typed `TurnExecutionContext`, wrapped by a `TurnScope`. |
| Overlap and nesting | D2: top-level turns serialize on one Branch lock; task-local nested calls reuse the active context without owning lifecycle. |
| Model, provider, control, and signal state | D3: all four are resolved or stored through the active context and never through independent branch-global slots. |
| Lifecycle and stream cleanup | D4: one operation-layer driver owns start, one terminal, signal drain, cancellation, generator close, and lock release. |
| Compatibility surface | D5: lifecycle signals gain additive turn identity and `last_context_report` becomes a last-terminal compatibility view. |

This ADR deliberately does **not** decide:

- how a provider request is compiled from messages, guidance, tools, images, and response schemas;
  that belongs to the messages-context ADR on canonical turn-request compilation;
- provider retry, timeout, subprocess, parsing, action, or tool-authorization policy;
- graph-node scheduling or `op_id` allocation; a turn id supplements graph identity rather than
  replacing it;
- cross-Branch concurrency; distinct branches remain independently schedulable;
- direct message-manager mutation or a top-level `act()` invoked outside a model turn; those paths
  can still interleave with a model turn and remain caller-coordinated under this ADR;
- persistence or restart recovery for an in-flight context; execution contexts are runtime-only;
- a transactional rollback of messages, logs, API events, or tool effects after a failed turn;
- per-turn usage accounting; existing usage projection remains governed by the signal and operation
  contracts.

## Decision

### D1 — One typed context owns one top-level turn's runtime state

The target types live with operation parameter contracts in `lionagi/operations/types.py`:

```python
from asyncio import Task
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from lionagi.protocols.context_providers import ProviderReport
from lionagi.service.imodel import iModel
from lionagi.session.control import LoopControl


@dataclass(slots=True)
class TurnExecutionContext:
    turn_id: UUID
    model: iModel
    owner_task: Task[Any]
    phase: Literal["active", "finalizing", "closed"] = "active"
    provider_blocks: tuple[str, ...] = ()
    provider_report: ProviderReport | None = None
    control: LoopControl | None = None
    signal_tasks: set[Task[Any]] = field(default_factory=set)
    nested_tasks: set[Task[Any]] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class TurnScope:
    context: TurnExecutionContext
    owns_lifecycle: bool
```

```python
# lionagi/operations/turn.py
class TurnFinalizingError(RuntimeError):
    """A task inherited this Branch's turn binding after finalization began."""
```

`TurnExecutionContext` is mutable because providers, controls, and scheduled signal tasks arrive
during execution. `TurnScope` is frozen because ownership must not change after entry. The context is
not a Pydantic model and is never added to Branch or Session serialization.

The minimum module boundary is:

```text
lionagi/
├── operations/
│   ├── types.py          TurnExecutionContext, TurnScope
│   └── turn.py           scope entry, coroutine driver, stream driver,
│                         signal scheduling/drain, terminal finalization,
│                         TurnFinalizingError
├── session/
│   ├── branch.py         _turn_lock, _active_turn_context, facade adapters
│   └── signal.py         additive turn_id lifecycle fields
└── operations/chat/
    └── _prepare.py       reads provider_blocks from TurnScope
```

The Branch carries only the synchronization primitive and an out-of-band pointer to the one active
context:

```python
_turn_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
_active_turn_context: TurnExecutionContext | None = PrivateAttr(None)
```

The pointer is not an alternate state store. It exists so an observer callback running outside the
turn's task-local context can address the active turn through `Branch.control()`. All mutable turn
data remains inside `TurnExecutionContext`.

**Exact construction semantics:**

- `turn_id` is a new `uuid4()` for every top-level entry that acquires the lock.
- `model` is resolved after lock acquisition from the call-specific override or the appropriate
  current Branch model. Waiting turns therefore see configuration as it exists when they start.
- `owner_task` is `asyncio.current_task()` at top-level entry. Driver entry is async, so a missing
  current task is an invariant failure rather than a supported context shape.
- `phase` starts as `"active"`, becomes `"finalizing"` exactly once after the operation result or
  primary exception is fixed, and becomes `"closed"` before the lock is released. It never moves
  backward.
- `provider_blocks` starts empty and is immutable as a tuple once provider gathering assigns it.
- `provider_report` and `control` start as `None`; the signal-task and nested-task sets start empty.
- A nested scope references the same context object; it does not copy fields or create a second id.

### D2 — Same-Branch top-level turns serialize; nested work reuses scope

`lionagi/operations/turn.py` owns a task-local binding:

```python
@dataclass(frozen=True, slots=True)
class _ActiveTurnBinding:
    branch_id: UUID
    scope: TurnScope


_active_turn_var: ContextVar[_ActiveTurnBinding | None] = ContextVar(
    "lionagi_active_turn",
    default=None,
)
```

The target drivers have separate coroutine and iterator shapes so the stream wrapper can retain the
lock across every `yield`:

```python
async def run_in_turn(
    branch: Branch,
    *,
    model: iModel | None,
    invoke: Callable[[TurnScope], Awaitable[T]],
) -> T: ...


async def stream_in_turn(
    branch: Branch,
    *,
    model: iModel | None,
    invoke: Callable[[TurnScope], AsyncIterator[T]],
) -> AsyncIterator[T]: ...
```

Entry follows this state machine:

```text
call adapter
  |
  +-- binding has this Branch id, points to Branch._active_turn_context,
  |   and phase == "active" -----------------> nested TurnScope
  |                                               owns_lifecycle=False
  |                                               no lock acquisition / no lifecycle signals
  |
  +-- same live binding is "finalizing" -----> raise TurnFinalizingError
  |                                               no lock wait / no signals
  |
  +-- no matching live binding, including a stale closed binding
                            --> await Branch._turn_lock
                                |
                                +-- cancelled while waiting --> propagate
                                |                              no context/event
                                |
                                +-- acquired --> create context
                                                  set Branch active pointer
                                                  bind task-local scope
                                                  owns_lifecycle=True
                                                  emit RunStart
                                                  invoke body
```

The lock covers provider gathering, request construction, provider invocation, message recording,
actions and parsing that belong to the top-level adapter, signal drainage, terminal emission, and
stream finalization. For an async generator, merely constructing the generator acquires nothing;
entry occurs on first iteration. Once started, the lock remains held until normal exhaustion,
`aclose()`/`GeneratorExit`, cancellation, or failure finalization completes.

ContextVar inheritance makes a child task created inside an active turn part of that turn when it
calls the same Branch. It receives a nested scope that shares the context but explicitly does not
own lifecycle. This is intentional: work spawned from an active turn is not a second top-level
conversation turn while the parent context is active.

When nested entry occurs from a task other than `owner_task`, the driver adds the current task to
`context.nested_tasks` once and installs a done callback that removes it and observes/logs any task
failure. The owning operation is expected to await its child work. If the owner body returns or
raises while nested tasks remain, finalization cancels unfinished tasks and awaits a snapshot with
`return_exceptions=True` before draining message-signal tasks. A child failure changes the operation
result only when the owner awaited and received it; an unawaited child failure is diagnostic.
Detached nested model work therefore cannot continue into the next turn, and cancellation of it is
cleanup rather than a second lifecycle failure signal.

Inheritance alone is not proof of liveness. A child task can outlive its parent and retain a copied
binding after the parent releases the Branch. Scope entry therefore also requires pointer identity
with `Branch._active_turn_context` and `phase == "active"`. A closed or displaced binding is stale
and the child enters as a new top-level contender. A same-binding call during `"finalizing"` fails
with `TurnFinalizingError` instead of waiting on the lock its own task lineage is helping release;
this prevents a terminal observer handler from deadlocking by synchronously re-entering the Branch.
The handler may schedule independent work after terminal completion or handle and retry the error.

Distinct branches have distinct locks and remain concurrent. Same-Branch contenders queue rather
than fail. No lock-wait timeout is introduced; the exact queuing/fairness properties are those of
`asyncio.Lock`.

The top-level adapters that invoke models enter the driver. Direct `chat`, `chat_and_record`,
`parse`, `operate`, `communicate`, `interpret`, `ReAct`, `ReActStream`, and `run` calls therefore
participate. `act` alone does not acquire the turn lock because it is not a model turn; when called
inside a composed turn it runs within the owning scope. A direct top-level `act()` can append action
messages while a model turn is active; this ADR does not serialize that caller-managed path.
Internal operation functions accept or retrieve `TurnScope` instead of creating independent
lifecycle wrappers.

### D3 — Model, provider, control, and signal state is isolated by context

#### Model resolution

A call-specific model is stored as `scope.context.model`. CLI `run()` uses that local object for
endpoint validation, session resume, event construction, streaming, and temporary stream callbacks.
It does not assign `branch.chat_model` and does not need a restore step.

For a composed operation, `context.model` is the primary generation model selected at top-level
entry. Explicit parse or interpretation submodels remain parameters to those nested operation calls;
they also must not mutate the Branch model manager. A later top-level turn without an override uses
the configured Branch model, not the previous context's model.

**Error cases:**

- An invalid CLI endpoint fails the current turn and leaves Branch configuration untouched.
- Provider failure or cancellation leaves the context eligible for terminal cleanup but never
  publishes its model back to Branch.
- Changing Branch configuration while another turn is waiting affects the waiting turn when it
  starts; changing it during an active turn does not replace that turn's snapshot.

#### Provider gathering and report visibility

The target provider helper receives the active scope:

```python
async def _apply_context_providers(
    branch: Branch,
    instruction: JsonValue | Instruction,
    param: ChatParam,
    scope: TurnScope,
) -> Instruction | None: ...
```

- With no registered providers, `provider_report` remains `None` and `provider_blocks` remains `()`.
- With providers but no system message, providers are not invoked; the context receives
  `ProviderReport(skipped=list(registry.names))` and no blocks.
- With a system message, `ContextProviderRegistry.gather()` supplies the report and the context stores
  `tuple(report.blocks)`.
- Request preparation reads only `scope.context.provider_blocks`. It never reads or clears a Branch
  injection slot.
- Provider exceptions retain the registry's current containment semantics: names appear under
  `failed` and the turn continues with remaining blocks.
- The context report is available throughout nested work. At top-level terminal finalization it
  becomes the compatibility view described in D5.

This ADR retains the provider registry's **2,000-token** default total budget and optional
per-provider cap from ADR-0016. It does not add a second turn-level budget.

#### Loop control

The target public compatibility surface becomes:

```python
def control(
    self,
    directive: LoopDirective,
    *,
    reason: str | None = None,
    turn_id: UUID | None = None,
) -> bool: ...

def poll_control(
    self,
    *,
    turn_id: UUID | None = None,
) -> LoopControl | None: ...
```

`control()` reads `_active_turn_context`. It returns `False` and stores nothing when no context is in
the `"active"` phase or when a supplied `turn_id` does not match. Otherwise it writes one
`LoopControl` into that context and returns `True`. Omitting `turn_id` addresses the sole active turn
and preserves existing observer-callback ergonomics under same-Branch serialization. A control
arriving during finalization is rejected rather than being applied to the completed result or queued
for the next turn.

`poll_control()` uses the same optional identity check, returns the active context's directive, and
sets only that context's field to `None`. Multiple writes before a poll retain current last-write-wins
behavior. A directive is never queued for a future turn.

`check_control()` keeps its existing meanings: no directive or `CONTINUE` is a no-op, `BREAK` raises
`LoopBreak`, and `CANCEL` raises the clean `StopStream` control signal. The change is attribution, not
directive vocabulary.

#### Message-signal tasks

Message scheduling consults both the task-local binding and the Branch active pointer:

```text
message added while binding, pointer identity, and active phase agree
  -> create emit_message task
  -> add task to context.signal_tasks
  -> done callback removes it and consumes/logs task failure

message added without that live triple, including from a stale child binding
  -> create detached best-effort task
  -> done callback consumes/logs task failure
  -> no future turn adopts or drains it
```

As today, no task is scheduled when no observer exists or no event loop is running. Top-level
finalization repeatedly drains the active context's current task set with
`return_exceptions=True` until it is empty, then emits the terminal lifecycle event. It never swaps
or drains another context's tasks.

There is no signal-drain timeout or retry budget in this ADR. Preserving “message signals before
terminal” means a non-completing observer task can delay terminal emission and lock release. Adding a
timeout would require a separate loss and cancellation policy rather than an unexplained number.

### D4 — One driver owns start, terminal, cancellation, and stream close

Only a scope with `owns_lifecycle=True` emits lifecycle signals, updates the compatibility report,
or releases the lock. Nested operation paths receive `owns_lifecycle=False` and must not call a
second wrapper.

Once the body result or primary exception is known, the owner changes the context phase to
`"finalizing"` before draining signals. That closes nested entry and control attribution while
leaving the context available to the finalizer. It then cancels and awaits unfinished nested tasks,
drains the context signal-task set, attempts the single terminal signal, publishes the compatibility
report, marks the context `"closed"`, clears the Branch pointer, resets the ContextVar token, and
releases the lock. An independently scheduled caller without the inherited binding may wait
throughout finalization; a caller with the same live binding receives `TurnFinalizingError` as
specified in D2.

The target terminal matrix is normative:

| Path after lock acquisition | Terminal signal | Operation result | Cleanup |
|-----------------------------|-----------------|------------------|---------|
| normal coroutine return | `RunEnd(turn_id=...)` | return unchanged | cancel/await unfinished nested tasks, drain signals, clear binding/pointer, release lock |
| normal stream exhaustion | `RunEnd(turn_id=...)` | iteration ends | same |
| `StopStream` clean control | `RunEnd(turn_id=...)` | stream ends without error | same |
| consumer `aclose()` / `GeneratorExit` after start | `RunEnd(turn_id=...)` | `GeneratorExit` is not converted | same; underlying stream is closed |
| provider, parse, action, or other `Exception` | `RunFailed(turn_id=..., data=exc)` | original exception re-raised | same |
| `LoopBreak` | `RunFailed(turn_id=..., data=exc)` | original exception re-raised | same |
| cancellation after start | `RunFailed(turn_id=..., data=exc)` | cancellation re-raised | cancellation-shielded terminal cleanup, then release |
| cancellation while waiting for lock | none | cancellation re-raised | no context or lock ownership |
| generator created but never iterated | none | no yielded value | no context or lock ownership |

The driver sets one local `terminal_emitted` guard before attempting terminal observation. Cleanup is
performed in a cancellation-shielded scope so cancellation cannot strand `_active_turn_context` or
the lock. Observer exceptions during start or terminal are logged and do not replace the operation
result or exception. A terminal observer failure is not retried and does not permit a second terminal
attempt.

Pending message signals are drained before terminal emission. The active task-local binding and
Branch pointer are cleared after terminal attempt and before lock release. The ContextVar token is
reset rather than assigned blindly, so an outer context is restored if different-Branch work was
nested.

The stream driver also retains the current deterministic close requirement: its `finally` closes the
underlying async generator, restores any temporary `streaming_process_func`, and performs configured
stream-persistence cleanup before the turn terminal is emitted. An ordinary `Exception` raised by
close is logged and suppressed. A close-time `BaseException` is re-raised only when no other
exception is already unwinding; otherwise it is logged as secondary and suppressed. This preserves
the existing provider-stream behavior while moving lifecycle selection to one owner.

Started work is not rolled back. Messages or external effects committed before failure remain; the
terminal event describes the outcome and releases serialization for the next turn.

### D5 — Turn identity is additive and the provider report has a compatibility view

Run lifecycle signals gain an optional UUID field in `lionagi/session/signal.py`:

```python
class RunStart(Signal):
    turn_id: UUID | None = None


class RunEnd(Signal):
    turn_id: UUID | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: float = 0.0


class RunFailed(Signal):
    turn_id: UUID | None = None
```

The field is optional for compatibility with manual signal construction and non-turn projections,
but the D4 driver always supplies it. UUID JSON serialization produces the stable string form used
by persistence payloads. This is an additive nullable field under the current signal version policy,
so it does not by itself bump `SIGNAL_SCHEMA_VERSION`. `op_id` remains graph-node identity; consumers
must not substitute one id for the other.

`build_run_end()` accepts and forwards `turn_id`. Existing usage and duration fields retain their
current defaults and calculation policy; this ADR does not claim that branch-history usage is
already isolated per turn.

```python
def build_run_end(
    branch: Any,
    *,
    turn_id: UUID | None = None,
    duration_ms: float = 0.0,
    result: Any = None,
) -> RunEnd: ...
```

`Branch.last_context_report` remains as a read-only compatibility property, with clarified target
meaning: report from the **last terminal top-level turn**, including failed, cancelled, or abandoned
turns. Finalization copies the active context's report into one private compatibility slot after
signal drainage. A currently running turn never publishes a partial report there. A no-provider turn
publishes `None`, so an older report cannot remain falsely current.

No unbounded `turn_id -> ProviderReport` map is added. The shipped compatibility surface therefore
retains only the last terminal report; durable report history would require a separately specified
observation or persistence contract. The execution context itself is released after terminal
cleanup.

Migration removes direct reads and writes of `_context_injection_slot`, `_loop_control`, and
`_signal_tasks`, and removes lifecycle suppression through `suppress_lifecycle_var`. Temporary
compatibility shims may exist during implementation, but the completed target has one lifecycle owner
and no operation path may use those branch-global slots.

## Consequences

- Provider blocks, provider reports, controls, model overrides, and message-signal tasks have an
  unambiguous turn owner. No top-level turn can clear, consume, or drain another's context.
- One Branch deliberately gives up overlapping top-level model turns. Work needing parallel model
  calls uses distinct branches or graph-created clones. Same-Branch callers wait instead of receiving
  a new conflict error.
- Child work created inside a turn shares that turn through ContextVar propagation. Contributors must
  treat it as nested work and must not emit lifecycle signals independently. Child tasks that
  outlive the owner body are cancelled and awaited before terminal emission; they cannot reuse a
  stale context. A finalizer-time re-entry fails fast and a post-close re-entry contends as a new
  top-level turn.
- A stream holds the Branch lock for its full active lifetime. Consumers that start iteration are
  responsible for exhausting or closing the iterator; the wrapper makes close deterministic once it
  receives `aclose()` or `GeneratorExit`.
- Observer failures remain diagnostic. Cancellation requires shielded cleanup, which slightly
  increases cancellation latency but prevents a permanently locked Branch.
- Out-of-band control can target a known turn id and safely rejects stale controls. The compatibility
  no-id form is safe only because one Branch has at most one active top-level turn.
- Reversing D2 is expensive because deterministic progression and lifecycle would need a merge
  protocol. D1, D3, and D5 are internal or additive contracts; D4 consolidates existing behavior but
  touches every model-turn adapter.

## Alternatives considered

### Allow fully concurrent turns with separate temporary contexts

This would isolate provider blocks and model overrides while preserving maximum throughput. It loses
because Branch still has one message progression, one append sequence, and one configured snapshot.
Two turns can build from different histories and append in completion order, producing a conversation
neither model saw. A merge policy would be a different architecture, not a context-field fix.

### Add only a branch-global lock

A lock alone would prevent overlap and is the smallest race fix. It loses because lifecycle
ownership, temporary model selection, provider attribution, signal-task grouping, and stale control
remain implicit private-field conventions. The typed context makes those contracts inspectable and
testable.

### Reject overlap instead of queueing

Fail-fast `TurnAlreadyActive` would make contention visible and avoid unbounded wait. It loses
compatibility with async callers that reasonably submit work before a prior stream closes and forces
retry policy into every caller. Serialization already defines a deterministic safe order; queuing is
the narrower behavior change.

### Clone Branch automatically for every overlapping call

This would preserve concurrency and give each call a separate progression. It loses conversation
identity: results would need an explicit merge, tools and memory have nontrivial clone semantics, and
the caller asked to continue one Branch rather than create graph fan-out. Explicit clones remain the
parallelism mechanism.

### Use ContextVar alone with no Branch active pointer

This would keep all state task-local and remove a Branch private field. It loses out-of-band control:
observer handlers or external control pollers can run in tasks that do not inherit the invocation's
ContextVar. The narrow `_active_turn_context` pointer makes that seam explicit while the context
retains the state.

### Trust an inherited ContextVar binding until the child task exits

This would avoid a phase field and pointer-identity check. It loses because asyncio copies context
into child tasks: a detached child can retain the parent's binding after terminal cleanup, bypass
the Branch lock, and mutate a later conversation turn through a closed context. Treating a
same-lineage finalizer call as an ordinary lock contender is also unsafe because a terminal observer
can await that call while the driver awaits the observer, producing self-deadlock. The explicit
active/finalizing/closed phases make both cases decidable.

### Keep one branch-global pending control for compatibility

This would preserve controls issued before a turn starts. It loses attribution: a stale cancel could
silently terminate the next unrelated turn. The target returns `False` when there is no matching
active context, making the race observable to the controller.

### Mutate the Branch model and restore it in `finally`

This is a smaller change to CLI `run()`. It loses because cancellation and overlap make restoration
order meaningful: one call can restore a model over a newer configuration. Resolving a local model
snapshot has no shared rollback problem.

### Retain separate lifecycle wrappers plus a suppression flag

This would minimize movement of working code. It loses because exact-once behavior remains a
convention copied among coroutine, reasoning-loop, and async-generator paths. A task-local boolean
answers only “suppress?”; it does not identify the owning turn or centralize terminal cleanup.

### Fold execution context into the compiled `TurnRequest`

One type would reduce names. It loses the boundary between immutable provider input and mutable
runtime resources. Controls and asyncio tasks are not request fields, while rendered messages and
tool schemas are not lifecycle ownership. Separate types allow request compilation to remain pure.

### Persist execution contexts for restart

This would appear to support resuming in-flight turns. It loses because locks, Tasks, provider stream
objects, and live callbacks cannot be reconstructed from a data record, and external effects may
already have occurred. Durable checkpoints require a separate operation-specific recovery protocol.

### Keep a report map for every completed turn

This would give direct random access by `turn_id`. It loses because retention, eviction, and
persistence policy are undefined and would make Branch an observability database. An additive id on
events supplies correlation without unbounded Branch memory.

## Notes

The target sequence for two independent calls on one Branch is:

```text
caller A            Branch / turn driver             caller B
   |                         |                           |
   | first iteration/call    |                           |
   |------------------------>| acquire lock              |
   |                         | RunStart(A)               |
   |                         | execute + message signals |
   |                         |<--------------------------| call waits
   |                         | drain(A)                  |
   |                         | RunEnd/RunFailed(A)       |
   |                         | release                   |
   |                         | acquire for B             |
   |                         | RunStart(B)               |
   |                         | execute + drain(B)        |
   |                         | RunEnd/RunFailed(B)       |
```

The ContextVar replaces the current lifecycle-suppression boolean with an explicit scope. It is not
a substitute for the Branch lock: task-local isolation alone cannot order shared conversation
history.
