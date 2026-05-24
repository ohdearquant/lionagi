# ADR-0028: Status Reason Model

**Status**: Proposed
**Date**: 2026-05-23
**Extends**: ADR-0024 (session health), ADR-0025 (session status vocabulary), ADR-0017 (session lifecycle)

## Context

Studio's entity statuses (`running`, `pending`, `failed`, `phantom`,
`blocked`, ...) are accurate but unexplained. Every status answers *what*
state the entity is in; none answer *why*.

### 1. "Pending why?" is a real bug

The plays table contains rows with `status='pending'` and a `depends_on`
JSON array. The frontend renders these as yellow "Pending" pills. An
operator looking at a show with 12 pending plays cannot tell:

- Which plays are blocked by unmet dependencies (waiting normally)?
- Which plays are blocked by *invalid* dependencies (typo in `depends_on`,
  dependency removed from the plan, etc.)?

- Which plays simply have not been launched yet (ready but unstarted)?

`status='pending'` covers all three. The distinction lives nowhere — the
frontend would have to reconstruct it from `plays.depends_on` ∩
`plays.status` per row. That is a join-and-compute every render.

### 2. Phantom session reasons are inline-and-cosmetic

ADR-0024 introduced phantom classification with three reasons:
`process_dead`, `missing_artifacts`, `stale_lock`. They live as enum
strings the admin endpoint returns, not as durable state. Today the UI
displays them as table rows but cannot answer "show me all sessions that
went phantom because the process died in the last 24h" without a custom
endpoint, because the classification is computed on every read.

### 3. Failed runs lose their reason

Per ADR-0025, sessions terminate with `failed`, `timed_out`, `aborted`,
or `cancelled`. The vocabulary is good. But the *cause* of a `failed`
session — exit code, exception class, "missing artifact contract"
(ADR-0029), gate verdict — is scattered across `node_metadata`, branch
errors, or just lost. The runs list shows a red pill; the user has to
open the run, scroll the timeline, and infer.

### 4. The Attention Queue (ADR-0030) cannot exist without this

The Attention Queue must answer "why is this in attention" for every
row. If the queue invents reasons inline (`"Stuck >60m"`, `"Missing
artifact"`, `"Phantom: process dead"`), it duplicates the classification
logic that ADR-0024 and ADR-0029 already encode and creates a third
parallel namespace. Without a canonical reason model, the queue becomes
a pile of frontend heuristics.

### 5. Failed-by-cause grouping cannot exist without this

The dashboard currently shows "6 reviewer failures in 24h" as a count.
The natural follow-up — "are they failing for the same reason?" —
requires a structured, queryable reason. Free-text status notes give
display value but no grouping primitive.

The triggering observation: every UI fix we discussed (Pending-why,
phantom diagnostics, failure clustering, Attention Queue, decision logs)
is a different rendering of the same missing primitive. Build the
primitive once at the data layer; every UI feature becomes a query.

## Decision

Introduce a two-layer status reason model:

1. **Hot path (denormalized current reason)** — three new columns
   (`status_reason_code`, `status_reason_summary`, `status_evidence_refs`)
   on every entity table that already has a `status` column. Read at the
   speed of the existing status query; no JOIN.

2. **Cold path (transition history)** — a new `status_transitions` table
   appending one row per status change with full reason payload, source,
   actor, and previous status. Read only when audit history is requested.

The executor canonicalizes reasons. Agents may emit hints
(`hint_reason_code`, `hint_summary`); the executor decides what to
persist. Both writes happen in a single SQLite transaction.

Reason codes are a controlled Python vocabulary in
`lionagi/state/reasons.py`, following the same pattern as
`VALID_SESSION_STATUSES` from ADR-0025: Python is the source of truth,
SQLite has no CHECK constraint on `reason_code`.

### 1. Schema additions

Seven entity tables gain three columns each:

```sql
-- sessions, shows, plays, invocations, schedule_runs, teams, chain_runs
ALTER TABLE <entity> ADD COLUMN status_reason_code TEXT;
ALTER TABLE <entity> ADD COLUMN status_reason_summary TEXT;
ALTER TABLE <entity> ADD COLUMN status_evidence_refs JSON;
```

One new table for transition history:

