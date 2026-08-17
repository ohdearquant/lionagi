# ADR-0120: Interception, observation, and durable delivery planes

- **Status**: Accepted
- **Kind**: Aspirational
- **Implementation-status**: partial — Phase 0 compatibility profiles characterized
- **Area**: hooks
- **Date**: 2026-08-16
- **Relations**: extends ADR-0047 (hook mechanism scopes), ADR-0048 (external hooks),
  ADR-0059 (durable dispatch outbox), and ADR-0095 (run-terminal callbacks); depends on
  ADR-0119 (deterministic declaration substrate)

## Context

LionAGI uses the words hook, observer, event, signal, callback, broadcaster, message, and bus for
several mechanisms that share dispatch mechanics but not semantics. ADR-0047 correctly rejected
one universal hook ABI and identified three public hook families. The current tree contains at
least fifteen callback or dispatch contracts once session signals, persistence callbacks,
scheduler signals, message callbacks, engine events, and external adapters are included.

The count is not itself a defect. Executable work, a veto before work, an immutable observation,
and delivery of a committed fact must not have the same behavior. The defect is that the semantic
difference is hidden inside class names and hard-coded branches rather than declared at the call
site.

**P1 — four semantic planes are mixed.** `protocols.generic.Event` is executable work with a
status/completion lifecycle. `HookRegistry`, tool preprocessors, and blocking HookBus points can
change or prevent work. `Signal`, `SessionObserver`, `SchedulerSignalBus`, and `Broadcaster`
announce facts. `TerminalCallbackRegistry`, message persistence retry, and the dispatch outbox
deliver committed state. Treating them all as events or buses makes ownership and failure
semantics impossible to infer.

**P2 — dispatch policy is encoded in implementation classes.** HookBus is sequential, preserves
registration order, supports `StopHook`, swallows ordinary failures, and propagates two hardcoded
points. SessionObserver calls synchronous subscribers inline and gathers asynchronous ones.
SchedulerSignalBus concurrently gathers and aggregates failures. TerminalCallbackRegistry
concurrently fans out under a shared deadline and offloads synchronous handlers. Message
callbacks are sequential and raise an exception group after running all handlers. None of those
policies is visible in a registration or emission contract.

**P3 — authorization can suppress audit observation.** SessionObserver uses one gate both for
pre-operation authorization and signal emission. It stores the signal in memory, then applies
the gate before routes/subscribers. StateDB persistence is one such subscriber. A governance
decision can therefore suppress the durable audit observation of a hook or operation that has
already occurred. Authorization and observation cannot share a gate.

**P4 — tool invocation has three enforcement layers and three denial shapes.** The action path
runs Session authorization, a blocking `HookBus.TOOL_PRE`, then tool/AgentSpec preprocessors.
Ordering differs between Agent factory and CodingToolkit. Some error-hook fields are stored but
never invoked. A direct `ActionManager`/`FunctionCalling` path bypasses the outer layers. This ADR
defines the interception semantics; ADR-0121 assigns the authoritative invocation owner.

**P5 — HookBus owns persistence business logic.** Session end secretly flushes message retry
queues. Default hook handlers write StateDB lifecycle rows that CLI setup/teardown also writes.
The retry implementation documents two teardown paths that do not know about each other. An
in-process interceptor dispatcher must not own post-commit delivery or database lifecycle.

**P6 — service hook events bypass their executable-event completion contract.** HookEvent writes
execution status directly rather than using the Event status setter, so terminal completion
signaling can be skipped. The Event implementation also uses a raw `asyncio.Event` despite the
internal concurrency primitive. These are small symptoms of two lifecycle models being joined by
inheritance rather than an explicit adapter.

**P7 — external-hook schemas drift across settings, CLI, Studio, frontend, and providers.** The
runtime external executor and `li hooks` trust/import path now exist, so ADR-0048's implementation
status is stale. Studio still presents a partly Claude-specific event set as provider-neutral,
requires a command string where runtime requires argv, and duplicates the event union in
TypeScript. One adapter schema must drive all projections.

**P8 — apparently dead surfaces are compatibility-bound.** `Broadcaster` has no production
consumer but is root-exported and contract-tested. `ARTIFACT_CREATED` is dormant but public and
warns on registration. HookSignal payloads are persisted and consumed by SSE/frontend code.
Deleting them before a compatibility and stored-data gate is not a quick win.

| Concern | Decision |
|---|---|
| Vocabulary | D1: executable Event, Interceptor, Signal/Subscriber, Message, and durable Delivery are distinct contracts. |
| Shared mechanics | D2: two dependency-light kernels provide ordered interception and policy-declared fan-out; no universal semantic bus exists. |
| Authorization and audit | D3: operation authorization is owned by the operation; observation never vetoes, and authorization never suppresses committed audit delivery. |
| Domain ownership | D4: each operation compiles a typed interceptor plan; existing hook families become adapters rather than peer authorities. |
| Durable work | D5: persistence, retry, outbox, idempotency, and post-commit ordering stay in `state`. |
| External hooks | D6: one versioned external profile and executor contract drives CLI, Studio, frontend, and provider adapters. |
| Placement and compatibility | D7: domain payloads remain with their owners; mechanics move behind façades, followed by explicit deprecation/deletion. |
| Existing ADR truth | D8: ADR-0047 and ADR-0048 receive amendments before implementation begins. |

