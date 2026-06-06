# Studio Frontend — Four UX Fixes Summary

**Branch**: `show/lionagi-sweep/studio-frontend` (based on `main`)
**Commit**: `18ab1d3f3` — `feat(studio/frontend): four Studio UX fixes (#1177 #1178 #1179 #1135)`
**Status**: committed locally only — **NOT pushed, no PR opened**.
**Critic verdict** (op-6): **APPROVE** — `CRIT:0 | MAJ:0 | MIN:2` (both MINOR are inherent-design
notes that conform to the contracts; neither blocks).

## Gate results (tester op-5, re-run and confirmed by critic op-6)

| Gate      | Command                           | Result                                                                     |
| --------- | --------------------------------- | -------------------------------------------------------------------------- |
| Lint      | `pnpm lint`                       | **PASS** — exit 0, 0 errors (3 pre-existing warnings, none from this work) |
| Typecheck | `pnpm typecheck` (`tsc --noEmit`) | **PASS** — exit 0, 0 type errors                                           |
| Build     | `pnpm build` (`next build`)       | **PASS** — ✓ Compiled successfully, 18/18 static pages                     |

> Pre-commit prettier hook reformatted the three `.tsx` files + `PERF.md` on first commit attempt;
> changes are formatting-only, were re-staged, and the eslint/prettier hooks both passed on the
> final commit.

---

## #1177 — Action panel auto-synthesize from failed plays

**What changed**: On the Show-detail ACTION panel, when no explicit blocker/next-step is declared
but failed plays exist (`rollup.failed.length > 0`), the panel now synthesizes a concrete next
action — each failed play named with its exit code (`play.meta.exit_code`) and a remediation hint
(exit 124 → "rerun with timeout override", otherwise → "inspect logs for details"). The literal
"No blockers or next action declared in plan." is now only reachable when there are zero failed
plays, so the misleading render is impossible.

**Files touched**: `app/shows/[topic]/page.tsx` (lines ~577–599).

**Gate**: lint 0 errors / typecheck clean / build green.

---

## #1178 — Filter chips show per-status counts

**What changed**: Runs page filter chips now display per-status counts derived from the already-
loaded runs via a `statusCounts` memo (recomputes on `[runs]`, seeds all `STATUS_FILTERS` to 0,
maps canonical DB `"completed"` → `"done"` chip per ADR-0025). Counts render through `Button`'s
existing `trailing` slot and are hidden during skeleton load. Active vs inactive distinction is
provided by the existing `Button variant="toggle"` (filled green vs outlined).

**Files touched**: `app/runs/page.tsx` (`statusCounts` memo ~342–356; chip render + call site).

**Gate**: lint 0 errors / typecheck clean / build green.

---

## #1179 — Play graph: color, click, zoom, critical path

**What changed** (ReactFlow extended, **not** swapped):

- Node color by status — `statusBackground()`: failed/error = red, running = blue,
  merged/completed/done = green, pending/blocked = neutral gray (all 5 CSS vars confirmed present
  in `globals.css`).
- Clickable nodes → inline expand of the matching play's table row (`onNodeClick` → `setExpanded`).
- Zoom controls + fit-to-view via `<Controls>` and `fitView`/`fitViewOptions`; height 220→300px.
- Critical-path highlight — Kahn topo-sort + DP longest-path (`criticalPathEdgeIds()`), critical
  edges restyled (running-blue stroke, width 2).
- `for...of` over Map replaced with `.forEach` for `target: es5`.

**Files touched**: `app/shows/[topic]/components/PlayDag.tsx`.

**Gate**: lint 0 errors / typecheck clean / build green; no graph regression.

---

## #1135 — jsx-a11y ESLint perf baseline

**What changed**: Measurement + doc only (no code behavior change). One-shot n=3 audit of
`pnpm lint` and `pnpm build` with/without the jsx-a11y plugin. Lint overhead ~+1.22s
(enabled 5.16s vs disabled 3.94s); production bundle **byte-identical** at 1,482,391 bytes / 45
files across all conditions (eslint is dev-only). eslint config restored to original
SHA-256 `0bc03ee3ef…` (empty `git diff`).

**Files touched**: `apps/studio/frontend/PERF.md` (new). **See `PERF.md` for the full #1135
baseline numbers, methodology, and raw timings.**

**Gate**: PERF.md present; config restored; lint pass.

---

## Disposition

Four fixes implemented, committed to `show/lionagi-sweep/studio-frontend` as `18ab1d3f3`.
All gates green. **Nothing pushed; no PR opened.**
