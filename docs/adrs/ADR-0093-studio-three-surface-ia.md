# ADR-0093: Studio Three-Surface Information Architecture

**Status**: Proposed (Amendment 1 applied — see below)
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
workflow definitions serialize to YAML canonically, with TOML accepted and produced at the
import/export boundary.

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

Two elements recovered from the desktop cockpit line (tag `desktop-v0.1.0-rc1`, never merged)
are first-class in this architecture:

- **The Leo operator panel** (founder-required): a dockable right-side panel present on every
  surface — an operator chat that also *drives the UI* (its command channel can navigate,
  filter, and open detail from conversation). The panel is a **persistent, scrollable
  session**, not a transient tail: the full conversation history loads and scrolls, and
  reopening Studio resumes the same session where it left off. Resurrected from the cockpit's
  panel + UI-command components and their daemon chat/signals services; the shell reserves the
  right dock from phase A, the live panel lands with its backend in phase B.
- **The cockpit's visual language** is the style anchor for all phases: the dark canvas
  chrome, signal chips, monospace accents, and status bar of that build, harmonized with the
  current token system and shipped in both themes under the established contrast floors.

The cockpit's engine-blueprint designer is *not* resurrected (founder: drop the engine
canvas). Its canvas foundation — port-based nodes, chain-as-spine layout, draft/topology
utilities — is reused for the **workflow canvas** in Library instead.

### Operations — the canvas

One continuous surface over a single **unified Run model** aggregated in the frontend from
existing endpoints (sessions, invocations + schedule-run failure fields, schedule runs, plays,
engine runs):

```text
Run { id, source: agent|schedule|script|flow, status, project, started_at, updated_at,
      duration, reason?{code, summary, error_detail, exit_code},
      chain?{parent, children[]}, refs{session_id?, invocation_id?, schedule_id?, topic?} }
```

- **The invocation noun is removed from the product.** A scheduler firing is simply a Run
  with `source=schedule`; there is no "invocation" kind, label, filter value, column, or
  detail tab anywhere in the UI (founder ruling: the concept is odd). The daemon's invocation
  records remain internal plumbing the aggregator consumes — `refs.invocation_id` exists for
  joining, and is never presented.
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
- **Any run is resumable, at any time** (founder-required). The slide-over carries a
  **Resume** action for every run with an underlying session: type a follow-up instruction
  and the daemon resumes that session in place (additive endpoint wrapping the existing
  resume machinery) — terminal status is no barrier, and the resumed turn appears as new
  activity on the canvas. Copying the CLI command stays as the escape hatch; the UI action
  lands in phase B alongside the Leo panel, sharing its daemon surface and Leo's
  authn/authz review.
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
live in Operations (`/?source=schedule&id=…` one click away). **Workflows** are a new kind
with two synchronized editors over one definition: a CodeMirror text editor with schema
validation and a plan preview before save, and a **workflow canvas** — a visual DAG editor
built on the recovered cockpit canvas foundation — as the primary authoring surface.
Definitions serialize to YAML canonically and **import/export as YAML or TOML** at the file
boundary; both are backed by additive daemon endpoints (`POST /api/workflows/validate`,
plan-preview). Every zero state invites creation with a primary CTA.

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

Every retired path redirects: `/runs→/?view=table` · `/runs/$id→/?run=$id` ·
`/invocations→/?view=table&source=schedule` (redirect only; the noun does not reappear) ·
`/invocations/$id→/?run=$id` ·
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

- **A — Shell + canvas v1**: three-surface rail, unified Run list with all three
  projections, slide-over (Overview/Output), attention chips, project lens. Redirects land
  for **list pages only** (`/runs`, `/invocations`, `/kanban`, `/playfield`, `/shows`), whose
  projections have parity from day one; **detail routes** (`/runs/$id`, `/invocations/$id`,
  `/shows/$topic`) keep working unchanged. The existing shell PR is re-tasked in place: its
  API-path pinning suite, favicon, design tokens, and ProjectContext carry over; its
  five-group rail is replaced. The cockpit visual language lands here (tokens, chrome,
  status bar) and the right dock is reserved for the Leo panel. Hosted deploy and its visual
  gate ride this phase.
