# ADR-0032: Navigation Reorganization

**Status**: Proposed
**Date**: 2026-05-23
**Related**: ADR-0031 (entity header pattern lands inside this nav structure)

## Context

Studio's top navigation today lists ten entity types as siblings at
the same visual weight (see `apps/studio/frontend/components/Shell.tsx:15`):

```text
Projects | Schedules | Playbooks | Agents | Plugins | Shows | Invocations | Runs | Teams | Admin
```

There is no `Dashboard` entry in the live nav — `/` renders the
dashboard, but it is reachable only via the project chip or by
typing the URL.

This mirrors the backend ontology accurately. But operationally, the
ten items are not equivalent — they answer different questions and are
visited at different cadences. The current flat structure gives
"Plugins" (browsed weekly) the same visual prominence as "Runs"
(opened many times per day). The cost is scan time on every page load,
plus the dashboard's invisibility.

### 1. Flat nav is not grouped by intent

The user thinks in workflows: *check the queue → open a failing run →
inspect the show that spawned it → maybe edit a playbook*. Not:
*navigate to the seventh item in the bar*. Grouping by operational
intent (work / library / admin) lets the operator predict where to
click without reading every label.

### 2. Schedules is mis-located if we keep flat nav

`Schedules` was added in ADR-0027 as a CRUD page for cron / interval /
github-poll triggers that fire agent runs. Schedules belong with
*work* (they create work) — not with *admin* (DB health, prune,
vacuum). The current flat nav doesn't force a decision; a grouped nav
does.

### 3. "Observability" looks tempting but is a junk drawer today

The ChatGPT review proposed an `Observability` top-level group for
logs, traces, metrics, artifacts. Studio has none of these as
first-class surfaces yet. Traces are not modelled; logs live on
individual runs; metrics are inline on the dashboard; artifacts are
already split between `runs/<id>/artifacts/` and the `artifacts`
table (ADR-0021). Creating an `Observability` group now would either
be empty or would silo features that already live (correctly) on
their parent entity. Defer.

### 4. Power users still need direct URLs

The current routes (`/runs`, `/shows`, `/schedules`, ...) are stable
and bookmarkable. A nav redesign must not change the underlying
routes — only how they are *grouped* in the menu.

## Decision

Replace the flat top nav with a four-group structure:

```text
Dashboard | Work ▾ | Library ▾ | Admin ▾
```

| Group | Contents |
|---|---|
| **Dashboard** | (single page, no submenu) |
| **Work** | Shows, Runs, Teams, Invocations, Schedules |
| **Library** | Playbooks, Agents, Plugins, Skills |
| **Admin** | Health, Maintenance |

`Observability` is *not* created. `Projects` is the project selector
(top-right or persistent global filter), not a nav item — see Section
4.

### 1. Group rationale

**Dashboard**: single most-visited surface. Stays at the top level.
This is where the Attention Queue (ADR-0030) lives.

**Work**: things currently happening or recently happened.

- *Shows* — orchestrated multi-play workflows
- *Runs* — individual agent runs / sessions
- *Teams* — collaborative multi-agent sessions
- *Invocations* — skill-level groupings of runs
- *Schedules* — definitions that produce future runs (ADR-0027). They
  belong here because their output *is* work. The contrast: pruning
  is admin; producing is work.

**Library**: capabilities, not instances. Browse-mode surfaces.

- *Playbooks* — reusable orchestration definitions
- *Agents* — reusable agent role profiles
- *Plugins* — marketplace plugins
- *Skills* — individual skill files from plugins

**Admin**: maintenance and system health. Lower frequency, higher
consequence.

- *Health* — DB health, phantom sessions, WAL pressure
- *Maintenance* — checkpoint, vacuum, prune, classify

### 2. Visual treatment

The four groups render as top-level menu items with hover-to-expand
submenus (or click-to-expand on touch devices). The current `tabs +
icon` layout is replaced with a thinner nav row:

