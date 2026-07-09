# ADR-0033: Unified Entity State Model

**Status**: Proposed
**Date**: 2026-05-26
**Supersedes**: [ADR-0017](ADR-0017-session-lifecycle-status.md) §"Status vocabulary" (partial), [ADR-0025](ADR-0025-session-status-vocabulary.md) §"Expanded vocabulary" (full)
**Extends**: [ADR-0024](ADR-0024-session-health-and-admin-surface.md), [ADR-0028](ADR-0028-status-reason-model.md), [ADR-0029](ADR-0029-artifact-contract.md)
**Related**: [ADR-0030](ADR-0030-attention-queue.md), [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md), [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md)
**Depends on**: [ADR-0009](ADR-0009-sqlite-state-layer.md) (current persistence implementation)

## Context

The unifying principle these ADRs serve is the **evidence chain**: every state has reasons, every reason has evidence, every claim has evidence. Auditability is not an export feature — it is the data model. This ADR establishes the state-side of the chain (entity status with structured reasons); [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) establishes the knowledge-side (learned facts with structured evidence); both share `EvidenceRef` (defined here).

Status semantics are currently spread across five ADRs, each adding a
dimension without unifying the model:

| ADR | Contribution | Problem |
|-----|-------------|---------|
| ADR-0017 | Session lifecycle: `running`, `completed`, `failed`, `aborted` | Too coarse — no timeout, no cancel distinction |
| ADR-0025 | Expanded vocabulary: adds `timed_out`, `cancelled` | Solves coarseness, but applies only to sessions |
| ADR-0024 | Health classification: `healthy` → `zombie` enum | Separate axis, computed per-read, not persisted |
| ADR-0028 | Reason codes: structured "why" | Right primitive, but defined only for sessions |
| ADR-0029 | Artifact contract: delivery verification | No integration with status/health model |

The consequence is visible in the UI today:

- **Issue #1176**: "Failed + Healthy" badge stack. These are two
  correct statements from two separate axes displayed as if they were
  one status. Users read it as contradictory.
- **Issue #1162**: Dashboard "Stale: 0" contradicts "1 stuck >60m"
  because two different detectors evaluate staleness with different
  thresholds and different entity scopes.
- **Issue #1161**: Show status reads "Active" from SQLite while the
  detail page reads "Merged" from `_show.md`. Two sources, no
  reconciliation, no single model.

The Attention Queue (ADR-0030) cannot be built correctly without a
unified state model. It needs to sort items by severity across entity
types — runs, shows, plays, schedules, teams. If each entity type
computes severity differently, the queue is incoherent.

## Decision

### Separate entity state into three orthogonal dimensions

Every operational entity (run/session, show, play, invocation, schedule,
team) carries a **normalized state** composed of three independent fields
plus a reason chain:

```text
lifecycle_status × process_health × delivery_state → severity
                                                    + reason_code[]
                                                    + evidence_ref[]
```

These dimensions answer different operator questions:

| Dimension | Question | Example values |
|-----------|----------|----------------|
| `lifecycle_status` | Did the work finish? What happened? | `running`, `completed`, `failed`, `timed_out`, `cancelled`, `aborted` |
| `process_health` | Is the runtime/process okay? | `ok`, `running`, `idle`, `stalled`, `process_dead`, `orphaned` |
| `delivery_state` | Did it produce required outputs? | `passed`, `partial`, `missing`, `invalid`, `not_expected` |

`severity` and `tone` are derived, never stored:

| Field | Purpose | Values |
|-------|---------|--------|
| `severity` | Determines placement and attention priority | `critical`, `warning`, `info`, `neutral` |
| `tone` | Determines badge/indicator color | `danger`, `warning`, `info`, `success`, `neutral` |

### NormalizedState schema

