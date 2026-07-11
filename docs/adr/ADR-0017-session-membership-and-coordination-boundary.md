# ADR-0017: Session membership and coordination boundary

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: session-branch
- **Date**: 2026-07-09
- **Relations**: none

## Context

`Session` coordinates a set of conversation branches. It gives those branches shared operations,
observation, hooks, a default memory store, and mailboxes without becoming a second owner of their
messages or logs. Six concrete problems define the boundary.

**P1 — membership must have one owner.** Two sessions attaching different observers, operation
registries, or mailboxes to one Branch would make behavior depend on the last write to private
fields. Membership therefore has an explicit owner id, batch preflight, removal protocol, and
remove-then-include reparenting path (`lionagi/session/session.py`).

**P2 — defaults must be shared without replacing explicit branch resources.** A session needs one
default branch and one default memory store, while a branch constructed with its own backend must
keep that backend. Constructor validation and post-validation memory rewiring make those rules hold
even when branches are supplied during model construction.

**P3 — in-process events need a deterministic dispatch order.** Governance checks, event history,
named routes, and subscribers have different effects. `SessionObserver.emit()` fixes their order as
gate, store, route, dispatch so denied events remain observable without reaching routes or handlers
(`lionagi/session/observer.py`).

**P4 — lifecycle hooks and signal persistence are related but not the same transport.** HookBus
executes closed hook points with handler-specific failure policy and records most hook emissions on
the observer. The observer's database-binding helper currently embeds StateDB serialization,
payload bounds, connection handling, and best-effort persistence; that is shipped behavior but an
identified layering delta.

**P5 — graph execution needs membership but is not membership.** Flow execution clones branches,
schedules dependencies, limits concurrency, and supports reactive mutation. Session resolves a
starting branch and delegates to `lionagi/operations/flow.py`; the executor registers clones through
Session so the membership invariants remain centralized.

**P6 — a default mailbox must not imply delivery guarantees it does not implement.** Every Session
constructs an `Exchange`, but messages move only when `collect`, `sync`, or the explicit `run` loop is
called. Exchange is process-local, excluded from serialization, and not automatically paired with a
Messenger tool.

| Concern | Decision |
|---------|----------|
| Membership and default branch | D1: Session exclusively owns membership and performs explicit attach/detach. |
| Shared operation and memory defaults | D2: member branches share the Session operation registry and adopt Session memory only when empty. |
| Event observation | D3: `SessionObserver` is the canonical in-process gate, history, router, and subscriber dispatcher. |
| Hooks and signal persistence | D4: HookBus remains a distinct hook executor; current DB binding is best-effort observer integration. |
| Graph execution | D5: `flow()` and `flow_stream()` are convenience delegates, not the execution kernel. |
| Branch mailboxes | D6: Exchange is explicitly pumped, non-durable, and non-serialized. |

This ADR deliberately does **not** decide:

- operation-graph scheduling, dependency, spawning, pause/resume, or reactive-mutation algorithms;
  those belong to the operations and orchestration areas;
- persistent signal-table schemas, recovery, or streaming delivery; those belong to persistence and
  Studio-facing contracts;
- action authorization policy; this ADR records only how the optional observer gate is consulted;
- provider, model, message, log, capability, or per-turn execution semantics owned by Branch and the
  adjacent areas;
- a durable interbranch communication product; D6 records the compatibility facility that exists
  today, and the delta records the unresolved posture.

## Decision

### D1 — Session owns exclusive membership and default-branch selection

The shipped model fields are (`lionagi/session/session.py`):

```python
class Session(Node, Relational):
    branches: Pile[Branch] = Field(
        default_factory=lambda: Pile(item_type={Branch}, strict_type=False)
    )
    exchange: Exchange = Field(default_factory=Exchange, exclude=True)
    default_branch: Any = Field(default=None, exclude=True)
    name: str = Field(default="Session")
    user: SenderRecipient | None = None

    _operation_manager: OperationManager = PrivateAttr(default_factory=OperationManager)
    _observer: Any = PrivateAttr(default=None)
    _hooks: Any = PrivateAttr(default=None)
    _memory: MemoryStore | None = PrivateAttr(default=None)

    def __init__(self, *, memory: MemoryStore | None = None, **kwargs: Any): ...
```

