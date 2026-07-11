# ADR-0080: Studio six-space cockpit information architecture

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: studio
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0015, v0-0031, v0-0032, v0-0093

## Context

Studio accumulated one route per backend entity. A later design compressed those routes
into three broad surfaces, but delivery acceptance restored a six-space cockpit organized
around operator intent. The amendment to v0-0093 is the last accepted IA decision and
explicitly supersedes its original three-route contract. This ADR carries that accepted
baseline forward while documenting that the checked-in shell has not restored every part.

The decision answers six concrete problems.

**P1 — Storage nouns are not an operator mental model.** Runs, invocations, shows,
schedules, playbooks, skills, plugins, engines, projects, and maintenance each acquired
routes even when they answered the same operator question. Navigation made the operator
reassemble “what needs attention,” “what ran,” and “what can run” from tables.

**P2 — Live attention and durable history are different work modes.** Compressing both
into one Operations surface made current intervention and retrospective inspection compete
for layout and filters. Acceptance therefore split Mission Control from History while
keeping one status and execution model underneath.

**P3 — Canvas authoring needs a first-class room.** Treating workflow design as a small
Library detail understates its spatial and editing needs. Designer is a peer space, not a
modal or catalog subtype.

**P4 — Schedule definition and firing health are operated together.** Folding schedules
into Library separates configuration from whether it will fire, while folding them into
History treats a control object as a past event. Acceptance kept one dedicated Schedules
space.

**P5 — Deep links outlive route implementations.** Existing URLs for runs, invocations,
shows, kanban, playbooks, and admin remain inbound contracts. Removing pages must not turn
bookmarks into ambiguous 404s or preserve an entire second IA.

**P6 — The checked-in rail and the accepted baseline disagree.** `IconRail.tsx` currently
shows Mission Control, Library, Schedules, and System; shortcuts map 1–3 and 5; Fleet is a
separate `/fleet` route/tab; no `/designer` or `/history` route file exists. Treating this
intermediate state as a third accepted taxonomy would reintroduce the ambiguity this ADR
eliminates.

| Concern | Decision |
|---|---|
| Top-level taxonomy | D1: Use exactly six peer cockpit spaces with shortcuts 1 through 6. |
| Execution ownership | D2: Separate live Mission Control/Fleet from canonical History while sharing execution semantics. |
| Definition ownership | D3: Give Designer, Library, and Schedules distinct responsibilities. |
| Project scope and detail | D4: Apply one project lens and keep selection/filter state URL-addressable. |
| Compatibility | D5: Redirect retired entity URLs into one owning cockpit space and remove duplicate implementations after parity. |
| Source drift | D6: Treat the current four-destination rail as incomplete restoration, not a competing decision. |

Out of scope:

- The exact unified execution/adaptation model is specified by ADR-0081.
- The daemon route, auth, and SSE boundary is ADR-0076.
- Component library and frontend deployment choices are ADR-0079.
- Leo is not declared shipped by this ADR. Its persistent command protocol is ADR-0083.
- Backend database nouns may remain for storage and joins; this ADR governs public
  navigation and product labels.

## Decision

### D1 — Exactly six peer cockpit spaces

The accepted public registry is:

```typescript
type CockpitSpaceId =
  | "mission"
  | "designer"
  | "library"
  | "history"
  | "schedules"
  | "system";

interface CockpitSpace {
  id: CockpitSpaceId;
  route: "/" | "/designer" | "/library" | "/history" | "/schedules" | "/system";
  shortcut: 1 | 2 | 3 | 4 | 5 | 6;
  scope: "project" | "machine";
}
```

Its values are normative:

| Space | Route | Shortcut | Scope | Responsibility |
|---|---|---:|---|---|
| Mission Control | `/` | ⌘/Ctrl-1 | project | Current attention, live board, and Fleet sub-view |
| Designer | `/designer` | ⌘/Ctrl-2 | project | Workflow and execution-plan canvas authoring |
| Library | `/library` | ⌘/Ctrl-3 | project/global catalog | Definitions and capability catalog |
| History | `/history` | ⌘/Ctrl-4 | project | Unified execution timeline and detail |
| Schedules | `/schedules` | ⌘/Ctrl-5 | project | Schedule definitions and firing health |
| System | `/system` | ⌘/Ctrl-6 | machine | Health, maintenance, and project inventory |

Exact semantics:

- The rail contains six peer navigation targets. Fleet is not a seventh peer.
- The shortcut requires Meta or Control plus the digit; it navigates to the registry route
  and prevents the browser default.
- The visible label, command-palette destination, shortcut, route, and operator-command
  vocabulary derive from one typed registry. Copying string sets is implementation drift.
- System remains visually allowed in a lower rail cluster, but its location does not change
  peer status or shortcut 6.
