# ADR-0029: Unified request admission, deadline, and resilience policy

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: service-providers
- **Date**: 2026-07-09
- **Relations**: extends ADR-0027

## Context

ADR-0027 records one public model facade but two materially different execution lifecycles. This
ADR defines one target lifecycle for both result shapes.

**P1 — `invoke()` can detach pending work.** Non-streaming `iModel.invoke()` appends an
`APICalling`, forwards it through `RateLimitedAPIProcessor`, and waits for a terminal signal. If the
event remains pending or processing after ten seconds, the timeout is swallowed and the event is
removed and returned anyway. A refresh interval longer than that fixed wait can leave a pending call
outside executor ownership (`lionagi/service/imodel.py`; `iModel.invoke`).

**P2 — `stream()` bypasses request and token admission.** Streaming appends the event and acquires
the processor semaphore, but calls `APICalling.stream()` directly instead of forwarding through
`RateLimitedAPIProcessor.request_permission()`. Configured request and token limits therefore do not
gate provider work before a stream starts (`lionagi/service/imodel.py`; `iModel.stream`, and
`lionagi/service/rate_limited_processor.py`; `RateLimitedAPIProcessor.request_permission`).

**P3 — `queue_capacity` is not a bounded queue.** The base `Processor` constructs
`asyncio.Queue(maxsize=0)` unless `max_queue_size` is separately supplied. The rate-limited
processor does not supply it. Its `queue_capacity` limits a processing cycle and participates in a
permission check; it does not bound queued memory
(`lionagi/protocols/generic/processor.py`; `Processor.__init__`, and
`lionagi/service/rate_limited_processor.py`; `RateLimitedAPIProcessor.__init__`).

**P4 — Relative timeouts are stacked instead of propagated.** The service has endpoint transport
timeouts, hook timeouts, a ten-second `invoke()` wait, replenishment sleeps, and retry delays, but no
single owner for total elapsed time. The CLI run operation already converts a positive relative
timeout to an absolute `anyio.current_time()` deadline, bounds each stream read by remaining time,
and explicitly closes the generator. That protects one caller after the call event is created; it
does not include admission, and the service cannot currently accept the inherited deadline
(`lionagi/operations/run/run.py`; `_stream_with_deadline`, `run`).

**P5 — HTTP stream resilience diverges from one-shot calls.** `Endpoint.call()` combines configured
retry and circuit policy or supplies a native aiohttp retry path. `Endpoint.stream()` calls
`_stream_aiohttp()` directly. Connection establishment and response-header failures therefore do
not receive the same policy. Replaying after a chunk reaches a caller is unsafe because generic
provider streams have no replay cursor
(`lionagi/service/connections/endpoint.py`; `Endpoint.call`, `Endpoint.stream`).

**P6 — Terminal state and cleanup have multiple owners.** `Event` already has `PENDING`,
`PROCESSING`, `COMPLETED`, `FAILED`, `SKIPPED`, `CANCELLED`, and `ABORTED` states plus a completion
signal. The processor, facade, event, endpoint, and operation nevertheless each own part of queueing,
cancellation, error capture, or generator close. No invariant currently prevents an event from being
removed while non-terminal (`lionagi/protocols/generic/event.py`; `Event`, `EventStatus`).

| Concern | Decision |
|---------|----------|
| Admission and capacity | D1: `invoke()` and `stream()` acquire the same FIFO bounded-queue, request, token, and concurrency lease before hooks or provider work. |
| Deadline and cancellation ownership | D2: one optional absolute monotonic deadline covers every stage; the lease supervisor alone terminalizes deadline, cancellation, and shutdown paths. |
| Public invoke/stream lifecycle | D3: both methods retain executor ownership until terminal cleanup; one-shot returns only a terminal event and stream failures propagate as typed exceptions. |
| Retry and circuit behavior | D4: HTTP call and stream establishment share one deadline-aware attempt policy; replay is prohibited after the first user-visible chunk. |
| Verification and observability | D5: a conformance matrix proves terminal state, capacity release, and absence of orphaned work for every exit path. |

This ADR deliberately does **not** decide:

- A durable job queue or persistence of deferred requests. Admission is process-local; restart does
  not resume queued calls.
- Provider-specific stream resume or replay cursors. An adapter with a real cursor needs an explicit
  policy under ADR-0030; generic replay remains prohibited after output.