This ADR deliberately does not decide:

- the authoritative callable executor or permission policy; ADR-0121 owns it;
- the persistence schema for signals/outbox rows; ADR-0118 and ADR-0059 own it;
- whether every observation is persisted. Durability is selected by an explicit sink;
- a broker, cross-process event bus, or distributed message system;
- removal of `Event`, `FlowEvent`, `EngineEvent`, `RunTerminalEnvelope`, or addressed Messenger
  payloads merely because they share lifecycle words;
- public breaking renames in the first implementation phase.

## Decision

### D1 — The semantic planes are closed and named

The vocabulary is:

| Term | Meaning | May mutate/veto source work? | Durable by itself? |
|---|---|---:|---:|
| `Event` | executable work with status and completion | its owner changes its lifecycle | no |
| `Interceptor` | ordered control around an authoritative operation | yes, through a typed decision | no |
| `Signal` / `DomainEvent` | immutable fact that occurred or was observed | no | no; a sink may persist it |
| `Subscriber` / `EventSink` | in-process consumer of a Signal | no | no |
| `CommitParticipant` | stages required evidence/outbox intent inside the owner transaction | may make commit fail before it occurs | yes, with the owner commit |
| `Message` | addressed payload with sender/recipient/delivery semantics | not by observation | transport-specific |
| `Delivery` | post-commit attempt to convey a durable fact | never changes the committed fact | yes |

“Bus” is not a semantic type. Existing names may retain it as a compatibility façade, but new
types use `InterceptorChain`, `Dispatcher`, `Sink`, `Outbox`, or a domain-specific transport.

Exact rules:

- a Signal is treated as immutable from emission onward; handlers receive the same logical fact
  and cannot replace it for later handlers;
- an interceptor runs only inside the operation owner's call boundary;
- a subscriber failure cannot retroactively change the source operation's result;
- a required `CommitParticipant` may make the commit protocol fail before commit; a subscriber or
  post-commit `DeliverySink` cannot, and delivery failure changes delivery state only;
- an Event may emit Signals and use Interceptors; it does not inherit either dispatcher to obtain
  those capabilities;
- FlowEvent remains a streaming projection of operation lifecycle, while node Signals remain
  observable facts;
- Engine domain DTOs may gain stable identity where replay needs it, but are not forced to inherit
  executable Event.

### D2 — Two kernels share mechanics, not semantics

The shared layer contains two primitives declared with ADR-0119 values.

```python
class InterceptorDisposition(str, Enum):
    CONTINUE = "continue"
    STOP = "stop"
    DENY = "deny"

@dataclass(frozen=True, slots=True, init=False, eq=False)
class InterceptorDecision(Params, Generic[ContextT]):
    disposition: InterceptorDisposition
    context: ContextT | UnsetType = Unset
    reason: str | UnsetType = Unset
    evidence: tuple[EvidenceRef, ...] = ()

@dataclass(frozen=True, slots=True, init=False, eq=False)
class InterceptorRegistration(Params, Generic[ContextT]):
    name: str
    stage: str
    handler: Interceptor[ContextT]
    order: int

class OrderedInterceptorChain(Generic[ContextT]):
    async def apply(self, context: ContextT) -> InterceptorReport[ContextT]: ...
```

An in-memory `InterceptorRegistration.handler` is runtime-only and is never part of a durable
digest. A persisted policy/profile stores ADR-0119 `CallableRef`; the composition root resolves it
to the handler before compiling the immutable plan, and the plan digest carries the stable ref,
not Python callable identity.

`CONTINUE` passes the current context, or a replacement when `context` is supplied. `STOP` stops
remaining registrations but allows the owning operation to continue with the current/replaced
context; it is the typed equivalent of `StopHook`. `DENY` stops and returns a typed denial to the
owner, which decides its public error/result type. Handlers do not raise a generic
`PermissionError` to express an ordinary denial.

Registration order is `(stage order declared by the owner, explicit order, composition order)`.
Names are unique within one compiled plan. The kernel does not invent stages such as security or
post; a domain owner declares them. Exception mapping is part of the plan: a security stage maps
an evaluator exception to denial, while a compatibility observation-like hook may log/isolate.
Cancellation always propagates unless an explicitly bounded teardown stage is shielded by its
owner.

Signal fan-out uses a separate policy:

```python
class DispatchConcurrency(str, Enum):
    SEQUENTIAL = "sequential"
    CONCURRENT = "concurrent"

class DispatchFailure(str, Enum):
    PROPAGATE = "propagate"
    COLLECT = "collect"
    ISOLATE = "isolate"

class SyncHandlerMode(str, Enum):
    INLINE = "inline"
    OFFLOAD = "offload"

class CompletionTiming(str, Enum):
    AWAIT_ALL = "await_all"
    STOP_ON_FAILURE = "stop_on_failure"
    CANCEL_REMAINING_ON_FAILURE = "cancel_remaining_on_failure"
    DETACH_BOUNDED = "detach_bounded"

class ErrorSurface(str, Enum):
    RETURN_REPORT = "return_report"
    RAISE_FIRST = "raise_first"
    RAISE_GROUP = "raise_group"
    UNWRAP_SINGLE_ELSE_GROUP = "unwrap_single_else_group"
    LOG_ONLY = "log_only"

class HandlerCancellation(str, Enum):
    PROPAGATE = "propagate"
    TRANSLATE = "translate"
    COLLECT = "collect"

class DeadlineDisposition(str, Enum):
    RAISE = "raise"
    RETURN_REPORT = "return_report"
    LOG_AND_STOP = "log_and_stop"
    SILENT_STOP = "silent_stop"

class PredicateFailure(str, Enum):
    PROPAGATE = "propagate"
    COLLECT_AS_HANDLER_FAILURE = "collect_as_handler_failure"
    NO_MATCH = "no_match"
    ISOLATE = "isolate"

class HandlerClassification(str, Enum):
    BY_DECLARATION = "by_declaration"
    BY_RETURN_VALUE = "by_return_value"

class ReturnedValueClassifier(str, Enum):
    INSPECT_AWAITABLE = "inspect_awaitable"
    ASYNCIO_COROUTINE_ONLY = "asyncio_coroutine_only"
    NOT_INSPECTED = "not_inspected"

class ReturnedAwaitableDisposition(str, Enum):
    AWAIT_IN_INVOCATION = "await_in_invocation"
    GATHER_AFTER_INVOCATIONS = "gather_after_invocations"
    DISCARD_UNAWAITED = "discard_unawaited"
    REJECT = "reject"

class SyncEntryPolicy(str, Enum):
    UNAVAILABLE = "unavailable"
    ALLOW = "allow"
    REJECT_DECLARED_ASYNC_BEFORE_OWNER_EFFECT = "reject_declared_async_before_owner_effect"

@dataclass(frozen=True, slots=True, init=False, eq=False)
class DispatchStagePolicy(Params):
    failure: DispatchFailure
    completion: CompletionTiming
    error_surface: ErrorSurface
    handler_cancellation: HandlerCancellation

@dataclass(frozen=True, slots=True, init=False, eq=False)
class DispatchPhase(Params):
    selector: Literal["all", "declared_sync", "declared_async"]
    concurrency: DispatchConcurrency
    sync_handlers: SyncHandlerMode
    handler_classification: HandlerClassification
    returned_value_classifier: ReturnedValueClassifier
    returned_awaitable: ReturnedAwaitableDisposition
    sync_entry: SyncEntryPolicy
    predicate_failure: PredicateFailure
    invocation: DispatchStagePolicy
    returned_awaitables: DispatchStagePolicy | UnsetType = Unset
    deadline_disposition: DeadlineDisposition | UnsetType = Unset
    deadline_seconds: float | UnsetType = Unset

@dataclass(frozen=True, slots=True, init=False, eq=False)
class DispatchPolicy(Params):
    phases: tuple[DispatchPhase, ...]
    preserve_registration_order: bool = True

class FanoutDispatcher(Generic[SignalT]):
    def preflight_sync(self) -> DispatchSnapshot: ...
    def emit_sync(self, signal: SignalT, *, snapshot: DispatchSnapshot) -> DispatchReport: ...
    async def emit(self, signal: SignalT) -> DispatchReport: ...
```

`DispatchReport` names every matched registration and records returned value, isolated failure,
timeout, or cancellation. Concurrent execution still reports registrations in deterministic
registration order. A deadline is one shared budget when declared, not a fresh full timeout per
subscriber. Sync offload uses internal concurrency utilities. Cancellation is never folded into
an ordinary handler error. Phases execute in declaration order and each registration is selected
by exactly one phase. Handler classification, the exact returned-value classifier, and
returned-awaitable handling are separate because
the current loops are observably different: some route by coroutine-function declaration, while
SessionObserver and SchedulerSignalBus use `inspect.isawaitable()` after invocation and Broadcaster
uses the narrower `asyncio.iscoroutine()`. For
`GATHER_AFTER_INVOCATIONS`, invocation is sequential in registration order and only the returned
awaitables are gathered concurrently.

`DISCARD_UNAWAITED` is an explicitly deprecated compatibility behavior, not a recommended default,
and its removal is a behavior change rather than cleanup. Today a handler that returns an
awaitable under this setting never runs that awaitable; after the flip it does, so work that has
never executed in production starts executing, and the first symptom is whatever that work does.
It therefore gets its own gate rather than riding the migration: the set of registrations
currently resolving to `DISCARD_UNAWAITED` is enumerated and each is either converted deliberately
or recorded as intentionally discarding, the flip lands as its own change with its own revert,
and the implementation issue names the owner accountable for that enumeration at the moment it
opens rather than when the flip is proposed. A deprecation with no named owner is removed
eventually by whoever is least aware of what it was protecting.

A synchronous owner calls `preflight_sync()` before its domain mutation and later supplies that
immutable registration snapshot to `emit_sync()`. The message-manager profile rejects a declared
async callback during that preflight, so rejection remains before message mutation. An async-only
profile has no sync entry. Empty, overlapping, or incomplete selectors, or a snapshot from another
dispatcher revision, fail rather than guessing an order.

