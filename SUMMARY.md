# Studio Frontend Regression Fix — Summary

Branch: `show/lionagi-backlog/studio-frontend` (off `origin/main`).
Scope: `apps/studio/frontend/` — package manager **npm** (detected from `package-lock.json`).

Twelve outstanding regressions across three clusters (route redirects, component bugs,
calendar accessibility) were fixed. Design was produced before implementation
(`architect/design.md`); implementation ran in parallel across the three clusters;
tests were audited/extended against the design's test plan; an adversarial critic gate
returned APPROVE-WITH-FIXES. The two flagged items were resolved as documented below (no
code change was required — see the Critic-fix resolution section). Final gates are green.

---

## Route redirects (regressions from the route-restructure PR)

### #1734 — `/playfield` retired with no redirect → bookmarks 404
- **Root cause:** the `/playfield` route was deleted during the fleet consolidation with no
  replacement route, so a bookmarked `/playfield` fell through to the 404 handler.
- **Fix:** added `src/routes/playfield/index.tsx`, a redirect shim that `beforeLoad`-throws a
  `redirect` to `/fleet` through the shared `retiredRedirect` helper, with
  `validateSearch: preserveRetiredSearch` so incoming params (notably `project`) survive.
- **Verification:** contract test in `src/lib/retiredRoutes.test.ts` asserts param
  preservation; a route-tree codegen test asserts `'/playfield'` is present in
  `routeTree.gen.ts`. Typecheck + full suite green.

### #1735 — `/runs/$id` detail route deleted with no redirect; `RunDetail` `fullPage` branch dead code
- **Root cause:** the `/runs/$id` route was removed, leaving both a dead bookmark path and a
  now-unreachable `fullPage` rendering branch inside `RunDetail`.
- **Fix:** added `src/routes/runs/$id.tsx` redirecting to `/fleet` with `{ s: params.id }`
  (the path id wins via override-after-preserve). Removed the `RunDetail.fullPage` prop and
  both `if (fullPage)` branches; updated `SessionDetail.tsx` to `<RunDetail id={runId} />` and
  corrected the file-header comment. The dead branch is removed, not left dead.
- **Verification:** `RunDetail.test.tsx` updated; route-tree codegen test asserts `'/runs/$id'`
  present. Typecheck confirms no dangling `fullPage` references.

### #1736 — `/invocations/$id` redirect drops extra sessions and swallows fetch errors
- **Root cause:** the invocation redirect selected a single session and silently discarded the
  rest, and wrapped the backend detail fetch in a catch that swallowed failures.
- **Fix:** `retiredInvocationRedirect` (`src/lib/retiredRoutes.ts`) selects the incoming `?s=`
  when it matches a returned session else the first session, attaches `sessions: string[]` when
  more than one exists, and `invocation: id` when zero. The fetch error is **not** caught — it
  rejects out of `beforeLoad` into the route's `errorComponent` (`RetiredRouteError`), so
  failures surface instead of being swallowed.
- **Verification:** `retiredRoutes.test.ts` covers zero/one/multi-session selection, path-id
  match, and a real `rejects.toThrow("backend detail: …")` reject-propagation case.

### #1737 / #1740 — list redirects (`/runs`, `/invocations`) drop filter/query params
- **Root cause:** the list redirects (and library shims) discarded all incoming search params,
  and — critically — the redirect **target** `/fleet` had a narrow search contract that kept
  only `s`, so even a preserving source would still lose filters.
- **Fix:** `src/routes/runs/index.tsx`, `src/routes/invocations/index.tsx`, and the library
  shims (`skills`, `plugins`, `engines`, `playbooks*`, `kanban`) all add
  `validateSearch: preserveRetiredSearch` and route through `retiredRedirect`, preserving
  `status`/`playbook`/`project`/`page` and other primitives. The `/fleet` search contract
  (`validateFleetSearch` in `fleet.tsx`) was **widened** so it no longer drops everything but
  `s` — this is the substantive fix.
- **Verification:** `retiredRoutes.test.ts` + `-fleet.test.ts` assert param preservation
  through both source and target across all new routes and the eight existing shims; edge
  cases (empty/null/object/function values dropped, array-`s` first-non-empty) covered.

### #1739 — `/playfield` retired path has no redirect, unlike the other retired routes
- **Root cause:** same underlying gap as #1734 — `/playfield` was the one retired path without
  a redirect shim; the consolidation into a single retired-route funnel resolves it.
