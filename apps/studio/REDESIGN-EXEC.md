# Cockpit Redesign — Execution Contract

Working doc for the presentation-layer rebuild. **Authority: DESIGN-SYSTEM.md**
(canonical binding rules + exact values) > DESIGN.md (IA, visual language) >
DESIGN-CANVAS.md (Designer/canvas grammar) > this file, which adds the
file-level implementation contract. On any conflict, DESIGN-SYSTEM.md wins.
Branch: `feat/studio-cockpit-redesign`.
Worktree: `/Users/lion/projects/lionagi-redesign`.

Ocean's verbatim bar (2026-06-11): "smooth as fuck, super optimized for speed,
looks elegant and sleek, user friendly"; engine building must be a **graphic
node-composition interface with individual node configuration**, including
**signal emission configuration and signal observation patterns** — not a modal.
"You can redesign entire thing from scratch yourself."

## Constraints (binding)

- lib/api.ts, lib/sse helpers, messages/ i18n plumbing are KEPT (DESIGN.md §8).
  This is a presentation rebuild on the same data layer.
- Design tokens in ONE file: `src/theme.css` (CSS custom properties); Tailwind
  maps tokens, components never hardcode color/spacing.
- Dark is the default theme. Light maintained, secondary (`[data-theme=light]`).
- Old POC routes stay mounted during transition (webapp keeps working) but are
  REMOVED from primary nav — reachable via ⌘K only. They die at the end.
- No new heavy deps without strong cause. reactflow@11 (already present) powers
  the canvas. ⌘K palette is hand-rolled (no cmdk dep).
- Gates per wave: `npm run build` (tsc + vite), `npm run lint`, `npm test` all
  green. CI uses npm (`package-lock.json`) — never introduce a second lockfile.
- No internal audit IDs / PR numbers / reviewer mentions in committed source.

## Wave 0 — Foundation (blocking; everything depends on it)

### src/theme.css — exact token values (DESIGN.md §4)

```css
:root { /* dark default */
  --surface-base: #0C0D10; --surface-raised: #13151A; --surface-overlay: #1A1D24;
  --edge-hairline: rgba(255,255,255,0.10); --edge-strong: rgba(255,255,255,0.18);
  --content-primary: #E8E6E1; --content-secondary: rgba(232,230,225,0.70);
  --content-muted: rgba(232,230,225,0.50);
  /* secondary/muted raised from spec'd 0.55/0.35 — Ocean live verdict
     2026-06-11: "too dark, not readable" on dense tables. Floor is binding. */
  --accent: #E8A33D;            /* lion amber — attention + primary actions ONLY */
  --status-running: #5BA8F5; --status-success: #4FB477;
  --status-pending: #E8A33D; --status-failure: #E5604C;
  --font-ui: -apple-system, BlinkMacSystemFont, Inter, sans-serif;
  --font-data: "JetBrains Mono", "SF Mono", ui-monospace, monospace;
}
```

Light block overrides surfaces/content only; status+accent stay. 8pt grid via
Tailwind spacing. Hairlines over boxes; depth via surface layers, no shadows.
Mono-for-data rule: ids, names, instructions, streams, numbers → `--font-data`.

### Shell architecture (new files under src/components/shell/)

- `AppShell.tsx` — grid: icon rail (56px) | content | optional inspector slot.
  Top edge draggable for Tauri (`data-tauri-drag-region` when `window.__TAURI__`),
  rail gets padding-top ≈40px in Tauri for traffic lights, ≈12px in web.
- `IconRail.tsx` — 5 spaces: Mission Control `/`, Designer `/designer`,
  Library `/library`, History `/history`, System `/system`. SVG glyphs (target,
  diamond/graph, grid, list, gear), 1.5px stroke. Active = amber left hairline +
  filled glyph. Keyboard ⌘1–⌘5. Bottom cluster: theme toggle + locale (moved out
  of primary nav).
