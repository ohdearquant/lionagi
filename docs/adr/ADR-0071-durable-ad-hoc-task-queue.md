# ADR-0071: Durable ad-hoc task queue

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: scheduling-control-plane
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0061, v0-0062, v0-0101

## Context

`schedule_runs` was generalized so `schedule_id` may be null. The in-process
`submit_task()` binding validates a `TaskApplication` and inserts a null-schedule row in `queued`
state with action arguments, execution target, required capabilities, optional library provenance,
queue timestamp, and an optional host-scoped concurrency key.

Submission idempotency is deliberately inactive: a non-null key is rejected rather than accepted
without deduplication. There is no `li task submit` command or `/api/tasks` binding. The queue is
therefore a real library capability, not yet a complete public task-admission surface.

The Studio scheduler tick hosts one worker. It heartbeats into `workers`, reaps expired leases,
pages through eligible ad-hoc rows, matches target/capability labels, and claims through a guarded
`queued -> running` transition. Execution reuses the scheduler subprocess argv builder. Terminal
writes retain lease identity so a stale worker cannot overwrite a row reclaimed by another worker.

The transition helper is a restricted compare-and-swap implementation for dispatch and
schedule-run rows. It appends `status_transitions` atomically with a successful status update, but
does not implement every state declared by the schema and does not deduplicate its modeled
`idempotency_key`. Scheduled fires still bypass this path and enter `running` under
`SchedulerEngine`.

This ADR answers five problems:

- **P1 — Ad-hoc work must survive until a worker sees it.** Direct function calls alone lose work
  when the daemon is unavailable.
- **P2 — Submission needs one typed validation boundary.** Action kind, payload, target,
  capabilities, and provenance must be normalized once before persistence.
- **P3 — Competing workers need one ownership transition.** A read followed by an unguarded update
  would let two workers execute the same row.
- **P4 — Worker death needs bounded recovery.** A lease must eventually return work to the queue or
  fail it instead of leaving `running` forever.
- **P5 — Placement metadata must not become authorization.** Capabilities and concurrency keys
  decide eligibility/order only; machine locks and permission checks retain separate authority.

| Concern | Decision |
|---|---|
| Submission contract | D1: Validate one frozen `TaskApplication`; reject unsupported idempotency keys. |
| Persistent queue entity | D2: Store ad-hoc work as `schedule_runs` rows with `schedule_id IS NULL` and initial `queued`. |
| State ownership | D3: Use one guarded transition function for the restricted shipped schedule-run graph and append transition facts atomically. |
| Worker lease and recovery | D4: Heartbeat, reap expired leases, claim with a five-minute lease, and stop after three expired attempts. |
| Capability and concurrency routing | D5: Match eligibility/serialization labels, prefer affinity labels, and use advisory host-scoped concurrency keys. |

Out of scope:

- CLI and HTTP submission bindings are not shipped.
- Submit-level idempotency is not shipped.
- Scheduled fires are not queue rows and are not claimed by this worker.
- Fixed `workflow` actions are persisted but explicitly excluded from claims because no workflow
  worker adapter is wired.
- Remote worker transport and remote execution-target implementations are not shipped.
- Capability tokens are not permissions. Authorization remains outside this queue.
- Dependency waiting, retry backoff states, timed-out/skipped transitions, and running cancellation
  are present in the table vocabulary but not in the restricted task transition graph.

## Decision

### D1 — `TaskApplication` is the single in-process submit shape

The shipped contract is:

```python
# lionagi/studio/services/task_applications.py
@dataclass(frozen=True)
class TaskApplication:
    action_kind: str
    args: dict[str, Any]
    execution_target: str
    required_capabilities: list[str] = field(default_factory=list)
    library_ref: str | None = None
    library_content_hash: str | None = None
    idempotency_key: str | None = None

async def submit_task(db: StateDB, app: TaskApplication) -> str: ...

async def cancel_task(
    db: StateDB,
    run_id: str,
    *,
    actor: Actor,
) -> bool: ...
```

Accepted action kinds are the subprocess launcher's set
`agent | flow | fanout | play | flow_yaml | engine`, plus `workflow`. `playbook` is accepted as an
input alias and normalized to `play` before storage. Execution targets are the closed set:

```python
{"host", "local_worktree", "daytona", "remote_agent", "process"}
```

Exact validation semantics:

- A non-null `idempotency_key` always raises `ValueError`. Empty string is also non-null and is
  rejected. This prevents a retrying caller from believing deduplication occurred.