Policy construction validates combinations. `RAISE_GROUP` and
`UNWRAP_SINGLE_ELSE_GROUP` require their stage's `failure=COLLECT`; `RAISE_GROUP` additionally
requires `completion=AWAIT_ALL`, while unwrap/group may use `AWAIT_ALL` or
`CANCEL_REMAINING_ON_FAILURE`. `LOG_ONLY` requires that stage's `failure=ISOLATE`.
`GATHER_AFTER_INVOCATIONS` requires `handler_classification=BY_RETURN_VALUE`, an async entry, and a
separate resolved `returned_awaitables` stage; `AWAIT_IN_INVOCATION`, `DISCARD_UNAWAITED`, and
`REJECT` forbid that second stage. `BY_RETURN_VALUE` requires an explicit non-`NOT_INSPECTED`
classifier; `declared_sync`/`declared_async` selectors require `BY_DECLARATION`; and a finite deadline must name a non-absent deadline
disposition. Named constructors below return the exact validated tuples—callers cannot recreate a
profile by choosing defaults field by field.

Existing mechanisms select policies that reproduce current behavior before any policy is
changed. The purpose is to make the difference inspectable, not normalize every domain to one
default.

The first compatibility profiles are normative and named rather than reconstructed from generic
defaults:

| Named profile | Invocation/classification | Error surface | Cancellation/deadline | Predicate/returned awaitable |
|---|---|---|---|---|
| HookBus blocking points | sequential; declaration-owned interceptor | raise first ordinary failure | emitter cancellation propagates; no deadline | no predicate; await handler result |
| HookBus observational points | sequential; declaration-owned interceptor | isolate/log; `StopHook` ends the chain | emitter cancellation propagates; no deadline | no predicate; await handler result |
| Broadcaster | async entry; sequential; invoke first and classify only `asyncio.iscoroutine()` results | isolate/log ordinary `Exception` | handler/emitter cancellation propagates; no deadline | type mismatch raises before dispatch; non-coroutine awaitables are not awaited |
| SessionObserver legacy observation | async entry; invoke every handler inline/sequentially, classify by returned value, then gather returned awaitables | invocation/filter failure stops immediately; only after all invocations succeed, returned-awaitable failures unwrap one or raise a group and cancel remaining awaitables | emitter cancellation propagates; no deadline | filter/route failure propagates; `GATHER_AFTER_INVOCATIONS` |
| message-added sync | sync preflight rejects declared async before mutation; sequential drain | unwrap one failure, group several `BaseException` values | caught handler failure surfaced after drain; no deadline | no predicate; sync returned awaitable is discarded (deprecated compatibility) |
| message-added async | declaration classification; sequential drain; declared async awaited | unwrap one failure, group several `BaseException` values | caught handler failure surfaced after drain; no deadline | no predicate; sync returned awaitable is discarded (deprecated compatibility) |
| SchedulerSignalBus | async entry; concurrent invocation; classify/await returned values in each task | `RAISE_GROUP` even for one ordinary failure | handler cancellation wins and becomes `SchedulerHandlerCancelled`; if ordinary errors also exist their `ExceptionGroup` is its cause; emitter cancellation propagates; no deadline | predicate failure joins the ordinary-error group |
| TerminalCallbackRegistry | async entry; declaration classification; concurrent; declared sync offloaded | ordinary failure log/isolate | handler cancellation is re-raised inside the task group; emitter cancellation propagates; shared-budget expiry silently cancels async work and returns; abandoned sync thread work may continue | registration filter cannot execute user code; returned awaitable is awaited |

The Scheduler constructor is pinned to one `selector="all"` phase with
`concurrency=CONCURRENT`, `sync_handlers=INLINE`,
`handler_classification=BY_RETURN_VALUE`, `returned_value_classifier=INSPECT_AWAITABLE`,
`returned_awaitable=AWAIT_IN_INVOCATION`,
`sync_entry=UNAVAILABLE`, `invocation=(failure=COLLECT, completion=AWAIT_ALL,
error_surface=RAISE_GROUP, handler_cancellation=TRANSLATE)`, `returned_awaitables=Unset`,
`deadline_disposition=Unset`, and
`predicate_failure=COLLECT_AS_HANDLER_FAILURE`. Fixtures cover zero/one/many
ordinary failures, cancellation only, and cancellation plus ordinary failures with the latter
group preserved as `__cause__`.

The TerminalCallback constructor is pinned to one `selector="all"` phase with
`concurrency=CONCURRENT`, `sync_handlers=OFFLOAD`,
`handler_classification=BY_DECLARATION`, `returned_value_classifier=INSPECT_AWAITABLE`,
`returned_awaitable=AWAIT_IN_INVOCATION`,
`sync_entry=UNAVAILABLE`, `invocation=(failure=ISOLATE, completion=AWAIT_ALL,
error_surface=LOG_ONLY, handler_cancellation=PROPAGATE)`, `returned_awaitables=Unset`,
`deadline_disposition=SILENT_STOP`, a shared finite budget, and
`predicate_failure=PROPAGATE` (registration filters execute no user code). Ordinary exceptions are logged; handler cancellation and emitter
cancellation are not logged as ordinary errors. Deadline cancellation produces no timeout log.
Because the current sync offload uses `abandon_on_cancel=True`, a deadline may return while that
thread continues; the compatibility report marks it `abandoned_unknown` and cannot claim teardown
or observe its later result. Changing that behavior requires a separately accepted profile change.

