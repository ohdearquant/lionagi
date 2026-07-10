# ADR-0003: In-Process Event Execution Lifecycle

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: core-data-model
- **Date**: 2026-07-09
- **Relations**: extends ADR-0001

## Context

API calls and executable operations need a shared in-process representation of pending work and its
outcome. Six concrete problems determine the shipped Event, Processor, and Executor lifecycle.

**P1 — Work needs one inspectable outcome shape.** Callers need to distinguish work that has not
started, is running, succeeded, failed, was denied, was cancelled, or was aborted, while retaining
duration, response, error, and retryability information on the work item.

**P2 — Business failures and cancellation do not have the same propagation contract.** Ordinary
operation and provider errors should be captured on the Event so a batch processor can continue.
Cancellation, keyboard interruption, and other base exceptions must still unwind the controlling
task after recording state.

**P3 — Waiters need a completion signal without eagerly binding Events to one loop.** Events are
often created before a waiter exists. A lazily created process-local signal avoids allocating an
`asyncio.Event` until needed while still waking waiters for every terminal status.

**P4 — Queueing needs capacity, optional concurrency limits, and explicit denial behavior.** A
processor may admit, terminally reject, or temporarily defer an Event. Deferred events must not be
dropped or busy-spin, and a bounded queue needs a non-blocking enqueue path.

**P5 — Live ownership must be separate from the Event's state fields.** Executor retains live Event
objects by UUID and a pending order, lazily creates the configured Processor, and exposes status
views without turning Event into a durable scheduler record.

**P6 — Durable delivery has a different state machine.** The outbox persists delivery attempts,
acknowledgements, expiry, retries, and dead-letter outcomes. It does not rehydrate or resume Event
objects, whose completion primitive and arbitrary response values are process-local (see the
persistence-state ADR on durable dispatch lifecycle).

The defining modules are `lionagi/protocols/generic/event.py` and
`lionagi/protocols/generic/processor.py`. Current consumers include
`lionagi/service/connections/api_calling.py`, `lionagi/service/imodel.py`, and
`lionagi/operations/node.py`.

| Concern | Decision |
|---|---|
| Outcome vocabulary and payload | D1: Event owns mutable Execution state with seven statuses and five terminal statuses. |
| Invocation and streaming | D2: The Event wrapper captures ordinary exceptions as failed, re-raises base exceptions as cancelled, and records duration in all started paths. |
| Completion, observation, and reuse | D3: Terminal assignment signals a lazy local event; state serializes for observation, cannot rehydrate, and can be cloned as fresh work. |
| Queue processing | D4: Processor applies capacity, optional semaphore concurrency, permission, terminal denial, and deferral policy in memory. |
| Live event ownership | D5: Executor stores Events in a typed Pile and pending UUIDs in a Progression, forwarding them to a lazily created Processor. |
| Persistence boundary | D6: Durable delivery keeps its own persisted lifecycle rather than adopting EventStatus. |

This ADR deliberately does **not** decide:

- provider rate-limit numbers, retry policy, or endpoint payloads; Processor accepts policy inputs
  and provider-specific processors own those values;
- operation-graph dependency scheduling; Operation reuses Event state, while the operations area
  owns graph execution;
- durable acknowledgement, expiry, retry, or dead-letter transitions; those belong to the
  persistence-state durable-dispatch contract; or
- hook execution and hook timeout behavior; HookedEvent layers that concern above Event.

## Decision

### D1 — Event owns a seven-state mutable Execution payload

**The contracts** (`lionagi/protocols/generic/event.py`):

```python
class EventStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    ABORTED = "aborted"
```

```python
class Execution:
    __slots__ = ("status", "duration", "response", "error", "retryable")

    def __init__(
        self,
        duration: float | None | UnsetType = Unset,
        response: Any = None,
        status: EventStatus = EventStatus.PENDING,
        error: str | BaseException | None = None,
        retryable: bool | None | UnsetType = Unset,
    ) -> None: ...

class Event(Element):
    execution: Execution = Field(default_factory=Execution)
    streaming: bool = Field(False, exclude=True)
```

The terminal set is exactly:

```python
frozenset({
    EventStatus.COMPLETED,
    EventStatus.FAILED,
    EventStatus.SKIPPED,
    EventStatus.CANCELLED,
    EventStatus.ABORTED,
})
```

**Exact semantics**:

- A new Event is `pending`; duration and retryability use the internal `Unset` sentinel, response
  and error are `None`, and `streaming` is false.