- Unknown action kind raises and lists the canonical set plus accepted aliases.
- Unknown execution target raises and lists the closed set.
- `args` must be a dict. An empty dict is valid.
- `required_capabilities` must be a list of non-empty strings. An empty list is valid and is the
  default. Duplicate strings are not rejected or normalized at submission.
- `library_ref` and `library_content_hash` are stored verbatim as optional provenance; submission
  does not resolve the ref or verify the hash.
- `TaskApplication` is frozen, so the submitted object is not mutated during alias normalization.
- Validation failure occurs before a transaction and inserts no row.

Why this way: one dataclass gives future CLI and HTTP bindings the same library boundary. Explicitly
rejecting the dormant idempotency field is safer than accepting a contract the database does not
enforce.

Code anchor: `lionagi/studio/services/task_applications.py` (`TaskApplication`, `_validate`,
`submit_task`).

### D2 — Ad-hoc tasks reuse `schedule_runs`

Submission generates a full string UUID, derives a concurrency key from serialization-class
capabilities, and inserts one row:

```sql
INSERT INTO schedule_runs (
  id, schedule_id, invocation_id, trigger_context,
  action_kind, action_args, status, chain_depth,
  fired_at, created_at, queued_at, concurrency_key,
  required_capabilities, execution_target,
  library_ref, library_content_hash
)
VALUES (
  :id, NULL, NULL, {},
  :action_kind, :action_args, 'queued', 0,
  :now, :now, :now, :concurrency_key,
  :required_capabilities, :execution_target,
  :library_ref, :library_content_hash
);
```

The table contract shared with scheduled runs is:

```sql
-- lionagi/state/schema.sql (queue-relevant columns)
CREATE TABLE schedule_runs (
  id                    TEXT PRIMARY KEY,
  schedule_id           TEXT REFERENCES schedules(id) ON DELETE CASCADE,
  invocation_id         TEXT REFERENCES invocations(id),
  trigger_context       JSON NOT NULL,
  action_kind           TEXT NOT NULL,
  action_args           JSON NOT NULL,
  status                TEXT NOT NULL DEFAULT 'running'
                        CHECK(status IN ('queued', 'waiting_dependency', 'running',
                                         'retry_wait', 'completed', 'failed',
                                         'timed_out', 'skipped', 'cancelled')),
  exit_code             INTEGER,
  chain_parent_id       TEXT REFERENCES schedule_runs(id),
  chain_depth           INTEGER NOT NULL DEFAULT 0,
  fired_at              REAL NOT NULL,
  ended_at              REAL,
  error_detail          TEXT,
  created_at            REAL NOT NULL,
  updated_at            REAL,
  status_reason_code    TEXT,
  status_reason_summary TEXT,
  status_evidence_refs  JSON,
  queued_at             REAL,
  leased_by             TEXT,
  lease_expires_at      REAL,
  concurrency_key       TEXT,
  lease_attempts        INTEGER NOT NULL DEFAULT 0,
  required_capabilities JSON,
  execution_target      TEXT,
  library_ref           TEXT,
  library_content_hash  TEXT
);

CREATE INDEX idx_schedule_runs_queue
  ON schedule_runs(status, queued_at)
  WHERE status IN ('queued', 'retry_wait');

CREATE INDEX idx_schedule_runs_concurrency
  ON schedule_runs(concurrency_key, status)
  WHERE status IN ('queued', 'running', 'retry_wait');
```

Exact initial-state semantics:

- `schedule_id`, `invocation_id`, lease owner, and lease expiry are null.
- `trigger_context` is `{}`; `chain_depth` is zero.
- `fired_at`, `created_at`, and `queued_at` all receive the submission timestamp. The use of
  `fired_at` is inherited from the shared run table even though no trigger fired.
- The initial insert does not append a `status_transitions` row. Transition history begins with the
  first post-submit status move.
- Queued persistence survives daemon restart because it is a database row; no in-memory claim is
  needed for discovery.
- Reusing the table does not unify ownership: scheduled rows have non-null `schedule_id` and enter
  `running`, while ad-hoc rows have null `schedule_id` and enter `queued`.

Why this way: the row already has run identity, lifecycle columns, invocation linkage, and history
surfaces. A second task table would duplicate those fields and require joins or migration before
one operator view could show both.

### D3 — Restricted schedule-run transitions use guarded CAS

The request/result models are:

```python
# lionagi/state/transitions.py
class Actor(BaseModel):
    type: Literal["scheduler", "operator", "system", "webhook", "agent"]
    id: str

class StateReason(BaseModel):
    code: str
    summary: str = ""
    evidence_refs: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

class TransitionRequest(BaseModel):
    entity_type: str
    entity_id: str
    from_state: str | None
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
    event_id: str | None = None

async def transition(
    db: Any,
    request: TransitionRequest,
    *,
    guard: dict[str, Any] | None = None,
    patch: dict[str, Any] | None = None,
) -> TransitionResult: ...
```