The SessionObserver constructor is a closed composite profile, not inferred from the table: one
`selector="all"`, `SEQUENTIAL`, `INLINE`, `BY_RETURN_VALUE`,
`returned_value_classifier=INSPECT_AWAITABLE`, `GATHER_AFTER_INVOCATIONS`,
`sync_entry=UNAVAILABLE` phase has
`invocation=(PROPAGATE, STOP_ON_FAILURE, RAISE_FIRST, PROPAGATE)` and
`returned_awaitables=(COLLECT, CANCEL_REMAINING_ON_FAILURE,
UNWRAP_SINGLE_ELSE_GROUP, COLLECT)`. Thus a synchronous invocation/filter error stops before the
gather stage; only after every invocation succeeds are returned awaitables gathered. If that first
stage fails after earlier handlers returned awaitables, current compatibility abandons those
unawaited values; a fixture pins the behavior and the facade emits a diagnostic until migration
removes it. Cancellation raised by one returned awaitable cancels its remaining siblings, is
collected separately from the ordinary error surface, and is returned as cancellation object(s)
through the legacy facade. A one-cancelled/one-sibling fixture pins that unusual behavior.

The Broadcaster constructor is pinned to one `selector="all"`, `SEQUENTIAL`, `INLINE`,
`BY_RETURN_VALUE`, `returned_value_classifier=ASYNCIO_COROUTINE_ONLY`,
`AWAIT_IN_INVOCATION`, `sync_entry=UNAVAILABLE`,
`invocation=(ISOLATE, AWAIT_ALL, LOG_ONLY, PROPAGATE)`, `returned_awaitables=Unset`, no deadline,
and pre-dispatch event-type validation that raises `ValueError`. Thus a generic awaitable that is
not an `asyncio` coroutine is not awaited under the compatibility profile.

MessageManager has two exact constructors. Both are one `selector="all"`, `SEQUENTIAL`, `INLINE`,
`BY_DECLARATION`, `returned_value_classifier=NOT_INSPECTED`, `DISCARD_UNAWAITED` phase with
`invocation=(COLLECT, AWAIT_ALL, UNWRAP_SINGLE_ELSE_GROUP, COLLECT)`,
`returned_awaitables=Unset`, and no deadline. The sync constructor uses
`sync_entry=REJECT_DECLARED_ASYNC_BEFORE_OWNER_EFFECT`; the async constructor uses
`sync_entry=UNAVAILABLE` and awaits declared coroutine functions inline. Both catch
`BaseException`, drain later callbacks, then re-raise one value or a `BaseExceptionGroup`; a
collected cancellation is therefore delayed until drain and is never converted into an ordinary
error. Fixtures cover one/many ordinary failures, one cancellation, cancellation plus ordinary
failure, declared-async sync preflight before message mutation, and a sync wrapper returning an
unawaited awaitable.

Service HookRegistry/HookedEvent is an interceptor profile rather than Signal fan-out: one handler
per hook/chunk key, a per-HookEvent deadline, captured Event status/error according to `exit`, and
emitter cancellation propagation. Phase 1 freezes its invoke and stream-teardown matrix separately.
No adapter may select a profile by copying another row's defaults; each row has a named fixture.

### D3 — Authorization and observation are separate registries

SessionObserver becomes an observation façade only:

```python
class SessionObservation:
    def observe(self, *filters: SignalFilter, handler: SignalHandler, ...) -> Registration: ...
    def unobserve(self, registration: Registration) -> None: ...
    async def emit(self, signal: Signal) -> DispatchReport: ...
```

Operation authorization moves to the operation's compiled interceptor plan. Existing
`SessionObserver.gate` behavior is adapted into a session-policy interceptor until callers move.

The ordering invariant for an operation is:

```text
authorize/intercept proposed operation
    -> perform or deny operation
    -> CommitParticipant stages required evidence/outbox intent in the owner transaction
    -> atomically commit authoritative outcome + staged intent
    -> emit immutable observation
    -> DeliverySink delivers committed intent according to its own contract
```

Exact semantics:

- denial of a proposed operation may itself emit an `OperationDenied` Signal;
- a gate never runs over that resulting Signal;
- a Signal subscriber cannot return a replacement operation or denial;
- a `CommitParticipant` failure aborts before commit; it is not a subscriber failure;
- a `DeliverySink` sees only a committed fact/intent, and its failure changes delivery state rather
  than the domain outcome;
- best-effort UI subscribers cannot block required audit sinks by running first;
- in-memory Flow storage, routing, and durable persistence are independent subscriptions;
- a signal may be emitted without storing it in Session Flow, and stored without being globally
  broadcast;
- subscriber mutation of a mutable payload is prevented by frozen DTOs or defensive wire
  snapshots at the dispatch boundary.

