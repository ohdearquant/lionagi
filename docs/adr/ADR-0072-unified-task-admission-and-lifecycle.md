# ADR-0072: Unified task admission and lifecycle

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: scheduling-control-plane
- **Date**: 2026-07-09
- **Relations**: extends ADR-0071

## Context

LionAGI currently has two execution owners in one table. Scheduled fires enter `schedule_runs` as
`running` and are awaited by `SchedulerEngine`; ad-hoc task applications enter as `queued` and are
leased by the host worker. The split means restart recovery, cancellation, transition history, and
worker placement apply to one row class but not the other.

Manual task submission is currently only an in-process function, and its idempotency field is
rejected. The schema already contains the generalized run identity, queue timestamps, lease owner
and expiry, capability labels, execution target, and provenance fields needed for convergence. A
new broker or parallel task entity is unnecessary at current scale.

This target must retain the distinction between graph-execution lanes and task admission. A task
selects a lane adapter after claim; it does not become a fourth orchestration lane. Outbound
`dispatch_outbox` delivery also remains separate because it transports already-committed producer
facts rather than owning work.

This ADR answers six problems:

- **P1 — Producers can create duplicate work.** Schedule ticks, CLI retries, and HTTP retries need
  one atomic idempotency rule.
- **P2 — Two owners write the same lifecycle differently.** Direct scheduled launch and leased
  execution cannot provide one recovery or audit contract.
- **P3 — Direct status writes bypass reason and lease guards.** One API must own state, evidence,
  lease fields, and transition facts in one transaction.
- **P4 — Workers need a stable adapter boundary.** Producers must not know how every action kind is
  executed, and a worker must not claim work it cannot route.
- **P5 — Cancellation and expiry race with terminal completion.** Ownership and terminalization
  need compare-and-swap rules that make exactly one outcome authoritative.
- **P6 — Delivery state can contaminate execution state.** Notification retry/ack must not reopen a
  completed, failed, or cancelled task.

| Concern | Decision |
|---|---|
| Admission shape | D1: All schedule fires and public submissions use one typed `TaskApplication`. |
| Atomic deduplication | D2: Persist a non-empty idempotency key plus canonical payload hash; same key/same payload returns the original task. |
| Lifecycle owner | D3: Enforce one closed state graph and atomically write status, reason, lease patch, and transition fact. |
| Worker and adapter dispatch | D4: Claim through leases, then route by a registry-backed action adapter. |
| Recovery, timeout, and cancellation | D5: Resolve ownership races through guarded transitions and separate queued from cooperative running cancellation. |
| Outbound delivery boundary | D6: Keep `dispatch_outbox` as a terminal-fact consumer, never a task state owner. |

Out of scope:

- This ADR does not change the three public orchestration lanes in ADR-0068.
- It does not specify distributed broker adoption, leader election, or cross-database transactions.
- It does not make capability labels authorization grants.
- It does not define task dependencies; `waiting_dependency` remains a reserved lifecycle state
  until a separate dependency contract names the dependency records and readiness rule.
- It does not define model fallback or usage-ledger policy.
- It does not merge live reactive controls with task cancellation; lane adapters translate a
  cancellation request only when they implement one.
- It does not replace dispatch delivery or its acknowledgement/dead-letter lifecycle.

## Decision

### D1 — One typed application admits all executable work

The target public contract is Python-native:

```python
from dataclasses import dataclass, field
from typing import Any, Literal

ActionKind = Literal[
    "agent",
    "flow",
    "fanout",
    "play",
    "flow_yaml",
    "workflow",
]

ExecutionTarget = Literal[
    "host",
    "local_worktree",
    "daytona",
    "remote_agent",
    "process",
]

@dataclass(frozen=True)
class TaskApplication:
    action_kind: ActionKind
    args: dict[str, Any]
    execution_target: ExecutionTarget
    idempotency_key: str
    required_capabilities: list[str] = field(default_factory=list)
    library_ref: str | None = None
    library_content_hash: str | None = None

async def submit_task(db: StateDB, app: TaskApplication) -> str: ...
```