```sql
CREATE TABLE IF NOT EXISTS status_transitions (
  id              TEXT    PRIMARY KEY,
  entity_type     TEXT    NOT NULL,     -- 'session' | 'show' | 'play' | ...
  entity_id       TEXT    NOT NULL,
  previous_status TEXT,                 -- NULL for the first transition
  status          TEXT    NOT NULL,
  reason_code     TEXT    NOT NULL,
  reason_summary  TEXT,
  evidence_refs   JSON,                 -- list[{kind, id|path|ref, label?}]
  source          TEXT    NOT NULL,     -- 'executor' | 'agent' | 'admin' | 'system'
  actor           TEXT,                 -- session_id, user, 'doctor_auto', ...
  created_at      REAL    NOT NULL,
  metadata        JSON                  -- optional: timing, exit code, exc class
);

CREATE INDEX IF NOT EXISTS idx_status_transitions_entity
  ON status_transitions(entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_status_transitions_reason
  ON status_transitions(reason_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_status_transitions_created
  ON status_transitions(created_at DESC);
```

`entity_type` is validated in Python, not SQL — matches ADR-0025's
"Python is the source of truth" pattern. New entity types can be added
without schema migration.

### 2. Reason code namespace

```python
# lionagi/state/reasons.py
from __future__ import annotations
from typing import Final

# Sentinel for rows that pre-date this ADR. Frontend renders as
# "Reason tracking not yet enabled" with a muted treatment.
LEGACY_IMPORTED: Final = "legacy.imported"

# Format: <domain>.<status_or_outcome>.<cause>
# Three segments. Lowercase. snake_case for multi-word causes.
# Compound conditions go in reason_summary, not in the code.

class RunReasons:
    COMPLETED_OK              = "run.completed.ok"
    FAILED_EXIT_NONZERO       = "run.failed.exit_nonzero"
    FAILED_EXCEPTION          = "run.failed.exception"
    FAILED_MISSING_ARTIFACT   = "run.failed.missing_artifact"   # ADR-0029
    TIMED_OUT_DEADLINE        = "run.timed_out.deadline"
    ABORTED_USER              = "run.aborted.user"
    CANCELLED_SYSTEM          = "run.cancelled.system"
    CANCELLED_ORCHESTRATOR    = "run.cancelled.orchestrator"

class SessionReasons:
    # Health-derived (ADR-0024 SessionHealth states get a reason code each)
    HEALTH_STALE_NO_HEARTBEAT     = "session.stale.no_heartbeat"
    HEALTH_ORPHANED_NO_PROCESS    = "session.orphaned.no_process"
    HEALTH_ZOMBIE_STALE_LOCKS     = "session.zombie.stale_locks"
    HEALTH_PHANTOM_PROCESS_DEAD   = "session.phantom.process_dead"
    HEALTH_PHANTOM_MISSING_ARTIFACTS = "session.phantom.missing_artifacts"

class PlayReasons:
    PENDING_WAITING_DEPS      = "play.pending.waiting_on_deps"
    PENDING_READY             = "play.pending.ready"
    BLOCKED_INVALID_DEPS      = "play.blocked.invalid_deps"
    BLOCKED_DEP_FAILED        = "play.blocked.dep_failed"
    GATE_FAILED_VERDICT       = "play.gate_failed.verdict"
    ESCALATED_GATE_TWICE      = "play.escalated.gate_twice"
    MERGED_OK                 = "play.merged.ok"

class ShowReasons:
    BLOCKED_NO_READY_PLAYS    = "show.blocked.no_ready_plays"
    COMPLETED_FINAL_GATE      = "show.completed.final_gate"
    ABORTED_OPERATOR          = "show.aborted.operator"

class ScheduleReasons:
    FIRED_DUE                 = "schedule.fired.due"
    SKIPPED_OVERLAP           = "schedule.skipped.overlap"
    SKIPPED_MISSED_FIRE       = "schedule.skipped.missed_fire"

VALID_REASON_CODES: Final = frozenset({
    LEGACY_IMPORTED,
    *vars(RunReasons).values(),
    *vars(SessionReasons).values(),
    *vars(PlayReasons).values(),
    *vars(ShowReasons).values(),
    *vars(ScheduleReasons).values(),
}) - {None}

# Codes are filtered to strings (vars() includes dunder attrs)
```

The vocabulary is intentionally seeded small. Add codes when a real
status transition needs one, not preemptively.

### 3. Evidence reference shape

`evidence_refs` is a list of typed references. The renderer dispatches on
`kind`:

```json
[
  {"kind": "run",      "id": "e288a6e2493f"},
  {"kind": "session",  "id": "0a1b2c3d", "label": "reviewer (round 2)"},
  {"kind": "artifact", "id": "...", "label": "review.md"},
  {"kind": "file",     "path": "artifacts/review.md"},
  {"kind": "branch",   "id": "eebf8f19"},
  {"kind": "play",     "id": "...", "label": "rust-cleanup"},
  {"kind": "log",      "ref": "branch:eebf8f19:stderr"},
  {"kind": "url",      "url": "https://github.com/.../pull/1070"}
]
```