```text
┌─ Dashboard ─ Work ▾ ─ Library ▾ ─ Admin ▾ ─────────── [project: lionagi ▾] ─ [≡]
│
│  (Page header / breadcrumb here, e.g. Work › Shows › sweep)
│
│  page content...
```

Submenu items reveal on hover (200ms delay) or click. The expanded
group is highlighted, the current page is bolded. The visual styling
otherwise matches the existing nav (same fonts, same colors, same
icons per item — borrowed from ADR-0025's color tokens for the
hover-active states).

Breadcrumbs above the page content show `Group › Section › Item`
(e.g., `Work › Runs › e288a6e2493f`), giving an always-visible "where
am I" that the flat nav currently lacks.

### 3. Route changes

Most direct URLs are unchanged; two need transitions:

| URL | Group | Old reachability | New nav | Migration |
|---|---|---|---|---|
| `/` | Dashboard | URL-only, no nav | Dashboard (now visible in nav) | — |
| `/projects` | (selector) | top nav | Project chip + popover; full list reachable via "View all projects" | — |
| `/shows` | Work | top nav | Work › Shows | — |
| `/shows/<topic>` | Work | (deep) | Work › Shows › `<topic>` | — |
| `/runs` | Work | top nav | Work › Runs | — |
| `/runs/<id>` | Work | (deep) | Work › Runs › `<id>` | — |
| `/teams` | Work | top nav | Work › Teams | — |
| `/invocations` | Work | top nav | Work › Invocations | — |
| `/schedules` | Work | top nav | Work › Schedules | — |
| `/playbooks` | Library | top nav | Library › Playbooks | — |
| `/agents` | Library | top nav | Library › Agents | — |
| `/plugins` | Library | top nav | Library › Plugins | — |
| `/skills` | Library | exists (`apps/studio/frontend/app/skills/page.tsx`) but not in top nav | Library › Skills | promote to nav |
| `/admin` | — | top nav lands at combined page | replaced | **301** → `/admin/health` for one release |
| `/admin/health` | Admin | (new) | Admin › Health | new route (extracted from `/admin`) |
| `/admin/maintenance` | Admin | (new) | Admin › Maintenance | new route (extracted from `/admin`) |

Two real route changes:

1. **`/admin` splits.** The combined page becomes two routes
   (`/admin/health` and `/admin/maintenance`). A 301 redirect from
   `/admin` → `/admin/health` lives in `next.config.mjs` for one
   release; the redirect can be removed in the release after.
2. **`/skills` becomes nav-promoted.** The page already exists, so
   this is not a new route — only a nav visibility change.

All other URLs are byte-stable. Bookmarks and CLI URL output continue
to work without changes.

### 4. Projects: chip, not nav item

`/projects` becomes the project list page (existing), but the *nav
slot* it currently occupies disappears. The active project is
represented as a chip in the top-right corner of the nav bar:

```text
[project: lionagi ▾]
```

Clicking the chip opens a project switcher (popover with project
list + "View all projects" link to `/projects`). The chosen project
becomes a global filter applied to list pages (`/runs`, `/shows`,
etc.).

This matches the operator mental model: project is *context*, not
*navigation*. You don't navigate *to* a project — you work within a
project and navigate to its work / library / admin.

### 5. Skills promoted to nav-level

`/skills` already exists as a page (`apps/studio/frontend/app/skills/page.tsx`)
but is not currently reachable from the top nav — users find skills via
`/plugins/<name>` detail pages. Promoting Skills to a sibling of
Plugins matches the user mental model (skills are the thing they
invoke; plugins are the delivery mechanism).

This ADR is a nav change, not a new page. The existing Skills list
keeps its current behaviour (read-only listing of `SKILL.md` files
from `~/.claude/plugins/` and the marketplace directory). CRUD is
owned by the marketplace plugin tooling, not Studio.

### 6. Admin: separate Health from Maintenance