Bindings are thin and share this function:

```text
li task submit ... ────────────┐
POST /api/tasks ───────────────┼─► TaskApplication ─► submit_task()
schedule trigger admission ───┤
in-process library caller ─────┘
```

Every accepted application creates a `schedule_runs` row with `status='queued'`. Schedule
evaluation supplies `schedule_id`, invocation linkage is created by the executing adapter rather
than the trigger, and no producer writes `running` directly.

Exact admission semantics:

- `action_kind`, `execution_target`, and capability validation follow ADR-0071's closed sets.
- The public canonical name is `play`; `playbook` may be accepted at compatibility bindings only if
  normalized before constructing `TaskApplication`.
- `args` must be a JSON-serializable mapping. Empty is valid when the selected adapter accepts it.
- `required_capabilities` is normalized to a stable de-duplicated list for hashing and storage;
  empty means no capability constraint.
- `idempotency_key` must be a non-empty string after trimming and is always required at the library
  boundary. CLI bindings generate and print one only when the caller omits it; HTTP clients may
  supply one explicitly.
- `library_ref` and `library_content_hash` are provenance. For `workflow`, both become required once
  ADR-0073's versioned registry lands; other adapters may leave them null.
- Validation or serialization failure writes nothing.
- A schedule's admission key is deterministic from schedule id and nominal fire time:
  `schedule:<schedule-id>:<nominal-fire-epoch>`. Re-evaluating the same cadence point therefore does
  not duplicate a row.
- Immediate/manual fire is still an operation over a schedule, but it submits through this contract
  with a unique caller-visible key and returns a task id, not evidence that execution started.

Why this way: producers agree on what work is before workers decide how to run it. Requiring the key
at the core makes idempotency impossible to omit accidentally in a new binding.

Current migration seams: `lionagi/studio/services/task_applications.py`,
`lionagi/studio/scheduler/engine.py`, `lionagi/state/schema.sql`.

### D2 — Idempotency is one atomic insert-or-return decision

The target extends the generalized row with admission identity:

```sql
ALTER TABLE schedule_runs ADD COLUMN idempotency_key TEXT;
ALTER TABLE schedule_runs ADD COLUMN application_hash TEXT;
ALTER TABLE schedule_runs ADD COLUMN admission_source TEXT;

CREATE UNIQUE INDEX idx_schedule_runs_idempotency
  ON schedule_runs(idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

New admitted rows always set all three columns. Historical direct scheduled rows may retain nulls
during migration; no new public admission may.

The hash input is canonical JSON over exactly:

```json
{
  "action_kind": "workflow",
  "args": {},
  "execution_target": "host",
  "required_capabilities": [],
  "library_ref": "daily-review@4",
  "library_content_hash": "sha256:..."
}
```

Keys are sorted, arrays use normalized order where order is not semantic (capabilities), and the
hash is `sha256:<lowercase hex>`. The idempotency key itself is not part of the hash.

Target transaction semantics:

1. Validate and canonicalize the application.
2. Attempt one queued-row insert with the unique key and hash.
3. On unique conflict, select the existing row by key inside the same transaction.
4. If hashes match, return the existing id without changing status, queue time, lease, attempts, or
   provenance.
5. If hashes differ, raise `TaskIdempotencyConflict(key, existing_task_id)`; never mutate the old
   task and never insert the new payload.

Exact error/restart semantics:

- A process crash before commit leaves no row; retry inserts normally.
- A crash after commit but before reply returns the same id on retry.
- Two concurrent same-key/same-payload submissions converge on one id.
- Two concurrent same-key/different-payload submissions produce one winner and one explicit
  conflict.
- Reusing a key after the original task is terminal still returns that original task. Keys are not
  a “currently pending only” lock.
- Cancellation or failure does not free a key for another payload.
- An empty key is rejected before SQL; null is reserved only for historical rows during migration.

Why this way: “effectively once” admission is achievable with one unique constraint and payload
comparison even though execution itself may be retried. Returning an existing row without checking
the payload would hide caller bugs; deleting terminal keys would make delayed retries duplicate
work.

### D3 — One transition owner enforces the full admitted-task lifecycle

The target lifecycle is the table's closed nine-state vocabulary:

```text
queued ───────────────► running ─────────► completed
  │                       ├──────────────► failed
  │                       ├──────────────► timed_out
  │                       ├──────────────► retry_wait ──► queued
  │                       └──────────────► cancelled
  ├───────────────────► waiting_dependency ──► queued
  │                           ├───────────────► skipped
  │                           └───────────────► cancelled
  ├───────────────────► skipped
  └───────────────────► cancelled

