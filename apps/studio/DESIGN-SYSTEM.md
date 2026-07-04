# Lion Studio — Design System (Canonical)

This is the single anti-drift authority for all Studio frontend work. When any
other document, prompt, or existing code disagrees with this file, **this file
wins**. Precedence:

```text
DESIGN-SYSTEM.md  (this file — binding rules, exact values)
  > DESIGN.md            (vision + IA rationale)
  > DESIGN-CANVAS.md     (canvas grammar deep-dive)
  > REDESIGN-EXEC.md     (per-wave execution contract)
  > existing code        (may predate the rules — fix it, don't copy it)
```

Amendments: only via a live dogfood verdict or an explicit owner decision.
Record the change here in the same commit that implements it. Never patch a
rule silently in code.

---

## 1. Principles

1. **Precision instrument, not a dashboard.** Studio is an operator's cockpit
   for agent orchestration. Density over whitespace, hairlines over boxes,
   data over chrome.
2. **Dark-first.** Dark is `:root`, light is the override. Every surface is
   designed and verified in dark, then checked in light.
3. **The canvas is the command-and-control center.** Designer is the
   centerpiece of the app and carries the highest quality bar. Everything
   else supports it.
4. **Validate on real screens.** Contrast, density, and motion are judged on
   dense tables and live boards at 1× — never on swatches or isolated
   components.
5. **Liveness is the product.** Running things visibly run, stale data visibly
   warns, terminal states visibly settle. A static screenshot of Studio should
   look *paused*, not *finished*.
6. **Every zero state invites creation.** A bare glyph and a sentence is a
   failure. Empty screens teach what the space is for and offer a concrete
   first action.

---

## 2. Design tokens

`frontend/src/theme.css` is the **single source of truth**. Tailwind maps
tokens; `globals.css` bridges legacy aliases. Never hardcode a color, and
never name a token after its value (`--gray-700` is forbidden; `--content-muted`
is correct) — tokens name **roles**, so values can move without renames.

### Dark (default, `:root`)

| Token | Value | Role |
|---|---|---|
| `--surface-base` | `#0C0D10` | App background |
| `--surface-raised` | `#13151A` | Rails, panels, cards |
| `--surface-overlay` | `#1A1D24` | Drawers, palette, popovers, active nav |
| `--edge-hairline` | `rgba(255,255,255,0.10)` | Default 1px separators |
| `--edge-strong` | `rgba(255,255,255,0.18)` | Focused/hovered edges, drawer borders |
| `--content-primary` | `#E8E6E1` | Headings, primary data |
| `--content-secondary` | `rgba(232,230,225,0.70)` | Body, labels |
| `--content-muted` | `rgba(232,230,225,0.50)` | Hints, timestamps, inactive icons |
| `--accent` | `#E8A33D` | Lion amber — attention + primary actions ONLY |
| `--status-running` | `#5BA8F5` | Live/in-flight |
| `--status-success` | `#4FB477` | Terminal success |
| `--status-pending` | `#E8A33D` | Queued/waiting/gated |
| `--status-failure` | `#E5604C` | Terminal failure |

**Contrast floor (binding, from live dogfood verdict 2026-06-11):** dark
`content-secondary` alpha ≥ 0.70, `content-muted` ≥ 0.50, hairline ≥ 0.10.
The originally spec'd 0.55/0.35 failed live ("too dark, not readable").
Do not lower these for aesthetics.

### Light (`[data-theme="light"]`)

Overrides **surfaces and content only**: base `#F5F5F3`, raised `#FFFFFF`,
overlay `#EEEEED`; hairline `rgba(0,0,0,0.07)`, strong `0.13`; primary
`#18181B`, secondary alpha `0.68`, muted `0.50`. **Status colors and accent
never change between themes** — a status color that shifts meaning per theme
is a bug.

### Signal category palette (canvas rails, badges, filters)