- Consolidation of hook registries or a general event bus. Existing pre/post call hooks remain the
  service observation points.
- Provider request schemas or error grammar. ADR-0030 normalizes agentic errors; API adapters remain
  vendor-owned.
- Cross-model global quotas. One `iModel` owns one controller. A shared quota service would require a
  separate scope and fairness decision.

## Decision

### D1 — One admission controller issues an async lease

Both public methods use one internal controller. The target Python contract is:

```python
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

MonotonicClock = Callable[[], float]
AsyncSleep = Callable[[float], Awaitable[None]]

@dataclass(frozen=True, slots=True)
class RequestBudget:
    # Absolute event-loop monotonic time. None means no caller deadline.
    deadline: float | None = None

    def remaining(self, clock: MonotonicClock) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - clock())

@dataclass(frozen=True, slots=True)
class AdmissionConfig:
    queue_capacity: int
    capacity_refresh_time: float = 60.0
    interval: float | None = None
    limit_requests: int | None = None
    limit_tokens: int | None = None
    concurrency_limit: int | None = None

class AdmissionLease(Protocol):
    call: APICalling
    budget: RequestBudget

    async def __aenter__(self) -> AdmissionLease: ...
    async def __aexit__(self, exc_type, exc, tb) -> bool: ...
    async def release(self) -> None: ...

class AdmissionController(Protocol):
    async def acquire(
        self,
        call: APICalling,
        *,
        budget: RequestBudget,
    ) -> AdmissionLease: ...

    async def close(self) -> None: ...
```

The controller uses a physical `asyncio.Queue(maxsize=config.queue_capacity)` or an equivalent
bounded AnyIO queue. `AdmissionLease.release()` is idempotent. `__aexit__()` always releases
concurrency and active-call bookkeeping, but it does not suppress exceptions.

Admission failures are typed:

```python
class AdmissionError(RuntimeError): ...
class AdmissionClosed(AdmissionError): ...
class AdmissionConflict(AdmissionError): ...
class TokenEstimateUnavailable(AdmissionError): ...
class RequestExceedsTokenWindow(AdmissionError): ...

class RequestDeadlineExceeded(AdmissionError):
    call_id: UUID
    phase: Literal[
        "enqueue", "rate", "concurrency", "hook",
        "retry_delay", "transport", "stream",
    ]
```

The acquisition sequence is normative:

```text
validate event + token estimate
            |
            v
bounded FIFO enqueue  -- full --> wait under same RequestBudget
            |
            v
head-of-line rate/token reservation under one lock
            |
            v
concurrency acquisition under same RequestBudget
            |
            v
remove from waiting queue; return AdmissionLease
            |
            v
pre-hook -> provider call/stream -> post-hook -> terminalize -> release
```

**Exact semantics**

- **Configuration validation.** `queue_capacity`, `capacity_refresh_time`, and a non-`None`
  `interval` must be greater than zero. Request, token, and concurrency limits must be `None` or a
  positive integer. Zero and negative values are configuration errors rather than ambiguous
  "unlimited" values; `None` is the sole unlimited spelling.
- **FIFO.** Waiting calls are admitted in enqueue order. A later small request does not bypass an
  earlier large request. This trades maximum utilization for a stable starvation-free rule.
- **Full queue.** Producers wait for a queue slot. They are neither dropped nor admitted around the
  queue. Deadline expiry or cancellation removes the waiting producer without leaving a queue item.
- **Duplicate event.** Acquiring a call id that is already queued or active raises
  `AdmissionConflict`. A terminal event cannot be reacquired; callers use `as_fresh_event()` for a
  new attempt.
- **Token estimate.** If token limiting is configured and `APICalling.required_tokens is None`,
  acquisition raises `TokenEstimateUnavailable`; unmetered provider work is not silently admitted.
  A required token count greater than the whole window raises `RequestExceedsTokenWindow`
  immediately rather than waiting forever.
- **Atomic reservation.** Request and token availability are checked under one lock. Both are
  provisionally decremented or neither is. A deadline/cancellation before concurrency acquisition
  restores both under that lock. Once a lease is returned, the units are consumed even if hooks or
  provider work later fail; the dependency attempt used the admitted request budget.
- **Fixed window.** At each refresh, available request/token counters reset to configured limits.
  `interval=None` means `capacity_refresh_time`; an explicit interval is used as written. This ADR
  does not change the algorithm to a sliding window or token bucket.