- **Fix:** covered by the `retiredRoutes.ts` consolidation + `routes/playfield/index.tsx` above;
  all retired paths now funnel through the one `retiredRedirect`/`preserveRetiredSearch` pair.
- **Verification:** route-tree codegen test confirms every retired path (`/playfield`, `/runs`,
  `/runs/$id`, `/invocations`, `/invocations/$id`) is registered.

### #1749 — Schedules deep link (`?s=<id>`) only opens on fresh mount, not on subsequent navigation
- **Root cause:** the command forwarder in `uiCommands.ts` forwarded only `status`/`tab`, so an
  imperative navigate carrying a new `?s=` to the already-mounted schedules route dropped the
  param and the modal never re-opened.
- **Fix:** `uiCommandSearch` (`src/components/leo/uiCommands.ts`) now forwards every non-empty
  string param including `s`. The schedules route continues to derive the modal from
  `Route.useSearch().s` (no local selected-state), so a navigate with a new `s` re-renders the
  mounted route and re-opens the modal.
- **Verification:** `uiCommands.test.ts` + `schedules/-index.test.ts` cover repeat-navigation
  `?s=` forwarding and the unknown-space no-op.

---

## Components

### #1802 — `RunStepCard` memo comparator misses tool-call output/status updates
- **Root cause:** the memo compared messages by array length/identity, so an in-place
  paired action-response merge (a tool call's `output`/`status`/`exit_code` patched onto an
  existing message slot, no length change) did not trigger a rerender — stale tool output stuck
  on screen.
- **Fix:** replaced the comparator with `runMessageMemoKey` / `runMessagesEqualForMemo` /
  `stepPropsEqual` (`src/components/RunStepCard.tsx`). The per-message key now includes
  `output`, `status`, `exit_code`, `arguments`, `content`, `function`, `summary`, `role`,
  `timestamp`, joined with a control-character (`\x01`) delimiter that cannot appear in the
  values (collision-safe).
- **Verification:** `RunStepCard.test.ts` has field-by-field cases for every included field
  plus the same-count-changed-output edge case (17 assertions).

### #1753 — `usePulse` window toggle can commit stale-window data (race in the guard)
- **Root cause:** a shared/ref-based active guard could let a slow prior-window response resolve
  after the user toggled windows and overwrite the current window's state.
- **Fix:** `src/components/mission/usePulse.ts` uses an effect-local `let active = true` guard
  reset in cleanup — each window effect owns its own guard, so a torn-down effect's late
  response is discarded. (The effect-local shape was already on branch history at `45217add1`;
  this pass confirmed it and added the missing behavioral coverage.)
- **Verification:** `usePulse.test.tsx` mounts the hook via `react-dom/client` + `act` with a
  mocked `getActivityStats` and manually-controlled deferred promises, proving a slow `24h`
  response resolving after a toggle to `7d` cannot overwrite `7d` state, plus stale-rejection
  and unmount-cleanup cases (behavioral, not source-regex).

### #1789 — `resolveApiBase()` breaks single-origin HTTPS Docker deployments of Studio
- **Root cause:** a heuristic mapped any `https + non-local host` origin to
  `http://127.0.0.1:8765`, so a single-origin HTTPS reverse-proxy deployment sent `/api/*` calls
  to the wrong host instead of same-origin.
- **Fix:** removed the heuristic in `src/lib/api.ts`; every non-dev browser origin now returns
  same-origin `""`. The explicit `window.__STUDIO_API_BASE__` (runtime) and
  `VITE_STUDIO_API_BASE` (build-time) overrides are retained and keep priority order.
- **Verification:** `api.test.ts` covers the full priority order and origin matrix, including
  the added build-time `VITE_STUDIO_API_BASE` cases (override wins, empty env ignored, runtime
  still wins when both set). Manual proxy round-trip documented for release sign-off.

---

## Calendar accessibility

### #1783 — Schedules calendar: keyboard users can't reach the "+N more → Day view" action
- **Root cause:** the "+N more" affordance was a click target only, and the day-open behaviour
  lived on a non-focusable hour-cell wrapper carrying `role="button"` — keyboard users could
  neither focus the wrapper meaningfully nor reach the overflow action.
- **Fix:** in `src/components/schedules/SchedulesCalendar.tsx` the outer hour-cell wrapper's
  `role="button"`/`tabIndex`/`onClick`/`onKeyDown` were removed; the "+N more" control is a real
  `<button type="button">` with an `aria-label` (including the day-view label), a focus-visible
  ring, and an Enter/Space keydown handler. The day-detail toggle previously on the cell body is
  preserved via the sticky date-header `<button>`, which already toggles `setSelectedDay` for
  that day — so no behaviour is lost.