```python
@dataclass
class NormalizedState:
    lifecycle: str

    outcome: str | None = None
    # succeeded | completed | merged | failed | timed_out
    # | cancelled | aborted | skipped | unknown

    health: str | None = None
    # ok | running | idle | degraded | stalled
    # | process_dead | orphaned | disconnected | unknown

    delivery: str | None = None
    # passed | partial | missing | invalid | not_expected | unknown

    severity: str = "neutral"
    # critical | warning | info | neutral

    tone: str = "neutral"
    # danger | warning | info | success | neutral

    reasons: list[StateReason] = field(default_factory=list)

    evaluated_at: float = 0.0
    policy_version: str = "v1"
    source: str = "backend"
    # backend | frontend_compat


@dataclass
class StateReason:
    code: str               # structured: "run.failed.exit_nonzero"
    message: str            # human: "Exit code 124 from python-tests"
    claim_status: str = "observed"
    # observed | inferred | hypothesis | verified | disputed | superseded
    confidence: float = 1.0
    # Confidence in this REASON explaining the state.
    # Distinct from Claim.confidence in ADR-0039, which measures
    # confidence in a learned fact being true.
    entity_type: str | None = None
    entity_id: str | None = None
    evidence: list[EvidenceRef] = field(default_factory=list)


@dataclass
class EvidenceRef:
    kind: str
    # Allowed kinds:
    #   message        — chat message in a session
    #   user_statement — explicit user assertion
    #   tool_result    — output from a tool call
    #   artifact       — produced file/report
    #   url            — web resource (include fetched_at, content_hash if available)
    #   file           — local file (include repo, commit_sha if available)
    #   model_inference — agent reasoning step
    #   human_assertion — human verified externally

    id: str | None = None
    session_id: str | None = None
    message_id: str | None = None
    tool_call_id: str | None = None
    artifact_id: str | None = None
    path: str | None = None
    url: str | None = None
    repo: str | None = None
    commit_sha: str | None = None
    content_hash: str | None = None
    fetched_at: float | None = None
    detail: str | None = None
    # Kind-specific descriptor:
    #   message         → relevant quote
    #   tool_result     → result summary
    #   model_inference → reasoning rationale
    #   user_statement  → paraphrased assertion
    #   artifact        → contract violation detail or note
    #   human_assertion → assertion text
```

### Severity derivation heuristic

Severity is computed from the three dimensions using a deterministic
priority cascade. The most severe condition wins:

```python
def derive_severity(state: NormalizedState) -> tuple[str, str]:
    """Returns (severity, tone)."""

    # Critical conditions
    if state.outcome in ("failed", "aborted"):
        return ("critical", "danger")
    if state.health in ("process_dead", "orphaned"):
        return ("critical", "danger")
    if state.delivery == "missing":
        return ("critical", "danger")
    if state.health == "stalled":
        return ("critical", "danger")
    if state.health == "misfired":
        return ("critical", "danger")

    # Warning conditions
    if state.outcome == "timed_out":
        return ("warning", "warning")
    if state.health in ("idle", "degraded", "disconnected"):
        return ("warning", "warning")
    if state.delivery in ("partial", "invalid"):
        return ("warning", "warning")

    # Info conditions
    if state.health == "running":
        return ("info", "info")
    if state.health == "due":
        return ("info", "info")
    if state.outcome == "skipped":
        return ("info", "neutral")

    # Success conditions
    if state.outcome in ("succeeded", "completed", "merged"):
        return ("neutral", "success")

    # Cancelled is intentional, not a problem
    if state.outcome == "cancelled":
        return ("neutral", "neutral")

    return ("neutral", "neutral")
```

The relationship between `severity` (4 values) and `tone` (5 values):

| severity | possible tones | when |
|----------|---------------|------|
| critical | danger | failures, dead processes, missing artifacts, stalls |
| warning | warning | timeouts, partial delivery, idle/degraded/disconnected |
| info | info, neutral | running, skipped |
| neutral | success, neutral | succeeded/completed/merged, cancelled, unknown |

Severity drives placement (attention queue inclusion, sort order, row border). Tone drives visual treatment (badge color). Both are pure functions of the three operational dimensions.

### Claim severity (knowledge, not operations)

Knowledge claims have their own severity derivation, defined in [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md). It is a parallel function, not unified with `derive_severity()` above, because claims are not operational entities — their severity is about knowledge health (disputed, low-confidence, superseded), not work health.

Both severity functions emit into the same Attention Queue ([ADR-0030](ADR-0030-attention-queue.md)) using identical `severity` and `tone` values.

### Per-entity lifecycle vocabularies

Each entity type defines its allowed lifecycle states. The `outcome`
field uses the entity-specific terminal status:

#### Runs / Sessions

```text
Lifecycle: pending → running → completed | failed | timed_out | cancelled | aborted
Health:    ok | running | idle | stalled | process_dead | orphaned
Delivery:  passed | partial | missing | not_expected
```

Extends ADR-0025's six-value vocabulary unchanged. ADR-0024's health
classification maps directly to the `health` field.

#### Shows

```text
Lifecycle: draft → planned → active → completed | merged | failed | archived
Health:    ok | running | blocked
Delivery:  passed | partial | missing | not_expected
```

#### Plays

```text
Lifecycle: planned → queued → running → run_complete → merged | failed | blocked | timed_out | cancelled | skipped
Health:    ok | running | stalled
Delivery:  passed | partial | missing | not_expected
```

#### Invocations