Renderer rules:

- `kind: run|session|play|show|invocation|branch` — link to detail page
- `kind: artifact` — link to artifact view (per ADR-0021)
- `kind: file` — copy-to-clipboard, optionally open in editor
- `kind: log` — open log tab on the relevant entity
- `kind: url` — external link
- Unknown `kind` — render as a labeled string, no link

### 4. Write path — atomic update

A single `update_status()` method on `StateDB` is the only sanctioned
mutation point. Direct UPDATE of `status` (without writing a reason) is
considered a bug:

```python
# lionagi/state/db.py
async def update_status(
    self,
    entity_type: str,
    entity_id: str,
    *,
    new_status: str,
    reason_code: str,
    reason_summary: str = "",
    evidence_refs: list[dict] | None = None,
    source: str = "executor",
    actor: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Atomically transition an entity's status and record the reason.

    Raises:
        ValueError: reason_code not in VALID_REASON_CODES.
        ValueError: entity_type not in VALID_ENTITY_TYPES.
        StateError: entity not found.
    """
    _validate_reason_code(reason_code)
    _validate_entity_type(entity_type)
    table = _entity_table(entity_type)
    evidence_json = json.dumps(evidence_refs or [])

    async with self.transaction():
        prev = await self.fetchone(
            f"SELECT status FROM {table} WHERE id = ?", (entity_id,)
        )
        if prev is None:
            raise StateError(f"{entity_type} {entity_id!r} not found")
        previous_status = prev["status"]
        now = time.time()

        await self.execute(
            f"UPDATE {table} SET "
            f"  status = ?, "
            f"  status_reason_code = ?, "
            f"  status_reason_summary = ?, "
            f"  status_evidence_refs = ?, "
            f"  updated_at = ? "
            f"WHERE id = ?",
            (new_status, reason_code, reason_summary,
             evidence_json, now, entity_id),
        )

        await self.execute(
            "INSERT INTO status_transitions "
            "(id, entity_type, entity_id, previous_status, status, "
            " reason_code, reason_summary, evidence_refs, "
            " source, actor, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid4().hex, entity_type, entity_id, previous_status, new_status,
             reason_code, reason_summary, evidence_json,
             source, actor, now,
             json.dumps(metadata) if metadata else None),
        )
```

Both writes are in the same SQLite transaction. Either both commit or
neither does. The denormalized columns and the history row never drift.

### 5. CLI write points (refactored from ADR-0025)

```python
# lionagi/cli/agent.py teardown — replace bare status update
async def _teardown_live_persist(live, status, *, exception=None, exit_code=None):
    reason_code, reason_summary, evidence = _resolve_reason(
        status=status, exception=exception, exit_code=exit_code,
    )
    async with StateDB.open() as db:
        await db.update_status(
            entity_type="session",
            entity_id=live.session_id,
            new_status=status,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=evidence,
            source="executor",
            metadata={"exit_code": exit_code} if exit_code is not None else None,
        )

def _resolve_reason(*, status, exception, exit_code):
    if status == "completed":
        return RunReasons.COMPLETED_OK, "Run completed successfully.", []
    if status == "timed_out":
        return (RunReasons.TIMED_OUT_DEADLINE,
                "Run exceeded the configured timeout.",
                [])
    if status == "aborted":
        return RunReasons.ABORTED_USER, "User pressed Ctrl-C.", []
    if status == "cancelled":
        return (RunReasons.CANCELLED_SYSTEM,
                "Task cancelled by the runtime (anyio CancelledError).",
                [])
    # status == "failed"
    if exit_code not in (None, 0):
        return (RunReasons.FAILED_EXIT_NONZERO,
                f"Process exited with code {exit_code}.",
                [])
    if exception is not None:
        return (RunReasons.FAILED_EXCEPTION,
                f"{type(exception).__name__}: {exception}",
                [])
    return RunReasons.FAILED_EXCEPTION, "Run failed.", []
```

ADR-0029's contract verifier writes `run.failed.missing_artifact` with
the unmet artifact IDs in `evidence_refs`. ADR-0024's doctor classifier
writes `session.phantom.*` codes when it transitions a session. The
admin transition API (per ADR-0025) requires a `reason_code` in the
request body — no more bare status updates from the UI.

### 6. Read path — entity API responses

Every detail endpoint that returns a status returns the reason inline:

```json
{
  "id": "play_a1b2c3d4",
  "name": "test-coverage",
  "status": "pending",
  "status_reason": {
    "code": "play.pending.waiting_on_deps",
    "summary": "Waiting on rust-cleanup, runtime-usability, graph-fixes.",
    "evidence_refs": [
      {"kind": "play", "id": "...", "label": "rust-cleanup"},
      {"kind": "play", "id": "...", "label": "runtime-usability"},
      {"kind": "play", "id": "...", "label": "graph-fixes"}
    ]
  },
  ...
}
```

If the columns are NULL (legacy row), the response is:

```json
"status_reason": {
  "code": "legacy.imported",
  "summary": "Reason tracking not yet enabled for this row.",
  "evidence_refs": []
}
```

The transition history endpoint pages the cold path:

```text
GET /api/{entity_type}/{id}/status-history?limit=20&before=<created_at>
```

### 7. Frontend rendering

The `StatusPill` component (ADR-0025) gains a paired `StatusReason`
treatment for tooltip and popover:

```tsx
<StatusPill
  taxonomy="play"
  value="pending"
  reason={{
    code: "play.pending.waiting_on_deps",
    summary: "Waiting on rust-cleanup, runtime-usability, graph-fixes.",
    evidence_refs: [...],
  }}
/>
```

- **Hover** (>500ms): tooltip shows `summary`.
- **Click**: popover shows `summary` + evidence_refs as clickable chips.
- **No reason** (legacy.imported or null): pill renders unchanged; no
  tooltip; no popover affordance. Consistent with how today's pills look.

The `code` is a stable machine identifier — it never appears in the UI
text. The UI shows `summary` (human-readable) and evidence chips. This
means changing the *wording* of a reason summary in
`SessionReasons.HEALTH_STALE_NO_HEARTBEAT` does not require a code
rename — the code is the contract, the summary is the message.

### 8. Migration

SQLite supports `ALTER TABLE ... ADD COLUMN`. Migration via
`StateDB._reconcile_columns()` (existing pattern from ADR-0025/0026):
add three columns to each of the seven entity tables on next
`StateDB.open()`. New `status_transitions` table created via `IF NOT
EXISTS`.

Existing rows have NULL reason columns. They render as
`legacy.imported` per the read-path contract above. No backfill — the
audit history for pre-ADR rows is genuinely lost; manufacturing reasons
would be worse than honest silence.

### 9. Relationship to existing ADRs

| ADR | Relationship |
|---|---|
| ADR-0017 | Original session lifecycle; this ADR adds the reason layer. |
| ADR-0024 | Phantom session classifier writes reason codes when transitioning a session via admin. The `SessionHealth` enum (computed) and `SessionReasons.HEALTH_*` (persisted on transition) are intentionally parallel: health is a read-time view, reasons are write-time records. |
| ADR-0025 | Session status vocabulary stays as-is. `update_status()` enforces both the status validator and the reason validator. |
| ADR-0029 | Artifact contract violations write `run.failed.missing_artifact` with evidence refs pointing to the unmet artifact IDs. |
| ADR-0030 | Attention Queue groups by `reason_code` for clustering; pulls `summary` and `evidence_refs` directly from entity responses. |
| ADR-0024 `admin_events` | Parallel append-only log. `admin_events` records the *action* (admin pruned X, admin classified Y); `status_transitions` records the *consequence* (X transitioned from running to phantom). Both rows get written when an admin action causes a transition. |

### 10. File map

New files:

```text
lionagi/state/reasons.py                  # Reason code namespace + validators
```

Modified files:

```text
lionagi/state/schema.sql                  # ALTER for 7 tables + status_transitions table
lionagi/state/db.py                       # update_status() + reconcile_columns
lionagi/cli/agent.py                      # teardown writes reason
lionagi/cli/orchestrate/flow.py           # flow termination writes reason
apps/studio/server/services/admin.py      # health classifier writes reason on transition
apps/studio/server/services/shows.py      # play/show transitions write reason
apps/studio/server/services/schedules.py  # schedule fire/skip writes reason
apps/studio/server/routers/admin.py       # admin transition API requires reason_code
apps/studio/frontend/components/status/StatusPill.tsx  # reason tooltip/popover
apps/studio/frontend/lib/api.ts           # status_reason type + fetch
```

## Consequences

**Positive**

- One canonical primitive ("why is this state true") replaces five
  scattered explanations (phantom enums, branch errors, node_metadata
  exit codes, frontend heuristics, inline log scraping).

- Attention Queue (ADR-0030), Pending-why, phantom diagnostics, failure
  clustering, and decision-log surfacing all become queries over a
  single shape instead of bespoke endpoints.