The shipped schedule-run graph is registered in the lifecycle policy registry
(`lionagi/state/lifecycle/policy.py:302-350`):

```text
queued ──► waiting_dependency | running | skipped | cancelled
waiting_dependency ──► queued | cancelled
running ──► completed | failed | timed_out | retry_wait | queued | cancelled
retry_wait ──► queued | cancelled

completed, failed, timed_out, skipped, cancelled ──► no outgoing edges
```

The registered terminal set carries all five terminal statuses, including `timed_out` and
`skipped`. An undeclared edge raises `ValueError`.

Exact compare-and-swap semantics:

- Unsupported entity type, invalid reason code, or guard/patch column outside the per-entity
  allowlist raises before mutation.
- Missing row raises `LookupError`.
- A `from_state` mismatch returns `applied=False, conflict=True` before vocabulary validation. A
  second cancel therefore becomes a conflict result, not a terminal-edge exception.
- Extra guard mismatch likewise returns a conflict.
- The update uses `WHERE id=:id AND status=:from_state` plus guard predicates. If another writer
  wins between read and update, rowcount zero returns a conflict.
- Status, `updated_at`, allowed patch fields, and one `status_transitions` insert commit in the same
  `db._tx()` transaction.
- Schedule-run guard/patch fields are limited to `leased_by`, `lease_expires_at`, and
  `lease_attempts`.
- A successful result has `event_id=None`; this fallback does not emit a separate event-plane fact.
- `TransitionRequest.idempotency_key` is required by the model but is not stored or checked by the
  current implementation. Repeating a request is safe only when state/guard mismatch makes it a
  conflict; arbitrary idempotency-key deduplication is not shipped.

Transition facts use:

```sql
CREATE TABLE status_transitions (
  id              TEXT PRIMARY KEY,
  entity_type     TEXT NOT NULL,
  entity_id       TEXT NOT NULL,
  previous_status TEXT,
  status          TEXT NOT NULL,
  reason_code     TEXT NOT NULL,
  reason_summary  TEXT,
  evidence_refs   JSON,
  source          TEXT NOT NULL,
  actor           TEXT,
  created_at      REAL NOT NULL,
  metadata        JSON
);
```

Why this way: CAS plus lease guards makes ownership loss an explicit non-write. An unrestricted
generic state engine was not required for the shipped slice; the cost is visible incompleteness and
a modeled idempotency field without deduplication.

### D4 — One host worker claims, executes, and reaps leased rows

Worker contracts and defaults are:

```python
# lionagi/studio/scheduler/worker.py
TASK_WORKER_ENABLED = True
DEFAULT_LEASE_TTL_SECONDS = 300.0
DEFAULT_HEARTBEAT_TTL_SECONDS = 90.0
MAX_LEASE_ATTEMPTS = 3

ExecuteFn = Callable[[dict[str, Any]], Awaitable[tuple[int, str]]]

async def register_heartbeat(
    db: StateDB,
    *,
    worker_id: str,
    advertised_capabilities: list[str] | None = None,
    execution_targets: list[str] | None = None,
    now: float | None = None,
) -> None: ...

async def reap_expired_leases(
    db: StateDB, *, now: float | None = None
) -> dict[str, int]: ...

async def claim_and_execute(
    db: StateDB,
    *,
    worker_id: str,
    execute: ExecuteFn | None = None,
    now: float | None = None,
    lease_ttl: float = 300.0,
    limit: int = 20,
    advertised_capabilities: list[str] | None = None,
    execution_targets: list[str] | None = None,
    heartbeat_ttl: float = 90.0,
) -> int: ...

async def worker_tick(...) -> dict[str, int]: ...
```

The workers table is:

```sql
CREATE TABLE workers (
  worker_id               TEXT PRIMARY KEY,
  advertised_capabilities JSON NOT NULL DEFAULT '[]',
  execution_targets       JSON NOT NULL DEFAULT '[]',
  last_heartbeat_at       REAL NOT NULL,
  leased_run_id           TEXT REFERENCES schedule_runs(id)
);
```

The Studio engine creates one id shaped `host:<8 hex>` and calls `worker_tick()` once per scheduler
tick. A tick heartbeats first, reaps expired leases second, and claims/executes third. The default
execution target is `host`.

Claim SQL considers only:

```sql
WHERE status = 'queued'
  AND schedule_id IS NULL
  AND action_kind != 'workflow'
ORDER BY queued_at ASC, id ASC
```