```text
Lifecycle: pending → running → succeeded | failed | timed_out | cancelled | skipped
Health:    (not applicable — invocations are short-lived)
Delivery:  (not applicable)
```

#### Schedules

```text
Lifecycle: enabled → paused → disabled
Health:    ok | due | running | misfired
Delivery:  (last_run_outcome carries delivery state)
```

#### Teams

```text
Lifecycle: active → idle → blocked → closed
Health:    ok | orphaned
Delivery:  (not applicable)
```

### State transition validity

Lifecycle transitions are NOT arbitrary. Each entity type has a state machine; the backend rejects invalid transitions. This addresses issues #1162, #1171, #1172, #1176 — sessions stuck in null/non-terminal states because nothing enforced terminal transitions.

**Enforcement points**:

1. **Write-side**: Any code path writing a lifecycle value MUST go through `state.transition(entity_type, entity_id, new_lifecycle, reason)`. Direct UPDATE on `status` columns is forbidden (enforced by code review, not SQL).
2. **Terminal enforcement**: When a process exits or a watchdog detects death, the corresponding lifecycle MUST land in a terminal value within the entity's vocabulary. Sessions that exit without a terminal status are auto-transitioned to `aborted` with reason `system.health.process_dead_no_terminal`.
3. **Phantom reconciliation**: A reaper job (per [ADR-0024](ADR-0024-session-health-and-admin-surface.md)) detects entities with non-terminal lifecycle AND `health in (process_dead, orphaned)` AND age > threshold. These are auto-transitioned to `aborted` with appropriate reason.

**Per-entity transition graphs**: Live in `lionagi/state/transitions/` as one module per entity type. Each module exports a `VALID_TRANSITIONS: dict[str, set[str]]` mapping current → allowed nexts. The protocol does not specify them inline because they evolve with operational learning; the modules are version-controlled and tested.

### Reason code namespace

Reason codes use a hierarchical dot-separated namespace:

```json
{entity_type}.{dimension}.{cause}
```

Examples:

| Code | Meaning |
|------|---------|
| `run.failed.exit_nonzero` | Process exited with non-zero code |
| `run.failed.artifact_contract` | Required artifact not produced (ADR-0029) |
| `run.health.process_dead` | PID check failed, process no longer running |
| `run.health.stalled` | No message activity beyond threshold |
| `run.delivery.missing` | Expected output files not found |
| `show.failed.critical_path_blocked` | A critical-path play failed |
| `play.blocked.dependency_failed` | Upstream play in `depends_on` failed |
| `play.blocked.dependency_invalid` | Upstream play name in `depends_on` doesn't exist |
| `schedule.health.misfired` | Scheduled fire time passed without execution |
| `team.health.orphaned` | Team's parent show/play is terminal but team is still active |
| `knowledge.disputed.conflicting_evidence` | Claim has evidence refs supporting contradictory positions |
| `knowledge.disputed.user_rejection` | Human operator marked claim disputed |
| `knowledge.superseded.newer_observation` | Newer claim with stronger evidence replaced this one |
| `knowledge.stale.unverified_too_long` | Claim aged past verification budget without confirmation |

**Authoritative registry**: The complete reason-code namespace lives in `lionagi/state/reason_codes.py` as a Python module (one constant per code with docstring). The table above is illustrative; the module is canonical. Any code emitted at runtime MUST appear in the registry — this is enforced by a unit test.

New codes can be added without schema changes. The namespace convention
is enforced by validation, not by SQL CHECK.

### Backend owns state evaluation

The backend computes `NormalizedState` for every entity and includes it
in API responses. The frontend renders it. This is the permanent
architecture:

```text
Backend evaluates operational truth.
Frontend renders it.
```

During migration, the frontend MAY contain a compatibility derivation
layer (`compat_derive_*` functions) that fills in `NormalizedState` when
the backend hasn't been updated yet. These functions MUST set
`source = "frontend_compat"` so the transition is traceable.

### State display contract

The UI renders compound state as a flat chain, not stacked badges:

```text
Failed · Infra OK · Trace present
Running · Stalled · Artifacts missing
Completed · Artifact contract passed
Timed out · No review.md produced
```

The first segment is always `outcome`. The second is `health` (omitted
if `ok` and outcome is terminal). The third is `delivery` (omitted if
`not_expected` or `passed` and outcome is terminal success).

Severity determines:

- Row left-border color
- Attention queue inclusion
- Sort priority in tables
- Primary icon in badges

Tone determines:

- Badge background/text color

## Consequences

**Positive**

- Single state model across all entity types — tables, dashboard,
  attention queue, and detail pages all render the same `NormalizedState`.
- "Failed + Healthy" becomes "Failed · Infra OK" — explicit and
  non-contradictory.