| Category | Signals (examples) | Hue |
|---|---|---|
| discovery | Finding, Conflict, Gap, Diagnosis, Synthesis | cyan |
| judgement | Verdict, RiskAssessment, Objection, Recommendation | amber |
| analysis | AnalysisResult, ComplexityScore | violet |
| planning | ExecutionPlan, TaskAssignment, DesignSpec | blue |
| production | ArtifactProduced, VerificationResult, Document | green |
| retrospective | Proposal, Postmortem | slate |
| universal | EscalationRequest, SpawnRequest | red, pulsing |

Category hues are a **separate family** from status colors — never reuse a
status token for a category or vice versa. Universal signals are the only
ones that may pulse at rest.

---

## 3. Typography & numbers

- `--font-ui` (system sans / Inter): labels, headings, prose.
- `--font-data` (JetBrains Mono / SF Mono): **all data** — IDs, durations,
  counts, timestamps, paths, model names, code, system prompts.
- If a value could appear in a log line, it renders in mono. No exceptions.
- Numeric columns and tickers use `tabular-nums` so digits don't jitter.
- Scale stays small and tight: 11–13px for data/tables, 13–14px body, 16–20px
  headings. No display typography inside the app shell.

## 4. Layout & spacing

- 8pt grid; 4px only for intra-component micro-spacing.
- **Hairlines over boxes**: separate regions with 1px `--edge-hairline`, not
  filled containers. Cards are reserved for genuinely discrete objects
  (runs, agents, nodes).
- Shell: 56px icon rail · 24px status footer · content fills the rest.
- **The window is the canvas** (binding, 2026-06-11): a space fills the
  window at every size. Centered `max-w-*` single columns inside a space are
  a rejected pattern — they waste the main body on wide windows. Width is
  absorbed by panes and grids (`minmax()` columns, `auto-fit` card grids,
  master-detail splits), never by growing side margins. Narrow-form
  exception: a focused create/edit form may cap its own line length, but
  the page around it still fills.
- **Master-detail over drawers** (binding, 2026-06-11 — supersedes the
  earlier "drawers over pages" rule): inspecting an object must not require
  an open/close ceremony. Lists live in a master pane; detail is a sibling
  pane of the main body, always visible, driven by selection (first row
  auto-selected). Use the shared `SplitPane` primitive: hairline divider,
  drag-resizable, width persisted per surface, collapses to stacked
  list→detail with a back affordance below 900px container width. Overlay
  drawers remain only for transient cross-space work surfaces (Copilot
  chat, command palette) — never for object detail or configuration.
- **Modals are dead.** A cramped centered modal is "outdated, out of place".
  Use the detail pane, a full-height editor view, or inline expansion. The
  command palette is the only floating layer.

## 5. Color semantics