- `CommandPalette.tsx` — ⌘K overlay on --surface-overlay; fuzzy match over a
  static registry (routes incl. legacy POC pages, actions: New Playbook,
  Launch…, Toggle theme); ↑↓/j-k navigate, ⏎ run, esc close. Registry in
  `src/lib/commands.ts` — pages can register contextual commands later.
- `Drawer.tsx` — right-side overlay drawer primitive (width 420–560px,
  esc/scrim closes, focus trap, slide ≤150ms, reduced-motion respected).
- `StatusFooter.tsx` — one hairline row: backend base+health dot, scheduler
  state, DB size, version (GET /api/stats + /api/health via existing api.ts).
- `__root.tsx` rewires to AppShell. Old Shell.tsx/NavGroup/Breadcrumb unused by
  the new chrome (leave files until final cleanup wave).

### Routes

New: `/designer`, `/library`, `/history`, `/system` placeholder pages, each with
the teaching empty state (DESIGN.md §6: zero-state shows the command that fills
it). `/` becomes Mission Control skeleton. Legacy routes untouched, just de-navved.
index.html inline script: set `data-theme` from localStorage else **dark** (kill
light-flash). i18n: new `shell.*` strings in en.json + zh.json.

## Wave 1 — Spaces (parallel after Wave 0 merges)

- **A · Mission Control**: attention queue (gates/failures/stalled — invocations
  + runs APIs), live board cards w/ SSE ticker, recent terminal runs, footer.
  Zero-state "all clear".
- **B · Designer canvas (flagship)**: full-bleed reactflow canvas; node palette
  (step/fanout/gate/join/input + **reaction node**); inspector drawer per node —
  step: instruction, agent/model (casts catalog API), tools, **emits grants**
  (typed emission ports rendered on node, colored by category per DESIGN-CANVAS
  §2 table); reaction node: when{type/from/where-predicate} → do{spawn/gate/
  judge/escalate/notify/stop} + guards{max,dedup} (observation patterns);
  signal rails when ≥1 reaction taps a type; YAML source toggle (two-way,
  serializes to DESIGN-CANVAS §5 spec shape); validate + Run (launches API).
  Engine presets open ON the canvas (research/review/coding/hypothesis/planning
  as shipped specs) — replaces the engines modal as the builder.
- **C · Library + History + System**: Library = one catalog (agents/playbooks/
  skills/plugins/engine-defs) filter chips + detail Drawer; History = one
  timeline (runs+invocations+shows merged), day-grouped, strong filters, detail
  w/ DAG + transcripts; System = health/maintenance/schedules/settings.

## Wave 1.5 — Tab IA + Copilot (directives 2026-06-11, λ-led)

- **Tabs, not pages** ("太多pages了，用户用不来的"): the 5 rail spaces are the
  ONLY pages. Everything else is a tab inside a space, rendered as child
  routes so deep-linking still works:
  - Home: Overview | Fleet
  - Library: Agents | Playbooks | Skills | Plugins | Engines
  - History: All | Runs | Invocations | Shows (filters become tabs)
  - System: Health | Maintenance | Schedules | Settings
  Standalone routes (/playbooks, /agents, /skills, /plugins, /schedules,
  /engines, /shows, /invocations list, /admin, /kanban) become redirects
  into their space tab. Shared TabBar component in shell/ — hairline
  underline, amber active indicator, tabs are URLs.
