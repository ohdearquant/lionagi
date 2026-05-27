# ADR-0034: Frontend Data & State Architecture

**Status**: Proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0033](ADR-0033-unified-entity-state-model.md), current persistence layer (presently [ADR-0009](ADR-0009-sqlite-state-layer.md))
**Related**: [ADR-0006](ADR-0006-sse-live-streaming.md), [ADR-0030](ADR-0030-attention-queue.md), [ADR-0035](ADR-0035-design-system-and-component-library.md), [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md)

## Context

Lion Studio's frontend fetches data with a plain `fetchJson` wrapper —
no caching, no deduplication, no retry, no cache invalidation. Every
page navigation re-fetches from the API. There is no real-time update
mechanism: operators must manually refresh to see state changes.

This creates three concrete problems:

1. **No freshness guarantee.** The dashboard can show "0 failed" while
   a run failed 30 seconds ago. An operator who trusts the dashboard
   misses the failure.

2. **No coordinated cache.** The dashboard fetches runs, shows, and
   system health independently. A run status change doesn't invalidate
   the dashboard summary or the attention queue. State drifts between
   components.

3. **No URL-addressable views.** Table filters, sort order, selected
   entity, and inspector state are component-local. An operator cannot
   share a link to "failed runs in project X, sorted by severity" with
   a teammate.

ADR-0033 defines backend-owned `NormalizedState` for every entity.
This ADR specifies how that state flows to the frontend, stays fresh,
and becomes URL-addressable.

## Decision

### Technology choices

| Concern | Choice | Rationale |
|---------|--------|-----------|
| Server state | TanStack Query v5 | Key-based invalidation, direct cache patching via `setQueryData`, background revalidation, mutation lifecycle hooks |
| Realtime transport | SSE (Server-Sent Events) | Unidirectional server→client telemetry; browser-native `EventSource` with reconnect; no WebSocket complexity |
| Client state | URL params (shareable) + Zustand (ephemeral UI only) | URL is source of truth for anything a teammate should see; Zustand for transient UI like toast queue and sidebar collapse |
| State truth | Backend-owned NormalizedState ([ADR-0033](ADR-0033-unified-entity-state-model.md)) | Frontend renders; frontend does not compute operational severity. Transitional compat layer permitted during backend rollout per ADR-0033 migration plan |

### Four state classes

Frontend state is partitioned into four classes with strict ownership:

| Class | Examples | Owner | Persistence |
|-------|----------|-------|-------------|
| Authoritative operational | Runs, shows, plays, system health, attention | Backend via TanStack Query | Query cache (memory) |
| Derived judgments | Severity, attention inclusion, staleness | Backend (permanent) or frontend compat layer (transitional) | Computed from Class 1 |
| View state | Filters, sort, selected entity, inspector tab, time range | URL search params | URL |
| UI preference | Theme, table density, column visibility | localStorage | localStorage |

Rule: **If state affects what an operator believes about system health,
it must come from the backend or be explicitly marked as stale/local.**

### Query key factory

All query keys use a typed factory. No hand-written string arrays.

```typescript
export const qk = {
  dashboard: {
    summary: (scope: string, range: string) =>
      ["dashboard", "summary", { scope, range }] as const,
  },
  runs: {
    list: (params: RunListParams) =>
      ["runs", "list", canonicalize(params)] as const,
    detail: (id: string) =>
      ["runs", "detail", id] as const,
  },
  shows: {
    list: (params: ShowListParams) =>
      ["shows", "list", canonicalize(params)] as const,
    detail: (id: string) =>
      ["shows", "detail", id] as const,
    plays: (id: string) =>
      ["shows", "detail", id, "plays"] as const,
  },
  invocations: {
    list: (params: InvocationListParams) =>
      ["invocations", "list", canonicalize(params)] as const,
    detail: (id: string) =>
      ["invocations", "detail", id] as const,
  },
  teams: {
    list: (params: TeamListParams) =>
      ["teams", "list", canonicalize(params)] as const,
  },
  schedules: {
    list: (params: ScheduleListParams) =>
      ["schedules", "list", canonicalize(params)] as const,
  },
  knowledge: {
    list: (scope: ScopeRef, status: string[]) =>
      ["knowledge", "list", { scope, status }] as const,
    detail: (id: string) =>
      ["knowledge", "detail", id] as const,
    byScope: (scopeType: string, scopeId: string) =>
      ["knowledge", "byScope", { scopeType, scopeId }] as const,
  },
  library: {
    list: (params: LibraryListParams) =>
      ["library", "list", canonicalize(params)] as const,
  },
  search: {
    cross: (params: SearchParams) =>
      ["search", "cross", canonicalize(params)] as const,
  },
} as const;
```