The after-model validator fabricates a fresh empty Branch as `default_branch` whenever the
`default_branch=` argument itself is absent — supplying `branches=[...]` alone does not suppress
it, so `Session(branches=[my_branch])` ends up with two branches and the auto-created one as the
default; only an explicit `default_branch=my_branch` yields a single-branch Session with the
caller's branch as default. The validator then includes the default in `branches` and passes the
complete pile through `include_branches()`. The `observer` accessor is lazy as an accessor, but ordinary
Session construction immediately forces it because inclusion assigns `branch._observer =
self.observer`. HookBus remains genuinely lazy until `Session.hooks` is read.

Membership entry points are:

```python
async def ainclude_branches(self, branches: ID[Branch].ItemSeq): ...
def include_branches(self, branches: ID[Branch].ItemSeq): ...

def new_branch(
    self,
    system: System | JsonValue = None,
    system_sender: SenderRecipient = None,
    system_datetime: bool | str = None,
    user: SenderRecipient = None,
    name: str | None = None,
    messages: Pile[RoledMessage] = None,
    tools: Tool | Callable | list = None,
    as_default_branch: bool = False,
    **kwargs,
) -> Branch: ...

def remove_branch(self, branch: ID.Ref, delete: bool = False): ...
def split(self, branch: ID.Ref) -> Branch: ...
def get_branch(self, branch: ID.Ref | str, default: Any = ..., /) -> Branch: ...
def change_default_branch(self, branch: ID.Ref): ...
```

`include_branches()` first materializes the supplied Branch objects and checks the whole batch. If
any candidate has a non-`None` `_owning_session_id` different from this Session id, it raises
`ValueError` before changing any candidate. The all-or-nothing guarantee is specifically for this
ownership preflight; the method is not a general transaction across arbitrary failures in later
attachment code.

For every accepted branch, inclusion performs this exact sequence:

1. Include it in `branches` when absent.
2. Set `_owning_session_id` to the Session id and set `branch.user` to the Session id.
3. Replace the branch operation manager with the Session manager.
4. Attach the Session observer.
5. Attach HookBus only when `_hooks` has already been initialized.
6. Assign Session memory only when `branch._memory is None`.
7. Register an Exchange mailbox when one is absent.
8. Make the branch default only when no default exists.

Including an already owned member in the same Session is permitted and repairs missing attachments
or mailbox registration. It does not duplicate the Branch in the pile.

`get_branch()` first attempts id normalization and pile lookup. If that fails and the input is a
string, it scans branch names in pile order and returns the first exact match. A miss raises
`ItemNotFoundError` unless the positional `default` argument was supplied, in which case that value
is returned. `change_default_branch()` accepts only a reference already resolvable through the pile;
a missing reference fails at pile lookup rather than implicitly including a new branch.

Removal performs the inverse coordination teardown:

```text
branches.exclude(branch)
exchange.unregister(branch.id)
branch._owning_session_id = None
branch._observer = None
branch._hooks = None
branch._operation_manager = OperationManager()
branch.user = None                 # only when it still equals this Session id
```

If the removed branch was default, the first remaining branch becomes default or the default becomes
`None` when the pile is empty. Messages, progression, models, tools, logs, metadata, providers,
capabilities, and adopted memory stay on the branch. `delete=True` only deletes the method's local
reference after detachment; it cannot destroy an object retained by another Python reference.

Inclusion also overwrites `branch.user` with the Session id. Removal clears that field only when it
still equals the removing Session id; it does not retain and restore an earlier conversational user
value. Code that needs a durable end-user identity must therefore keep it outside this attachment
field or restore it explicitly after detachment.

An absent id raises `ItemNotFoundError`. Reparenting is deliberately remove from the old Session,
then include in the new one. `split()` clones the selected Branch and includes the clone in the same
Session, so clone coordination uses this same attachment path.

The serialized Session includes the public branch pile, name, user, and inherited Node fields.
`exchange` and `default_branch` are explicitly excluded; private operation, observer, hook, and
memory objects are also absent. Deserialization reconstructs coordination through validation rather
than restoring live references.

### D2 — Shared operations and memory are defaults with different detach behavior

#### Operation registry

Session operation methods are:

```python
def register_operation(
    self,
    operation: str,
    func: Callable,
    *,
    update: bool = False,
): ...

def operation(self, name: str = None, *, update: bool = False): ...

async def run_operation(
    self,
    operation: str,
    *,
    branch: Branch | ID.Ref | None = None,
    **kwargs: Any,
) -> Any: ...
```

The decorator uses the explicit name or the function's `__name__`. `OperationManager.register()`
rejects non-async functions and duplicate names unless `update=True`
(`lionagi/operations/manager.py`). `run_operation()` selects the supplied Branch or Session default,
resolves string/UUID references through the branch pile, and calls `branch.get_operation()`. Branch
attributes take precedence over registry entries; an unknown name raises `ValueError`.

All current members share the one manager by identity. A branch included before registration sees a
later registration immediately. Removal installs a fresh empty manager, so session-registered names
do not leak into standalone or reparented operation lookup.

#### Memory default

```python
@property
def memory(self) -> MemoryStore:
    if self._memory is None:
        self._memory = InMemoryStore()
    return self._memory
```

The Session has no public memory setter. Inclusion gives this one instance to every branch whose
private memory slot is still `None`. An explicit or previously adopted branch store wins.

Pydantic after-validation runs before the custom `Session.__init__()` body. A constructor-supplied
branch can therefore cause the validator to lazily create a temporary Session store and attach it.
When the caller also supplies `memory=explicit_store`, the constructor replaces `_memory` and
rewires only branches still pointing to that temporary object. Branches with independent stores are
not rewritten.

Removal does not clear memory. A default adopted from one Session therefore remains the branch's
store during standalone use and after reparenting. This is the same first-claim rule recorded from
the Branch side in ADR-0016.

### D3 — SessionObserver dispatches gate, store, route, then subscribers

The observer's concrete state and public surface are (`lionagi/session/observer.py`):

```python
Handler = Callable[[Any, SessionObserver], Any]
Predicate = Callable[[Any], bool]
Gate = Callable[[Any], Any]

class SessionObserver(Observer):
    def __init__(self, session: Any = None) -> None:
        self.session = session
        self.flow: Flow = Flow(name="session-events")
        self._subs: list[tuple[Filter, Handler]] = []
        self._routes: list[tuple[Predicate, str]] = []
        self._gate: Gate | None = None

    def observe(self, *keys, handler=None, role: str | None = None) -> Any: ...
    def unobserve(self, handler: Handler) -> int: ...
    def route(self, condition: Predicate, *, into: str) -> SessionObserver: ...
    def gate(self, check: Gate) -> SessionObserver: ...
    async def authorize(self, action: Any) -> bool: ...
    async def emit(self, event: Any) -> list[Any]: ...
    def stream(self, name: str) -> list[Any]: ...
    def by_type(self, event_type: type) -> list[Any]: ...
```

#### Emission semantics

1. A non-`Observable` input is wrapped as `Signal(data=input)`. Filters and the gate normally see
   `Signal.data`; role filters inspect the envelope.
2. If a gate exists, it is awaited when necessary and coerced to `bool`. A gate exception is treated
   as denial.
3. The event is added to `flow` whether allowed or denied.
4. Denial returns `[]`; no named route or subscriber runs.
5. Allowed route predicates run in registration order. Matching events are appended to a lazily
   created named `Progression`.
6. Subscribers run in registration order. Synchronous results are collected immediately;
   awaitables are then awaited concurrently. The return shape is synchronous results followed by
   asynchronous results.

Raw route and subscriber exceptions are not globally swallowed by `emit()`. Isolation belongs to
the caller that needs it: Branch lifecycle paths use `_safe_emit()` so observer failures cannot
change an operation result, HookBus records best-effort, and the bound DB subscriber swallows its
own persistence failures.

`observe()` AND-composes multiple conditions. With `role=`, the payload condition and envelope role
must both match. With no condition and no role, it raises `TypeError`. `unobserve()` removes all
registrations of the exact handler object and returns the removal count. An absent named stream is
read as `[]`.

#### Authorization semantics

`authorize(action)` returns `True` when no gate is installed. A falsy result or gate exception is a
denial: it stores `GateDenied(data=action)` directly in observer history and returns `False`.
Authorization does not route or dispatch that `GateDenied` through subscribers. Branches call this
method before guarded actions; standalone Branch authorization returns `True` without an observer.

