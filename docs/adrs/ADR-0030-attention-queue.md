# ADR-0030: Attention Queue

**Status**: Proposed — depends on [ADR-0033](ADR-0033-unified-entity-state-model.md), extended to include knowledge events per [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md)
<!-- No Attention Queue implementation on main as of 2026-06-03: only shared reason-code
     scaffolding (ADR-0028) and references exist; the queue itself is unbuilt. -->
**Date**: 2026-05-23
**Depends on**: ADR-0028 (status reason model) — without persisted reason codes, the queue's grouping and dismissal logic become frontend heuristics
**Related**: ADR-0029 (artifact contract), ADR-0024 (session health), ADR-0031 (entity actions reuse)

---

> **Extension notice**: [ADR-0033](ADR-0033-unified-entity-state-model.md) provides the canonical severity computation (`derive_severity()`) and reason-code chain that the Attention Queue consumes. The Queue's severity values (`critical, warning, info, neutral`) now have uniform semantics across entity types because they all derive from NormalizedState. [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) adds knowledge-related items to the Queue (disputed claims, low-confidence aged hypotheses, supersession anomalies) via a parallel `derive_claim_severity()` function emitting into the same Queue. The Queue's mechanics defined here are preserved; the sources of severity are now plural and unified.

---

## Context

Studio's dashboard surfaces counts: "Running: 6 / Failed: 6 / Slow: 3 /
Needs review: 0". This is accurate, but it is a *table of contents*,
not a *work queue*. To act on any of those numbers, the operator opens
a different page, scans rows, infers severity from context, and decides
which item to address first. The dashboard does not say which of the
six failures matters more, or whether any of them are duplicates of
each other.

### 1. The state across entity types is fragmented

Items that need attention live in:

- `sessions` (failed, stuck, timed_out, plus operator action needed on phantom classifications)
- `shows` (active with no ready plays)
- `plays` (blocked, escalated, gate_failed)
- `schedule_runs` (skipped, failed)
- (deferred — see Non-Goals) `chain_runs` waiting_approval, system-level DB/WAL conditions

There is no single page that aggregates these. The closest surface is
the dashboard's failure cards, but they only show sessions and only
the last 24h. A failed schedule run from this morning, a play blocked
by a missing dependency since yesterday, and a phantom session from
last week sit on three different pages.

### 2. Failure clustering doesn't exist

When `/codex-pr-review` failed six times last week, the dashboard
showed "6 failures" as six separate red rows. They were all the same
failure (missing artifact contract — see ADR-0029). Grouping them by
cause turns six rows of work into one.

### 3. The operator needs a place to dismiss

Some attention items are real but not urgent. Phantom sessions older
than 30 days can be pruned later. A blocked play with a known
workaround can be deferred. Without a snooze/dismiss mechanism, the
queue grows until the operator ignores it entirely — banner blindness.

### 4. ADR-0028 makes this finally cheap to build

With status reason codes persisted on each entity, the queue endpoint
becomes a union over entity tables filtered by status × reason_code,
sorted by a severity heuristic. Without ADR-0028, this endpoint would
have to recompute "why is this in attention" inline for every row —
exactly the duplication that ADR-0028 exists to prevent.

## Decision

Add a single backend endpoint, `GET /api/attention`, that returns a
flat list of attention items aggregated across entity types. Severity
is server-computed from a heuristic table (Section 4). Fingerprint-
based dismissals live in a new `attention_dismissals` table. Refresh
uses polling (5-15s) in v1; SSE deferred until Studio has real event
semantics.

### 1. Endpoint shape

```text
GET /api/attention?severity=critical,warning&limit=50&include_dismissed=false
```

Response:

