# ADR-0057: Operational lifecycle and transition audit

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: persistence-state
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0017, v0-0024, v0-0025, v0-0028

## Context

Operational entities do not share one universal lifecycle. Sessions and invocations use a
seven-value execution vocabulary that includes `completed_empty`; shows, plays, teams, schedule
runs, and dispatches each use vocabularies shaped by their own workflow. These values are stored on
their respective relational rows.

This ADR answers six concrete problems in the current code:

**P1 — One flat enum would erase domain meaning.** `merged` is meaningful for a play but not a
session; `completed_empty` distinguishes an execution that produced no trusted evidence; `acked`
belongs to dispatch transport. Consumers must know which vocabulary applies to each row.

**P2 — Current state alone is insufficient for diagnosis.** Six entity types store a current reason
beside current status, while `status_transitions` records reason-bearing changes over time. Both
views must be written together or they can disagree.

**P3 — Concurrent writers need an explicit conflict contract.** Reapers, operators, schedulers, and
normal teardown can act on the same record. Status membership and optional `updated_at` guards must
prevent a stale decision from overwriting a newer one.

**P4 — Terminal records need a repair boundary.** A terminal status must not silently return to an
active state or oscillate to another terminal value. Deliberate repairs still need a path, but the
actor and justification must be recorded.

**P5 — Coverage is heterogeneous.** `StateDB.update_status()` covers six reason-bearing entity
types. The smaller adapter in `lionagi/state/transitions.py` covers both dispatch and schedule run,
so schedule run currently has two sanctioned-looking paths with different policies and results.
Creation methods set initial status without a transition row. Branches and engine runs have status
columns outside the six-type reason registry.

**P6 — Session health is an observation, not stored lifecycle.** Liveness depends on current time,
process evidence, produced artifacts, activity, and stale locks. Persisting that classification as
the lifecycle status would make it stale as soon as those observations changed.

There is no implemented `NormalizedState`, generic delivery axis, policy-version field,
severity/tone evaluator, or universal evidence-list model. This retrospective ADR records the
partial system that exists; ADR-0058 defines the narrower consolidation target.

| Concern | Decision |
|---------|----------|
| Status meaning | D1: Preserve distinct per-entity vocabularies and terminal sets. |
| Reason and history data | D2: Store current reason on managed rows and append reason-bearing history. |
| Guarded mutation | D3: `StateDB.update_status()` validates, locks, compare-and-sets, and appends atomically. |
| Terminal integrity | D4: Reject terminal changes unless an identified, justified override is audited. |
| Coverage boundary | D5: Treat the current two transition paths and creation gaps as explicit limitations. |
| Session diagnostics | D6: Derive session health at read time, separate from persisted lifecycle. |

This ADR deliberately does **not** decide:

- The target unified lifecycle API and migration. ADR-0058 owns that aspirational design.
- Dispatch delivery guarantees, retries, acknowledgement, or purge. ADR-0059 owns those semantics.
- A generic process-health model for branches, workers, or arbitrary entities. The shipped
  classifier is session-specific.
- Complete legal-edge graphs for every vocabulary. The current code supplies only an integrity
  floor plus a partial schedule-run graph; delta 3 requires the full decision.

## Decision

### D1 — Per-entity vocabularies remain distinct

Each entity retains its declared status vocabulary. A single flat lifecycle enum is not current
architecture.

**The contract.** Current relational and Python vocabularies are:

| Entity | Declared/current values | Terminal set used by `StateDB.update_status()` |
|--------|-------------------------|------------------------------------------------|
| `session` | `running`, `completed`, `completed_empty`, `failed`, `timed_out`, `aborted`, `cancelled` | all except `running` |
| `invocation` | same seven execution values | all except `running` |
| `show` | `active`, `completed`, `aborted`, `imported` | `completed`, `aborted` |
| `play` | `pending`, `prepared`, `running`, `running_complete`, `gated`, `gate_failed`, `redoing`, `merged`, `escalated`, `blocked`, `aborted_after_finish` | `merged`, `escalated`, `gate_failed`, `blocked`, `aborted_after_finish` |
| `team` | `active`, `archived` | `archived` |
| `schedule_run` schema | `queued`, `waiting_dependency`, `running`, `retry_wait`, `completed`, `failed`, `timed_out`, `skipped`, `cancelled` | see mismatch below |
| `dispatch` | `pending`, `delivering`, `delivered`, `acked`, `dead_letter`, `expired` | enforced by table CHECK, not the six-type registry |