The observer is process-local. Its `Flow` is an in-memory event history, not a restart or delivery
guarantee.

### D4 — HookBus is distinct; observer DB persistence is best-effort

`Session.hooks` constructs one bus with `build_session_bus(observer=self.observer)` and caches it
(`lionagi/hooks/loader.py`). The closed hook vocabulary is (`lionagi/hooks/bus.py`):

```python
class HookPoint(str, Enum):
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    BRANCH_CREATE = "branch.create"
    API_PRE_CALL = "api.pre_call"
    API_POST_CALL = "api.post_call"
    API_STREAM_CHUNK = "api.stream_chunk"
    TOOL_PRE = "tool.pre"
    TOOL_POST = "tool.post"
    TOOL_ERROR = "tool.error"
    MESSAGE_ADD = "message.add"
    ARTIFACT_CREATED = "artifact.created"

class HookSignal(Signal):
    point: HookPoint | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)
```

Declaration does not imply automatic emission. The current wiring is:

| Hook point | Production emission path | Default handler |
|------------|--------------------------|-----------------|
| `SESSION_START` | CLI run-persistence setup | `persist_session_start` |
| `SESSION_END` | CLI run-persistence teardown | `persist_session_end` |
| `BRANCH_CREATE` | CLI run-persistence setup | `persist_branch_provenance` |
| `TOOL_PRE` / `TOOL_POST` / `TOOL_ERROR` | action invocation | none |
| `MESSAGE_ADD` | `route_message_persistence()` attaches `Branch._persist_via_bus()` as an async message callback | `persist_message` |
| `API_PRE_CALL` / `API_POST_CALL` / `API_STREAM_CHUNK` | no production emitter | none |
| `ARTIFACT_CREATED` | no production emitter | none |

The no-emitter points remain valid registration and manual-emission values; they are not promises
that provider or artifact code fires them. String inputs to `on()`, `off()`, `handlers_for()`, and
`emit()` are normalized through `HookPoint(value)` and an unknown string raises `ValueError`.
Merely assigning or constructing a HookBus does not emit `MESSAGE_ADD`; the persistence router in
`lionagi/hooks/persist.py` must also append the async Branch callback. That router removes the
generic default `persist_message` handler and installs a branch-demultiplexed handler for the
supplied persistence callback.

Once that async callback is installed, the synchronous `MessageManager.add_message()` path rejects
the call with `RuntimeError` before mutating the message pile; callers must use
`a_add_message()`, which awaits registered callbacks. This constraint is enforced by
`lionagi/protocols/messages/manager.py` and is why persistence wiring happens only in an async
lifecycle after construction-time synchronous system-message setup.

`TOOL_PRE` uses `blocking_emit`: handlers run sequentially, ordinary exceptions propagate, and
`StopHook` skips remaining handlers. Other points run sequentially, log and isolate ordinary handler
exceptions, and also honor `StopHook`. Most emissions record a best-effort `HookSignal` on the
observer after handlers. `MESSAGE_ADD` omits that record because `MessageAdded` already represents
the same message on the signal bus.

The default bus installs persistence-related handlers, but bus creation does not retroactively set
`branch._hooks` on branches already included. Later inclusion does attach the initialized bus. Other
persistence setup code may explicitly bind a bus and message callback; merely reading
`Session.hooks` is not a complete persistence lifecycle. This asymmetry is retained as a delta rather
than described as an intentional guarantee.

The observer also ships this direct persistence seam:

```python
def bind_db_persistence(self, session_id: str, db: Any = None) -> None: ...
def unbind_db_persistence(self) -> None: ...
```

For every `Signal`, the subscriber writes `session_id`, concrete signal class name, optional
`op_id`, current epoch timestamp, and a sanitized payload through
`StateDB.insert_session_signal()`. When a DB is supplied it is reused. Otherwise the subscriber opens
`DEFAULT_DB_PATH` per event only if that path exists. All persistence exceptions are swallowed.

Payload sanitization has these exact rules:

- base Signal identity/timestamp/data/role fields are not blindly promoted;
- `MessageAdded.data` becomes a compact `message_ref` containing available id, role, sender, and
  recipient;