`canonicalize()` sorts object keys so equivalent filters produce
identical cache keys.

### Dashboard summary endpoint

The dashboard uses a single backend-computed summary, not coordinated
frontend queries:

```text
GET /api/dashboard/summary?project=all&range=24h
```

Returns: attention items, metrics, active runs, at-risk shows,
at-risk schedules, system health, recent activity, inventory counts.

The `AttentionQueue`, `OpsSnapshotGrid`, and risk panels all read
from the same summary object. No race conditions between independent
queries.

### SSE architecture

One central `EventSource` per active project scope. Components do NOT subscribe independently.

```text
GET /api/events?project=all
Accept: text/event-stream
```

A `StudioRealtimeProvider` opens the stream and dispatches events to TanStack Query's cache.

#### Event envelope

Every event uses this shape:

```typescript
type StudioEvent = {
  id: string;                          // for Last-Event-ID resume
  type: StudioEventType;
  scope: { project: string };
  invalidates: QueryKey[];             // cache keys to refresh
  patch?: { key: QueryKey; data: unknown };  // optional direct cache patch
  version?: number;                    // for race-resolution with mutations
  emitted_at: number;
};
```

#### Event type taxonomy

Events are namespaced by entity type:

| Namespace | Events |
|-----------|--------|
| `run.*` | `created`, `updated`, `completed`, `failed`, `cancelled`, `aborted` |
| `play.*` | `updated`, `blocked`, `merged`, `failed` |
| `show.*` | `updated`, `merged`, `failed` |
| `invocation.*` | `updated`, `completed`, `failed` |
| `schedule.*` | `due`, `fired`, `misfired` |
| `team.*` | `created`, `closed`, `orphaned` |
| `attention.*` | `added`, `cleared`, `severity_changed` |
| `knowledge.*` | `created`, `verified`, `disputed`, `superseded` |
| `system.*` | `health_changed`, `resync_required` |

The `knowledge.*` events feed the Knowledge lens (see [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md)) and may also generate attention queue items (e.g., a claim transitioning to `disputed` is attention-worthy).

#### Dispatch strategy

- **Row-level updates** (e.g., `run.updated` with `patch`): Direct cache patch via `setQueryData`. No debounce — operators should see failures immediately.
- **Aggregate invalidation** (`invalidates: ["dashboard", "summary", ...]`): Debounced (250-500ms) `invalidateQueries`. Debounce key is the stringified query key; rapid identical invalidations coalesce.

#### Mutation/SSE race resolution

A mutation's success handler patches the cache; the SSE event for the same change may arrive milliseconds later. Both code paths apply IDEMPOTENT patches with the same shape, so re-application is harmless. Where order matters (e.g., user sees an optimistic patch and then a server-corrected one), the `version` field on the event resolves: higher version wins, lower version is ignored.

#### Disconnect behavior

1. Status → `reconnecting` after 0s
2. Compact global banner after 3s
3. Fallback polling starts for operational queries (uses freshness budget "Disconnected poll" column)
4. On reconnect, resume from `Last-Event-ID`
5. If event gap exceeds retention (server-side), emit `system.resync_required` → full invalidation

### Freshness budgets

| Surface | SSE live | Disconnected poll | Stale warning |
|---------|---------|-------------------|---------------|
| Dashboard summary | 30s verification | 5s | 15s |
| Show detail | 15s | 5s | 10s |
| Runs table | 30s | 10s | 20s |
| Run detail | 15s | 5s | 10s |
| System health | 15s | 5s | 10s |
| Library content | 5m | 60s | 10m |
| Knowledge lens | 60s verification | 30s | 120s |

Knowledge claims change on agent action, not continuously. A longer budget reflects this: a verified claim from 2 hours ago is not "stale" the way a session from 30 seconds ago might be.

The UI renders freshness state on every operational surface:

```text
Live · verified 8s ago
Disconnected · polling every 5s
Stale · last verified 48s ago
```

### URL as state

The URL owns all shareable view state:

| Param | Purpose | Example |
|-------|---------|---------|
| `project` | Project scope | `project=lionagi` |
| `range` | Time range | `range=24h` |
| `q` | Search text | `q=exit%20124` |
| `status` | Outcome filter | `status=failed,timed_out` |
| `health` | Health filter | `health=stalled` |
| `sort` | Sort order | `sort=-severity,-updated` |
| `selected` | Selected entity | `selected=run:dad0dc05` |
| `panel` | Inspector panel | `panel=logs` |