Exact lease semantics:

- A known worker whose heartbeat age exceeds 90 seconds claims nothing. A worker with no heartbeat
  row is treated as not stale for compatibility with direct callers. The normal Studio tick writes
  its own heartbeat immediately before checking.
- Candidates are paged 50 rows at a time using a `(queued_at, id)` keyset cursor. A pass scans at
  most 5000 queued rows and attempts at most 20 eligible claims.
- The 50-row page is a database/dialect-friendly batching choice; 5000 bounds one tick's scan so a
  deep ineligible prefix does not monopolize it; 20 bounds executions per pass. No measured tuning
  rationale for the exact values is recorded.
- Claim is one `queued -> running` transition that atomically writes `leased_by`,
  `lease_expires_at=now+300`, and increments `lease_attempts`.
- Lost claim or concurrent cancel is skipped; the worker does not retry that row in the same pass.
- `default_execute()` converts `action_args` into a schedule-like dict, resolves the absolute `li`
  prefix, builds argv through the same launcher, and awaits the child. Build/resolve errors become
  `(1, error)` rather than escaping.
- Exit zero attempts `running -> completed`; non-zero or executor exception attempts
  `running -> failed`. Both terminal writes guard the exact lease owner and stored expiry value.
- Lease expiry by time alone does not invalidate a terminal write. The stale worker loses only
  after a reaper/claimant changes the guarded fields or status.
- The current worker does not renew leases during long execution.
- Reaper selects `running` rows with non-null expiry strictly less than `now`. Under three attempts,
  it guards the observed expiry, clears lease fields, and returns to `queued`. At three or more, it
  transitions to terminal `failed`.
- A reaper/terminal race is resolved by CAS. A stale terminal writer cannot overwrite a reclaimed
  lease.
- Terminal execution currently changes status and writes a transition fact but does not patch
  `ended_at`, `exit_code`, or `error_detail` on the schedule-run row.

Five minutes gives ordinary subprocess tasks time to complete before recovery, 90 seconds gives
three 30-second Studio ticks before heartbeat staleness, and three expiries bounds recovery. Only
the “conservative bounded recovery” intent is recorded; no workload measurement justifies the exact
five-minute or three-attempt values.

Why this way: a lease turns worker ownership into database state and makes restart recovery
possible without a broker. Reusing the subprocess launcher avoids a second action vocabulary. The
cost is queue latency tied to Studio, sequential execution inside a claim pass, and no lease
renewal.

### D5 — Capabilities route and order; they do not authorize

Capability classification is centralized:

```python
# lionagi/studio/scheduler/capabilities.py
DEFAULT_CAPABILITY_CLASS = "eligibility"

CAPABILITY_CLASSES: dict[str, str] = {
    "gpu-exclusive": "serialization",
    "warmed-cache": "affinity",
}

def worker_can_serve(
    required_capabilities: Iterable[str] | None,
    advertised_capabilities: Iterable[str] | None,
) -> bool: ...

def affinity_score(...) -> int: ...

def host_scoped_concurrency_key(
    host: str,
    required_capabilities: Iterable[str] | None,
) -> str | None: ...
```

Exact routing semantics:

- Unknown tokens default to `eligibility`.
- Eligibility and serialization tokens must be a subset of worker advertisements.
- `execution_target` must be in the worker target set. A null/empty stored target is claimable by
  any worker, though `TaskApplication` validation always requires a known non-empty target.
- Affinity tokens never filter. Candidate order is descending count of matching affinity tokens,
  with the original oldest-first SQL order retained for ties by stable sort.
- Serialization tokens are sorted and folded into
  `<hostname>:<token+token...>`. No serialization tokens yields null concurrency key.
- Before claiming, the worker reads concurrency keys of all `running` rows. A matching key blocks a
  candidate.
- Once one key is claimed in a pass, it remains in the pass-local blocked set even if execution
  finishes before later candidates are examined. The next tick may admit the next row.
- This is advisory serialization. A machine-local resource such as a GPU still requires an
  authoritative worker-side lock; the queue key cannot prevent another local process outside this
  queue from using it.
- Tokens do not grant tool or data permission. A worker that can route a task must still execute
  under the relevant authorization policy.

Why this way: a small declarative map keeps placement rules testable and prevents token-name
branches from spreading through submit and worker code. Separating eligibility, affinity, and
serialization avoids turning a warm cache preference into a hard scheduling failure.

## Consequences

- Ad-hoc queued work survives daemon restart before claim.
- Guarded leases prevent competing claimants and block stale terminal writes after ownership
  changes.