- **Amber discipline**: `--accent` marks the one primary action per screen
  and active-state indicators (rail's 2px left bar). Never decorative, never
  on two competing elements at once.
- Status dots/badges are the liveness vocabulary: running = `--status-running`
  with pulse, pending = amber static, success/failure = terminal static.
- Meaning never travels by color alone — pair with a glyph or label
  (color-blind safety, and zh labels are wider than en).

## 6. Iconography (one SVG contract)

Every icon in the app — chrome, body, canvas, empty states — follows one
contract. Style drift between chrome and content reads as "two different
apps".

```text
viewBox="0 0 24 24"  fill="none"  stroke="currentColor"
strokeWidth="1.5"    strokeLinecap="round"  strokeLinejoin="round"
```

- Rendered at 20px in the rail, 16px inline, 14px in dense tables.
- **Literal shapes, not abstractions**: a clock for History, a node graph for
  Designer, a grid for Library. If a first-time user can't name the icon's
  object, redraw it.
- One optical weight everywhere — no mixing filled and stroked sets, no
  importing a second icon library. Icons live as local components
  (see `components/shell/IconRail.tsx` for the reference style).
- Always `aria-hidden="true"` with the accessible name on the interactive
  parent.

## 7. Motion

Motion tokens follow the schema
`{duration_ms, easing, properties, interrupt_policy, reduced_motion_alt}`.

- Anchors: **100ms** (hover/press feedback) · **150ms** (drawers, palette,
  reveals) · **250ms** (layout shifts, canvas pan/zoom settles).
- Animate **opacity and transform only** — never width/height/top/left in
  hot paths.
- Interrupt policy: user-driven motion retargets (spring) rather than
  restarting; drags never animate against the pointer.
- **Terminal-state rule**: when something reaches a terminal state
  (success/failure/cancelled), all of its motion stops. A pulsing
  finished run is a lie.
- `prefers-reduced-motion`: every animation declares its reduced alternative
  (usually instant + opacity). Pulses become static dots.

## 8. Keyboard & focus

- ⌘1–⌘5 switch spaces (client-side `navigate`, never a full reload).
  ⌘K opens the palette anywhere.
- Palette: typing always types (no bare-letter nav bindings); vim j/k only
  with Ctrl held; first guard in keydown is `if (e.isComposing) return;`
  (IME safety for zh input).
- **No keyboard shortcut may trigger a destructive action** (delete, cancel
  run, discard). Destructive actions require a pointer + confirm affordance.
- Focus ring: tokenized `:focus-visible` only — 2px ring, 2px offset, ≥3:1
  contrast against its surface, verified in both themes. Never
  `outline: none` without a replacement.
- Drawers and the palette trap focus, restore it on close, close on Esc.
- Every drag interaction has a keyboard/pointer-click equivalent.

## 9. Status, liveness & staleness

- **Hysteresis**: a live view shows a stale badge after >5s of event silence;
  the badge clears only after ≥2 consecutive healthy responses (or 1s of
  healthy stream) — never on the first packet after a gap.
- **Client-side watchdog**: staleness is decided by a local timer, never by
  trusting a server-sent "healthy" flag.
- **Empty, stale, and error are three visually distinct states.** Empty
  invites creation; stale shows last-known data dimmed + badge; error states
  the failure and offers retry. Rendering them alike is a bug.
- Running items tick: elapsed durations update every second (mono,
  tabular-nums). Frozen timers on live items are a defect.

## 10. Zero states

Structure, in order: literal glyph (contract §6) → one sentence of what the
space is for → primary CTA (amber, creates the thing) → ⌘K hint or example.
"No items yet." with no action is a rejected pattern. The CTA should land the
user in the canvas or a creation flow, not on documentation.

## 11. Component patterns

- **Tables**: hairline row separators only, mono for ids/durations/counts,
  status dot leftmost, row click selects into the sibling detail pane
  (§4 master-detail) — never an overlay. Virtualize past ~200 rows.
- **Cards**: only for discrete live objects; status edge or dot, ticking
  duration, hover raises to `--edge-strong`.
- **Editors use space generously**: in any agent/playbook editor the system
  prompt is the **dominant element** — full-height, mono, resizable; metadata
  fields are a compact sidebar/header around it. A system prompt in a
  3-line textarea is a rejected pattern.
- **Forms**: labels above fields, hairline inputs on `--surface-raised`,
  validation inline below the field in `--status-failure`.
- **Command palette** is the universal entry point: every navigable space,
  every creatable object, every toggle is registered with en + zh labels and
  keywords. New surfaces ship with their palette entries in the same PR.
- **Tabs, not pages** (binding, 2026-06-11): the five rail spaces are the
  only pages. New surfaces are tabs inside a space — child routes under the
  space's URL so they stay deep-linkable — never new standalone pages. The
  shared TabBar: text tabs on a hairline baseline, 2px amber underline on
  the active tab, content-muted inactive, no boxes or pills.

## 12. Canvas (Designer)

**Engine truth** (binding, 2026-06-11 — supersedes the freeform-workflow
grammar of DESIGN-CANVAS.md §2/§5): the canvas renders what
`lionagi/engines/` actually executes, nothing else. An engine is a *kind*
(research · review · coding · hypothesis · planning) whose pipeline —
stages, roles, reactions, judge gate, budget — is fixed in that kind's
`_run()`; users configure the knobs the backend persists
(`name, kind, model, max_depth, max_agents, options.test_cmd,
options.export_dir, description`). Rules:

1. **No invented vocabulary.** Node kinds, emission names, and reaction
   labels come from the engine sources (e.g. `FindingEmitted`,
   `IssueFound → adversarial verify`, plan→implement→test→fix-loop→verify).
   Freeform step graphs, "join"/"debounce" nodes, and emission grants that
   `engine_defs` cannot persist are rejected fictions.
2. **Topology is data, not drawing.** Each kind's pipeline lives in one
   λ-reviewed catalog (`lib/designer/topology.ts`); the canvas auto-lays it
   out. Users never wire engine edges by hand.
3. **Nodes are information-dense, inline.** A node shows its stage name,
   role, resolved model (per-stage override or engine default), real
   emission type names, and budget/threshold annotations — on the node
   face. Hover/selection may add emphasis, but no fact may exist *only*
   behind a click-open panel.
4. **Configuration is a persistent pane**, always visible beside the canvas
   (§4 master-detail) — not a disappearing inspector. Selecting a node
   scrolls/highlights the relevant knobs; deselecting hides nothing.
5. **Honest knobs only.** Editable fields are exactly the persisted
   definition fields; engine-internal defaults (dimensions, thresholds,
   roles) render as read-only "default" facts so the diagram stays truthful
   without pretending they are saved.
6. Run overlay and replay render through **one shared idempotent reducer** —
   live and replayed events must produce pixel-identical boards.

## 13. Data layer

- **Immutable history / mutable head split**: terminal runs, finished
  invocations, and closed segments are cached forever (`staleTime:
  Infinity`); only the live edge polls or streams. Refetching terminal data
  is wasted work and visible jank.
- Stream rendering micro-batches through rAF (≤16ms budget per flush);
  snapshots every N events so replay is snapshot + tail, never full scan.
- Rebuilds happen in a **shadow projection with atomic swap** — users never
  watch a board tear down and re-fill.

## 14. Voice & i18n

- Terse operator language: "3 running · 1 gated", not "You currently have…".
  Sentence case everywhere; no exclamation marks in chrome.
- **en + zh ship together** — every new string lands in both
  `messages/en.json` and `messages/zh.json` in the same commit, namespaced by
  feature (`mission.*`, `designer.*`, `library.*`, …). A hardcoded UI string
  is a gate failure.
- Layouts are verified with zh strings (often wider per-glyph, shorter
  overall) before merge.

## 15. Performance budgets

- Initial route bundle code-split per space; no space pays for the canvas
  unless it renders it.
- 60fps during canvas pan/zoom and live-board updates; any list >200 rows is
  virtualized; any interaction >100ms gets immediate visual feedback.
- `npm run build` warnings on chunk size are treated as failures to
  investigate, not noise.

## 16. Review checklist (run before every merge)

```text
□ Tokens only — no hardcoded colors, no value-named tokens
□ Dark verified on dense real screens at 1×; light spot-checked
□ Contrast floor respected (secondary ≥0.70, muted ≥0.50 in dark)
□ Icons follow the §6 contract — one set, literal shapes
□ All strings in en.json + zh.json; layout holds in zh
□ Empty/stale/error are distinct; zero states invite creation
□ Running items pulse + tick; terminal items are still
□ No modal where a drawer/full-height view belongs
□ System prompt (if present) is the dominant editor element
□ No destructive keyboard shortcut; focus rings tokenized + visible
□ Palette entries registered for new surfaces/actions
□ npm gates green: lint, typecheck, test, build — then a human clicks
  through the running app before calling it done
```

The last line is the real gate: green CI does not certify interaction
quality. Someone opens the app and uses the feature before it merges.
