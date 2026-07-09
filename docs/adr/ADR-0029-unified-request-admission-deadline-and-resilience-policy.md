# ADR-0029: Unified request admission, deadline, and resilience policy

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: service-providers
- **Date**: 2026-07-09
- **Relations**: extends ADR-0027

## Context

Non-streaming `iModel.invoke()` appends an `APICalling`, forwards it to
`RateLimitedAPIProcessor`, and waits for a terminal event. The processor atomically checks request
and estimated-token capacity, defers denied work until replenishment, and uses a semaphore for
concurrency. The caller wait is nevertheless fixed at ten seconds: it then removes and returns the
event even if a longer refresh interval left it queued and pending (`lionagi/service/imodel.py`,
`lionagi/service/rate_limited_processor.py`).

`iModel.stream()` follows a different path. It appends the event and uses the processor semaphore,
but calls `APICalling.stream()` directly instead of forwarding it through permission checks. A
configured request or token limit therefore does not gate a stream before provider work starts.
Generator cleanup removes the event from the executor, but there is no shared deadline contract for
queued work, an active provider stream, and caller cancellation.

Resilience also differs by path. `Endpoint.call()` composes circuit breaking with configured retry
and otherwise supplies a native HTTP retry policy. `Endpoint.stream()` invokes the HTTP stream
transport directly, so connection establishment and response-header failures do not use the same
policy. Retrying after a chunk has reached the caller would be unsafe because most provider streams
do not expose a replay cursor (`lionagi/service/connections/endpoint.py`,
`lionagi/service/resilience.py`).

`Event` already provides pending, processing, completed, failed, skipped, cancelled, and aborted
states plus a completion signal. The missing contract is ownership: who admits the work, how long a
caller is willing to wait, which scope holds scarce capacity, and who terminalizes or cancels the
event when that scope ends (`lionagi/protocols/generic/event.py`).

The CLI run operation already converts its relative timeout to a monotonic deadline, bounds each
stream read by the remaining time, and explicitly closes the generator. That protects one operation
caller but does not cover admission or define the service-wide contract. The service budget must
accept that existing deadline rather than stack a fresh timeout around it
(`lionagi/operations/run/run.py`).

## Decision

Introduce one internal admission operation used by both `invoke()` and `stream()`. It returns an
async lease only after queue capacity, request budget, estimated-token budget, and concurrency
capacity have been acquired. The lease begins before hooks or provider work and ends only after the
event reaches a terminal state or cancellation cleanup completes.

```python
@dataclass(frozen=True, slots=True)
class RequestBudget:
    deadline: float | None  # absolute event-loop monotonic time

class AdmissionController(Protocol):
    async def acquire(
        self,
        call: APICalling,
        *,
        budget: RequestBudget,
    ) -> AdmissionLease: ...
```

The lifecycle is the same for both result shapes:

```text
create APICalling
       │
       v
bounded enqueue ──> rate + token admission ──> concurrency lease
       │                         │                      │
       │                         │                      v
       │                         │                pre-call hooks
       │                         │                      │
       │                         │             endpoint call / stream
       │                         │                      │
       │                         │                post-call hooks
       │                         │                      │
       └──── deadline/cancel ────┴────────────> terminalize + release
```

The load-bearing invariants are:

- Queueing is bounded and happens before expensive provider work. Request and estimated-token units
  are checked and debited atomically. Concurrency is a lease over active hook and provider work, not
  merely a semaphore used by one public method. A full queue applies producer backpressure until
  capacity is available, the request deadline expires, or the caller cancels.
- A stream is fully admitted before its pre-invocation hook runs or its endpoint starts. No chunk may
  be produced by work that bypassed configured request, token, or concurrency policy.
- One optional absolute deadline, measured on the event loop's monotonic clock, covers queue wait,
  admission wait, all retry delays, provider transport, and stream consumption. Nested stages derive
  remaining time; they do not reset a relative timeout.
- Caller cancellation removes queued work or cancels active work, closes an active stream, and
  re-raises cancellation after cleanup. Deadline expiry at any phase cancels underlying work, sets
  the event to `ABORTED`, and raises `RequestDeadlineExceeded`; caller cancellation sets
  `CANCELLED`; transport or provider failure sets `FAILED`. An `APICalling` is never removed from
  executor ownership while pending or processing.
- `invoke()` returns only a terminal `APICalling`. Stream setup and transport failures terminalize
  the same event and surface as typed exceptions rather than normal end-of-stream; normal EOF marks
  completion.
- Retry and circuit policy cover HTTP stream establishment through receipt of valid response
  headers and the first provider event. Automatic retry is allowed only before a user-visible chunk
  is emitted and only within the remaining deadline and retry budget. A mid-stream failure is
  recorded and raised as `ProviderStreamError` without generic replay. The circuit gates stream
  start, records normal EOF as success, and records any transport failure even when replay is
  prohibited. Provider-specific resumability requires a separate explicit adapter policy.
- Service hooks remain observation and policy points around the admitted call. This ADR does not
  consolidate hook systems or introduce another event bus (see the hooks ADR on call-boundary
  hooks).

An operation that already computed a monotonic deadline passes that absolute value into
`RequestBudget`; the service never starts a second full-duration timeout. Conformance tests use a
refresh interval longer than the caller budget, a full queue, a rate-limited stream, a cancelled
queued call, a cancelled active stream, pre-first-chunk transport failure, mid-stream failure, and
circuit-open behavior. They assert terminal state, provider call count, queue removal, lease release,
and absence of background work after the caller scope exits.

## Consequences

Quotas and overload behavior apply consistently to one-shot and streaming calls. A caller deadline
has one meaning across queueing, retries, and transport, and cancellation cannot detach work that the
executor later runs. Stream retry behavior becomes safe to reason about because no generic replay
occurs after observable output.

Long-lived streams hold concurrency for their lifetime, which is the honest accounting model but
may reduce throughput. Bounded queues and deadlines can reject work that the current implementation
waits on indefinitely or returns while pending. Typed failure propagation may expose failures that
were previously visible only through event state, so the migration requires release notes and
compatibility tests.

## Notes

Keeping separate stream and invoke schedulers was rejected because it makes quota configuration
method-dependent. Retrying an entire stream after output begins was rejected because it can duplicate
text, tool requests, or side effects. A fixed caller wait independent of the rate window was rejected
because it permits detached pending work. Persisting every deferred request was not selected; this is
an in-process service admission contract, not a durable job queue.