The reason-bearing entity registry is exactly:

```python
VALID_ENTITY_TYPES = frozenset({
    "session",
    "show",
    "play",
    "invocation",
    "team",
    "schedule_run",
})

ENTITY_TYPE_TO_TABLE = {
    "session": "sessions",
    "show": "shows",
    "play": "plays",
    "invocation": "invocations",
    "team": "teams",
    "schedule_run": "schedule_runs",
}
```

Aliases accepted by that registry are `run -> session` and plural table names to their singular
forms.

Code anchors: `lionagi/state/db.py`; `lionagi/state/reasons.py`;
`lionagi/state/schema_meta.py`.

**Exact semantics.**

- Unknown reason-bearing entity types raise `ValueError` before table selection.
- Session status may be SQL `NULL` for legacy or not-yet-classified rows; the seven non-null values
  are the Python vocabulary.
- Session and invocation deliberately share the same seven values and terminal set.
- A same-status write is permitted even when the value is terminal; it refreshes current reason and
  appends history rather than counting as leaving terminal.
- The schedule-run declarations are reconciled through the lifecycle policy registry. Both the
  `StateDB.update_status()` validator and the guarded adapter source the nine-value vocabulary and
  the five-value terminal set (which includes `timed_out`) from the registered policy
  (`lionagi/state/db.py:284-291`, `lionagi/state/lifecycle/policy.py:305-320`); `pending` is not
  admitted by either surface.
- The registered schedule-run edge graph declares `queued -> {waiting_dependency, running, skipped,
  cancelled}`, `waiting_dependency -> {queued, cancelled}`, `running -> {completed, failed,
  timed_out, retry_wait, queued, cancelled}`, and `retry_wait -> {queued, cancelled}`; terminal
  states have no outgoing edges (`lionagi/state/lifecycle/policy.py:321-326`).
- Dispatch is absent from `VALID_ENTITY_TYPES`; its adapter maps it directly to
  `dispatch_outbox` and validates only its reason code plus database CHECK at write time.

**Why this way.** The vocabularies encode different domain outcomes. Keeping them separate prevents
transport acknowledgement or play merge state from becoming misleading generic lifecycle values.
The schedule-run discrepancy is retained here as current truth, not rationalized as a coherent
design.

### D2 — Current reason plus append-only transition history

For the six registered entity types, current status and reason live on the entity row while
`status_transitions` stores reason-bearing history.

**The contract.** Each managed row has:

```text
status                 TEXT
status_reason_code     TEXT NULL
status_reason_summary  TEXT NULL
status_evidence_refs   JSON NULL
updated_at              FLOAT  # present on every table used by update_status()
```

Transition history is:

```text
status_transitions
  id               TEXT PRIMARY KEY
  entity_type      TEXT NOT NULL
  entity_id        TEXT NOT NULL
  previous_status  TEXT NULL
  status           TEXT NOT NULL
  reason_code      TEXT NOT NULL
  reason_summary   TEXT NULL
  evidence_refs    JSON NULL
  source           TEXT NOT NULL
  actor            TEXT NULL
  created_at       FLOAT NOT NULL
  metadata         JSON NULL

INDEX(entity_type, entity_id, created_at)
INDEX(reason_code, created_at)
INDEX(created_at)
```

The controlled reason-code registry contains these shipped groups:

```text
run.*:       run.started.ok, run.completed.ok, run.completed_empty.no_evidence,
             run.failed.exit_nonzero, run.failed.exception,
             run.failed.missing_artifact, run.failed.missing_cwd,
             run.failed.escalated, run.timed_out.deadline, run.aborted.user,
             run.cancelled.sigint, run.cancelled.sigterm, run.cancelled.system,
             run.cancelled.orchestrator, run.cancelled.manual_kill,
             run.cancelled.force_kill, run.cancelled.stale_auto,
             run.paused.operator, run.queued.lease_expired,
             run.failed.lease_attempts_exhausted
session.*:   session.stale.no_heartbeat, session.orphaned.no_process,
             session.zombie.stale_locks, session.phantom.process_dead,
             session.phantom.missing_artifacts
play.*:      play.pending.waiting_on_deps, play.pending.ready,
             play.blocked.invalid_deps, play.blocked.dep_failed,
             play.gate_failed.verdict, play.escalated.gate_twice, play.merged.ok
show.*:      show.blocked.no_ready_plays, show.completed.final_gate,
             show.aborted.operator
schedule.*:  schedule.fired.due, schedule.skipped.overlap,
             schedule.skipped.missed_fire, schedule.deferred.capacity,
             schedule.budget.exhausted
dispatch.*:  dispatch.pending.enqueued, dispatch.delivering.attempt,
             dispatch.delivered.transport_ok, dispatch.pending.retry_backoff,
             dispatch.dead_letter.max_attempts, dispatch.dead_letter.ack_timeout,
             dispatch.expired.deadline, dispatch.acked.consumer
legacy:      legacy.imported
```