URL state is validated through Zod schemas. Browser back/forward
restores the full view state. Opening a shared link restores filters,
sort, selected entity, and inspector tab.

### Zustand scope

Zustand holds ONLY ephemeral UI state:

```typescript
type UiStore = {
  commandPaletteOpen: boolean;
  sidebarCollapsed: boolean;
  toasts: ToastState[];
};
```

Zustand MUST NOT store runs, shows, schedules, or any operational data.

### Mutations

TanStack mutations for all write operations. No optimistic success for
destructive or operationally meaningful actions:

- Allowed: button shows "Cancelling...", row action disabled
- Not allowed: immediately flipping Running → Cancelled before backend confirms

On mutation success: patch detail cache, invalidate lists and dashboard.
On mutation failure: inline row error + toast. Do not hide the row.

### Cross-entity search

```text
GET /api/search?q=failed&project=all&range=1h&type=all
```

Returns results across runs, shows, plays, invocations, teams, schedules, library entities, AND knowledge claims. Each result carries its `NormalizedState` (operational entities) or `claim_status` (knowledge claims) for consistent severity rendering across the result set.

Surfaced via `Cmd/Ctrl-K` command palette for quick navigation and
a dedicated `/search` route for shareable result pages.

### Backend-frontend sync contract

Issues like #1161 (show status stuck at "Active"), #1167 (Runs page MODEL column empty), and #1177 (action panel auto-synthesis) reveal a sync-contract gap: the backend has data, the frontend doesn't render it. This section makes the contract explicit.

**Three sync modes, used together**:

| Mode | When | Mechanism |
|------|------|-----------|
| **Pull on access** | User navigates to a route | Route loader fires the relevant TanStack Query; cache-first if fresh, network if stale |
| **Push on change** | Server-side event | SSE event from `StudioRealtimeProvider` patches or invalidates affected cache keys |
| **Reconcile on interval** | Long-disconnected client | Disconnected polling per freshness budget; on reconnect, resync from `Last-Event-ID` or full re-fetch |

**No mode operates alone**. Every operational surface uses (Pull on access) + (Push on change), with (Reconcile on interval) as the disconnected fallback. The `data-freshness-badge` (defined in [ADR-0035](ADR-0035-design-system-and-component-library.md)) reflects which mode is currently authoritative.

**Contract guarantees**:

- A backend write becomes visible to the frontend within the SSE freshness budget for that entity (e.g., 15s for Show detail). If SSE is disconnected, within the poll interval (5s for Show detail).
- The displayed `NormalizedState` is never more than one budget cycle stale; staler than that, the freshness badge transitions to `stale` and the operator sees the warning.
- Mutations patch the cache synchronously on success; SSE for the same change arriving later is a no-op (idempotent re-patch).

## Consequences

**Positive**

- Single cache for operational state — no drift between dashboard
  and detail pages.
- SSE provides near-instant failure visibility without polling overhead.
- URL-addressable views enable operational collaboration ("look at this
  filtered view").
- Freshness indicators prevent false confidence in stale data.
- Query key factory prevents cache key collisions and enables precise
  invalidation.

**Negative**

- TanStack Query adds ~40KB to the bundle (gzipped).
- Zustand adds ~2KB.
- SSE requires a new backend endpoint and event persistence table.
- Dashboard summary endpoint requires backend implementation before
  the frontend migration can complete.
- URL state adds complexity to every table/filter component.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| SWR instead of TanStack Query | No built-in mutation lifecycle hooks; less precise cache invalidation |
| WebSocket instead of SSE | Bidirectional not needed; SSE has native browser reconnect; lower complexity |
| Redux for all state | Too much ceremony for the state we actually have; most state is server-owned |
| Per-component polling | Race conditions between components; no cache coordination; O(n) API calls |
| localStorage for view state | Not shareable; no deep linking; doesn't survive incognito |

## References

- [ADR-0006](ADR-0006-sse-live-streaming.md) — SSE Live Streaming (event transport)
- [ADR-0009](ADR-0009-sqlite-state-layer.md) — SQLite State Layer (current backend persistence)
- [ADR-0030](ADR-0030-attention-queue.md) — Attention Queue
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — Unified Entity State Model
- [ADR-0035](ADR-0035-design-system-and-component-library.md) — Design System & Component Library
- [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) — Knowledge Substrate
- TanStack Query v5 documentation
- MDN: Using Server-Sent Events
- Issue #1161, #1167, #1177 — backend-frontend sync gaps addressed by §Backend-frontend sync contract