- **Concurrency.** `concurrency_limit=None` means no semaphore. Otherwise the lease holds one permit
  from before the pre-invocation hook until terminal state and cleanup. Long-lived streams therefore
  consume concurrency for their entire lifetime.
- **Queue accounting.** `queue_capacity` counts waiting calls. Active calls have left the queue and
  are bounded by `concurrency_limit`. Executor inspection reports the two populations separately.
- **Empty controller.** Closing a controller with no queued or active work is a no-op. Repeated close
  is idempotent.
- **Refresh failure.** An unexpected replenisher failure closes admission, terminalizes queued work
  as `ABORTED`, cancels active work, and is observable. It is not logged and ignored while calls wait
  forever.

The default values retain current surface behavior where it is coherent:

| Endpoint class | Queue bound | Active concurrency | Reason |
|----------------|-------------|--------------------|--------|
| API endpoint | `100` | `None` unless configured | Inherited API defaults; no recorded numeric rationale. The target changes `100` from cycle capacity into an actual waiting bound. |
| Base agentic endpoint | `10` | `3` | Inherited from `AgenticEndpoint`; no recorded numeric rationale. |
| AG2 in-process agent/group chat | `3` | `1` | Inherited adapter overrides; serialization avoids concurrent mutation of adapter-owned agent state, while the exact values have no recorded rationale. |
| AG2 remote NLIP | `10` | `3` | Inherited adapter defaults; no recorded numeric rationale. |

The default refresh window remains 60 seconds. It is inherited from `iModel`; the source records no
reason for that exact duration. Callers that need a different provider window must configure it.

**Why this way.** A lease makes ownership visible: admission is not complete until every scarce
capacity has been acquired, and capacity is not released until cleanup is complete. A physical bound
makes overload backpressure explicit instead of hiding it in memory. Atomic provisional reservation
prevents a cancelled concurrency waiter from leaking quota. FIFO is deliberately simple and
testable; a weighted scheduler would be a separate fairness decision.

### D2 — One absolute monotonic deadline owns every stage

A relative timeout is converted exactly once, at the outermost caller that introduces it:

```python
def request_budget_from_timeout(
    timeout: float | None,
    *,
    clock: MonotonicClock,
) -> RequestBudget:
    # Compatibility: None, zero, and negative values mean no caller deadline.
    if timeout is None or timeout <= 0:
        return RequestBudget()
    return RequestBudget(deadline=clock() + float(timeout))
```

If an operation already owns an absolute deadline, it passes `RequestBudget(deadline=deadline)`
directly. The service never adds the original relative duration again.

**Exact semantics**

- **Clock.** Deadline arithmetic uses the running event loop's monotonic clock (`loop.time()` or
  `anyio.current_time()`), never UTC or `time.time()`. The clock is injected into the controller for
  deterministic tests.
- **Scope.** The budget begins before bounded enqueue and covers queue wait, rate wait, concurrency
  wait, pre-hook, retry delay, every transport attempt, stream consumption, post-hook, and mandatory
  cleanup.
- **Remaining time.** Before every wait, sleep, hook, and transport attempt, the stage computes
  remaining time. A local stage cap is `min(local_cap, remaining)`; it never receives a fresh full
  duration.
- **Past deadline.** A non-`None` deadline at or before current monotonic time expires immediately in
  the current phase. No provider work starts.
- **No deadline.** `None` removes only the caller deadline. Local hook and endpoint transport caps
  still apply.
- **Deadline expiry.** The supervisor cancels queued or active work, closes an active stream,
  performs cleanup, sets `EventStatus.ABORTED`, releases all reservations/permits, removes the event
  from ownership only after it is terminal, and raises `RequestDeadlineExceeded` with the phase.
- **External cancellation.** Caller cancellation is not translated into a deadline. Cleanup runs,
  the event becomes `CANCELLED`, and the original cancellation is re-raised.
- **Consumer close.** If a stream consumer calls `aclose()` or abandons iteration before normal EOF,
  the supervisor treats it as caller cancellation for event state: cleanup completes and the event
  becomes `CANCELLED`, but no new exception is injected into an otherwise clean `aclose()`.
- **Shutdown.** Controller close rejects new acquisition with `AdmissionClosed`, marks queued and
  active events `ABORTED`, cancels active scopes, awaits their cleanup, and then stops replenishment.