completed | failed | timed_out | skipped | cancelled ──► no outgoing edges
```

`waiting_dependency` has no public producer until a dependency ADR supplies durable dependency
records. The transition owner nevertheless reserves only the edges above, so a future dependency
implementation cannot invent a different lifecycle accidentally.

Target request/result contracts retain the shipped models and make idempotency real:

```python
class TransitionRequest(BaseModel):
    entity_type: Literal["schedule_run"]
    entity_id: str
    from_state: str
    to_state: str
    reason: StateReason
    actor: Actor
    idempotency_key: str

class TransitionResult(BaseModel):
    applied: bool
    conflict: bool = False
    previous_state: str | None = None
    current_state: str
    transition_id: str
    event_id: str

async def transition_task(
    db: StateDB,
    request: TransitionRequest,
    *,
    guard: dict[str, Any] | None = None,
    patch: dict[str, Any] | None = None,
) -> TransitionResult: ...
```

Each successful call commits in one transaction:

- compare-and-swap guard over id, current status, and any declared lease fields;
- new status and `updated_at`;
- reason/evidence projection on `schedule_runs`;
- allowed lifecycle patch (`queued_at`, lease fields, attempts, terminal timestamps, exit detail,
  retry time, cancellation-request fields);
- exactly one `status_transitions` fact;
- exactly one task lifecycle event, whose id is returned as `event_id`.

The transition idempotency key gains a unique index scoped to entity id. Repeating the same key and
same canonical transition request returns the original result; reusing it with different content
raises a conflict.

Exact transition semantics:

- Missing task raises typed `TaskNotFound`; it does not synthesize a failed transition.
- Wrong `from_state`, lease-owner mismatch, or a race lost between read and write returns
  `applied=False, conflict=True` with the observed current state.
- An undeclared edge raises before mutation.
- A terminal state never re-enters queued through this API. Operator retry is a new submission with
  a new idempotency key or a separately approved future contract, not a hidden terminal rewrite.
- Direct updates to `schedule_runs.status` for admitted work are forbidden. Database maintenance
  may repair corrupt rows only through an explicitly audited admin path.
- Transition facts are the stable source for later projections; UI/operator work views consume them
  but never own status.

Why this way: status, evidence, lease ownership, and audit history represent one decision. Splitting
them across writes creates crash windows where the row and history disagree. A closed graph also
makes impossible transitions reviewable rather than convention-only.

### D4 — Workers claim first and select a registered adapter second

Workers retain ADR-0071's heartbeat, capability, target, concurrency, and lease rules. The execution
boundary becomes a registry rather than a hard-coded subprocess-only function:

```python
class TaskAdapter(Protocol):
    action_kind: str

    def validate_args(self, args: dict[str, Any]) -> None: ...

    async def execute(
        self,
        task: dict[str, Any],
        *,
        cancellation: "CancellationToken",
    ) -> "TaskExecutionResult": ...

@dataclass(frozen=True)
class TaskExecutionResult:
    status: Literal["completed", "failed", "timed_out", "cancelled"]
    exit_code: int | None = None
    error_detail: str | None = None
    invocation_id: str | None = None
    evidence_refs: list[dict] = field(default_factory=list)

class TaskAdapterRegistry:
    def register(self, adapter: TaskAdapter) -> None: ...
    def get(self, action_kind: str) -> TaskAdapter | None: ...
