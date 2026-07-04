# ADR-0093: Studio Three-Surface Information Architecture

**Status**: Proposed
**Date**: 2026-07-04
Related: ADR-0026 (project detection), ADR-0034 (frontend data/state architecture), ADR-0063
(task board work center — this ADR supersedes its operator-UI decision; its `work_items`
schema remains dormant), ADR-0061/0062 (scheduler + state machine).

## Context

Lion Studio's frontend grew one route per database entity: dashboard, runs, invocations,
kanban, playfield, shows, schedules, playbooks, agents, teams, skills, plugins, engines,
projects, admin/health, admin/maintenance — roughly sixteen leaf destinations, most of them a
thin table over one table. The founder's review of the live app: cluttered, ill designed,
"just a glossary of info, like a db reader", and — after a first incremental nav pass — "still
a bit too many pages and tabs, and this is not redesign enough". An earlier design review had
already ruled that runs, shows, and invocations as separate pages is wrong and they must
become one unified history timeline, and that the canvas is the command-and-control center of
the app.

The deeper problem is that the route tree mirrors the storage schema instead of the operator's
mental model. Everything in Studio is one of three things: something that **executed** (a
session, an invocation, a schedule run, a play — different provenance, same shape: started,
ran, ended in a status), something **defined** (a script, an agent profile, a schedule, a
workflow, a skill, a plugin, an engine, a team), or the **machine itself** (database health,
staleness, maintenance). Sixteen pages force the operator to reassemble that model by hand on
every visit; status semantics ("running", staleness) are currently derived differently on
each page, and the kanban view renders every historical run unvirtualized.

Constraints: daemon/API changes must be additive only; e2e tests never touch the real
state.db (a seeded temp-db harness exists); every retired path keeps a redirect; the stack
stays React 19 + TanStack Router + Vite + Tailwind (CodeMirror and @tanstack/react-virtual are
the only new dependencies); both themes ship together with established contrast floors;
workflow definitions are YAML for now.

## Decision

Rebuild the frontend around **three surfaces**, each answering one operator question, with all
former pages becoming URL-addressable *states* (params, filters, slide-overs) of those
surfaces rather than routes:

| Surface | Route | Question | Absorbs |
|---------|-------|----------|---------|
| **Operations** | `/` | What is happening, and what happened? | dashboard, runs, invocations, kanban, playfield, shows |
| **Library** | `/library` | What can run, and how is it defined? | playbooks (as *scripts*), agents, schedules, workflows (new), skills, plugins, engines, teams |
| **System** | `/system` | Is the machine healthy? | admin/health, admin/maintenance, projects (inventory) |

A global **project lens** (top bar switcher, `?project=` + localStorage, per ADR-0026 columns)
scopes Operations fully; in Library it filters project-scoped items while keeping global items
visible and labeled; System is never scoped. A **command palette** (⌘K) provides cross-surface
jump and actions, which is what lets three destinations carry sixteen pages of reach.

### Operations — the canvas

One continuous surface over a single **unified Run model** aggregated in the frontend from
existing endpoints (sessions, invocations + schedule-run failure fields, schedule runs, plays,
engine runs):

```text
Run { id, source: agent|schedule|script|flow, status, project, started_at, updated_at,
      duration, reason?{code, summary, error_detail, exit_code},
      chain?{parent, children[]}, refs{session_id?, invocation_id?, schedule_id?, topic?} }
```

- **Attention header, not a home page.** The landing state carries compact stat chips
  (Running, Failed, Stale, Slow) computed from the same query the canvas shows. A chip is a
  filter, not a link: clicking it narrows the canvas in place. There is no separate dashboard
  route; the old dashboard's job (triage) is the canvas's default state.