- other data becomes `model_dump()` for Pydantic models or a string/repr fallback;
- JSON serialization uses safe fallback and produces JSON-native values;
- if that serialization gate still raises, the payload becomes
  `{sanitize_error: repr(signal)[:256]}`;
- the stored payload column is capped at **16,384 bytes**; oversized data becomes
  `{truncated: true, original_bytes: N, data: clipped_text}`;
- fitting uses at most **8** measured shrink iterations, then falls back to an empty data string.

Cap enforcement itself is best-effort: an unexpected exception in the measurement/truncation block
is swallowed and returns the already JSON-safe payload, which may then exceed 16,384 bytes. The
256-character fallback bounds a last-resort diagnostic and the source records no historical reason
for exactly 256. The recorded reason for 16,384 bytes is to bound the normal payload column while
leaving a truncation marker; the source records no historical evidence for choosing exactly 16 KiB.
The eight-iteration guard is a termination bound; its exact value also has no recorded empirical
rationale. The cap is not an SSE-frame cap because transport envelope bytes are added later.

### D5 — Flow methods delegate to the operation graph kernel

Session exposes (`lionagi/session/session.py`):

```python
async def flow(
    self,
    graph: Graph,
    *,
    context: dict[str, Any] | None = None,
    parallel: bool = True,
    max_concurrent: int = 5,
    verbose: bool = False,
    default_branch: Branch | ID.Ref | None = None,
    alcall_params: Any = None,
    on_progress: Any = None,
    reactive: bool = False,
    spawn_type: type | None = None,
    node_builder: Any = None,
    max_spawn: int = 50,
    executor_ref: dict[str, Any] | None = None,
    on_branch_created: Callable[[Any], None] | None = None,
    spawn_branch_setup: Callable[[Any, Any], None] | None = None,
) -> dict[str, Any]: ...

async def flow_stream(
    self,
    graph: Graph,
    *,
    context: dict[str, Any] | None = None,
    max_concurrent: int = 5,
    verbose: bool = False,
    default_branch: Branch | ID.Ref | None = None,
    alcall_params: Any = None,
    spawn_type: type | None = None,
    node_builder: Any = None,
    max_spawn: int = 50,
): ...
```

Both select the supplied default or Session default and resolve string/UUID references through the
branch pile. `flow()` returns the operation module's result; `flow_stream()` yields its events.
Scheduling, dependency validation, capacity limiting, branch preallocation, and reactive spawn
remain in `lionagi/operations/flow.py`.

The executor uses `session.include_branches(clone)` for preallocated and reactive clones, so every
clone receives ownership, observer, operation registry, memory default, mailbox, and any already
initialized HookBus through D1. The Session methods do not reproduce that wiring.

The default concurrency limit is **5** and default reactive spawn limit is **50**. These values are
forwarded unchanged and have no rationale recorded in the Session module; their tuning belongs to
the operation-graph contract. The `parallel` flag exists only on the non-streaming convenience
method.

### D6 — Exchange is an explicit, process-local mailbox router

Every Session constructs one excluded `Exchange`. Its state and public routing surface are
(`lionagi/session/exchange.py`):

```python
OUTBOX = "outbox"

class Exchange(Element):
    flows: Pile[Flow[Message, Progression]] = None
    _owner_index: dict[UUID, UUID] = PrivateAttr(default_factory=dict)
    _stop: bool = PrivateAttr(default=False)

    def register(self, owner_id: UUID) -> Flow[Message, Progression]: ...
    def unregister(self, owner_id: UUID) -> Flow[Message, Progression] | None: ...
    def get(self, owner_id: UUID) -> Flow[Message, Progression] | None: ...
    def has(self, owner_id: UUID) -> bool: ...
    @property
    def owner_ids(self) -> list[UUID]: ...
    def send(
        self,
        sender: UUID,
        recipient: UUID | None,
        content: Any,
        channel: str | None = None,
    ) -> Message: ...
    async def collect(self, owner_id: UUID) -> int: ...
    async def collect_all(self) -> int: ...
    async def sync(self) -> int: ...
    async def run(self, interval: float = 1.0) -> None: ...
    def stop(self) -> None: ...
    def receive(self, owner_id: UUID, sender: UUID | None = None) -> list[Message]: ...
    def pop_message(self, owner_id: UUID, sender: UUID) -> Message | None: ...
```