The Admin page today bundles DB health (size, WAL, phantom sessions)
with maintenance actions (prune, checkpoint, vacuum) on one screen.
Split:

- `/admin/health` — read-only view of current system state. Renders
  the DB health strip and a read-only phantom sessions table (no
  checkboxes, no action buttons). Includes a "Manage in Maintenance →"
  link to the mutating surface.
- `/admin/maintenance` — actions that mutate state (`prune selected`,
  `prune all phantom`), each gated by a `window.confirm()` dialog
  per the ADR-0031 `EntityAction.requires_confirm` pattern.

The split clarifies which surface is "look" vs "act". Today the bundle
makes routine inspection feel risky (the prune button is right there).

**Scope clarification (v1):** Only `prune` lands as a maintenance
action in v1 because the backend admin router currently exposes only
`POST /api/admin/prune`. `checkpoint` and `vacuum` are deferred to a
follow-up that first adds `POST /api/admin/checkpoint` and
`POST /api/admin/vacuum` endpoints (today these operations exist as
`li state checkpoint` / `li state vacuum` CLI subcommands, not as
HTTP endpoints). When those endpoints land, this ADR is amended in
place and the corresponding buttons join the Maintenance page with
the same confirmation pattern.

### 7. Mobile / narrow-viewport behavior

The nav collapses into a hamburger menu (`≡`) on viewports under
768px. The four groups become a vertical accordion in the drawer.
This matches the existing dashboard responsive breakpoint.

This is not a fully-designed mobile experience — Studio is
desktop-first (ADR-0008 §"Studio v1 scope"). The hamburger ensures
nothing is *unreachable* on a narrow viewport, not that the
experience is delightful.

### 8. File map

Modified files:

```text
apps/studio/frontend/components/nav/TopNav.tsx          # group structure
apps/studio/frontend/components/nav/NavGroup.tsx        # new: group with submenu
apps/studio/frontend/components/nav/ProjectChip.tsx     # new: top-right chip
apps/studio/frontend/components/nav/Breadcrumb.tsx      # new: above page content
apps/studio/frontend/components/nav/types.ts            # NavGroup, NavItem types
apps/studio/frontend/components/Shell.tsx               # consume new nav structure
apps/studio/frontend/app/layout.tsx                     # nav composition
apps/studio/frontend/app/admin/page.tsx                 # delete: replaced by 301
apps/studio/frontend/app/admin/health/page.tsx          # new: extracted health view
apps/studio/frontend/app/admin/maintenance/page.tsx     # new: extracted maintenance view
apps/studio/frontend/app/skills/page.tsx                # existing — no code change,
                                                        # only nav-level promotion
apps/studio/frontend/next.config.mjs                    # add 301 redirect:
                                                        #   /admin -> /admin/health
                                                        # (remove the redirect one
                                                        #  release later)
```

No backend changes.

### 9. Effort

One PR, 1-2 days. The Skills page is the only net-new surface; it's a
file-listing component that reads from the marketplace directory and
renders SKILL.md frontmatter — similar to the existing Playbooks page.

## Consequences

**Positive**

- Operators scan a 4-item top bar instead of an 11-item bar. The
  cognitive scan cost drops on every page navigation.

- Schedules sits with its operational siblings (Runs, Shows) instead
  of among system maintenance.

- Admin splits cleanly into "look" (Health) and "act" (Maintenance),
  reducing accidental-click risk on the maintenance actions.

- The project chip pattern matches industry convention (GitHub,
  Linear, Stripe) and frees the top nav for *what to do*, not *whose
  project you're in*.

- Skills become discoverable in the nav for the first time (the page
  itself already exists).

- No backend changes; entirely a frontend reshuffle.

- Most direct URLs are byte-stable. The only exception is `/admin`,
  which 301-redirects to `/admin/health` for one release before being
  removed. Bookmarks and CLI URL output continue to work.

**Negative**

- Hover-expand submenus are a new interaction on Studio. Need to
  ensure click-to-expand also works (accessibility, touch devices).