- Unknown space ids are rejected. They do not fall back to Mission Control.
- Missing route implementations are defects against this accepted registry, not permission
  to omit registry entries.

Why this way: six spaces preserve the accepted separation between attention, design,
catalog, record, automation, and machine maintenance. The taxonomy describes operator
intent rather than StateDB tables.

### D2 — Mission Control/Fleet and History have distinct roles

Mission Control answers “what needs attention now.” History answers “what happened and
why.” Both consume the execution workspace contract in ADR-0081.

The route roles are:

```text
/                  Mission Control default attention view
/fleet             Mission Control sub-view for live operational projection
/history            canonical execution record and master-detail owner
```

Exact semantics:

- Fleet is selectable as a Mission Control tab/sub-view at `/fleet`; its separate path is
  addressability, not peer-space status.
- Mission Control may show recent or failed executions as attention cards, but full
  historical filtering and detail ownership live in History.
- History unifies sessions/runs, schedule firings, script/show executions, and their
  internal invocation joins. These backend nouns do not regain peer rail pages.
- “Invocation” is not a user-facing kind, tab, filter, or rail label. It remains an internal
  correlation record.
- One status oracle supplies Mission Control, Fleet, and History. A dead process cannot
  appear healthy merely because its database row says `running`; spent one-shot schedules
  cannot appear active.
- Detail presentation is master-detail and does not unload the owning list. The selection is
  reconstructible from the URL.
- Empty live work produces an inviting zero state, not a fallback navigation into History.

This separation reverses the earlier three-route design's rejection of a home/attention
surface. The later acceptance is controlling under the last-in-time canon.

### D3 — Designer, Library, and Schedules are separate workspaces

The three definition/control spaces have non-overlapping primary responsibilities:

```text
Designer   author and validate visual workflow/execution-plan graphs
Library    browse and inspect agents, workflows, playbooks, skills, plugins, engines
Schedules  configure cadence/action and inspect next/last firing health
```

Exact semantics:

- Designer owns canvas authoring. Library may link to a workflow definition or summary but
  does not host a second full canvas implementation.
- Library owns the capability catalog. Current Library kinds may include agent, workflow,
  playbook, skill, plugin, and engine; adding a kind does not add a peer rail space.
- Schedules owns both definition mutation and health because operators use them together.
  Schedule firing execution detail links into History rather than duplicating History.
- Public terminology remains “playbook” where the checked-in product uses it (ADR-0079);
  this ADR does not revive invocation as a product noun.
- System owns maintenance and project inventory. Destructive operations remain separated
  from ordinary workspaces and require their own confirmation contract.

### D4 — One project lens and URL-addressable detail

The global lens applies to Mission Control, Designer, Library, History, and Schedules where
the underlying object can be project-scoped. System is machine-scoped.

The minimum URL-state contract is:

```typescript
interface CockpitLocationState {
  project?: string;
  selected?: string;
  status?: string | string[];
  q?: string;
  view?: string;
  cursor?: string;
}
```

Each space may publish additional typed search fields, but `project` and detail selection
must mean one thing across scoped spaces.

Exact semantics:

- Changing project re-runs the current space query. If selected detail belongs outside the
  new project, selection is explicitly cleared with a replace-navigation; it is never left
  showing cross-project data under the new lens.
- A deep link restores space, project, filters, and selected entity without dependence on
  prior in-memory React state.
- Empty or invalid search values are normalized by the route validator.
- System ignores project scope rather than showing a filtered machine-health view.
- Project-global Library objects remain visible with an explicit global label; they are not
  silently assigned to the selected project.
- Changing a presentation pane does not discard filters or selection unless its route
  contract says that selection is incompatible.

### D5 — Redirect-only compatibility with one implementation owner

Accepted compatibility destinations are:

```text
/runs[/<id>]          → /history with run selection
/invocations[/<id>]   → /history without exposing the invocation noun
/shows[/<topic>]      → /history with script/show selection
/kanban               → /fleet
/playbooks[...]       → /library with playbook/workflow selection
/skills|/plugins|/engines → /library with the matching kind
/admin[...]           → /system
```

The exact query key can evolve through an explicit route migration, but the owning space
and the absence of duplicate feature logic are invariant.

Exact semantics:

- A retired URL validates and sanitizes its incoming search before redirecting.
- Detail redirects preserve stable ids and project/filter context when representable.
- Redirect errors show an actionable route error; they do not silently land on an unrelated
  home view.
- Redirect shims remain permanent compatibility code after old page components are deleted.
- Old components are removed only after target-space parity covers the old task and a deep-
  link test passes.
- Adding new behavior to a retired route is prohibited; behavior belongs to its cockpit
  owner.

### D6 — Current shell differences are a delta, not a new taxonomy

The checked-in source contract at the date of this ADR is:

```typescript
// apps/studio/frontend/src/components/shell/IconRail.tsx
const SPACES = [
  { id: "home", href: "/", key: 1 },
  { id: "library", href: "/library", key: 2 },
  { id: "schedules", href: "/schedules", key: 3 },
];
const SYSTEM_SPACE = { id: "system", href: "/system", key: 5 };

// key handler accepts only 1..5, maps 5 to System, otherwise SPACES[n - 1]
```

The command registry adds Fleet as a separate navigation command. `routes/index.tsx`
renders Mission Control with Overview and Fleet tabs; `routes/fleet.tsx` implements the
Fleet path. There are no `routes/designer*` or `routes/history*` files.

The exact drift is therefore:

| Accepted contract | Checked-in source | Interpretation |
|---|---|---|
| six entries, shortcuts 1–6 | four visible entries; keys 1,2,3,5 | incomplete restoration |
| Designer `/designer` | absent | missing accepted space |
| History `/history` | absent | missing canonical record owner |
| Library key 3 | key 2 | shortcut drift |
| Schedules key 5 | key 3 | shortcut drift |
| System key 6 | key 5 | shortcut drift |
| Fleet Mission sub-view | `/fleet` tab plus separate command | partially aligned, not a seventh accepted space |

This ADR intentionally records both the accepted decision and present code. It does not
blur the gap into an aspirational redesign: the accepted IA is retrospective corpus truth,
and the table is the current-vs-ideal delta requiring implementation restoration.

## Consequences

- The rail communicates one stable model: attend, design, browse, inspect history, operate
  schedules, and maintain the machine.
- Mission Control and History can evolve independently while sharing status, identity,
  project, and detail adapters.
- Designer receives enough space for canvas work; Schedules keeps configuration and firing
  health together.
- Route consolidation has a parity cost. A redirect cannot land until the target owns the
  old task, and old implementation files cannot become permanent shadows.
- Current source does not meet D1. Contributors must consult the drift table rather than
  assuming the four-entry rail is canonical.
- Reversing D1 would affect rail, shortcuts, commands, redirects, project lens, Leo
  vocabulary, and tests; its reversal cost is high and requires a new IA ADR.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Restore Designer and History as first-class rail entries and implement the complete ⌘/Ctrl-1 through ⌘/Ctrl-6 shortcut contract with keyboard and screen-reader tests. | M | (filled at issue-open time) |
| 2 | Make Fleet a Mission Control sub-view and History the sole execution-history owner; retain old entity URLs as redirect-only adapters after feature-parity tests pass. | L | (filled at issue-open time) |
| 3 | Derive the rail, command palette, redirect destinations, and Leo navigation vocabulary from one typed space registry so no consumer can target a nonexistent cockpit space. | M | (filled at issue-open time) |
| 4 | Add the global project lens to every scoped space and prove that changing project preserves or intentionally clears URL-addressable selection and filters. | M | (filled at issue-open time) |
| 5 | Adopt one run-status oracle and windowed or virtualized history rendering across Mission Control, Fleet, and History; dead processes and spent one-shot schedules must not appear healthy or active. | L | (filled at issue-open time) |

## Alternatives considered

### Route per backend entity

This makes every API noun directly discoverable and lets each team build a small page. It
lost because operators must correlate multiple pages to understand one execution and the
rail becomes a database glossary. Backend nouns remain addressable inside owning spaces.

### Original three-surface design

Operations, Library, and System produced a very small rail and one unified operations
canvas. It bought simplicity and strong consolidation. It lost at later acceptance because
live attention and historical record needed distinct rooms, canvas authoring needed a peer
Designer, and schedules were operated too frequently to bury in Library. The last-in-time
canon makes the six-space amendment controlling.

### Four-destination checked-in rail as the new standard

Mission Control, Library, Schedules, and System are coherent and already implemented;
Fleet provides live depth. Adopting them would minimize work. It lost because it silently
abandons the accepted Designer and History responsibilities and assigns shortcuts that
conflict with the accepted registry. Code drift is evidence of incomplete work, not a
decision record.

### Fleet as a seventh peer space

A peer Fleet entry would make active orchestration immediately reachable and match its
standalone URL. It lost because Fleet is one projection of Mission Control's “what needs
attention now” job. A seventh peer would split the same question and weaken the six-space
mental model.

### Designer as a Library kind

This would keep all definitions in one place and reduce the rail. It lost because a canvas,
text synchronization, validation, and plan preview need an authoring workspace materially
different from catalog browsing.

### Invocation as a History tab

This would map directly to the current database and preserve existing deep links. It lost
because invocation is correlation plumbing: users investigate executions, not the internal
aggregation row. Redirect resolution can use invocation ids without presenting the noun.

### Slide-over as the sole detail presentation

The earlier design kept one canvas visible behind a right slide-over and made every state
shareable. The cockpit accepted master-detail panes instead. URL-addressability survives;
the specific slide-over constraint does not.