- `Event.status` returns `execution.status`. Its setter accepts an EventStatus or one of the seven
  legal strings; an unknown string or other type raises `ValueError`.
- The setter validates membership but does not enforce a transition graph. Callers can assign any
  legal status from any other status. Direct assignment to `execution.status` bypasses both setter
  validation and completion signalling.
- `response` is a direct property proxy to `execution.response`. Base `request` returns `{}`;
  subclasses provide the permission/request metadata used by processors.
- `retryable` is descriptive state only. Event and Processor do not automatically retry based on
  it.
- `Event.q.status`, `.duration`, `.response`, `.error`, and `.retryable` produce field references to
  the nested `execution.*` paths for query expressions.

Execution is a mutable slot object rather than a second Pydantic model because it is updated
throughout one live invocation and serialized through Event's dedicated field serializer.

### D2 — Invoke and stream wrap subclass work with total ordinary-failure capture

Subclasses implement `_invoke` and optionally `_stream`; the base wrapper owns state transitions.

**The contract** (`lionagi/protocols/generic/event.py`):

```python
async def invoke(self) -> None: ...
async def _invoke(self) -> Any: ...

async def stream(self): ...
async def _stream(self): ...
```

Invoke transitions are:

```text
pending -> processing -> completed   (_invoke returned)
pending -> processing -> failed      (_invoke raised Exception)
pending -> processing -> cancelled   (_invoke raised BaseException outside Exception)
non-pending -> unchanged             (invoke is a no-op)
```

**Exact invoke semantics**:

- Only `pending` starts. Every other status, including `processing`, returns immediately without
  changing response, error, duration, or completion state.
- Start assigns `execution.status = processing` and records the current UTC timestamp.
- A normal `_invoke` return is stored as response and status is assigned `completed` through the
  property setter.
- An ordinary `Exception` is not re-raised. Status becomes `failed` and the exception is accumulated
  by `Execution.add_error`.
- A `BaseException` not caught by `Exception` is accumulated, status becomes `cancelled`, and the
  same exception is re-raised. This includes asyncio/AnyIO cancellation classes and process-control
  exceptions on the running Python version.
- Duration is overwritten in `finally` with elapsed wall-clock seconds for every started path,
  including failure and cancellation. A no-op invocation retains its earlier duration.
- Calling the bare Event's `_invoke` captures its `NotImplementedError` as a failed Event rather than
  raising to the caller.

**Exact stream semantics**:

- Stream returns without yielding when status is any terminal value. Unlike invoke, a non-terminal
  `processing` status does not block a new stream attempt; the wrapper assigns `processing` again.
- Every `_stream` chunk is yielded unchanged. Chunks are not accumulated into `response` by the base
  class.
- Exhausting the async generator assigns `completed`. An ordinary exception after any yielded
  chunks is captured as `failed` and iteration ends without re-raising it. A base exception records
  `cancelled` and is re-raised.
- Duration is recorded when generator execution closes through normal completion, captured failure,
  or cancellation.
- Subclasses that override `invoke` or `stream` directly bypass these guarantees. Compatibility
  tests retain that extension shape, so the wrapper cannot enforce duration or completion
  signalling for a direct override.

This split lets batch processing treat ordinary business failure as data while preserving
cancellation as control flow.

### D3 — Completion is local and one-shot; serialization is observational

**The contract** (`lionagi/protocols/generic/event.py`):

```python
_completion_event: asyncio.Event | None = PrivateAttr(default=None)

@property
def completion_event(self) -> asyncio.Event: ...

@classmethod
def from_dict(cls, data: dict) -> Event:
    raise NotImplementedError("Cannot recreate an event once it's done.")

def assert_completed(self) -> None: ...
def as_fresh_event(self, copy_meta: bool = False) -> Event: ...
```

The serialized execution payload is always five keys:

```python
{
    "status": "pending",
    "duration": None,
    "response": None,
    "error": None,
    "retryable": None,
}
```

**Exact completion semantics**:

- The asyncio.Event is created on first access. If status is already terminal, it is immediately
  set; otherwise it starts unset.
- Assigning a terminal value through `Event.status` sets an already-created signal. Assigning a
  non-terminal value does not clear it. Completion is therefore a one-shot signal for the live Event,
  not a reusable condition variable.
- Directly mutating `execution.status` or overriding lifecycle methods can bypass signalling.
- `assert_completed` succeeds only for `completed`. Every other status raises `RuntimeError` with
  the serialized execution fields except response.

**Exact observation semantics**:

- Simple response values serialize directly. Complex responses are first tested with JSON dumping,
  then recursively converted to a dictionary when possible; values still not serializable become
  the string `"<unserializable>"`.
- A normal exception serializes as `{"error": <class name>, "message": <text>}`. An ExceptionGroup
  recursively serializes nested exceptions.
- `Execution.add_error` stores the first exception, groups the second, and appends later exceptions
  to the group. It caps a group at **100 errors**; further errors are ignored. The value is inherited
  from the implementation and has no recorded design rationale beyond bounding diagnostic growth.
- ExceptionGroup serialization stops only after depth **100** and emits a max-depth marker; it also
  detects a repeated object identity and emits a circular-reference marker. This numeric limit is
  likewise inherited with no recorded rationale.
- `Unset` duration and retryability serialize as `None`. `streaming` is excluded from the payload.
- Event state may be serialized through Element for logs and inspection, but `from_dict` always
  raises. A payload is not a resumable Event.

`as_fresh_event` is the supported reuse mechanism. It constructs the same concrete class while
excluding old `execution`, `id`, `created_at`, and metadata, reattaches other excluded fields with a
best-effort deep copy, and receives a fresh identity and pending Execution. Optional metadata copy
is best-effort deep; the new metadata always gains:

```python
{"original": {"id": str(old.id), "created_at": old.created_at}}
```

### D4 — Processor performs bounded in-process queue cycles

**The contract** (`lionagi/protocols/generic/processor.py`):

```python
class Processor(Observer):
    event_type: ClassVar[type[Event]]

    def __init__(
        self,
        queue_capacity: int,
        capacity_refresh_time: float,
        concurrency_limit: int,
        max_queue_size: int = 0,
    ) -> None: ...

    async def enqueue(self, event: Event) -> None: ...
    def try_enqueue(self, event: Event) -> bool: ...
    async def process(self) -> None: ...
    async def join(self) -> None: ...
    async def execute(self) -> None: ...
    async def request_permission(self, **kwargs: Any) -> bool: ...
    async def handle_denied(self, event: Event) -> bool: ...
```

**Parameter semantics**:

- `queue_capacity` must be at least 1 and bounds the number of capacity-consuming events (accepted,
  or terminally denied) handled in one processing cycle. It is not a cap on total dequeue
  operations: a deferred event is re-enqueued without decrementing `available_capacity`, so total
  queue traffic in one `process()` call can exceed `queue_capacity` when accepted, denied, and
  deferred events interleave. The generic class has no default; a caller or subclass owns the
  chosen number.
- `capacity_refresh_time` must be greater than zero and is the sleep interval for the long-running
  loop and for a `join` cycle that made no queue-size progress. The generic class does not choose a
  value or rationale.
- A truthy `concurrency_limit` creates an AnyIO semaphore; zero or `None` means no semaphore. A
  negative truthy value is rejected by the semaphore constructor.
- `max_queue_size=0` uses asyncio.Queue's unbounded convention. A positive value bounds queued
  Events. This zero default is intentional compatibility with the underlying queue API; no finite
  generic queue budget is imposed.
- A negative `max_queue_size` is not rejected. `asyncio.Queue` treats it as unbounded, but
  Processor's `queue_full` property compares the current size to the negative number and therefore
  reports true even for an empty queue. Negative values are accepted but internally inconsistent.
- Despite its name, `capacity_refresh_time` does not replenish `available_capacity`; it controls
  only the sleep interval in `join` and `execute`. Capacity resets only through the dispatched-work
  path described below.

**Exact queue and processing semantics**:

- `enqueue` waits for space. `try_enqueue` returns false rather than waiting on `QueueFull`.
  `queue_full` is always false for the unbounded zero case and otherwise compares current size to
  the configured maximum.
- `process` dequeues while cycle capacity remains and the queue is non-empty. It calls
  `request_permission(**event.request)` before dispatch.
- Default permission always allows. Default denial assigns `skipped` and returns true, meaning a
  terminal denial. A subclass can return false from `handle_denied` to defer instead.
- A terminal denial consumes one unit of available cycle capacity and leaves the Event out of the
  queue. A deferral re-enqueues the still-pending Event and consumes no capacity.
- The cycle stops after a full lap of deferrals, detected by deferred count reaching current queue
  size; this prevents a permanently denied queue from busy-spinning inside one call.
- Non-streaming Events run `invoke`; streaming Events are fully consumed through `stream`. When a
  semaphore exists, each task holds one permit around the entire invoke or stream.
- Scheduled tasks run in a task group, so `process` waits for all work selected in that cycle before
  returning. Ordinary Event failures normally remain captured state rather than task errors.