```json
{
  "generated_at": 1716517632.4,
  "total": 14,
  "by_severity": {"critical": 4, "warning": 7, "info": 3},
  "items": [
    {
      "id": "attn_<uuid>",
      "fingerprint": "session:e288a6e2493f:run.failed.missing_artifact",
      "severity": "critical",
      "entity": {
        "type": "session",
        "id": "e288a6e2493f",
        "label": "reviewer run for /codex-pr-review"
      },
      "status": "failed",
      "reason": {
        "code": "run.failed.missing_artifact",
        "summary": "Required artifact review.md was not produced.",
        "evidence_refs": [
          {"kind": "expected_artifact", "id": "review", "label": "review.md"}
        ]
      },
      "impact": "Blocks /codex-pr-review pipeline for PR #1064.",
      "cluster_id": "cluster_run.failed.missing_artifact",
      "cluster_size": 6,
      "first_seen_at": 1716517000.0,
      "last_updated_at": 1716517630.0,
      "actions": [
        {"id": "inspect", "label": "Open", "kind": "primary", "href": "/runs/e288a6e2493f"},
        {"id": "retry",   "label": "Retry", "kind": "secondary", "endpoint": "/api/runs/e288a6e2493f/retry", "method": "POST"},
        {"id": "snooze",  "label": "Snooze 1d", "kind": "secondary", "endpoint": "/api/attention/snooze", "method": "POST", "requires_confirm": false}
      ]
    }
  ]
}
```

Notes on fields:

- `fingerprint` is the stable identity used for snooze/dismiss. Format:
  `<entity_type>:<entity_id>:<reason_code>`. When the reason code
  changes, the fingerprint changes, so a dismissal does not silently
  hide a new condition.

- `cluster_id` groups items with the same `reason_code` (cross-entity).
  `cluster_size` is the number of items in that cluster server-side
  (not just on this page).

- `actions` reuses the shape defined in ADR-0031 (`EntityAction`).
  Same descriptor type, same renderer.

- `entity.label` is server-rendered. For sessions, it is
  `playbook_name` + agent role; for plays, `<show.topic>/<play.name>`;
  for shows, `topic`. The frontend never has to reconstruct labels.

### 2. Schema additions

```sql
CREATE TABLE IF NOT EXISTS attention_dismissals (
  id                     TEXT    PRIMARY KEY,
  fingerprint            TEXT    NOT NULL,
  entity_type            TEXT    NOT NULL,
  entity_id              TEXT    NOT NULL,
  reason_code            TEXT    NOT NULL,      -- the reason at dismissal time
  status_at_dismissal    TEXT    NOT NULL,      -- the entity status at dismissal time
                                                -- used by clear_on_status_change cleanup
  snoozed_until          REAL,                  -- NULL = permanent dismiss (until status change)
  dismissed_at           REAL    NOT NULL,
  dismissed_by           TEXT    DEFAULT 'operator',
  clear_on_status_change INTEGER NOT NULL DEFAULT 1
                         CHECK(clear_on_status_change IN (0, 1)),
  created_at             REAL    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_attention_dismissals_fp
  ON attention_dismissals(fingerprint);
CREATE INDEX IF NOT EXISTS idx_attention_dismissals_snoozed
  ON attention_dismissals(snoozed_until) WHERE snoozed_until IS NOT NULL;

-- Compensating index for queries that JOIN by (entity_type, entity_id)
-- when computing the live fingerprint client-side.
CREATE INDEX IF NOT EXISTS idx_attention_dismissals_entity
  ON attention_dismissals(entity_type, entity_id);
```

`fingerprint` is the dismissal's identity (`<entity_type>:<entity_id>:<reason_code>`).
Snooze is an upsert keyed by fingerprint — re-snoozing the same condition
extends the deadline; a *different* reason on the same entity gets a new
fingerprint and surfaces fresh.

### 3. Item generation

The endpoint runs SQL queries per entity type and merges the results.
Each query selects rows whose `status` × `status_reason_code` matches
an attention-worthy condition (see severity table). Sample query for
sessions:

```sql
-- failed sessions in last 24h, not dismissed for THIS reason code,
-- AND status hasn't changed since the dismissal was recorded
SELECT
  'session' AS entity_type,
  s.id AS entity_id,
  s.status, s.status_reason_code, s.status_reason_summary, s.status_evidence_refs,
  s.updated_at,
  COALESCE(s.playbook_name, s.agent_name, 'agent') AS label_root
FROM sessions s
WHERE s.status = 'failed'
  AND s.updated_at >= ?  -- now - 86400
  AND NOT EXISTS (
    SELECT 1 FROM attention_dismissals d
    WHERE d.fingerprint = (
        'session:' || s.id || ':' || COALESCE(s.status_reason_code, '')
      )
      AND (d.snoozed_until IS NULL OR d.snoozed_until > ?)
      -- clear_on_status_change=1 dismissals stop hiding the item as
      -- soon as the entity's status changes from what it was at
      -- dismissal time. clear_on_status_change=0 dismissals ignore
      -- status drift entirely.
      AND (d.clear_on_status_change = 0 OR d.status_at_dismissal = s.status)
  );
```

Filtering matches the *fingerprint*, not just `entity_id` — so a
dismissal of `session:X:session.phantom.process_dead` does not hide
a later `session:X:session.phantom.missing_artifacts` item. The
`status_at_dismissal` comparison makes `clear_on_status_change=1`
behavior immediate at read time, not deferred until vacuum.

The Python aggregator runs these queries (one per entity type), applies
severity scoring, computes cluster ids, and returns the merged list.

For the v1 scale (single-user Studio with hundreds of sessions),
running ~6 queries on every poll is well within budget. The
existing `plays(status)` index (`lionagi/state/schema.sql:271`)
covers the play queries; the existing
`idx_sessions_status_last_msg` (`lionagi/state/schema.sql:159`) is
a partial index for `status='running'` only and does **not** cover
`failed`/`timed_out` lookups. This ADR therefore adds:

```sql
CREATE INDEX IF NOT EXISTS idx_sessions_status_updated
  ON sessions(status, updated_at DESC);
```

so the failed/timed_out queries (severity table rows 1-2) avoid
a full scan.

### 4. Severity heuristic