- **Race.** A terminal provider result that wins the race with cancellation remains terminal. A
  cancellation observed first owns the result; late provider completion is discarded inside the
  cancelled scope and cannot overwrite status.
- **Restart.** Admission state, reservations, and queued events are process-local. A restart begins
  empty; there is no replay or recovery scan.

Local caps remain useful inside the one deadline:

| Cap | Shipped default carried forward | Target interpretation | Numeric rationale |
|-----|---------------------------------|-----------------------|-------------------|
| Registered API transport | `600s` | Maximum one transport attempt when no shorter caller remainder exists. | Inherited; no recorded rationale. |
| Generic endpoint transport | `300s` | Same, for explicitly constructed generic config. | Inherited; no recorded rationale. |
| Registered agentic config | `3600s` | Adapter-level maximum only when the adapter uses it; caller remainder still wins. | Inherited; long-running agent intent is evident, exact value is not justified in source. |
| Pre-create/pre/post hook | `10s/30s/30s` | Effective hook cap is the lesser of local cap and caller remainder. | Inherited; no exact numeric rationale. |
| Model-manager shutdown | `10s` per model | Independent close cap outside any completed request budget. | Prevents one model blocking peers; exact value is inherited. |

**Why this way.** Absolute deadlines compose. A stage that receives only "ten seconds" cannot tell
whether nine seconds were already spent queueing; a monotonic deadline can. Deadline and external
cancellation remain distinct because they carry different caller meaning and terminal states.
Cleanup is inside the supervised lifetime so a timeout cannot return while provider work continues.

### D3 — `invoke()` and `stream()` use the same supervised lifecycle

The public surface accepts an internal budget without forcing ordinary callers to construct one:

```python
class iModel:
    async def invoke(
        self,
        api_call: APICalling | None = None,
        *,
        request_budget: RequestBudget | None = None,
        **request: Any,
    ) -> APICalling: ...

    async def stream(
        self,
        api_call: APICalling | None = None,
        *,
        request_budget: RequestBudget | None = None,
        **request: Any,
    ) -> AsyncGenerator[StreamChunk, None]: ...
```

`request_budget=None` means `RequestBudget(deadline=None)`. Existing public `timeout` values are
adapted at the caller boundary during migration; nested code passes `RequestBudget`.

The shared sequence is:

```text
create or accept APICalling
          |
          v
append to executor ownership
          |
          v
AdmissionController.acquire(call, budget)
          |
          v
pre-invocation hook
          |
          +------------------+
          |                  |
          v                  v
    endpoint.call       endpoint.stream
          |                  |
          v                  v
    store response       yield chunks
          |                  |
          +--------+---------+
                   v
             post-invocation hook
                   |
                   v
         terminal state -> cleanup -> release -> remove
```

**Exact semantics**

- **Creation failure.** Payload/config validation before append raises its typed validation error and
  creates no executor-owned event. Hook-enabled pre-create behavior remains as specified by the
  hooks contract.
- **Append.** A successfully created event is appended before acquisition so inspection can see
  queued state. Failure after append must terminalize and remove it through the supervisor.
- **Pre-hook ordering.** Pre-invocation hooks run only after all admission resources are acquired.
  A rate-limited or queued call cannot execute policy or provider work early.
- **One-shot success.** The endpoint response is stored, post-hook runs, status becomes `COMPLETED`,
  the lease is released, and the terminal `APICalling` is returned.
- **One-shot ordinary failure.** Hook/provider `Exception` is retained in `execution.error`, status
  becomes `FAILED`, the lease is released, and the terminal `APICalling` is returned. The method no
  longer wraps every error as a generic `ValueError`. Admission/deadline/cancellation failures keep
  their typed control-flow behavior.
- **One-shot invariant.** `invoke()` never returns `PENDING` or `PROCESSING`. It does not use an
  independent ten-second safety wait and never removes a non-terminal event.
- **Stream success.** A stream is fully admitted before the first provider call. Normal EOF runs the
  post-stream hook, marks `COMPLETED`, releases, and removes. A result chunk is permitted but not
  required for normal EOF.
- **Provider-declared stream error.** An adapter error chunk must have `type="error"` and
  `is_error=True`. The supervisor yields that normalized chunk at most once, records it, prohibits
  further output, marks the event `FAILED`, and raises `ProviderStreamError` after the chunk boundary
  so direct and operation consumers observe failure.