- Reusing `schedule_runs` gives one run-shaped storage entity but does not yet give scheduled fires
  the queue's semantics.
- Public clients cannot submit through CLI/HTTP, and retries cannot deduplicate submission.
- Workflow actions may be stored but remain permanently ineligible for the shipped worker.
- Queue service time is coupled to the 30-second Studio tick and sequential child execution.
- The transition helper is load-bearing but incomplete: contributors must check the closed graph,
  guard/patch allowlist, and absent idempotency-key storage before adding a state.
- Capability labels improve routing without becoming a permission system; machine-local locking
  remains an execution responsibility.
- Reversing D2 would require migrating historical ad-hoc rows to a new task table. D3-D5 are easier
  to replace behind their typed functions, but pending leases and transition history require a
  compatibility plan.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Add `li task submit` and HTTP task submission as thin bindings over `submit_task()`; acceptance: both bindings apply identical validation and persist the same provenance fields. | M | (filled at issue-open time) |
| 2 | Implement atomic submit idempotency; acceptance: repeating one non-empty idempotency key returns the original task id and never inserts a second row. | M | (filled at issue-open time) |
| 3 | Admit fixed `workflow` tasks to an eligible worker adapter; acceptance: a queued fixed workflow is leased, executed, and terminalized without bypassing the lease guard. | M | (filled at issue-open time) |
| 4 | Complete the schedule-run transition owner and event record; acceptance: every declared lifecycle edge uses one guarded API and produces one durable transition fact. | M | (filled at issue-open time) |
| 5 | Add lease renewal or explicitly constrain admitted task duration below the lease; acceptance: a healthy long-running worker cannot lose ownership solely because execution exceeds the initial TTL. | M | (filled at issue-open time) |
| 6 | Persist worker execution outcome columns atomically with terminal transition; acceptance: `ended_at`, `exit_code`, error detail, status, and the transition fact cannot disagree after a crash. | M | (filled at issue-open time) |

## Alternatives considered

### A parallel `tasks` table

A dedicated table could avoid inherited schedule columns such as `fired_at` and make the domain
name clearer. It lost because `schedule_runs` already carries identity, lifecycle, invocation,
reason, lease, and provenance columns. A second table would duplicate the claim protocol and split
operator history.

### Accept but ignore `idempotency_key`

This would preserve forward API compatibility and let callers start sending keys. It lost because
a retry would create a duplicate while appearing safe. Explicit rejection truthfully exposes the
missing guarantee.

### External broker

A broker could provide mature leasing, visibility timeouts, and multi-worker scale. It lost for the
current single-operator deployment because the database already supplies durable rows and guarded
transactions. Adding a service would increase operations cost before the existing path reaches its
limits.

### Unguarded `SELECT` then `UPDATE`

This would simplify SQL. It lost because two workers could read the same queued row and both launch
it. The compare-and-swap result is the ownership decision.

### Unbounded lease requeue

Always returning expired work to `queued` would maximize retries. It lost because permanently
failing or non-idempotent tasks could churn forever. Three attempts makes terminal failure
inspectable, while the exact cap remains tunable future policy.

### Heartbeat as lease ownership

Keeping a worker heartbeat fresh could be treated as proof that all of its tasks remain owned. It
lost because one live worker can still hang a single task. Per-row lease expiry is the recovery
fact; heartbeat affects only eligibility for new claims.

### Capabilities as permissions

Using the same token to route and authorize would reduce concepts. It lost because advertisements
are worker placement claims, not trusted grants, and an advisory concurrency key cannot secure a
machine resource.

### Hard affinity filtering

Requiring `warmed-cache` advertisements would improve locality when present. It lost because an
otherwise capable worker would leave work queued indefinitely merely because a performance hint was
absent. Stable preferential ordering retains the optimization without turning it into availability
policy.

### In-memory semaphore instead of persisted concurrency key

A semaphore would be simpler for one worker process. It lost because queue admission must remain
visible across restarts and future workers. The database key is still only advisory, but it prevents
known queue claim overlap without pretending to replace the OS resource lock.

## Notes

An earlier revision of this record transcribed the pre-registry validator behavior; the unified lifecycle policy registry (`lionagi/state/lifecycle/policy.py`) reconciled the schedule-run and dispatch vocabularies, terminal sets, and edge graphs, and the corrected text above reflects that registry.

The transition module's top docstring names both `dispatch` and `schedule_run`, and
`_ENTITY_TABLES`, the vocabulary, and production callers include `schedule_run`. The executable
contract is the code shape recorded above, including its deliberately restricted graph and inactive
idempotency-key field.