- The breadcrumb adds a row of vertical pixels above every page.
  Roughly 24px taller content area on every page. Acceptable.

- The four-group taxonomy is opinionated. Some users might want
  Schedules in Admin or Invocations in its own slot. The grouping is
  reversible — promote / demote a section if usage data shows it's
  wrong.

- Splitting Admin requires URL changes for `/admin` → `/admin/health`
  - `/admin/maintenance`. Old `/admin` URL needs a redirect to
  `/admin/health` for one release.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Keep the flat nav | Maximum object visibility, worst scan cost. The whole reason this ADR exists. |
| Add `Observability` as a fifth group | Junk drawer until traces/logs/events are first-class. Better to wait. |
| Put Schedules under Admin | Schedules produce *work*; Admin should stay focused on system maintenance. Schedules are operationally closer to Shows than to Vacuum. |
| Keep `Projects` as a nav item | Project is context, not navigation. The chip pattern (top-right) matches user mental model and frees the top bar. |
| Five groups: Dashboard / Work / Library / Observability / Admin | Premature; see above. |
| Role-specific nav (Operator / Author / Admin views) | Studio is single-user (ADR-0008). All roles inhabit one person. Defer. |
| Hide low-traffic pages entirely | Power users need direct access. Hidden ≠ removed; hiding makes them harder to reach without removing the surface area. |
| Sidebar nav instead of top nav | Sidebar takes 200px of horizontal real estate permanently. The four-group top nav fits in a 56px row and leaves the page width unconstrained. |
| Drop submenus, use one click to a group landing page | Two-click navigation to reach any list. Submenus give one-click access while preserving the group as a meaningful container (the landing page exists too — `/work` could show the work overview). |

## Non-Goals

- **No command palette in v1.** Keyboard-driven navigation (`Cmd+K`)
  is a separate, larger design. Defer.

- **No saved views / filtering UI.** "My failed runs", "schedules I
  own", etc. — deferred until the basic structure is in place.

- **No customizable nav.** Operators cannot rearrange groups or pin
  items in v1.

- **No notification badges on nav items.** Attention Queue surfaces
  counts; nav stays clean.

- **No tabs within the page header above the breadcrumb.** Existing
  page-level tabs (e.g., `Summary / Timeline / Artifacts` on Run
  detail) stay intact below the breadcrumb.

- **No multi-project filtering.** The project chip selects exactly
  one project at a time (matches existing `/api/...?project=X`
  filter behavior).

- **No mobile-first redesign.** Mobile gets a working hamburger; the
  desktop layout is the primary target.

## References

- [ADR-0008](ADR-0008-studio-v1-scope.md) — Studio is single-user, desktop-first.
- [ADR-0026](ADR-0026-project-detection.md) — Project is per-session context; the chip pattern matches this model.
- [ADR-0027](ADR-0027-scheduled-runs.md) — Schedules feature being relocated under Work.
- [ADR-0030](ADR-0030-attention-queue.md) — Attention Queue lives at the top of the Dashboard.
- [ADR-0031](ADR-0031-entity-header-pattern.md) — Entity headers render inside the page body, below the breadcrumb.
- ChatGPT frontend design review (external) — proposed the 4-group structure; this ADR adopts it with the Observability deferral, project-as-chip, and Admin split.

### Prior art

- **GitHub navigation** (`Code / Issues / Pull requests / Actions /
  Projects / Wiki / Security / Insights / Settings`) — nine top-level
  items, but visually grouped by frequency. Studio's four-group
  approach achieves the same end with less width.

- **Linear navigation** — left sidebar with `Inbox / My Issues /
  Active / Backlog` (work) and `Views / Projects / Members` (library).
  Same intent split.

- **Stripe Dashboard** — `Home / Payments / Customers / Products /
  Reports / More` with a workspace switcher chip in the top-right.
  Direct precedent for the project chip pattern.