All codes except `legacy.imported` follow three lowercase dot-separated segments; compound detail
belongs in the summary rather than extending the code.

Code anchors: `lionagi/state/schema_meta.py`; `lionagi/state/reasons.py`;
`lionagi/state/db.py`.

**Exact semantics.**

- An unregistered reason code raises `ValueError` before mutation.
- Valid `source` values for `StateDB.update_status()` are exactly `executor`, `agent`, `admin`, and
  `system`; any other source raises `ValueError`.
- `evidence_refs=None` is persisted as an empty list in both current row and history.
- `reason_summary` defaults to an empty string; `metadata` defaults to SQL/JSON null when omitted.
- Current reason fields are denormalized for cheap reads. The transition row is the history record;
  there is no foreign key from a transition to its entity because the table covers several entity
  tables.
- Successful status updates always append a new transition id, including same-status reason
  refreshes.
- Initial inserts for sessions, invocations, shows, plays, and schedule runs set status directly and
  do not append a creation transition. History therefore begins at the first reason-bearing update,
  not necessarily at entity creation.
- The transition table is append-only through these APIs, but the database schema does not forbid a
  caller with raw SQL access from updating or deleting its rows.

**Why this way.** Denormalized current reason answers the common question without scanning history;
the append supplies the diagnostic trail. Writing both in one transaction avoids a current row that
claims a reason absent from history or a history row whose state never became current.

### D3 — `StateDB.update_status()` is the reason-bearing mutation path

For the six registered entity types, `StateDB.update_status()` is the sanctioned reason-bearing
mutation path. It validates inputs, reads under the backend's write discipline, applies a storage
compare-and-set, updates current reason, and appends history in one transaction.

**The contract.**

```python
async def update_status(
    self,
    entity_type: str,
    entity_id: str,
    *,
    new_status: str,
    reason_code: str,
    reason_summary: str = "",
    evidence_refs: list[dict[str, Any]] | None = None,
    source: str = "executor",
    actor: str | None = None,
    metadata: dict[str, Any] | None = None,
    expected_statuses: set[str | None] | frozenset[str | None] | None = None,
    expected_updated_at: float | None = None,
    extra_fields: dict[str, Any] | None = None,
    override: bool = False,
    override_actor: str | None = None,
    override_justification: str | None = None,
) -> bool: ...
```

The only same-row companion field currently allowlisted is:

```python
EXTRA_STATUS_WRITE_FIELDS_BY_ENTITY_TYPE = {
    "session": frozenset({"ended_at"}),
}
```

The successful transaction is:

```text
validate entity, reason, source, target, extra fields
  -> SELECT current status (PostgreSQL adds FOR UPDATE)
  -> check expected_statuses
  -> check terminal policy
  -> UPDATE entity
       SET status, current reason, evidence, extra fields, updated_at
       WHERE id AND NULL-safe previous-status CAS
       [AND updated_at = expected_updated_at]
  -> INSERT status_transitions
  -> COMMIT
```

Code anchor: `lionagi/state/db.py`.

**Exact semantics.**

- Validation errors occur before the write transaction except checks that require the current row.
- A missing entity raises `LookupError`; it is not a conflict result.
- `expected_statuses=None` disables the caller-supplied membership guard. A set may contain `None`
  to accept a SQL-null status.
- If current status is not in `expected_statuses`, the method returns `False` with no row, history,
  or admin-event mutation.
- The entity update reasserts the status read earlier using portable NULL-safe equality. This is a
  database compare-and-set, not only a Python pre-check.
- If `expected_updated_at` is supplied and that version changed, zero updated rows returns `False`.
  The timestamp is the version token; there is no separate integer version column.
- If the storage status compare-and-set loses without an optional timestamp guard, the method raises
  `RuntimeError` rather than silently treating an unexpected race as an ordinary conflict.