- **B — Operations depth**: command palette (sequenced early in the phase), the **Leo
  operator panel** live in the right dock (chat + UI-drive commands, scrollable persistent
  session, daemon endpoints resurrected from the cockpit line), chain cards, SSE live tail,
  centralized staleness adopted everywhere, cancel/re-fire/**resume** actions. The
  slide-over reaches full parity
  (Messages · Artifacts · Chain · Raw), and only then do the detail-route redirects land.
- **C — Library**: catalog + editors, workflow canvas + text editor with validate/plan-preview
  and YAML/TOML import/export (additive endpoints land here), script naming, inviting zero
  states.
- **D — System + removal**: System page, retired route files deleted (redirects stay),
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
  or operators lose capability mid-migration (mitigation: redirects are staged — phase A
  redirects only list pages whose projections already have parity; detail routes redirect in
  phase B once the slide-over reaches full tab parity; the retired route files are deleted in
  phase D with all redirects staying).
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
| YAML-only workflow definitions (no TOML) | Founder wants both at the file boundary; YAML stays the canonical on-disk form, TOML handled by import/export conversion so the editor and daemon see one format |
| Resurrect the cockpit's engine-blueprint designer | Founder dropped it; its canvas foundation is reused for the workflow canvas, which has a concrete artifact (a runnable definition) rather than a speculative blueprint |

## References

- Founder design directives, 2026-07-04 (complete redesign; project-oriented scoping;
  operation canvas; workflow definitions; shows removal; play → script rename; adopt the
  desktop-cockpit visual style; Leo operator panel required; workflow canvas with YAML/TOML
  import/export; engine canvas dropped)
- Design-review rulings, 2026-06-11 (unified history timeline; canvas as command center;
  inviting zero states; contrast floors; generous editors)
- Live QA examination, 2026-07-04: status-derivation drift across pages, unvirtualized
  kanban, project-list pollution
- ADR-0026, ADR-0034, ADR-0063

---

## Amendment 1 (2026-07-04): the cockpit shell is the shipped baseline

### What changed

During phase-A delivery the founder live-steered the implementation against the running app
and ruled that the desktop cockpit line (tag `desktop-v0.1.0-rc1`) was the better product:
its frontend was restored wholesale onto the phase-A branch and accepted as the baseline for
all further Studio work. The original Decision's three-route contract was therefore never the
shipped surface. This amendment records the shipped architecture, marks which parts of the
original Decision it supersedes, and re-anchors the delivery phases. The original text above
is preserved unedited as the design record.

### Shipped information architecture

The public surface is a six-space cockpit rail with keyboard shortcuts, plus one sub-view:

| Space | Route | Shortcut | Carries |
|-------|-------|----------|---------|
| Mission Control | `/` | ⌘1 | attention header + live board; **Fleet** (`/fleet`) is a tab within this space (board projection of active work; the old kanban redirects here) |
| Designer | `/designer` | ⌘2 | canvas authoring surface (workflow canvas home) |
| Library | `/library` | ⌘3 | definitions catalog — scripts/workflows, skills, plugins, engines as tabs |
| History | `/history` | ⌘4 | unified execution timeline (runs, schedule firings, script runs as tabs) |
| Schedules | `/schedules` | ⌘5 | schedule definitions and firing status |
| System | `/system` | ⌘6 | health, maintenance, project inventory |

Why the shipped shape departs from three surfaces:

- **Operations splits into Mission Control and History.** The founder's live verdict
  preferred a dedicated attention surface (what needs me now) separate from the record
  (what happened). The unified-timeline ruling survives inside History; the "no separate
  home" rejection in Alternatives is reversed by acceptance — Mission Control is that home,
  and the founder chose it.
- **Designer is a first-class space.** The canvas is the command-and-control center
  (2026-06-11 ruling) and gets its own room rather than living as a Library kind.
- **Schedules keep a dedicated space.** Definitions and firing health are operated together
  too often to split across Library and Operations.

### Superseded by acceptance

- The three-route contract and its param grammar (§Route contract). The **redirect policy
  survives** and is honored by the shipped shell: `/runs → /history?tab=run` ·
  `/invocations → /history` · `/shows → /history` · `/kanban → /fleet` ·
  `/playbooks → /library?tab=workflow` · `/skills|/plugins|/engines → /library` (kind tabs) ·
  `/admin → /system`.
- Slide-over as the sole detail presentation. The cockpit uses master-detail panes in Fleet
  and History; detail-state-in-URL remains the rule, the presentation is the pane.
- The phase-A scope as written (three-surface rail + canvas projections). Phase A as shipped
  is the cockpit restore itself.

### Still binding, unchanged

- **Unified Run model and one status oracle** (`deriveRunStatus` semantics: dead-process
  demotion to Stale, spent one-shots as Expired). The cockpit inherits the per-page
  status-drift debt this ADR set out to retire; consolidation lands in phase B.
- **The invocation noun stays removed** from user-facing surfaces; `/invocations` is
  redirect-only and the concept never reappears in labels, filters, or tabs.
- **Windowing and virtualization as structural defaults.** Session detail now loads a tail
  window (default 200 messages) with load-older paging and per-branch totals; long feeds
  virtualize.
- **Leo operator panel is phase B**, gated on an explicit authn/authz story reviewed before
  implementation, together with the resume-run endpoints that share its daemon surface.
- **Workflow definitions**: YAML canonical, TOML at the import/export boundary, additive
  validate/plan-preview endpoints.
- **Engineering riders**: daemon changes additive-only; e2e keyed on routes, params, and
  stable test ids against a seeded temp db; both themes ship with the established contrast
  floors.

### Re-anchored delivery

- **A (shipped)**: cockpit shell restore, six-space rail with shortcuts, legacy redirects,
  session-detail tail-window pagination; e2e selectors reworked against the cockpit shell as
  part of the phase-A merge gate.
- **B**: Leo panel + resume actions (after the authn/authz review), SSE live tail, chain
  cards, single status oracle adopted across all spaces.
- **C**: workflow canvas in Designer with synchronized text/visual editors,
  validate/plan-preview and YAML/TOML endpoints, Library catalog depth, inviting zero states.
- **D**: retired route files deleted (redirects stay), i18n sweep, contrast/a11y audit in
  both themes.