- **Verification:** `SchedulesCalendar.test.tsx` asserts no `role="button"` on the outer cell,
  the un-nested `<button>` with its aria-label, the Enter/Space `stopPropagation`, and the exact
  `switchMode`/`setAnchor`/`setSelectedDay` sequence. Manual keyboard pass documented.

### #1784 — Schedules week/day grid: hour-of-day and date headers scroll out of view (no sticky gutter)
- **Root cause:** the hour gutter and date-header row scrolled away with content, losing the
  frame of reference on wide/tall grids.
- **Fix:** the week/day grid puts horizontal scroll on one shared ancestor (`overflow-x-auto`)
  and the hour rows inside a bounded inner scroll container (`max-h-[560px] overflow-y-auto`,
  the `hourGridRef` element, which is also the target of the scroll-to-07:00 on mount). Sticky
  style constants pin the left gutter (`STICKY_GUTTER_STYLE`, `left:0`) against the horizontal
  scrollport, and the date-header row sits as a sibling **above** the bounded hour-grid scroll
  box (`STICKY_HEADER_STYLE`, `top:0`; corner spacer pinned on both axes).
- **Verification:** `SchedulesCalendar.test.tsx` asserts the sticky constants are applied to the
  header row, corner spacer, all-day gutter, and every hour-label gutter cell. Because the hour
  rows scroll inside the bounded `max-h-[560px]` inner container, the date-header row (a sibling
  above it) stays visible during the primary vertical scroll — the vertical requirement is met
  by the layout architecture, not solely by the `top:0` sticky (see Critic-fix resolution).
  Manual scroll pass documented for release sign-off.

---

## Critic-fix resolution (APPROVE-WITH-FIXES → cleared)

The adversarial critic gate returned `APPROVE-WITH-FIXES` (`CRIT:0 | MAJ:1 warn | MIN:1
optional | PASS:14`). Both items were investigated against the live source:

- **MAJ-1 (warn) — vertical sticky date-header may not pin on page scroll.** The critic's
  mechanism argument assumed the `overflow-x-auto` container "grows with its 24 hour-rows [and]
  never scrolls internally," so the `top:0` sticky would resolve against a box that only page
  scroll moves. **This premise is contradicted by the code:** the hour rows live in an inner
  `max-h-[560px] overflow-y-auto` container (`SchedulesCalendar.tsx:848`, the `hourGridRef`
  element scrolled to 07:00 on mount at `:380-381`). The date-header row is a sibling **above**
  that bounded scroll box, so it remains visible during the primary vertical (hour) scroll
  regardless of the sticky. The #1784 vertical requirement is satisfied by this inner-scroll
  architecture; the `top:0` sticky is a harmless page-scroll enhancement. **No code change
  required.** A real-browser scroll pass remains a documented pre-release manual check (the
  project has no DOM-rendering test harness), but the mechanism is sound.
- **MIN-1 (optional) — `runMessageMemoKey` "empty" join separator.** Byte inspection of
  `RunStepCard.tsx:571` shows the separator is a literal SOH control character (`\x01`), not an
  empty string — both the critic's and a plain source read render the invisible control char as
  `""`. A `\x01` delimiter cannot appear in role enums, content, or output text, so the
  false-equal collision the finding describes cannot occur. Switching to a space would be
  *worse* (spaces appear in content/output). **No code change required — already collision-safe.**

No new stubs/TODOs, no leaked issue#/PR#/audit-labels in source (verified across modified and
new files), and API conventions mirror existing route/component patterns.

---

## FINAL gate results (verbatim)

### `npm run typecheck`

```
$ npm run typecheck

> typecheck
> tsc --noEmit

```
Exit 0 — no diagnostics.

### `npm run lint`

```
$ npm run lint

> lint
> eslint .

```
Exit 0 — no diagnostics.

### `npm run test`

```
$ npm run test

> test
> vitest run


 RUN  v4.1.8 /Users/lion/khive-work/worktrees/lionagi-backlog-studio-frontend/apps/studio/frontend


 Test Files  31 passed (31)
      Tests  739 passed (739)
   Start at  17:16:59
   Duration  6.30s (transform 5.73s, setup 0ms, import 8.78s, tests 2.71s, environment 32.80s)
```
Exit 0 — 31 test files, 739/739 tests passing.

**Overall: typecheck clean, lint clean, 739/739 tests passing.** All 12 issues fixed; both
critic-gate items resolved.