- Unknown `extra_fields` raise `ValueError`. Allowed companion fields are applied in the same update
  as status and reason.
- A database error during entity update or history insert rolls back both.
- A successful write returns `True` after commit.

**Why this way.** The status plus timestamp guards distinguish a stale observation from a current
decision and give callers a non-exceptional skip result where expected. Reasserting the previous
status in SQL closes the read-to-write gap even if code bypasses in-process serialization.

### D4 — Terminal changes require an audited override

Once an entity reaches the terminal set in D1, a write that changes the value is rejected unless the
caller supplies an explicit override actor and justification. Same-status writes are not terminal
exits and remain allowed.

**The contract.** Rejection writes:

```json
{
  "action": "status_transition_rejected",
  "target_id": "<entity id>",
  "details": {
    "entity_type": "<canonical type>",
    "previous_status": "<terminal value>",
    "attempted_status": "<new value>",
    "reason_code": "<registered code>",
    "source": "<status source>"
  },
  "actor": "<actor or source>"
}
```

Override writes:

```json
{
  "action": "status_transition_override",
  "target_id": "<entity id>",
  "details": {
    "entity_type": "<canonical type>",
    "previous_status": "<terminal value>",
    "new_status": "<new value>",
    "reason_code": "<registered code>",
    "justification": "<required text>"
  },
  "actor": "<required override_actor>"
}
```

`admin_events` stores `id`, `created_at`, `action`, nullable `target_id`, JSON `details`, and
non-null `actor`.

Code anchors: `lionagi/state/db.py`; `lionagi/state/schema_meta.py`.

**Exact semantics.**

- `override=True` without both a non-empty actor and justification raises `ValueError` before row
  lookup.
- A rejected terminal change inserts and commits its admin event but does not update the entity or
  append `status_transitions`.
- After that commit, the method raises `TransitionRejectedError` carrying entity type, entity id,
  previous status, and attempted status.
- An override inserts the admin event, updates status/current reason, and appends transition history
  in one transaction. Any failure rolls all three back.
- A same-status terminal write bypasses rejection and override handling. It updates reason and
  appends ordinary history.
- `expected_statuses` is evaluated before terminal rejection. A guard mismatch returns `False` and
  does not emit a rejected-attempt admin event.

**Why this way.** Terminal integrity is an operational safety floor, not an absolute ban on repair.
Committing rejection evidence before raising makes forbidden attempts observable; requiring an
identified justification makes exceptional repair distinguishable from normal flow.

### D5 — Current coverage is partial and split across two APIs

The comprehensive guarantees above apply to the six registered entity types only. A smaller
Pydantic-based adapter independently performs guarded transitions for dispatch and schedule run.
Creation and several status-bearing tables remain outside a universal lifecycle service.

**The contract.** The smaller adapter exposes:

```python
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

Its registered tables and allowed guard/patch columns are:

```python
_ENTITY_TABLES = {
    "dispatch": "dispatch_outbox",
    "schedule_run": "schedule_runs",
}

_GUARD_PATCH_COLUMNS = {
    "dispatch": {"attempt", "next_attempt_at", "last_error"},
    "schedule_run": {"leased_by", "lease_expires_at", "lease_attempts"},
}
```

Code anchors: `lionagi/state/transitions.py`; `lionagi/state/db.py`.

**Exact semantics.**

- Unsupported adapter entity type or guard/patch column raises `ValueError` before mutation.
- Missing rows raise `LookupError`.
- `from_state=None` disables the request-level state precondition; the final SQL update still guards
  against the status read inside the transaction.
- A `from_state` mismatch or extra guard mismatch returns `TransitionResult(applied=False,
  conflict=True, ...)` with no write.
- A generated `transition_id` is returned even for conflicts, although no history row with that id
  is inserted.
- Patch fields update atomically with status and history. Guard fields are reasserted in the SQL
  `WHERE` clause.
- `idempotency_key` is required by the model but is not persisted, queried, or used to deduplicate
  calls in the shipped adapter.
- `event_id` is always `None` in the returned success shape; there is no separate event append.
- Dispatch has no adapter-level allowed-edge graph. Any current-to-target pair that passes the
  caller's CAS and the table's six-value CHECK can be written through this function.
- Schedule run has the partial closed graph described in D1.
- `StateDB.update_status()` and this adapter both cover `schedule_run`. Both source the vocabulary
  and terminal set from the lifecycle policy registry; they still differ in guard/patch allowlists,
  return types, and edge-enforcement behavior.
- Branches and engine runs have stored statuses but are not in the six-type reason registry.
- Initial entity inserts do not use either transition API, except `enqueue_dispatch()`, which
  explicitly inserts its initial transition in the same transaction.
- `update_session()`, `update_invocation()`, `update_show()`, `update_play()`, and
  `update_schedule_run()` route a supplied status through `update_status()` and then write remaining
  fields separately. They do not automatically pass those remaining fields as `extra_fields`, so a
  status plus companion update can span two transactions.

**Why this way.** The smaller adapter was introduced for guarded dispatch and later schedule-run
queue work without replacing the older facade. It closes specific claim races but leaves duplicate
policy. Recording that overlap is necessary: callers cannot infer universal conflict or audit
semantics from the presence of a `transition()` function.

### D6 — Session health is derived at read time

Session health is a pure, session-scoped diagnostic derived from a session mapping and
caller-supplied observations. It is not stored as lifecycle.

**The contract.**

```python
class SessionHealth(str, Enum):
    HEALTHY = "healthy"
    IDLE = "idle"
    UNRESPONSIVE = "unresponsive"
    STALE = "stale"
    ORPHANED = "orphaned"
    ZOMBIE = "zombie"