- If at least one Event was dispatched, available capacity resets to `queue_capacity` after the task
  group completes. A cycle containing only terminal denials decrements capacity without this reset;
  that is the current implementation, not a general token-bucket contract.
- If terminal denials consume all available capacity while denied Events remain queued, later
  `process` calls cannot dequeue them. `join` and `execute` continue sleeping at
  `capacity_refresh_time`, but no timer restores capacity; the remaining queue stalls until code
  outside Processor changes `available_capacity` or the caller cancels the loop.
- `join` repeats until the queue is empty. If a cycle leaves queue size unchanged, it sleeps
  `capacity_refresh_time`; permanently deferred work therefore keeps `join` alive until external
  policy changes or the caller cancels it.
- `execute` sets `execution_mode`, clears a prior stop signal, runs process/sleep cycles until
  stopped, and then clears `execution_mode`.

Processor is a live scheduling facility. Its queue and stop signal do not persist across restart.

### D5 — Executor owns live Events and pending order

**The contract** (`lionagi/protocols/generic/processor.py`):

```python
class Executor(Observer):
    processor_type: ClassVar[type[Processor]]

    def __init__(
        self,
        processor_config: dict[str, Any] | None = None,
        strict_event_type: bool = False,
    ) -> None:
        self.pending = Progression()
        self.processor: Processor | None = None
        self.pile = Pile(
            item_type=self.processor_type.event_type,
            strict_type=strict_event_type,
        )
```

**Exact semantics**:

- `append` asynchronously includes the live Event in the typed Pile and set-like includes its UUID
  in `pending`; appending the same Event again replaces the Pile value and does not duplicate the
  pending UUID.
- `start` lazily creates the configured Processor and resets its stop signal. `stop` is a no-op when
  no Processor has been created.
- `forward` removes every pending UUID from the left of the Progression, retrieves the corresponding
  Event, awaits queue insertion, then calls one Processor cycle. It expects a Processor to have been
  created by `start`.
- Completed, pending, failed, cancelled, and skipped properties construct new filtered Piles.
  `aborted` and `processing` have no dedicated property but remain visible in `status_counts`.
- `cleanup_completed` removes only completed Events and returns the count. Other terminal states
  remain owned until a consumer removes them.
- `inspect_state` reports total events, a string-keyed status histogram, pending UUID count, and
  processor running/stopped flags. It is a live snapshot assembled from current objects.

`APICalling` uses Event for endpoint work; `Operation` combines Node and Event so graph-addressable
operations share the same execution payload. `iModel.invoke` waits only while a call is pending or
processing, using the completion event with a **10-second** timeout before it removes and returns the
live call. That timeout replaced an earlier polling bound in the consumer; no stronger completion
guarantee is provided by Event itself, and timeout ownership stays in the service layer.

### D6 — Durable delivery remains a separate lifecycle

Event serialization is diagnostic and Event reconstruction is forbidden. Processor and Executor
hold live objects, local locks/signals, arbitrary response values, and in-memory queues. They cannot
provide restart recovery.

The durable outbox instead persists delivery identifiers, attempts, acknowledgement state, expiry,
retry timing, and dead-letter disposition. Those states answer whether a payload was delivered, not
whether an in-process operation's `_invoke` returned, failed, was skipped by permission, or was
cancelled by its task.

**Exact boundary semantics**:

- no outbox row is an Event snapshot;
- no Event `from_dict` path resumes an outbox delivery;
- Event `retryable` does not schedule a durable retry; and
- integrations must translate outcomes explicitly when they connect execution to delivery (see the
  persistence-state ADR on durable dispatch lifecycle).

Keeping the vocabularies separate prevents a delivery acknowledgement from being confused with a
successful operation response or a process-local cancellation.

## Consequences

API calls and graph Operations share one outcome shape, completion signal, error capture rule, and
processor model. Ordinary failures remain inspectable on the live Event without aborting sibling
work, while cancellation retains task-control semantics. Processors can apply capacity,
concurrency, queue, and permission policy without placing durable delivery concepts in Event.

The costs are concrete:

- legal status assignments are not a validated state machine, and direct `execution.status`
  mutation can bypass completion signalling;
- the completion primitive is tied to the process and is one-shot;
- direct lifecycle overrides can bypass duration, signalling, and failure capture;
- Processor denial/deferral policy and capacity values must be understood by each subclass;
- negative queue bounds produce contradictory queue-state reporting, and terminal-only denial can
  exhaust capacity without a refresh path; and
