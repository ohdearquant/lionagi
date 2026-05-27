# ADR-0012: Studio Execution Lineage & UX Redesign

**Status**: Accepted
**Date**: 2026-05-20
**Extends**: ADR-0009 (SQLite state layer), ADR-0010 (plugins), ADR-0011 (shows data model), ADR-0017 (session lifecycle)

---

> **Related update**: This ADR defines execution lineage and status display vocabulary. [ADR-0033](ADR-0033-unified-entity-state-model.md) formalizes the backend-owned `NormalizedState` that drives all status display, severity computation, and reason-code attachment. The display mappings here are preserved for backwards compatibility; new display code should consume `NormalizedState` directly. The lineage relationships (run → invocation → tool_call) remain authoritative for execution provenance.

---

## Context

Lion Studio does not have a UI naming problem. It has an **execution data-contract
problem**. Users can see playbooks, agents, plugins, runs, and shows as isolated
pages, but the causal chain between them is not surfaced:

```text
Playbook / Agent → Show Play → Session → Branch → Messages / Tool Calls → Artifacts
```

Three rounds of design review (initial assessment, developer counterpoint, synthesis)
converged on these findings:

1. The app has two disconnected persistence layers: filesystem runs (549) and SQLite
   sessions (376). They overlap partially, use different schemas, and are queried by
   different pages. The dashboard counts filesystem runs; the list page shows sessions.
2. Show plays have `session_id` in the database but the frontend ignores it entirely.
3. Sessions do not store which playbook, show, or agent spawned them.
4. Status vocabulary is inconsistent across pages (`Run complete`, `Merged + pushed`,
   `completed`, `running_complete`).
5. `ExecutionDag.tsx` (step-level execution graph) is disconnected — exists but not
   imported anywhere.
6. The run detail page has no structural organization — flat branch/message scroll.

## Decision

### 1. SQLite as canonical query layer; enrich sessions

SQLite becomes the canonical source for all execution queries. Filesystem runs
(`~/.lionagi/runs/`) become a legacy import source, not a query target.

**Do not create a separate `executions` table.** At 376 sessions and 2 shows, an
extra abstraction layer adds migration/API/query overhead without solving an
immediate problem. Instead, enrich the `sessions` table with provenance columns:

```sql
ALTER TABLE sessions ADD COLUMN playbook_name  TEXT;
ALTER TABLE sessions ADD COLUMN agent_name     TEXT;  -- agent/role that ran (e.g., "architect", "analyst")
ALTER TABLE sessions ADD COLUMN invocation_kind TEXT;  -- agent|play|flow|fanout|show-play
ALTER TABLE sessions ADD COLUMN show_topic     TEXT;
ALTER TABLE sessions ADD COLUMN show_play_name TEXT;
ALTER TABLE sessions ADD COLUMN artifacts_path TEXT;
ALTER TABLE sessions ADD COLUMN source_kind    TEXT DEFAULT 'live';  -- live|imported_fs
```

The filesystem runs import (`li state import`) writes enriched session rows with
these fields populated. Future CLI invocations (`li play`, `li agent`, `li o flow`)
write provenance at session creation time.

If the sessions table becomes unwieldy later, extracting an `executions` layer from
enriched sessions is straightforward. Going the other direction is harder.

### 2. Navigation: keep "Runs", reorder, no Dashboard item

```text
Playbooks | Agents | Plugins | Shows | Runs
```

Changes from current (`Playbooks | Agents | Plugins | Runs | Shows`):

- **Shows** moves before **Runs** — shows orchestrate plays that create sessions.
  Left-to-right matches causality.
- **"Runs"** stays as the nav label. Users think "I ran a playbook," not "I created
  a session." The internal data model uses sessions; the user-facing concept is runs.
- **No Dashboard nav item.** Logo-as-home is a universal convention. Adding it costs
  nav space on a power-user tool with one primary user. Add a tooltip on logo hover.

### 3. Status vocabulary: display mapping over raw statuses

Keep raw statuses in the data layer (the show skill's state machine needs them for
resume). Add a display mapping for the UI only.

**Display vocabulary**:

| Display Status | Raw Statuses | Color | Category |
|---------------|-------------|-------|----------|
| `pending` | pending, prepared | Amber | Lifecycle |
| `running` | running | Blue | Lifecycle |
| `awaiting_gate` | running_complete, gated | Amber | Lifecycle |
| `completed` | completed, done, success, finished | Green | Lifecycle |
| `failed` | failed, error, gate_failed | Red | Lifecycle |
| `aborted` | aborted, aborted_after_finish, cancelled | Gray | Lifecycle |
| `redoing` | redoing | Blue | Lifecycle |
| `blocked` | blocked | Orange | Lifecycle |
| `escalated` | escalated | Orange | Review |
| `completed` | merged | Green | Lifecycle (+ integration badge `merged`) |

