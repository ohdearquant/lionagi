# Lion Studio v2 — seed-demo redesign (λ-designed, 2026-06-11)

Mandate: full creative authority — add/remove pages, remove information
display, alter presentation. Fix: some pages information-overloaded, some blank.
Rethink the canvas from scratch. Bar: seed-fundraising demo quality.

## 1. Scale system (the global fix — why every page felt wrong)

The app was built on 9–11px type. Replace with tokens in `theme.css`; purge every
hardcoded `fontSize: 9|10|11` (inline styles) and sub-12px tailwind text classes.

```css
--t-xs: 11px;   /* chips, badges — the FLOOR. nothing below 11px ever */
--t-sm: 12px;   /* secondary, mono data, table meta */
--t-base: 13px; /* body, table cells, inputs */
--t-md: 14px;   /* emphasized body, list titles */
--t-lg: 16px;   /* section titles */
--t-xl: 18px;   /* page titles */
```

Spatial: controls 32px tall (36px primary), card padding 14px, section gap 20px,
pane widths proportional `clamp(360px, 26vw, 480px)`. Bigger window ⇒ bigger
working surfaces, never bigger margins.

Light theme is first-class: secondary text ≥ #3a3d44 on white, hairlines visible
(#d8d6d1), chip fills hold color. Validate on dense tables at 1×, both themes.

## 2. IA — 5 spaces, rail shrinks, nothing exists only to redirect

1. **Command** `/` — Mission Control + Fleet MERGED. Live board (running
   runs/engines w/ progress + agent fleet by session) + attention queue +
   quick-launch. Idle state = recent activity + inviting launch surface, never
   blank. Projects & Teams become tabs here (operational context).
2. **Canvas** `/designer` — the centerpiece. See §3.
3. **Library** — agents, playbooks, skills, plugins, engine defs. Master-detail.
   Skills/plugins detail panes gain invocation stats (count, recent, success).
4. **History** — unified timeline. Run detail gains an **Invocations tab**.
   `/invocations/$id` standalone page DIES (route redirects into History).
5. **Schedules** — kanban board (lanes: upcoming / today / running / done) +
   month calendar toggle. λ-designed.

System/settings → gear at rail bottom, not a primary space. Playfield folds into
Canvas as a secondary tab or dies if redundant. Shows already live in History.

## 3. Canvas rethink (from scratch)

Old framing: "a settings form next to a static diagram." New framing: **the
canvas IS the launch console** — design-by-direct-manipulation:

- **Left rack** (collapsible, 280px): saved engine defs + 5 kind blueprints as
  compact cards with mini topology glyphs. Click = load onto canvas.
- **Center**: topology rendered large (serpentine pipeline, 1:1 zoom cap stays).
  Stages editable IN PLACE: model chip on each stage is a click-to-edit popover
  wired to the real per-stage `models` routing dict; max_depth renders as a
  badge on the loop edge; max_agents as a budget badge on the canvas header.
  The config form dissolves into the diagram.
- **Bottom dock**: prompt bar ("what should this engine do…") + name field +
  Save/Launch buttons. Launch straight from canvas (POST /api/launches/,
  action_kind=engine).
- **Right pane**: request preview + advanced only (export_dir, test_cmd),
  collapsible. Not the primary editing surface anymore.
- Engine truth rules from DESIGN-SYSTEM §12 still bind: real stage names, real
  emission classes, real conditions, honest knobs only.

## 4. Density rebalance (overload ↓, blank ↑)

- Overloaded: System health (progressive disclosure — summary cards first, raw
  tables behind a "details" expander), request-preview JSON walls (collapsed by
  default), History detail meta dumps (group + truncate).
- Blank: every empty state gets (a) one-line what-this-is, (b) primary CTA,
  (c) 2–3 concrete starting points (per taste-rule 4). Fleet-idle shows recent
  sessions instead of a redirect CTA.

## 5. Execution waves

- W1: tokens + global type/spacing sweep + light theme pass. ← biggest visible delta
- W2: IA restructure (rail, Command merge, invocations fold, settings gear).
- W3: Canvas rebuild per §3.
- W4: Schedules kanban/calendar.
- W5: coherence + perf (code-split reactflow chunk) + λ dogfood at 4 widths
  both themes + commit per wave.

Gates per wave: tsc, eslint 0 errors, vitest green, build, restart 8801,
λ screenshots before declaring done. Prettier hook aborts commits while
reformatting — always `git log -1` to verify a commit actually landed.