- **Transport/parser stream failure.** The event becomes `FAILED`; the original typed provider or
  transport exception escapes after cleanup. `Event.stream()` is adapted so ordinary stream
  exceptions are not silently converted into normal EOF.
- **Mid-stream hook failure.** A pre-hook failure emits no provider output and fails the event. A
  chunk-processing hook failure after provider start fails the event and propagates. Existing
  post-stream hook behavior remains: once output has been sent, its failure is logged and does not
  replace a successful provider stream.
- **Session state.** A system chunk with a provider session identifier may update the endpoint during
  streaming; a one-shot response may update it after completion. Cancellation or failure does not
  erase the last confirmed identifier. ADR-0030 defines which adapters are resumable.
- **Ownership removal.** Removal from the executor pile occurs after terminal state and cleanup on
  every path. Inspection may retain a separate bounded history snapshot, but active ownership never
  drops a non-terminal event.

**Why this way.** Method-dependent quotas are not meaningful quotas. Running the same supervisor on
both result shapes makes status, capacity, deadline, hook, and cleanup behavior citeable. One-shot
keeps the existing event-return style for ordinary provider failures; streaming must propagate
because there is no terminal event return value for the caller to inspect.

### D4 — Retry and circuit policy cover stream establishment, never observed output

The internal policy uses total attempts, avoiding the current ambiguity where one `max_retries=3`
path means three total attempts and another means one plus three retries:

```python
@dataclass(frozen=True, slots=True)
class AttemptPolicy:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    backoff_factor: float = 2.0
    jitter_factor: float = 0.2
    retry_statuses: frozenset[int] = frozenset({429})
    retry_server_errors: bool = True

@dataclass(frozen=True, slots=True)
class CircuitPolicy:
    failure_threshold: int = 5
    recovery_time: float = 30.0
    half_open_max_calls: int = 1
```

`retry_server_errors=True` means every HTTP status at or above 500 is retryable. A non-429 4xx is
never retried. `max_attempts` includes the first call and must be at least one. Delays and circuit
values must be non-negative, the backoff factor must be at least one, and half-open calls and failure
threshold must be positive.

The HTTP stream attempt boundary is:

```text
circuit gate
    |
    v
attempt: connect -> receive headers -> validate status -> parse first provider event
    | failure before emitted StreamChunk
    +---- retryable + attempts/time remain ----> deadline-clipped backoff -> next attempt
    |
    v
emit first StreamChunk  ===== replay boundary closes permanently =====
    |
    v
continue stream -> normal EOF | failure (record, never generic-retry)
```

**Exact semantics**

- **Retryable setup failures.** Connection/timeouts, HTTP 429, and HTTP status 500 or above may retry
  before any `StreamChunk` is yielded. Non-429 4xx, payload validation, authentication classification,
  parser contract errors, cancellation, and circuit-open errors are not generic retry candidates
  unless a provider adapter explicitly classifies them before output.
- **First-output boundary.** The retry flag closes immediately before the first normalized chunk is
  yielded from the endpoint. It never reopens, including if the first chunk is `system` or
  `thinking` rather than text.
- **Mid-stream failure.** Any transport or parser failure after that boundary records circuit
  failure, marks the event `FAILED`, and raises `ProviderStreamError` or the more specific provider
  error. No automatic replay occurs.
- **Normal EOF.** EOF records circuit success even when the stream yielded no chunks. A
  provider-declared error chunk is failure, not normal EOF.
- **Circuit unit.** The circuit gates one logical request, not each internal retry. Exhausted setup
  retries count as one circuit failure. A normal one-shot response or normal stream EOF counts as one
  success. A rejected open-circuit request makes no transport attempt.
- **Half-open stream.** A half-open permit remains in use until stream EOF or failure; receiving one
  chunk does not close the circuit early.
- **Deadline.** Before every attempt and sleep, remaining caller time is checked. Attempt transport
  timeout is `min(endpoint_local_timeout, remaining)`. Backoff sleep is clipped to remaining time and
  cannot start if no useful budget remains.
- **Retry-After.** A valid HTTP `Retry-After` delay is a floor for the next 429/5xx retry. If that
  delay cannot fit in remaining time, the last provider error is raised without sleeping past the
  deadline. Invalid header values are ignored in favor of client backoff.
- **Jitter.** Jitter randomizes only the computed client delay. An injected async sleep and monotonic
  clock make the schedule testable without real waiting.