def classify_session_health(
    session: dict[str, Any],
    *,
    now: float,
    process_alive: bool | None,
    has_artifacts: bool,
    has_stale_locks: bool,
) -> SessionHealth: ...

def worst_health(values: list[SessionHealth]) -> SessionHealth: ...

def staleness_check(
    session: dict[str, Any],
    *,
    now: float | None = None,
) -> str | None: ...
```

Thresholds are:

```text
idle boundary:             3,600 seconds
agent/play stale boundary: 21,600 seconds (6 hours)
flow/fanout/show-play:      43,200 seconds (12 hours)
unknown/missing kind:       21,600 seconds (6 hours)
```

Code anchors: `lionagi/state/health.py`; `lionagi/state/staleness.py`.

**Exact semantics.**

- Terminal execution statuses classify `zombie` only when stale locks remain; otherwise they are
  `healthy`. Artifact presence alone never makes a terminal session a zombie.
- A null or absent status is treated as completed and therefore follows the terminal branch.
- Activity time is the first truthy value of `last_message_at`, `updated_at`, `started_at`, then 0.
- For a running session with no confirmed live process, no artifacts, and zero messages,
  classification is `orphaned` before any activity-age test.
- Confirmed process death (`False`) yields `stale` even with recent activity, unless the earlier
  orphan condition applies.
- Unknown process state (`None`) with recent activity yields `healthy` up to one hour, `idle` above
  one hour through the kind threshold, then `stale`.
- A confirmed live process yields `healthy` up to one hour, `idle` above one hour through the kind
  threshold, then `unresponsive`.
- `worst_health([])` returns `healthy`; otherwise it chooses the maximum severity in the exact order
  shown by the enum list above.
- `staleness_check()` is narrower: non-running returns `None`; a running row older than its kind
  threshold returns the string `stale`; it does not consider process, artifacts, messages count,
  locks, idle, orphaned, zombie, or unresponsive.
- The one-hour idle boundary is documented as the quiet-session floor. The 6/12-hour split gives
  multi-operation flows more headroom than single executions. No measured calibration study is
  recorded for the exact values.

**Why this way.** Process and file observations are volatile and may be unavailable. Passing them
into a pure classifier makes uncertainty explicit and keeps persisted lifecycle from oscillating as
observers come and go. The narrower staleness helper remains a duplicate evaluation path, which is
why consolidation is a delta rather than claimed current architecture.

## Consequences

- Current status and reason are cheap to read, while append history preserves diagnostic detail.
- Compare-and-set guards and terminal protection prevent important classes of stale-writer
  overwrite without forcing unrelated workflows into one enum.
- Coverage is heterogeneous. Callers cannot infer that every status field has a reason, creation
  event, complete edge graph, or the same conflict result.
- The two schedule-run paths are a concrete policy-drift risk: a value may be admitted by the schema
  but rejected by one or both service paths.
- Same-status history can be used to record reason refreshes, but consumers counting transitions
  must not assume every row changes the status value.
- Reversing D1 would require data migration and consumer changes. Reversing D2 would either make
  current reads slower or discard history. D3/D4 can be extracted behind the facade, as proposed by
  ADR-0058, without changing their observable guarantees.
- Health remains recomputable but cannot be reconstructed from the session row alone because some
  inputs are caller observations.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Reconcile the schedule-run database CHECK, status validator, terminal set, and guarded-transition vocabulary for `waiting_dependency`, `retry_wait`, and `timed_out`; acceptance requires one tested vocabulary in every sanctioned write path. | M | (filled at issue-open time) |
| 2 | Consolidate `StateDB.update_status()` and `lionagi/state/transitions.py` behind the lifecycle service in ADR-0058; acceptance requires one conflict result, one policy registry, atomic reason/history writes, and compatibility wrappers for existing callers. | M | (filled at issue-open time) |
| 3 | Define and enforce allowed transition graphs for sessions, schedule runs, dispatches, shows, and plays; acceptance requires tests for every allowed edge, every terminal edge, and the selected same-status reason-refresh rule. | M | (filled at issue-open time) |
| 4 | Make terminal companion fields such as `sessions.ended_at` part of the same guarded status transaction; acceptance requires teardown and wrapper paths to leave no crash window between terminal status and its companion fields. | S | (filled at issue-open time) |
| 5 | Emit a reason-bearing initial lifecycle event when creating every managed entity, as required by ADR-0058; acceptance requires `previous_status=NULL` creation history in the same transaction as the entity insert. | S | (filled at issue-open time) |
| 6 | Replace the overlapping session health and staleness entry points with one threshold source and evaluator; acceptance requires both current call patterns to return the same classification at threshold boundaries. | S | (filled at issue-open time) |

## Alternatives considered

### Universal `NormalizedState`

Persist one object with generic lifecycle, health, delivery, severity, tone, policy version, and
evidence fields for every entity. This would offer a uniform UI and query model. It lost as a
retrospective decision because none of that universal object or most of those axes exists, and the
axes are not equivalent: process health is derived, dispatch delivery is transport state, and
artifact verification is output evidence. ADR-0058 keeps only the shared mutation mechanics.

### Status column only, without reason/history

Store only the latest value. This would minimize writes and schema surface. It lost because terminal
repairs, reaper actions, and operational failures need to answer why and by whom a value changed.
The existing denormalized reason plus history supplies that evidence.

### History only, deriving current state by replay

Make transitions the sole authority and replay the last event for reads. This would eliminate
current/history dual writes and approach event sourcing. It lost because the requirement is audited
current state, not replay of all operational behavior; hot current-status queries are common, and
existing entity rows already carry status-dependent indexes and constraints.

### One unrestricted transition function

Allow any source status to any target as long as both values are in the entity vocabulary. This
would preserve maximum workflow flexibility and simplify policy. It lost as a desired architecture
because it permits terminal re-entry and cannot distinguish stale-writer conflicts from intentional
repairs. The current code still approximates this for many nonterminal edges, which is recorded as a
delta rather than hidden.

### Exceptions for every conflict

Raise whenever an expected status or version does not match. This would make failed writes loud.
It lost because optimistic conflicts are normal control flow for overlapping reapers and claimers.
The current APIs return `False` or a structured conflict for expected losses while reserving
exceptions for validation, missing rows, forbidden terminal changes, and anomalous unguarded CAS
losses.

### Persist session health

Write `healthy`, `idle`, `stale`, and related values onto the session. This would make dashboards
simple to query. It lost because classification depends on time and volatile process/file/lock
observations; a stored value would require a continuously correct observer and would still become
stale between sweeps.

### Keep `StateDB.update_status()` and `transitions.transition()` indefinitely

Treat the two paths as domain-specific implementations. This preserves all callers and avoids a
migration. It lost as the target because schedule run already overlaps both, and their entity sets,
edge validation, guard fields, companion patches, idempotency claims, and result shapes differ. The
current split is documented here only as retrospective truth.

### Treat entity creation as outside lifecycle history

Begin audit only at the first mutation. This matches current behavior and saves one insert per
entity. It lost as the target because a history without the initial reason cannot distinguish a
record created directly in its current state from one whose creation event is merely absent.

## Notes

`TransitionRequest.idempotency_key` is a shape-only field in the current adapter. Maintainers must
not claim transition deduplication until a durable key and replay result are implemented. Likewise,
the presence of terminal sets is not evidence of a complete legal-edge graph.