Session exposes only the explicit-pump subset as direct delegates
(`lionagi/session/session.py`):

```python
def register_participant(self, entity_id: UUID) -> Flow[Message, Progression]: ...
def send(
    self,
    sender: UUID,
    recipient: UUID | None,
    content: Any,
    channel: str | None = None,
) -> Message: ...
def receive(self, owner_id: UUID, sender: UUID | None = None) -> list[Message]: ...
def pop_message(self, owner_id: UUID, sender: UUID) -> Message | None: ...
async def collect(self, owner_id: UUID) -> int: ...
async def sync(self) -> int: ...
```

There is no Session delegate for `Exchange.run()` or `stop()` and Session never starts the loop.

Each owner gets one Flow with an `outbox` progression. Delivered messages enter a progression named
`inbox_<sender-uuid>` in the recipient Flow.

**Exact routing semantics:**

- Duplicate registration raises `ValueError`; unregistering an unknown owner returns `None`.
- Unregistering a known owner removes its entire Flow, including queued outbox and delivered inbox
  messages; re-registering the UUID creates a new empty mailbox.
- `send()` queues one `Message` in the sender outbox and raises `ValueError` when the sender is not
  registered. It does not deliver immediately; the optional channel is stored on the Message but
  does not change Exchange routing.
- `collect(owner)` removes queued messages under the Exchange lock, then performs recipient
  deliveries concurrently outside that lock.
- A direct message to a currently registered recipient is delivered. A direct message to an
  unregistered recipient is removed from the outbox and dropped.
- A broadcast creates a model copy for every registered owner except the sender. Delivery to an
  owner unregistered between collection and delivery is a no-op.
- Per-delivery exceptions are gathered as results and not re-raised. The returned count is the
  number of unique source message ids scheduled for delivery, not a confirmation count, so one
  broadcast counts as one even with many copies.
- An empty outbox, a direct message whose recipient is no longer registered, or a broadcast with no
  other registered owners returns zero after consuming any queued source message involved.
- `collect_all()` iterates a snapshot of owner ids and skips owners that disappear. `sync()` is an
  alias for `collect_all()`.
- `receive()` is non-destructive and optionally filters by sender. Unknown owners return `[]`.
  `pop_message()` is FIFO for one sender and returns `None` for an unknown owner, absent inbox, or
  empty inbox.
- `run()` is an opt-in polling loop; `stop()` only sets its in-memory flag. Its default **1.0-second**
  interval has no recorded rationale. Session does not start this loop.

The adjacent `LionMessenger` requires explicit `LionMessenger(exchange)` construction and
`bind(branch, roster, ...)` before it yields a Tool (`lionagi/tools/communication/messenger.py`).
The default Session does not provision or bind it. Exchange state, pending mail, the owner index, and
the pump are not serialized, recovered, or acknowledged durably.

## Consequences

- Branch ownership, shared operations, observer wiring, memory adoption, and mailbox registration
  have one lifecycle. Reparenting is explicit and testable.
- Membership currently reuses `Branch.user` as Session identity and does not restore its prior
  value. This is observable after detachment and must be accounted for by callers that separately
  track a conversational user.
- Branch conversation data remains usable after removal, but session operation names, subscriptions,
  routes, hooks, and mailbox contents do not follow it automatically.
- Observer history makes denied and allowed emissions inspectable in process. It does not make raw
  subscriber failures best-effort; callers must choose the appropriate isolation boundary.
- HookBus and SessionObserver can interoperate without pretending to be the same abstraction. The
  cost is two related registration surfaces and current persistence wiring in the wrong layer.
  Several declared HookPoints are extension vocabulary only and have no production emitter.
- Flow-created clones obey normal membership rules, while Session stays independent of graph
  scheduling algorithms.
- Exchange is useful for explicitly coordinated in-process mail, but callers must pump it and accept
  loss on restart, removal, unknown recipients, or ignored delivery errors.
