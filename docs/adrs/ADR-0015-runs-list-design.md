# ADR-0015: Runs List Design — Identity, Filters, Pagination

**Status**: Accepted
**Date**: 2026-05-20
**Extends**: ADR-0012 (execution lineage), ADR-0004 (data authority)

## Context

The runs list page (`/runs`) shows 376 SQLite sessions in a flat table with
minimal identity: name (often just "flow" or "agent"), branch count, message
count, status (almost all "completed"), and timestamps. With enriched session
provenance (ADR-0012: `playbook_name`, `agent_name`, `invocation_kind`,
`show_topic`, `show_play_name`, `source_kind`), the list can become an
operational execution index.

The page currently has no search, no filters, no pagination, and no way to
distinguish 376 rows that mostly share the same name and status.

## Decision

### 1. Two-line row with display name + meta line

Each row has a primary display name (computed from provenance) and a secondary
meta line (session ID + supplementary context):

```
unimpl-adr-sweep / closeout-audit        show play   completed   1 br · 24 msg   15h ago
  session 20260520T0A7... · playbook show · agent orchestrator

flow                                     flow        completed   6 br · 632 msg  1:33 PM
  session 20260520T015... · started May 19 11:06 PM
```

### Display-name algorithm

Fallthrough from most-specific to least-specific:

```ts
if (show_topic && show_play_name) → `${show_topic} / ${show_play_name}`
else if (playbook_name)           → playbook_name
else if (agent_name)              → agent_name
else if (name)                    → name
else                              → shortSessionId
```

Meta line: session ID + any provenance not already in the display name +
start time. **No "unlinked" or "unavailable" labels** — if provenance is
absent, the row simply falls back to session ID and timestamp.

### 2. Default columns

| Column | Width | Default | Content |
|--------|-------|---------|---------|
| Run / Origin | flex:1, min 420px | Visible | Display name + meta line |
| Kind | 110px | Visible | `agent`, `play`, `flow`, `fanout`, `show play` |
| Status | 120px | Visible | Normalized display status (ADR-0012 mapping) |
| Activity | 140px | Visible | `N br · M msg` |
| Updated | 150px | Visible | Relative timestamp, primary sort |

Hidden columns (available via `[Columns]` toggle):
- Started, Source kind, Playbook, Agent, Show topic, Show play, Session ID,
  Branches (split), Messages (split)

### 3. Filter bar

Dense two-row filter bar above the table:

```
[ Search name, id, show, play, playbook, agent... ]      [Columns ▾]
Status: [All] [Running] [Completed] [Failed] [Awaiting gate]
Kind:   [All] [Agent] [Play] [Flow] [Fanout] [Show play]
Source: [All] [Live] [Imported fs] [With show] [With playbook]   Rows: [100 ▾]
```

Search matches: `session.id`, `session.name`, `playbook_name`, `agent_name`,
`invocation_kind`, `show_topic`, `show_play_name`, `source_kind`. Client-side
filtering on the loaded session list (no server-side search endpoint needed at
376 rows).

### 4. Pagination at 100 rows/page

| Row count | Strategy |
|-----------|----------|
| 0–200 | Render all, no pagination needed |
| 200–2,000 | Paginate, default 100/page |
| 2,000+ | Server-side pagination (add LIMIT/OFFSET to sessions query) |

No virtual scroll. It complicates row heights, browser find, copy workflows,
keyboard navigation, and future expandable rows. Pagination is simpler.

### 5. Empty provenance handling

During the transition period (most sessions have null provenance):
- Rows with provenance get descriptive display names (show/play, playbook, agent)
- Rows without provenance fall back to session name + timestamp
- No "unlinked" labels, no "missing" indicators — absence is handled by
  graceful fallback, not by calling attention to what's missing
- As new sessions are created with provenance, they naturally stand out

## Consequences

**Positive**
- Every row is identifiable even when 100+ share the same session name.
- Filters enable targeted diagnosis (show me all failed show-plays).
- Pagination prevents render performance degradation.
- Column toggle lets power users expose raw metadata when needed.

**Negative**
- Two-line rows use more vertical space per row (~52px vs ~36px).
- Display-name algorithm requires client-side computation per row.
- Filter state is local (no URL query params yet — add when deep-linking needed).

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Virtual scroll instead of pagination | Complicates browser find, copy, keyboard nav; pagination is simpler |
| Per-row error count column | Requires loading all messages for every session in the list query; too expensive |
| "Unlinked" labels for missing provenance | Visual noise on 376 rows that ALL lack provenance; trains users to ignore the block |
| Sidebar filters | Takes horizontal space from the table; above-table filter bar is denser |
| Server-side search | Not needed at 376 rows; client-side filtering is sufficient |