| Condition | Detection | Severity |
|---|---|---|
| Run failed (missing required artifact) | `status_reason_code='run.failed.missing_artifact'` | `critical` |
| Session `failed` (any other cause, last 24h) | `status='failed'` AND `updated_at >= now - 24h` AND `status_reason_code != 'run.failed.missing_artifact'` | `critical` |
| Session `timed_out` (last 24h) | `status='timed_out'` AND `updated_at >= now - 24h` | `warning` |
| Session stuck (`running` >60m, no heartbeat) | `status='running'` AND `now - last_message_at > 3600` | `critical` |
| Session slow (`running`, alive, >60m) | `status='running'` AND `60m < age < 3h` AND has recent heartbeat | `info` |
| Session went phantom (process_dead, operator hasn't transitioned) | doctor classification (see "Live doctor-derived items" below) | `warning` |
| Session went phantom (missing_artifacts, operator hasn't transitioned) | doctor classification `missing_artifacts` — catches sessions that pre-date ADR-0029's contract or had no contract declared | `warning` |
| Session went phantom (stale_lock, operator hasn't transitioned) | doctor classification `stale_lock` | `warning` |
| Play `blocked` (invalid deps) | `status_reason_code='play.blocked.invalid_deps'` | `warning` |
| Play `blocked` (dep failed) | `status_reason_code='play.blocked.dep_failed'` | `warning` |
| Play `escalated` | `status='escalated'` | `critical` |
| Play `gate_failed` attempt 2 | `status='gate_failed'` AND `attempt=2` | `critical` |
| Show `active` with 0 ready plays | `status_reason_code='show.blocked.no_ready_plays'` | `warning` |
| Schedule run `failed` (last 24h) | `status='failed'` AND `fired_at >= now - 24h` | `warning` |

Sessions with `status_reason_code = 'legacy.imported'` are excluded —
they are pre-ADR-0028 and have no useful reason to surface.

#### Live doctor-derived items

The three "Session went phantom" rows above are *not* generated from
the entity-table status × status_reason_code query path. Phantom
classifications are derived at read time by the doctor service per
ADR-0024 (`apps/studio/server/services/admin.py:_classify_phantom`),
and the operator may not yet have transitioned the session. The
attention aggregator therefore queries the doctor service alongside
the entity tables:

```python
# apps/studio/server/services/attention.py — sketch
# Consumes the existing public admin API
# (apps/studio/server/services/admin.py:104) which returns a list of
# dicts shaped { "session_id", "reason", "name", "playbook_name",
# "started_at", "updated_at", "artifacts_path", "status", ... }.
# `reason` is a PhantomReason literal defined at admin.py:16.
async def _phantom_items(stale_hours: float = 1.0) -> list[AttentionItem]:
    phantoms = await admin_service.list_phantom_sessions(stale_hours=stale_hours)
    out: list[AttentionItem] = []
    for entry in phantoms:
        session_id = entry["session_id"]   # admin.py:125 yields session_id
        reason_code = _PHANTOM_TO_REASON_CODE[entry["reason"]]
        # entry["reason"] ∈ {"process_dead", "missing_artifacts", "stale_lock"}
        out.append(AttentionItem(
            entity_type="session",
            entity_id=session_id,
            status="running",  # phantom items are still pre-transition
            reason=StatusReason(
                code=reason_code,
                summary=_PHANTOM_TO_SUMMARY[entry["reason"]],
                evidence_refs=[
                    {"kind": "session", "id": session_id},
                ],
            ),
            fingerprint=f"session:{session_id}:{reason_code}",
            severity="warning",
        ))
    return out

_PHANTOM_TO_REASON_CODE = {
    "process_dead":      SessionReasons.HEALTH_PHANTOM_PROCESS_DEAD,
    "missing_artifacts": SessionReasons.HEALTH_PHANTOM_MISSING_ARTIFACTS,
    "stale_lock":        SessionReasons.HEALTH_ZOMBIE_STALE_LOCKS,
}
```

Once the operator transitions the session (per ADR-0028 Section 5,
the admin transition API), the row's persisted `status_reason_code`
matches the same code; the entity-table query path picks it up; the
doctor-derived path stops returning the same session because it is
no longer classified as phantom.

**Deferred to v1.1** (see Non-Goals): chain-run `waiting_approval`
attention items wait for the `chain_runs` table (ADR-0021 proposed,
not landed). System-level DB/WAL attention items require a `system`
entity_type with synthetic `entity_id` (e.g. `system:db`) and matching
reason codes (`system.db.wal_degraded`) — separate ADR.

### 5. Clustering

Items with the same `reason_code` (across entity types) cluster:

```python
def cluster_items(items: list[dict]) -> list[dict]:
    by_code = defaultdict(list)
    for item in items:
        code = item["reason"]["code"]
        by_code[code].append(item)
    for items_in_cluster in by_code.values():
        cluster_id = f"cluster_{items_in_cluster[0]['reason']['code']}"
        for item in items_in_cluster:
            item["cluster_id"] = cluster_id
            item["cluster_size"] = len(items_in_cluster)
    return items
```

The frontend can render the queue grouped by cluster (`{ cluster_id ->
items[] }`) or flat. Default in v1: grouped by cluster when
`cluster_size > 1`.

This is the cheapest possible clustering. More sophisticated taxonomy
(e.g., grouping `run.failed.exception:TimeoutError` with
`run.failed.exception:CancelledError`) is deferred — once we see real
data, we can promote shared substrings to first-class reason codes.

### 6. Dismissal semantics

- `POST /api/attention/snooze` with `{fingerprint, until}` upserts a
  row keyed on `fingerprint`. The endpoint stores the entity's
  current status as `status_at_dismissal` so the cleanup query has
  something to compare against. The next attention poll filters
  snoozed items.

- `POST /api/attention/dismiss` with `{fingerprint}` upserts with
  `snoozed_until=NULL` and records `status_at_dismissal` (permanent
  until status change).

- `clear_on_status_change=1` (default): when the entity's *status*
  enum changes (`running -> failed`), the dismissal is invalidated.
  When only the *reason code* changes (`session.phantom.process_dead
  -> session.phantom.missing_artifacts`), the fingerprint changes (it
  encodes the reason code), so a new attention item appears and the
  old dismissal stays — correct, because the underlying signal
  changed.

- `clear_on_status_change=0`: the dismissal persists until the entity
  is deleted. For "I never want to see this again" cases. The query
  filter still applies, but cleanup never runs on these rows.

The cleanup query runs as a step in `li state vacuum` (per ADR-0024's
maintenance flow):

```sql
DELETE FROM attention_dismissals
WHERE clear_on_status_change = 1
  AND EXISTS (
    SELECT 1 FROM <entity_table>
    WHERE id = attention_dismissals.entity_id
      AND status != attention_dismissals.status_at_dismissal
  );
```

In v1, this runs on `li state vacuum` (existing maintenance entrypoint).

### 7. Refresh model

Polling. The dashboard re-fetches `/api/attention` every 10 seconds
when the tab is active. When the tab is hidden, polling pauses (uses
the existing `Page Visibility API` pattern in the frontend).

SSE is deferred. Real-time push only matters when items appear
faster than humans can read them. At Studio's scale (single user,
~tens of items/hour), polling is sufficient.

### 8. Frontend rendering

The Attention Queue lives at the top of the Dashboard (ADR-0032
groups it under `Dashboard`). Layout:

```text
ATTENTION QUEUE                                    14 items
  
  CRITICAL · 4 items
    [▼ run.failed.missing_artifact · 6 items]
       reviewer e288a... · 2m ago · review.md not produced
         [Open] [Retry] [Snooze 1d]
       reviewer 3def... · 14m ago · review.md not produced
         [Open] [Retry] [Snooze 1d]
       ...
    [▼ play.escalated · 1 item]
       sweep/test-coverage · 1h ago · gate failed twice
         [Open] [Reassign] [Snooze 1d]

  WARNING · 7 items
    ...

  INFO · 3 items
    ...
```

Clusters with `cluster_size > 1` collapse by default. Each row gets
its `actions[]` rendered as buttons via the same `EntityAction`
component from ADR-0031.

The "[Snooze 1d]" button is shown by default; "[Dismiss]" hidden in a
menu (heavier consequences). Dismiss popup confirms with the item
summary.

### 9. File map

New files:

```text
apps/studio/server/services/attention.py     # query aggregator + clustering
apps/studio/server/routers/attention.py      # GET endpoint + snooze/dismiss
apps/studio/frontend/components/dashboard/AttentionQueue.tsx
apps/studio/frontend/components/dashboard/AttentionCluster.tsx
apps/studio/frontend/components/dashboard/AttentionItem.tsx
```

Modified files:

```text
lionagi/state/schema.sql                     # attention_dismissals table
lionagi/state/db.py                          # snooze/dismiss CRUD
apps/studio/server/app.py                    # register attention router
apps/studio/frontend/app/page.tsx            # mount AttentionQueue at top of dashboard
apps/studio/frontend/lib/api.ts              # fetchAttention(), snoozeItem()
```

## Consequences

**Positive**

- Operators get a single page that answers "what should I look at first"
  instead of scanning four entity-type pages and inferring priority.

- Clustering by reason code turns six identical failures into one row
  with cluster_size=6, fixing the noise that the dashboard currently
  generates for repeated failures.

- Dismissal is a real primitive, not a frontend localStorage hack.
  Survives reloads, clears correctly on status change, persists across
  sessions.

- Server-side severity scoring means the frontend stays a renderer.
  Adding a new attention condition is a backend change with a single
  responsibility (extend the severity table).

- Reuses ADR-0031's `EntityAction` descriptor — no duplicate action UI
  schema for the queue vs entity detail pages.

- Direct beneficiary of ADR-0028: every item has a stable reason code,
  human summary, and evidence refs without any extra work.

**Negative**

- Polling load. At 10s intervals × 6 entity-type queries × a single
  user, the impact is negligible. If Studio scales to multi-user (it
  is currently single-user only — see ADR-0008), polling will need
  reconsideration.

- The severity table is opinionated. Conditions and thresholds are
  judgment calls; some operators may want different defaults
  ("phantom: process_dead" as critical vs warning). Override hooks are
  deferred — start opinionated, soften based on feedback.

- Clustering is intentionally simple (same reason_code). More
  sophisticated grouping (e.g., shared file path, same agent role)
  would require a real classifier and is out of scope.

- Dismissal can become a debt magnet. An operator who snoozes
  everything ends up with a quiet queue and a broken system. The
  "clear on status change" default mitigates this for the common case.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Aggregate per-entity endpoints client-side | Severity, dedup, snooze, and clustering become inconsistent across pages. Each entity page would re-implement filtering and sorting. One endpoint owning the queue is the cheapest correct path. |
| SSE / WebSocket push from v1 | Polling at 10s is sufficient for the data freshness Studio needs. SSE introduces connection management, reconnect logic, backpressure — not free. Defer until events become first-class. |
| Materialized view / persistent attention items table | Cache invalidation problem on every status change. Re-running ~7 indexed queries per poll is cheaper than maintaining a materialized table whose freshness depends on every entity write touching it. |
| Embed Attention Queue inside each entity page | Operator would have to navigate to find their queue. The point of the queue is to *avoid* navigation — one stop, prioritized list. |
| No dismissal at all | Queue grows until it's banner-blindness. The "I'll deal with this later" affordance is necessary. |
| Severity inferred client-side from status alone | Status doesn't carry enough information (e.g., `failed` covers both "missing artifact" and "exception" — different severities). Need server-side rules with access to reason codes. |
| Cluster by entity type instead of reason code | Reason code clusters surface the actual repeated failure mode. Entity-type clustering would put unrelated session failures together. |

## Non-Goals

- **No pagination beyond `limit`.** The queue should never have more
  than ~100 items at steady state; if it does, the system is broken,
  not the UI. Pagination is deferred.

- **No filtering UI in v1.** Filter by severity is supported via the
  endpoint query string; an in-UI filter dropdown is deferred. The
  default view shows all severities, clustered.

- **No assignment / ownership.** Studio is single-user (ADR-0008).
  Multi-user assignment is deferred to whenever multi-user becomes a
  goal.

- **No notification system.** No emails, Slack pings, or browser
  notifications when new items appear. The queue is pull-only.

- **No automatic remediation.** No "Auto-retry up to 3 times when
  this reason code appears." Retry is operator-initiated, by design.

- **No root-cause analysis.** The cluster just groups by code;
  Studio does not infer or display deeper causality.

- **No external integrations.** Linear / GitHub Issues escalation is
  deferred. The queue lives in Studio.

- **No system-level attention items in v1.** DB/WAL pressure, disk
  exhaustion, and other non-entity conditions would require a
  synthetic `entity_type='system'` with no row in any entity table,
  which the schema (entity_id NOT NULL) does not accommodate cleanly.
  Deferred to a follow-up that introduces `system` reason codes
  (e.g. `system.db.wal_degraded`) and either a `system_entities`
  table or a documented synthetic-id convention.

- **No chain-run attention items in v1.** Chain runs are proposed in
  ADR-0021 but `chain_runs` is not in the current schema. When
  ADR-0021 lands, a follow-up extends the queue with
  `chain_run.waiting_approval`.

## References

- [ADR-0024](ADR-0024-session-health-and-admin-surface.md) — Phantom session classifier feeds attention items.
- [ADR-0028](ADR-0028-status-reason-model.md) — Persisted reason codes are the queue's primary input.
- [ADR-0029](ADR-0029-artifact-contract.md) — Contract failures become `run.failed.missing_artifact` attention items.
- [ADR-0031](ADR-0031-entity-header-pattern.md) — `EntityAction` descriptor shape reused for queue actions.
- [ADR-0032](ADR-0032-navigation-reorganization.md) — Queue lives at the top of Dashboard.
- `apps/studio/server/services/admin.py` — Existing health classifier (already detects phantom; this ADR wires its output into the queue).
- ChatGPT frontend design review (external) — proposed the Attention Queue as Phase 1; this ADR delays it until ADR-0028 lands so the queue can sit on persisted reason codes rather than inline heuristics.

### Prior art

- **PagerDuty / Opsgenie alert queues** — severity-based grouping with snooze and acknowledge. The fingerprint mechanism is borrowed directly.
- **Sentry issue grouping** — clusters errors by stack-trace fingerprint, surfaces "X occurrences" rather than X separate rows. Same shape, different signal.
- **Inbox Zero / GTD attention triage** — the queue is structured to support "process to inbox zero" cadence: open, action, snooze, or dismiss each item.