- **One canvas, three projections.** `?view=stream` (default; reverse-chron chain cards),
  `?view=board` (grouped by status), `?view=table` (dense, sortable — the power-user escape
  hatch preserving today's runs/invocations tables). Same query, same filter bar
  (project · status · source · window · text), different projection. Views are a presentation
  control, not tabs with different content.
- **Detail is a slide-over, never a navigation.** `?run=<id>` opens a right slide-over
  (Overview · Output with SSE live tail · Messages · Artifacts · Chain · Raw) with full parity
  to the old detail pages, plus actions: cancel, re-fire (schedule-sourced), copy
  `li agent -r <id>`. Deep links keep working; the canvas never unloads behind it.
- **Chains render as one card.** schedule → schedule_run (chain_parent_id) → invocation →
  session collapse into a single feed card with children, ending the four-page hunt for one
  firing's story.
- **One status oracle.** A single `deriveRunStatus` module (shared by chips, all three
  projections, and the slide-over) demotes "running" to **Stale** when the process-liveness
  signal says dead, and renders spent one-shot schedules as **Expired**. No surface ever again
  shows "Running · Healthy" from a dead pid.
- **Windowed and virtualized by default.** Default window 24h with load-more; board and
  stream virtualize via @tanstack/react-virtual. Rendering 3,868 historical runs at once is a
  defect class this ADR retires.

### Library — the definitions

A single surface with a type rail (`?kind=script|agent|schedule|workflow|skill|plugin|engine|team`),
a catalog list per kind, and a **generous full-height editor** (`?id=`) — no per-kind routes,
no cramped modals. Plays/playbooks present as **Scripts** everywhere in the UI (backend nouns
unchanged; a subtitle maps the old name during transition). Schedules edit here; their firings
live in Operations (`/?source=schedule&id=…` one click away). **Workflows** are a new kind:
YAML definitions edited in CodeMirror with schema validation and a plan preview before save,
backed by additive daemon endpoints (`POST /api/workflows/validate`, plan-preview). Every zero
state invites creation with a primary CTA.

### System — the machine

One scrolling page, no tabs: health (db size, WAL, connections, staleness sweep) on top,
maintenance actions (checkpoint, prune, vacuum — each with explicit confirmation) below,
project inventory at the end. Destructive operations get a deliberate surface, which is why
System stays a route rather than a popover.

### Route contract

Three routes plus params are the entire public URL surface:

```text
/          ?project ?view ?status ?source ?window ?q ?run ?topic ?live
/library   ?project ?kind ?id ?new
/system    (#maintenance anchor)
```

Every legacy path redirects: `/runs→/?view=table` · `/runs/$id→/?run=$id` ·
`/invocations→/?view=table&source=schedule` · `/invocations/$id→/?run=$id` ·
`/kanban→/?view=board` · `/playfield→/?view=stream&live=1` · `/shows→/?source=script` ·
`/shows/$topic→/?source=script&topic=$topic` · `/schedules[/$id]→/library?kind=schedule[&id=$id]` ·
`/playbooks[/$name]→/library?kind=script[&id=$name]` · `/agents[/$id]→/library?kind=agent[&id=$id]` ·
`/teams→/library?kind=team` · `/skills→/library?kind=skill` · `/plugins→/library?kind=plugin` ·
`/engines→/library?kind=engine` · `/projects→/system` · `/admin/health→/system` ·
`/admin/maintenance→/system#maintenance`.

E2e smoke tests key on routes and params (plus stable test ids), so the selector contract
survives visual iteration.

### Delivery

Four phases, each one PR that leaves the app coherent, codex-gated, visually examined in both
themes with screenshots in the PR body:

- **A — Shell + canvas v1**: three-surface rail, redirects, unified Run list with all three
  projections, slide-over (Overview/Output), attention chips, project lens. The existing shell
  PR is re-tasked in place: its API-path pinning suite, favicon, design tokens, and
  ProjectContext carry over; its five-group rail is replaced. Hosted deploy and its visual
  gate ride this phase.
- **B — Operations depth**: chain cards, SSE live tail, centralized staleness adopted
  everywhere, cancel/re-fire actions, command palette.
- **C — Library**: catalog + editors, YAML workflow editor with validate/plan-preview
  (additive endpoints land here), script naming, inviting zero states.
- **D — System + removal**: System page, legacy route files deleted (redirects stay),
  i18n sweep (EN-first), contrast/a11y audit in both themes.

## Consequences

**Positive**

- Sixteen destinations become three; the nav explains the product in one glance
  (define → run → observe).
- One Run model and one status oracle end the per-page semantics drift found in QA.
- URL params as the state carrier give stable deep links, stable e2e selectors, and make
  every view shareable.
- The operator surface ADR-0063 wanted arrives with zero backend schema risk (frontend
  aggregation; daemon stays additive).
- Windowing + virtualization are structural, not per-page fixes.

**Negative / risks**

- The slide-over must reach full parity with the old detail pages before their routes retire,
  or operators lose capability mid-migration (mitigation: redirects land only in phase D;
  parity is a phase-A/B exit criterion).
- Muscle memory and existing bookmarks break (mitigation: permanent redirects, palette).
- Dense-table power workflows could regress inside a filter-driven canvas (mitigation:
  `?view=table` is exactly the old table, kept at parity deliberately).
- A bigger phase A than the incremental plan; more review surface per PR (accepted: the
  founder explicitly chose radical over incremental).

**Capability-loss audit (self-refutation).** The strongest case against consolidation:
multi-window operators who today open runs and invocations side by side, and the playfield's
live spatial view. Both survive — every canvas state is a URL, so two browser windows with
different params replace two routes; `?view=stream&live=1` carries the playfield's live feed
role. The genuinely lost artifact is per-entity breadcrumbs, judged not worth a route each.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Keep v1 five-group nav (Home/Operations/Automations/Library/Admin, pages as children) | Founder verdict after live use: still too many pages and tabs, not a redesign; groups relabel the schema instead of replacing it |
| Two surfaces (System folded into Operations) | Destructive maintenance ops (vacuum, prune) need a deliberate, confirmable surface, not a popover |
| Four surfaces (separate Home) | A home page whose job is linking elsewhere is a page tax; attention chips on the canvas do the triage job in place |
| Restyle existing pages (status quo, better tables) | Fails the "db reader" critique; leaves N status oracles and N thin pages |
| ADR-0063 `work_items` backend model + Task Board | Requires new schema and services, violating the additive-only rider; frontend aggregation reaches the same operator surface now — 0063's UI layer is superseded, its schema stays dormant |
| TOML workflow definitions alongside YAML | Deferred; YAML-only for now, format seam kept cheap to widen |

## References

- Founder design directives, 2026-07-04 (complete redesign; project-oriented scoping;
  operation canvas; YAML workflow definitions; shows removal; play → script rename)
- Design-review rulings, 2026-06-11 (unified history timeline; canvas as command center;
  inviting zero states; contrast floors; generous editors)
- Live QA examination, 2026-07-04: status-derivation drift across pages, unvirtualized
  kanban, project-list pollution
- ADR-0026, ADR-0034, ADR-0063
