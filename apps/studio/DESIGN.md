# Lion Studio Desktop — Design Direction

**Status**: authoritative for the desktop app. The webapp was a proof of concept;
this document supersedes its design decisions wherever they conflict.
**Target**: macOS first. Dogfood-grade — built for daily operation of our own
agent fleets, not for a hypothetical enterprise buyer.

---

## 1. Thesis

Lion Studio is an **operator's cockpit for agent orchestration**, not an admin
dashboard. The POC organized the product around *nouns* (13 flat nav sections of
list/detail CRUD pages — runs, agents, playbooks, teams, shows, …). The desktop
app organizes around the **operator's loop**:

```
DESIGN a workflow → LAUNCH it → OBSERVE it live → INTERVENE at gates/failures → REVIEW outcomes
```

Everything on screen earns its place by serving a step of that loop. If a surface
is something you *file* rather than *fly*, it lives in a drawer, not in primary nav.

## 2. Who it's for (dogfood user stories)

1. **Fire and watch.** "I want to launch a playbook/flow, watch agents stream in
   real time, and know at a glance which step is doing what — without tailing a
   terminal." (today: `li o flow` + squinting at logs)
2. **Catch the gate.** "A critic/gate verdict landed. I want the app to pull my
   attention to it within seconds, show me the verdict + evidence, and let me
   pass/fix/re-fire from the same screen."
3. **Triage the morning.** "Open the app, see in under five seconds: what ran
   overnight, what failed, what's waiting on me." This is the home screen's only job.
4. **Design without YAML.** "Compose a multi-agent DAG on a canvas — steps,
   fanouts, gates — and the artifact is a playbook the engine actually runs.
   Then watch the run light up *on the same canvas* I designed."
5. **Reach for anything in two keystrokes.** "⌘K, type three letters, hit enter —
   run detail, agent profile, launch action. Navigation chrome is a fallback,
   not the primary path."

## 3. Information architecture — 5 spaces, not 13 sections

```
●●●  [traffic lights]  ┊ icon rail ┊            content                ┊ inspector
                       ┊          ┊                                    ┊ (contextual)
  ◉  Mission Control   ←  home: now + needs-you + recent
  ◆  Designer          ←  full-bleed graph canvas (the flagship)
  ▤  Library           ←  agents · playbooks · skills · plugins (one catalog)
  ≡  History           ←  runs · invocations · shows (one timeline)
  ⚙  System            ←  health · maintenance · schedules · settings
```

POC → desktop mapping: `runs/invocations/shows` merge into **History** (they are
one concept — "work that happened" — at three granularities). `agents/playbooks/
skills/plugins/teams` merge into **Library** (browsable catalog + detail drawer;
team = a saved composition). `kanban/playfield` fold into Mission Control and
Designer respectively. `admin/*` + `schedules` become **System**.

### Mission Control (home)

- **Attention queue** (top, always first): pending gates, failures, stalled runs.
  Zero-state when clear: a quiet "all clear" line — emptiness is a feature.
- **Live board**: each active run is a card — name, phase, agent activity ticker
  (last emission, one line, streaming), elapsed, mini DAG progress strip. Cards
  pulse subtly while streaming; click → full run view.
- **Recent**: last ~10 terminal runs as compact rows (verdict glyph, name,
  duration, cost) — not a paginated table.
- **Status footer**: backend port, scheduler state, DB size, version. One hairline row.

### Designer (flagship)

- The canvas IS the page. No chrome except: floating node palette (left),
  inspector panel (right, slides in on selection), top pill bar (playbook name,
  validate state, Run button).
- Node taxonomy v1: **step** (agent task), **fanout** (N parallel), **gate**
  (critic/approval), **join**, **input/args**. Edges carry dependency + optional
  condition.
- Inspector edits the selected node: instruction, agent/model, tools, artifacts
  expected. Form fields map 1:1 to playbook YAML — the YAML is always visible via
  a "source" toggle (two-way: canvas ⇄ YAML).
- **Run overlay**: launching from the canvas annotates nodes live — queued/
  running/done/failed states, token + duration badges, click a node mid-run to
  tail its stream in the inspector. Design-time and run-time are the same surface.
- ⌘K on canvas: insert node, jump to node, run, validate.

### Library