- Reversing D1–D2 would require a branch-ownership and backend migration. D3–D4 would affect event
  ordering and integrations. D5 is cheap to move mechanically but would increase Session coupling.
  Reversing D6 requires a separate delivery and recovery protocol, not an internal refactor.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Extract StateDB signal persistence from `SessionObserver` into a persistence-owned subscription adapter; acceptance requires the observer module to contain no StateDB construction or payload-size policy while CLI and Studio retain best-effort writes, payload bounds, and unbind behavior. | S | (filled at issue-open time) |
| 2 | Move versioned signal and loop-control vocabulary to a neutral low-level module with compatibility re-exports from `lionagi.session`; acceptance requires unchanged schema versions, serialized payloads, `lane_for()` results, dispatch envelopes, and public import behavior throughout the relocation. | M | (filled at issue-open time) |
| 3 | Choose and implement one Exchange product posture: provision Messenger, collection, durability, and recovery as an end-to-end supported path, or make Exchange opt-in and deprecate the default Session mailbox; acceptance requires documentation and integration tests that demonstrate the selected lifecycle. | M | (filled at issue-open time) |
| 4 | Define and implement HookBus initialization for existing members; acceptance requires either retroactive attachment to every current branch or an explicit non-attachment contract, plus tests covering branches included both before and after first access to `Session.hooks`. | S | #1964 |

## Alternatives considered

### Allow one Branch to belong to multiple Sessions

This would let several coordinators observe or operate on one conversation without cloning. It loses
because the Branch has one observer, one hook reference, one owning id, and one operation-manager
slot. Multi-membership would make attachment order semantic or require replacing those fields with a
new multiplexing layer.

### Mutate a batch as each branch passes ownership validation

This is simpler than preflight. It loses because a later owned branch would leave earlier candidates
partially claimed. Whole-batch ownership validation buys a clear retry boundary with little cost.

### Always replace branch memory with the Session store

This would make every member share one backend and simplify cross-branch reads. It loses because an
explicit or already-used backend may contain branch data. Silent replacement would disconnect that
data and make reparenting destructive. The first-claim rule gives callers an explicit choice at
Branch construction or before first memory access.

### Clear adopted memory on removal

This would make detached branches return to a uniform empty standalone state. It loses because the
adopted store may now contain the branch's durable data and may be intentionally shared. Coordination
attachments are reversible; adopted storage is treated as branch state once assigned.

### Move graph execution into Session

This would make `Session.flow()` self-contained. It loses because dependency scheduling, capacity
limits, reactive mutation, and operation execution would combine with ownership, observer, and
mailbox lifecycle. The delegate retains a narrow integration point: branch resolution in, normal
membership for clones, result out.

### Merge Observer, HookBus, and Exchange into one transport

One bus would reduce the number of names. It loses because the three have incompatible contracts:
Observer matches typed payloads and stores history, HookBus executes closed lifecycle points with
ordered failure policies, and Exchange holds addressed messages until an explicit pump. A common
class would hide rather than remove those distinctions.

### Dispatch before storing the observer event

This would avoid retaining events that handlers never consume. It loses diagnostic history on gate
denial and makes reentrant handlers observe an event stream that does not yet contain the event they
are processing. Store-before-route fixes that ordering.

### Fail open when an observer gate raises

This would keep event delivery running through gate defects. It loses the meaning of a governance
check: a broken check would silently permit the action or event. Current observer gates treat raised
checks as denial, while lifecycle-observer failures after authorization remain best-effort.

### Make Exchange delivery automatic and durable inside Session

This would make the default mailbox an end-to-end communication system. It requires pump ownership,
restart recovery, acknowledgements, recipient lifecycle, and a persistent schema that do not exist.
The shipped Exchange stays explicit; the product-level choice is retained in the delta instead of
being implied by construction.

### Keep StateDB serialization inside SessionObserver permanently

This buys a one-call binding API and is the organic shape currently shipped. It loses layering:
observer code now owns a database path, row payload policy, size cap, and connection lifecycle. The
retrospective contract records the behavior so extraction can preserve it; the delta assigns the
adapter to persistence rather than silently rewriting the current truth.

## Notes

```text
                         Session
              membership and shared defaults
                  /          |          \
            Branches     Observer      Memory
               |          + Hooks       default
               +---- shared operations ----+
               |                            |
          flow delegates                Exchange
               |                      explicit pump
               v
       operation graph execution kernel
```