**Gate badges** (plays only): `passed` (green), `failed` (red), `skipped` (gray).
**Integration badges** (plays only): `merged` (green), `local` (gray).

The shows detail page uses a single State column with a primary lifecycle pill plus optional secondary gate and integration badges (see ADR-0011 for the badge spec).
List views show the lifecycle pill as primary, with gate/integration as secondary badges.
Detail views show the raw status in a metadata section.

**"Completed with errors" pattern**: Tool errors are diagnostic, not
status-changing. A completed session with intermediate tool failures displays as:

```text
completed · 112 intermediate tool errors
```

Color: green status pill + amber diagnostic chip (not a different status). No
`completed_with_errors` status — that would turn normal agent retry behavior
into an apparent degraded lifecycle state.

- **Runs list**: do not distinguish clean vs error-containing completed sessions
  until error counts are precomputed. Same green pill for both.
- **Dashboard metrics**: intermediate tool errors do not feed `Needs review` or
  `Failed`. Dashboard cards mean: Running (active), Failed (terminal), Slow
  (duration threshold), Needs review (human gate, critic escalation, blocked).
- **Run detail Overview**: label as `TOOL ERRORS: 112 intermediate`. Section
  renamed from "Errors" to "Tool errors" with explanatory copy: "These are
  failed intermediate tool calls during a session that completed."
- **Threshold**: no error-rate threshold yet. Do not flag high error rates as
  `Needs review` until the system can distinguish exploratory failure from
  actual degradation. A passing session is not degraded because it had many
  intermediate tool failures.

### 4. Run detail: anchored sections with sticky nav

The run detail page restructures from a flat branch/message scroll to anchored
sections with a sticky section nav. **Not tabs** — tabs hide information, which is
wrong for a debugging tool.

```text
Sticky section nav (dynamic ordering):
[Overview] [Errors 112] [Branches 7] [Files 11] [Execution]

When errors > 0: Errors appears second (before Branches).
When errors = 0: Errors shows "No errors ✓" after Branches.
All sections visible on one scroll.
Heavy sections (branches, messages) are collapsible/lazy-rendered.
Each branch is an accordion.
```

**Overview section** (new):