- **Studio Copilot** ("a chatbot that can work this for the user, directly
  in the browser"): right-side chat drawer in the shell (⌘J + rail button),
  persistent across spaces. The copilot operates Studio for the user —
  launch playbooks, create workflows, query history/fleet, run maintenance —
  via tool-calls against the existing studio APIs. Backend: chat session
  endpoint wired to a lionagi Branch with studio tools registered. Frontend:
  streaming chat panel per DESIGN-SYSTEM (drawer contract, mono for
  tool-calls, terminal-state motion).
- **zh quality pass** ("很多中文很奇怪，直接翻译根本就错了"): after all wave
  branches merge, λ rewrites BOTH locale files in one pass — native
  developer-tool register (阿里云/飞书 style), not word-for-word translation.
  No new zh strings ship without reading like product Chinese.
- **Coherence pass**: three agents produced three styling dialects (inline
  px styles vs Tailwind semantic classes vs mixed). Extract shared
  primitives (SectionHead, Badge, EmptyState, DataRow, TabBar) and converge
  every space on them. One dialect everywhere.

## Wave 2 — Live + native + speed

- **Fleet view `/fleet` (directive 2026-06-11, priority)**: "Kanban is for
  tasks, it is not an agent lifecycle monitor — we need a dedicated place to
  watch organizational agent running situations." Fleet is that place: a live
  monitor of active invocations → child sessions/agents (status, model,
  elapsed, branch/message counts), grouped by org unit, drawer detail per
  agent, Mission Control liveness contract (watchdog + hysteresis + three
  states). The runs-status lanes at /kanban are deprecated by Fleet; the
  kanban page is reserved for a future task substrate and must never grow
  lifecycle features.
- Run overlay on canvas (SSE: lifecycle lanes, emission pulses, rail counters,
  spawn materialization); replay scrubber over session_signals.
- Tauri: overlay titlebar w/ inset traffic lights, vibrancy rail, real menus,
  native notifications + dock badge, ⌘1-5/⌘W/⌘,.
- Speed: route-level code splitting (lazy mount Designer/reactflow), list
  virtualization in History, bundle audit, React.memo on canvas nodes,
  SSE backpressure (batch state updates via rAF).
- POC cleanup: delete dead routes/components, then demo-grade click-through.

## Knowledge-grounded upgrades (khive corpus sweep, 2026-06-11 — BINDING)

Distilled from composed corpus briefings (orchestration observability, motion
systems, operator consoles, event-sourcing replay). These refine the waves above.

### Canvas semantics
- Signal envelope is the port schema: every emission carries `id` (per-event),
  `type`, `source`, `time`, `correlation_id` (stable per run). Inspector shows
  the envelope; the run overlay groups lifecycle lanes by `correlation_id`
  (branch/run identity), never by event id.
- Port naming convention is load-bearing: event-output ports use past-tense
  types (`Finding`, `NodeCompleted`); command-input ports use imperatives
  (`SpawnAgent`, `GateExecution`). The grammar tells users what the runtime
  guarantees at each port.
- Two rail types, visually distinct: **keyed rail** (ordered per branch/
  aggregate) vs **fanout rail** (unordered broadcast). Ordering scope is
  architecture — render it.
- **Debounce/collapse node** joins the palette (collapses event storms into one
  downstream emission). Distinct glyph from pass-through reactions.
- Canvas validation warns when a reaction node fans into heavy downstream work
  with no budget guard (event-loop-blocking anti-pattern made visible).

### Motion tokens (extends theme.css)
- Every transition token = `{duration_ms, easing, properties, interrupt_policy,
  reduced_motion_alt}`. `properties` whitelist: **opacity + transform only** on
  any live-updating element (pulses, lanes, spawn materialization).
- Anchors: 100ms pressed-state feedback · 150ms micro-feedback (success/warn)
  · 250ms modal/drawer enter (opacity+transform). Drag/reposition uses spring
  with `interrupt_policy: retarget` (duration-only tokens fail mid-gesture).
- `reduced_motion_alt` mandatory for spatial motion: fade-only + immediate
  focus placement, zero layout shift.
- Signal-category colors and status colors are SEPARATE token families — one
  hue may never mean both a category and an alert. Validate tokens on the real
  dense canvas at 1×, not on swatches.

### Interaction
- Every palette command also reachable via a visible UI path; chord printed
  beside palette rows AND at menu right-edge AND in focus tooltips (mirror
  hover teaching on keyboard focus).
- **Modifier-hold overlay**: holding ⌘ reveals in-place hotkey badges on canvas
  controls — expert rehearsal without mode switching.
- Palette contract: ⌘K opens → focus search → ↑↓ navigate → ⏎ run → esc
  restores prior focus. Node config modals: `<dialog>`+showModal, initial focus
  on least-destructive control, focus returns to the opening node.
- Mission Control attention queue carries an explicit SLO ("queue age < Xs")
  and the display shows its own measurement lag — the dashboard is not the
  sensor. Stale-data badge when SSE silent > 5s.
- Every canvas drag has a single-pointer non-drag alternative (Move affordance
  + undo after commit).

### Replay & live rendering (Wave 2 architecture — BINDING)
- The persisted signal log is the source of truth; live overlay AND replay
  scrubber are projections through **one shared reducer**. Only the input
  offset differs. Reducer must be idempotent (seek overshoot re-applies).
- Scrubber seeks build state in a **shadow projection**, then atomic-swap into
  the render target — never interleave replay writes with live state.
- Ordered playback per branch (`correlation_id` partition); tolerate
  reordering across branches.
- SSE → rAF micro-batches (collect ≤16ms, apply once). Virtualize lanes +
  signal log. Snapshot canvas state every N events so scrubber seeks replay
  only the tail (e.g. snapshot @2000, replay 400 ≈ 60ms vs 360ms full).
- Long-range seeks show progress; never block the UI thread.

### λ direct-read addenda (full briefings re-read 2026-06-11 — BINDING)

Rules the distillation missed; from λ's own read of the composed material.

**Canvas validation (Designer)**
- **Command ports have exactly one owner.** Events fan out; commands do not
  ("do not broadcast a command and hope the right service acts"). A
  command-type signal wired to >1 consumer is a validation warning.
- **Emission-type cycle detection.** Static check: node emits type T → reaction
  taps T → spawns node that emits T = positive feedback loop (failure→retry
  amplification class). Render the loop visibly; require a max/budget guard on
  any reaction inside a cycle before Run is allowed.
- **Spawn materialization anchors to causation.** The envelope carries
  causation (which event triggered this) alongside correlation (which run).
  The spawn animation grows from the causing emission pulse, drawing the
  causal edge — not appearing from nowhere.

**Status semantics (Mission Control + run overlay)**
- **Hysteresis on displayed health states.** Never flap on one bad/good frame:
  stale badge appears after >5s SSE silence and clears only after stable
  resumption (≥2 events or 1s healthy stream). Same dwell rule for run-status
  pills.
- **Stale detection is a client-side watchdog timer** — never inferred from a
  server-sent "healthy" event (a process that can hang can't report its own
  hang).
- **Three distinct non-data states**, visually unmistakable: empty (teaching
  zero-state), stale (pipeline silent — last-updated shown), error (known
  failure). "No data" must never be confusable with "stopped updating."

**Data layer (History + replay)**
- **Immutable history / mutable head split.** Persisted signal-log pages never
  change: fetch once, cache in memory keyed by offset range, never re-poll.
  Only the live edge is polled/streamed. (Same split for run lists: terminal
  runs are immutable.)

**Keyboard & focus (shell-wide)**
- **No shortcut on rare destructive actions** (stop run, delete) — visible
  control + confirm only. One chord per semantic command everywhere; never
  bind chords the OS intercepts.
- **Tokenized focus ring in theme.css**: `:focus-visible` outline 2px offset,
  ≥3:1 contrast against its actual surface, verified per theme separately
  (a ring that passes on light fails silently on dark).

**Motion QA gate**
- **Terminal-state rule**: every animated state must land in a fully usable
  static state if the animation is skipped, interrupted, or reduced-motion
  replaced. Review each transition against this before merge.
- Never name a token after its value (`fast`, `amber`) — name by role/purpose.

## Dogfood loop (every wave)

λ boots `li studio` from the worktree, screenshots every surface (light+dark),
fixes promptly, iterates until sleek. Screenshots →
`.khive/workspaces/20260611/redesign-dogfood/`.