This directly fixes the current path where governance can suppress HookSignal persistence.

### D4 — The operation owner compiles the interceptor plan

There is no global hook registry deciding every operation. Each authoritative owner names its
context and phase plan.

| Owner | Typed context | Plan |
|---|---|---|
| ActionExecutor | normalized tool descriptor, arguments, trusted identity, execution scope, optional Run, policy/evidence profile | capacity -> governed start -> authorization -> intrinsic guard -> action policy -> user transform -> revalidate/recheck -> pre-call evidence -> invoke -> terminal evidence -> projection |
| iModel/service call | request, endpoint, stream state, retry/deadline metadata | pre-create -> pre-invoke -> invoke/stream -> post/error/teardown |
| Branch/Session lifecycle | Session/Branch identity and transition request | lifecycle validation -> commit -> observation |
| provider/native harness | HarnessPlan, provider capabilities, workspace lease | pre-spawn admission -> compile -> spawn -> observe -> teardown |

HookBus points become adapters:

- blocking `TOOL_PRE` and `USER_PROMPT_SUBMIT` registrations compile into the corresponding
  owner plan;
- API and lifecycle points that only observe become typed Signals;
- `TOOL_POST`/`TOOL_ERROR` become post/error interceptors only if they are allowed to alter the
  returned tool result; otherwise they are subscribers;
- arbitrary `HookSignal.kwargs` remains a compatibility projection during dual-write, not the
  new authority;
- hardcoded `_BLOCKING_POINTS` disappears only after every public registration is classified.

Service `HookRegistry`/`HookedEvent` remain façades over a service-owned plan. Their streaming
teardown budgets, status translation, and exit compatibility are retained. HookEvent terminal
state goes through the Event setter, and raw concurrency is replaced with internal primitives,
as an independently testable mechanical fix.

The tool plan is shared with ADR-0121. This ADR fixes what an interceptor is; ADR-0121 ensures no
callable path bypasses the owner that runs it.

### D5 — Durable delivery stays in `state`

Transactional evidence/outbox participation and post-commit delivery are different protocols:

```python
class CommitParticipant(Protocol):
    async def stage(self, transaction: TransactionRef, fact: PendingDomainFact) -> CommitRefs: ...

class DeliverySink(Protocol):
    async def deliver(self, committed: CommittedDeliveryIntent) -> DeliveryResult: ...
```

`CommitParticipant` executes inside the domain owner's transaction and may make that transaction
fail before commit. It writes required audit evidence or a durable outbox intent; it never performs
network delivery and is not scheduled by `FanoutDispatcher`. `DeliverySink` begins only from a
committed intent, uses idempotency/acknowledgement state, and cannot rewrite the source outcome.
One component may implement both ports, but one call cannot pretend the two phases are atomic.

Durable delivery adds semantics neither shared kernel provides:

- the source fact is already committed;
- delivery has an idempotency/acknowledgement identity;
- retries and backoff are durable;
- filters and destinations are persisted or resolved from a versioned snapshot;
- terminal facts must not be reordered before the transition that produced them;
- failure changes delivery state, not source lifecycle state.

`TerminalCallbackRegistry` remains under `state/lifecycle`. It may delegate handler fan-out to a
`FanoutDispatcher` policy that reproduces its current concurrent, shared-deadline, sync-offload,
and cancellation semantics. Entity kind/id filtering, override resolution, post-commit ordering,
and its envelope remain domain-owned.

Message persistence and retry move out of `hooks` into a StateDB adapter/outbox. The target name
is `PendingMessageWrite`, not an Event implying generic execution. HookBus no longer flushes a
retry queue on `SESSION_END`; lifecycle teardown asks the durable owner to drain and records an
explicit incomplete-delivery result when the bounded drain fails.

The scheduler keeps typed scheduler Signals and counters. Its local bus mechanics can use the
fan-out kernel, while counter coordination and durable admin-event failure reporting remain with
the scheduler. Messenger remains an addressed coordination transport, not a generic Signal bus.

### D6 — External hooks have one versioned adapter contract

External hooks are an integration profile over the semantic planes, not another internal bus.

```python
@dataclass(frozen=True, slots=True, init=False, eq=False)
class ExternalHookProfile(Params):
    profile: Literal["claude", "codex", "lionagi"]
    version: str
    registrations: tuple[ExternalHookRegistration, ...]

@dataclass(frozen=True, slots=True, init=False, eq=False)
class ExternalHookRegistration(Params):
    external_event: str
    matcher: HookMatcher | UnsetType = Unset
    command: tuple[str, ...] | UnsetType = Unset
    python_ref: str | UnsetType = Unset
    timeout_seconds: float | UnsetType = Unset

class ExternalHookAdapter(Protocol):
    def compile(self, profile: ExternalHookProfile) -> tuple[InterceptorRegistration | SignalSink, ...]: ...
```

Exactly one of `command` or `python_ref` is present. `command` is argv and never reaches a shell.
Stdin/stdout payloads are versioned wire DTOs generated from one `Spec`/`Operable` declaration.
Unknown events or fields fail closed at import. `Unset` inherits a profile default; `None` is not
silently interpreted as inheritance.