- serialized Event state cannot resume after process loss.

Today, consumers that expose a smaller status vocabulary risk projecting `cancelled` or `aborted`
as success. Reactive flow's completion record currently has only `completed`, `failed`, and
`skipped`, and its fallback maps any non-failed, non-explicitly-skipped Event to completed (see the
operations ADR on operation-flow completion projection).

Reversing D1 or D2 breaks all Event consumers. Replacing the local completion primitive requires an
async-runtime compatibility audit. Merging D6 with the outbox would require a migration and a new
definition of execution versus delivery success, not an enum rename.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Make the flow completion projection total over every terminal EventStatus; acceptance requires `cancelled` and `aborted` to remain non-success outcomes through status or reason fields, with tests for all five terminal states. | S | (filled at issue-open time) |
| 2 | Document the translation boundary between in-process Event execution and durable dispatch delivery; acceptance requires each integration point to identify ownership of retry, acknowledgement, expiry, and terminal-outcome mapping without merging the two state machines. | M | (filled at issue-open time) |
| 3 | Validate Processor queue bounds; acceptance requires negative `max_queue_size` to be rejected or assigned one coherent queue and `queue_full` meaning, with zero and positive-bound regression tests. | S | (filled at issue-open time) |
| 4 | Restore progress after terminal denial exhausts cycle capacity; acceptance requires more than `queue_capacity` terminally denied Events to reach `skipped` under `join` and `execute`, with the capacity-refresh rule stated and tested. | S | (filled at issue-open time) |

## Alternatives considered

### Raise every invocation error to the processor

This would use ordinary coroutine exception semantics and reduce mutable error state. A task-group
failure could then cancel sibling Events and force every batch caller to reconstruct which work
completed. Capturing ordinary `Exception` as `failed` keeps per-Event outcomes independent;
BaseException still unwinds control flow.

### Swallow cancellation as a failed Event

Treating cancellation like a business failure would make invocation total, but the controlling task
could not reliably stop work. The wrapper records `cancelled`, records duration and error, then
re-raises so structured concurrency remains authoritative.

### One terminal status

A single `done` value would simplify waiters. It would erase success, business failure, permission
skip, cancellation, and abort distinctions that processors and operation flows already need.
Completion signalling uses a shared terminal set without collapsing the externally inspected status.

### Eagerly allocate the completion event

Eager allocation makes the field simpler but binds every Event to an asyncio primitive whether or
not anything waits. Lazy allocation supports pre-loop construction and immediately sets the signal
when a waiter first arrives after terminal completion.

### Rehydrate serialized Events

Persisted Events could appear to support restart recovery. Their arbitrary response/error objects,
private completion primitive, subclass runtime dependencies, and possible in-flight side effects do
not form a durable replay contract. `from_dict` rejects the operation explicitly; callers create a
fresh Event or use durable dispatch.

### One status enum for Event and durable dispatch

This would reduce vocabulary count but collapse different questions: execution outcome versus
delivery attempt/acknowledgement/expiry. Retry and dead-letter transitions do not correspond to
`processing`, `skipped`, or task cancellation. The outbox retains its independent state machine.

### Drop denied Events

Dequeuing a temporarily rate-limited Event without requeueing loses work. Processor distinguishes
terminal denial (`skipped`) from deferral (requeue pending) and stops after a full deferred lap.

### Busy-wait deferred work

Immediately retrying a queue whose permission state has not changed consumes CPU and starves other
tasks. `process` returns after a full lap and `join` sleeps the configured refresh interval before
trying again.

### Time-based capacity replenishment

Processor could restore `available_capacity` after each `capacity_refresh_time` window regardless
of whether the prior cycle dispatched work. That would make the parameter a true capacity window
and would allow a terminal-denial-only queue to keep draining. The shipped implementation resets
capacity only when at least one Event was dispatched; no recorded rationale explains why terminal
denials are excluded. Delta 4 retains the required progress correction without retroactively
claiming a particular rate-limit algorithm.

### Persist Processor and Executor directly

Persisting queue order and live Event objects would couple generic execution to one storage and
recovery model. Their process-local task groups, semaphores, locks, and response objects do not
round-trip safely. Durable delivery remains a separate integration.

## Notes

The generic Processor intentionally does not assign provider capacity numbers. Service consumers
currently choose values such as queue capacity and refresh intervals; their rationales belong to
the service-provider area. The only numeric safety caps owned by this ADR are the two inherited
Execution diagnostic caps of 100, for which no recorded rationale was found.