```

Canonical routing is:

| `action_kind` | Adapter |
|---|---|
| `play` | reactive-play adapter |
| `flow`, `flow_yaml` | planned reactive-flow adapter |
| `workflow` | fixed-workflow adapter from ADR-0073 |
| `agent`, `fanout` | their existing execution adapters |

Exact worker semantics:

- Submission validates that an adapter name is registered for the deployment before committing a
  new public task. Historical rows with a now-missing adapter remain queued and surface a placement
  reason; workers do not claim them.
- A worker filters by execution target and required capabilities before claim, then performs one
  guarded `queued -> running` transition with lease fields and attempt increment.
- Adapter lookup occurs again after claim. If the adapter disappeared between admission and claim,
  execution records a typed failure or returns through retry policy; it never dispatches as another
  kind.
- The worker passes the claimed task id, immutable application payload, current attempt, and
  cancellation token. Adapters do not update task status directly.
- The worker converts `TaskExecutionResult` into one guarded terminal transition while it still
  owns the lease. Exit details, invocation linkage, terminal timestamp, evidence, and status commit
  together through D3.
- If the terminal guard loses, the adapter result is retained only as diagnostic evidence; it does
  not overwrite the new owner.
- Producers depend only on `submit_task`; workers depend only on the registry; adapters depend on
  their lane/kernel. This prevents imports from every producer to every executor.

Why this way: claiming before adapter execution preserves one ownership contract. Registry lookup
keeps action selection extensible without making producers topology-aware.

### D5 — Recovery, timeouts, and cancellation are guarded lifecycle operations

Lease expiry re-enters policy through D3:

```text
running + expired lease + attempts remaining
  → retry_wait (clear owner/expiry, set retry_at, record lease-expiry reason)
retry_wait + retry_at due
  → queued (set queued_at, record retry-ready reason)
running + expired lease + attempts exhausted
  → failed
```

No transition is triggered merely by wall-clock passage; a reaper wins it through CAS. A worker may
still complete after nominal expiry if no reaper has changed the guarded row, matching the current
ownership rule.

Target cancellation signatures are:

```python
async def request_task_cancel(
    db: StateDB,
    task_id: str,
    *,
    actor: Actor,
    idempotency_key: str,
) -> "CancelResult": ...

class CancelResult(BaseModel):
    task_id: str
    state: Literal["cancelled", "cancellation_requested", "already_terminal"]
    current_status: str
```

Exact cancellation semantics:

- **Queued:** transition directly to `cancelled`; no worker may subsequently claim it.
- **Waiting dependency or retry wait:** transition directly to `cancelled` and clear any readiness
  timer/claimable marker.
- **Running:** atomically set `cancel_requested_at/by` and append a cancellation-request event while
  leaving status `running`. The worker forwards the request to the adapter's cooperative token.
- **Adapter acknowledges cancellation:** current lease owner transitions `running -> cancelled`.
- **Adapter ignores cancellation:** task remains running until it completes, times out, or loses its
  lease; the system does not falsely report cancellation.
- **Completion races cancellation request:** the first guarded terminal transition wins. A request
  arriving after terminal returns `already_terminal` and does not rewrite history.
- **Repeated request with the same key:** returns the original cancel result. A later request with a
  new key sees current state and remains idempotent at the lifecycle level.
- **Timeout:** a deadline reaper may transition current `running -> timed_out` only while its lease
  and deadline guard still match. Adapter cleanup cannot later overwrite it.
- **Restart:** queued/retry-wait state is durable; running work recovers only through lease expiry;
  cancellation-request columns and event remain visible to the next owner/policy.

Why this way: queued cancellation can be immediate because no external work owns the row. Running
cancellation is necessarily cooperative for heterogeneous adapters. Persisting “requested” rather
than immediately claiming “cancelled” keeps the state truthful.

### D6 — Dispatch consumes committed facts and never owns task state

`dispatch_outbox` remains the ADR-0070 contract. A producer may enqueue a terminal notification in
the same transaction or in an idempotent post-transition hook keyed by the transition/event id:

```text
task terminal transition committed
  └─► enqueue_dispatch(
        kind="task_terminal",
        dedup_key="task-terminal:<transition-id>",
        schedule_run_id=<task-id>,
        body={status, reason, evidence_refs}
      )
