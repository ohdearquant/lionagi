# ADR-0083: Canonical Per-Node Lifecycle Signal Contract

**Status**: Accepted
**Date**: 2026-06-10
**Implements**: GitHub #1251
**Builds on**: ADR-0076 (observer as hook transport) · ADR-0072 (reactive capability bus)

## Context

The signals introduced in ADR-0072 and extended in ADR-0076 (`NodeStarted`,
`NodeCompleted`, `NodeFailed`, `GateDenied`, `RunStart`, `RunEnd`, …) are
ad-hoc to specific execution paths. Any subscriber that wants to derive a
node's current state — a progress board, an audit log, a test assertion —
must parse the signal stream with bespoke conditionals and tolerate gaps:

- There is no `queued` signal to mark when a node enters the runnable graph
  (including injected `SpawnRequest` children).
- There is no `awaiting_approval` signal to distinguish "paused for a gate
  decision" from "still running".
- There is no `escalated` signal to mark nodes that emitted an
  `EscalationRequest` and are waiting for re-dispatch or give-up.

The result is that a subscriber reading the observer's `Flow` cannot
determine the node's current lifecycle lane without coupling to the
executor's internal dispatch logic.

## Decision

Define a **canonical per-node lifecycle** with six states and a
**projection helper** that reduces any ordered, single-node signal stream
to its current lane.

### 1. Canonical states

```python
NodeLifecycleState = Literal[
    "queued", "running", "awaiting_approval", "succeeded", "failed", "escalated"
]
```

The natural transition graph:

```text
queued ──► running ──► succeeded
               │
               ├──► awaiting_approval ──► running (approval granted)
               │
               ├──► failed
               │
               └──► escalated
```

A node may retry: a subsequent `NodeQueued` or `NodeStarted` signal resets
the state from any terminal (`succeeded | failed | escalated`) back to
`queued` or `running` for the new attempt.

### 2. Three new signals completing the contract

| Signal | State | When to emit |
|--------|-------|--------------|
| `NodeQueued(op_id, name, elapsed=0.0)` | `queued` | When a node enters the runnable graph, including `SpawnRequest` injections. |
| `NodeAwaitingApproval(op_id, name, reason=None)` | `awaiting_approval` | Immediately before a blocking gate/approval wait. |
| `NodeEscalated(op_id, name, reason, route)` | `escalated` | When an `EscalationRequest` is routed — `route="higher_tier"` if re-dispatch is scheduled, `route="give_up"` if no escalation path is configured. |

`NodeStarted / NodeCompleted / NodeFailed` (existing) cover `running /
succeeded / failed` and require no changes.

**`op_id` is the only stable key.** The `name` field is an informational
display label and is NOT normative for grouping or identity: emitters may
derive it from the node's `reference_id` (queue time, before a branch is
assigned) or from the executing branch's name (execution time — the existing
contract consumed by the CLI heartbeat display). Observers MUST group and
project by `op_id`; a node's `name` may legitimately differ across its own
lifecycle signals.

### 3. Projection helper: `lane_for`

```python
def lane_for(signals: Iterable[Signal | Any]) -> NodeLifecycleState:
    """Project an ordered, single-node signal stream into its current lane."""
```

Invariants:

- **Default** state for an empty or non-state-bearing stream is `"queued"`.
- **Latest wins**: the last state-bearing signal governs.
- **Terminal sticky**: once `succeeded | failed | escalated` is reached,
  only a subsequent `NodeQueued` or `NodeStarted` (a new attempt) can reset
  it. Other signals are ignored.
- Callers must **pre-filter** signals to a single `op_id` before calling
  `lane_for`; the helper does not group by node id.

### 4. Signal-to-state mapping

| Signal or condition | Projected state | Notes |
|---------------------|-----------------|-------|
| (empty stream) | `queued` | Default. |
| `NodeQueued` | `queued` | New — see §2. |
| `NodeStarted` | `running` | Existing. |
| `RunStart` | `running` | Run-scoped fallback; valid for single-run cards. |
| `NodeAwaitingApproval` | `awaiting_approval` | New — see §2. |
| `NodeCompleted` | `succeeded` | Existing. |
| `RunEnd` | `succeeded` | Run-scoped fallback. |
| `NodeFailed` | `failed` | Existing. |
| `RunFailed` | `failed` | Existing. |
| `NodeEscalated` | `escalated` | New — see §2. |
| `NodePaused` | `paused` | Added by ADR-0085 slice 1 — a node blocked at an operation boundary, awaiting `resume()`. |
| `StructuredOutput(data=EscalationRequest)` | `escalated` | Capability-emission path before `NodeEscalated` is issued. |
| `GateDenied` | *(ignored)* | Governance detail; may trigger `NodeAwaitingApproval` upstream but is not a lane by itself. |
| `MessageAdded`, `HookSignal`, others | *(ignored)* | Not state-bearing. |
| `EventStatus.SKIPPED` | `failed` (v1) | `_execute_operation` emits `on_progress(..., "failed")` in the skip path, producing `NodeFailed` on the bus. Conservative mapping until a `cancelled` lane is added. |
| `EventStatus.CANCELLED / ABORTED` | *(not emitted in v1)* | These statuses are raised as exceptions and propagate up the call stack; no signal is emitted for them in the current executor. A `cancelled` lane is deferred to a follow-up. |