- **Cancellation.** Cancellation during an attempt or delay propagates immediately after cleanup and
  is never caught by retry classification.
- **Caching.** Cache control remains a one-shot concern. A stream is never replayed from the generic
  call cache.
- **Provider resume.** A resumable adapter may define an application-level continuation after
  failure, but that is a new explicit request using a confirmed cursor. It is not an automatic
  retry hidden inside this policy.

The target carries forward these defaults with clarified meaning:

| Value | Target meaning | Reason |
|-------|----------------|--------|
| `max_attempts=3` | Three total transport attempts for one logical request. | Preserves the native `EndpointConfig.max_retries=3` attempt count; the exact number is inherited. Explicit `RetryConfig(max_retries=3)` callers require migration because that path currently permits four attempts. |
| `base_delay=1s`, `max_delay=60s`, factor `2.0`, jitter `0.2` | Deadline-clipped exponential backoff with jitter. | Pattern choice avoids synchronized retries; exact values are inherited from `RetryConfig` (whose `jitter_factor` default is 0.2 — the native no-`RetryConfig` aiohttp path currently hardcodes 0.5), with no recorded tuning evidence. |
| Circuit `5` failures, `30s` recovery, `1` half-open call | Logical-request circuit thresholds. | Inherited current defaults; no measured tuning rationale is recorded. |

**Why this way.** A retry boundary is safe only before output becomes observable. Total attempts are
easier to reason about than a field whose meaning changes by path. The circuit measures logical
dependency health rather than magnifying one caller's retry sequence into several failures. Deadline
checks before attempts and sleeps prevent resilience policy from violating the caller's budget.

### D5 — Conformance proves resource and terminal-state invariants

The controller exposes a redacted inspection snapshot:

```python
@dataclass(frozen=True, slots=True)
class AdmissionSnapshot:
    queued: int
    active: int
    available_requests: int | None
    available_tokens: int | None
    queue_capacity: int
    concurrency_limit: int | None
    closed: bool
```

No request payload, header, API key, prompt, or provider response appears in this snapshot. Retry and
circuit telemetry records call id, provider, endpoint, attempt number, phase, delay, remaining
budget, terminal status, and exception class; it does not log secret-bearing payloads.

The conformance suite is normative:

| Case | Required assertions |
|------|---------------------|
| Refresh interval longer than budget | Call becomes `ABORTED`; deadline error escapes; queue and active counts return to zero; provider count is zero. |
| Physically full queue | Next producer waits; on slot release it proceeds FIFO; on cancellation it leaves no queue item. |
| Request and token conflict | Neither counter is debited when either check fails. |
| Token estimate unavailable | Typed admission error; no provider/hook call. |
| Token request larger than window | Immediate typed error; no refresh wait. |
| Rate-limited stream | No pre-hook or provider work before rate admission. |
| Cancelled queued call | `CANCELLED`; provisional units restored; no active task. |
| Cancelled active one-shot | Provider scope cancelled and awaited; `CANCELLED`; semaphore released. |
| Cancelled or closed active stream | Generator closed; transport/process cleanup awaited; `CANCELLED`; no later chunks. |
| Pre-first-chunk retryable failure | Retry count and delays match policy and remaining deadline. |
| Permanent 4xx | One attempt; `FAILED`; no retry delay. |
| Mid-stream transport failure | Prior chunks appear once; `FAILED`; typed error; provider count remains one logical request with no replay. |
| Provider error chunk | Exactly one normalized error chunk with `is_error=True`; then typed failure, no result chunk. |
| Normal empty EOF | `COMPLETED`; circuit success; lease released. |
| Circuit open | No provider attempt; typed open error; lease cleanup complete. |
| Controller close | Queued/active work becomes `ABORTED`; close waits for cleanup and is idempotent. |

Every test asserts the event is terminal before it disappears from active executor ownership. The
suite also checks task enumeration or adapter-specific probes so "counts returned to zero" cannot
hide a detached background task.

**Why this way.** Async lifecycle bugs often pass return-value tests while leaking a task, queue
item, semaphore permit, socket, or process. The snapshot and conformance matrix make cleanup and
terminal state first-class outputs. Injected clock and sleep collaborators keep deadline/retry tests
deterministic.

## Consequences

- Quotas and overload behavior apply equally to one-shot and streaming calls. A full waiting queue
  becomes caller-visible backpressure instead of unbounded memory growth.