- Verdict/outcome badge with error qualifier: `completed · 112 intermediate tool errors`
- Duration with disambiguation: session duration vs branch duration when different
- Branch count, message count, tool call count, error count (labeled "intermediate
  tool errors" not just "errors")
- Source provenance: show/play/playbook backlinks (from enriched session fields).
  Show provenance block only when at least one field is populated — do not display
  5 lines of "unlinked / unavailable" for every historical session.

**Errors section**: failed tool calls grouped by tool function name, with count,
branch, timestamp, excerpt, and expandable raw output. No inferred impact column —
the final verdict is the impact signal. Add "collapse recovered failures" toggle
when agents emit recovery metadata. Always present (shows "No errors" with check
when empty — this is a positive signal, not dead weight).

**Branches section**: each branch as a collapsible accordion showing messages.
Tool failures use a red left border, not full red rows. Sequential failures of
the same tool are grouped.

**Files section**: list of touched/created files from tool call arguments.

**Execution section**: `ExecutionDag` rendered here only when playbook context is
known (navigated from playbook detail or session has `playbook_name`). Otherwise
shows "Playbook context unknown — navigate from playbook detail for execution graph."

### 5. ExecutionDag restoration: playbook-first

Restore `ExecutionDag.tsx` starting from **playbook context**, not arbitrary session
context. This sidesteps the session-to-playbook resolution problem.

| Location | What it shows | Prerequisite |
|----------|---------------|-------------|
| Playbook detail → Executions section | Graph with latest execution status overlay | Playbook has steps/links (graph-format) |
| Run detail → Execution section | Same graph with this run's status | Session has `playbook_name` matching a graph-format playbook |

For **declarative playbooks** (no steps/links), do not synthesize a fake graph.
Show a linear execution summary instead:

```text
Agent: architect → 47 tool calls → 11 files → Verdict: PASS
```

**Provenance instrumentation starts now**: all CLI commands (`li play`, `li agent`,
`li o flow`, `li o fanout`) write `playbook_name`, `agent_name`, `invocation_kind`
to the session at creation time. This is cheap and makes future ExecutionDag
wiring automatic.

> **`show_topic` / `show_play_name` (show-play lineage) is deferred.** The
> show orchestration runner that would supply these fields lives outside the
> single-process CLI surface this PR ships. Standalone agent / flow / fanout /
> play invocations write `NULL` for those columns; the `show-play` value in
> the `invocation_kind` vocabulary is reserved for the future runner that
> will pass `--show-topic` / `--show-play-name` (or an equivalent env
> contract) through to `start_live_persist`. Tracking issue: TBD.

### 6. Show → Session lineage

Surface `plays.session_id` on the shows detail page:

- **Inline accordion** for play details (not a drawer — preserves DAG/table visual
  link). Clicking a play row expands it vertically to show: intent, agent/playbook,
  session link (`Open Session →`), gate verdict, duration, artifacts.
- **Reverse lookup**: run detail page queries `plays WHERE session_id = ?` to show
  "Source: Show {topic} / Play {name}" backlink in the Overview section.
- **Historical backfill**: one-time effort to match the 26 existing plays to sessions
  by timestamp overlap or branch name similarity. Small effort, high payoff — the
  shows page works fully for existing data, not just future data.

### 7. Per-page filters (before global search)

Complete the half-done per-page filtering:

| Page | Current | Add |
|------|---------|-----|
| Playbooks | No filter | Search input in left pane |
| Agents | Has search | Done |
| Plugins | Has filter | Skill search within selected plugin |
| Runs | No filter/search/pagination | Full redesign per ADR-0015: identity column, filter bar, pagination (100/page) |
| Shows | No filter | Status filter chips |

Global search / command palette deferred to a later phase. At ~500 entities, per-page
filters give better ROI. Search becomes important when artifacts are indexed.

### 8. Shows detail enhancements

- **Layout: plays table is full-width primary.** `_show.md` moves below the plays
  table as a collapsed full-width toggle, not a side-by-side column. Always collapsed
  by default (no auto-open for active shows).
- **PlayDag**: compact dependency graph strip above plays table (112-220px tall
  depending on play count). Visible by default. See ADR-0011 for pixel guidance.
- **DAG + table linked**: hover row highlights node, hover node highlights row,
  click node scrolls to and expands play row.
- **Play details as inline accordion.** Session link (`Open Session →`) is the
  first element in the expanded area.
- **State cell**: structured multi-badge — primary lifecycle pill + secondary gate
  and integration badges in one column. See ADR-0011 for badge spec.

### 9. Quick fixes

- **Toast wiring**: Toast component exists. Wire into save (agents, playbooks),
  create (new agent, new playbook), run (playbook Run button), and rollback actions.
  This is the fastest UX win available — zero feedback on save is actively harmful.
- **Plugin source display names**: map raw source slugs to human labels in the
  frontend. `marketplace` → `Lion Marketplace`, `claude-plugins-official` →
  `Anthropic Official`. Raw value in tooltip.
- **Breadcrumbs**: on deep pages (`Shows / topic`, `Runs / session-name`).
- **Cross-links**: `Open in Agents →` from plugin agent tab. `View source` from skills.
- **Proportional font** for README/prose; monospace for YAML/paths/commands/logs.
- **Diagnostic empty states**: plugins show scanned path + last scan time when 0 found.
  Other pages use simple "No items" — no onboarding copy for a power-user tool.
- **Version history**: move to drawer, hide sidebar when empty. `Versions (N)` button
  in definition header.
- **Playbook left-pane search** — now the only two-pane page without a filter,
  inconsistent with Agents and Plugins.

### 10. Dashboard source of truth and improvements

**Dashboard queries SQLite sessions only.** The inventory strip shows the count
of sessions the UI can actually list and open — not filesystem run directories.
If the runs page shows 376 sessions, the dashboard shows 376 runs.

```text
INVENTORY    20 playbooks    17 agents    376 runs    2 shows
```

The 549 filesystem run directories are visible on the runs page as import status,
not on the dashboard:

```text
Import status: 549 filesystem dirs detected · 376 indexed · 173 unindexed
[Run import]
```

This separation keeps operational state on the dashboard and migration state where
it is actionable.

Other improvements:

- Metric cards clickable, routing to filtered runs list (`/runs?status=failed`).
- "Needs attention" surfaces slow/stale when count > 0. Does NOT include
  intermediate tool errors (those are diagnostic, not operational).
- Recent activity includes source context (show/playbook/agent from enriched sessions).
- Interval refresh (30s `setInterval` on `/api/stats`), not SSE.

## Implementation Phases

**Phase 0 — Prerequisites** (implemented on `feat/studio-monitoring-polish`,
pending merge):

- Session schema enrichment + provenance columns (v2→v3 migration)
- Show → Session drill-down (play accordion with session links)
- Run detail anchored sections (Overview, Tool errors, Branches, Files)
- Nav reorder (Shows before Runs) + status display mapping
- Toast component (not yet wired to actions)
- Plugin marketplace name labels

**Next** (implementation order per design review):

| Phase | Scope | Priority | Effort | ADR |
|-------|-------|----------|--------|-----|
| 1 | Runs list redesign (identity column, filter bar, pagination) | P0 | Medium | ADR-0015 |
| 2 | Dashboard sessions-only count + import status on runs page | P0 | Quick | ADR-0012 §10 |
| 3 | Tool errors naming + "completed with intermediate errors" copy | P0 | Quick | ADR-0012 §3 |
| 4 | Shows table State cell (lifecycle + gate + integration badges) | P1 | Medium | ADR-0011 |
| 5 | Compact PlayDag strip above full-width plays table | P1 | Medium | ADR-0011 |
| 6 | Wire toasts into save/create/run/rollback actions | P1 | Quick | ADR-0012 §9 |
| 7 | Quick fixes (breadcrumbs, cross-links, playbook search, version drawer) | P1 | Quick | ADR-0012 §9 |
| 8 | Shows layout: _show.md below plays, always collapsed | P1 | Quick | ADR-0012 §8 |
| 9 | ExecutionDag on playbook detail (graph-format playbooks only) | P2 | Medium | ADR-0012 §5 |
| 10 | Definitions API + `definitions` table + save/rollback versioning | P1 | Medium | ADR-0016 |
| 11 | Definition editor standardization (shared shell, version drawer) | P2 | Medium | — |

## Consequences

**Positive**

- Full execution chain navigable: show → play → session → messages → artifacts.
- Single query source (enriched sessions) eliminates count mismatches.
- Status display mapping preserves raw state machine while showing consistent UI.
- Anchored sections preserve debugging scanability (nothing hidden behind tabs).
- Provenance instrumentation is cheap now, expensive to retrofit later.
- Zero new tables — enriched sessions avoid premature abstraction.

**Negative**

- Session table gains 7 nullable columns — acceptable at current scale, may need
  extraction to an `executions` table if the table becomes unwieldy.
- Historical backfill for 26 plays requires manual/heuristic matching.
- Anchored sections require lazy rendering for large branch/message counts.
- Display status mapping adds a translation layer that must stay in sync with
  the show skill's state machine.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Separate `executions` table | Premature abstraction at 376 sessions. Adds migration/API/FK overhead. Extract later if needed. |
| Rename "Runs" to "Sessions" | Users think "I ran a playbook," not "I created a session." Label matches mental model, not data model. |
| Add Dashboard nav item | Logo-as-home is universal convention. 6 nav items costs cognitive load. Add tooltip instead. |
| Hard tabs on run detail | Tabs hide information. Debugging tool needs everything visible. Anchored sections with sticky nav is better. |
| Drawer for show play details | Competes with DAG for horizontal space. Inline accordion preserves table/DAG visual link. |
| Global search as priority | Per-page filters are half-done and cheaper. ~500 entities does not justify a command palette yet. |
| Component library (Radix, etc.) | Zero-dependency pattern worth keeping. Custom toast is ~100 LOC. Revisit when dialogs proliferate. |

## References

- Design review round 1: ChatGPT executive assessment (2026-05-20)
- Design review round 2: Developer counterpoint (`DESIGN_REVIEW_FOLLOWUP.txt`)
- Design review round 3: Synthesis with final decisions
- Design review round 4: Post-implementation visual review (confirmed anchored sections,
  identified _show.md layout regression, error labeling gap)
- ADR-0009: SQLite state layer
- ADR-0010: Plugin-aware Studio (updated: cross-links, source badges)
- ADR-0011: Shows data model (updated: play accordion, status badges, provenance)
- ADR-0013: Zero-dependency UI components
- ADR-0014: CLI-primary, Studio-secondary
- ADR-0015: Runs list design (identity, filters, pagination)
- ADR-0017: Session lifecycle and status derivation (status column, dashboard queries)
- `apps/studio/frontend/components/ExecutionDag.tsx` (disconnected, to restore)