- Single catalog, filter chips for kind (agent / playbook / skill / plugin / team).
- Grid of cards with identity (name, kind glyph, model, last-used, run count).
- Detail opens as a **drawer over the catalog** (esc closes) — editing a profile
  never loses your place. Full-page editor only for the Designer.

### History

- One reverse-chron timeline, strong filters (kind, status, project, time range),
  grouped by day. Each entry: verdict glyph, name, what-changed summary, duration,
  cost. Click → detail view with the DAG replay, transcripts, artifacts.

### System

- Health, DB maintenance, schedules, app settings (theme, locale, backend
  connection). Deliberately boring.

## 4. Visual language

**Identity**: precision instrument, not SaaS dashboard. Dark-first (operators run
dark; light theme maintained but secondary).

- **Color**: near-black layered neutrals (base `#0C0D10`, raised `#13151A`,
  overlay `#1A1D24`), hairline borders (`#FFFFFF` @ 7–10%), high-contrast text
  (`#E8E6E1` primary / 55% secondary). **One accent: lion amber** (`#E8A33D`) —
  reserved for attention and primary actions, never decoration. Semantic status:
  running `#5BA8F5`, success `#4FB477`, gate-pending amber, failure `#E5604C`.
  Status is also always carried by a glyph, never color alone.
- **Type**: two-face system. UI chrome/labels in a clean grotesque (system
  `-apple-system`/Inter); **data in mono** (ids, names, instructions, streams,
  numbers — JetBrains Mono / SF Mono). The mono-for-data rule is the visual
  signature; all-mono (the POC) reads flat and tires on prose.
  Tabular numerals everywhere numbers align.
- **Density**: information-dense but layered — 8pt grid, generous *section*
  spacing with tight *row* spacing. Hairlines over boxes; depth from layered
  surface tones, not drop shadows.
- **Motion**: only where it carries state — streaming text appears character-
  cheap (no typewriter gimmick), node state transitions cross-fade ≤150ms,
  attention-queue entries slide in once. Nothing loops, nothing bounces.
  `prefers-reduced-motion` respected.

## 5. macOS-native treatment

- `titleBarStyle: overlay` — traffic lights inset into the icon rail; the whole
  top edge is draggable. No web-style fake titlebar.
- Sidebar/icon rail on window **vibrancy** (NSVisualEffectView via Tauri), content
  area opaque.
- **⌘K command palette** is the primary navigation (jump anywhere, launch
  anything). System shortcuts honored: ⌘1–5 spaces, ⌘W/⌘Q behave natively,
  ⌘, opens settings.
- **Native notifications** for gate-pending and run-terminal events (deep-link
  back into the app). Dock badge = attention-queue count.
- Menu bar with real menus (File → New Playbook, View → spaces, etc.).
- App icon: lion mark, amber on near-black, macOS squircle.

## 6. Interaction system

- **Drawers over pages** for anything that's a detour (library detail, node
  inspector, settings). Full navigation only when the destination is a workspace
  (run view, canvas).
- **Keyboard-first**: every list j/k navigable, enter to open, esc to back out.
  Every action in ⌘K. Mouse is never *required* except on the canvas.
- **Optimistic + streaming**: SSE drives everything live; no manual refresh
  buttons anywhere in the app.
- **Empty states teach**: each space's zero-state shows the one command that
  fills it (e.g. Designer empty state = "⌘K → New Playbook, or drop a YAML").

## 7. What dies from the POC

- The 13-section flat sidebar. The locale/theme toggles in primary nav (→ settings).
- Full-page CRUD forms as the primary editing modality (→ drawers + Designer).
- Table-first information display for live state (→ cards/board; tables remain
  for History where scanning is the job).
- Light-as-default. Same-page-reload locale switching.

## 8. Engineering notes (binding on implementation)

- Substrate: Vite + TanStack Router SPA (already migrated) inside Tauri 2;
  FastAPI backend spawned by the shell (`li studio --no-frontend`).
- The lib/api.ts client, types, and SSE plumbing are kept — this redesign is a
  presentation-layer rebuild on the same data layer.
- Design tokens live in one file (`src/theme.css` CSS custom properties +
  Tailwind config mapping). No hardcoded colors/spacing in components.
- Webapp parity is NOT a goal. The webapp keeps working during the transition
  (it serves the same SPA), but desktop UX decisions win every conflict.