- One caller deadline has one meaning across admission, hooks, retries, transport, and stream
  consumption. Nested layers cannot silently reset it.
- A call remains owned until it is terminal and cleanup has completed. Detached pending work and
  post-timeout provider calls are contract violations detectable by conformance tests.
- Long streams hold concurrency for their lifetime. This can reduce throughput, but it represents
  actual scarce provider work rather than undercounting it.
- FIFO can cause head-of-line blocking when a large token request waits for refresh. Replacing FIFO
  with size-aware scheduling would require a fairness ADR and starvation proof.
- Zero and negative quota values become invalid rather than ambiguous. Existing configurations that
  used them must switch to `None` for unlimited.
- Explicit retry config semantics change from "retries after first" to total attempts through the
  internal policy. Compatibility adaptation and release notes are required.
- Stream setup gains safe retries; mid-stream failures become more visible because they raise typed
  errors instead of looking like normal EOF. Consumers that inferred failure only from content must
  migrate.
- Reversing D1/D2 is high cost once operations rely on one lease and deadline. Tuning numeric
  defaults is low cost if semantics remain fixed. Adding durable admission is a separate large
  design because restart and acknowledgement contracts would change.

## Alternatives considered

### Keep separate `invoke()` and `stream()` schedulers

This minimizes code movement and lets streams remain low-latency by taking only the semaphore. It
lost because request and token limits would remain method-dependent, queue ownership would still
diverge, and a provider call could bypass policy merely by asking for streaming output.

### Treat the current `queue_capacity` as sufficient overload control

This preserves `Processor` unchanged. It lost because the actual `asyncio.Queue` is unbounded and
`queue_capacity` is a cycle/permission value, not a memory bound. A name cannot provide
backpressure; the queue primitive must be bounded.

### Fixed per-stage relative timeouts

Admission, hooks, retries, and transport could each keep a local duration. This is easy to configure
and reason about in isolation. It lost because durations add: each stage can consume its full limit
after earlier stages already spent the caller's budget. One absolute monotonic deadline composes
without budget inflation.

### Keep the ten-second `invoke()` safety wait

The fixed wait prevents a caller from waiting forever and is already deployed. It lost because it is
independent of provider refresh and transport budgets and returns a non-terminal event after removing
ownership. A caller deadline plus supervised cleanup provides an actual bound.

### Drop when the queue is full

Immediate rejection protects memory and avoids waiting. It lost as the default because current
callers expect work to wait for provider capacity. Bounded producer backpressure preserves that
behavior while respecting cancellation/deadline. A future explicit fail-fast admission mode can be
added without changing the default.

### Refund quota after every provider failure

Refunding would maximize successful work per fixed window. It lost because a failed provider attempt
still consumed dependency capacity and automatic refund can amplify an outage. Only reservations
cancelled before the lease begins are restored.

### Retry an entire stream after output begins

This could hide transient disconnects and improve completion rates. It lost because replay can
duplicate text, tool requests, or side effects, and generic streams expose no cursor or idempotency
proof. Only an explicit provider continuation request may resume.

### Mark circuit success on the first chunk

This would release half-open probes early and improve circuit throughput for long streams. It lost
because a dependency that disconnects after one chunk is not healthy for the promised stream. Normal
EOF is the success boundary; mid-stream transport failure is circuit failure.

### Persist every deferred request

A durable queue would survive restart and allow later replay. It lost because persistence introduces
serialization, acknowledgement, duplicate suppression, credential lifetime, and recovery semantics
far beyond an in-process model client. This ADR intentionally aborts queued work on shutdown.

### Use wall-clock timestamps for deadlines

UTC deadlines are easy to log and pass across processes. They lost inside this process because clock
adjustments can make remaining time jump. The caller may translate an external timestamp once, but
all service budget arithmetic uses monotonic time.

## Notes

This is a target-state ADR. The current source constraints are
`lionagi/service/{imodel,rate_limited_processor,resilience}.py`,
`lionagi/service/connections/{endpoint,api_calling,agentic_endpoint}.py`,
`lionagi/protocols/generic/{processor,event}.py`, and `lionagi/operations/run/run.py`. The design
retains the existing event vocabulary and service hook boundary while replacing split scheduling
and unpropagated timeouts. The composed architecture domains reinforced four choices already forced
by source evidence: a physical queue bound, one absolute deadline, immediate cancellation
propagation, and a replay boundary at first observable output.