```

Exact boundary semantics:

- Dispatch enqueue failure is observable and retryable, but cannot roll back an already committed
  task terminal transition unless both participate in one local database transaction.
- Delivery, acknowledgement, expiry, dead letter, operator retry, and purge change only
  `dispatch_outbox`.
- A dead-letter notification does not turn a completed task into failed.
- Dispatch payloads may refer to task transition ids, but dispatch consumers cannot mutate task
  state without invoking a separate authorized task command.
- Live reactive pause/message controls remain `session_controls`; they are not sent through the
  dispatch outbox.

Why this way: execution state answers what happened to work. Dispatch state answers whether an
external side effect communicated that fact. Conflating them makes transport availability part of
task correctness.

## Consequences

- Every admitted task gains the same queued, lease, restart, deduplication, reason, cancellation,
  and terminal-write semantics.
- Trigger evaluation becomes separable from execution. `SchedulerEngine` submits and advances
  cadence; workers own launch.
- Scheduled work gains queue latency and requires the trigger to make overlap/missed-fire decisions
  before admission.
- Idempotency keys become permanent identities for an application payload; callers must not reuse
  them for different work.
- The transition owner becomes load-bearing and requires concurrency, crash-window, and
  idempotency tests for every edge.
- Adapter registration becomes a deployment contract. Removing an adapter can strand queued
  historical work unless migration or explicit failure policy is provided.
- Running cancellation is truthful but not instantaneous; adapters that cannot cooperate rely on
  timeout/lease recovery.
- Dispatch remains independently retryable and cannot contaminate terminal task status.
- Reversing D1-D3 after public bindings ship is costly because task ids, idempotency keys, and
  transition facts are durable contracts. D4 adapters can evolve behind the registry; D5 policies
  can tune budgets without changing state names.

## Alternatives considered

### Keep immediate scheduled launch

This preserves lower latency and avoids changing the mature schedule fire path. It lost because it
retains two owners and two restart/cancellation contracts in the same table, which is exactly P2.

### External message broker

A broker would buy mature consumer groups, visibility timeouts, and scale. It lost for current
single-operator scale because the database already provides durable admission and guarded leases.
The adapter and transition contracts leave room to replace storage later if measured demand
justifies it.

### Separate task table

A clean task domain table would avoid inherited schedule-run names. It lost because the generalized
row already contains run, queue, lease, provenance, and reason fields. Migration would duplicate
history before delivering a different operational capability.

### Optional idempotency keys

Making the key optional would ease local callers. It lost because every retrying binding would need
its own rule for when omission is safe. Required core keys make schedule and network retries
uniform; convenience bindings may generate a key visibly.

### Return existing id without comparing payload

This is the simplest insert-or-get behavior. It lost because accidental key reuse with different
work would silently execute the first payload while the caller believed the second was admitted.

### Direct terminal status writes by adapters

Adapters could update the row with their domain-specific results and reduce central API surface. It
lost because lease ownership, reason projection, terminal columns, and transition history could
split across transactions or bypass guards.

### Immediate `running -> cancelled` on request

This would give the user a fast terminal response. It lost because a provider/tool subprocess may
continue producing side effects. “Cancellation requested” is distinct from “execution stopped.”

### Requeue terminal failures in place

Moving `failed -> queued` would preserve one task id and key. It lost because terminal history would
become mutable and automatic retry safety depends on adapter side effects. Lease-loss recovery is
explicitly non-terminal; retrying a terminal application should be a separately authorized
submission/policy.

### Use dispatch as both task queue and notification outbox

One generic durable queue would reduce tables. It lost because inbound execution ownership and
outbound producer delivery have different states, consumers, and failure consequences. A missing
ack must not hold a task open.

## Notes

This is a target-state ADR. Current code anchors identify the migration seam, not proof that
idempotent admission, full lifecycle ownership, queued schedules, workflow adapter dispatch, or
cooperative running cancellation already exist.