CLI import/trust, project/global settings, Studio editing, and frontend catalogs all consume the
same backend profile schema and event catalog. Provider-specific events are labeled with their
profile. Studio does not present Claude's `Stop` behavior as a provider-neutral post-response
event.

One `ExternalHookExecutor` owns subprocess execution, internal process-group cleanup,
serialization, deadlines, output bounds, and redaction. The legacy `_make_shell_hook` executor is
translated/deprecated and then deleted.

### D7 — Mechanics move inward; domain types stay with owners

Target placement is conceptual first and may be reached through compatibility modules:

```text
lionagi/
├── protocols/execution/        executable Event/Processor contracts
├── protocols/signals/          Signal base and shared typed facts
├── runtime/dispatch/           interceptor and fan-out kernels/policies
├── session/observation.py      Session façade and routing
├── service/interceptors.py     iModel/service compatibility façade
├── integrations/hooks/         external profiles/executor/provider adapters
├── state/adapters/             signal persistence, message writes, terminal delivery
└── cli/adapters/hooks.py       CLI presentation/import commands
```

The exact folder move is sequenced with the modularity ADR so it does not create new reverse
imports. Compatibility rules:

- current root and module imports remain aliases for one announced deprecation window unless the
  symbol was never public;
- persisted HookSignal and SSE payloads dual-read/dual-project until stored compatibility is
  proven;
- `Broadcaster` becomes a deprecated façade over the fan-out kernel before deletion;
- dormant HookPoint values remain accepted/warned during the same window;
- AgentSpec/CodingToolkit error hooks are either wired into the explicit error stage or formally
  deprecated; they are never silently dropped;
- the process-global hook DataLogger and relative `./data/logs` side effect are removed when the
  service façade moves; observation is injected by composition roots;
- empty nominal Observer/Manager bases receive a downstream impact audit before deprecation.

Deletion happens only after import, runtime, and stored-payload gates pass.

### D8 — ADR-0047 and ADR-0048 are amended for current truth

ADR-0047 retains its central principle: control stays with the narrowest operation owner, and
handler ABIs are not forced into one semantic API. Its amendment must:

- describe three public hook families as examples, not an exhaustive inventory;
- record both blocking HookBus points (`TOOL_PRE`, `USER_PROMPT_SUBMIT`);
- mark lazy Session bus attachment, pre-create replacement consumption, post-transform
  validation, and MCP hook attachment according to current code;
- separate observer recording from governance authorization;
- replace the rejection of shared mechanics with the narrower rejection in this ADR: common
  kernels are allowed, common semantic ownership is not;
- retain current residual errors, including unwired error hooks and direct invocation bypasses.

ADR-0048 keeps `Status: Accepted` and receives an amendment/current implementation table. It
must record the shipped external executor, settings/parser, `li hooks` import/trust commands,
MCP attachment, and conformance suite. Its remaining delta becomes:

- duplicate legacy subprocess executor;
- internal-utility violations in the current executor;
- Studio/backend/frontend/profile schema drift;
- direct FunctionCalling/manager bypass;
- unwired error hooks;
- provider-specific `Stop` vocabulary presented as neutral.

An ADR statement that an implemented feature is absent is corrected before this architecture
uses it as migration evidence.

## Characterization and migration sequence

### Phase 0 — truth and behavior matrix

Amend ADR-0047/0048 and add one matrix covering every current dispatcher except the service
HookRegistry/HookedEvent profile, whose invoke and stream-teardown matrix Phase 1 freezes
separately for the reason given in D2: it is an interceptor with one handler per hook or chunk
key, and the fan-out rows below — registration and invocation order across several handlers,
single-versus-grouped error surfaces, sequential versus concurrent completion — have no subject
there. Characterizing it under this matrix would produce rows describing a shape it does not
have. Phase 1's merge gate covers it, so it is deferred rather than exempt, and the deferral is
stated here so that "every current dispatcher" is not read as a claim this phase does not make:

- registration and invocation order;
- sync and async handlers;
- context mutation/replacement;
- stop, deny, exception, cancellation, and deadline;
- sequential versus concurrent start/completion order;
- tool success/deny/error/cancel sequencing;
- gate-denied audit Signal durability;
- Event completion-event behavior;
- persisted HookSignal JSON and SSE projection;
- message commit/retry/final drain;
- every named DispatchPolicy profile above, including single-versus-grouped error surfaces,
  handler/emitter cancellation, predicate failure, and deadline disposition;
- crash injection before commit, after commit/before observation, and after observation/before
  delivery acknowledgement;
- external profile fixtures and Studio catalog equivalence;
- public import contracts.

No deletion or public rename occurs in Phase 0.

### Phase 1 — kernels behind existing façades

Implement the two kernels with ADR-0119 declarations. Adapt HookBus, Broadcaster,
SessionObserver, MessageManager's sync/async message-added loops, SchedulerSignalBus,
TerminalCallbackRegistry, and service HookRegistry while selecting policies that preserve each
current behavior. A compatibility test must fail if an adapter accidentally adopts another bus's
defaults.