- Reason codes are queryable — "show me all failures with
  `run.failed.missing_artifact` in 24h" is one SQL query.

- Transactional consistency guarantees the denormalized "current
  reason" and the transition history never drift.

- Evidence references give the UI structured navigation: the reason
  always knows what to link to.

- Python-validated namespace means adding a reason code is a code
  change, not a schema migration.

- Backfill is deliberately not attempted — historical silence is more
  honest than fabricated reasons.

**Negative**

- Seven `ALTER TABLE` statements. Pre-release, so acceptable; still
  three more columns × seven tables of schema width.

- Every status mutation site in the codebase has to be updated to call
  `update_status()` with a reason. The current pattern is to write
  `status` directly. The transition is mechanical but touches many
  files (see File Map).

- Agents cannot write authoritative reasons. They can emit hints
  (`hint_reason_code`), but the executor canonicalizes. This is
  intentional but reduces agent autonomy by a degree.

- `status_transitions` accumulates rows forever. At ~10 transitions per
  session and ~1000 sessions/month, that is ~120k rows/year. Add a
  retention policy in a follow-up ADR (or trim older than N days
  during `vacuum`).

- The reason code vocabulary is small at first. Codes that should
  exist but don't will be filed as `run.failed.exception` with a free-
  text summary; the curation gap shows up over time.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| `status_note TEXT` column only (denormalized, no history, no code) | Gives the tooltip but loses grouping, filtering, dismissal stability, evidence linking, and audit history. ChatGPT proposed this as the "80% solution"; it is actually the 20% solution. The reason *code* is the load-bearing primitive. |
| Single shared `status_reason_json` JSON column per entity table | Same schema sprawl (one column × seven tables) without indexable code. Filtering by reason becomes a JSON extract on every query. |
| Status reasons only in a separate `status_transitions` table (no denormalization) | Every status pill render becomes a JOIN. Dashboard would N+1 across hundreds of entities per page. The 3-column denormalization on entities is the read-path optimization. |
| Reuse `admin_events` for status transitions | `admin_events` is for admin actions (transition, prune, checkpoint, vacuum, classify). Most status transitions are runtime, not admin. Conflating them would mean every session completion writes to a table named "admin_events", which is semantically wrong. Both tables coexist. |
| Agents write reasons directly | Agents can hallucinate reasons. The executor has authoritative process/exit-code/contract state. Agents emit hints; the executor canonicalizes. |
| Full event stream (CloudEvents / event sourcing) | Premature. Studio needs a transition log, not a generalized event bus. Re-evaluate when chains (ADR-0021) and traces become first-class. |
| Free-form `reason_code` string with no validator | Typos compound. Within six months the namespace becomes `run_failed`, `run.failed`, `run-failed`, `RunFailed` all in production. The Python validator costs ~2 lines and prevents this. |
| Backfill historical rows with inferred reasons | Inferring reasons from `updated_at`, `node_metadata`, and branch errors produces guesses that look authoritative. `legacy.imported` is more honest. |

## References

- [ADR-0017](ADR-0017-session-lifecycle-status.md) — Session lifecycle status (foundation)
- [ADR-0024](ADR-0024-session-health-and-admin-surface.md) — Session health classification (parallel: health is read-time, reasons are write-time)
- [ADR-0025](ADR-0025-session-status-vocabulary.md) — Session status vocabulary (Python validation pattern)
- [ADR-0029](ADR-0029-artifact-contract.md) — Artifact contract (writes `run.failed.missing_artifact`)
- [ADR-0030](ADR-0030-attention-queue.md) — Attention Queue (consumer)
- `lionagi/state/schema.sql` — `admin_events` table (parallel append-only log)
- `lionagi/state/db.py` — `_reconcile_columns()` migration pattern
- ChatGPT frontend design review (external) — proposed `status_history` table as part of ADR-A; this ADR adopts the table but adds the denormalized hot-path columns and Python-validated reason code namespace that the proposal missed.

### Prior art

- **PostgreSQL `pg_stat_*` views** — denormalized current state + cumulative counters, no JOIN required on read path. Same pattern.
- **Kubernetes object conditions** — every K8s object has `.status.conditions[]` with `type`, `status`, `reason`, `message`, `lastTransitionTime`. The reason field is a stable machine-readable code; the message is human-readable. Direct inspiration.
- **AWS CloudWatch alarms** — `StateReason` (free text) and `StateReasonData` (structured JSON). Their bifurcation into prose and structure mirrors our `reason_summary` and `evidence_refs`.