### 5. Location

All new symbols live in `lionagi/session/signal.py` and are exported from
`lionagi/session/__init__.py`. No new module is introduced. The existing
`Signal` hierarchy is extended directly.

## Consequences

**Positive**

- Any subscriber (board, dashboard, audit, test) calls `lane_for(signals)`
  and gets a stable, typed state — no bespoke parser per consumer.
- `NodeQueued` closes the gap for `SpawnRequest`-injected children that had
  no observable queued state.
- `NodeEscalated` makes escalation visible in the audit `Flow` without
  requiring observers to parse `EscalationRequest` payloads.
- `NodeAwaitingApproval` distinguishes "blocked on approval" from "still
  running", enabling time-to-approval metrics.
- Zero breaking changes: existing signals are unmodified; new signals are
  additive. Subscribers not yet emitting new signals get `"queued"` as the
  default, which is conservative and correct.

**Negative**

- Emitters (executor, reactive flow) must be updated to emit `NodeQueued`
  at the right seams. Until that wiring lands, the default state for
  un-instrumented nodes is `"queued"` (acceptable conservative fallback).
- `lane_for` trusts signal ordering. Out-of-order delivery (rare on a
  synchronous in-process bus) could misproject state.
- In the batch executor (`DependencyAwareExecutor`), `NodeQueued` is emitted
  for all nodes upfront before any dependency check, so it means "entered the
  runnable graph" rather than "dependencies met". This is intentional v1
  semantics and consistent with the ADR definition; a future "ready" lane
  could close the gap if needed.

## Bridge points wired in this ADR

| Location | Signal | When |
|----------|--------|------|
| `engines/engine.py` `_on_progress` | `NodeQueued` | `status=="queued"` callback from flow |
| `engines/engine.py` `_on_progress` | `NodeFailed` | `status=="failed"` — covers both execution failure and skipped nodes |
| `operations/flow.py` `DependencyAwareExecutor.execute()` | via `on_progress` callback | Before `_alcall` on initial nodes |
| `operations/flow.py` `DependencyAwareExecutor._execute_operation()` | via `on_progress("failed")` | In the skip path when edge conditions are not met |
| `operations/flow.py` `ReactiveExecutor.execute()` | via `on_progress` callback | Before `tg.start_soon` for each initial node |
| `operations/flow.py` `ReactiveExecutor.execute_stream()` | via `on_progress` callback | Before `tg.start_soon` for each initial node (inside `_driver`) |
| `operations/flow.py` `ReactiveExecutor._accept_node()` | via `on_progress` callback | After `_assign_injected_branch` for injected children |

`NodeAwaitingApproval` live emission is deferred to a future approval-gate
feature (no blocking gate seam exists in the current execution path).

## Follow-ups (not in this landing)

1. **Wire `NodeAwaitingApproval`** before any blocking gate wait (ADR-0076
   Follow-up — the real pre-invoke gate).
2. **Wire `NodeEscalated`** in the escalation routing handler when an
   `EscalationRequest` is routed (ships with the escalation routing slice).
3. Add a `cancelled` lane for `EventStatus.CANCELLED / ABORTED` if the
   scope is expanded.
4. `lane_for` does not group by `op_id`; a higher-level helper that
   segments a mixed-node stream by id and projects each may be needed for
   whole-run dashboards.

## Alternatives Considered

| Alternative | Why rejected |
|-------------|--------------|
| Add state fields to existing signals | Breaks existing observers keyed on `NodeStarted` etc.; conflates transport with state machine. |
| Dedicated `NodeLifecycleEvent` with an enum field | Replaces the typed-signal pattern ADR-0072 established without benefit; loses the `observe(NodeCompleted)` ergonomic. |
| Leave projection to each subscriber | The current state that motivated this ADR; bespoke parsers drift and diverge. |
| `verifying / verified / cancelled` from the issue text | The flow instruction narrows the required states to six; the broader set is deferred (see Follow-up 3). |

## References

- [ADR-0072](ADR-0072-reactive-capability-bus.md) — reactive capability bus
- [ADR-0076](ADR-0076-observer-as-hook-transport.md) — observer as hook transport
- `lionagi/session/signal.py` — `NodeQueued`, `NodeAwaitingApproval`, `NodeEscalated`, `lane_for`
- `tests/session/test_lifecycle_signals.py` — contract tests