Phase 0's per-profile matrix is a merge gate on this phase, not only a Phase 0 deliverable.
Nothing in Phase 0 forces a consumer to read its own output, so a profile row that is merely
asserted survives it intact, and Phase 1 would then faithfully preserve this record's *belief*
about current behavior instead of the behavior. The gate is therefore stated as a condition on
Phase 1 merges: each named profile row has a characterization test written against pre-migration
code that goes red when the row is wrong, and a row without one blocks the adapter that claims it.
The rows most worth the trouble are the ones this record already distinguishes by mechanism,
where declaration-based classification, `inspect.isawaitable()` after invocation, and the narrower
`asyncio.iscoroutine()` disagree on the same handler.

### Phase 2 — split Session authorization from observation

Compile the old gate into operation interceptors. Make SessionObservation signal-only. Prove that
denials and interceptor failures still reach required audit sinks and that UI subscriber failures
cannot block persistence.

The gate for this phase has three arms, and the third is the one current code fails silently:

1. a gate that returns a denial still persists the audit record;
2. a gate that is absent changes nothing;
3. **a gate that raises still persists the audit record.**

Arm 3 is separate because `SessionObserver.emit` wraps the gate call in
`except Exception: allowed = False` (`lionagi/session/observer.py:235-236`) and then returns at
`:240`, before routes and before subscribers. `bind_db_persistence()` registers the durable write
as a subscriber, so a gate that merely throws suppresses the audit trail with no signal at all.
Fail-closed is correct for authorization and wrong for audit, and a test that only exercises
explicit denial passes while a buggy gate silently empties the record.

### Phase 3 — authoritative operation plans

In coordination with ADR-0121, make ActionExecutor the sole tool owner and compile session,
intrinsic, agent, user, external, and error/post handlers into one ordered plan. Migrate service
HookedEvent in place without changing stream teardown behavior.

### Phase 4 — typed signals and durable ownership

Introduce typed API/tool/lifecycle Signals, dual-project HookSignal compatibility, move message
persistence/retry under StateDB, and use the fan-out kernel inside terminal/scheduler adapters.
The phase cannot land until crash injection proves: pre-commit participant failure leaves neither
outcome nor intent; crash after commit leaves a recoverable intent even if no Signal emitted; and
crash after delivery side effect but before acknowledgement retries under the sink's idempotency
contract without changing source lifecycle.

### Phase 5 — external schema authority

Generate CLI/Studio/frontend/provider projections from ExternalHookProfile. Remove the legacy
executor after configuration translation and the compatibility window.

### Phase 6 — deprecation deletion

Delete façade implementations, dormant values, Broadcaster, arbitrary HookSignal kwargs, and
empty marker bases only when their specific gates and announced windows are complete.

## Consequences

- Maintainers can tell from a type whether a handler controls work, observes a fact, or delivers a
  committed record.
- Two dispatch implementations replace repeated concurrency/ordering/error mechanics without
  creating a universal domain bus.
- Permission cannot suppress audit evidence, and a UI callback cannot mutate an operation.
- StateDB business logic leaves the generic hooks package.
- Existing public and persisted shapes require a migration window, so the final deletion is not
  immediate.
- Domain owners must declare dispatch policy explicitly. That is more configuration than relying
  on a class's hidden default and makes review/test behavior local.
- Some “post hooks” will be reclassified as subscribers when they are only advisory. Callers that
  relied on result mutation must use a typed interceptor stage.
- Durable delivery remains intentionally more complex than in-process fan-out because retry,
  acknowledgement, and post-commit truth cannot be abstracted away safely.

## Alternatives considered

### One universal EventBus/MessageBus

It would reduce class count but require one failure, ordering, mutation, and durability model.
Every available choice breaks at least one current contract: fail-closed security, isolated UI
observation, sequential transforms, concurrent terminal delivery, or executable Event completion.
Rejected.

### Keep every dispatcher independent and only improve naming

This preserves behavior with minimal migration risk. It leaves duplicated raw concurrency,
deadline, cancellation, registration, and reporting code, which continue drifting. Two mechanical
kernels are small enough to share without moving semantic ownership. Rejected as the end state.

### Make SessionObserver the universal runtime

It already has typed filtering, routing, Flow storage, and persistence bindings. It also has the
authorization/audit coupling this ADR must remove, and scheduler/terminal domains do not need its
Flow or route semantics. A lighter common dispatcher under it is preferable.

### Replace TerminalCallbackRegistry with Broadcaster

Broadcaster lacks entity filters, override rules, shared deadline, sync offload, cancellation, and
post-commit ordering. Adding them would move lifecycle policy into a generic singleton. Only
fan-out mechanics are shared.

### Delete apparently unused APIs immediately

Broadcaster, dormant points, error-hook fields, and nominal observer bases have no or few internal
consumers. They are public, persisted, or configuration-visible. Characterization plus a
deprecation window costs less than an unmeasured ecosystem break.

### Treat provider-native hook/tool telemetry as internal authorization

Telemetry arrives after the provider subprocess acts unless the provider exposes a true approval
callback. Labeling it as permission evidence would be false. External adapters report whether a
control was actually enforceable; ADR-0121 owns that contract.