- Stale/stuck disagreement resolved: one heuristic, one threshold
  config, one result.
- Reason codes enable failure clustering (ADR-0030), queryable cause
  analysis, and structured alerting.
- Evidence refs connect state to proof — auditable.
- Frontend compatibility layer makes migration incremental.

**Negative**

- Every API response grows by ~200 bytes per entity (the state object).
- Backend must compute health and delivery on every read until indexed
  materialized state is implemented.
- Existing ADRs (0017, 0024, 0025, 0028) are partially superseded —
  increases the ADR cross-reference burden.

## Migration

This ADR partially supersedes [ADR-0017](ADR-0017-session-lifecycle-status.md) and [ADR-0025](ADR-0025-session-status-vocabulary.md), and extends [ADR-0024](ADR-0024-session-health-and-admin-surface.md), [ADR-0028](ADR-0028-status-reason-model.md), [ADR-0029](ADR-0029-artifact-contract.md). Migration order:

1. **Backend computes NormalizedState** for all entity reads. The three dimensions (lifecycle, health, delivery) source from existing columns or are computed inline. No DB migration required for read path.
2. **Frontend compat derivation** lands as `compat_derive_normalized_state(entity)` functions, called when backend response lacks `NormalizedState`. All compat-derived states set `source = "frontend_compat"`.
3. **Backend persistence migration**: ALTER TABLE adds health/delivery/reason_codes columns. Backfill computes from existing data. New writes use the new columns.
4. **Frontend switches to backend-only rendering**. Compat layer removed entity-by-entity as backend coverage verified.
5. **Legacy single-status field deprecated**. Reads still served for one release. Then removed.

The frontend compat layer is the migration bridge. Both reads (backend has it / backend doesn't) are valid during the transition; the `source` field makes it traceable.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Keep dimensions separate (status + health as independent API fields) | Frontend still has to compose them; no single severity sort; "Failed + Healthy" persists |
| Frontend-only derivation (no backend state computation) | Two browser tabs can derive different states from stale caches; no single source of truth for attention queue |
| Store full NormalizedState as JSON column | Not queryable by individual fields; can't index on health or delivery |
| Single flat status enum with 30+ values | Combinatorial explosion; can't independently query "all failed regardless of health" |

## References

- [ADR-0017](ADR-0017-session-lifecycle-status.md) — Session Lifecycle and Status Derivation (partially superseded)
- [ADR-0024](ADR-0024-session-health-and-admin-surface.md) — Session Health Classification and Admin Surface (extended)
- [ADR-0025](ADR-0025-session-status-vocabulary.md) — Session Status Vocabulary (fully superseded)
- [ADR-0028](ADR-0028-status-reason-model.md) — Status Reason Model (extended)
- [ADR-0029](ADR-0029-artifact-contract.md) — Artifact Contract (extended)
- [ADR-0030](ADR-0030-attention-queue.md) — Attention Queue (consumes severity)
- [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md) — Frontend Data & State Architecture
- [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) — Knowledge Substrate (parallel claim model)
- Issue #1161: Show status stuck at "Active"
- Issue #1162: Dashboard stale/stuck contradiction
- Issue #1171: Terminal status enforcement
- Issue #1172: Phantom session reaper
- Issue #1176: "Failed + Healthy" contradictory badges

## Appendix A: Current SQLite Implementation

This appendix documents the current persistence layer ([ADR-0009](ADR-0009-sqlite-state-layer.md)). The DDL below is one storage implementation of `NormalizedState`; the contract is `NormalizedState` itself, not these columns. Future stores (Postgres, distributed) will materialize the same model differently. Treat this appendix as evolving with the data layer.

```sql
-- Add to sessions table
ALTER TABLE sessions ADD COLUMN health       TEXT;
ALTER TABLE sessions ADD COLUMN delivery     TEXT;
ALTER TABLE sessions ADD COLUMN reason_codes JSON DEFAULT '[]';

-- Add to plays table
ALTER TABLE plays ADD COLUMN health       TEXT;
ALTER TABLE plays ADD COLUMN delivery     TEXT;
ALTER TABLE plays ADD COLUMN reason_codes JSON DEFAULT '[]';

-- Composite index for attention queries
CREATE INDEX IF NOT EXISTS idx_sessions_severity
    ON sessions(status, health) WHERE status != 'completed';

CREATE INDEX IF NOT EXISTS idx_plays_severity
    ON plays(status, health) WHERE status NOT IN ('merged', 'skipped');
```

The `NormalizedState` object itself is computed at query time from the
stored fields — not stored as a JSON blob. This keeps the columns
queryable and indexable.
